"""Pulse-path self-test (PS-92): does the mount actually move on a guide pulse?

On 2026-09-18, -25 and -26 PHD2 sent full-length pulses the Paramount did not
follow (RA commanded 64"/min, the star moved 0.9"/min). No PHD2 tuning helps
if corrections never reach the mount, so this is tested before guiding
starts instead of being found in the morning log.

run_selftest(config, context, slot):
  * preconditions: PHD2 Stopped or Looping (Guiding only from the NINA slot,
    which then stops it: NINA's next item is StartGuiding); the mount tracking,
    not parked, not slewing; Dec / pier side / altitude from NINA;
  * scale from config at PHD2's binning (a PHD2 profile that disagrees is
    reported, not used); guide speed from NINA (GuideRate*ArcsecPerSec), else
    the newest PHD2 guide-log header, else guide_rate_sidereal x 15.041"/s;
  * per axis a pulse that should move the star about selftest_step_px
    (100 to 2000 ms); with PHD2 looping: W x3, E x3, N x3, S x3, each
    measured on its own: save_image before, guide_pulse, wait the pulse plus
    two new frames, save_image after, register the two frames (FFT phase
    correlation, hot-pixel map masked), so drift stays well under a pixel;
  * verdict (shared.guide_motion.selftest_verdict): PASS when every
    direction moves 0.5 to 1.5x of expected and W/E, N/S oppose; WARN above
    1.5x (guide-rate mismatch, PS-89); FAIL under 0.5x or not opposing;
    INCONCLUSIVE with under 3 stars, not tracking, a timeout or NINA / PHD2
    unreachable;
  * exit: twilight slot -> PHD2 stopped; target slot -> looping on a vetted
    real star (PS-91) for NINA's StartGuiding; manual -> as found.

Holds the phd2_ops lock ("selftest") and uses its own short-lived PHD2
connection. Results go to <data_dir>/phd2/selftest.jsonl. On FAIL: one
Pushover per night (selftest-<night>) naming the likely causes, then
armer.fallback_unguided only when selftest_on_fail = "unguided" (default
"alert" until PS-85).

record_passive_fail(): the RC16 agent's passive checks (after a meridian
flip, and the guard's D4 on a real star) feed the same record and alert.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from photonscript.shared import guide_motion as gm
from photonscript.shared import phd2_store as store
from photonscript.telescope_agent import phd2_ops

logger = logging.getLogger(__name__)

DIRECTIONS = ("W", "E", "N", "S")
PASSIVE_WINDOW_S = 180.0

LIKELY_CAUSES = [
    "Bisque ASCOM driver: 'use DirectGuide' unchecked (TheSky drops ASCOM "
    "PulseGuide)",
    "Bisque ASCOM driver: 'Can Get Pointing State' off",
    "TheSky scripting / TCP server not enabled",
    "TheSky autoguide rate zero or not what PHD2 assumes",
    "mount not tracking",
]


def _pier(v):
    from photonscript.telescope_agent.agent import _pier_side
    return _pier_side(v)


def _f(v):
    try:
        x = float(v)
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def speed_from_log(config) -> tuple[float | None, float | None]:
    """(RA, Dec) guide speed "/s from the newest PHD2 guide-log header."""
    try:
        from photonscript.scheduler import phd2_logs as pl
        from pathlib import Path
        files = pl.find_logs(config, "guide")["files"]
        for p in reversed(files[-3:]):
            secs = pl.parse_guide_log(Path(p).read_text(encoding="utf-8",
                                                        errors="replace"), Path(p).name)
            for s in reversed(secs):
                h = s.get("header") or {}
                if h.get("ra_guide_speed"):
                    return _f(h.get("ra_guide_speed")), _f(h.get("dec_guide_speed"))
    except Exception as e:  # noqa: BLE001
        logger.debug("guide speed from log failed: %s", e)
    return None, None


