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


# PS-114: every QA gate per rig, one mechanism. Each row is
# (RC16 config key, Piggy-600 override key, fallback when the config has no
# override key at all). The RC16 reads the plain key (env PS_<KEY>); the
# Piggy-600 view gets the override (env PS_PIGGYBACK_<...>) under the plain
# key's name, so shared.qa_rules.thresholds() reads one key per gate
# whatever the rig. A fallback of None means "same as the RC16 key". The
# values and the why of each live in shared/config.py; the System page
# shows both rigs side by side (GET /api/qa/gates).
PIGGYBACK_GATES = (
    ("quality_hfr_abs_max", "piggyback_hfr_abs_max", 4.5),
    # The FWHM + eccentricity gates are scale-dependent too: the RC16's
    # 4.0" FWHM gate rejected every piggyback sub on 2026-09-19 ("FWHM
    # 6.5\" > 4.0\"") because a 1.29"/px wide-field star is legitimately
    # larger in arcsec. PS-114: set from the Piggy-600's own subs.
    ("quality_fwhm_max", "piggyback_fwhm_max", 15.0),
    # OSC FWHM is advisory, not a hard reject: it's inflated by extended
    # bright objects, so a tight-HFR sub can read a large FWHM and still be
    # sharp. HFR + ecc stay the hard gates for this rig (2026-09-20).
    ("quality_fwhm_soft", "piggyback_fwhm_soft", True),
    ("quality_eccentricity_max", "piggyback_ecc_max", 0.75),
    # PS-71: the sub-physical star-size floor is scale-dependent too.
    ("quality_fwhm_min_arcsec", "piggyback_fwhm_min_arcsec", 2.0),
    # PS-67: 1.29"/px over a much wider field: off target only far out
    ("pointing_off_target_flag_arcmin", "piggyback_off_target_flag_arcmin",
     30.0),
    ("pointing_off_target_reject_arcmin", "piggyback_off_target_reject_arcmin",
     60.0),
    # PS-114: the rest of the gates, same values as the RC16 until the
    # Piggy-600's own baseline (photonscript qa-baselines) says otherwise
    ("quality_star_min", "piggyback_star_min", None),
    ("quality_star_max", "piggyback_star_max", None),
    ("qa_background_rel_max", "piggyback_background_rel_max", None),
    ("qa_hfr_outlier_factor", "piggyback_hfr_outlier_factor", None),
    ("quality_tracking_rms_max", "piggyback_tracking_rms_max", None),
    ("qa_tracking_jump_max", "piggyback_tracking_jump_max", None),
    ("quality_corner_spread_max", "piggyback_corner_spread_max", None),
    ("quality_bias_floor_margin_adu", "piggyback_bias_floor_margin_adu", None),
)


def gate_key(rig: str, key: str) -> str:
    """The config key that holds gate `key` (an RC16 key) for `rig`."""
    if rig == PIGGYBACK:
        for base, pb, _ in PIGGYBACK_GATES:
            if base == key:
                return pb
    return key


def rig_config(config, rig: str):
    """Return a config whose device-facing fields target `rig`.

    RC16 -> the config itself. PIGGYBACK -> a copy overriding nina_base_url,
    pixel_scale_arcsec, default_gain/offset, (if set) image_watch_dir, and
    every QA gate in PIGGYBACK_GATES, so shared code hits the 2nd NINA with
    the right scale and grades with the Piggy-600's own limits.
    """
    if rig != PIGGYBACK:
        return config
    updates = {
        "nina_base_url": getattr(config, "piggyback_nina_base_url",
                                 "http://localhost:1889/v2/api"),
        "pixel_scale_arcsec": getattr(config, "piggyback_pixel_scale_arcsec", 1.29),
        "default_gain": getattr(config, "piggyback_default_gain", 100),
        "default_offset": getattr(config, "piggyback_default_offset", 256),
        "camera_setpoint_c": getattr(config, "piggyback_setpoint_c", 0.0),
        "camera_readout_mode": getattr(config, "piggyback_readout_mode", "LCG"),
        "dark_exposures": getattr(config, "piggyback_dark_exposures", "120"),
        # NINA #2 has its own safety driver (a shared one deadlocks on the
        # driver's trace-log file lock), so pin its own chooser Id.
        "safety_monitor_device_id": getattr(
            config, "piggyback_safety_monitor_device_id", ""),
    }
    for base, pb, fallback in PIGGYBACK_GATES:
        v = getattr(config, pb, None)
        if v is None:
            v = fallback if fallback is not None else getattr(config, base, None)
        if v is not None:
            updates[base] = v
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


