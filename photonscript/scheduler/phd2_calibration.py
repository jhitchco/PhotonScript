"""PHD2 calibration manager, the pure half and the store (PS-93).

Why: PS-72 stopped forcing a calibration at the first target, but nothing
decided when a calibration is needed, where to take it, or whether it is any
good. On 2026-09-25 PHD2 calibrated 20 times; on 2026-09-26 the night ran on a
15.1 deg ortho-error calibration taken at Dec 66.6. The Dec 0 one that night
was still poor (ortho 9.2) because each step was 250 ms: 3 steps per axis.

This module:
  * grade(rec, prev): PASS / WARN / FAIL for one calibration, on top of the
    PS-88 phd2_analysis.calibration_quality numbers;
  * recommended_step_ms(): the PHD2 Calibration Step that gives about 12
    steps over the calibration distance at the measured rate;
  * needs_calibration(): does tonight's sequence need a calibration slot;
  * pick_calibration_field(): a star field near Dec +5 on the first target's
    side of the meridian (pure math, tracking_test's ephemeris);
  * dec_runaway(): the after-flip Dec check on guide_motion.axis_stats;
  * the store under <data_dir>/phd2/ (shared.phd2_store helpers):
        calibration.json      the calibration PHD2 is using now (graded)
        calibrations.jsonl    every graded calibration
        cal_plan.json         tonight's planned calibration slot (armer -> agent)
        live.json             what the RC16 agent last saw of PHD2 (profile,
                              binning, scale, calibrated)
        cal_request.json      a pending manual request (POST /api/phd2/calibrate)
    seed_from_logs() fills an empty store from the newest PS-88 guide-log
    calibration on the first deploy; seed_from_registry() (PS-119) grades
    the calibration PHD2 stored in its profile (scope/calibration) when
    there is no record or it is newer than the one on record.

The live half (grade on CalibrationComplete, retry once during the hold,
the flip record) is telescope_agent/phd2_calmanager.py; the NINA slot is
nina_sequence_json._build_phd2_calibration_container.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.shared import guide_motion as gm
from photonscript.shared import phd2_store as store

logger = logging.getLogger(__name__)

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
MODES = ("auto", "always", "never")
CONTAINER_NAME = "PHD2_CALIBRATION"

ORTHO_MAX_DEG = 5.0       # PHD2 wants the axes within 5 deg of perpendicular
RATIO_TOL = 0.30          # RA/Dec rate ratio vs cos(Dec) x speed ratio
RATE_TOL = 0.30           # absolute rate vs guide speed / scale (WARN only)
MIN_STEPS = 8             # per axis; PHD2 aims for about 12
MIN_STEPS_FAIL = 3        # PS-119: fewer steps than this cannot measure an axis
TARGET_STEPS = 12
MIN_MOVE_PX = 3.0         # an axis that moved less than this did not move
RATIO_DEC_MAX = 60.0      # the RA rate is poorly measured above this |Dec|
LOCATION_DEC_MAX = 20.0   # calibrate within 20 deg of Dec 0 ...
LOCATION_HA_MAX = 3.0     # ... and within 3 h of the meridian
SCALE_TOL = 0.05          # a guide scale change this large invalidates

# Where to calibrate: Dec -5 to +15, 0.25 to 1 h from the meridian on the
# first target's side (no crossing within 15 min), at least 50 deg up.
FIELD_DEC = (-5.0, 15.0)
FIELD_HA = (0.25, 1.0)
FIELD_HA_BEST = 0.6
FIELD_ALT_MIN = 50.0
FIELD_DEC_BEST = 5.0
# Rich open clusters (plus a few globulars) near the celestial equator, J2000.
# The OAG sees only about 8 x 5 arcmin beside the main field, so a dense field
# matters more than the cluster itself.
FIELDS = [
    {"name": "NGC 2244", "ra_hours": 6.532, "dec_degrees": 4.94},
    {"name": "NGC 2264", "ra_hours": 6.683, "dec_degrees": 9.90},
    {"name": "NGC 2301", "ra_hours": 6.863, "dec_degrees": 0.47},
    {"name": "M50", "ra_hours": 7.047, "dec_degrees": -8.33},
    {"name": "M48", "ra_hours": 8.228, "dec_degrees": -5.75},
    {"name": "M67", "ra_hours": 8.855, "dec_degrees": 11.82},
    {"name": "M5", "ra_hours": 15.310, "dec_degrees": 2.08},
    {"name": "M12", "ra_hours": 16.787, "dec_degrees": -1.95},
    {"name": "M10", "ra_hours": 16.952, "dec_degrees": -4.10},
    {"name": "M14", "ra_hours": 17.627, "dec_degrees": -3.25},
    {"name": "IC 4665", "ra_hours": 17.772, "dec_degrees": 5.72},
    {"name": "NGC 6633", "ra_hours": 18.455, "dec_degrees": 6.57},
    {"name": "IC 4756", "ra_hours": 18.652, "dec_degrees": 5.43},
    {"name": "M11", "ra_hours": 18.852, "dec_degrees": -6.27},
    {"name": "NGC 6709", "ra_hours": 18.858, "dec_degrees": 10.35},
    {"name": "NGC 6934", "ra_hours": 20.574, "dec_degrees": 7.40},
    {"name": "M15", "ra_hours": 21.500, "dec_degrees": 12.17},
    {"name": "M2", "ra_hours": 21.558, "dec_degrees": -0.82},
]


def _f(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _r(v, n=2):
    return round(v, n) if isinstance(v, (int, float)) and math.isfinite(v) else None


def cfg_mode(config) -> str:
    m = str(getattr(config, "phd2_cal_mode", "auto") or "auto").strip().lower()
    return m if m in MODES else "auto"


# --------------------------------------------------------------------------
# records
# --------------------------------------------------------------------------

def record_from_log(c: dict, header: dict | None = None) -> dict:
    """A PS-88 phd2_analysis._cal_record (one guide-log calibration) in the
    calibration-manager record shape."""
    h = header or {}
    return {
        "t_utc": c.get("start_utc"), "source": "log", "file": c.get("file"),
        "result": c.get("result"), "pier_side": c.get("pier_side"),
        "dec_deg": c.get("dec_deg"), "ha_hr": c.get("hour_angle_hr"),
        "alt_deg": c.get("alt_deg"),
        "ra": {"angle_deg": (c.get("ra") or {}).get("angle_deg"),
               "rate_px_s": (c.get("ra") or {}).get("rate_px_s"), "parity": None},
        "dec": {"angle_deg": (c.get("dec") or {}).get("angle_deg"),
                "rate_px_s": (c.get("dec") or {}).get("rate_px_s"), "parity": None},
        "steps": dict(c.get("steps") or {}), "moved_px": dict(c.get("moved_px") or {}),
        "step_ms": c.get("step_ms"), "distance_px": c.get("distance_px"),
        "ortho_err_deg": c.get("ortho_err_deg"),
        "message": "; ".join(c.get("messages") or []) or None,
        "profile": h.get("profile"), "binning": h.get("binning"),
        "scale_arcsec_px": h.get("pixel_scale"),
        "focal_length_mm": h.get("focal_length_mm"),
        "ra_speed": h.get("ra_guide_speed"), "dec_speed": h.get("dec_guide_speed"),
    }


def _local_mdy(config, text) -> str | None:
    """PHD2's profile timestamp "10/2/2026 00:28:55" (scope-PC local) as UTC Z."""
    from photonscript.scheduler.phd2_analysis import to_utc
    try:
        loc = datetime.strptime(str(text).strip(), "%m/%d/%Y %H:%M:%S")
    except (TypeError, ValueError):
        return None
    return store.iso_z(to_utc(config, loc))


