"""Guide-camera hot-pixel map (PS-91).

PHD2's auto-select picks the brightest "star" it can find; with no dark
library or defect map a hot pixel qualifies (2026-09-25 / -26). PhotonScript
keeps its own map, used to vet find_star picks (the guard's D5 and the
recovery's star choice) and to mask hot pixels out of the PS-92 self-test
frames.

Capture: with the roof closed, PHD2 loops at its own guide exposure, gain and
binning; 8 frames come back through PHD2's own pipeline (save_image, so its
dark / noise reduction apply), are median-stacked, and every isolated peak or
small (up to 2x2) cluster well above the local background is a hot pixel.
Saved as <data_dir>/phd2/hotpix.json (+ the median frame as FITS) with the
binning, exposure, camera, gain and time. Stale after
phd2_hotpix_max_age_days or any binning / exposure change.

Building PHD2's own dark library or defect map is GUI-only (PS-89 audit).
Triggers: the armer (ARMED, in the 30 min before pre-config, while the
safety monitor reads unsafe; and opportunistically in PAUSED_UNSAFE) and
POST /api/phd2/hotpix/capture.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime

from photonscript.shared import phd2_store as store
from photonscript.telescope_agent import phd2_ops

logger = logging.getLogger(__name__)

N_FRAMES = 8
NSIGMA = 8.0
MAX_CLUSTER = 4   # pixels: isolated peaks and 2x2 clusters


def build_map(frames, nsigma: float = NSIGMA, max_cluster: int = MAX_CLUSTER):
    """(median frame, [[x, y, excess ADU], ...], ignored blobs) from a stack
    of dark guide frames."""
    import numpy as np
    from scipy import ndimage
    stack = np.asarray([np.asarray(f, dtype=float) for f in frames])
    med = np.median(stack, axis=0)
    resid = med - ndimage.median_filter(med, size=5)
    noise = float(np.median(np.abs(resid - np.median(resid)))) * 1.4826 or 1.0
    hot = resid > nsigma * noise
    # Size is judged on everything bright (not just the local-peak pixels):
    # a glow or a leaked star is a big bright region whose edges also stand
    # out from a 5x5 median, and none of it is a hot pixel.
    lvl = float(np.median(med))
    gnoise = float(np.median(np.abs(med - lvl))) * 1.4826 or 1.0
    bright = hot | (med - lvl > nsigma * gnoise)
    lbl, n = ndimage.label(bright, structure=np.ones((3, 3)))
    pixels, ignored = [], 0
    for i, sl in enumerate(ndimage.find_objects(lbl), start=1):
        if sl is None:
            continue
        sel = lbl[sl] == i
        if int(sel.sum()) > max_cluster:
            ignored += 1
            continue
        ys, xs = np.nonzero(sel & hot[sl])
        for y, x in zip(ys + sl[0].start, xs + sl[1].start):
            pixels.append([int(x), int(y), round(float(resid[y, x]), 1)])
    return med, pixels, ignored


def load(config) -> dict | None:
    return store.read_json(store.hotpix_path(config))


def staleness(hp: dict | None, config, binning=None, exposure_ms=None,
              now: datetime | None = None) -> str | None:
    """Why the map needs a refresh, or None when it is current."""
    if not hp:
        return "no hot-pixel map yet"
    now = now or datetime.utcnow()
    made = store.parse_z(hp.get("created_utc"))
    max_age = float(getattr(config, "phd2_hotpix_max_age_days", 7) or 7)
    if made is None or (now - made).total_seconds() > max_age * 86400:
        return f"older than {max_age:g} days"
    if binning and hp.get("binning") and int(binning) != int(hp["binning"]):
        return f"binning changed ({hp['binning']} -> {binning})"
    if exposure_ms and hp.get("exposure_ms") and \
            int(exposure_ms) != int(hp["exposure_ms"]):
        return f"exposure changed ({hp['exposure_ms']} -> {exposure_ms} ms)"
    return None


def status(config) -> dict:
    hp = load(config)
    made = store.parse_z((hp or {}).get("created_utc"))
    age = (datetime.utcnow() - made).total_seconds() / 86400 if made else None
    return {"exists": bool(hp), "created_utc": (hp or {}).get("created_utc"),
            "age_days": round(age, 1) if age is not None else None,
            "stale": staleness(hp, config),
            "count": len((hp or {}).get("pixels", [])),
            "binning": (hp or {}).get("binning"),
            "exposure_ms": (hp or {}).get("exposure_ms"),
            "camera": (hp or {}).get("camera"), "gain": (hp or {}).get("gain"),
            "frames": (hp or {}).get("frames"), "reason": (hp or {}).get("reason"),
            "ignored_blobs": (hp or {}).get("ignored_blobs"),
            "max_age_days": float(getattr(config, "phd2_hotpix_max_age_days", 7) or 7)}


async def _client(config):
    from photonscript.telescope_agent.phd2_client import PHD2Client
    c = PHD2Client(config.phd2_host, config.phd2_port, config=config)
    if not await c.connect():
        return None, None
    task = await c.start_event_loop()
    return c, task


async def capture(config, reason: str = "manual", *, client=None,
                  n_frames: int = N_FRAMES, wait_s: float = 0.0) -> dict:
    """Capture and save a new map. Needs PHD2 Stopped or Looping (never
    while guiding) and the phd2_ops lock. Never raises."""
    try:
        async with phd2_ops.hold("hotpix", wait_s=wait_s):
            own = client is None
            task = None
            if own:
                client, task = await _client(config)
                if client is None:
                    return {"ok": False, "note": "PHD2 not reachable"}
            try:
                return await _capture(config, client, reason, n_frames)
            finally:
                if own:
                    await client.disconnect()
                    if task:
                        task.cancel()
    except phd2_ops.PHD2Busy as e:
        return {"ok": False, "note": str(e)}
    except Exception as e:  # noqa: BLE001
        logger.warning("hot-pixel map capture failed: %s", e)
        return {"ok": False, "note": f"{type(e).__name__}: {e}"}


async def _capture(config, client, reason, n_frames) -> dict:
    from photonscript.telescope_agent.guide_guard import load_fits
    state = await client.get_app_state()
    if state not in ("Stopped", "Looping"):
        return {"ok": False, "note": f"PHD2 is {state}: not capturing while it "
                                     "guides or calibrates"}
    exposure_ms = await client.get_exposure()
    binning = await client.get_camera_binning()
    try:
        eq = await client.get_current_equipment()
        camera = ((eq or {}).get("camera") or {}).get("name")
    except Exception:  # noqa: BLE001
        camera = None
    started = state == "Stopped"
    if started:
        await client.loop()
    frames, gain = [], None
    wait = max(10.0, 3.0 * (exposure_ms or 2000) / 1000.0 + 5.0)
    try:
        for _ in range(n_frames):
            if client.app_state in ("Guiding", "Calibrating", "LostLock"):
                return {"ok": False, "note": "PHD2 started guiding: map not built"}
            if not await client.wait_frames(1, timeout=wait):
                return {"ok": False, "note": "PHD2 stopped delivering frames"}
            path = await client.save_image()
            img, hdr = await asyncio.to_thread(load_fits, path)
            gain = hdr.get("GAIN", gain)
            frames.append(img)
            try:
                os.remove(path)  # PHD2's temp copy; the client owns cleanup
            except OSError:
                pass
    finally:
        # Only undo our own loop: if NINA started guiding meanwhile (the roof
        # reopened), leave PHD2 alone.
        if started and client.app_state in ("Looping", "Stopped", "Unknown"):
            try:
                await client.stop_capture()
            except Exception as e:  # noqa: BLE001
                logger.warning("hot-pixel capture: stop_capture failed: %s", e)
    med, pixels, ignored = await asyncio.to_thread(build_map, frames)
    hp = {"created_utc": store.iso_z(datetime.utcnow()), "reason": reason,
          "binning": binning, "exposure_ms": exposure_ms, "camera": camera,
          "gain": gain, "frames": len(frames), "width": int(med.shape[1]),
          "height": int(med.shape[0]), "nsigma": NSIGMA,
          "ignored_blobs": ignored, "pixels": pixels}
    store.write_json(store.hotpix_path(config), hp)
    try:
        from astropy.io import fits
        fits.PrimaryHDU(med.astype("float32")).writeto(
            store.hotpix_fits_path(config), overwrite=True)
    except Exception as e:  # noqa: BLE001
        logger.debug("hot-pixel median FITS not written: %s", e)
    logger.info("hot-pixel map: %d pixels (bin %s, %s ms, %s)", len(pixels),
                binning, exposure_ms, reason)
    return {"ok": True, **{k: hp[k] for k in ("created_utc", "binning",
                                             "exposure_ms", "camera", "frames")},
            "count": len(pixels)}


async def maybe_capture(config, reason: str) -> dict:
    """Capture only when the map is missing or stale for PHD2's current
    binning / exposure (the armer's roof-closed triggers)."""
    if phd2_ops.busy():
        return {"ok": False, "note": f"PHD2 busy ({phd2_ops.owner()})"}
    client, task = await _client(config)
    if client is None:
        return {"ok": False, "note": "PHD2 not reachable"}
    try:
        try:
            binning = await client.get_camera_binning()
            exposure_ms = await client.get_exposure()
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "note": f"PHD2 did not answer: {e}"}
        why = staleness(load(config), config, binning, exposure_ms)
        if why is None:
            return {"ok": True, "skipped": "map is current"}
        return await capture(config, f"{reason} ({why})", client=client)
    finally:
        await client.disconnect()
        if task:
            task.cancel()
