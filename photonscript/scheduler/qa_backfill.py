"""PS-71 backfill: re-grade a night's lights for roof-closed / parked
signatures and move the rejects out of the stack set.

DRY-RUN BY DEFAULT: `regrade_parked(config, date)` only reports what it would
change. With apply=True it rewrites <date>_subs.jsonl (passed_qa=False, the
reason, qa_flag) and moves any Library hardlink of a newly rejected sub to
Library/_rejected/<target>/<filter>/ (a rename, so Syncthing ships a move and
the original capture is never touched), then re-syncs goal progress.

Metrics come from the stored sub records (the grader already measured them);
exposure start comes from the FITS DATE-OBS when the file is readable, else
the record time. Unsafe windows come from safety_history.jsonl, else the
Pushover audit (notifications.jsonl), plus any explicit --unsafe windows.
Manual verdicts (manual_qa) are never overridden. Never un-rejects.

CLI: photonscript qa-backfill --date 2026-09-26 [--apply]
     [--unsafe 2026-09-27T11:39:44Z/2026-09-27T13:00:00Z]
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def _start_of(rec: dict):
    """Exposure start (naive UTC): DATE-OBS from the FITS when readable;
    else a backfill record's time (it IS DATE-OBS); else a live record's
    time (written at grading, just after readout) minus the exposure."""
    from photonscript.shared.qa_signatures import exposure_start
    p = rec.get("abs_path")
    if p:
        try:
            from astropy.io import fits as _fits
            d = _fits.getheader(p).get("DATE-OBS")
            if d:
                return exposure_start(d)
        except Exception:  # noqa: BLE001 - file gone / not on this machine
            pass
    # PS-162: start_utc, else `time` read by its convention (shared.sub_time)
    from photonscript.shared.sub_time import sub_start
    return sub_start(rec)


def _library_links(config, rec: dict) -> list[Path]:
    """Existing Library hardlinks of this sub (any target folder)."""
    from photonscript.scheduler.runs import library_root, _safe_name
    lib = library_root(config)
    name = Path(str(rec.get("abs_path") or rec.get("file") or "")
                .replace("\\", "/")).name
    if not name or not lib.exists():
        return []
    fdir = _safe_name(rec.get("filter", "?"))
    out = []
    for tdir in lib.iterdir():
        if not tdir.is_dir() or tdir.name.lower() in (
                "calibration", "piggyback", "_rejected", "_analysis"):
            continue
        cand = tdir / fdir / name
        if cand.exists():
            out.append(cand)
    return out


def regrade_parked(config, date: str, apply: bool = False,
                   extra_unsafe: list[tuple] | None = None) -> dict:
    from photonscript.scheduler.runs import (edit_subs, library_root,
                                             sync_goal_progress)
    from photonscript.shared.qa_rules import record_fwhm
    from photonscript.shared.qa_signatures import parked_frame_verdict
    from photonscript.shared.rigs import rig_config
    from photonscript.shared.safety_history import unsafe_windows

    day = datetime.fromisoformat(date)
    span = (day + timedelta(hours=12), day + timedelta(hours=40))
    wins, source = unsafe_windows(config, *span, extra=extra_unsafe)
    # PS-140: a metadata pass, under the night lock from load to rewrite
    with edit_subs(config, date, write=apply) as subs:
        changes, moves = [], []
        for rec in subs:
            if not rec.get("passed_qa") or rec.get("manual_qa"):
                continue
            cfg = rig_config(config, rec.get("rig") or "rc16")
            start = _start_of(rec)
            v = parked_frame_verdict(
                cfg, hfr_px=rec.get("hfr"),
                # live and PS-83 records carry a real FWHM; older backfill
                # ones carry HFR x scale (qa_rules.record_fwhm)
                fwhm_arcsec=record_fwhm(rec),
                background=rec.get("background"), exp_s=rec.get("exp_s"),
                stars=rec.get("stars"), start_utc=start, unsafe_windows=wins)
            if not v.reject:
                continue
            links = _library_links(config, rec)
            changes.append({"file": rec.get("file"), "rig": rec.get("rig"),
                            "start_utc": start.isoformat() + "Z" if start else None,
                            "filter": rec.get("filter"), "exp_s": rec.get("exp_s"),
                            "reasons": v.reasons, "flag": v.flag,
                            "library_links": [str(x) for x in links]})
            if apply:
                rec["passed_qa"] = False
                rec["reason"] = "; ".join(filter(None, [rec.get("reason"),
                                                        *v.reasons]))
                rec["qa_flag"] = v.flag
                rec["regraded"] = "PS-71"
                lib = library_root(config)
                for src in links:
                    dest = lib / "_rejected" / src.relative_to(lib)
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        if dest.exists():
                            src.unlink()
                        else:
                            os.replace(src, dest)
                        moves.append(f"{src} -> {dest}")
                    except OSError as e:
                        logger.warning("PS-71 library move %s failed: %s", src, e)
    if apply and changes:
        sync_goal_progress(config)
    result = {"date": date, "mode": "apply" if apply else "dry-run",
              "unsafe_windows": [(a.isoformat() + "Z", b.isoformat() + "Z")
                                 for a, b in wins],
              "unsafe_source": source, "rejects": changes,
              "library_moves": moves}
    logger.info("PS-71 regrade %s (%s): %d rejects, %d library moves",
                date, result["mode"], len(changes), len(moves))
    return result