def _moved(steps_text, n) -> float | None:
    """Distance (px) after n steps from PHD2's "{dx dy}, {dx dy}, ..." list."""
    import re as _re
    pts = [(float(a), float(b)) for a, b in
           _re.findall(r"\{\s*(-?[\d.]+)\s+(-?[\d.]+)\s*\}", str(steps_text or ""))]
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    if not pts or n <= 0 or n >= len(pts):
        return None
    return round(math.hypot(pts[n][0] - pts[0][0], pts[n][1] - pts[0][1]), 1)


def _dword(x) -> int | None:
    """A registry DWORD as a signed int (winreg reads 0xffffffff unsigned)."""
    f = _f(x)
    if f is not None and f >= 2 ** 31:
        f -= 2 ** 32
    return int(f) if f is not None else None


def record_from_registry(config, cal: dict, values: dict | None = None) -> dict:
    """PS-119: a record from the calibration PHD2 keeps in its profile
    (phd2_profile_store.stored_calibration: angles and declination in
    radians, rates in px/ms, guide rates in deg/s), with the profile's
    calibration step and distance."""
    v = {k: (x or {}).get("value") for k, x in (values or {}).items()}
    xa, ya = _f(cal.get("xAngle")), _f(cal.get("yAngle"))
    xa = math.degrees(xa) if xa is not None else None
    ya = math.degrees(ya) if ya is not None else None
    dec = _f(cal.get("declination"))
    dec = math.degrees(dec) if dec is not None and abs(dec) <= math.pi / 2 + 1e-6 else None
    xr, yr = _f(cal.get("xRate")), _f(cal.get("yRate"))
    _i = _dword
    pier = {0: "East", 1: "West"}.get(_i(cal.get("pierSide")))
    par = {1: "+", -1: "-"}
    rs, ds = cal.get("ra_step_count"), cal.get("dec_step_count")
    try:
        issue = int(cal.get("last_issue") or 0)
    except (TypeError, ValueError):
        issue = 0
    from photonscript.scheduler.phd2_profile_store import CAL_ISSUES
    rg, dg = _f(cal.get("ra_guide_rate")), _f(cal.get("dec_guide_rate"))
    return {
        "t_utc": _local_mdy(config, cal.get("timestamp")), "source": "registry",
        "result": "complete", "pier_side": pier,
        "dec_deg": _r(dec, 1), "ha_hr": None, "alt_deg": None,
        "ra": {"angle_deg": _r(xa, 2), "rate_px_s": _r(xr * 1000.0, 3) if xr else None,
               "parity": par.get(_i(cal.get("raGuideParity")))},
        "dec": {"angle_deg": _r(ya, 2), "rate_px_s": _r(yr * 1000.0, 3) if yr else None,
                "parity": par.get(_i(cal.get("decGuideParity")))},
        "steps": {k: int(n) for k, n in (("West", rs), ("North", ds)) if _f(n) is not None},
        "moved_px": {k: m for k, m in (("West", _moved(cal.get("ra_steps"), rs)),
                                       ("North", _moved(cal.get("dec_steps"), ds)))
                     if m is not None},
        "step_ms": _f(v.get("calibration_step_ms")),
        "distance_px": _f(v.get("calibration_distance_px")),
        "ortho_err_deg": _r(_f(cal.get("ortho_error")), 2),
        "message": CAL_ISSUES.get(issue) and f"PHD2 flagged: {CAL_ISSUES.get(issue)}",
        "phd2_issue": issue or None,
        "profile": v.get("name"), "binning": _f(cal.get("binning")),
        "scale_arcsec_px": _f(cal.get("image_scale")),
        "focal_length_mm": _f(cal.get("focal_length")),
        "ra_speed": _r(rg * 3600.0, 3) if rg else None,
        "dec_speed": _r(dg * 3600.0, 3) if dg else None,
    }


