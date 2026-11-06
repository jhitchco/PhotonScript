"""PS-64: operator Pause of a running night (the Armer's PAUSED_OPERATOR).

A pause stops a NINA sequence through ninaAPI's GET /sequence/stop (the call
every armer abort path already uses), but not in the middle of a sub:
stop_after_exposure() polls that NINA's camera and stops once the current
exposure has finished and downloaded (camera idle again), so the frame is
kept. It stops at once when nothing is exposing (slew, center, AF, dither),
when the camera cannot be read, or when a NEW exposure has started (the
idle gap was missed: a few seconds are lost, not a sub). The wait is
bounded by the exposure end + EXPOSURE_END_GRACE_S and MAX_WAIT_S.

A stopped sequence leaves the mount tracking, the cooler at setpoint and
PHD2 guiding (NINA's End area never runs on a stop): nothing is parked,
warmed or closed here. Resume is the armer's mid-night re-dispatch.

Only GETs; every reader returns None on a failure (callers treat that as
"unknown"). Never raises.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

POLL_S = 2.0                 # camera poll while waiting for the exposure end
SAVE_SETTLE_S = 4.0          # after the camera goes idle: NINA queues the save
EXPOSURE_END_GRACE_S = 60.0  # download + save margin past ExposureEndTime
MAX_WAIT_S = 960.0           # never wait longer than this (a 900 s sub + margin)
NEW_EXPOSURE_JUMP_S = 5.0    # ExposureEndTime moved later by this = a new sub

# NINA CameraInfo.CameraState values that mean "a frame is in flight"
BUSY_CAMERA_STATES = ("exposing", "download", "downloading")


def camera_busy(cam) -> bool | None:
    """True while the camera exposes or downloads, False when idle, None
    when the payload says nothing (unreadable)."""
    if not isinstance(cam, dict):
        return None
    st = str(cam.get("CameraState") or "").strip().lower()
    if cam.get("IsExposing") or st in BUSY_CAMERA_STATES:
        return True
    if "IsExposing" in cam or st:
        return False
    return None


def parse_nina_time(value) -> datetime | None:
    """A ninaAPI DateTime ("2026-10-06T21:03:44.1234567-06:00", or without
    an offset = the scope PC's local time) as an aware UTC datetime. None for
    NINA's empty value (year 1) or anything unparseable."""
    if not value or not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    # .NET writes 7 fractional digits; fromisoformat takes at most 6
    if "." in s:
        head, rest = s.split(".", 1)
        digits = ""
        while rest and rest[0].isdigit():
            digits, rest = digits + rest[0], rest[1:]
        s = head + "." + (digits[:6] or "0") + rest
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.year < 2000:
        return None
    if dt.tzinfo is None:
        dt = dt.astimezone()   # naive = this PC's local time (it is the scope PC)
    return dt.astimezone(timezone.utc)


def exposure_end(cam) -> datetime | None:
    if not isinstance(cam, dict):
        return None
    return parse_nina_time(cam.get("ExposureEndTime"))


def seconds_left(cam, now_utc: datetime | None = None) -> float | None:
    """Seconds until the running exposure ends (0 when past), None when the
    camera is not exposing or reports no end time."""
    if not isinstance(cam, dict) or not cam.get("IsExposing"):
        return None
    end = exposure_end(cam)
    if end is None:
        return None
    now_utc = now_utc or datetime.now(timezone.utc)
    return max(0.0, (end - now_utc).total_seconds())


async def stop_after_exposure(read_camera, stop, *, when: str = "after_exposure",
                              sleep=asyncio.sleep, clock=time.monotonic,
                              utcnow=None, poll_s: float = POLL_S,
                              max_wait_s: float = MAX_WAIT_S) -> dict:
    """Wait (bounded) for the current exposure to finish, then call stop().
    read_camera: async () -> camera info payload or None. stop: async () ->
    bool. Returns {"ok", "how", "waited_s"}; how is one of now, idle,
    unreadable, after_exposure, new_exposure, timeout. Cancelling the task
    (resume before the stop) sends no stop."""
    utcnow = utcnow or (lambda: datetime.now(timezone.utc))
    t0 = clock()
    how = "now"
    if when != "now":
        cam = await read_camera()
        busy = camera_busy(cam)
        if busy is None:
            how = "unreadable"
        elif not busy:
            how = "idle"
        else:
            end0 = exposure_end(cam)
            left = seconds_left(cam, utcnow())
            limit = max_wait_s if left is None else min(
                max_wait_s, left + EXPOSURE_END_GRACE_S)
            how = "timeout"
            while clock() - t0 < limit:
                await sleep(poll_s)
                cam = await read_camera()
                busy = camera_busy(cam)
                if busy is None:
                    how = "unreadable"
                    break
                if not busy:
                    await sleep(SAVE_SETTLE_S)
                    how = "after_exposure"
                    break
                end = exposure_end(cam)
                if end0 is not None and end is not None and \
                        (end - end0).total_seconds() > NEW_EXPOSURE_JUMP_S:
                    how = "new_exposure"
                    break
                if end0 is None:
                    end0 = end
    try:
        ok = bool(await stop())
    except Exception as e:  # noqa: BLE001
        logger.warning("pause: sequence stop failed: %s", e)
        ok = False
    return {"ok": ok, "how": how, "waited_s": round(clock() - t0, 1)}


HOW_TEXT = {"now": "stopped at once (asked for now)",
            "idle": "stopped between subs (camera idle)",
            "unreadable": "stopped at once (camera unreadable)",
            "after_exposure": "stopped after the current sub",
            "new_exposure": "stopped as a new sub began (seconds lost)",
            "timeout": "stopped at the wait limit"}


def describe(res: dict | None) -> str:
    if not isinstance(res, dict):
        return "not stopped"
    if not res.get("ok"):
        return "stop FAILED"
    txt = HOW_TEXT.get(str(res.get("how")), str(res.get("how")))
    w = float(res.get("waited_s") or 0)
    return txt + (f", waited {w / 60:.1f} min" if w >= 30 else "")


# --- other rigs' NINA (the Piggy-600) ------------------------------------------

async def nina_get(base_url: str, path: str, timeout: float = 10.0):
    """GET a ninaAPI path, the {"Response": ...} payload or None."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(base_url.rstrip("/") + path)
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001
        logger.debug("ninaAPI %s%s failed: %s", base_url, path, e)
        return None
    if isinstance(data, dict):
        if data.get("Success") is False:
            return None
        return data.get("Response", data)
    return data


async def camera_info(base_url: str):
    p = await nina_get(base_url, "/equipment/camera/info")
    return p if isinstance(p, dict) else None


async def sequence_stop(base_url: str) -> bool:
    return await nina_get(base_url, "/sequence/stop", timeout=30.0) is not None