def guide_speeds(config, mount: dict) -> dict:
    """{"ra", "dec" ("/s), "source", "zero"}: NINA first, then the PHD2 log,
    then guide_rate_sidereal x sidereal. "zero" when NINA reports 0."""
    ra = _f((mount or {}).get("GuideRateRightAscensionArcsecPerSec"))
    dec = _f((mount or {}).get("GuideRateDeclinationArcsecPerSec"))
    zero = (ra == 0.0 or dec == 0.0)
    if ra and dec and ra > 0 and dec > 0:
        return {"ra": ra, "dec": dec, "source": "nina", "zero": False}
    lra, ldec = speed_from_log(config)
    if lra and lra > 0:
        return {"ra": lra, "dec": ldec or lra, "source": "phd2 log", "zero": zero}
    k = float(getattr(config, "guide_rate_sidereal", 0.5) or 0.5)
    return {"ra": k * gm.SIDEREAL_ARCSEC_S, "dec": k * gm.SIDEREAL_ARCSEC_S,
            "source": f"config ({k:g} x sidereal)", "zero": zero}


def _result(verdict, reasons, **kw) -> dict:
    out = {"verdict": verdict, "reasons": list(reasons)}
    out.update(kw)
    return out


def cached_pass(config, night: str, pier: str | None) -> dict | None:
    """Tonight's PASS / WARN on this pier side, if any (the NINA slots skip
    the test then)."""
    for r in reversed(store.selftest_results(config, night=night)):
        if r.get("kind", "active") != "active":
            continue
        if r.get("verdict") in ("PASS", "WARN") and (r.get("pier_side") == pier
                                                     or pier is None):
            return r
    return None