async def nina_warm(base_url: str, minutes: float = 0.0) -> dict:
    """Warm a rig's camera back up (ninaAPI GET warm).

    minutes=0 (the default) is an INSTANT warm: release the setpoint / cut the
    TEC now and let the sensor drift to ambient on its own — no forced ramp.
    A ramp fights an arm/precool that wants to cool right now, so we don't hold
    one. Pass minutes>0 only to deliberately bring the gradual ramp back."""
    base = base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(base + "/equipment/camera/warm",
                                  params={"minutes": minutes})
            r.raise_for_status()
            how = "instant (cooler off)" if minutes <= 0 else f"over {minutes}m"
            return {"ok": True, "detail": f"warming {how}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


async def nina_dew_heater(base_url: str, on: bool) -> dict:
    """Toggle a rig camera's window dew heater (ninaAPI GET dew-heater).

    The OGMA/ToupTek cameras carry a window heater; NINA exposes it at
    /equipment/camera/dew-heater?power=true|false. Not every camera/driver
    supports it — on those the call errors and the detail says so."""
    base = base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(base + "/equipment/camera/dew-heater",
                                  params={"power": "true" if on else "false"})
            r.raise_for_status()
            return {"ok": True, "detail": f"dew heater {'ON' if on else 'OFF'}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


async def nina_camera_info(base_url: str) -> dict | None:
    """Read a rig camera's live state (ninaAPI GET info): CoolerOn,
    CoolerPower, Temperature, DewHeaterOn... Returns the payload dict, or
    None when the rig/NINA is unreachable (callers treat that as unknown)."""
    base = base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(base + "/equipment/camera/info")
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and data.get("Success") is False:
                logger.warning("camera info (%s): %s", base_url, data.get("Error"))
                return None
            payload = data.get("Response", data) if isinstance(data, dict) else data
            return payload if isinstance(payload, dict) else None
    except Exception as e:  # noqa: BLE001
        logger.warning("camera info (%s) failed: %s", base_url, e)
        return None


def rig_setpoint(config, rig: str) -> float:
    if rig == PIGGYBACK:
        return float(getattr(config, "piggyback_setpoint_c", 0.0))
    return float(getattr(config, "camera_setpoint_c", 0.0))


# PS-128: FITS keywords that carry the camera readout mode. NINA writes
# READOUTM (the camera's ReadoutModes[] name; on the OGMA AP26MC / AP26CC
# "High Conversion Gain" / "Low Conversion Gain", every frame in the Library
# has it). READMODE / READOUT are what some other capture programs write.
READOUT_KEYS = ("READOUTM", "READMODE", "READOUT")


def normalize_readout(v) -> str | None:
    """PS-128: one spelling per readout mode: "High Conversion Gain" / "HCG"
    -> "HCG", "Low Conversion Gain" / "LCG" -> "LCG", anything else upper
    case and trimmed. None / blank -> None."""
    if v is None:
        return None
    s = " ".join(str(v).split()).upper()
    if not s:
        return None
    if s in ("HCG", "HIGH CONVERSION GAIN", "HIGH GAIN", "HIGHCONVERSIONGAIN"):
        return "HCG"
    if s in ("LCG", "LOW CONVERSION GAIN", "LOW GAIN", "LOWCONVERSIONGAIN"):
        return "LCG"
    return s


def header_readout(hdr) -> tuple[str | None, str | None]:
    """PS-128: (normalized, raw) readout mode from a FITS header, the first
    of READOUT_KEYS present; (None, None) when none is."""
    get = getattr(hdr, "get", None)
    if get is None:
        return None, None
    for k in READOUT_KEYS:
        raw = get(k)
        if raw not in (None, ""):
            return normalize_readout(raw), str(raw).strip()
    return None, None


def rig_readout(config, rig: str) -> str | None:
    """PS-128: the readout mode a rig's lights use and its darks / bias must
    match (camera_readout_mode / piggyback_readout_mode, normalized). Also
    the mode assumed for a frame whose header has no readout keyword. None
    (key set blank) = readout is not matched (the pre-PS-128 behavior)."""
    key = "piggyback_readout_mode" if rig == PIGGYBACK else "camera_readout_mode"
    default = "LCG" if rig == PIGGYBACK else "HCG"
    return normalize_readout(getattr(config, key, default))


def camera_info_readout(info: dict | None) -> tuple[str | None, str | None]:
    """PS-128: (normalized, raw) readout mode NINA will shoot sequence frames
    at, from a ninaAPI camera info payload: ReadoutModes[ReadoutModeForNormalImages]
    (the profile's "readout mode for sequence images"; NINA applies it to
    every non-snapshot capture, lights, darks and bias alike), else
    ReadoutModes[ReadoutMode] (the camera's current mode). (None, None) when
    the payload does not say (fail open: callers treat it as unknown)."""
    if not isinstance(info, dict):
        return None, None
    modes = info.get("ReadoutModes")
    if isinstance(modes, dict):          # a $values wrapper
        modes = modes.get("$values")
    for key in ("ReadoutModeForNormalImages", "ReadoutMode"):
        idx = info.get(key)
        if isinstance(idx, str) and not idx.strip().lstrip("-").isdigit():
            return normalize_readout(idx), idx.strip()
        try:
            i = int(idx)
        except (TypeError, ValueError):
            continue
        if isinstance(modes, (list, tuple)) and 0 <= i < len(modes):
            raw = str(modes[i]).strip()
            return normalize_readout(raw), raw
    return None, None


def light_epoch_fields(hdr) -> dict:
    """PS-122: the dark-matching epoch of a light from its FITS header (gain,
    offset, binning, readout mode) for the subs log, so the Calibration owed
    view can give off-epoch lights their own dark bucket. Missing keys -> None.
    The readout is the raw header string (normalize_readout when matching)."""
    def _int(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None
    get = getattr(hdr, "get", None)
    if get is None:
        return {"gain": None, "offset": None, "xbin": None, "readout": None}
    return {"gain": _int(get("GAIN")), "offset": _int(get("OFFSET")),
            "xbin": _int(get("XBINNING")),
            "readout": header_readout(hdr)[1]}


async def nina_sequence_stop(base_url: str) -> dict:
    """Stop whatever sequence a rig's NINA is running (ninaAPI GET
    sequence/stop; harmless if idle). The armer's dawn shutdown uses it on
    NINA #2 so a companion wedged waiting for safe can't resume lights or
    flats after sunrise if the roof reopens (PS-36). Returns {ok, detail}."""
    base = base_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.get(base + "/sequence/stop")
            r.raise_for_status()
            data = r.json()
            if isinstance(data, dict) and data.get("Success") is False:
                return {"ok": False, "detail": str(data.get("Error"))}
        return {"ok": True, "detail": "sequence stopped"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}


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
