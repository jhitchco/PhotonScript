"""Astronomical calculations — visibility, altitude, transit, seasonal planning."""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import lru_cache
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
    """Compute astronomical twilight start/end (sun at -18 deg) for a given night.

    Memoized on (site, scan window): the 200-point sun scan costs ~0.5 s and is
    requested many times per page (once per target before the 2026-09 perf
    pass). Callers get a fresh dict each time, so mutating it is safe.
    """
    evening = date_utc.replace(hour=23, minute=0)  # ~5pm MST in UTC
    morning = date_utc.replace(hour=13, minute=0) + timedelta(days=1)  # ~6am MST next day
    start, end = _twilight_cached(
        float(obs.latitude), float(obs.longitude), float(obs.elevation),
        evening, morning)
    return {"astro_dark_start": start, "astro_dark_end": end}


@lru_cache(maxsize=256)
def _twilight_cached(lat: float, lon: float, elev: float,
                     evening_dt: datetime, morning_dt: datetime,
                     ) -> tuple[Optional[datetime], Optional[datetime]]:
    location = EarthLocation(lat=lat * u.deg, lon=lon * u.deg, height=elev * u.m)
    evening = Time(evening_dt)
    morning = Time(morning_dt)

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

    return astro_dark_start, astro_dark_end


def _dark_sample_times(twilight: dict) -> list[datetime]:
    """10-minute samples across astronomical darkness ([] if no dark window)."""
    dark_start = twilight.get("astro_dark_start")
    dark_end = twilight.get("astro_dark_end")
    if dark_start is None or dark_end is None:
        return []
    samples = int((dark_end - dark_start).total_seconds() / 600)
    if samples < 1:
        return []
    return [dark_start + timedelta(minutes=i * 10) for i in range(samples + 1)]


def _visibility_from_alts(
    target: CelestialTarget,
    obs: ObservatoryLocation,
    date_utc: datetime,
    times: list[datetime],
    alts,
    min_altitude: float,
) -> dict:
    visible_times = [t for t, a in zip(times, alts) if a >= min_altitude]
    if not visible_times:
        return {"visible": False, "hours": 0.0, "rise_time": None, "set_time": None}
    return {
        "visible": True,
        "hours": round(len(visible_times) * 10 / 60, 1),
        "rise_time": visible_times[0],
        "set_time": visible_times[-1],
        "transit_time": compute_transit_time(target, obs, date_utc),
    }


def compute_visibility_window(
    target: CelestialTarget,
    obs: ObservatoryLocation,
    date_utc: datetime,
    min_altitude: float = 30.0,
    twilight: Optional[dict] = None,
) -> dict:
    """Compute when a target is above min_altitude during astronomical darkness.

    One vectorized AltAz transform over all 10-minute samples (was one astropy
    transform per sample). Pass ``twilight`` to skip the twilight lookup.
    """
    if twilight is None:
        twilight = get_twilight_times(obs, date_utc)
    times = _dark_sample_times(twilight)
    if not times:
        return {"visible": False, "hours": 0.0, "rise_time": None, "set_time": None}

    frame = AltAz(obstime=Time(times), location=get_earth_location(obs))
    alts = get_sky_coord(target).transform_to(frame).alt.deg
    return _visibility_from_alts(target, obs, date_utc, times, alts, min_altitude)


def altitude_grid(
    targets: list[CelestialTarget],
    obs: ObservatoryLocation,
    times,
) -> np.ndarray:
    """Altitude (deg) of every target at every time: shape (targets, times),
    from ONE broadcast AltAz transform (PS-30 campaign planner slots)."""
    if not len(targets) or not len(times):
        return np.zeros((len(targets), len(times)))
    ra = np.array([t.ra_hours * 15 for t in targets])[:, None]
    dec = np.array([t.dec_degrees for t in targets])[:, None]
    coords = SkyCoord(ra=ra * u.deg, dec=dec * u.deg)
    tt = times if isinstance(times, Time) else Time(list(times))
    frame = AltAz(obstime=tt[None, :], location=get_earth_location(obs))
    return np.asarray(coords.transform_to(frame).alt.deg)


def altitude_series(
    target: CelestialTarget,
    obs: ObservatoryLocation,
    times,
) -> np.ndarray:
    """Altitude (deg) of one target at each time (one vectorized transform;
    compute_altitude() does one transform per call)."""
    return altitude_grid([target], obs, times)[0]


def dark_windows(
    obs: ObservatoryLocation,
    dates: list[datetime],
    step_min: float = 2.0,
) -> list[tuple[Optional[datetime], Optional[datetime]]]:
    """Astronomical dark (sun below -18 deg) for many nights at once.

    Same convention as get_twilight_times(): `dates` are UTC midnights and the
    scan runs 23:00 UTC that day to 13:00 UTC the next. One sun transform for
    all nights (get_twilight_times costs ~0.5 s per uncached night), crossings
    linearly interpolated between `step_min` samples."""
    if not dates:
        return []
    n = int(round(14 * 60 / step_min)) + 1
    offs = np.arange(n) * step_min / 1440.0
    base = np.array([Time(d.replace(hour=23, minute=0, second=0,
                                    microsecond=0)).jd for d in dates])
    jd = (base[:, None] + offs[None, :]).ravel()
    t = Time(jd, format="jd")
    alt = get_sun(t).transform_to(
        AltAz(obstime=t, location=get_earth_location(obs))
    ).alt.deg.reshape(len(dates), n)
    out = []
    for row, b in zip(alt, base):
        start = end = None
        for i in range(n - 1):
            a0, a1 = row[i], row[i + 1]
            if start is None and a0 > -18 >= a1:
                f = (a0 + 18) / (a0 - a1)
                start = Time(b + (i + f) * step_min / 1440.0,
                             format="jd").datetime
            if a0 <= -18 < a1:
                f = (-18 - a0) / (a1 - a0)
                end = Time(b + (i + f) * step_min / 1440.0,
                           format="jd").datetime
        out.append((start, end))
    return out