async def _grab(client):
    from photonscript.telescope_agent.guide_guard import load_fits
    path = await client.save_image()
    try:
        img, _ = await asyncio.to_thread(load_fits, path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return img


async def run_selftest(config, context: str = "manual", slot: str = "manual", *,
                       nina=None, client=None, clock_scale: float = 1.0) -> dict:
    """Run (or skip) the self-test, record it, alert on FAIL. Never raises."""
    now = datetime.utcnow()
    night = store.night_of(config, now)
    if context == "nina" and not getattr(config, "phd2_selftest_enabled", False):
        return _result("SKIPPED", ["phd2_selftest_enabled is off"], cached=False)
    own_nina = nina is None
    if own_nina:
        from photonscript.telescope_agent.nina_client import NinaClient
        nina = NinaClient(config.nina_base_url)
    mount_err = ""
    try:
        mount = await nina.get_mount_info()
    except Exception as e:  # noqa: BLE001
        mount = None
        mount_err = f"NINA unreachable ({e})"
    finally:
        if own_nina:
            try:
                await nina.close()
            except Exception:  # noqa: BLE001
                pass
    pier = _pier((mount or {}).get("SideOfPier"))
    if context == "nina" and mount:
        hit = cached_pass(config, night, pier)
        if hit:
            return _result(hit["verdict"], ["already passed tonight on the "
                                            f"{pier or '?'} pier side"],
                           cached=True, pier_side=pier)
    timeout = float(getattr(config, "selftest_timeout_s", 240) or 240)
    try:
        async with phd2_ops.hold("selftest", wait_s=10):
            if mount is None:
                res = _result("INCONCLUSIVE", [mount_err])
            else:
                res = await asyncio.wait_for(
                    _run(config, context, slot, mount, client, clock_scale), timeout)
    except phd2_ops.PHD2Busy as e:
        res = _result("INCONCLUSIVE", [str(e)])
    except asyncio.TimeoutError:
        res = _result("INCONCLUSIVE", [f"timed out after {timeout:g} s"])
    except Exception as e:  # noqa: BLE001
        logger.warning("pulse self-test errored: %s", e)
        res = _result("INCONCLUSIVE", [f"{type(e).__name__}: {e}"])
    res.setdefault("pier_side", pier)
    rec = {"kind": "active", "night": night, "t_utc": store.iso_z(now),
           "context": context, "slot": slot, **res}
    store.append_jsonl(store.selftest_path(config), rec)
    logger.info("pulse self-test (%s/%s): %s %s", context, slot, res["verdict"],
                "; ".join(res.get("reasons", [])))
    if res["verdict"] == "FAIL":
        await _on_fail(config, night, rec)
    return rec


async def _run(config, context, slot, mount, client, clock_scale) -> dict:
    from photonscript.telescope_agent.phd2_client import PHD2Client
    if not mount.get("Connected", True):
        return _result("INCONCLUSIVE", ["mount not connected in NINA"])
    if mount.get("AtPark"):
        return _result("INCONCLUSIVE", ["mount parked"])
    if mount.get("Slewing"):
        return _result("INCONCLUSIVE", ["mount slewing"])
    if not mount.get("Tracking", mount.get("TrackingEnabled", False)):
        return _result("INCONCLUSIVE", ["mount not tracking"])
    dec = _f(mount.get("Declination")) or 0.0
    info = {"dec_deg": round(dec, 2), "pier_side": _pier(mount.get("SideOfPier")),
            "alt_deg": _f(mount.get("Altitude"))}
    own = client is None
    task = None
    if own:
        client = PHD2Client(config.phd2_host, config.phd2_port, config=config)
        if not await client.connect():
            return _result("INCONCLUSIVE", ["PHD2 not reachable"], **info)
        task = await client.start_event_loop()
    try:
        return await _measure(config, client, context, slot, dec, info, mount,
                              clock_scale)
    except asyncio.CancelledError:
        # the hard timeout: never leave PHD2 looping at the twilight slot
        if slot == "twilight":
            try:
                await asyncio.wait_for(client.stop_capture(), 5)
            except Exception:  # noqa: BLE001
                pass
        raise
    finally:
        if own:
            await client.disconnect()
            if task:
                task.cancel()


async def _measure(config, client, context, slot, dec, info, mount,
                   clock_scale) -> dict:
    from photonscript.telescope_agent import guide_hotpix
    from photonscript.telescope_agent.guide_guard import detect_stars, hotpix_mask
    from photonscript.telescope_agent.phd2_client import guide_scale_from_config
    notes = []
    state = await client.get_app_state()
    if state in ("Guiding", "Calibrating", "LostLock", "Paused"):
        if context != "nina":
            return _result("INCONCLUSIVE", [f"refused: PHD2 is {state} (only the "
                                            "NINA slot may stop guiding)"], **info)
        await client.stop_capture()
        notes.append(f"stopped PHD2 ({state}) for the test")
        state = "Stopped"
    binning = await client.get_camera_binning() or 1
    scale = guide_scale_from_config(config, binning)
    if not scale:
        return _result("INCONCLUSIVE", ["guide pixel scale unknown (config)"], **info)
    try:
        prof = await client.call("get_pixel_scale")
        prof = _f(prof)
        if prof and abs(prof - scale) / scale > 0.25:
            notes.append(f"PHD2 profile says {prof:.3f}\"/px, config optics give "
                         f"{scale:.3f}\"/px: using config")
    except Exception:  # noqa: BLE001
        prof = None
    sp = guide_speeds(config, mount)
    exp_ms = await client.get_exposure() or 2000
    frame_wait = max(10.0, 3.0 * exp_ms / 1000.0 + 5.0)
    started = state != "Looping"
    if started:
        await client.loop()
    if not await client.wait_frames(2, timeout=frame_wait):
        return _result("INCONCLUSIVE", ["PHD2 delivered no frames"], **info)
    hotpix = guide_hotpix.load(config)
    before = await _grab(client)
    mask = hotpix_mask(before.shape, hotpix, binning)
    n_stars = len(await asyncio.to_thread(detect_stars, before, mask))
    base = dict(info, scale_arcsec_px=round(scale, 4), binning=binning,
                profile_scale=prof, stars=n_stars, notes=notes,
                speeds={"ra": round(sp["ra"], 3), "dec": round(sp["dec"], 3),
                        "source": sp["source"]})
    if n_stars < 3:
        await _exit_state(client, slot, started, hotpix)
        return _result("INCONCLUSIVE", [f"only {n_stars} star(s) in the guide "
                                        "frame (need 3)"], **base)
    return await _pulses(config, client, slot, started, hotpix, before, mask,
                         dec, scale, sp, base, frame_wait, clock_scale)


async def _pulses(config, client, slot, started, hotpix, before, mask, dec, scale,
                  sp, base, frame_wait, clock_scale) -> dict:
    steps_n = max(1, int(getattr(config, "selftest_steps", 3) or 3))
    step_px = float(getattr(config, "selftest_step_px", 10.0) or 10.0)
    ms = {"ra": gm.pulse_ms_for(step_px, sp["ra"], dec, scale, "ra"),
          "dec": gm.pulse_ms_for(step_px, sp["dec"], dec, scale, "dec")}
    steps = []
    for d in DIRECTIONS:
        axis = "ra" if d in ("W", "E") else "dec"
        for _ in range(steps_n):
            await client.guide_pulse(ms[axis], d)
            await asyncio.sleep(ms[axis] / 1000.0 * clock_scale)
            if not await client.wait_frames(2, timeout=frame_wait):
                await _exit_state(client, slot, started, hotpix)
                return _result("INCONCLUSIVE", ["PHD2 stopped delivering frames"],
                               steps=steps, **base)
            after = await _grab(client)
            dx, dy, peak = await asyncio.to_thread(gm.register_shift, before, after, mask)
            steps.append({"dir": d, "ms": ms[axis], "dx": round(dx, 3),
                          "dy": round(dy, 3), "peak": round(peak, 3),
                          "expected_px": round(gm.expected_px(
                              sp[axis], dec, ms[axis], scale, axis), 3)})
            before = after
    v = gm.selftest_verdict(steps, float(getattr(config, "selftest_ratio_min", 0.5)),
                            float(getattr(config, "selftest_ratio_max", 1.5)))
    try:
        cal = await client.get_calibration_data()
        if cal.get("calibrated"):
            v["calibration"] = {"x_angle": cal.get("xAngle"), "y_angle": cal.get("yAngle"),
                                "x_rate": cal.get("xRate"), "y_rate": cal.get("yRate"),
                                "declination": cal.get("declination")}
            w = v["directions"].get("W", {}).get("angle_deg")
            if w is not None and cal.get("xAngle") is not None:
                v["calibration"]["w_vs_x_angle_deg"] = round(
                    gm._ang_diff(w, float(cal["xAngle"])), 1)
    except Exception:  # noqa: BLE001 - info only (PS-93)
        pass
    reasons = v.pop("reasons")
    verdict = v.pop("verdict")
    if sp.get("zero"):
        verdict = "FAIL"
        reasons.insert(0, "NINA reports a guide rate of zero")
    await _exit_state(client, slot, started, hotpix)
    return _result(verdict, reasons, steps=steps, pulse_ms=ms, **base, **v)


async def _exit_state(client, slot, started, hotpix) -> None:
    """twilight: PHD2 stopped. target: looping on a vetted real star (for
    NINA's StartGuiding). manual: as found."""
    try:
        if slot == "twilight" or (slot == "manual" and started):
            await client.stop_capture()
        elif slot == "target":
            from photonscript.telescope_agent.guard_recovery import pick_star, select_star
            star = await pick_star(client, hotpix)
            if star is not None:
                await select_star(client, star)
    except Exception as e:  # noqa: BLE001
        logger.warning("self-test exit state (%s): %s", slot, e)


async def _on_fail(config, night: str, rec: dict) -> None:
    from photonscript.shared.pushover import notify, record
    causes = list(LIKELY_CAUSES)
    sp = rec.get("speeds") or {}
    if sp:
        causes[3] += (f" (assumed RA {sp.get('ra', 0):.2f}\"/s from "
                      f"{sp.get('source')})")
    if getattr(config, "thesky_enabled", False):
        try:
            from photonscript.telescope_agent.thesky_client import client_from_config
            ok = await asyncio.to_thread(client_from_config(config).ping)
            causes[2] += f" (TheSky ping: {'answered' if ok else 'NO answer'})"
        except Exception as e:  # noqa: BLE001
            causes[2] += f" (TheSky ping failed: {e})"
    msg = (f"Pulse self-test FAIL ({rec.get('context')}, pier "
           f"{rec.get('pier_side') or '?'}): " + "; ".join(rec.get("reasons", []))
           + ". Likely causes, in order: " + " | ".join(causes) + ".")
    if store.alert_once(config, f"selftest-{night}"):
        await notify(config, msg, title="PhotonScript pulse self-test", priority=1)
    else:
        record(config, msg, title="PhotonScript pulse self-test", priority=1,
               reason="selftest-once-per-night")
    if str(getattr(config, "selftest_on_fail", "alert")).lower() == "unguided":
        from photonscript.scheduler.armer import request_fallback_unguided
        await request_fallback_unguided(config, "pulse self-test FAIL")


async def passive_reversed_alert(config, detail: str, night: str,
                                 pier_side: str | None) -> None:
    """Dec reversed after a flip: one push per night with the PS-93 fix."""
    from photonscript.shared.pushover import notify
    if store.alert_once(config, f"selftest-reversed-{night}"):
        await notify(config, f"Guiding after the meridian flip (pier "
                     f"{pier_side or '?'}): Dec corrections move the star the "
                     f"wrong way ({detail}). Fix: PHD2 'Reverse Dec output after "
                     "meridian flip' (Advanced > Mount) must match the mount, or "
                     "recalibrate on this pier side (PS-93).",
                     title="PhotonScript pulse self-test", priority=1)


async def record_passive_fail(config, detail: str, *, source: str,
                              evidence: dict | None = None, night: str | None = None,
                              pier_side: str | None = None,
                              verdict: str = "FAIL") -> dict | None:
    """Passive check result (post-flip watch, guard D4 on a real star): one
    record per night and source, alert through the FAIL path."""
    night = night or store.night_of(config)
    for r in store.selftest_results(config, night=night):
        if r.get("kind") == "passive" and r.get("source") == source \
                and r.get("verdict") == verdict:
            return None
    rec = {"kind": "passive", "night": night,
           "t_utc": store.iso_z(datetime.utcnow()), "context": "passive",
           "source": source, "verdict": verdict, "reasons": [detail],
           "pier_side": pier_side, "evidence": evidence or {}}
    store.append_jsonl(store.selftest_path(config), rec)
    logger.warning("passive pulse check (%s): %s %s", source, verdict, detail)
    if verdict == "FAIL":
        await _on_fail(config, night, rec)
    return rec


def passive_verdict(frames, rates_px_s, scale) -> tuple[str | None, dict]:
    """The passive post-flip check on the guide-frame buffer: (verdict, axes).
    'FAIL' when either axis is 'not moving'; 'REVERSED' when Dec is reversed
    (the PS-93 fix); None when it looks fine or there is too little demand."""
    win = [f for f in frames if not f.get("drop") and not f.get("settling")]
    if len(win) < 10 or not rates_px_s or not scale:
        return None, {}
    mx_ra = max((f["ra_ms"] for f in win), default=0) or None
    mx_dec = max((f["dec_ms"] for f in win), default=0) or None
    r = gm.response(win, rates_px_s[0], rates_px_s[1], scale, mx_ra, mx_dec)
    if any(r[a].get("response_verdict") == "not moving" for a in ("ra", "dec")):
        return "FAIL", r
    if r["dec"].get("response_verdict") == "reversed":
        return "REVERSED", r
    return None, r