def _dec_or_none(v):
    """PHD2 reports an unknown declination as a huge sentinel (997 deg)."""
    d = _f(v)
    return d if d is not None and abs(d) <= 90.0 else None


def record_from_api(cal: dict, *, mount: dict | None = None,
                    steps: dict | None = None, moved_px: dict | None = None,
                    result: str = "complete", message: str | None = None,
                    live: dict | None = None, speeds: dict | None = None,
                    when: datetime | None = None) -> dict:
    """A record from PHD2's get_calibration_data (rates px/s, angles deg),
    plus NINA's mount info (Dec, hour angle, pier side, altitude), the step
    counts seen in PHD2's Calibrating events and the PHD2 profile."""
    from photonscript.telescope_agent.agent import _pier_side
    from photonscript.scheduler.phd2_logs import ortho_error
    cal = cal or {}
    mount = mount or {}
    live = live or {}
    dec = _dec_or_none(cal.get("declination"))
    if dec is None:
        dec = _f(mount.get("Declination"))
    ha = None
    lst, ra = _f(mount.get("SiderealTime")), _f(mount.get("RightAscension"))
    if lst is not None and ra is not None:
        ha = round((lst - ra + 12.0) % 24.0 - 12.0, 2)
    xa, ya = _f(cal.get("xAngle")), _f(cal.get("yAngle"))
    if result == "complete" and not cal.get("calibrated", True):
        result, message = "failed", message or "PHD2 reports no calibration"
    ok = result == "complete"
    return {
        "t_utc": store.iso_z(when or datetime.utcnow()), "source": "phd2",
        "result": result,
        "pier_side": _pier_side(mount.get("SideOfPier")),
        "dec_deg": _r(dec, 1), "ha_hr": ha, "alt_deg": _r(_f(mount.get("Altitude")), 1),
        "ra": {"angle_deg": xa if ok else None,
               "rate_px_s": _f(cal.get("xRate")) if ok else None,
               "parity": cal.get("xParity") if ok else None},
        "dec": {"angle_deg": ya if ok else None,
                "rate_px_s": _f(cal.get("yRate")) if ok else None,
                "parity": cal.get("yParity") if ok else None},
        "steps": dict(steps or {}), "moved_px": dict(moved_px or {}),
        "step_ms": live.get("cal_step_ms"), "distance_px": live.get("cal_distance_px"),
        "ortho_err_deg": ortho_error(xa, ya) if ok else None,
        "message": message,
        "profile": live.get("profile"), "binning": live.get("binning"),
        "scale_arcsec_px": live.get("scale_arcsec_px"),
        "focal_length_mm": live.get("focal_length_mm"),
        "ra_speed": (speeds or {}).get("ra"), "dec_speed": (speeds or {}).get("dec"),
    }


