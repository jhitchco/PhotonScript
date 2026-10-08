"""PS-171: TPoint mapping run (sample N sky points into TheSky's TPoint model).

The Paramount MX drifts ~0.4"/min in RA and ~0.3"/min in Dec unguided;
a denser TPoint model (with ProTrack) is the fix. TheSky's own Automated
Pointing Calibration Run needs TheSky to own the camera, which NINA #1
does. This recipe keeps NINA in charge:

* the sequence (``build``): N alt/az points spread evenly over the sky
  above ``tpoint_mapping_min_alt`` (an equal-area spiral), points near the
  moon, near the meridian (either pier side could be chosen) and under the
  pole (|HA| > ``tpoint_mapping_max_ha_h``) dropped, ordered east of the
  meridian first, then west (one pier flip), in hour-angle strips walked
  serpentine in Dec (short slews). For each point NINA does Slew to Alt/Az
  (no center, no sync), one short L frame, then the tpoint-sample
  ExternalScript (telescope_agent.tpoint_sample: TheSky Image Link on the
  frame, then a TPoint sample when allowed, always a CSV row).
* the sideload recipe tpoint_mapping_then_tonight splices it into the
  Targets area before the night loop (runs once), tonight's targets follow.

Pure geometry (grid, conversions, separation, ordering) is unit-tested;
the moon position comes from astropy and is optional (no moon skip, said
so, when it cannot be computed).
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_POINTS = 60
MAX_POINTS = 300
MIN_POINTS = 3
DEFAULT_MIN_ALT = 30.0
MAX_ALT = 85.0                 # no zenith passes (az changes fast up there)
MERIDIAN_BAND_H = 0.25         # |HA| under this: pier side ambiguous, skipped
DEFAULT_MAX_HA_H = 6.0         # beyond: under the pole, skipped
DEFAULT_MOON_DEG = 15.0
HA_STRIP_H = 1.0               # ordering: hour-angle strip width
EAST, WEST = "east", "west"
_GOLDEN_DEG = 180.0 * (3.0 - math.sqrt(5.0))   # ~137.5 deg


# ------------------------------------------------------------- settings

def _f(config, key: str, default: float) -> float:
    try:
        v = getattr(config, key, default)
        return float(default if v is None or v == "" else v)
    except (TypeError, ValueError):
        return float(default)


def params(config) -> dict:
    """The run's settings from the config (PS_TPOINT_MAPPING_*), clamped."""
    n = int(_f(config, "tpoint_mapping_points", DEFAULT_POINTS))
    return {
        "points": max(MIN_POINTS, min(n, MAX_POINTS)),
        "min_alt": max(15.0, min(_f(config, "tpoint_mapping_min_alt",
                                    DEFAULT_MIN_ALT), 70.0)),
        "exposure_s": max(1.0, min(_f(config, "tpoint_mapping_exposure_s",
                                      5.0), 60.0)),
        "binning": max(1, min(int(_f(config, "tpoint_mapping_binning", 2)), 4)),
        "moon_deg": max(0.0, _f(config, "tpoint_mapping_moon_deg",
                                DEFAULT_MOON_DEG)),
        "max_ha_h": max(1.0, min(_f(config, "tpoint_mapping_max_ha_h",
                                    DEFAULT_MAX_HA_H), 12.0)),
    }


# ------------------------------------------------------------- geometry

def grid(n: int, min_alt: float = DEFAULT_MIN_ALT,
         max_alt: float = MAX_ALT) -> list[tuple[float, float]]:
    """n (alt, az) points (deg) spread evenly over the sky cap between
    min_alt and max_alt: an equal-area Fibonacci spiral (sin(alt) uniform,
    az stepping by the golden angle)."""
    n = max(0, int(n))
    lo, hi = math.sin(math.radians(min_alt)), math.sin(math.radians(max_alt))
    out = []
    for i in range(n):
        z = lo + (hi - lo) * (i + 0.5) / n
        out.append((round(math.degrees(math.asin(z)), 2),
                    round((i * _GOLDEN_DEG) % 360.0, 2)))
    return out


