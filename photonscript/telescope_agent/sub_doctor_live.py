"""The telescope agent's side of the sub doctor (scheduler/sub_doctor.py).

after_sub(agent, night) runs once after each graded light is appended to
the subs log: it gathers the live context (armer mode, PHD2 state and RMS,
the active PHD2 calibration, the last NINA AF report and focuser
temperature, autofocus stall events, the rig's last plate solve), asks
sub_doctor.diagnose, keeps the state / run event through sub_doctor.record,
pushes once per cause per rig per night and runs an opt-in remedy:

    stop_phd2      sub_doctor_stop_phd2_unguided: PHD2 stop_capture on an
                   unguided night (RC16 agent only; never while another
                   PhotonScript actor holds PHD2, e.g. the PS-93
                   calibration manager, nor while PHD2 is calibrating)
    run_af         sub_doctor_request_af: ninaAPI focuser auto-focus, only
                   when the camera is idle at the check (retried on the
                   next sub otherwise)
    shorten_subs   sub_doctor_shorten_subs: the drift cap for the RC16's
                   next dispatch (sub_doctor.set_cap / apply_caps); nothing
                   is re-dispatched here

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
    if out.get("remedy"):
        await remedy(agent, night, rig, diag, out["remedy"])
    return diag


async def remedy(agent, night: str, rig: str, diag: dict, action: dict) -> bool | None:
    """Run one opt-in remedy; returns ok, or None when it was not tried
    (left for a later sub)."""
    from photonscript.scheduler import sub_doctor as sd
    config = agent.config
    aid = str(action.get("id") or "")
    try:
        if aid == "stop_phd2":
            return await _stop_phd2(agent, night, rig, action)
        if aid == "run_af":
            return await _run_af(agent, night, rig, action)
        if aid.startswith("shorten_subs:"):
            s = float(aid.split(":", 1)[1])
            if rig != "rc16":
                sd.note_remedy(config, night, rig, action, False,
                               "Piggy-600 sub length is the companion's; not capped")
                return False
            c = sd.set_cap(config, night, rig, s, diag.get("headline") or "")
            sd.note_remedy(config, night, rig, action, True,
                           f"RC16 subs capped at {c['exposure_s']:g} s from the "
                           "next dispatch (nothing re-dispatched now)")
            return True
    except Exception as e:  # noqa: BLE001
        logger.warning("sub doctor remedy %s failed: %s", aid, e)
        sd.note_remedy(config, night, rig, action, False, f"error: {e}")
        return False
    return None


async def _stop_phd2(agent, night: str, rig: str, action: dict) -> bool | None:
    from photonscript.scheduler import sub_doctor as sd
    from photonscript.telescope_agent import phd2_ops
    config = agent.config
    if rig != "rc16":
        return None                       # the RC16 agent owns PHD2 actions
    if sd.armed_mode(config) != "unguided":
        sd.note_remedy(config, night, rig, action, False,
                       "not stopped: the night is no longer armed unguided")
        return False
    try:
        async with phd2_ops.hold("sub_doctor", wait_s=0):
            st = str(await agent.phd2.get_app_state() or "")
            if st.lower() == "calibrating":
                return None               # try again after the calibration
            if st.lower() in ("stopped", "unknown", ""):
                sd.note_remedy(config, night, rig, action, True,
                               f"PHD2 already {st or 'unknown'}")
                return True
            await agent.phd2.stop_capture()
    except phd2_ops.PHD2Busy as e:
        logger.info("sub doctor: PHD2 busy (%s), stop left for a later sub", e.owner)
        return None
    sd.note_remedy(config, night, rig, action, True,
                   f"PHD2 stopped (was {st}) on an unguided night")
    try:
        from photonscript.shared.pushover import notify
        await notify(config, f"Sub doctor stopped PHD2 ({st}) on the unguided night; "
                     "the mount now tracks on TPoint + ProTrack.",
                     title=f"{sd.ALERT_TITLE}: remedy", priority=0)
    except Exception:  # noqa: BLE001
        pass
    return True


async def _run_af(agent, night: str, rig: str, action: dict) -> bool | None:
    from photonscript.scheduler import sub_doctor as sd
    config = agent.config
    try:
        cam = await agent.nina.get_camera_info()
    except Exception:  # noqa: BLE001
        return None
    st = str((cam or {}).get("CameraState") or "").lower()
    if not isinstance(cam, dict) or cam.get("IsExposing") or st in ("exposing", "download",
                                                                    "downloading"):
        return None                       # a frame is in flight: next sub
    await agent.nina.start_autofocus()
    sd.note_remedy(config, night, rig, action, True,
                   "NINA autofocus requested (ninaAPI focuser auto-focus)")
    return True