# --------------------------------------------------------------------------
# grading
# --------------------------------------------------------------------------

def recommended_step_ms(distance_px, rate_px_s, target_steps: int = TARGET_STEPS):
    """PHD2 Calibration Step (ms) that covers distance_px in about
    target_steps steps at the measured RA rate (px/s), to the nearest 5 ms.
    09-26 20:17: 25 px / 12 / 41.9 px/s = 50 ms (PHD2 used 250)."""
    d, r = _f(distance_px), _f(rate_px_s)
    if not d or not r or d <= 0 or r <= 0:
        return None
    ms = d / float(target_steps) / r * 1000.0
    return int(max(5, 5 * round(ms / 5.0)))


def grade(rec: dict, prev: dict | None = None) -> dict:
    """Judge one calibration record (see record_from_log / record_from_api).

    FAIL: not completed (failed / aborted), an axis that moved under 3 px,
    ortho error over 5 deg, RA/Dec rate ratio more than 30% from cos(Dec)
    (judged only within 60 deg of the equator), parity different from the
    last good calibration on the same pier side.
    WARN: taken far from Dec 0 or the meridian, an absolute rate 30% off the
    guide speed (PS-92 guide-rate mismatch), fewer than 8 steps on an axis.
    PASS: none of these. Returns {grade, reasons, warnings, quality,
    recommended_step_ms}."""
    from photonscript.scheduler.phd2_analysis import calibration_quality
    reasons, warns = [], []
    ra, de = rec.get("ra") or {}, rec.get("dec") or {}
    dec, ha = _f(rec.get("dec_deg")), _f(rec.get("ha_hr"))
    steps = rec.get("steps") or {}
    moved = rec.get("moved_px") or {}
    result = rec.get("result") or "complete"
    if result != "complete":
        reasons.append(f"calibration {result}"
                       + (f" ({rec['message']})" if rec.get("message") else ""))
    for ax in ("West", "North"):
        m = _f(moved.get(ax))
        if m is not None and m < MIN_MOVE_PX and (steps.get(ax) or 0) >= 3:
            reasons.append(f"star moved only {m:g} px in {steps.get(ax)} {ax} steps "
                           "(pulses not reaching the mount, or a hot pixel)")
    q = {}
    if result == "complete":
        q = calibration_quality(dec, ha, ra.get("angle_deg"), ra.get("rate_px_s"),
                                de.get("angle_deg"), de.get("rate_px_s"),
                                rec.get("scale_arcsec_px"), rec.get("ra_speed"),
                                rec.get("dec_speed"), ortho=rec.get("ortho_err_deg"))
        ortho = q.get("ortho_err_deg")
        if ortho is not None and ortho > ORTHO_MAX_DEG:
            reasons.append(f"axes {ortho:g} deg from perpendicular (max "
                           f"{ORTHO_MAX_DEG:g})")
        rv = q.get("ratio_vs_expected")
        if rv is not None and abs(rv - 1.0) > RATIO_TOL:
            if dec is not None and abs(dec) <= RATIO_DEC_MAX:
                reasons.append(f"RA/Dec rate ratio {rv:g}x of cos(Dec) "
                               f"(tolerance {RATIO_TOL:.0%})")
        for key, name in (("ra_rate_vs_expected", "RA"),
                          ("dec_rate_vs_expected", "Dec")):
            v = q.get(key)
            if v is not None and abs(v - 1.0) > RATE_TOL:
                warns.append(f"{name} rate {v:g}x of the guide speed (check the "
                             "mount's guide rate, PS-92)")
        if prev and prev.get("grade") in (PASS, WARN) \
                and prev.get("pier_side") and prev.get("pier_side") == rec.get("pier_side"):
            for ax, cur in (("RA", ra), ("Dec", de)):
                p0 = ((prev.get("ra") if ax == "RA" else prev.get("dec")) or {}).get("parity")
                p1 = cur.get("parity")
                if p0 in ("+", "-") and p1 in ("+", "-") and p0 != p1:
                    reasons.append(f"{ax} parity {p1} differs from the last good "
                                   f"calibration on the {rec.get('pier_side')} side ({p0})")
    if dec is not None and abs(dec) > LOCATION_DEC_MAX:
        warns.append(f"taken at Dec {dec:g} (calibrate within "
                     f"{LOCATION_DEC_MAX:g} deg of Dec 0)")
    if ha is not None and abs(ha) > LOCATION_HA_MAX:
        warns.append(f"taken {abs(ha):g} h from the meridian")
    rec_ms = recommended_step_ms(rec.get("distance_px"), ra.get("rate_px_s"))
    few = {ax: steps.get(ax) for ax in ("West", "North")
           if steps.get(ax) is not None and steps.get(ax) < MIN_STEPS}
    too_few = {ax: n for ax, n in few.items() if n < MIN_STEPS_FAIL}
    if too_few and result == "complete":
        # PS-119: 2 or 3 steps cannot measure an axis (PHD2 itself flags it)
        reasons.append("too few steps to measure the axis ("
                       + ", ".join(f"{k} {v}" for k, v in too_few.items())
                       + f"; at least {MIN_STEPS_FAIL}, PHD2 aims for about {TARGET_STEPS})"
                       + (f": set PHD2 Calibration Step to about {rec_ms} ms" if rec_ms else ""))
        few = {ax: n for ax, n in few.items() if ax not in too_few}
    if few and result == "complete":
        warns.append("few steps (" + ", ".join(f"{k} {v}" for k, v in few.items())
                     + f"; PHD2 aims for about {TARGET_STEPS})"
                     + (f": set PHD2 Calibration Step to about {rec_ms} ms"
                        if rec_ms else ""))
    g = FAIL if reasons else (WARN if warns else PASS)
    return {"grade": g, "reasons": reasons, "warnings": warns,
            "quality": {k: q.get(k) for k in ("ortho_err_deg", "rate_ratio",
                                              "expected_ratio", "ratio_vs_expected",
                                              "ra_rate_vs_expected",
                                              "dec_rate_vs_expected")},
            "recommended_step_ms": rec_ms}