def rank_targets_for_night(
    targets: list[CelestialTarget],
    obs: ObservatoryLocation,
    date_utc: datetime,
    min_altitude: float = 30.0,
) -> list[dict]:
    """Rank targets by visibility hours and assign tiers.

    Twilight is computed once and every target x sample altitude comes from a
    single broadcast AltAz transform (was ~60 transforms per target: ~13 s for
    the dashboard's seasonal list on the scope PC).
    """
    results = []
    twilight = get_twilight_times(obs, date_utc)
    times = _dark_sample_times(twilight)
    if targets and times:
        ra = np.array([t.ra_hours * 15 for t in targets])[:, None]
        dec = np.array([t.dec_degrees for t in targets])[:, None]
        coords = SkyCoord(ra=ra * u.deg, dec=dec * u.deg)
        frame = AltAz(obstime=Time(times)[None, :], location=get_earth_location(obs))
        alt_grid = coords.transform_to(frame).alt.deg  # shape (targets, samples)
        for target, alts in zip(targets, alt_grid):
            vis = _visibility_from_alts(target, obs, date_utc, times, alts, min_altitude)
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

    # PS-124: RC16-scale autumn / winter additions (the rest of the ticket's
    # list was already here; their goal defaults live in CATALOG_EXTRAS).
    # NGC 604: giant HII region in M33, J2000 01h34m33s +30d47m (SIMBAD).
    {"name": "NGC 604", "catalog_id": "NGC 604", "ra": 1.5758, "dec": 30.783, "type": "emission nebula", "mag": None, "size": 2.0, "months": [9, 10, 11, 12, 1], "hours": 10},
    # IC 410 + NGC 1893: centered on the cluster (J2000 05h22m44s +33d24m42s);
    # the Tadpoles sit a few arcmin NE of it. Unverified offline: check the
    # framing on the first night (RC16 field is 24' x 16', IC 410 is ~40').
    {"name": "Tadpoles Nebula", "catalog_id": "IC 410", "ra": 5.37889, "dec": 33.41167, "type": "emission nebula", "mag": None, "size": 40, "months": [11, 12, 1, 2], "hours": 15},

    # --- Expanded catalog (OpenNGC via pyongc, precise J2000; auto-generated 2026-09-18). The curated entries above are kept verbatim for their intentional framing centers; entries below use catalog centers. ---
    {"name": 'Pleiades', "catalog_id": 'M 45', "ra": 3.79128, "dec": 24.10528, "type": 'open cluster', "mag": 1.2, "size": 150.0, "months": [1, 10, 11, 12], "hours": 6},
    {"name": 'Small Sgr Star Cloud', "catalog_id": 'M 24', "ra": 18.28226, "dec": -18.51456, "type": 'association', "mag": 4.5, "size": 120.0, "months": [5, 6, 7, 8], "hours": 12},
    {"name": 'Beehive', "catalog_id": 'M 44', "ra": 8.67283, "dec": 19.67206, "type": 'open cluster', "mag": 3.1, "size": 108.6, "months": [1, 2, 3, 12], "hours": 6},
    {"name": "Ptolemy's Cluster", "catalog_id": 'M 7', "ra": 17.89755, "dec": -34.79283, "type": 'open cluster', "mag": 3.3, "size": 22.2, "months": [5, 6, 7, 8], "hours": 6},
    {"name": 'Butterfly Cluster', "catalog_id": 'M 6', "ra": 17.67243, "dec": -32.25417, "type": 'open cluster', "mag": 4.2, "size": 15.6, "months": [5, 6, 7, 8], "hours": 6},
    {"name": 'Hercules Globular Cluster', "catalog_id": 'M 13', "ra": 16.6949, "dec": 36.46131, "type": 'globular cluster', "mag": 5.8, "size": 16.5, "months": [4, 5, 6, 7], "hours": 8},
    {"name": "Bode's Galaxy", "catalog_id": 'M 81', "ra": 9.92588, "dec": 69.06531, "type": 'galaxy', "mag": 6.9, "size": 21.6, "months": [1, 2, 3, 4], "hours": 15},
    {"name": 'Wild Duck Cluster', "catalog_id": 'M 11', "ra": 18.85166, "dec": -6.27003, "type": 'open cluster', "mag": 5.8, "size": 9.0, "months": [5, 6, 7, 8], "hours": 6},  # PS-135: was the French "Amas de l'Ecu de Sobieski"
    {"name": 'Southern Pinwheel Galaxy', "catalog_id": 'M 83', "ra": 13.61693, "dec": -29.86542, "type": 'galaxy', "mag": 7.2, "size": 13.6, "months": [3, 4, 5, 6], "hours": 15},
    {"name": "Mairan's Nebula", "catalog_id": 'M 43', "ra": 5.59205, "dec": -5.26747, "type": 'emission nebula', "mag": 9.0, "size": 20.0, "months": [1, 2, 11, 12], "hours": 20},
    {"name": 'Dumbbell Nebula', "catalog_id": 'M 27', "ra": 19.99344, "dec": 22.72103, "type": 'planetary nebula', "mag": 7.4, "size": 6.7, "months": [6, 7, 8, 9], "hours": 10},
    {"name": 'Cigar Galaxy', "catalog_id": 'M 82', "ra": 9.93131, "dec": 69.67939, "type": 'galaxy', "mag": 8.3, "size": 11.0, "months": [1, 2, 3, 4], "hours": 15},
    {"name": 'Sunflower Galaxy', "catalog_id": 'M 63', "ra": 13.2637, "dec": 42.02928, "type": 'galaxy', "mag": 8.6, "size": 11.8, "months": [3, 4, 5, 6], "hours": 15},
    {"name": 'Black Eye Galaxy', "catalog_id": 'M 64', "ra": 12.94546, "dec": 21.68297, "type": 'galaxy', "mag": 8.5, "size": 10.5, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Virgo Galaxy', "catalog_id": 'M 87', "ra": 12.51373, "dec": 12.39111, "type": 'galaxy', "mag": 9.0, "size": 7.1, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Coma Pinwheel', "catalog_id": 'M 99', "ra": 12.31378, "dec": 14.4165, "type": 'galaxy', "mag": 9.8, "size": 5.0, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Little Dumbbell Nebula', "catalog_id": 'M 76', "ra": 1.70547, "dec": 51.57547, "type": 'planetary nebula', "mag": 10.1, "size": 2.7, "months": [9, 10, 11, 12], "hours": 10},  # PS-135: was "Barbell Nebula", 1.1' (NGC 650 lobe only); M 76 is 2.7' x 1.8'
    {"name": 'Carina Nebula', "catalog_id": 'NGC 3372', "ra": 10.75237, "dec": -59.86669, "type": 'emission nebula', "mag": 3.0, "size": 120.0, "months": [1, 2, 3, 4], "hours": 20},
    {"name": 'Omicron Velorum Cluster', "catalog_id": 'IC 2391', "ra": 8.67552, "dec": -53.03547, "type": 'open cluster', "mag": 2.5, "size": 29.1, "months": [1, 2, 3, 12], "hours": 6},
    {"name": '47 Tuc Cluster', "catalog_id": 'NGC 104', "ra": 0.40149, "dec": -72.08144, "type": 'globular cluster', "mag": 4.1, "size": 31.8, "months": [8, 9, 10, 11], "hours": 8},
    {"name": 'Wishing Well Cluster', "catalog_id": 'NGC 3532', "ra": 11.09662, "dec": -58.7705, "type": 'open cluster', "mag": 3.0, "size": 12.0, "months": [2, 3, 4, 5], "hours": 6},
    {"name": 'Omega Centauri', "catalog_id": 'NGC 5139', "ra": 13.44608, "dec": -47.47686, "type": 'globular cluster', "mag": 5.3, "size": 27.0, "months": [3, 4, 5, 6], "hours": 8},
    {"name": 'Lambda Centauri Nebula', "catalog_id": 'IC 2944', "ra": 11.59637, "dec": -63.01983, "type": 'cluster + nebula', "mag": 4.5, "size": 7.2, "months": [2, 3, 4, 5], "hours": 15},
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
    {"name": 'Sculptor Galaxy', "catalog_id": 'NGC 253', "ra": 0.79253, "dec": -25.28822, "type": 'galaxy', "mag": 7.1, "size": 26.8, "months": [8, 9, 10, 11], "hours": 15},  # PS-135: was "Sculptor Filament", mag 11.1 (V ~7.1)
    {"name": "Barnard's Galaxy", "catalog_id": 'NGC 6822', "ra": 19.74937, "dec": -14.80344, "type": 'galaxy', "mag": 10.1, "size": 17.4, "months": [6, 7, 8, 9], "hours": 15},
    {"name": 'Saturn Nebula', "catalog_id": 'NGC 7009', "ra": 21.06966, "dec": -11.36325, "type": 'planetary nebula', "mag": 8.0, "size": 0.7, "months": [7, 8, 9, 10], "hours": 10},
    {"name": 'Theta Carinae Cluster', "catalog_id": 'IC 2602', "ra": 10.71596, "dec": -64.39419, "type": 'open cluster', "mag": None, "size": 48.0, "months": [1, 2, 3, 4], "hours": 6},
    {"name": 'Spindle Galaxy', "catalog_id": 'NGC 3115', "ra": 10.08722, "dec": -7.71858, "type": 'galaxy', "mag": 9.1, "size": 7.1, "months": [1, 2, 3, 4], "hours": 15},
    {"name": "Copeland's Blue Snowball", "catalog_id": 'NGC 7662', "ra": 23.43164, "dec": 42.53494, "type": 'planetary nebula', "mag": 8.3, "size": 0.3, "months": [8, 9, 10, 11], "hours": 10},
    {"name": 'Needle Galaxy', "catalog_id": 'NGC 4565', "ra": 12.60577, "dec": 25.98767, "type": 'galaxy', "mag": 10.9, "size": 16.8, "months": [2, 3, 4, 5], "hours": 15},
    {"name": "Cat's Eye Nebula", "catalog_id": 'NGC 6543', "ra": 17.97594, "dec": 66.63319, "type": 'planetary nebula', "mag": 9.0, "size": 0.9, "months": [4, 5, 6, 7, 8, 9, 10, 11], "hours": 10},  # Draco, dec +66 -> circumpolar from AARO (never sets); widened months 2026-09-25 so it's offered in autumn too
    {"name": "Fetus Nebula", "catalog_id": 'NGC 7008', "ra": 21.0092, "dec": 54.5436, "type": 'planetary nebula', "mag": 10.7, "size": 1.4, "months": [7, 8, 9, 10, 11], "hours": 15},
    {"name": 'Eight-Burst Nebula', "catalog_id": 'NGC 3132', "ra": 10.11715, "dec": -40.43658, "type": 'planetary nebula', "mag": 9.2, "size": 0.5, "months": [1, 2, 3, 4], "hours": 10},
    {"name": 'Blinking Planetary', "catalog_id": 'NGC 6826', "ra": 19.7467, "dec": 50.52503, "type": 'planetary nebula', "mag": 9.4, "size": 0.4, "months": [6, 7, 8, 9], "hours": 10},
    {"name": 'Eskimo Nebula', "catalog_id": 'NGC 2392', "ra": 7.48632, "dec": 20.91183, "type": 'planetary nebula', "mag": 9.6, "size": 0.9, "months": [1, 2, 3, 12], "hours": 10},
    {"name": 'Bug Nebula', "catalog_id": 'NGC 6302', "ra": 17.22906, "dec": -37.10314, "type": 'planetary nebula', "mag": 9.6, "size": 0.7, "months": [5, 6, 7, 8], "hours": 10},
    {"name": "Hubble's Nebula", "catalog_id": 'NGC 2261', "ra": 6.65264, "dec": 8.74433, "type": 'reflection nebula', "mag": 11.8, "size": 2.0, "months": [1, 2, 11, 12], "hours": 15},
    {"name": 'Bow-Tie Nebula', "catalog_id": 'NGC 40', "ra": 0.21695, "dec": 72.52194, "type": 'planetary nebula', "mag": 11.9, "size": 0.8, "months": [8, 9, 10, 11], "hours": 10},
    {"name": 'Perseus A', "catalog_id": 'NGC 1275', "ra": 3.33004, "dec": 41.51169, "type": 'galaxy', "mag": 12.2, "size": 2.2, "months": [1, 10, 11, 12], "hours": 15},
    {"name": "Herschel's Jewel Box", "catalog_id": 'NGC 4755', "ra": 12.89363, "dec": -60.35631, "type": 'open cluster', "mag": None, "size": 7.8, "months": [2, 3, 4, 5], "hours": 6},
    {"name": 'Large Magellanic Cloud', "catalog_id": 'ESO056-115', "ra": 5.39292, "dec": -69.75611, "type": 'galaxy', "mag": 0.3, "size": 646.0, "months": [1, 2, 11, 12], "hours": 15},
    {"name": 'Small Magellanic Cloud', "catalog_id": 'NGC 292', "ra": 0.87911, "dec": -72.82861, "type": 'galaxy', "mag": 2.3, "size": 299.9, "months": [8, 9, 10, 11], "hours": 15},
    {"name": 'California Nebula', "catalog_id": 'NGC 1499', "ra": 4.05401, "dec": 36.36747, "type": 'nebula', "mag": 5.0, "size": 160.0, "months": [1, 10, 11, 12], "hours": 20},
    {"name": 'Hyades', "catalog_id": 'C041', "ra": 4.44833, "dec": 15.86667, "type": 'open cluster', "mag": None, "size": 329.0, "months": [1, 10, 11, 12], "hours": 6},
    {"name": 'Coma Star Cluster', "catalog_id": 'Mel111', "ra": 12.41833, "dec": 26.1, "type": 'open cluster', "mag": None, "size": 253.5, "months": [2, 3, 4, 5], "hours": 6},
    {"name": 'Witch Head Nebula', "catalog_id": 'NGC 1909', "ra": 5.08207, "dec": -7.26564, "type": 'reflection nebula', "mag": None, "size": 180.0, "months": [1, 2, 11, 12], "hours": 15},
    {"name": "Brocchi's Cluster", "catalog_id": 'Cl399', "ra": 19.42333, "dec": 20.18333, "type": 'association', "mag": 3.6, "size": 70.0, "months": [6, 7, 8, 9], "hours": 12},
    {"name": 'Rho Ophiuchi Nebula', "catalog_id": 'IC 4604', "ra": 16.42532, "dec": -23.43658, "type": 'nebula', "mag": 5.1, "size": 60.0, "months": [4, 5, 6, 7], "hours": 20},
    {"name": 'IC 434', "catalog_id": 'IC 434', "ra": 5.68358, "dec": -2.45378, "type": 'emission nebula', "mag": 11.0, "size": 90.0, "months": [1, 2, 11, 12], "hours": 20},  # PS-135: was "Flame Nebula" (that is NGC 2024); IC 434 is the Horsehead's backdrop
    {"name": 'Flame Nebula', "catalog_id": 'NGC 2024', "ra": 5.69833, "dec": -1.85, "type": 'emission nebula', "mag": None, "size": 30.0, "months": [1, 2, 11, 12], "hours": 20},  # PS-135: the real Flame (05 41 54, -01 51)
    {"name": 'Pelican Nebula', "catalog_id": 'IC 5070', "ra": 20.8502, "dec": 44.4015, "type": 'emission nebula', "mag": 8.0, "size": 60.0, "months": [6, 7, 8, 9], "hours": 20},
    {"name": 'Lower Sword', "catalog_id": 'NGC 1980', "ra": 5.59055, "dec": -5.90989, "type": 'cluster + nebula', "mag": 2.5, "size": 9.3, "months": [1, 2, 11, 12], "hours": 15},
    {"name": 'h Persei Cluster', "catalog_id": 'NGC 869', "ra": 2.31627, "dec": 57.11725, "type": 'open cluster', "mag": 3.7, "size": 14.4, "months": [9, 10, 11, 12], "hours": 6},
    {"name": 'Christmas Tree Cluster', "catalog_id": 'NGC 2264', "ra": 6.68285, "dec": 9.89547, "type": 'cluster + nebula', "mag": 3.9, "size": 11.4, "months": [1, 2, 11, 12], "hours": 15},
    {"name": 'chi Persei Cluster', "catalog_id": 'NGC 884', "ra": 2.37558, "dec": 57.14411, "type": 'open cluster', "mag": 3.8, "size": 10.5, "months": [9, 10, 11, 12], "hours": 6},
    {"name": 'Upper Sword', "catalog_id": 'NGC 1981', "ra": 5.586, "dec": -4.42506, "type": 'cluster + nebula', "mag": 4.2, "size": 9.0, "months": [1, 2, 11, 12], "hours": 15},
    {"name": 'Great Bird Cluster', "catalog_id": 'NGC 2301', "ra": 6.86258, "dec": 0.45919, "type": 'open cluster', "mag": 6.0, "size": 10.2, "months": [1, 2, 11, 12], "hours": 6},
    # PS-135: IC 4703 "Eagle Nebula" dropped, it is the nebula of the M 16 row (IC 4703 is an M 16 alias)
    {"name": 'NGC 6995 (Eastern Veil, part)', "catalog_id": 'NGC 6995', "ra": 20.95299, "dec": 31.23517, "type": 'supernova remnant', "mag": 7.0, "size": 12.0, "months": [6, 7, 8, 9], "hours": 20},
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
    {"name": 'Eyes (NGC 4438)', "catalog_id": 'NGC 4438', "ra": 12.46266, "dec": 13.00883, "type": 'galaxy', "mag": 10.9, "size": 9.2, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Merope Nebula', "catalog_id": 'NGC 1435', "ra": 3.76947, "dec": 23.76497, "type": 'nebula', "mag": None, "size": 30.0, "months": [1, 10, 11, 12], "hours": 20},
    {"name": 'Pencil Nebula', "catalog_id": 'NGC 2736', "ra": 9.00471, "dec": -45.94806, "type": 'emission nebula', "mag": None, "size": 30.0, "months": [1, 2, 3, 4], "hours": 20},
    {"name": 'Butterfly Galaxies (NGC 4568)', "catalog_id": 'NGC 4568', "ra": 12.60952, "dec": 11.23889, "type": 'galaxy', "mag": 10.8, "size": 4.3, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Fourcade-Figueroa', "catalog_id": 'ESO270-017', "ra": 13.57981, "dec": -45.5475, "type": 'galaxy', "mag": 11.7, "size": 11.5, "months": [3, 4, 5, 6], "hours": 15},
    {"name": 'Umbrella Galaxy', "catalog_id": 'NGC 4651', "ra": 12.72851, "dec": 16.39339, "type": 'galaxy', "mag": 10.8, "size": 3.9, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Fornax B', "catalog_id": 'NGC 1317', "ra": 3.37897, "dec": -37.10369, "type": 'galaxy', "mag": 10.9, "size": 3.1, "months": [1, 10, 11, 12], "hours": 15},
    {"name": 'Eyes (NGC 4435)', "catalog_id": 'NGC 4435', "ra": 12.46125, "dec": 13.07894, "type": 'galaxy', "mag": 11.0, "size": 3.0, "months": [2, 3, 4, 5], "hours": 15},
    {"name": "Barnard's Merope Nebula", "catalog_id": 'IC 349', "ra": 3.77225, "dec": 23.93981, "type": 'reflection nebula', "mag": None, "size": 25.7, "months": [1, 10, 11, 12], "hours": 15},
    {"name": 'Helix Galaxy', "catalog_id": 'NGC 2685', "ra": 8.92631, "dec": 58.73439, "type": 'galaxy', "mag": 11.3, "size": 4.3, "months": [1, 2, 3, 12], "hours": 15},
    {"name": 'Sextans B', "catalog_id": 'UGC05373', "ra": 10.00003, "dec": 5.33222, "type": 'galaxy', "mag": 11.5, "size": 4.9, "months": [1, 2, 3, 4], "hours": 15},
    {"name": 'Butterfly Galaxies (NGC 4567)', "catalog_id": 'NGC 4567', "ra": 12.60909, "dec": 11.258, "type": 'galaxy', "mag": 11.3, "size": 2.7, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Blue Flash Nebula', "catalog_id": 'NGC 6905', "ra": 20.37305, "dec": 20.10453, "type": 'planetary nebula', "mag": 11.1, "size": 0.7, "months": [6, 7, 8, 9], "hours": 10},
    {"name": 'Sextans A', "catalog_id": 'PGC029653', "ra": 10.18356, "dec": -4.69278, "type": 'galaxy', "mag": 11.8, "size": 5.2, "months": [1, 2, 3, 4], "hours": 15},
    {"name": 'Little Gem', "catalog_id": 'NGC 6445', "ra": 17.82085, "dec": -20.0095, "type": 'planetary nebula', "mag": 11.2, "size": 0.6, "months": [5, 6, 7, 8], "hours": 10},
    {"name": 'Little Ghost Nebula', "catalog_id": 'NGC 6369', "ra": 17.48903, "dec": -23.75944, "type": 'planetary nebula', "mag": 11.4, "size": 0.6, "months": [5, 6, 7, 8], "hours": 10},
    {"name": 'Bear Paw Galaxy', "catalog_id": 'NGC 2537', "ra": 8.22073, "dec": 45.98981, "type": 'galaxy', "mag": 11.7, "size": 2.1, "months": [1, 2, 3, 12], "hours": 15},
    {"name": 'Box Nebula', "catalog_id": 'NGC 6309', "ra": 17.23453, "dec": -12.91056, "type": 'planetary nebula', "mag": 11.5, "size": 0.3, "months": [5, 6, 7, 8], "hours": 10},
    {"name": 'Phantom Streak Nebula', "catalog_id": 'NGC 6741', "ra": 19.04361, "dec": -0.44939, "type": 'planetary nebula', "mag": 11.5, "size": 0.1, "months": [6, 7, 8, 9], "hours": 10},  # PS-124: J2000 Dec -00d26'58" (the generator dropped the sign of "-00")
    {"name": 'Rim Nebula', "catalog_id": 'NGC 6188', "ra": 16.66829, "dec": -48.66228, "type": 'nebula', "mag": None, "size": 20.0, "months": [4, 5, 6, 7], "hours": 20},
    {"name": 'Red Spider Nebula', "catalog_id": 'NGC 6537', "ra": 18.08697, "dec": -19.84297, "type": 'planetary nebula', "mag": 11.6, "size": 0.2, "months": [5, 6, 7, 8], "hours": 10},
    {"name": 'Miniature Spiral', "catalog_id": 'NGC 3928', "ra": 11.86323, "dec": 48.68314, "type": 'galaxy', "mag": 12.5, "size": 1.4, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Medusa Galaxy Merger', "catalog_id": 'NGC 4194', "ra": 12.23596, "dec": 54.52683, "type": 'galaxy', "mag": 12.8, "size": 1.6, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Running Man Nebula', "catalog_id": 'NGC 1977', "ra": 5.58772, "dec": -4.84433, "type": 'cluster + nebula', "mag": None, "size": 10.2, "months": [1, 2, 11, 12], "hours": 15},
    {"name": 'Omicron Persei Cloud', "catalog_id": 'IC 348', "ra": 3.74283, "dec": 32.16283, "type": 'cluster + nebula', "mag": None, "size": 10.0, "months": [1, 10, 11, 12], "hours": 15},
    {"name": 'Rosette B', "catalog_id": 'NGC 2246', "ra": 6.54272, "dec": 5.12828, "type": 'nebula', "mag": None, "size": 10.0, "months": [1, 2, 11, 12], "hours": 20},
    {"name": 'Polarissima Australis', "catalog_id": 'NGC 2573', "ra": 1.6937, "dec": -89.33453, "type": 'galaxy', "mag": 13.5, "size": 1.9, "months": [9, 10, 11, 12], "hours": 15},
    {"name": 'Toby Jug Nebula', "catalog_id": 'IC 2220', "ra": 7.94749, "dec": -59.12578, "type": 'reflection nebula', "mag": None, "size": 5.0, "months": [1, 2, 3, 12], "hours": 15},
    {"name": 'Fornax Dwarf Cluster 3', "catalog_id": 'NGC 1049', "ra": 2.66337, "dec": -34.25825, "type": 'globular cluster', "mag": 13.6, "size": 1.2, "months": [9, 10, 11, 12], "hours": 8},
    {"name": "Stephan's Quintet", "catalog_id": 'HCG092', "ra": 22.59972, "dec": 33.95833, "type": 'galaxy group', "mag": None, "size": 4.4, "months": [7, 8, 9, 10], "hours": 18},
    {"name": 'War and Peace Nebula', "catalog_id": 'NGC 6357', "ra": 17.4121, "dec": -34.20133, "type": 'cluster + nebula', "mag": None, "size": 3.9, "months": [5, 6, 7, 8], "hours": 15},
    {"name": 'Mice Galaxy', "catalog_id": 'NGC 4676', "ra": 12.76964, "dec": 30.72722, "type": 'galaxy pair', "mag": None, "size": 3.0, "months": [2, 3, 4, 5], "hours": 18},
    {"name": "Seyfert's Sextet", "catalog_id": 'HCG079', "ra": 15.98664, "dec": 20.75861, "type": 'galaxy group', "mag": None, "size": 2.8, "months": [4, 5, 6, 7], "hours": 18},
    {"name": 'NGC 4990', "catalog_id": 'NGC 4990', "ra": 13.1548, "dec": -5.27281, "type": 'galaxy', "mag": 13.8, "size": 0.9, "months": [3, 4, 5, 6], "hours": 15},  # PS-135: was "Cocoon Galaxy" (that is NGC 4490, Canes Venatici)
    {"name": 'Cocoon Galaxy', "catalog_id": 'NGC 4490', "ra": 12.51, "dec": 41.64333, "type": 'galaxy', "mag": 9.8, "size": 6.3, "months": [2, 3, 4, 5], "hours": 15},  # PS-135: 12 30 36, +41 38
    {"name": 'Guitar Galaxy', "catalog_id": 'NGC 3561', "ra": 11.187, "dec": 28.69647, "type": 'galaxy', "mag": 14.7, "size": 1.7, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'Polarissima Borealis', "catalog_id": 'NGC 3172', "ra": 11.78722, "dec": 89.09306, "type": 'galaxy', "mag": 15.0, "size": 1.1, "months": [2, 3, 4, 5], "hours": 15},
    {"name": 'IC 2431', "catalog_id": 'IC 2431', "ra": 9.07631, "dec": 14.59578, "type": 'galaxy', "mag": 14.0, "size": 0.6, "months": [1, 2, 3, 4], "hours": 15},
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
    {"name": 'M 2', "catalog_id": 'M 2', "ra": 21.5575, "dec": -0.82331, "type": 'globular cluster', "mag": 6.2, "size": 8.4, "months": [7, 8, 9, 10], "hours": 8},  # PS-124: Dec -00d49'24" (sign was lost)
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
    {"name": 'M 77', "catalog_id": 'M 77', "ra": 2.71131, "dec": -0.01328, "type": 'galaxy', "mag": 9.3, "size": 6.1, "months": [9, 10, 11, 12], "hours": 15},  # PS-124: Dec -00d00'48" (sign was lost)
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
    {"name": 'NGC 4945', "catalog_id": 'NGC 4945', "ra": 13.09097, "dec": -49.46822, "type": 'galaxy', "mag": 9.3, "size": 23.3, "months": [3, 4, 5, 6], "hours": 15},
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


# PS-124: goal defaults and extra names per catalog_id, merged into the entry
# at lookup time (the generated rows above stay one line each).
#   aliases     more names the Add box accepts (matched with target_key, so
#               case, spaces and punctuation do not matter)
#   goal_hours  RC16 goal when the Add box gives no budget (else 8 h)
#   mix         narrowband split {filter: percent} (galaxies keep LRGB)
#   osc_hours   Piggy-600 OSC goal added with the project (two-rig bonus)
#   note        one line for the dashboard
CATALOG_EXTRAS: dict[str, dict] = {
    "NGC 604": {"aliases": ["NGC604 in M33"], "goal_hours": 10,
                "mix": {"Ha": 50, "OIII": 40, "SII": 10}, "osc_hours": 10,
                "note": "HII region in M33; the Piggy-600 frames all of M33 "
                        "(70' x 40') about 12' off centre"},
    "IC 410": {"aliases": ["Tadpoles", "Tadpole Nebula", "NGC 1893"],
               "goal_hours": 15, "osc_hours": 8,
               "note": "RC16 crops the Tadpoles; the Piggy-600 gets the whole "
                       "IC 410 region"},
    "NGC 7331": {"aliases": ["Deer Lick Group", "Deer Lick"], "goal_hours": 15,
                 "note": "Deer Lick companions fit the RC16 field"},
    "NGC 891": {"aliases": ["Silver Sliver"], "goal_hours": 15},
    "HCG092": {"aliases": ["HCG 92"], "goal_hours": 18},
    "M 76": {"aliases": ["Little Dumbbell", "Barbell Nebula", "NGC 650"],
             "goal_hours": 10, "mix": {"Ha": 50, "OIII": 40, "SII": 10}},
    "NGC 7662": {"aliases": ["Blue Snowball"], "goal_hours": 8,
                 "mix": {"Ha": 40, "OIII": 50, "SII": 10}},
    "NGC 40": {"aliases": ["Bow-Tie"], "goal_hours": 10,
               "mix": {"Ha": 50, "OIII": 30, "SII": 20}},
    "M 1": {"aliases": ["Crab", "NGC 1952"], "goal_hours": 12,
            "mix": {"Ha": 40, "OIII": 30, "SII": 30}},
    "NGC 2392": {"aliases": ["Clownface Nebula"], "goal_hours": 8,
                 "mix": {"Ha": 40, "OIII": 50, "SII": 10}},
    "M 74": {"aliases": ["Phantom Galaxy", "NGC 628"], "goal_hours": 15},
    "M 77": {"aliases": ["Cetus A", "NGC 1068"], "goal_hours": 12},
    "NGC 7008": {"goal_hours": 10, "mix": {"Ha": 40, "OIII": 50, "SII": 10}},
    # PS-135 rows follow-up: one canonical target per object, old names kept
    "NGC 2024": {"aliases": ["Flame"], "goal_hours": 10,
                 "mix": {"Ha": 60, "OIII": 15, "SII": 25}},
    "M 16": {"aliases": ["Eagle Nebula", "IC 4703"]},
    "NGC 6992": {"aliases": ["Eastern Veil", "NGC 6995"]},
    "NGC 6995": {"aliases": ["Eastern Veil", "NGC 6992"],
                 "note": "the southern end of the Eastern Veil; goal credit "
                         "goes to the Eastern Veil (NGC 6992)"},
    "NGC 2537": {"aliases": ["Bear Claw Nebula", "Bear's Paw Galaxy"]},
    "IC 2431": {"aliases": ["Browning"]},
}

# PS-124: the user catalog (<data_dir>/user_catalog.json), set by
# scheduler/catalog.py at startup and after each add. Same row shape as
# SEASONAL_TARGETS.
_USER_TARGETS: list[dict] = []


def set_user_targets(entries: list[dict]) -> None:
    global _USER_TARGETS
    _USER_TARGETS = list(entries or [])
    _ALIAS_CACHE.pop("sig", None)


def catalog_entries() -> list[dict]:
    """Every catalog row: the built-in list, then the user catalog, each
    merged with its CATALOG_EXTRAS (a copy; the lists are not changed)."""
    out = []
    for e in (*SEASONAL_TARGETS, *_USER_TARGETS):
        extra = CATALOG_EXTRAS.get(e.get("catalog_id", ""))
        out.append({**extra, **e} if extra else dict(e))
    return out


def rig_hint(size_arcmin: float | None) -> str:
    """PS-124: which rig suits the target: "rc16" under 15', "piggyback" over
    60', "both" in between (RC16 crop plus a Piggy-600 wide field)."""
    if size_arcmin is None:
        return "rc16"
    if size_arcmin < 15:
        return "rc16"
    if size_arcmin > 60:
        return "piggyback"
    return "both"


def months_for_ra(ra_hours: float) -> list[int]:
    """Four months around the midnight transit (RA 0h transits at midnight
    in late September), the rule the generated rows above follow."""
    start = int(round(7.9 + (ra_hours % 24) / 2))
    return [((start + i - 1) % 12) + 1 for i in range(4)]


def _entry_keys(e: dict) -> set[str]:
    from photonscript.shared.target_names import target_key
    names = [e.get("name"), e.get("catalog_id"), *(e.get("aliases") or [])]
    names += str(e.get("catalog_id") or "").split("/")
    return {target_key(n) for n in names} - {""}


def find_catalog_entry(name: str) -> dict | None:
    """PS-124: the catalog row for a name, catalog id or alias ("M76",
    "m 76", "Little Dumbbell" all find M 76). Built-in rows win over the
    user catalog; None when nothing matches."""
    from photonscript.shared.target_names import target_key
    key = target_key(name)
    if not key:
        return None
    for e in catalog_entries():
        if key in _entry_keys(e):
            return e
    return None


# PS-135: Messier number -> NGC / IC designation (the standard cross
# identifications; M 24, M 40, M 45 and M 102 have no single NGC object).
MESSIER_NGC: dict[int, str] = {
    1: "NGC 1952", 2: "NGC 7089", 3: "NGC 5272", 4: "NGC 6121",
    5: "NGC 5904", 6: "NGC 6405", 7: "NGC 6475", 8: "NGC 6523",
    9: "NGC 6333", 10: "NGC 6254", 11: "NGC 6705", 12: "NGC 6218",
    13: "NGC 6205", 14: "NGC 6402", 15: "NGC 7078", 16: "NGC 6611",
    17: "NGC 6618", 18: "NGC 6613", 19: "NGC 6273", 20: "NGC 6514",
    21: "NGC 6531", 22: "NGC 6656", 23: "NGC 6494", 25: "IC 4725",
    26: "NGC 6694", 27: "NGC 6853", 28: "NGC 6626", 29: "NGC 6913",
    30: "NGC 7099", 31: "NGC 224", 32: "NGC 221", 33: "NGC 598",
    34: "NGC 1039", 35: "NGC 2168", 36: "NGC 1960", 37: "NGC 2099",
    38: "NGC 1912", 39: "NGC 7092", 41: "NGC 2287", 42: "NGC 1976",
    43: "NGC 1982", 44: "NGC 2632", 46: "NGC 2437", 47: "NGC 2422",
    48: "NGC 2548", 49: "NGC 4472", 50: "NGC 2323", 51: "NGC 5194",
    52: "NGC 7654", 53: "NGC 5024", 54: "NGC 6715", 55: "NGC 6809",
    56: "NGC 6779", 57: "NGC 6720", 58: "NGC 4579", 59: "NGC 4621",
    60: "NGC 4649", 61: "NGC 4303", 62: "NGC 6266", 63: "NGC 5055",
    64: "NGC 4826", 65: "NGC 3623", 66: "NGC 3627", 67: "NGC 2682",
    68: "NGC 4590", 69: "NGC 6637", 70: "NGC 6681", 71: "NGC 6838",
    72: "NGC 6981", 73: "NGC 6994", 74: "NGC 628", 75: "NGC 6864",
    76: "NGC 650", 77: "NGC 1068", 78: "NGC 2068", 79: "NGC 1904",
    80: "NGC 6093", 81: "NGC 3031", 82: "NGC 3034", 83: "NGC 5236",
    84: "NGC 4374", 85: "NGC 4382", 86: "NGC 4406", 87: "NGC 4486",
    88: "NGC 4501", 89: "NGC 4552", 90: "NGC 4569", 91: "NGC 4548",
    92: "NGC 6341", 93: "NGC 2447", 94: "NGC 4736", 95: "NGC 3351",
    96: "NGC 3368", 97: "NGC 3587", 98: "NGC 4192", 99: "NGC 4254",
    100: "NGC 4321", 101: "NGC 5457", 103: "NGC 581", 104: "NGC 4594",
    105: "NGC 3379", 106: "NGC 4258", 107: "NGC 6171", 108: "NGC 3556",
    109: "NGC 3992", 110: "NGC 205",
}

# words a common name may drop ("Andromeda" for "Andromeda Galaxy")
_GENERIC_WORDS = ("nebula", "galaxy", "galaxies", "cluster")
_ALIAS_CACHE: dict = {}


def _cross_id_keys(key: str) -> set[str]:
    """Messier <-> NGC/IC keys for one target_key ("m31" -> "ngc224",
    "messier31"; "ngc224" -> "m31", "messier31")."""
    import re
    from photonscript.shared.target_names import target_key
    m = re.fullmatch(r"(?:m|messier)(\d{1,3})", key)
    if m:
        n = int(m.group(1))
        out = {f"m{n}", f"messier{n}"}
        if n in MESSIER_NGC:
            out.add(target_key(MESSIER_NGC[n]))
        return out
    if "ngc" not in _ALIAS_CACHE:
        _ALIAS_CACHE["ngc"] = {target_key(v): k for k, v in MESSIER_NGC.items()}
    n = _ALIAS_CACHE["ngc"].get(key)
    return {f"m{n}", f"messier{n}"} if n else set()


def _derived_names(e: dict) -> set[str]:
    """Looser spellings of a row's common name: without a parenthetical,
    without a leading "The" or catalog id, and without the generic last
    word ("Andromeda Galaxy" -> "Andromeda")."""
    import re
    from photonscript.shared.target_names import target_key
    out = set()
    name = str(e.get("name") or "").strip()
    base = re.sub(r"\s*\(.*?\)\s*", " ", name).strip()
    cid = str(e.get("catalog_id") or "").strip()
    for cand in {name, base}:
        if cid and cand.lower().startswith(cid.lower() + " "):
            cand = cand[len(cid):].strip()
        if cand.lower().startswith("the "):
            cand = cand[4:].strip()
        out.add(cand)
        words = cand.split()
        if len(words) > 1 and words[-1].lower() in _GENERIC_WORDS:
            out.add(" ".join(words[:-1]))
    return {target_key(n) for n in out} - {""}


def _alias_index() -> dict:
    """{target_key: frozenset of every key of that object}; built from
    catalog_entries() once per user-catalog change. A derived spelling
    (see _derived_names) is kept only when exactly one row has it and no
    row uses it as a real name, id or alias."""
    sig = (id(_USER_TARGETS), len(_USER_TARGETS))
    if _ALIAS_CACHE.get("sig") == sig:
        return _ALIAS_CACHE["idx"]
    entries = catalog_entries()
    direct = [_entry_keys(e) for e in entries]
    taken = set().union(*direct) if direct else set()
    derived = [_derived_names(e) - d for e, d in zip(entries, direct)]
    count: dict[str, int] = {}
    for ks in derived:
        for k in ks:
            count[k] = count.get(k, 0) + 1
    groups = []
    for d, extra in zip(direct, derived):
        g = set(d) | {k for k in extra if count[k] == 1 and k not in taken}
        for k in list(g):
            g |= _cross_id_keys(k)
        groups.append(g)
    idx: dict = {}
    for g in groups:  # built-in rows first: the first row to claim a key wins
        fg = frozenset(g)
        for k in g:
            idx.setdefault(k, fg)
    # one hop: an object listed twice (M 42 and NGC 1976 rows) is one group
    for k, g in list(idx.items()):
        merged = set(g)
        for k2 in g:
            merged |= idx.get(k2, frozenset())
        idx[k] = frozenset(merged)
    _ALIAS_CACHE.update(idx=idx, sig=sig)
    return idx


def catalog_alias_keys(name) -> frozenset:
    """PS-135: every target_key the catalog knows for the object `name`
    names (its catalog id, the Messier / NGC / IC cross id with and without
    a space, its common name and aliases, CATALOG_EXTRAS and the user
    catalog). "NGC 224", "M31", "m 31", "Messier 31" and "Andromeda" all give
    the M 31 set. Empty when the catalog does not know the name."""
    from photonscript.shared.target_names import target_key
    key = target_key(name)
    if not key:
        return frozenset()
    hit = _alias_index().get(key)
    if hit is None:
        cross = _cross_id_keys(key)
        if not cross:
            return frozenset()
        hit = frozenset(cross | {key})
    return hit


def entry_to_target(entry: dict) -> CelestialTarget:
    return CelestialTarget(
        name=entry["name"],
        catalog_id=entry.get("catalog_id", ""),
        ra_hours=entry["ra"],
        dec_degrees=entry["dec"],
        object_type=entry.get("type", ""),
        magnitude=entry.get("mag"),
        angular_size_arcmin=entry.get("size"),
        recommended_total_hours=entry.get("hours", 10),
    )


def get_seasonal_targets(month: int) -> list[CelestialTarget]:
    """Return curated targets appropriate for a given month (built-in list
    plus the PS-124 user catalog)."""
    return [entry_to_target(e) for e in catalog_entries()
            if month in (e.get("months") or [])]
