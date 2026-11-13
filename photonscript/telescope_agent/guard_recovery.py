"""Recovery from a non-star lock (PS-91), run by the RC16 telescope agent
only when guard_auto_recover is on (the first guarded night is observe-only).

Gated: PhotonScript must get the phd2_ops lock, PHD2 must be Guiding (not
settling or calibrating), the mount not slewing and no NINA meridian flip
running; at most MAX_PER_HOUR recoveries per target per hour. NINA keeps
exposing through it (a NINA dither during the recovery fails harmlessly).

Steps: stop_capture, loop; save_image and detect stars on PHD2's own frame
with the hot-pixel map masked; vet them (HFD 1.5 to 10 px, SNR >= 15, not
clipped, away from the edges and from mapped hot pixels); find_star with a
16 px ROI on the best one and confirm PHD2's lock landed on it; guide
(settle 1.5 px / 10 s / 60 s, recalibrate=false). For an impossible state
(D3) the only action is stop_capture.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time

from photonscript.telescope_agent import phd2_ops
from photonscript.telescope_agent.guide_guard import (
    detect_stars, hotpix_mask, load_fits, vet_stars)

logger = logging.getLogger(__name__)

MAX_PER_HOUR = 2
ROI_PX = 16
CONFIRM_PX = 2.0


class RecoveryCap:
    """At most MAX_PER_HOUR recoveries per target in any rolling hour."""

    def __init__(self, per_hour: int = MAX_PER_HOUR):
        self.per_hour = per_hour
        self._log: list[tuple[str, float]] = []

    def allow(self, target: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        self._log = [(t, at) for t, at in self._log if now - at < 3600]
        return sum(1 for t, _ in self._log if t == (target or "?")) < self.per_hour

    def note(self, target: str, now: float | None = None) -> None:
        self._log.append((target or "?", time.monotonic() if now is None else now))


def gate(client, *, slewing: bool | None, flip_running: bool | None) -> str | None:
    """Why a recovery must not start now, or None."""
    if client.app_state != "Guiding":
        return f"PHD2 is {client.app_state}, not Guiding"
    if client.settling:
        return "PHD2 is settling"
    if slewing:
        return "mount is slewing"
    if flip_running:
        return "a meridian flip is running"
    if phd2_ops.busy():
        return f"PHD2 busy ({phd2_ops.owner()})"
    return None


async def pick_star(client, hotpix, *, wait_s: float = 30.0) -> dict | None:
    """Save PHD2's current frame and return the best vetted real star."""
    path = await client.save_image()
    try:
        img, _hdr = await asyncio.to_thread(load_fits, path)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    mask = hotpix_mask(img.shape, hotpix, client.binning)
    stars = await asyncio.to_thread(detect_stars, img, mask)
    vetted = vet_stars(stars, img.shape, hotpix=hotpix, binning=client.binning)
    return vetted[0] if vetted else None


async def select_star(client, star: dict, settle_s: float = 0.5) -> tuple | None:
    """find_star with a ROI_PX box on `star`; the lock position if PHD2
    locked within CONFIRM_PX of it, else None."""
    roi = [int(round(star["x"] - ROI_PX / 2)), int(round(star["y"] - ROI_PX / 2)),
           ROI_PX, ROI_PX]
    xy = await client.find_star(roi)
    await asyncio.sleep(settle_s)  # let the LockPositionSet event arrive
    lock = client.lock_position or (tuple(xy) if xy else None)
    if lock is None:
        return None
    if math.hypot(lock[0] - star["x"], lock[1] - star["y"]) > CONFIRM_PX:
        return None
    return tuple(lock)


async def recover(client, config, hotpix, *, target: str, cap: RecoveryCap,
                  slewing: bool | None = None, flip_running: bool | None = None,
                  frame_timeout_s: float = 60.0, settle_s: float = 0.5) -> dict:
    """Re-select a vetted real star and resume guiding. Never raises.
    {"ok", "skipped" (gate reason), "detail", "steps", "lock"}."""
    why = gate(client, slewing=slewing, flip_running=flip_running)
    if why:
        return {"ok": False, "skipped": why, "steps": []}
    if not cap.allow(target):
        return {"ok": False, "skipped": f"recovery cap reached ({cap.per_hour} "
                                        f"per target per hour)", "steps": []}
    cap.note(target)
    steps: list[str] = []
    try:
        async with phd2_ops.hold("guard"):
            await client.stop_capture()
            steps.append("stop_capture")
            await client.loop()
            steps.append("loop")
            if not await client.wait_frames(2, timeout=frame_timeout_s):
                return {"ok": False, "detail": "PHD2 delivered no frames",
                        "steps": steps}
            star = await pick_star(client, hotpix)
            if star is None:
                return {"ok": False, "detail": "no real star in the guide frame "
                        "(all candidates failed the vetting)", "steps": steps}
            steps.append(f"star ({star['x']:.1f}, {star['y']:.1f}) HFD "
                         f"{star['hfd']:.1f} SNR {star['snr']:.0f}")
            lock = await select_star(client, star, settle_s=settle_s)
            if lock is None:
                return {"ok": False, "detail": "find_star did not lock on the "
                        "vetted star", "steps": steps}
            steps.append(f"find_star lock ({lock[0]:.1f}, {lock[1]:.1f})")
            await client.start_guiding(1.5, 10, 60, recalibrate=False)
            steps.append("guide")
            return {"ok": True, "detail": "guiding resumed on a vetted star",
                    "steps": steps, "lock": list(lock)}
    except phd2_ops.PHD2Busy as e:
        return {"ok": False, "skipped": str(e), "steps": steps}
    except Exception as e:  # noqa: BLE001
        logger.warning("guard recovery failed: %s", e)
        return {"ok": False, "detail": f"{type(e).__name__}: {e}", "steps": steps}


async def stop_impossible(client) -> dict:
    """D3: PHD2 guiding a parked / not tracking / closed-roof scope."""
    try:
        async with phd2_ops.hold("guard"):
            await client.stop_capture()
        return {"ok": True, "detail": "stop_capture sent", "steps": ["stop_capture"]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(e).__name__}: {e}", "steps": []}