def graded(rec: dict, prev: dict | None = None) -> dict:
    """rec with its grade fields merged in."""
    out = dict(rec)
    out.update(grade(rec, prev))
    return out


# --------------------------------------------------------------------------
# when
# --------------------------------------------------------------------------

def needs_calibration(record: dict | None, live: dict | None, config,
                      now: datetime | None = None,
                      request: dict | None = None) -> dict:
    """{"needed": bool, "reason": str}: does tonight need a calibration slot?
    phd2_cal_mode never = no (today's PS-72 behavior), always = yes; auto =
    a pending manual request, no record, a FAIL, older than
    phd2_cal_max_age_days, PHD2 reporting no calibration, or the PHD2
    profile / binning / focal length / guide scale changed since."""
    mode = cfg_mode(config)
    if mode == "never":
        return {"needed": False, "reason": "phd2_cal_mode is never"}
    if mode == "always":
        return {"needed": True, "reason": "phd2_cal_mode is always"}
    if request:
        return {"needed": True, "reason": f"manual request ({request.get('mode')}, "
                                          f"{request.get('t_utc')})"}
    if not record:
        return {"needed": True, "reason": "no calibration on record"}
    if record.get("grade") == FAIL:
        return {"needed": True, "reason": "last calibration FAILED: "
                + "; ".join(record.get("reasons") or [])}
    now = now or datetime.utcnow()
    t = store.parse_z(record.get("t_utc"))
    max_age = float(getattr(config, "phd2_cal_max_age_days", 30) or 30)
    if t is not None and now - t > timedelta(days=max_age):
        return {"needed": True, "reason": f"calibration is {(now - t).days} days "
                                          f"old (max {max_age:g})"}
    live = live or {}
    if live.get("calibrated") is False:
        return {"needed": True, "reason": "PHD2 reports no calibration"}
    for key, name in (("profile", "PHD2 profile"), ("binning", "guide binning"),
                      ("focal_length_mm", "guide focal length")):
        a, b = record.get(key), live.get(key)
        if a not in (None, "") and b not in (None, "") and str(a) != str(b):
            return {"needed": True, "reason": f"{name} changed ({a} -> {b})"}
    a, b = _f(record.get("scale_arcsec_px")), _f(live.get("scale_arcsec_px"))
    if a and b and abs(a - b) / a > SCALE_TOL:
        return {"needed": True, "reason": f"guide scale changed ({a:g} -> {b:g} \"/px)"}
    return {"needed": False, "reason": f"calibration of {record.get('t_utc')} is "
                                       f"{record.get('grade') or 'ungraded'}"}


