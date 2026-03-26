"""Astronomical calculations — visibility, altitude, transit, seasonal planning."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import numpy as np
from astropy.coordinates import EarthLocation, SkyCoord, AltAz, get_sun
from astropy.time import Time
import astropy.units as u

from photonscript.shared.models import CelestialTarget, ObservatoryLocation, TargetTier


def get_earth_location(obs: ObservatoryLocation) -> EarthLocation:
    return EarthLocation(lat=obs.latitude * u.deg, lon=obs.longitude * u.deg, height=obs.elevation * u.m)


def get_sky_coord(target: CelestialTarget) -> SkyCoord:
    return SkyCoord(ra=target.ra_hours * 15 * u.deg, dec=target.dec_degrees * u.deg)


def compute_altitude(
    target: CelestialTarget,
    obs: ObservatoryLocation,
    time_utc: datetime,
) -> float:
    """Return altitude in degrees for a target at a given time."""
    location = get_earth_location(obs)
    t = Time(time_utc)
    altaz_frame = AltAz(obstime=t, location=location)
    coord = get_sky_coord(target)
    altaz = coord.transform_to(altaz_frame)
    return float(altaz.alt.deg)


def compute_transit_time(
    target: CelestialTarget,
    obs: ObservatoryLocation,
    date_utc: datetime,
) -> Optional[datetime]:
    """Approximate meridian transit time for the target on a given night."""
    location = get_earth_location(obs)
    coord = get_sky_coord(target)

    # Compute LST at midnight local
    midnight = Time(date_utc.replace(hour=7, minute=0, second=0))  # ~midnight MST in UTC
    lst_midnight = midnight.sidereal_time("mean", longitude=obs.longitude * u.deg)

    # Hour angle = LST - RA
    ha = lst_midnight.hour - target.ra_hours
    # Transit occurs when HA = 0, so offset from midnight
    transit_offset_hours = -ha
    if transit_offset_hours > 12:
        transit_offset_hours -= 24
    elif transit_offset_hours < -12:
        transit_offset_hours += 24

    transit_time = midnight.datetime + timedelta(hours=transit_offset_hours)
    return transit_time


def get_twilight_times(
    obs: ObservatoryLocation,
    date_utc: datetime,
) -> dict[str, datetime]:
    """Compute astronomical twilight start/end (sun at -18 deg) for a given night."""
    location = get_earth_location(obs)
    # Scan around sunset/sunrise
    evening = Time(date_utc.replace(hour=23, minute=0))  # ~5pm MST in UTC
    morning = Time(date_utc.replace(hour=13, minute=0)) + timedelta(days=1)  # ~6am MST next day

    times = Time(np.linspace(evening.jd, morning.jd, 200), format="jd")
    altaz_frame = AltAz(obstime=times, location=location)
    sun_alts = get_sun(times).transform_to(altaz_frame).alt.deg

    # Find where sun crosses -18 degrees
    astro_dark_start = None
    astro_dark_end = None

    for i in range(len(sun_alts) - 1):
        if sun_alts[i] > -18 and sun_alts[i + 1] <= -18:
            astro_dark_start = times[i].datetime
        if sun_alts[i] <= -18 and sun_alts[i + 1] > -18:
            astro_dark_end = times[i + 1].datetime

    return {
        "astro_dark_start": astro_dark_start,
        "astro_dark_end": astro_dark_end,
    }


def compute_visibility_window(
    target: CelestialTarget,
    obs: ObservatoryLocation,
    date_utc: datetime,
    min_altitude: float = 30.0,
) -> dict:
    """Compute when a target is above min_altitude during astronomical darkness."""
    twilight = get_twilight_times(obs, date_utc)
    dark_start = twilight.get("astro_dark_start")
    dark_end = twilight.get("astro_dark_end")

    if dark_start is None or dark_end is None:
        return {"visible": False, "hours": 0.0, "rise_time": None, "set_time": None}

    # Sample every 10 minutes through the dark window
    samples = int((dark_end - dark_start).total_seconds() / 600)
    if samples < 1:
        return {"visible": False, "hours": 0.0, "rise_time": None, "set_time": None}

    visible_times = []
    for i in range(samples + 1):
        t = dark_start + timedelta(minutes=i * 10)
        alt = compute_altitude(target, obs, t)
        if alt >= min_altitude:
            visible_times.append(t)

    if not visible_times:
        return {"visible": False, "hours": 0.0, "rise_time": None, "set_time": None}

    return {
        "visible": True,
        "hours": round(len(visible_times) * 10 / 60, 1),
        "rise_time": visible_times[0],
        "set_time": visible_times[-1],
        "transit_time": compute_transit_time(target, obs, date_utc),
    }


def rank_targets_for_night(
    targets: list[CelestialTarget],
    obs: ObservatoryLocation,
    date_utc: datetime,
    min_altitude: float = 30.0,
) -> list[dict]:
    """Rank targets by visibility hours and assign tiers."""
    results = []
    for target in targets:
        vis = compute_visibility_window(target, obs, date_utc, min_altitude)
        if not vis["visible"]:
            continue
        results.append({
            "target": target,
            "visibility": vis,
        })

    # Sort by hours visible (descending)
    results.sort(key=lambda r: r["visibility"]["hours"], reverse=True)

    # Assign tiers: top 20% = best, next 30% = better, rest = good
    n = len(results)
    for i, r in enumerate(results):
        pct = i / max(n, 1)
        if pct < 0.2:
            r["tier"] = TargetTier.BEST
        elif pct < 0.5:
            r["tier"] = TargetTier.BETTER
        else:
            r["tier"] = TargetTier.GOOD

    return results


# ---------------------------------------------------------------------------
# Seasonal target catalog — curated DSO list for the year
# ---------------------------------------------------------------------------

SEASONAL_TARGETS: list[dict] = [
    # Winter (Dec-Feb) — Orion, Taurus, Gemini region
    {"name": "Orion Nebula", "catalog_id": "M 42", "ra": 5.588, "dec": -5.39, "type": "emission nebula", "mag": 4.0, "size": 85, "months": [12, 1, 2], "hours": 8},
    {"name": "Horsehead Nebula", "catalog_id": "B 33", "ra": 5.681, "dec": -2.46, "type": "dark nebula", "mag": None, "size": 8, "months": [12, 1, 2], "hours": 20},
    {"name": "Rosette Nebula", "catalog_id": "NGC 2237", "ra": 6.535, "dec": 4.95, "type": "emission nebula", "mag": None, "size": 80, "months": [12, 1, 2, 3], "hours": 15},
    {"name": "Crab Nebula", "catalog_id": "M 1", "ra": 5.575, "dec": 22.01, "type": "supernova remnant", "mag": 8.4, "size": 7, "months": [11, 12, 1, 2], "hours": 10},
    {"name": "Monkey Head Nebula", "catalog_id": "NGC 2174", "ra": 6.164, "dec": 20.49, "type": "emission nebula", "mag": None, "size": 40, "months": [12, 1, 2, 3], "hours": 12},

    # Spring (Mar-May) — Leo, Virgo, Coma Berenices
    {"name": "Leo Triplet", "catalog_id": "M 65/M 66/NGC 3628", "ra": 11.315, "dec": 13.09, "type": "galaxy group", "mag": 9.3, "size": 30, "months": [3, 4, 5], "hours": 15},
    {"name": "Markarian's Chain", "catalog_id": "Virgo Cluster", "ra": 12.45, "dec": 13.0, "type": "galaxy chain", "mag": 9.0, "size": 60, "months": [3, 4, 5, 6], "hours": 20},
    {"name": "Whirlpool Galaxy", "catalog_id": "M 51", "ra": 13.498, "dec": 47.20, "type": "galaxy", "mag": 8.4, "size": 11, "months": [3, 4, 5, 6], "hours": 15},
    {"name": "Sombrero Galaxy", "catalog_id": "M 104", "ra": 12.666, "dec": -11.62, "type": "galaxy", "mag": 8.0, "size": 9, "months": [3, 4, 5], "hours": 12},
    {"name": "M 101 Pinwheel Galaxy", "catalog_id": "M 101", "ra": 14.054, "dec": 54.35, "type": "galaxy", "mag": 7.9, "size": 29, "months": [3, 4, 5, 6], "hours": 15},
    {"name": "Antennae Galaxies", "catalog_id": "NGC 4038/4039", "ra": 12.03, "dec": -18.87, "type": "galaxy pair", "mag": 10.5, "size": 5, "months": [3, 4, 5], "hours": 20},
    {"name": "Owl Nebula", "catalog_id": "M 97", "ra": 11.248, "dec": 55.02, "type": "planetary nebula", "mag": 9.9, "size": 3, "months": [3, 4, 5], "hours": 10},

    # Summer (Jun-Aug) — Sagittarius, Cygnus, Scorpius
    {"name": "Eagle Nebula (Pillars of Creation)", "catalog_id": "M 16", "ra": 18.313, "dec": -13.79, "type": "emission nebula", "mag": 6.0, "size": 35, "months": [6, 7, 8], "hours": 15},
    {"name": "Lagoon Nebula", "catalog_id": "M 8", "ra": 18.063, "dec": -24.38, "type": "emission nebula", "mag": 6.0, "size": 45, "months": [6, 7, 8], "hours": 10},
    {"name": "Trifid Nebula", "catalog_id": "M 20", "ra": 18.038, "dec": -23.03, "type": "emission nebula", "mag": 6.3, "size": 29, "months": [6, 7, 8], "hours": 12},
    {"name": "Swan Nebula", "catalog_id": "M 17", "ra": 18.341, "dec": -16.18, "type": "emission nebula", "mag": 6.0, "size": 46, "months": [6, 7, 8], "hours": 12},
    {"name": "North America Nebula", "catalog_id": "NGC 7000", "ra": 20.981, "dec": 44.53, "type": "emission nebula", "mag": None, "size": 120, "months": [6, 7, 8, 9], "hours": 20},
    {"name": "Veil Nebula (Western)", "catalog_id": "NGC 6960", "ra": 20.76, "dec": 30.72, "type": "supernova remnant", "mag": None, "size": 70, "months": [6, 7, 8, 9], "hours": 20},
    {"name": "Veil Nebula (Eastern)", "catalog_id": "NGC 6992", "ra": 20.94, "dec": 31.72, "type": "supernova remnant", "mag": None, "size": 60, "months": [6, 7, 8, 9], "hours": 20},
    {"name": "Crescent Nebula", "catalog_id": "NGC 6888", "ra": 20.2, "dec": 38.35, "type": "emission nebula", "mag": None, "size": 25, "months": [6, 7, 8, 9], "hours": 25},
    {"name": "Ring Nebula", "catalog_id": "M 57", "ra": 18.893, "dec": 33.03, "type": "planetary nebula", "mag": 8.8, "size": 2.5, "months": [6, 7, 8], "hours": 8},

    # Autumn (Sep-Nov) — Andromeda, Cassiopeia, Cepheus
    {"name": "Andromeda Galaxy", "catalog_id": "M 31", "ra": 0.712, "dec": 41.27, "type": "galaxy", "mag": 3.4, "size": 178, "months": [9, 10, 11, 12], "hours": 15},
    {"name": "Triangulum Galaxy", "catalog_id": "M 33", "ra": 1.564, "dec": 30.66, "type": "galaxy", "mag": 5.7, "size": 73, "months": [9, 10, 11, 12], "hours": 20},
    {"name": "Heart Nebula", "catalog_id": "IC 1805", "ra": 2.555, "dec": 61.47, "type": "emission nebula", "mag": None, "size": 60, "months": [9, 10, 11, 12], "hours": 20},
    {"name": "Soul Nebula", "catalog_id": "IC 1848", "ra": 2.852, "dec": 60.43, "type": "emission nebula", "mag": None, "size": 60, "months": [9, 10, 11, 12], "hours": 20},
    {"name": "Pacman Nebula", "catalog_id": "NGC 281", "ra": 0.878, "dec": 56.63, "type": "emission nebula", "mag": None, "size": 35, "months": [9, 10, 11], "hours": 15},
    {"name": "Elephant Trunk Nebula", "catalog_id": "IC 1396", "ra": 21.647, "dec": 57.50, "type": "emission nebula", "mag": None, "size": 170, "months": [8, 9, 10, 11], "hours": 25},
    {"name": "Bubble Nebula", "catalog_id": "NGC 7635", "ra": 23.345, "dec": 61.20, "type": "emission nebula", "mag": None, "size": 15, "months": [9, 10, 11], "hours": 15},
    {"name": "Cave Nebula", "catalog_id": "Sh2-155", "ra": 22.945, "dec": 62.62, "type": "emission nebula", "mag": None, "size": 50, "months": [9, 10, 11], "hours": 20},
]


def get_seasonal_targets(month: int) -> list[CelestialTarget]:
    """Return curated targets appropriate for a given month."""
    results = []
    for entry in SEASONAL_TARGETS:
        if month in entry["months"]:
            results.append(CelestialTarget(
                name=entry["name"],
                catalog_id=entry["catalog_id"],
                ra_hours=entry["ra"],
                dec_degrees=entry["dec"],
                object_type=entry["type"],
                magnitude=entry.get("mag"),
                angular_size_arcmin=entry.get("size"),
                recommended_total_hours=entry.get("hours", 10),
            ))
    return results