def altaz_to_hadec(alt: float, az: float, lat: float) -> tuple[float, float]:
    """(hour angle h in -12..12, Dec deg) for an alt / az (deg, az from
    north through east) at latitude lat. HA < 0 is east of the meridian."""
    a, z, p = map(math.radians, (alt, az, lat))
    sd = math.sin(a) * math.sin(p) + math.cos(a) * math.cos(p) * math.cos(z)
    dec = math.asin(max(-1.0, min(1.0, sd)))
    y = -math.sin(z) * math.cos(a)
    x = math.sin(a) * math.cos(p) - math.cos(a) * math.sin(p) * math.cos(z)
    ha = math.degrees(math.atan2(y, x)) / 15.0
    return ((ha + 12.0) % 24.0) - 12.0, math.degrees(dec)


def separation(alt1: float, az1: float, alt2: float, az2: float) -> float:
    """Great-circle distance (deg) between two alt / az positions."""
    a1, a2 = math.radians(alt1), math.radians(alt2)
    dz = math.radians(az1 - az2)
    c = (math.sin(a1) * math.sin(a2)
         + math.cos(a1) * math.cos(a2) * math.cos(dz))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def side_of(ha_h: float) -> str:
    return EAST if ha_h < 0 else WEST


def order_points(points: list[dict]) -> list[dict]:
    """East of the meridian first, then west (one pier flip). Each side in
    hour-angle strips of HA_STRIP_H, walked from the east horizon toward the
    meridian and on to the west, Dec alternating up / down per strip
    (serpentine), so consecutive slews stay short."""
    out: list[dict] = []
    for side in (EAST, WEST):
        pts = [p for p in points if p["side"] == side]
        strips: dict[int, list[dict]] = {}
        for p in pts:
            strips.setdefault(int(math.floor(p["ha_h"] / HA_STRIP_H)), []).append(p)
        for k, key in enumerate(sorted(strips)):
            out += sorted(strips[key], key=lambda p: p["dec_deg"],
                          reverse=bool(k % 2))
    return out