# --------------------------------------------------------------------------
# where
# --------------------------------------------------------------------------

def pick_calibration_field(config, when_utc: datetime,
                           first_target_ha: float | None = None,
                           fields: list | None = None) -> dict:
    """The field to calibrate on at when_utc. On the first target's side of
    the meridian (east = hour angle -1.0 to -0.25 h, west = +0.25 to +1.0 h;
    unknown = west), Dec -5 to +15, at least 50 deg up; best = hour angle
    nearest 0.6 h, then Dec nearest +5. When no listed field fits, a
    synthetic point at hour angle 0.6 h, Dec +5 (source 'fallback')."""
    from photonscript.scheduler.tracking_test import altitude, hour_angle, lst_hours
    lat = float(getattr(config, "observatory_lat", 31.9))
    lon = float(getattr(config, "observatory_lon", -109.0))
    side = -1 if (first_target_ha is not None and first_target_ha < 0) else 1
    lo, hi = FIELD_HA
    best, best_key = None, None
    for c in fields or FIELDS:
        dec = float(c["dec_degrees"])
        if not FIELD_DEC[0] <= dec <= FIELD_DEC[1]:
            continue
        ha = hour_angle(float(c["ra_hours"]), when_utc, lon)
        if not lo <= side * ha <= hi:
            continue
        alt = altitude(float(c["ra_hours"]), dec, when_utc, lat, lon)
        if alt < FIELD_ALT_MIN:
            continue
        key = (abs(abs(ha) - FIELD_HA_BEST), abs(dec - FIELD_DEC_BEST))
        if best_key is None or key < best_key:
            best_key = key
            best = dict(c, ha_hours=round(ha, 2), alt_deg=round(alt, 1),
                        source="list")
    if best is None:
        ra = (lst_hours(when_utc, lon) - side * FIELD_HA_BEST) % 24.0
        best = {"name": f"Calibration field HA {side * FIELD_HA_BEST:+.1f}h Dec +5",
                "ra_hours": round(ra, 4), "dec_degrees": FIELD_DEC_BEST,
                "ha_hours": round(side * FIELD_HA_BEST, 2),
                "alt_deg": round(altitude(ra, FIELD_DEC_BEST, when_utc, lat, lon), 1),
                "source": "fallback"}
    best["side"] = "east" if side < 0 else "west"
    best["for_utc"] = store.iso_z(when_utc)
    return best


# --------------------------------------------------------------------------
# after a meridian flip
# --------------------------------------------------------------------------

def dec_runaway(frames, y_rate_px_s, scale, max_ms=None) -> dict:
    """After a flip, does Dec run away in one direction? On
    guide_motion.axis_stats: the Dec response is 'reversed' (corrections push
    the star further), or nearly every Dec pulse goes one way while the Dec
    error grows (2 px or more) and drifts at least 1"/min."""
    win = [f for f in frames if not f.get("drop") and not f.get("settling")]
    if len(win) < 10:
        return {"runaway": None, "reason": "too few frames", "stats": {}}
    if max_ms is None:
        max_ms = max((f["dec_ms"] for f in win), default=0) or None
    st = gm.axis_stats(win, "dec", "dec_ms", "dec_dir", y_rate_px_s, max_ms, scale)
    if st.get("response_verdict") == "reversed":
        return {"runaway": True, "reason": f"Dec response reversed "
                                           f"({st.get('response')})", "stats": st}
    one_way = st["pulses"] >= 10 and (st.get("dominant_pct") or 0) >= 90.0
    first, last = st.get("error_first_px"), st.get("error_last_px")
    growing = (first is not None and last is not None
               and abs(last) - abs(first) >= 2.0)
    drift = st.get("drift_arcsec_min")
    drifting = drift is not None and abs(drift) >= 1.0
    if one_way and growing and drifting:
        return {"runaway": True,
                "reason": f"{st['dominant_pct']:g}% of Dec pulses {st['dominant']} "
                          f"while the Dec error grew {first:g} -> {last:g} px",
                "stats": st}
    return {"runaway": False, "reason": "Dec holds", "stats": st}


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------

