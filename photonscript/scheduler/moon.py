"""Moon ephemeris for scheduling: illumination + moon-free dark hours.

The scheduling insight: hour-by-hour moon ALTITUDE matters more than
phase — even at full moon, the hours before moonrise are pristine for
broadband. "Moon-free dark hours" = astro-dark time with the moon below
the horizon.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

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


def moon_window_tonight(config) -> dict:
    """Moon geometry for the coming night: is the moon down at dusk, when
    does it rise (local HH, MM), and tonight's illumination."""
    import numpy as np
    from astropy import units as u
    from astropy.coordinates import AltAz, get_body
    from astropy.time import Time
    from photonscript.shared.astronomy import (get_earth_location,
                                               get_twilight_times)
    from photonscript.shared.localtime import utc_offset_hours

    obs = config.get_observatory()
    tw = get_twilight_times(obs, datetime.utcnow().replace(
        hour=0, minute=0, second=0, microsecond=0))
    dusk, dawn = tw.get("astro_dark_start"), tw.get("astro_dark_end")
    if not dusk or not dawn or dawn <= dusk:
        return {"available": False}
    hours = (dawn - dusk).total_seconds() / 3600
    times = Time(dusk) + np.linspace(0, hours, 60) * u.hour
    loc = get_earth_location(obs)
    alt = get_body("moon", times, loc).transform_to(
        AltAz(obstime=times, location=loc)).alt.deg
    info = night_moon(config, dusk.strftime("%Y-%m-%d"), dusk, dawn)
    down_at_dusk = bool(alt[0] < 0)
    rise_utc = None
    if down_at_dusk:
        for i in range(len(alt) - 1):
            if alt[i] < 0 <= alt[i + 1]:
                rise_utc = dusk + (dawn - dusk) * (i + 1) / (len(alt) - 1)
                break
    off = utc_offset_hours(config, dusk)
    rise_local = ((rise_utc + timedelta(hours=off))
                  if rise_utc else None)
    return {"available": True, "down_at_dusk": down_at_dusk,
            "illum_pct": info.get("illum_pct"),
            "rise_local_hh": rise_local.hour if rise_local else None,
            "rise_local_mm": rise_local.minute if rise_local else None}
