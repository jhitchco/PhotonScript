"""PS-188: file calibration now, not only at dawn.

    file_now(config, rigs=None, days=None, dry_run=False, regrade=False,
             rejudge=False, measure_new=True, source="manual") -> dict
    last_report(config) -> dict | None
    format_report(rep) -> str
    filing_tick(config, state, prev) / filing_loop(config_fn, armer_fn)

Jeremy 2026-10-10: "can we think about getting calibration pictures in
the right place?" PS-178's calibration sweep (runs.link_recent_calibration)
only ran in the dawn pass. The night of 2026-10-09 was disarmed for a
manual TPoint run, so no dawn filing ran and the RC16 2026-09-27 300 s
darks (24, QA pass, a calibration-only folder) were still in the NINA watch
dir, 13 days old, one day from leaving the 14 day sweep window. Meanwhile
the QA store still carried the pre-PS-182 verdicts of the HCG darks (the
LCG dark-current limit), so frames that pass today sat in the quarantine.

file_now() is the same filing on demand, for both rigs:

  1. per rig, every calibration folder of the last `days`
     (CAL_SWEEP_DAYS, at most library_cal_days) goes through
     runs._link_calibration_night: QA first (cached measurements; a frame
     never measured is measured unless measure_new is off; regrade
     re-measures), failing frames to the quarantine, the rest hardlinked
     into Library/Calibration. A frame whose quarantine copy exists is left
     where it is unless rejudge.
  2. rejudge (optional): calibration_qa.rejudge judges the rig's whole
     store by today's rule and moves filed frames to match (quarantine ->
     Library for frames that pass now).
  3. the READOUTM fill (calibration_qa.fill_missing_readouts, header reads).
  4. the Piggy-600 dawn plan when piggyback_dark_capture_window is
     dawn-roof-closed (calibration_window.defer).

It returns, per rig, what was filed, quarantined and left (with the
reason), saves the report as data_dir/calibration_filing.json and is run by
POST /api/calibration/file, `photonscript calibration-file`, the capture
job's end (calibration_capture._finish) and filing_loop: at arm time and
when a night ends without a dawn pass (armer RUNNING / PAUSED / WATCHING
-> IDLE / COMPLETE / ERROR), when calibration_file_auto is on. While the
armer images, new frames are not measured (the grading owns the CPU):
they stay "QA pending" for the dawn sweep. Never touches a camera or NINA.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

REPORT_NAME = "calibration_filing.json"
IMAGING_STATES = ("RUNNING", "PAUSED_UNSAFE", "PAUSED_OPERATOR", "WATCHING")
ENDED_STATES = ("IDLE", "COMPLETE", "ERROR", "DISARMED")
DETAIL_MAX = 400   # frames listed per rig and outcome in the saved report

_lock = threading.Lock()


def _rig_targets(config, rigs) -> list[tuple[str, object]]:
    """[(rig, rig config view)] for the rigs to file."""
    from photonscript.shared.rigs import PIGGYBACK, rig_config, rig_ids
    out = []
    for rg in (rigs or rig_ids(config)):
        if rg == PIGGYBACK:
            if PIGGYBACK not in rig_ids(config) or not getattr(
                    config, "piggyback_image_watch_dir", ""):
                continue
            out.append((rg, rig_config(config, PIGGYBACK)))
        else:
            out.append((rg, config))
    return out


def file_now(config, *, rigs=None, days: int | None = None, dry_run: bool = False,
             regrade: bool = False, rejudge: bool = False,
             measure_new: bool = True, source: str = "manual",
             save: bool = True) -> dict:
    """File every recent calibration folder of `rigs` (default both) now.
    See the module doc. Never raises per rig: a failing rig carries
    "error"."""
    from photonscript.scheduler import calibration_qa as cq
    from photonscript.scheduler.runs import (CAL_SWEEP_DAYS, _link_calibration_night,
                                             calibration_nights, library_root)
    cal_days = int(getattr(config, "library_cal_days", 120))
    days = min(int(days or CAL_SWEEP_DAYS), cal_days)
    rep = {"generated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
           "source": source, "dry_run": dry_run, "regrade": regrade,
           "rejudge": rejudge, "measure_new": measure_new, "days": days,
           "qa_mode": cq.mode(config), "rigs": {}}
    if not _lock.acquire(blocking=False):
        rep["error"] = "a calibration filing is already running"
        return rep
    try:
        for rg, view in _rig_targets(config, rigs):
            try:
                rep["rigs"][rg] = _file_rig(
                    config, view, rg, days=days, dry_run=dry_run, regrade=regrade,
                    rejudge=rejudge, measure_new=measure_new,
                    nights_fn=calibration_nights, link_fn=_link_calibration_night,
                    lib=library_root(view))
            except Exception as e:  # noqa: BLE001 - one rig must not hide the other
                logger.warning("calibration filing failed for %s", rg, exc_info=True)
                rep["rigs"][rg] = {"error": f"{type(e).__name__}: {e}"}
        try:
            rep["dawn_plan"] = _piggy_dawn_plan(config, dry_run=dry_run)
        except Exception as e:  # noqa: BLE001
            rep["dawn_plan"] = {"error": f"{type(e).__name__}: {e}"}
        rep["line"] = summary_line(rep)
        if save and not dry_run:
            _save(config, rep)
        logger.info("Calibration filing (%s): %s", source, rep["line"])
        return rep
    finally:
        _lock.release()


def _file_rig(config, view, rig: str, *, days, dry_run, regrade, rejudge,
              measure_new, nights_fn, link_fn, lib) -> dict:
    from photonscript.scheduler import calibration_qa as cq
    out: dict = {"library": str(lib), "watch_dir": str(view.image_watch_dir)}
    if rejudge and cq.mode(config) != "off":
        rj = cq.rejudge(config, rig, dry_run=dry_run)   # base config: rig_view inside
        out["rejudge"] = {"changed": rj["changed"], "restored": rj["restored"][:DETAIL_MAX],
                          "quarantined": rj["quarantined"][:DETAIL_MAX],
                          "n_restored": len(rj["restored"]),
                          "n_quarantined": len(rj["quarantined"])}
    watch = Path(view.image_watch_dir)
    nights = nights_fn(watch, days)
    detail: list = []
    linked = already = 0
    for d in nights:
        n_l, n_s = link_fn(view, watch, lib, d, rig=rig, detail=detail,
                           measure_new=measure_new, recheck=regrade, dry_run=dry_run,
                           hold_quarantined=not rejudge)
        linked += n_l
        already += n_s
    out["nights"] = nights
    out["already_there"] = already
    for kind in ("filed", "quarantined", "left"):
        rows = [r for r in detail if r["outcome"] == kind]
        out[f"n_{kind}"] = len(rows)
        out[kind] = rows[:DETAIL_MAX]
    out["left_reasons"] = _count(r["reason"] for r in detail if r["outcome"] == "left")
    out["outside_window"] = _outside_window(view, watch, days)
    if not dry_run:
        n = int(getattr(config, "calibration_readout_fill_at_dawn", 400) or 0)
        out["readout_fill"] = cq.fill_missing_readouts(config, rig, n) if n else None
    return out


def _count(items) -> dict:
    c: dict = {}
    for x in items:
        c[x] = c.get(x, 0) + 1
    return c


def _outside_window(view, watch: Path, days: int) -> list:
    """Calibration folders older than the sweep window but still inside
    library_cal_days that hold frames not in the Library: they are only
    filed with a longer --days."""
    from photonscript.scheduler.runs import calibration_nights, library_root
    cal_days = int(getattr(view, "library_cal_days", 120))
    if days >= cal_days:
        return []
    inside = set(calibration_nights(watch, days))
    lib = library_root(view)
    out = []
    for d in calibration_nights(watch, cal_days):
        if d in inside:
            continue
        root = watch / d
        missing = 0
        for f in root.rglob("*.fits"):
            parts = [p.upper() for p in f.relative_to(root).parts]
            typ = next(({"BIASES": "BIAS", "BIAS": "BIAS"}.get(p, p.rstrip("S"))
                        for p in parts if p in ("BIAS", "BIASES", "DARK", "DARKS",
                                                "FLAT", "FLATS")), None)
            if typ and not (lib / "Calibration" / typ / d / f.name).exists():
                missing += 1
        if missing:
            age = (datetime.now() - datetime.strptime(d, "%Y-%m-%d")).days
            out.append({"date": d, "age_days": age, "not_in_library": missing,
                        "action": f"photonscript calibration-file --days {age + 1}"})
    return out


def _piggy_dawn_plan(config, *, dry_run: bool) -> dict | None:
    """piggyback_dark_capture_window dawn-roof-closed: save the Piggy-600's
    owed darks as its deferred dawn plan (calibration_window), which
    dawn_tick starts when calibration_dawn_capture is on. None otherwise."""
    if dark_capture_window(config) != "dawn-roof-closed":
        return None
    from photonscript.shared.rigs import PIGGYBACK, rig_ids
    if PIGGYBACK not in rig_ids(config):
        return None
    from photonscript.scheduler.calibration import dark_quota, night_dark_exposures
    from photonscript.shared.rigs import rig_config
    pcfg = rig_config(config, PIGGYBACK)   # the companion's own view
    owed = []
    for e in night_dark_exposures(pcfg, PIGGYBACK):
        need = dark_quota(pcfg, PIGGYBACK, e)["need"]
        if need:
            owed.append([float(e), int(need)])
    if not owed:
        return {"darks": [], "note": "no Piggy-600 darks owed"}
    from photonscript.scheduler import calibration_window as cw
    w = cw.dawn_window(config, PIGGYBACK)
    reason = ("piggyback_dark_capture_window dawn-roof-closed: the Piggy-600 has no "
              "shutter and the dome leaks daylight, so these need the lens cap on")
    if dry_run:
        return {"darks": owed, "window": w, "would_defer": True}
    cur = cw.deferred(config, PIGGYBACK) or {}
    if cur.get("darks") == owed and not cur.get("attempted"):
        return {"darks": owed, "window": cur.get("window"), "unchanged": True}
    rec = cw.defer(config, PIGGYBACK, owed, 0, reason, window=w)
    return {"darks": owed, "window": rec.get("window"), "deferred": True}


DARK_WINDOWS = ("unsafe-night", "dawn-roof-closed", "manual")


def dark_capture_window(config) -> str:
    """piggyback_dark_capture_window, normalized (unknown = unsafe-night)."""
    v = str(getattr(config, "piggyback_dark_capture_window", "unsafe-night")
            or "unsafe-night").strip().lower()
    return v if v in DARK_WINDOWS else "unsafe-night"


def piggy_dark_action(config) -> str:
    """The completeness 'action' for Piggy-600 darks that are short.
    PS-189: with the Wanderer Cover enabled the closed cover is the
    shutter, so the window does not apply (the companion and a daytime
    capture job shoot them with the cover closed)."""
    if getattr(config, "piggyback_cover_enabled", False):
        return ("the Piggy-600 cover is its shutter (PS-189): the companion's "
                "OSC_DARKS_COVER_CLOSED fills them before the night's lights "
                "whatever the roof, or start a capture job (rig piggyback) any "
                "time, daytime included; it closes the cover first")
    w = dark_capture_window(config)
    base = ("the Piggy-600 has no shutter and the dome leaks daylight (RC16 900 s "
            "day dark 5661 ADU over bias), so its darks are shot only with the roof "
            "closed at night or with the lens cap on")
    if w == "unsafe-night":
        return (f"{base}: the companion's OSC_DARKS_IF_UNSAFE fills them on the next "
                "roof-closed night (cooler gate at the setpoint, needs the safety "
                "monitor in the NINA #2 profile); or cap the lens and start a capture "
                "job (rig piggyback)")
    if w == "dawn-roof-closed":
        return (f"{base}: saved as the Piggy-600 dawn plan (after shutdown, roof "
                "closed): put the lens cap on; it starts at dawn when "
                "calibration_dawn_capture is on, else start the capture job by hand")
    return (f"{base}: piggyback_dark_capture_window is manual: cap the lens and start "
            "a capture job (rig piggyback, the owed lengths)")


def summary_line(rep: dict) -> str:
    parts = []
    for rg, r in (rep.get("rigs") or {}).items():
        if r.get("error"):
            parts.append(f"{rg} failed ({r['error']})")
            continue
        txt = (f"{rg}: {r.get('n_filed', 0)} filed, {r.get('n_quarantined', 0)} "
               f"quarantined, {r.get('n_left', 0)} left")
        rj = r.get("rejudge")
        if rj:
            txt += (f", re-judge restored {rj['n_restored']} / quarantined "
                    f"{rj['n_quarantined']}")
        if r.get("outside_window"):
            txt += f", {len(r['outside_window'])} older folder(s) outside the window"
        parts.append(txt)
    pre = "Calibration filing" + (" (dry run)" if rep.get("dry_run") else "")
    return f"{pre}: " + ("; ".join(parts) if parts else "no rig to file")


def format_report(rep: dict) -> str:
    lines = [summary_line(rep), f"  ({rep.get('generated')}, source {rep.get('source')}, "
             f"last {rep.get('days')} days, QA mode {rep.get('qa_mode')})"]
    if rep.get("error"):
        lines.append(f"  error: {rep['error']}")
    for rg, r in (rep.get("rigs") or {}).items():
        lines.append("")
        if r.get("error"):
            lines.append(f"{rg}: failed: {r['error']}")
            continue
        lines.append(f"{rg}: {len(r.get('nights') or [])} folder(s) swept, "
                     f"{r.get('already_there', 0)} already in the Library ({r.get('library')})")
        rj = r.get("rejudge")
        if rj:
            lines.append(f"  re-judge: {rj['changed']} verdict(s) changed, "
                         f"{rj['n_restored']} back into the Library, "
                         f"{rj['n_quarantined']} to the quarantine")
            for k, why in rj["restored"][:20]:
                lines.append(f"    restored {k} (was: {why})")
            for k, why in rj["quarantined"][:20]:
                lines.append(f"    quarantined {k}: {why}")
        for kind in ("filed", "quarantined", "left"):
            rows = r.get(kind) or []
            if not rows:
                continue
            lines.append(f"  {kind} {r.get('n_' + kind, len(rows))}:")
            groups: dict = {}
            for x in rows:
                k = (x["type"], x["date"], x.get("reason") or "")
                groups[k] = groups.get(k, 0) + 1
            for (t, d, why), n in sorted(groups.items()):
                lines.append(f"    {t:<5} {d} x{n}" + (f": {why}" if why else ""))
        for o in r.get("outside_window") or []:
            lines.append(f"  folder {o['date']} ({o['age_days']} d) outside the window: "
                         f"{o['not_in_library']} frame(s) not in the Library; {o['action']}")
    dp = rep.get("dawn_plan")
    if dp:
        lines.append("")
        lines.append(f"Piggy-600 dawn plan: {dp}")
    return "\n".join(lines)


def _path(config) -> Path:
    return Path(getattr(config, "data_dir", "") or ".") / REPORT_NAME


def _save(config, rep: dict) -> None:
    try:
        p = _path(config)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rep, indent=1, default=str), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        logger.warning("calibration filing report not saved: %s", e)


def last_report(config) -> dict | None:
    try:
        return json.loads(_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --- automatic runs: arm time, night end, capture job end ---------------------

def auto_enabled(config) -> bool:
    return bool(getattr(config, "calibration_file_auto", True))


def trigger(prev: str | None, state: str) -> str | None:
    """The filing source for an armer state change, or None."""
    if prev is None or prev == state:
        return None
    if state == "ARMED":
        return "arm"
    if prev in IMAGING_STATES and state in ENDED_STATES:
        return "night ended"
    return None


def run_auto(config, source: str, state: str = "", rigs=None) -> dict | None:
    """One automatic filing (never raises). New frames are not measured
    while the armer images."""
    if not auto_enabled(config):
        return None
    try:
        return file_now(config, rigs=rigs, source=source,
                        measure_new=state not in IMAGING_STATES)
    except Exception:  # noqa: BLE001
        logger.warning("automatic calibration filing (%s) failed", source, exc_info=True)
        return None


async def filing_loop(config_fn, armer_fn, tick_s: float = 30.0) -> None:
    """Watch the armer state; file at arm time and when a night ends."""
    prev = None
    while True:
        try:
            state = str(armer_fn().state)
            src = trigger(prev, state)
            prev = state
            if src:
                await asyncio.to_thread(run_auto, config_fn(), src, state)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.debug("calibration filing loop tick failed", exc_info=True)
        await asyncio.sleep(tick_s)
