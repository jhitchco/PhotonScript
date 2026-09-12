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
        "camera_setpoint_c": getattr(config, "piggyback_setpoint_c", 0.0),
        "dark_exposures": getattr(config, "piggyback_dark_exposures", "120"),
    }
    wd = getattr(config, "piggyback_image_watch_dir", "")
    if wd:
        updates["image_watch_dir"] = wd
    # Keep the piggyback's calibration + lights in their own library subtree so
    # they never mix with the RC16's (calibration_health/build_library key off
    # library_dir).
    pb_lib = getattr(config, "piggyback_library_dir", "") or ""
    if not pb_lib:
        main_lib = getattr(config, "library_dir", "") or ""
        from pathlib import Path as _P
        base = _P(main_lib) if main_lib else (_P(getattr(config, "data_dir", ".")) / "Library")
        pb_lib = str(base / "piggyback")
    updates["library_dir"] = pb_lib
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


async def nina_cool(base_url: str, temperature: float, minutes: float = 10.0) -> dict:
    """Drive a rig's camera to a cooling setpoint (ninaAPI GET cool)."""
    base = base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(base + "/equipment/camera/cool",
                                  params={"temperature": temperature,
                                          "minutes": minutes})
            r.raise_for_status()
            return {"ok": True, "detail": f"cooling to {temperature}C over {minutes}m"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


async def nina_warm(base_url: str, minutes: float = 5.0) -> dict:
    """Warm a rig's camera back up (ninaAPI GET warm)."""
    base = base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(base + "/equipment/camera/warm",
                                  params={"minutes": minutes})
            r.raise_for_status()
            return {"ok": True, "detail": f"warming over {minutes}m"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


def rig_setpoint(config, rig: str) -> float:
    if rig == PIGGYBACK:
        return float(getattr(config, "piggyback_setpoint_c", 0.0))
    return float(getattr(config, "camera_setpoint_c", 0.0))


async def nina_dispatch(base_url: str, seq: dict) -> dict:
    """Load + start a sequence on a rig's NINA (used for piggyback calibration,
    which has no armer state machine). Returns {ok, detail}."""
    base = base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            await client.get(base + "/sequence/stop")  # harmless if idle
            ld = await client.post(base + "/sequence/load", json=seq)
            ld.raise_for_status()
            st = await client.get(base + "/sequence/start",
                                  params={"skipValidation": "true"})
            st.raise_for_status()
        return {"ok": True, "detail": "loaded + started"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}
