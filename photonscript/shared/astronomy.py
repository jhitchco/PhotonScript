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
    {"name": "Crescent Nebula", "catalog_id": "NGC 6888", "ra": 20.20181, "dec": 38.35500, "type": "emission nebula", "mag": None, "size": 25, "months": [6, 7, 8, 9], "hours": 25},  # J2000 20h12m06.5s +38d21'18" (was 20.2/38.35 ~= 1.3' W in RA)
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

    # --- Expanded catalog (OpenNGC via pyongc, precise J2000; auto-generated 2026-09-18). The curated entries above are kept verbatim for their intentional framing centers; entries below use catalog centers. ---
    {"name": 'Pleiades', "catalog_id": 'M 45', "ra": 3.79128, "dec": 24.10528, "type": 'open cluster', "mag": 1.2, "size": 150.0, "months": [1, 10, 11, 12], "hours": 6},
    {"name": 'Small Sgr Star Cloud', "catalog_id": 'M 24', "ra": 18.28226, "dec": -18.51456, "type": 'association', "mag": 4.5, "size": 120.0, "months": [5, 6, 7, 8], "hours": 12},
    {"name": 'Beehive', "catalog_id": 'M 44', "ra": 8.67283, "dec": 19.67206, "type": 'open cluster', "mag": 3.1, "size": 108.6, "months": [1, 2, 3, 12], "hours": 6},
    {"name": "Ptolemy's Cluster", "catalog_id": 'M 7', "ra": 17.89755, "dec": -34.79283, "type": 'open cluster', "mag": 3.3, "size": 22.2, "months": [5, 6, 7, 8], "hours": 6},
    {"name": 'Butterfly Cluster', "catalog_id": 'M 6', "ra": 17.67243, "dec": -32.25417, "type": 'open cluster', "mag": 4.2, "size": 15.6, "months": [5, 6, 7, 8], "hours": 6},
    {"name": 'Hercules Globular Cluster', "catalog_id": 'M 13', "ra": 16.6949, "dec": 36.46131, "type": 'globular cluster', "mag": 5.8, "size": 16.5, "months": [4, 5, 6, 7], "hours": 8},
    {"name": "Bode's Galaxy", "catalog_id": 'M 81', "ra": 9.92588, "dec": 69.06531, "type": 'galaxy', "mag": 6.9, "size": 21.6, "months": [1, 2, 3, 4], "hours": 15},
    {"name": "Amas de l'Ecu de Sobieski", "catalog_id": 'M 11', "ra": 18.85166, "dec": -6.27003, "type": 'open cluster', "mag": 5.8, "size": 9.0, "months": [5, 6, 7, 8], "hours": 6},
    {"name": 'Southern Pinwheel Galaxy', "catalog_id": 'M 83', "ra": 13.61693, "dec": -29.86542, "type": 'galaxy', "mag": 7.2, "size": 13.6, "months": [3, 4, 5, 6], "hours": 15},
    {"name": "Mairan's Nebula", "catalog_id": 'M 43', "ra": 5.59205, "dec": -5.26747, "type": 'emission nebula', "mag": 9.0, "size": 20.0, "months": [1, 2, 11, 12], "hours": 20},
    {"name": 'Dumbbell Nebula', "catalog_id": 'M 27', "ra": 19.99344, "dec": 22.72103, "type": 'planetary nebula', "mag": 7.4, "size": 6.7, "months": [6, 7, 8, 9], "hours": 10},
    {"name": 'Cigar Galaxy', "catalog_id": 'M 82', "ra": 9.93131, "dec": 69.67939, "type": 'galaxy', "mag": 8.3, "size": 11.0, "months": [1, 2, 3, 4], "hours": 15},
    {"name": 'Sunflower Galaxy', "catalog_id": 'M 63', "ra": 13.2637, "dec": 42.02928, "type": 'galaxy', "mag": 8.6, "size": 11.8, "months": [3, 4, 5, 6], "hours": 15},
    {"name": 'Black Eye Galaxy', "catalog_id": 'M 64', "ra": 12.94546, "dec": 21.68297, "type": 'galaxy', "mag": 8.5, "size": 10.5, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Virgo Galaxy', "catalog_id": 'M 87', "ra": 12.51373, "dec": 12.39111, "type": 'galaxy', "mag": 9.0, "size": 7.1, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Coma Pinwheel', "catalog_id": 'M 99', "ra": 12.31378, "dec": 14.4165, "type": 'galaxy', "mag": 9.8, "size": 5.0, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Barbell Nebula', "catalog_id": 'M 76', "ra": 1.70547, "dec": 51.57547, "type": 'planetary nebula', "mag": 10.1, "size": 1.1, "months": [9, 10, 11, 12], "hours": 10},
    {"name": 'Carina Nebula', "catalog_id": 'NGC 3372', "ra": 10.75237, "dec": -59.86669, "type": 'emission nebula', "mag": 3.0, "size": 120.0, "months": [1, 2, 3, 4], "hours": 20},
    {"name": 'omi Vel Cluster', "catalog_id": 'IC 2391', "ra": 8.67552, "dec": -53.03547, "type": 'open cluster', "mag": 2.5, "size": 29.1, "months": [1, 2, 3, 12], "hours": 6},
    {"name": '47 Tuc Cluster', "catalog_id": 'NGC 104', "ra": 0.40149, "dec": -72.08144, "type": 'globular cluster', "mag": 4.1, "size": 31.8, "months": [8, 9, 10, 11], "hours": 8},
    {"name": 'Wishing Well Cluster', "catalog_id": 'NGC 3532', "ra": 11.09662, "dec": -58.7705, "type": 'open cluster', "mag": 3.0, "size": 12.0, "months": [2, 3, 4, 5], "hours": 6},
    {"name": 'Omega Centauri', "catalog_id": 'NGC 5139', "ra": 13.44608, "dec": -47.47686, "type": 'globular cluster', "mag": 5.3, "size": 27.0, "months": [3, 4, 5, 6], "hours": 8},
    {"name": 'lam Cen Nebula', "catalog_id": 'IC 2944', "ra": 11.59637, "dec": -63.01983, "type": 'cluster + nebula', "mag": 4.5, "size": 7.2, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Flaming Star Nebula', "catalog_id": 'IC 405', "ra": 5.27486, "dec": 34.35617, "type": 'nebula', "mag": 10.0, "size": 50.0, "months": [1, 2, 11, 12], "hours": 20},
    {"name": 'Centaurus A', "catalog_id": 'NGC 5128', "ra": 13.42434, "dec": -43.01911, "type": 'galaxy', "mag": 7.2, "size": 25.9, "months": [3, 4, 5, 6], "hours": 15},
    {"name": 'S Nor Cluster', "catalog_id": 'NGC 6087', "ra": 16.31405, "dec": -57.93458, "type": 'open cluster', "mag": 5.4, "size": 10.2, "months": [4, 5, 6, 7], "hours": 6},
    {"name": 'Pearl Cluster', "catalog_id": 'NGC 3766', "ra": 11.604, "dec": -61.60517, "type": 'open cluster', "mag": 5.3, "size": 6.9, "months": [2, 3, 4, 5], "hours": 6},
    {"name": '30 Dor Cluster', "catalog_id": 'NGC 2070', "ra": 5.6451, "dec": -69.10089, "type": 'emission nebula', "mag": 7.2, "size": 16.0, "months": [1, 2, 11, 12], "hours": 20},
    {"name": 'Helix Nebula', "catalog_id": 'NGC 7293', "ra": 22.49405, "dec": -20.83733, "type": 'planetary nebula', "mag": 7.3, "size": 16.3, "months": [7, 8, 9, 10], "hours": 10},
    {"name": 'Owl Cluster', "catalog_id": 'NGC 457', "ra": 1.32574, "dec": 58.29069, "type": 'open cluster', "mag": 6.4, "size": 7.8, "months": [9, 10, 11, 12], "hours": 6},
    {"name": 'Cocoon Nebula', "catalog_id": 'IC 5146', "ra": 21.89132, "dec": 47.26692, "type": 'cluster + nebula', "mag": 7.2, "size": 10.0, "months": [7, 8, 9, 10], "hours": 15},
    {"name": 'Iris Nebula', "catalog_id": 'NGC 7023', "ra": 21.02656, "dec": 68.16956, "type": 'nebula', "mag": 7.2, "size": 10.0, "months": [7, 8, 9, 10], "hours": 20},
    {"name": "Caroline's Cluster", "catalog_id": 'NGC 2360', "ra": 7.29531, "dec": -15.64131, "type": 'open cluster', "mag": 7.2, "size": 9.0, "months": [1, 2, 3, 12], "hours": 6},
    {"name": 'Coalsack Cluster', "catalog_id": 'NGC 4609', "ra": 12.70467, "dec": -62.99575, "type": 'open cluster', "mag": 6.9, "size": 5.4, "months": [2, 3, 4, 5], "hours": 6},
    {"name": 'Whale Galaxy', "catalog_id": 'NGC 4631', "ra": 12.70223, "dec": 32.5415, "type": 'galaxy', "mag": 9.2, "size": 14.4, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Fireworks Galaxy', "catalog_id": 'NGC 6946', "ra": 20.5812, "dec": 60.15392, "type": 'galaxy', "mag": 9.1, "size": 11.4, "months": [6, 7, 8, 9], "hours": 15},
    {"name": "Jupiter's Ghost Nebula", "catalog_id": 'NGC 3242', "ra": 10.4128, "dec": -18.64222, "type": 'planetary nebula', "mag": 7.7, "size": 0.4, "months": [1, 2, 3, 4], "hours": 10},
    {"name": 'Sculptor Filament', "catalog_id": 'NGC 253', "ra": 0.79253, "dec": -25.28822, "type": 'galaxy', "mag": 11.1, "size": 26.8, "months": [8, 9, 10, 11], "hours": 15},
    {"name": "Barnard's Galaxy", "catalog_id": 'NGC 6822', "ra": 19.74937, "dec": -14.80344, "type": 'galaxy', "mag": 10.1, "size": 17.4, "months": [6, 7, 8, 9], "hours": 15},
    {"name": 'Saturn Nebula', "catalog_id": 'NGC 7009', "ra": 21.06966, "dec": -11.36325, "type": 'planetary nebula', "mag": 8.0, "size": 0.7, "months": [7, 8, 9, 10], "hours": 10},
    {"name": 'tet Car Cluster', "catalog_id": 'IC 2602', "ra": 10.71596, "dec": -64.39419, "type": 'open cluster', "mag": None, "size": 48.0, "months": [1, 2, 3, 4], "hours": 6},
    {"name": 'Spindle Galaxy', "catalog_id": 'NGC 3115', "ra": 10.08722, "dec": -7.71858, "type": 'galaxy', "mag": 9.1, "size": 7.1, "months": [1, 2, 3, 4], "hours": 15},
    {"name": "Copeland's Blue Snowball", "catalog_id": 'NGC 7662', "ra": 23.43164, "dec": 42.53494, "type": 'planetary nebula', "mag": 8.3, "size": 0.3, "months": [8, 9, 10, 11], "hours": 10},
    {"name": 'Needle Galaxy', "catalog_id": 'NGC 4565', "ra": 12.60577, "dec": 25.98767, "type": 'galaxy', "mag": 10.9, "size": 16.8, "months": [2, 3, 4, 5], "hours": 15},
    {"name": "Cat's Eye Nebula", "catalog_id": 'NGC 6543', "ra": 17.97594, "dec": 66.63319, "type": 'planetary nebula', "mag": 9.0, "size": 0.9, "months": [5, 6, 7, 8], "hours": 10},
    {"name": 'Eight-Burst Nebula', "catalog_id": 'NGC 3132', "ra": 10.11715, "dec": -40.43658, "type": 'planetary nebula', "mag": 9.2, "size": 0.5, "months": [1, 2, 3, 4], "hours": 10},
    {"name": 'Blinking Planetary', "catalog_id": 'NGC 6826', "ra": 19.7467, "dec": 50.52503, "type": 'planetary nebula', "mag": 9.4, "size": 0.4, "months": [6, 7, 8, 9], "hours": 10},
    {"name": 'Eskimo Nebula', "catalog_id": 'NGC 2392', "ra": 7.48632, "dec": 20.91183, "type": 'planetary nebula', "mag": 9.6, "size": 0.9, "months": [1, 2, 3, 12], "hours": 10},
    {"name": 'Bug Nebula', "catalog_id": 'NGC 6302', "ra": 17.22906, "dec": -37.10314, "type": 'planetary nebula', "mag": 9.6, "size": 0.7, "months": [5, 6, 7, 8], "hours": 10},
    {"name": "Hubble's Nebula", "catalog_id": 'NGC 2261', "ra": 6.65264, "dec": 8.74433, "type": 'reflection nebula', "mag": 11.8, "size": 2.0, "months": [1, 2, 11, 12], "hours": 15},
    {"name": 'Bow-Tie nebula', "catalog_id": 'NGC 40', "ra": 0.21695, "dec": 72.52194, "type": 'planetary nebula', "mag": 11.9, "size": 0.8, "months": [8, 9, 10, 11], "hours": 10},
    {"name": 'Perseus A', "catalog_id": 'NGC 1275', "ra": 3.33004, "dec": 41.51169, "type": 'galaxy', "mag": 12.2, "size": 2.2, "months": [1, 10, 11, 12], "hours": 15},
    {"name": "Herschel's Jewel Box", "catalog_id": 'NGC 4755', "ra": 12.89363, "dec": -60.35631, "type": 'open cluster', "mag": None, "size": 7.8, "months": [2, 3, 4, 5], "hours": 6},
    {"name": 'Large Magellanic Cloud', "catalog_id": 'ESO056-115', "ra": 5.39292, "dec": -69.75611, "type": 'galaxy', "mag": 0.3, "size": 646.0, "months": [1, 2, 11, 12], "hours": 15},
    {"name": 'Small Magellanic Cloud', "catalog_id": 'NGC 292', "ra": 0.87911, "dec": -72.82861, "type": 'galaxy', "mag": 2.3, "size": 299.9, "months": [8, 9, 10, 11], "hours": 15},
    {"name": 'California Nebula', "catalog_id": 'NGC 1499', "ra": 4.05401, "dec": 36.36747, "type": 'nebula', "mag": 5.0, "size": 160.0, "months": [1, 10, 11, 12], "hours": 20},
    {"name": 'Hyades', "catalog_id": 'C041', "ra": 4.44833, "dec": 15.86667, "type": 'open cluster', "mag": None, "size": 329.0, "months": [1, 10, 11, 12], "hours": 6},
    {"name": 'Coma Star Cluster', "catalog_id": 'Mel111', "ra": 12.41833, "dec": 26.1, "type": 'open cluster', "mag": None, "size": 253.5, "months": [2, 3, 4, 5], "hours": 6},
    {"name": 'the Witch Head Nebula', "catalog_id": 'NGC 1909', "ra": 5.08207, "dec": -7.26564, "type": 'reflection nebula', "mag": None, "size": 180.0, "months": [1, 2, 11, 12], "hours": 15},
    {"name": "Brocchi's Cluster", "catalog_id": 'Cl399', "ra": 19.42333, "dec": 20.18333, "type": 'association', "mag": 3.6, "size": 70.0, "months": [6, 7, 8, 9], "hours": 12},
    {"name": 'rho Oph Nebula', "catalog_id": 'IC 4604', "ra": 16.42532, "dec": -23.43658, "type": 'nebula', "mag": 5.1, "size": 60.0, "months": [4, 5, 6, 7], "hours": 20},
    {"name": 'Flame Nebula', "catalog_id": 'IC 434', "ra": 5.68358, "dec": -2.45378, "type": 'emission nebula', "mag": 11.0, "size": 90.0, "months": [1, 2, 11, 12], "hours": 20},
    {"name": 'Pelican Nebula', "catalog_id": 'IC 5070', "ra": 20.8502, "dec": 44.4015, "type": 'emission nebula', "mag": 8.0, "size": 60.0, "months": [6, 7, 8, 9], "hours": 20},
    {"name": 'Lower Sword', "catalog_id": 'NGC 1980', "ra": 5.59055, "dec": -5.90989, "type": 'cluster + nebula', "mag": 2.5, "size": 9.3, "months": [1, 2, 11, 12], "hours": 15},
    {"name": 'h Persei Cluster', "catalog_id": 'NGC 869', "ra": 2.31627, "dec": 57.11725, "type": 'open cluster', "mag": 3.7, "size": 14.4, "months": [9, 10, 11, 12], "hours": 6},
    {"name": 'Christmas Tree Cluster', "catalog_id": 'NGC 2264', "ra": 6.68285, "dec": 9.89547, "type": 'cluster + nebula', "mag": 3.9, "size": 11.4, "months": [1, 2, 11, 12], "hours": 15},
    {"name": 'chi Persei Cluster', "catalog_id": 'NGC 884', "ra": 2.37558, "dec": 57.14411, "type": 'open cluster', "mag": 3.8, "size": 10.5, "months": [9, 10, 11, 12], "hours": 6},
    {"name": 'Upper Sword', "catalog_id": 'NGC 1981', "ra": 5.586, "dec": -4.42506, "type": 'cluster + nebula', "mag": 4.2, "size": 9.0, "months": [1, 2, 11, 12], "hours": 15},
    {"name": 'Great Bird Cluster', "catalog_id": 'NGC 2301', "ra": 6.86258, "dec": 0.45919, "type": 'open cluster', "mag": 6.0, "size": 10.2, "months": [1, 2, 11, 12], "hours": 6},
    {"name": 'Eagle Nebula', "catalog_id": 'IC 4703', "ra": 18.31562, "dec": -13.84539, "type": 'nebula', "mag": 6.0, "size": 5.0, "months": [5, 6, 7, 8], "hours": 20},
    {"name": 'Eastern Veil', "catalog_id": 'NGC 6995', "ra": 20.95299, "dec": 31.23517, "type": 'supernova remnant', "mag": 7.0, "size": 12.0, "months": [6, 7, 8, 9], "hours": 20},
    {"name": 'Gem A', "catalog_id": 'IC 443', "ra": 6.27706, "dec": 22.53167, "type": 'supernova remnant', "mag": 12.0, "size": 50.0, "months": [1, 2, 11, 12], "hours": 20},
    {"name": 'Fornax Dwarf Spheroidal', "catalog_id": 'ESO356-004', "ra": 2.66648, "dec": -34.44919, "type": 'galaxy', "mag": 7.4, "size": 12.9, "months": [9, 10, 11, 12], "hours": 15},
    {"name": 'Foxhead Cluster', "catalog_id": 'NGC 6819', "ra": 19.68836, "dec": 40.18675, "type": 'open cluster', "mag": 7.3, "size": 6.9, "months": [6, 7, 8, 9], "hours": 6},
    {"name": 'Maia Nebula', "catalog_id": 'NGC 1432', "ra": 3.76378, "dec": 24.36786, "type": 'emission nebula', "mag": None, "size": 60.0, "months": [1, 10, 11, 12], "hours": 20},
    {"name": 'Sextans Dwarf Spheroidal', "catalog_id": 'PGC088608', "ra": 10.21747, "dec": -1.61472, "type": 'galaxy', "mag": 10.4, "size": 30.2, "months": [1, 2, 3, 4], "hours": 15},
    {"name": 'Sculptor Dwarf Elliptical', "catalog_id": 'ESO351-030', "ra": 1.0026, "dec": -33.70903, "type": 'galaxy', "mag": 8.6, "size": 15.3, "months": [9, 10, 11, 12], "hours": 15},
    {"name": 'Fornax A', "catalog_id": 'NGC 1316', "ra": 3.37826, "dec": -37.20822, "type": 'galaxy', "mag": 8.5, "size": 13.5, "months": [1, 10, 11, 12], "hours": 15},
    {"name": 'Double Cluster', "catalog_id": 'C014', "ra": 2.345, "dec": 57.1375, "type": 'association', "mag": None, "size": 50.0, "months": [9, 10, 11, 12], "hours": 12},
    {"name": 'Blue Planetary', "catalog_id": 'NGC 3918', "ra": 11.83832, "dec": -57.18233, "type": 'planetary nebula', "mag": 8.1, "size": 0.3, "months": [2, 3, 4, 5], "hours": 10},
    {"name": "Coddington's Nebula", "catalog_id": 'IC 2574', "ra": 10.47319, "dec": 68.41214, "type": 'galaxy', "mag": 10.5, "size": 12.9, "months": [1, 2, 3, 4], "hours": 15},
    {"name": 'Leo I', "catalog_id": 'UGC05470', "ra": 10.14114, "dec": 12.30639, "type": 'galaxy', "mag": 10.4, "size": 11.8, "months": [1, 2, 3, 4], "hours": 15},
    {"name": 'Little Gem Nebula', "catalog_id": 'NGC 6818', "ra": 19.7327, "dec": -14.15317, "type": 'planetary nebula', "mag": 9.3, "size": 0.8, "months": [6, 7, 8, 9], "hours": 10},
    {"name": 'Wolf-Lundmark-Melotte', "catalog_id": 'PGC000143', "ra": 0.03282, "dec": -15.46092, "type": 'galaxy', "mag": 10.8, "size": 10.5, "months": [8, 9, 10, 11], "hours": 15},
    {"name": 'Circinus Galaxy', "catalog_id": 'ESO097-013', "ra": 14.21943, "dec": -65.33922, "type": 'galaxy', "mag": 10.6, "size": 8.7, "months": [3, 4, 5, 6], "hours": 15},
    {"name": "Hind's Nebula", "catalog_id": 'NGC 1555', "ra": 4.36651, "dec": 19.53517, "type": 'reflection nebula', "mag": 10.0, "size": 1.8, "months": [1, 10, 11, 12], "hours": 15},
    {"name": 'Eyes', "catalog_id": 'NGC 4438', "ra": 12.46266, "dec": 13.00883, "type": 'galaxy', "mag": 10.9, "size": 9.2, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Merope Nebula', "catalog_id": 'NGC 1435', "ra": 3.76947, "dec": 23.76497, "type": 'nebula', "mag": None, "size": 30.0, "months": [1, 10, 11, 12], "hours": 20},
    {"name": 'Pencil Nebula', "catalog_id": 'NGC 2736', "ra": 9.00471, "dec": -45.94806, "type": 'emission nebula', "mag": None, "size": 30.0, "months": [1, 2, 3, 4], "hours": 20},
    {"name": 'Butterfly Galaxies', "catalog_id": 'NGC 4568', "ra": 12.60952, "dec": 11.23889, "type": 'galaxy', "mag": 10.8, "size": 4.3, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Fourcade-Figueroa', "catalog_id": 'ESO270-017', "ra": 13.57981, "dec": -45.5475, "type": 'galaxy', "mag": 11.7, "size": 11.5, "months": [3, 4, 5, 6], "hours": 15},
    {"name": 'Umbrella Galaxy', "catalog_id": 'NGC 4651', "ra": 12.72851, "dec": 16.39339, "type": 'galaxy', "mag": 10.8, "size": 3.9, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Fornax B', "catalog_id": 'NGC 1317', "ra": 3.37897, "dec": -37.10369, "type": 'galaxy', "mag": 10.9, "size": 3.1, "months": [1, 10, 11, 12], "hours": 15},
    {"name": 'Eyes', "catalog_id": 'NGC 4435', "ra": 12.46125, "dec": 13.07894, "type": 'galaxy', "mag": 11.0, "size": 3.0, "months": [2, 3, 4, 5], "hours": 15},
    {"name": "Barnard's Merope Nebula", "catalog_id": 'IC 349', "ra": 3.77225, "dec": 23.93981, "type": 'reflection nebula', "mag": None, "size": 25.7, "months": [1, 10, 11, 12], "hours": 15},
    {"name": 'Helix Galaxy', "catalog_id": 'NGC 2685', "ra": 8.92631, "dec": 58.73439, "type": 'galaxy', "mag": 11.3, "size": 4.3, "months": [1, 2, 3, 12], "hours": 15},
    {"name": 'Sextans B', "catalog_id": 'UGC05373', "ra": 10.00003, "dec": 5.33222, "type": 'galaxy', "mag": 11.5, "size": 4.9, "months": [1, 2, 3, 4], "hours": 15},
    {"name": 'Butterfly Galaxies', "catalog_id": 'NGC 4567', "ra": 12.60909, "dec": 11.258, "type": 'galaxy', "mag": 11.3, "size": 2.7, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Blue Flash Nebula', "catalog_id": 'NGC 6905', "ra": 20.37305, "dec": 20.10453, "type": 'planetary nebula', "mag": 11.1, "size": 0.7, "months": [6, 7, 8, 9], "hours": 10},
    {"name": 'Sextans A', "catalog_id": 'PGC029653', "ra": 10.18356, "dec": -4.69278, "type": 'galaxy', "mag": 11.8, "size": 5.2, "months": [1, 2, 3, 4], "hours": 15},
    {"name": 'Little Gem', "catalog_id": 'NGC 6445', "ra": 17.82085, "dec": -20.0095, "type": 'planetary nebula', "mag": 11.2, "size": 0.6, "months": [5, 6, 7, 8], "hours": 10},
    {"name": 'Little Ghost Nebula', "catalog_id": 'NGC 6369', "ra": 17.48903, "dec": -23.75944, "type": 'planetary nebula', "mag": 11.4, "size": 0.6, "months": [5, 6, 7, 8], "hours": 10},
    {"name": 'Bear Claw Nebula', "catalog_id": 'NGC 2537', "ra": 8.22073, "dec": 45.98981, "type": 'galaxy', "mag": 11.7, "size": 2.1, "months": [1, 2, 3, 12], "hours": 15},
    {"name": 'Box Nebula', "catalog_id": 'NGC 6309', "ra": 17.23453, "dec": -12.91056, "type": 'planetary nebula', "mag": 11.5, "size": 0.3, "months": [5, 6, 7, 8], "hours": 10},
    {"name": 'Phantom Streak Nebula', "catalog_id": 'NGC 6741', "ra": 19.04361, "dec": 0.44939, "type": 'planetary nebula', "mag": 11.5, "size": 0.1, "months": [6, 7, 8, 9], "hours": 10},
    {"name": 'Rim Nebula', "catalog_id": 'NGC 6188', "ra": 16.66829, "dec": -48.66228, "type": 'nebula', "mag": None, "size": 20.0, "months": [4, 5, 6, 7], "hours": 20},
    {"name": 'Red Spider Nebula', "catalog_id": 'NGC 6537', "ra": 18.08697, "dec": -19.84297, "type": 'planetary nebula', "mag": 11.6, "size": 0.2, "months": [5, 6, 7, 8], "hours": 10},
    {"name": 'Miniature Spiral', "catalog_id": 'NGC 3928', "ra": 11.86323, "dec": 48.68314, "type": 'galaxy', "mag": 12.5, "size": 1.4, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Medusa Galaxy Merger', "catalog_id": 'NGC 4194', "ra": 12.23596, "dec": 54.52683, "type": 'galaxy', "mag": 12.8, "size": 1.6, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'the Running Man Nebula', "catalog_id": 'NGC 1977', "ra": 5.58772, "dec": -4.84433, "type": 'cluster + nebula', "mag": None, "size": 10.2, "months": [1, 2, 11, 12], "hours": 15},
    {"name": 'omi Per Cloud', "catalog_id": 'IC 348', "ra": 3.74283, "dec": 32.16283, "type": 'cluster + nebula', "mag": None, "size": 10.0, "months": [1, 10, 11, 12], "hours": 15},
    {"name": 'Rosette B', "catalog_id": 'NGC 2246', "ra": 6.54272, "dec": 5.12828, "type": 'nebula', "mag": None, "size": 10.0, "months": [1, 2, 11, 12], "hours": 20},
    {"name": 'Polarissima Australis', "catalog_id": 'NGC 2573', "ra": 1.6937, "dec": -89.33453, "type": 'galaxy', "mag": 13.5, "size": 1.9, "months": [9, 10, 11, 12], "hours": 15},
    {"name": 'Toby Jug Nebula', "catalog_id": 'IC 2220', "ra": 7.94749, "dec": -59.12578, "type": 'reflection nebula', "mag": None, "size": 5.0, "months": [1, 2, 3, 12], "hours": 15},
    {"name": 'Fornax Dwarf Cluster 3', "catalog_id": 'NGC 1049', "ra": 2.66337, "dec": -34.25825, "type": 'globular cluster', "mag": 13.6, "size": 1.2, "months": [9, 10, 11, 12], "hours": 8},
    {"name": "Stephan's Quintet", "catalog_id": 'HCG092', "ra": 22.59972, "dec": 33.95833, "type": 'galaxy group', "mag": None, "size": 4.4, "months": [7, 8, 9, 10], "hours": 18},
    {"name": 'the War and Peace Nebula', "catalog_id": 'NGC 6357', "ra": 17.4121, "dec": -34.20133, "type": 'cluster + nebula', "mag": None, "size": 3.9, "months": [5, 6, 7, 8], "hours": 15},
    {"name": 'Mice Galaxy', "catalog_id": 'NGC 4676', "ra": 12.76964, "dec": 30.72722, "type": 'galaxy pair', "mag": None, "size": 3.0, "months": [2, 3, 4, 5], "hours": 18},
    {"name": "Seyfert's Sextet", "catalog_id": 'HCG079', "ra": 15.98664, "dec": 20.75861, "type": 'galaxy group', "mag": None, "size": 2.8, "months": [4, 5, 6, 7], "hours": 18},
    {"name": 'Cocoon Galaxy', "catalog_id": 'NGC 4990', "ra": 13.1548, "dec": -5.27281, "type": 'galaxy', "mag": 13.8, "size": 0.9, "months": [3, 4, 5, 6], "hours": 15},
    {"name": 'the Guitar', "catalog_id": 'NGC 3561', "ra": 11.187, "dec": 28.69647, "type": 'galaxy', "mag": 14.7, "size": 1.7, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Polarissima Borealis', "catalog_id": 'NGC 3172', "ra": 11.78722, "dec": 89.09306, "type": 'galaxy', "mag": 15.0, "size": 1.1, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Browning', "catalog_id": 'IC 2431', "ra": 9.07631, "dec": 14.59578, "type": 'galaxy', "mag": 14.0, "size": 0.6, "months": [1, 2, 3, 4], "hours": 15},
    {"name": 'M 4', "catalog_id": 'M 4', "ra": 16.39317, "dec": -26.52553, "type": 'globular cluster', "mag": 5.4, "size": 28.2, "months": [4, 5, 6, 7], "hours": 8},
    {"name": 'M 47', "catalog_id": 'M 47', "ra": 7.60973, "dec": -14.48261, "type": 'open cluster', "mag": 4.4, "size": 19.8, "months": [1, 2, 3, 12], "hours": 6},
    {"name": 'M 35', "catalog_id": 'M 35', "ra": 6.15141, "dec": 24.33864, "type": 'open cluster', "mag": 5.1, "size": 24.0, "months": [1, 2, 11, 12], "hours": 6},
    {"name": 'M 39', "catalog_id": 'M 39', "ra": 21.53009, "dec": 48.43817, "type": 'open cluster', "mag": 4.6, "size": 19.5, "months": [7, 8, 9, 10], "hours": 6},
    {"name": 'M 48', "catalog_id": 'M 48', "ra": 8.22866, "dec": -5.75044, "type": 'open cluster', "mag": 5.8, "size": 28.2, "months": [1, 2, 3, 12], "hours": 6},
    {"name": 'M 34', "catalog_id": 'M 34', "ra": 2.70206, "dec": 42.74614, "type": 'open cluster', "mag": 5.2, "size": 22.5, "months": [9, 10, 11, 12], "hours": 6},
    {"name": 'M 67', "catalog_id": 'M 67', "ra": 8.85559, "dec": 11.81194, "type": 'open cluster', "mag": 6.9, "size": 33.0, "months": [1, 2, 3, 12], "hours": 6},
    {"name": 'M 25', "catalog_id": 'M 25', "ra": 18.52966, "dec": -19.11494, "type": 'open cluster', "mag": 4.6, "size": 14.1, "months": [5, 6, 7, 8], "hours": 6},
    {"name": 'M 41', "catalog_id": 'M 41', "ra": 6.76665, "dec": -20.75422, "type": 'open cluster', "mag": 4.5, "size": 12.0, "months": [1, 2, 11, 12], "hours": 6},
    {"name": 'M 23', "catalog_id": 'M 23', "ra": 17.95133, "dec": -18.98533, "type": 'open cluster', "mag": 5.5, "size": 16.8, "months": [5, 6, 7, 8], "hours": 6},
    {"name": 'M 46', "catalog_id": 'M 46', "ra": 7.69634, "dec": -14.81, "type": 'open cluster', "mag": 6.1, "size": 21.0, "months": [1, 2, 3, 12], "hours": 6},
    {"name": 'M 10', "catalog_id": 'M 10', "ra": 16.9525, "dec": -4.09933, "type": 'globular cluster', "mag": 5.0, "size": 9.3, "months": [4, 5, 6, 7], "hours": 8},
    {"name": 'M 5', "catalog_id": 'M 5', "ra": 15.30938, "dec": 2.08269, "type": 'globular cluster', "mag": 6.0, "size": 15.0, "months": [4, 5, 6, 7], "hours": 8},
    {"name": 'M 50', "catalog_id": 'M 50', "ra": 7.04458, "dec": -8.36403, "type": 'open cluster', "mag": 5.9, "size": 14.1, "months": [1, 2, 3, 12], "hours": 6},
    {"name": 'M 37', "catalog_id": 'M 37', "ra": 5.87176, "dec": 32.553, "type": 'open cluster', "mag": 5.6, "size": 11.4, "months": [1, 2, 11, 12], "hours": 6},
    {"name": 'M 93', "catalog_id": 'M 93', "ra": 7.74145, "dec": -23.85308, "type": 'open cluster', "mag": 6.2, "size": 15.0, "months": [1, 2, 3, 12], "hours": 6},
    {"name": 'M 3', "catalog_id": 'M 3', "ra": 13.70312, "dec": 28.37544, "type": 'globular cluster', "mag": 6.4, "size": 16.2, "months": [3, 4, 5, 6], "hours": 8},
    {"name": 'M 14', "catalog_id": 'M 14', "ra": 17.62671, "dec": -3.24592, "type": 'globular cluster', "mag": 5.7, "size": 9.9, "months": [5, 6, 7, 8], "hours": 8},
    {"name": 'M 22', "catalog_id": 'M 22', "ra": 18.60672, "dec": -23.90342, "type": 'globular cluster', "mag": 6.2, "size": 12.6, "months": [5, 6, 7, 8], "hours": 8},
    {"name": 'M 19', "catalog_id": 'M 19', "ra": 17.0438, "dec": -26.26794, "type": 'globular cluster', "mag": 5.6, "size": 7.5, "months": [5, 6, 7, 8], "hours": 8},
    {"name": 'M 12', "catalog_id": 'M 12', "ra": 16.78737, "dec": -1.94783, "type": 'globular cluster', "mag": 6.1, "size": 11.1, "months": [4, 5, 6, 7], "hours": 8},
    {"name": 'M 92', "catalog_id": 'M 92', "ra": 17.28535, "dec": 43.13653, "type": 'globular cluster', "mag": 6.5, "size": 14.4, "months": [5, 6, 7, 8], "hours": 8},
    {"name": 'M 15', "catalog_id": 'M 15', "ra": 21.49955, "dec": 12.16683, "type": 'globular cluster', "mag": 6.3, "size": 11.1, "months": [7, 8, 9, 10], "hours": 8},
    {"name": 'M 55', "catalog_id": 'M 55', "ra": 19.6665, "dec": -30.96208, "type": 'globular cluster', "mag": 6.5, "size": 12.0, "months": [6, 7, 8, 9], "hours": 8},
    {"name": 'M 36', "catalog_id": 'M 36', "ra": 5.60493, "dec": 34.14075, "type": 'open cluster', "mag": 6.0, "size": 7.2, "months": [1, 2, 11, 12], "hours": 6},
    {"name": 'M 21', "catalog_id": 'M 21', "ra": 18.0704, "dec": -22.49006, "type": 'open cluster', "mag": 5.9, "size": 6.0, "months": [5, 6, 7, 8], "hours": 6},
    {"name": 'M 38', "catalog_id": 'M 38', "ra": 5.47847, "dec": 35.85492, "type": 'open cluster', "mag": 6.4, "size": 9.6, "months": [1, 2, 11, 12], "hours": 6},
    {"name": 'M 2', "catalog_id": 'M 2', "ra": 21.5575, "dec": 0.82331, "type": 'globular cluster', "mag": 6.2, "size": 8.4, "months": [7, 8, 9, 10], "hours": 8},
    {"name": 'M 71', "catalog_id": 'M 71', "ra": 19.89614, "dec": 18.77839, "type": 'globular cluster', "mag": 6.1, "size": 6.9, "months": [6, 7, 8, 9], "hours": 8},
    {"name": 'M 52', "catalog_id": 'M 52', "ra": 23.41344, "dec": 61.59317, "type": 'open cluster', "mag": 6.9, "size": 9.9, "months": [8, 9, 10, 11], "hours": 6},
    {"name": 'M 30', "catalog_id": 'M 30', "ra": 21.67278, "dec": -23.17908, "type": 'globular cluster', "mag": 7.1, "size": 9.0, "months": [7, 8, 9, 10], "hours": 8},
    {"name": 'M 110', "catalog_id": 'M 110', "ra": 0.6728, "dec": 41.68531, "type": 'galaxy', "mag": 8.2, "size": 16.2, "months": [8, 9, 10, 11], "hours": 15},
    {"name": 'M 18', "catalog_id": 'M 18', "ra": 18.33291, "dec": -17.10197, "type": 'open cluster', "mag": 6.9, "size": 6.0, "months": [5, 6, 7, 8], "hours": 6},
    {"name": 'M 29', "catalog_id": 'M 29', "ra": 20.39938, "dec": 38.50767, "type": 'open cluster', "mag": 6.6, "size": 3.6, "months": [6, 7, 8, 9], "hours": 6},
    {"name": 'M 28', "catalog_id": 'M 28', "ra": 18.40914, "dec": -24.86983, "type": 'globular cluster', "mag": 6.9, "size": 5.1, "months": [5, 6, 7, 8], "hours": 8},
    {"name": 'M 62', "catalog_id": 'M 62', "ra": 17.02017, "dec": -30.11236, "type": 'globular cluster', "mag": 7.4, "size": 7.8, "months": [5, 6, 7, 8], "hours": 8},
    {"name": 'M 80', "catalog_id": 'M 80', "ra": 16.28403, "dec": -22.97511, "type": 'globular cluster', "mag": 7.3, "size": 5.7, "months": [4, 5, 6, 7], "hours": 8},
    {"name": 'M 53', "catalog_id": 'M 53', "ra": 13.21534, "dec": 18.16911, "type": 'globular cluster', "mag": 7.8, "size": 9.0, "months": [3, 4, 5, 6], "hours": 8},
    {"name": 'M 103', "catalog_id": 'M 103', "ra": 1.55606, "dec": 60.658, "type": 'open cluster', "mag": 7.4, "size": 4.5, "months": [9, 10, 11, 12], "hours": 6},
    {"name": 'M 49', "catalog_id": 'M 49', "ra": 12.49632, "dec": 8.00047, "type": 'galaxy', "mag": 8.3, "size": 10.2, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'M 54', "catalog_id": 'M 54', "ra": 18.91757, "dec": -30.4785, "type": 'globular cluster', "mag": 7.7, "size": 5.1, "months": [5, 6, 7, 8], "hours": 8},
    {"name": 'M 68', "catalog_id": 'M 68', "ra": 12.65778, "dec": -26.74303, "type": 'globular cluster', "mag": 8.0, "size": 6.6, "months": [2, 3, 4, 5], "hours": 8},
    {"name": 'M 32', "catalog_id": 'M 32', "ra": 0.71162, "dec": 40.86528, "type": 'galaxy', "mag": 8.1, "size": 7.7, "months": [8, 9, 10, 11], "hours": 15},
    {"name": 'M 106', "catalog_id": 'M 106', "ra": 12.31597, "dec": 47.30397, "type": 'galaxy', "mag": 9.3, "size": 17.0, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'M 79', "catalog_id": 'M 79', "ra": 5.40294, "dec": -24.52422, "type": 'globular cluster', "mag": 8.2, "size": 7.2, "months": [1, 2, 11, 12], "hours": 8},
    {"name": 'M 94', "catalog_id": 'M 94', "ra": 12.84807, "dec": 41.12044, "type": 'galaxy', "mag": 8.2, "size": 7.7, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'M 86', "catalog_id": 'M 86', "ra": 12.43659, "dec": 12.94622, "type": 'galaxy', "mag": 8.9, "size": 11.5, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'M 78', "catalog_id": 'M 78', "ra": 5.77939, "dec": 0.07931, "type": 'reflection nebula', "mag": 8.0, "size": 4.5, "months": [1, 2, 11, 12], "hours": 15},
    {"name": 'M 9', "catalog_id": 'M 9', "ra": 17.31994, "dec": -18.51625, "type": 'globular cluster', "mag": 8.4, "size": 6.9, "months": [5, 6, 7, 8], "hours": 8},
    {"name": 'M 69', "catalog_id": 'M 69', "ra": 18.52312, "dec": -32.34797, "type": 'globular cluster', "mag": 8.3, "size": 5.7, "months": [5, 6, 7, 8], "hours": 8},
    {"name": 'M 56', "catalog_id": 'M 56', "ra": 19.27653, "dec": 30.1845, "type": 'globular cluster', "mag": 8.4, "size": 5.8, "months": [6, 7, 8, 9], "hours": 8},
    {"name": 'M 75', "catalog_id": 'M 75', "ra": 20.10134, "dec": -21.92222, "type": 'globular cluster', "mag": 8.3, "size": 3.6, "months": [6, 7, 8, 9], "hours": 8},
    {"name": 'M 107', "catalog_id": 'M 107', "ra": 16.5422, "dec": -13.05364, "type": 'globular cluster', "mag": 8.8, "size": 7.8, "months": [4, 5, 6, 7], "hours": 8},
    {"name": 'M 60', "catalog_id": 'M 60', "ra": 12.72777, "dec": 11.55269, "type": 'galaxy', "mag": 8.8, "size": 6.8, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'M 74', "catalog_id": 'M 74', "ra": 1.6116, "dec": 15.78367, "type": 'galaxy', "mag": 9.3, "size": 9.9, "months": [9, 10, 11, 12], "hours": 15},
    {"name": 'M 26', "catalog_id": 'M 26', "ra": 18.75518, "dec": -9.38361, "type": 'open cluster', "mag": 8.9, "size": 6.0, "months": [5, 6, 7, 8], "hours": 6},
    {"name": 'M 96', "catalog_id": 'M 96', "ra": 10.77937, "dec": 11.81994, "type": 'galaxy', "mag": 9.2, "size": 8.3, "months": [1, 2, 3, 4], "hours": 15},
    {"name": 'M 85', "catalog_id": 'M 85', "ra": 12.42336, "dec": 18.1915, "type": 'galaxy', "mag": 9.1, "size": 7.0, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'M 70', "catalog_id": 'M 70', "ra": 18.72018, "dec": -32.29189, "type": 'globular cluster', "mag": 9.1, "size": 6.6, "months": [5, 6, 7, 8], "hours": 8},
    {"name": 'M 72', "catalog_id": 'M 72', "ra": 20.89109, "dec": -12.53706, "type": 'globular cluster', "mag": 9.0, "size": 4.5, "months": [6, 7, 8, 9], "hours": 8},
    {"name": 'M 90', "catalog_id": 'M 90', "ra": 12.61383, "dec": 13.16294, "type": 'galaxy', "mag": 9.5, "size": 9.1, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'M 77', "catalog_id": 'M 77', "ra": 2.71131, "dec": 0.01328, "type": 'galaxy', "mag": 9.3, "size": 6.1, "months": [9, 10, 11, 12], "hours": 15},
    {"name": 'M 105', "catalog_id": 'M 105', "ra": 10.79711, "dec": 12.58161, "type": 'galaxy', "mag": 9.3, "size": 4.9, "months": [1, 2, 3, 4], "hours": 15},
    {"name": 'M 100', "catalog_id": 'M 100', "ra": 12.3819, "dec": 15.82181, "type": 'galaxy', "mag": 9.5, "size": 6.1, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'M 84', "catalog_id": 'M 84', "ra": 12.41771, "dec": 12.88697, "type": 'galaxy', "mag": 9.8, "size": 7.4, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'M 95', "catalog_id": 'M 95', "ra": 10.73269, "dec": 11.70381, "type": 'galaxy', "mag": 9.8, "size": 7.2, "months": [1, 2, 3, 4], "hours": 15},
    {"name": 'M 109', "catalog_id": 'M 109', "ra": 11.95999, "dec": 53.37453, "type": 'galaxy', "mag": 9.9, "size": 8.1, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'M 59', "catalog_id": 'M 59', "ra": 12.70062, "dec": 11.64703, "type": 'galaxy', "mag": 9.6, "size": 4.5, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'M 89', "catalog_id": 'M 89', "ra": 12.59439, "dec": 12.55633, "type": 'galaxy', "mag": 10.1, "size": 8.1, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'M 88', "catalog_id": 'M 88', "ra": 12.5331, "dec": 14.42039, "type": 'galaxy', "mag": 10.3, "size": 8.7, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'M 61', "catalog_id": 'M 61', "ra": 12.36525, "dec": 4.47364, "type": 'galaxy', "mag": 10.2, "size": 6.9, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'M 98', "catalog_id": 'M 98', "ra": 12.23008, "dec": 14.90033, "type": 'galaxy', "mag": 10.8, "size": 11.0, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'M 108', "catalog_id": 'M 108', "ra": 11.19194, "dec": 55.67411, "type": 'galaxy', "mag": 10.1, "size": 4.0, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'M 58', "catalog_id": 'M 58', "ra": 12.62876, "dec": 11.81819, "type": 'galaxy', "mag": 10.3, "size": 5.0, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'M 91', "catalog_id": 'M 91', "ra": 12.59068, "dec": 14.49633, "type": 'galaxy', "mag": 11.0, "size": 5.5, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'NGC 2516', "catalog_id": 'NGC 2516', "ra": 7.96863, "dec": -60.75347, "type": 'open cluster', "mag": 3.8, "size": 24.3, "months": [1, 2, 3, 12], "hours": 6},
    {"name": 'NGC 752', "catalog_id": 'NGC 752', "ra": 1.95967, "dec": 37.83339, "type": 'open cluster', "mag": 5.7, "size": 39.0, "months": [9, 10, 11, 12], "hours": 6},
    {"name": 'NGC 6231', "catalog_id": 'NGC 6231', "ra": 16.90303, "dec": -41.82425, "type": 'open cluster', "mag": 2.6, "size": 13.8, "months": [4, 5, 6, 7], "hours": 6},
    {"name": 'NGC 2362', "catalog_id": 'NGC 2362', "ra": 7.31152, "dec": -24.95419, "type": 'open cluster', "mag": 4.1, "size": 7.2, "months": [1, 2, 3, 12], "hours": 6},
    {"name": 'NGC 6397', "catalog_id": 'NGC 6397', "ra": 17.67816, "dec": -53.67369, "type": 'globular cluster', "mag": 5.2, "size": 15.3, "months": [5, 6, 7, 8], "hours": 8},
    {"name": 'NGC 2477', "catalog_id": 'NGC 2477', "ra": 7.86938, "dec": -38.53325, "type": 'open cluster', "mag": 5.8, "size": 18.6, "months": [1, 2, 3, 12], "hours": 6},
    {"name": 'NGC 2239', "catalog_id": 'NGC 2239', "ra": 6.5321, "dec": 4.94294, "type": 'cluster + nebula', "mag": 4.8, "size": 9.3, "months": [1, 2, 11, 12], "hours": 15},
    {"name": 'NGC 6025', "catalog_id": 'NGC 6025', "ra": 16.05494, "dec": -60.43136, "type": 'open cluster', "mag": 5.1, "size": 11.4, "months": [4, 5, 6, 7], "hours": 6},
    {"name": 'NGC 6124', "catalog_id": 'NGC 6124', "ra": 16.42224, "dec": -40.65369, "type": 'open cluster', "mag": 5.8, "size": 13.5, "months": [4, 5, 6, 7], "hours": 6},
    {"name": 'NGC 7243', "catalog_id": 'NGC 7243', "ra": 22.25238, "dec": 49.8975, "type": 'open cluster', "mag": 6.4, "size": 15.0, "months": [7, 8, 9, 10], "hours": 6},
    {"name": 'NGC 6752', "catalog_id": 'NGC 6752', "ra": 19.18105, "dec": -59.98186, "type": 'globular cluster', "mag": 6.3, "size": 13.2, "months": [6, 7, 8, 9], "hours": 8},
    {"name": 'NGC 55', "catalog_id": 'NGC 55', "ra": 0.24822, "dec": -39.19664, "type": 'galaxy', "mag": 8.5, "size": 29.9, "months": [8, 9, 10, 11], "hours": 15},
    {"name": 'NGC 362', "catalog_id": 'NGC 362', "ra": 1.05395, "dec": -70.84822, "type": 'globular cluster', "mag": 6.6, "size": 8.7, "months": [9, 10, 11, 12], "hours": 8},
    {"name": 'NGC 188', "catalog_id": 'NGC 188', "ra": 0.79098, "dec": 85.26964, "type": 'open cluster', "mag": 8.1, "size": 17.7, "months": [8, 9, 10, 11], "hours": 6},
    {"name": 'NGC 2403', "catalog_id": 'NGC 2403', "ra": 7.61428, "dec": 65.60256, "type": 'galaxy', "mag": 8.4, "size": 19.9, "months": [1, 2, 3, 12], "hours": 15},
    {"name": 'NGC 1851', "catalog_id": 'NGC 1851', "ra": 5.2352, "dec": -40.04661, "type": 'globular cluster', "mag": 7.2, "size": 9.0, "months": [1, 2, 11, 12], "hours": 8},
    {"name": 'NGC 300', "catalog_id": 'NGC 300', "ra": 0.91486, "dec": -37.68439, "type": 'galaxy', "mag": 8.7, "size": 19.4, "months": [8, 9, 10, 11], "hours": 15},
    {"name": 'NGC 2506', "catalog_id": 'NGC 2506', "ra": 8.00049, "dec": -10.76964, "type": 'open cluster', "mag": 7.6, "size": 10.8, "months": [1, 2, 3, 12], "hours": 6},
    {"name": 'NGC 663', "catalog_id": 'NGC 663', "ra": 1.77113, "dec": 61.21819, "type": 'open cluster', "mag": 7.1, "size": 6.0, "months": [9, 10, 11, 12], "hours": 6},
    {"name": 'NGC 6541', "catalog_id": 'NGC 6541', "ra": 18.13398, "dec": -43.71589, "type": 'globular cluster', "mag": 7.3, "size": 7.5, "months": [5, 6, 7, 8], "hours": 8},
    {"name": 'NGC 4833', "catalog_id": 'NGC 4833', "ra": 12.99304, "dec": -70.87458, "type": 'globular cluster', "mag": 7.8, "size": 8.4, "months": [2, 3, 4, 5], "hours": 8},
    {"name": 'NGC 247', "catalog_id": 'NGC 247', "ra": 0.78571, "dec": -20.76039, "type": 'galaxy', "mag": 9.2, "size": 19.7, "months": [8, 9, 10, 11], "hours": 15},
    {"name": 'NGC 4236', "catalog_id": 'NGC 4236', "ra": 12.27837, "dec": 69.46258, "type": 'galaxy', "mag": 9.8, "size": 23.5, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'NGC 3201', "catalog_id": 'NGC 3201', "ra": 10.29354, "dec": -46.41122, "type": 'globular cluster', "mag": 8.2, "size": 9.6, "months": [1, 2, 3, 4], "hours": 8},
    {"name": 'IC 342', "catalog_id": 'IC 342', "ra": 3.78014, "dec": 68.09636, "type": 'galaxy', "mag": 9.7, "size": 19.8, "months": [1, 10, 11, 12], "hours": 15},
    {"name": 'IC 1613', "catalog_id": 'IC 1613', "ra": 1.07994, "dec": 2.11778, "type": 'galaxy', "mag": 9.5, "size": 18.3, "months": [9, 10, 11, 12], "hours": 15},
    {"name": 'NGC 6744', "catalog_id": 'NGC 6744', "ra": 19.16281, "dec": -63.85753, "type": 'galaxy', "mag": 9.2, "size": 15.7, "months": [6, 7, 8, 9], "hours": 15},
    {"name": 'NGC 5823', "catalog_id": 'NGC 5823', "ra": 15.09184, "dec": -55.60375, "type": 'open cluster', "mag": 7.9, "size": 3.9, "months": [4, 5, 6, 7], "hours": 6},
    {"name": 'NGC 5286', "catalog_id": 'NGC 5286', "ra": 13.77405, "dec": -51.37347, "type": 'globular cluster', "mag": 8.3, "size": 6.6, "months": [3, 4, 5, 6], "hours": 8},
    {"name": 'NGC 185', "catalog_id": 'NGC 185', "ra": 0.64944, "dec": 48.33739, "type": 'galaxy', "mag": 9.2, "size": 12.9, "months": [8, 9, 10, 11], "hours": 15},
    {"name": 'NGC 6352', "catalog_id": 'NGC 6352', "ra": 17.42477, "dec": -48.42269, "type": 'globular cluster', "mag": 8.9, "size": 7.2, "months": [5, 6, 7, 8], "hours": 8},
    {"name": 'NGC 1261', "catalog_id": 'NGC 1261', "ra": 3.20426, "dec": -55.21681, "type": 'globular cluster', "mag": 8.6, "size": 5.1, "months": [1, 10, 11, 12], "hours": 8},
    {"name": 'NGC 4244', "catalog_id": 'NGC 4244', "ra": 12.29157, "dec": 37.80711, "type": 'galaxy', "mag": 10.2, "size": 16.2, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'NGC 7331', "catalog_id": 'NGC 7331', "ra": 22.61778, "dec": 34.41553, "type": 'galaxy', "mag": 9.4, "size": 9.3, "months": [7, 8, 9, 10], "hours": 15},
    {"name": 'NGC 4372', "catalog_id": 'NGC 4372', "ra": 12.42927, "dec": -72.65908, "type": 'globular cluster', "mag": 9.8, "size": 12.0, "months": [2, 3, 4, 5], "hours": 8},
    {"name": 'NGC 559', "catalog_id": 'NGC 559', "ra": 1.49255, "dec": 63.30144, "type": 'open cluster', "mag": 9.5, "size": 9.0, "months": [9, 10, 11, 12], "hours": 6},
    {"name": 'NGC 891', "catalog_id": 'NGC 891', "ra": 2.37595, "dec": 42.34914, "type": 'galaxy', "mag": 10.0, "size": 13.0, "months": [9, 10, 11, 12], "hours": 15},
    {"name": 'NGC 1097', "catalog_id": 'NGC 1097', "ra": 2.77196, "dec": -30.27489, "type": 'galaxy', "mag": 9.8, "size": 10.6, "months": [9, 10, 11, 12], "hours": 15},
    {"name": 'NGC 4697', "catalog_id": 'NGC 4697', "ra": 12.80997, "dec": -5.80075, "type": 'galaxy', "mag": 9.4, "size": 7.1, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'NGC 147', "catalog_id": 'NGC 147', "ra": 0.55337, "dec": 48.50875, "type": 'galaxy', "mag": 9.7, "size": 9.4, "months": [8, 9, 10, 11], "hours": 15},
    {"name": 'NGC 4559', "catalog_id": 'NGC 4559', "ra": 12.59935, "dec": 27.96, "type": 'galaxy', "mag": 9.9, "size": 10.6, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'NGC 4945', "catalog_id": 'NGC 4945', "ra": 13.09097, "dec": -49.46822, "type": 'galaxy', "mag": 11.9, "size": 23.3, "months": [3, 4, 5, 6], "hours": 15},
    {"name": 'NGC 4449', "catalog_id": 'NGC 4449', "ra": 12.46975, "dec": 44.09364, "type": 'galaxy', "mag": 9.6, "size": 4.7, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'NGC 6934', "catalog_id": 'NGC 6934', "ra": 20.56986, "dec": 7.40411, "type": 'globular cluster', "mag": 9.8, "size": 5.4, "months": [6, 7, 8, 9], "hours": 8},
    {"name": 'NGC 5248', "catalog_id": 'NGC 5248', "ra": 13.62556, "dec": 8.88517, "type": 'galaxy', "mag": 10.0, "size": 4.1, "months": [3, 4, 5, 6], "hours": 15},
    {"name": 'NGC 2419', "catalog_id": 'NGC 2419', "ra": 7.63554, "dec": 38.87997, "type": 'globular cluster', "mag": 10.1, "size": 4.5, "months": [1, 2, 3, 12], "hours": 8},
    {"name": 'NGC 6101', "catalog_id": 'NGC 6101', "ra": 16.43016, "dec": -72.20156, "type": 'globular cluster', "mag": 10.1, "size": 4.5, "months": [4, 5, 6, 7], "hours": 8},
    {"name": 'NGC 2867', "catalog_id": 'NGC 2867', "ra": 9.35694, "dec": -58.31167, "type": 'planetary nebula', "mag": 9.7, "size": 0.2, "months": [1, 2, 3, 4], "hours": 10},
    {"name": 'NGC 2775', "catalog_id": 'NGC 2775', "ra": 9.17226, "dec": 7.03794, "type": 'galaxy', "mag": 10.2, "size": 4.2, "months": [1, 2, 3, 4], "hours": 15},
    {"name": 'NGC 7006', "catalog_id": 'NGC 7006', "ra": 21.02479, "dec": 16.18753, "type": 'globular cluster', "mag": 10.5, "size": 4.2, "months": [7, 8, 9, 10], "hours": 8},
    {"name": 'NGC 7814', "catalog_id": 'NGC 7814', "ra": 0.05414, "dec": 16.14542, "type": 'galaxy', "mag": 10.6, "size": 4.4, "months": [8, 9, 10, 11], "hours": 15},
    {"name": 'NGC 5005', "catalog_id": 'NGC 5005', "ra": 13.18229, "dec": 37.05919, "type": 'galaxy', "mag": 10.7, "size": 4.8, "months": [3, 4, 5, 6], "hours": 15},
    {"name": 'NGC 246', "catalog_id": 'NGC 246', "ra": 0.78427, "dec": -11.87194, "type": 'planetary nebula', "mag": 10.9, "size": 4.1, "months": [8, 9, 10, 11], "hours": 10},
    {"name": 'NGC 5694', "catalog_id": 'NGC 5694', "ra": 14.66014, "dec": -26.53833, "type": 'globular cluster', "mag": 10.9, "size": 3.3, "months": [3, 4, 5, 6], "hours": 8},
    {"name": 'NGC 3626', "catalog_id": 'NGC 3626', "ra": 11.33439, "dec": 18.35683, "type": 'galaxy', "mag": 11.0, "size": 2.9, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'NGC 7479', "catalog_id": 'NGC 7479', "ra": 23.0824, "dec": 12.32289, "type": 'galaxy', "mag": 11.1, "size": 3.6, "months": [8, 9, 10, 11], "hours": 15},
    {"name": 'NGC 6729', "catalog_id": 'NGC 6729', "ra": 19.03206, "dec": -36.95764, "type": 'nebula', "mag": None, "size": 25.0, "months": [6, 7, 8, 9], "hours": 20},
    {"name": 'NGC 4889', "catalog_id": 'NGC 4889', "ra": 13.00226, "dec": 27.977, "type": 'galaxy', "mag": 11.4, "size": 2.6, "months": [3, 4, 5, 6], "hours": 15},
    {"name": 'NGC 3195', "catalog_id": 'NGC 3195', "ra": 10.15583, "dec": -80.85858, "type": 'planetary nebula', "mag": 11.6, "size": 0.7, "months": [1, 2, 3, 4], "hours": 10},
    {"name": 'NGC 6193', "catalog_id": 'NGC 6193', "ra": 16.68895, "dec": -48.76253, "type": 'open cluster', "mag": None, "size": 8.1, "months": [4, 5, 6, 7], "hours": 6},
    {"name": 'NGC 6882', "catalog_id": 'NGC 6882', "ra": 20.19885, "dec": 26.48881, "type": 'open cluster', "mag": 14.1, "size": 7.5, "months": [6, 7, 8, 9], "hours": 6},
    {"name": 'IC 353', "catalog_id": 'IC 353', "ra": 3.88363, "dec": 25.848, "type": 'nebula', "mag": None, "size": 182.0, "months": [1, 10, 11, 12], "hours": 20},
    {"name": 'IC 360', "catalog_id": 'IC 360', "ra": 4.15071, "dec": 26.13119, "type": 'nebula', "mag": None, "size": 180.0, "months": [1, 10, 11, 12], "hours": 20},
    {"name": 'IC 4592', "catalog_id": 'IC 4592', "ra": 16.19963, "dec": -19.45467, "type": 'reflection nebula', "mag": 3.9, "size": 60.0, "months": [4, 5, 6, 7], "hours": 15},
    {"name": 'IC 341', "catalog_id": 'IC 341', "ra": 3.68214, "dec": 21.96019, "type": 'nebula', "mag": None, "size": 134.9, "months": [1, 10, 11, 12], "hours": 20},
    {"name": 'IC 354', "catalog_id": 'IC 354', "ra": 3.89942, "dec": 23.147, "type": 'nebula', "mag": None, "size": 128.8, "months": [1, 10, 11, 12], "hours": 20},
    {"name": 'IC 1831', "catalog_id": 'IC 1831', "ra": 2.73233, "dec": 62.41164, "type": 'nebula', "mag": None, "size": 120.2, "months": [9, 10, 11, 12], "hours": 20},
    {"name": 'IC 4605', "catalog_id": 'IC 4605', "ra": 16.50347, "dec": -25.11517, "type": 'nebula', "mag": 4.7, "size": 30.0, "months": [4, 5, 6, 7], "hours": 20},
    {"name": 'IC 4665', "catalog_id": 'IC 4665', "ra": 17.77422, "dec": 5.64872, "type": 'open cluster', "mag": 4.2, "size": 24.6, "months": [5, 6, 7, 8], "hours": 6},
    {"name": 'IC 4756', "catalog_id": 'IC 4756', "ra": 18.64764, "dec": 5.46217, "type": 'open cluster', "mag": 4.6, "size": 24.0, "months": [5, 6, 7, 8], "hours": 6},
    {"name": 'NGC 2232', "catalog_id": 'NGC 2232', "ra": 6.46698, "dec": -4.84744, "type": 'open cluster', "mag": 3.9, "size": 9.9, "months": [1, 2, 11, 12], "hours": 6},
    {"name": 'NGC 3114', "catalog_id": 'NGC 3114', "ra": 10.04155, "dec": -60.13053, "type": 'open cluster', "mag": 4.2, "size": 12.3, "months": [1, 2, 3, 4], "hours": 6},
    {"name": 'IC 4628', "catalog_id": 'IC 4628', "ra": 16.94956, "dec": -40.45097, "type": 'nebula', "mag": None, "size": 89.1, "months": [4, 5, 6, 7], "hours": 20},
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
