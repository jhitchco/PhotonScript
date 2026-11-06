"""PS-71: grade out frames shot with the roof closed or the mount parked.

2026-09-26 11:46-12:17Z: seven RC16 OIII 300 s "lights" passed QA with
background 257 ADU (the bias floor), 12-22 "stars", HFR 1.4-1.7 px and
FWHM 0.57". Good subs that night read HFR 5-8 px / 2.0-2.4". They were dark
frames: the sequence kept exposing inside the closed roof (PS-77) and the
extractor graded hot pixels as stars.

Three signatures, judged per rig (pass the rig's config view):
  1. sub-physical stars: star size below the optics' physical floor
     (quality_fwhm_min_arcsec). Judged on FWHM ~ 2 x HFR x scale and on the
     measured FWHM when the grader has one; BOTH must be under the floor.
  2. bias-floor background: background <= default_offset +
     quality_bias_floor_margin_adu, i.e. no sky signal. On its own this
     only rejects lights of quality_bias_floor_min_exp_s or longer, because
     short narrowband subs legitimately sit at the floor (09-26 Ha 60 s:
     257 ADU with 156 real stars). With (1) it is a dark-frame signature at
     any length.
  3. shot while unsafe: the exposure overlaps a window the safety monitor
     read UNSAFE (shared.safety_history).
(2) and (3) flag the sub "roof-closed" so the review UI can say why.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

ROOF_CLOSED = "roof-closed"
HOT_PIXELS = "hot-pixels"


@dataclass
class Verdict:
    reasons: list[str] = field(default_factory=list)
    flag: str = ""

    @property
    def reject(self) -> bool:
        return bool(self.reasons)


def star_size_arcsec(hfr_px, fwhm_arcsec, pixel_scale) -> float | None:
    """Largest available star-size estimate in arcsec (None if none)."""
    est = []
    try:
        if hfr_px and float(hfr_px) > 0:
            est.append(2.0 * float(hfr_px) * float(pixel_scale))
    except (TypeError, ValueError):
        pass
    try:
        if fwhm_arcsec and float(fwhm_arcsec) > 0:
            est.append(float(fwhm_arcsec))
    except (TypeError, ValueError):
        pass
    return max(est) if est else None


def parked_frame_verdict(config, *, hfr_px=None, fwhm_arcsec=None,
                         background=None, exp_s=None, stars=None,
                         start_utc: datetime | None = None,
                         unsafe_windows: list | None = None,
                         image_type: str = "LIGHT") -> Verdict:
    """Signatures 1-3 for one LIGHT. `unsafe_windows` [(from, to)] naive
    UTC; pass None to skip the unsafe check (no history available)."""
    v = Verdict()
    if str(image_type or "LIGHT").upper().find("LIGHT") < 0:
        return v
    scale = float(getattr(config, "pixel_scale_arcsec", 1.0) or 1.0)
    floor = float(getattr(config, "quality_fwhm_min_arcsec", 0.0) or 0.0)
    size = star_size_arcsec(hfr_px, fwhm_arcsec, scale)
    tiny = bool(floor > 0 and size is not None and (stars or 0) > 0
                and size < floor)

    offset = float(getattr(config, "default_offset", 0) or 0)
    margin = float(getattr(config, "quality_bias_floor_margin_adu", 6.0))
    min_exp = float(getattr(config, "quality_bias_floor_min_exp_s", 600.0))
    at_floor = False
    try:
        at_floor = (background is not None and offset > 0
                    and float(background) <= offset + margin)
    except (TypeError, ValueError):
        pass
    try:
        exp = float(exp_s or 0)
    except (TypeError, ValueError):
        exp = 0.0

    if tiny and at_floor:
        v.reasons.append(
            f"dark-frame signature: background {float(background):g} ADU at "
            f"the bias floor and stars {size:.2f}\" under the {floor:g}\" "
            "physical floor (hot pixels, not stars): roof closed / parked")
        v.flag = ROOF_CLOSED
    elif tiny:
        v.reasons.append(f"stars {size:.2f}\" under the {floor:g}\" physical "
                         "floor: hot pixels, not stars")
        v.flag = HOT_PIXELS
    elif at_floor and exp >= min_exp:
        v.reasons.append(
            f"background {float(background):g} ADU at the bias floor "
            f"(<= {offset:g}+{margin:g}) on a {exp:g} s light: no sky "
            "signal, roof closed / parked")
        v.flag = ROOF_CLOSED

    if (unsafe_windows and start_utc is not None
            and getattr(config, "quality_reject_unsafe_subs", True)):
        from photonscript.shared.safety_history import overlap_seconds
        end = start_utc + timedelta(seconds=exp)
        ov = overlap_seconds(unsafe_windows, start_utc, end)
        if ov > 0:
            v.reasons.append(f"shot while the safety monitor read UNSAFE "
                             f"({ov:.0f} s of {exp:g} s): roof closed / parked")
            v.flag = ROOF_CLOSED
    return v


def exposure_start(date_obs, end_utc=None, exp_s=None) -> datetime | None:
    """Exposure start in naive UTC: DATE-OBS when present, else the record's
    end time minus the exposure."""
    from photonscript.shared.safety_history import _utc
    t = _utc(date_obs) if date_obs else None
    if t is not None:
        return t
    e = _utc(end_utc) if end_utc else None
    if e is None:
        return None
    try:
        return e - timedelta(seconds=float(exp_s or 0))
    except (TypeError, ValueError):
        return e
