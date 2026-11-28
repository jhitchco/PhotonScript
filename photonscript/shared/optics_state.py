"""PS-164: the optical-train state a frame was shot at, from its FITS header.

NINA writes the focuser position as FOCPOS (and FOCTEMP); a rotator, when
one is connected, as ROTATOR (mechanical angle, deg) or ROTATANG. Neither
rig has a rotator today, so `rotator_deg` is normally None. Subs records
(runs/<night>_subs.jsonl) and the calibration QA store's flat records carry
both fields, so the calibration coverage (PS-160) can tell flats shot at a
different focus or rotation from the lights they calibrate.
"""

from __future__ import annotations

FOCUS_KEYS = ("FOCPOS", "FOCUSPOS")
ROTATOR_KEYS = ("ROTATOR", "ROTATANG", "ROTANGLE")


def _num(v):
    try:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _first(hdr, keys):
    for k in keys:
        try:
            v = hdr.get(k)
        except Exception:  # noqa: BLE001 - odd header objects
            v = None
        n = _num(v)
        if n is not None:
            return n
    return None


def optics_fields(hdr) -> dict:
    """{"focpos": int | None, "rotator_deg": float | None} from a header
    (astropy Header or dict). Always both keys, None when absent."""
    hdr = hdr or {}
    fp = _first(hdr, FOCUS_KEYS)
    rot = _first(hdr, ROTATOR_KEYS)
    return {"focpos": int(round(fp)) if fp is not None else None,
            "rotator_deg": round(rot, 2) if rot is not None else None}


def angle_diff(a: float, b: float) -> float:
    """Smallest absolute difference of two angles in degrees."""
    d = abs(float(a) - float(b)) % 360.0
    return min(d, 360.0 - d)
