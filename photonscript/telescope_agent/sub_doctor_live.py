"""The telescope agent's side of the sub doctor (scheduler/sub_doctor.py).

after_sub(agent, night) runs once after each graded light is appended to
the subs log: it gathers the live context (armer mode, PHD2 state and RMS,
the active PHD2 calibration, the last NINA AF report and focuser
temperature, autofocus stall events, the rig's last plate solve), asks
sub_doctor.diagnose, keeps the state / run event through sub_doctor.record
and pushes once per cause per rig per night.

Diagnosis only (2026-10-13, one actor for remedies): the doctor's own
opt-in remedies (PHD2 stop_capture, ninaAPI autofocus, the caps.json sub
cap) are retired. The night pilot (scheduler/night_pilot.py) reads the
doctor's run events and is the only thing that acts on them, so a PHD2
stop, an AF or a sub cap is never sent by two actors.

Never raises; a failure costs the diagnosis, never the sub.
"""
from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def _phd2_live(agent) -> dict:
    g = getattr(getattr(agent, "state", None), "guiding", None)
    if g is None:
        return {}
    st = getattr(g, "state", "")
    st = str(getattr(st, "value", st) or "").lower()
    rms = (g.rms_total_arcsec if getattr(g, "units", "arcsec") == "arcsec"
           and getattr(g, "samples", 0) else None)
    return {"state": st or None, "rms_arcsec": rms, "snr": getattr(g, "snr", None),
            "hfd_px": getattr(g, "hfd_px", None)}


def _focus(agent, config, night: str, rig: str) -> dict:
    from photonscript.shared import phd2_store as store
    from photonscript.shared.night_events import events_path
    from photonscript.shared.sub_time import parse_utc
    reps = list((getattr(agent, "_af_reports", None) or {}).values())
    reps = [r for r in reps if parse_utc(r.get("Timestamp"))]
    last = max(reps, key=lambda r: parse_utc(r.get("Timestamp")), default=None)
    foc = getattr(agent, "_focuser_info", None) or {}
    stall = [e for e in store.read_jsonl(events_path(config, night))
             if e.get("kind") == "af_stall" and (e.get("rig") or "rc16") == rig]
    return {"last_af_utc": (parse_utc(last.get("Timestamp")).isoformat() + "Z"
                            if last else None),
            "last_af_temp_c": (last or {}).get("Temperature"),
            "temp_c": foc.get("Temperature"), "af_known": bool(reps),
            "af_stall": stall[-1] if stall else None}


def _pointing(agent, config, night: str, rig: str, last: dict) -> dict | None:
    """The PS-67 pointing record of the last sub (pier, ha_h), else the
    mount's live pier side."""
    try:
        from photonscript.shared import pointing as pt
        from photonscript.shared.sub_file import norm_file
        rec = pt.load(config, night).get((rig, norm_file(last.get("file"))))
        if rec:
            return rec
    except Exception:  # noqa: BLE001
        pass
    side = getattr(getattr(agent, "state", None), "mount_side_of_pier", None)
    return {"pier": side} if side else None


async def _guider_start(agent, config, night: str, rig: str, ctx: dict) -> dict | None:
    """2026-10-13: what started PHD2 on an unguided night (the unguided
    guard's detect run event; the RC16 agent also reads NINA #1's event
    history when PHD2 is active). None when not unguided or unknown."""
    from photonscript.scheduler import sub_doctor as sd
    if ctx.get("armed") != "unguided":
        return None
    try:
        from photonscript.shared import phd2_store as store
        from photonscript.shared.night_events import events_path
        run_events = store.read_jsonl(events_path(config, night))
    except Exception:  # noqa: BLE001
        run_events = []
    hist = None
    active = str((ctx.get("phd2") or {}).get("state") or "") in sd.ACTIVE_GUIDE
    if rig == "rc16" and active and getattr(agent, "nina", None) is not None:
        try:
            hist = await agent.nina.get_event_history()
        except Exception:  # noqa: BLE001
            hist = None
    try:
        return sd.guider_start_at(hist, run_events, None)
    except Exception:  # noqa: BLE001
        return None


async def after_sub(agent, night: str | None) -> dict | None:
    """Diagnose the rig's last subs after a new one was graded."""
    from photonscript.scheduler import phd2_calibration as pc
    from photonscript.scheduler import sub_doctor as sd
    from photonscript.scheduler import sub_pairs as sp
    from photonscript.scheduler.runs import _load_subs
    config = agent.config
    if not night or sd.mode(config) == "off":
        return None
    rig = getattr(agent, "rig", "rc16")
    try:
        subs = _load_subs(config, night)
        window = sp.rig_subs(subs, rig)[-sd.window_n(config):]
        if not window:
            return None
        cache = getattr(agent, "_sd_solve", None)
        if not cache or cache[0] != night:
            agent._sd_solve = cache = (night, sd.latest_solve(config, night, rig))
        try:
            cal = pc.load_active(config, seed=False)
        except Exception:  # noqa: BLE001
            cal = None
        ctx = {"armed": sd.armed_mode(config), "phd2": _phd2_live(agent),
               "calibration": cal, "focus": _focus(agent, config, night, rig),
               "solve": cache[1], "now": datetime.utcnow().isoformat() + "Z"}
        ctx["guider_start"] = await _guider_start(agent, config, night, rig, ctx)
        # 2026-10-14: guider_conflict needs PHD2 really guiding (the guide
        # log over the window); the pier side / HA name a tracking trail
        ctx["guide_log"] = sd.guide_log(config, night, window)
        ctx["pointing"] = _pointing(agent, config, night, rig, window[-1])
        # 2026-10-23: optics vs tracking compares exposures across nights
        ctx["history_subs"] = sd.history_subs(config, night)
        diag = sd.diagnose(config, rig, window, subs=subs, ctx=ctx)
        out = sd.record(config, night, rig, diag)
    except Exception as e:  # noqa: BLE001
        logger.warning("sub doctor skipped: %s", e)
        return None
    if diag:
        logger.info("sub doctor %s: %s (%s) %s", rig, diag["cause"],
                    diag["confidence"], diag["headline"])
    if out.get("push"):
        try:
            from photonscript.shared.pushover import notify
            await notify(config, out["push"],
                         title=f"{sd.ALERT_TITLE}: {sd._label(config, rig)}",
                         priority=1 if diag["cause"] in ("guider_conflict", "focuser")
                         else 0)
        except Exception as e:  # noqa: BLE001
            logger.warning("sub doctor push failed: %s", e)
    return diag