def active_path(config) -> Path:
    return store.phd2_dir(config) / "calibration.json"


def history_path(config) -> Path:
    return store.phd2_dir(config) / "calibrations.jsonl"


def plan_path(config) -> Path:
    return store.phd2_dir(config) / "cal_plan.json"


def live_path(config) -> Path:
    return store.phd2_dir(config) / "live.json"


def request_path(config) -> Path:
    return store.phd2_dir(config) / "cal_request.json"


def history(config, days: int | None = None) -> list[dict]:
    rows = store.read_jsonl(history_path(config))
    if days:
        cut = datetime.utcnow() - timedelta(days=int(days))
        rows = [r for r in rows if (store.parse_z(r.get("t_utc")) or cut) >= cut]
    return rows


def last_good(config, pier: str | None = None) -> dict | None:
    for r in reversed(history(config)):
        if r.get("grade") in (PASS, WARN) and (pier is None or r.get("pier_side") == pier):
            return r
    return None


def save_record(config, rec: dict, active: bool = True) -> dict:
    """Append to the history and (active=True) make it the calibration PHD2
    uses now. Keeps the flip results of the active record when the new one
    is the same calibration (a re-read)."""
    store.append_jsonl(history_path(config), rec)
    if active:
        try:
            store.write_json(active_path(config), rec)
        except OSError as e:
            logger.warning("calibration record not saved: %s", e)
    return rec


def load_active(config, seed: bool = True) -> dict | None:
    rec = store.read_json(active_path(config))
    if rec is None and seed:
        rec = seed_from_logs(config)
    return rec


def seed_from_logs(config, max_files: int = 5) -> dict | None:
    """First deploy: take the newest completed calibration in the PHD2 guide
    logs (PS-88 parser), grade it and store it as the active record. None
    when there is no log or no completed calibration."""
    try:
        from photonscript.scheduler import phd2_analysis as pa
        from photonscript.scheduler import phd2_logs as pl
        files = pl.find_logs(config, "guide")["files"]
    except Exception as e:  # noqa: BLE001
        logger.debug("calibration seed: no logs (%s)", e)
        return None
    for p in reversed(files[-max_files:]):
        try:
            secs = pl.parse_guide_log(Path(p).read_text(encoding="utf-8",
                                                        errors="replace"), Path(p).name)
        except OSError:
            continue
        cals = [(s, pa._cal_record(i, s, config))
                for i, s in enumerate(s for s in secs if s["kind"] == "calibration")]
        done = [(s, c) for s, c in cals if c["result"] == "complete"]
        if not done:
            continue
        sec, c = done[-1]
        rec = graded(record_from_log(c, sec.get("header")))
        rec["context"] = "seed"
        t = store.parse_z(rec.get("t_utc"))
        rec["night"] = store.night_of(config, t) if t else None
        save_record(config, rec)
        logger.info("calibration seeded from %s: %s %s", Path(p).name,
                    rec["grade"], "; ".join(rec["reasons"] + rec["warnings"]))
        return rec
    return None


def seed_from_registry(config, cal: dict | None, values: dict | None = None,
                       min_newer_s: float = 600.0) -> dict | None:
    """PS-119: grade the calibration PHD2 stored in its profile and make it
    the active record when there is none, or it is more than min_newer_s
    newer than the active one (the RC16 agent's live grade of the same
    calibration stays). Returns the new record, else None. Never raises."""
    try:
        if not cal:
            return None
        rec = record_from_registry(config, cal, values)
        t = store.parse_z(rec.get("t_utc"))
        if t is None:
            return None
        cur = load_active(config, seed=False)
        ct = store.parse_z((cur or {}).get("t_utc"))
        if cur and ct and (t - ct).total_seconds() <= min_newer_s:
            return None
        rec = graded(rec, last_good(config, rec.get("pier_side")))
        rec["context"] = "registry"
        rec["night"] = store.night_of(config, t)
        save_record(config, rec)
        logger.info("calibration graded from the PHD2 profile: %s %s", rec["grade"],
                    "; ".join(rec["reasons"] + rec["warnings"]))
        return rec
    except Exception as e:  # noqa: BLE001
        logger.debug("calibration from the registry failed: %s", e)
        return None


