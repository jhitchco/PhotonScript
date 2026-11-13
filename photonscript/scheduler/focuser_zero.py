"""Focuser reporting position 0: focus unknown, the light is not usable.

2026-10-08 about midday the RC16's ZWO EAF dropped off USB. NINA #1 kept
reporting the focuser Connected with Position 0 (StepSize 600000, a frozen
Temperature), so every RC16 light after that has FITS FOCPOS = 0 (the
morning lights before: 5837 to 5857), autofocus started 55 times and never
finished, and the subs came out at about 5" FWHM. Neither rig ever focuses
at 0 (RC16 about 5600 to 6100, Piggy-600 about 10900 to 11100), so a 0 is a
dead focuser, not a position (Jeremy 2026-10-11: "if the focuser reports 0
it's no good").

Pieces (the per-sub check itself is shared.qa_rules.focuser_zero_check, row
focuser_zero, judged by both graders and qa-rescore):

    is_zero_reading(focuser) -> bool        a NINA focuser/info payload at 0
    alert_text(config, rig, focuser, target)  the live page (telescope agent
                                            _focuser_zero_watch, one per rig
                                            per night, focuser_zero_alert)
    scan_library(lib, since, until, rig)    READ ONLY header scan of a
                                            Library (the desktop mirror):
                                            LIGHTs with FOCPOS exactly 0
    summarize(hits)                         grouped by rig, target, night
    night_pass(config, date, apply, rig)    the verdict-writing path: swap
                                            the focuser_zero row of each
                                            stored scorecard of a night's
                                            subs log (runs.edit_subs, the
                                            same path as the PS-13 / PS-67
                                            dawn passes) and move newly
                                            rejected subs out of the Library.
                                            Human verdicts are never changed.

CLI: photonscript qa-focuser-zero --since 2026-10-08 [--rig rc16]
     [--library PATH] [--apply]   (dry run unless --apply; --apply works on
     the machine that holds the subs logs, i.e. the scope PC)
"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

logger = logging.getLogger(__name__)

ALERT_TITLE = "PhotonScript focuser at 0"
LIGHT_EXT = (".fits", ".fit", ".fts")


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def is_zero_reading(focuser: dict | None) -> bool:
    """A NINA focuser/info payload reporting Position exactly 0 while not
    explicitly disconnected (the 2026-10-08 failure said Connected=true)."""
    f = focuser or {}
    if f.get("Connected") is False:
        return False
    return _num(f.get("Position")) == 0


def alert_text(config, rig: str, focuser: dict | None, target=None) -> str:
    from photonscript.shared.rigs import rig_label
    f = focuser or {}
    name = str(f.get("DisplayName") or f.get("Name") or "the focuser")
    what = f" ({target})" if target and target != "?" else ""
    return (f"{rig_label(config, rig)}: focuser {name} reports position 0 while "
            f"imaging{what}. NINA still shows it connected, but a 0 means the "
            "focuser dropped off USB or froze: focus is unknown, autofocus "
            "cannot finish, and every light shot now is rejected (FOCPOS 0). "
            "Check the focuser USB cable and power, reconnect it in NINA "
            "(Equipment > Focuser) and rerun autofocus.")


# ------------------------------------------------------- Library scan (RO)

def _rig_of(frame) -> str:
    return "piggyback" if frame.is_osc else "rc16"


def _target_of(rel: Path, frame) -> str:
    """The Library target folder (Library/<Target>/<Filter>/file), else the
    header OBJECT; "_rejected/<Target>/..." names the target too."""
    parts = rel.parts
    if len(parts) >= 3 and parts[0].lower() == "_rejected":
        return parts[1]
    if len(parts) >= 2 and not parts[0].startswith("_"):
        return parts[0]
    return frame.object or "?"


def scan_library(lib, since: str = "", until: str = "", rig: str = "",
                 *, read=None, tz: str = "America/Denver") -> list[dict]:
    """Every LIGHT under `lib` whose FOCPOS header is exactly 0, nights in
    [since, until] (evening dates, inclusive; '' = open), optionally one rig.
    Header-only reads; never writes. A missing FOCPOS is not a hit."""
    from photonscript.integration.frames import read_frame
    from photonscript.shared.qa_rules import is_light_type
    read = read or read_frame
    lib = Path(lib)
    out: list[dict] = []
    if not lib.is_dir():
        return out
    for p in sorted(lib.rglob("*")):
        if not p.is_file() or p.suffix.lower() not in LIGHT_EXT:
            continue
        rel = p.relative_to(lib)
        if rel.parts and rel.parts[0].lower() == "calibration":
            continue
        try:
            fr = read(p, tz=tz)
        except Exception as e:  # noqa: BLE001 - unreadable header
            logger.debug("focuser-zero scan: %s unreadable (%s)", p, e)
            continue
        if not is_light_type(fr.kind) or fr.focpos is None or fr.focpos != 0:
            continue
        r = _rig_of(fr)
        if rig and r != rig:
            continue
        if since and fr.night and fr.night < since:
            continue
        if until and fr.night and fr.night > until:
            continue
        out.append({"path": str(p), "file": p.name, "rel": rel.as_posix(),
                    "rig": r, "instrument": fr.instrument,
                    "target": _target_of(rel, fr), "night": fr.night,
                    "filter": fr.filter, "exp": fr.exp,
                    "in_rejected": bool(rel.parts) and rel.parts[0].lower()
                    in ("_rejected", "bad")})
    return out


def summarize(hits: list[dict]) -> list[dict]:
    """[{rig, instrument, target, night, n, in_rejected}] sorted by night."""
    c: Counter = Counter()
    rej: Counter = Counter()
    for h in hits:
        k = (h["rig"], h.get("instrument") or "", h["target"], h["night"])
        c[k] += 1
        rej[k] += int(bool(h.get("in_rejected")))
    return [{"rig": k[0], "instrument": k[1], "target": k[2], "night": k[3],
             "n": n, "in_rejected": rej[k]}
            for k, n in sorted(c.items(), key=lambda kv: (kv[0][3], kv[0][0],
                                                          kv[0][2]))]


# ------------------------------------------------- subs log verdict pass

def _record_focpos(rec: dict):
    """(focpos, source): the record's own FOCPOS (PS-164), else the FITS
    header when the file is readable here, else (None, None)."""
    if "focpos" in rec and rec.get("focpos") is not None:
        return rec["focpos"], "record"
    p = rec.get("abs_path")
    if p:
        try:
            from astropy.io import fits as _fits
            from photonscript.shared.optics_state import optics_fields
            fp = optics_fields(_fits.getheader(p))["focpos"]
            if fp is not None:
                return fp, "header"
        except Exception:  # noqa: BLE001 - file gone / not on this machine
            pass
    return None, None


def night_pass(config, date: str, apply: bool = False, rig: str = "") -> dict:
    """Re-judge one night's subs log for FOCPOS 0 (dry run unless apply).
    Swaps only the focuser_zero row of each stored scorecard
    (qa_rules.regrade_focuser_zero); a record without a scorecard (pre
    PS-21) is rejected directly. Human verdicts are never changed (listed
    as kept). A newly rejected sub leaves the Library (links move to
    Library/_rejected/), then goal progress re-syncs."""
    from photonscript.scheduler.pointing_record import _move_out_of_library
    from photonscript.scheduler.runs import (_human_verdict, edit_subs,
                                             runs_dir, sync_goal_progress)
    from photonscript.shared import qa_rules
    out = {"date": date, "mode": "apply" if apply else "dry-run",
           "subs": 0, "zero": 0, "newly_rejected": 0, "already_rejected": 0,
           "human_kept": 0, "records_updated": 0, "library_moves": 0,
           "unknown_focpos": 0, "files": []}
    if not (runs_dir(config) / f"{date}_subs.jsonl").exists():
        out["missing_log"] = True
        return out
    with edit_subs(config, date, write=apply) as subs:
        for r in subs:
            rr = r.get("rig") or "rc16"
            if rig and rr != rig:
                continue
            out["subs"] += 1
            fp, src = _record_focpos(r)
            if fp is None:
                out["unknown_focpos"] += 1
                continue
            if _num(fp) != 0:
                continue
            out["zero"] += 1
            human = _human_verdict(r)
            was = bool(r.get("passed_qa"))
            row = {"file": r.get("file"), "rig": rr,
                   "target": r.get("target"), "filter": r.get("filter"),
                   "focpos_src": src,
                   "verdict_before": "passed" if was else "rejected",
                   "action": "kept (human verdict)" if human else
                   ("reject" if was else "add reason")}
            out["files"].append(row)
            if human:
                out["human_kept"] += 1
                continue
            t = qa_rules.thresholds(config, rr, r.get("target"), r.get("filter"))
            if not was:
                out["already_rejected"] += 1
            elif t.get("focuser_zero_mode", "fail") == "fail":
                out["newly_rejected"] += 1
            else:
                row["action"] = f"{t.get('focuser_zero_mode')} only"
            if not apply:
                continue
            fields = qa_rules.regrade_focuser_zero(r, t, focpos=fp)
            if fields is None and not r.get("scorecard"):
                why = qa_rules.FOCUSER_ZERO_WHY + " (FOCPOS header 0)"
                fields = {"passed_qa": False,
                          "reason": "; ".join(x for x in (
                              r.get("reason"), why) if x),
                          "focpos": int(_num(fp))}
            if fields is None:
                continue
            if not fields.get("reviewed") and r.get("review_source") == "auto":
                fields.update(reviewed=False, review_source=None)
            r.update(fields)
            if r.get("review_source") is None:
                r.pop("review_source", None)
            r["regraded"] = "focuser-zero"
            out["records_updated"] += 1
            if was and not r.get("passed_qa"):
                out["library_moves"] += len(_move_out_of_library(config, r))
    if apply and out["records_updated"]:
        sync_goal_progress(config)
    logger.info("focuser-zero %s (%s): %d of %d subs at FOCPOS 0, %d newly "
                "rejected", date, out["mode"], out["zero"], out["subs"],
                out["newly_rejected"])
    return out


def log_nights(config, since: str = "", until: str = "") -> list[str]:
    """Nights (YYYY-MM-DD) that have a subs log here, in [since, until]."""
    from photonscript.scheduler.runs import runs_dir
    out = []
    for p in sorted(runs_dir(config).glob("*_subs.jsonl")):
        d = p.name[:10]
        if since and d < since:
            continue
        if until and d > until:
            continue
        out.append(d)
    return out