def plan_points(n: int, lat: float, min_alt: float = DEFAULT_MIN_ALT,
                moon_altaz: list | None = None,
                moon_deg: float = DEFAULT_MOON_DEG,
                max_ha_h: float = DEFAULT_MAX_HA_H) -> dict:
    """The run's points: n kept points in run order (the spiral is made
    denser until n survive the skips, at most 4x), plus what was skipped.
    moon_altaz: moon (alt, az) positions over the run (start, middle, end);
    a point within moon_deg of any of them is skipped (a moon below the
    horizon skips nothing). {"points": [{"alt", "az", "ha_h", "dec_deg",
    "side"}], "skipped": {"moon", "meridian", "pole"}, "candidates"}."""
    n = max(MIN_POINTS, min(int(n), MAX_POINTS))
    moons = [(float(a), float(z)) for a, z in (moon_altaz or [])
             if a is not None and float(a) > -1.0]
    best = None
    for m in range(n, 4 * n + 1, max(1, n // 10)):
        kept, skipped = [], {"moon": 0, "meridian": 0, "pole": 0}
        for alt, az in grid(m, min_alt):
            ha, dec = altaz_to_hadec(alt, az, lat)
            if abs(ha) < MERIDIAN_BAND_H:
                skipped["meridian"] += 1
                continue
            if abs(ha) > max_ha_h:
                skipped["pole"] += 1
                continue
            if moon_deg > 0 and any(separation(alt, az, ma, mz) < moon_deg
                                    for ma, mz in moons):
                skipped["moon"] += 1
                continue
            kept.append({"alt": alt, "az": az, "ha_h": round(ha, 3),
                         "dec_deg": round(dec, 3), "side": side_of(ha)})
        best = {"points": kept, "skipped": skipped, "candidates": m}
        if len(kept) >= n:
            break
    pts = best["points"]
    if len(pts) > n:
        # thin evenly (keeps the spread) rather than cut the spiral's top
        step = len(pts) / n
        pts = [pts[int(i * step)] for i in range(n)]
    best["points"] = order_points(pts)
    return best


# ------------------------------------------------------------- the night

def start_time(config, at: str = "", now: datetime | None = None) -> datetime:
    """When the run starts (UTC, naive): `at` if given, else now, or
    tonight's astronomical dusk when that is later (an afternoon preview)."""
    from photonscript.scheduler.optics_test import parse_at
    when = parse_at(at)
    if when:
        return when
    now = (now or datetime.utcnow()).replace(tzinfo=None)
    try:
        from photonscript.shared.astronomy import get_twilight_times
        tw = get_twilight_times(config.get_observatory(), now.replace(
            hour=0, minute=0, second=0, microsecond=0))
        dusk = tw.get("astro_dark_start")
        if dusk and now < dusk.replace(tzinfo=None):
            return dusk.replace(tzinfo=None)
    except Exception as e:  # noqa: BLE001
        logger.debug("tpoint mapping: no dusk (%s)", e)
    return now


def moon_track(config, when: datetime, hours: float) -> list | None:
    """Moon (alt, az) at the start, middle and end of the run; None when it
    cannot be computed (no moon skip then)."""
    try:
        from astropy import units as u
        from astropy.coordinates import AltAz, get_body
        from astropy.time import Time
        from photonscript.shared.astronomy import get_earth_location
        loc = get_earth_location(config.get_observatory())
        t = Time([when, when + timedelta(hours=hours / 2),
                  when + timedelta(hours=hours)])
        aa = get_body("moon", t, loc).transform_to(AltAz(obstime=t, location=loc))
        return [(round(float(a), 2), round(float(z), 2))
                for a, z in zip(aa.alt.deg, aa.az.deg)]
    except Exception as e:  # noqa: BLE001
        logger.warning("tpoint mapping: moon position failed (%s)", e)
        return None


def sample_script(config) -> str | None:
    """deploy\\tpoint-sample.cmd when it exists on this machine (the armer
    generates on the scope PC), else None: then the run takes frames only
    and the sequence says so."""
    from pathlib import Path
    path = str(getattr(config, "tpoint_sample_script", "") or "").strip()
    if not path:
        return None
    try:
        return path if Path(path).is_file() else None
    except OSError:
        return None


def build(config, at: str = "", now: datetime | None = None) -> dict:
    """The run for `at` (or tonight): {"field", "points", "skipped",
    "moon", "test_json", "params", "script"}. field is what the sideload
    view shows (name, nominal zenith RA / Dec, length)."""
    from photonscript.scheduler.nina_sequence_json import (
        generate_tpoint_mapping_json, tpoint_mapping_duration_s,
        tpoint_mapping_name)
    from photonscript.scheduler.tracking_test import lst_hours
    p = params(config)
    lat = float(getattr(config, "observatory_lat", 31.9))
    lon = float(getattr(config, "observatory_lon", -109.0))
    when = start_time(config, at, now)
    hours = tpoint_mapping_duration_s(p["points"], p["exposure_s"]) / 3600
    moon = moon_track(config, when, hours)
    plan = plan_points(p["points"], lat, p["min_alt"], moon, p["moon_deg"],
                       p["max_ha_h"])
    pts = plan["points"]
    script = sample_script(config)
    zen_ra = lst_hours(when, lon)
    test_json = generate_tpoint_mapping_json(
        [[q["alt"], q["az"], q["side"]] for q in pts], ra_hours=zen_ra,
        dec_degrees=lat, exposure_s=p["exposure_s"], binning=p["binning"],
        script=script or "", min_altitude=p["min_alt"])
    est_min = round(tpoint_mapping_duration_s(len(pts), p["exposure_s"]) / 60)
    field = {"name": tpoint_mapping_name(len(pts)),
             "ra_hours": round(zen_ra, 4), "dec_degrees": round(lat, 4),
             "points": len(pts),
             "east": sum(1 for q in pts if q["side"] == EAST),
             "west": sum(1 for q in pts if q["side"] == WEST),
             "est_minutes": est_min,
             "for_utc": when.strftime("%Y-%m-%dT%H:%MZ"),
             "moon_skip": ("off (moon position unavailable)" if moon is None
                           else f"{plan['skipped']['moon']} near the moon"),
             "sample_script": script or "MISSING (frames only)",
             "source": "tpoint_mapping"}
    sk = plan["skipped"]
    field["summary"] = (
        f"{field['east']} points east of the meridian, then {field['west']} "
        f"west (one flip), {p['exposure_s']:g} s L bin {p['binning']}, "
        f"~{est_min} min from {field['for_utc']}; skipped "
        f"{sk['meridian']} near the meridian, {sk['pole']} under the pole, "
        f"{field['moon_skip']}; sample script {field['sample_script']}; "
        f"TPoint add {getattr(config, 'tpoint_sample_add', 'off')}")
    return {"field": field, "points": pts, "skipped": plan["skipped"],
            "moon": moon, "test_json": test_json, "params": p,
            "script": script}