def set_flip(config, pier: str | None, ok: bool, detail: str) -> dict | None:
    """Record the after-flip Dec check on the active calibration:
    flip[pier] = {ok, detail, t_utc}."""
    rec = store.read_json(active_path(config))
    if rec is None or not pier:
        return None
    rec.setdefault("flip", {})[pier] = {"ok": bool(ok), "detail": detail,
                                        "t_utc": store.iso_z(datetime.utcnow())}
    try:
        store.write_json(active_path(config), rec)
    except OSError as e:
        logger.warning("flip result not saved: %s", e)
    return rec


def note_flipped(config, detail: str = "PHD2 flipped the calibration data") -> None:
    """PHD2's CalibrationDataFlipped: kept on the active record."""
    rec = store.read_json(active_path(config))
    if rec is None:
        return
    rec.setdefault("flipped", []).append({"t_utc": store.iso_z(datetime.utcnow()),
                                          "detail": detail})
    rec["flipped"] = rec["flipped"][-10:]
    try:
        store.write_json(active_path(config), rec)
    except OSError:
        pass


def load_live(config) -> dict:
    return store.read_json(live_path(config)) or {}


def save_live(config, live: dict) -> None:
    try:
        store.write_json(live_path(config), dict(live, t_utc=store.iso_z(datetime.utcnow())))
    except OSError as e:
        logger.debug("live PHD2 snapshot not saved: %s", e)


def pending_request(config) -> dict | None:
    return store.read_json(request_path(config))


def set_request(config, mode: str) -> dict:
    req = {"mode": mode, "t_utc": store.iso_z(datetime.utcnow())}
    store.write_json(request_path(config), req)
    return req


def clear_request(config) -> None:
    try:
        request_path(config).unlink()
    except OSError:
        pass


def load_plan(config) -> dict | None:
    return store.read_json(plan_path(config))


def save_plan(config, plan: dict | None) -> None:
    if plan is None:
        try:
            plan_path(config).unlink()
        except OSError:
            pass
        return
    try:
        store.write_json(plan_path(config), plan)
    except OSError as e:
        logger.warning("calibration plan not saved: %s", e)


def plan_active(plan: dict | None, now: datetime | None = None) -> bool:
    """A planned slot that is still waiting for (or retrying) its calibration,
    and not older than 14 h."""
    if not plan or plan.get("status") not in ("pending", "graded", "retrying"):
        return False
    t = store.parse_z(plan.get("created_utc"))
    now = now or datetime.utcnow()
    return t is not None and now - t < timedelta(hours=14)


def summary(config, date: str | None = None) -> dict:
    """The calibration block for the API, the System page and the night
    report: the active record, tonight's plan, stale / poor flags (for the
    PS-89 audit) and the recommended step."""
    rec = load_active(config, seed=False)
    max_age = float(getattr(config, "phd2_cal_max_age_days", 30) or 30)
    t = store.parse_z((rec or {}).get("t_utc"))
    age = round((datetime.utcnow() - t).total_seconds() / 86400.0, 1) if t else None
    plan = load_plan(config)
    rows = history(config)
    if date:
        rows = [r for r in rows if r.get("night") == date]
    return {"mode": cfg_mode(config),
            "record": rec,
            "grade": (rec or {}).get("grade"),
            "age_days": age,
            "stale": bool(age is not None and age > max_age),
            "poor": (rec or {}).get("grade") == FAIL,
            "recommended_step_ms": (rec or {}).get("recommended_step_ms"),
            "flip": (rec or {}).get("flip") or {},
            "plan": plan if (plan and (not date or plan.get("night") == date)) else None,
            "request": pending_request(config),
            "tonight": [{k: r.get(k) for k in ("t_utc", "context", "grade", "reasons",
                                                "warnings", "dec_deg", "ha_hr",
                                                "pier_side", "ortho_err_deg")}
                        for r in rows] if date else None}
