"""Moon ephemeris for scheduling: illumination + moon-free dark hours.

The scheduling insight: hour-by-hour moon ALTITUDE matters more than
phase — even at full moon, the hours before moonrise are pristine for
broadband. "Moon-free dark hours" = astro-dark time with the moon below
the horizon.
"""

from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

_cache: dict[str, dict] = {}


def night_moon(config, date_str: str, dark_start: datetime,
               dark_end: datetime) -> dict:
    """Illumination %, moon-free dark hours, and a BB/NB tag for one night."""
    if date_str in _cache:
        return _cache[date_str]
    import numpy as np
    from astropy import units as u
    from astropy.coordinates import AltAz, get_body, get_sun
    from astropy.time import Time
    from photonscript.shared.astronomy import get_earth_location

    if not dark_start or not dark_end or dark_end <= dark_start:
        return {"illum_pct": None, "moon_free_h": None, "tag": "?"}
    hours = (dark_end - dark_start).total_seconds() / 3600
    times = Time(dark_start) + np.linspace(0, hours, 25) * u.hour
    loc = get_earth_location(config.get_observatory())
    frame = AltAz(obstime=times, location=loc)
    moon = get_body("moon", times, loc)
    alt = moon.transform_to(frame).alt.deg
    elong = get_sun(times).separation(moon).deg
    illum = float((1 - np.cos(np.radians(np.median(elong)))) / 2 * 100)
    moon_free = float(np.mean(alt < 0.0) * hours)
    # BB window: enough moonless dark time, or a faint moon all night
    if illum < 20 or moon_free >= 2.5:
        tag = "BB"
    elif illum < 45:
        tag = "NB+OIII"
    else:
        tag = "NB"
    out = {"illum_pct": round(illum), "moon_free_h": round(moon_free, 1),
           "tag": tag}
    if len(_cache) > 64:
        _cache.clear()
    _cache[date_str] = out
    return out
