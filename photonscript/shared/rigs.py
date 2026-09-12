"""Rig registry — the main RC16 rig and the optional piggyback 2nd NINA.

A "rig" is one NINA instance with its own camera. PhotonScript was single-rig;
this module lets every device-facing hook target either instance by handing it a
config *view* whose nina_base_url / pixel scale / gain point at that rig. The
main rig ('rc16') returns the config unchanged; the piggyback ('piggyback')
returns a pydantic copy with the 2nd NINA's settings, so preflight, connect_all,
image QA, etc. all work per-rig with no duplication.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

RC16 = "rc16"
PIGGYBACK = "piggyback"

# Devices each rig's NINA instance owns. The piggyback instance connects only
# its own camera + focuser; the mount, guider, safety monitor and weather live
# on the main RC16 instance and are shared.
RIG_DEVICES = {
    RC16: ("camera", "filterwheel", "focuser", "mount", "guider",
           "weather", "safetymonitor"),
    PIGGYBACK: ("camera", "focuser"),
}


def rig_ids(config) -> list[str]:
    ids = [RC16]
    if getattr(config, "piggyback_enabled", False):
        ids.append(PIGGYBACK)
    return ids


def rig_label(config, rig: str) -> str:
    if rig == PIGGYBACK:
        return getattr(config, "piggyback_name", "Piggy-600")
    return "RC16"


def rig_devices(rig: str) -> tuple:
    return RIG_DEVICES.get(rig, RIG_DEVICES[RC16])


def rig_config(config, rig: str):
    """Return a config whose device-facing fields target `rig`.

    RC16 -> the config itself. PIGGYBACK -> a copy overriding nina_base_url,
    pixel_scale_arcsec, default_gain/offset, and (if set) image_watch_dir and
    the HFR gate, so shared code hits the 2nd NINA with the right scale.
    """
    if rig != PIGGYBACK:
        return config
    updates = {
        "nina_base_url": getattr(config, "piggyback_nina_base_url",
                                 "http://localhost:1889/v2/api"),
        "pixel_scale_arcsec": getattr(config, "piggyback_pixel_scale_arcsec", 1.29),
        "default_gain": getattr(config, "piggyback_default_gain", 100),
        "default_offset": getattr(config, "piggyback_default_offset", 256),
        "quality_hfr_abs_max": getattr(config, "piggyback_hfr_abs_max", 4.5),
    }
    wd = getattr(config, "piggyback_image_watch_dir", "")
    if wd:
        updates["image_watch_dir"] = wd
    try:
        return config.model_copy(update=updates)  # pydantic v2
    except Exception:  # noqa: BLE001 - non-pydantic config in tests
        import copy as _copy
        c = _copy.copy(config)
        for k, v in updates.items():
            try:
                setattr(c, k, v)
            except Exception:  # noqa: BLE001
                pass
        return c


async def nina_capture(base_url: str, duration: float = 2.0,
                       gain: int | None = None) -> dict:
    """Fire a single test exposure via the ninaAPI camera-capture endpoint.

    Bench test only: does not save the frame, does not touch the mount or roof.
    Returns {"ok": bool, "detail": ...}. On any error the detail carries the
    real message (e.g. a 404 if this plugin build names the endpoint
    differently) so the caller can report it.
    """
    base = base_url.rstrip("/")
    params = {"duration": duration, "save": "false", "getResult": "false"}
    if gain is not None:
        params["gain"] = gain
    try:
        async with httpx.AsyncClient(timeout=max(20.0, duration + 15)) as client:
            r = await client.get(base + "/equipment/camera/capture", params=params)
            r.raise_for_status()
            data = r.json()
            payload = data.get("Response", data) if isinstance(data, dict) else data
            if isinstance(data, dict) and data.get("Success") is False:
                return {"ok": False, "detail": data.get("Error", payload)}
            return {"ok": True, "detail": payload}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}
