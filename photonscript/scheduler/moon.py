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

_cache: dict[tuple, dict] = {}

# broadband_deferred(): a moon up at dusk defers broadband from this lit %
BROADBAND_DEFER_ILLUM_PCT = 20
# ... unless it sets within this many hours of astro dusk: a young moon low in
# the west at dusk (2026-10-14: 20% lit, alt 10 deg, set 1 h after dusk) left
# the whole dark night with 0 broadband targets
BROADBAND_MOONSET_GRACE_H = 1.5


def night_moon(config, date_str: str, dark_start: datetime,
               dark_end: datetime) -> dict:
    """Illumination %, moon-free dark hours, and a BB/NB tag for one night.
    Cached by the dark window itself: callers label nights differently (the
    forecast by local date, moon_window_tonight by the UTC date of dusk), and
    a date-only key served one night's moon for another (2026-10-12)."""
    key = (date_str, _minute(dark_start), _minute(dark_end))
    if key in _cache:
        return _cache[key]
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
    alt = get_body("moon", times, loc).transform_to(frame).alt.deg
    # geocentric moon for elongation: right for illumination, and avoids
    # astropy's noisy NonRotationTransformationWarning wall
    elong = get_sun(times).separation(get_body("moon", times)).deg
    illum = float((1 - np.cos(np.radians(np.median(elong)))) / 2 * 100)
    moon_free = float(np.mean(alt < 0.0) * hours)
    out = {"illum_pct": round(illum), "moon_free_h": round(moon_free, 1),
           "tag": moon_tag(illum, moon_free)}
    if len(_cache) > 64:
        _cache.clear()
    _cache[key] = out
    return out


def _minute(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M") if isinstance(dt, datetime) else str(dt)


def moon_tag(illum: float, moon_free_h: float) -> str:
    """Night tag: BB window (enough moonless dark time, or a faint moon all
    night), NB+OIII, or NB."""
    if illum < 20 or moon_free_h >= 2.5:
        return "BB"
    if illum < 45:
        return "NB+OIII"
    return "NB"


def moon_series(config, times) -> tuple:
    """PS-30: moon altitude (deg) at each time and illumination (%) at each
    time, vectorized (one transform each), for the campaign planner's
    10-minute slots. Geocentric elongation, like night_moon()."""
    import numpy as np
    from astropy.coordinates import AltAz, get_body, get_sun
    from astropy.time import Time
    from photonscript.shared.astronomy import get_earth_location

    t = times if isinstance(times, Time) else Time(list(times))
    loc = get_earth_location(config.get_observatory())
    alt = get_body("moon", t, loc).transform_to(
        AltAz(obstime=t, location=loc)).alt.deg
    elong = get_sun(t).separation(get_body("moon", t)).deg
    illum = (1 - np.cos(np.radians(elong))) / 2 * 100
    return np.asarray(alt), np.asarray(illum)


def moon_window_tonight(config, now: datetime | None = None) -> dict:
    """Moon geometry for tonight (the night in progress, or the coming one
    once its dawn has passed): is the moon down at dusk, its altitude then,
    when does it rise / set (local HH, MM), and tonight's illumination.
    2026-10-12: this anchored to the UTC date, so from 18:00 MDT on (the
    whole night) it described TOMORROW night's moon."""
    import numpy as np
    from astropy import units as u
    from astropy.coordinates import AltAz, get_body
    from astropy.time import Time
    from photonscript.shared.astronomy import (get_earth_location,
                                               get_twilight_times)
    from photonscript.shared.localtime import night_base_date, utc_offset_hours

    obs = config.get_observatory()
    now = now or datetime.utcnow()
    base = night_base_date(config, now)
    tw = get_twilight_times(obs, base)
    if tw.get("astro_dark_end") and tw["astro_dark_end"] < now:
        tw = get_twilight_times(obs, base + timedelta(days=1))
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
    set_utc = None
    if not down_at_dusk:
        for i in range(len(alt) - 1):
            if alt[i] >= 0 > alt[i + 1]:
                set_utc = dusk + (dawn - dusk) * (i + 1) / (len(alt) - 1)
                break
    off = utc_offset_hours(config, dusk)
    rise_local = ((rise_utc + timedelta(hours=off))
                  if rise_utc else None)
    set_local = (set_utc + timedelta(hours=off)) if set_utc else None
    return {"available": True, "down_at_dusk": down_at_dusk,
            "illum_pct": info.get("illum_pct"),
            "alt_at_dusk_deg": round(float(alt[0]), 1),
            "dusk_utc": dusk, "dawn_utc": dawn,
            "rise_local_hh": rise_local.hour if rise_local else None,
            "rise_local_mm": rise_local.minute if rise_local else None,
            # PS-180: the planner caps a broadband-only target's window here
            "rise_utc": rise_utc,
            "set_utc": set_utc,
            "set_local": set_local.strftime("%H:%M") if set_local else None}


def broadband_deferred(mw: dict) -> bool:
    """Single moon rule shared by the planner and the sequence generator.

    True when tonight's broadband (L/RGB/OSC) should be deferred: the moon is
    up at dusk and at least 20% lit. Unknown geometry (mw unavailable) defers,
    matching the generator's historical fallback. Keeping ONE rule stops the
    planner from keeping a broadband-only target that the generator then
    empties (the 2026-09-20/21 empty-Andromeda re-acquire loop, PS-27)."""
    if not mw or not mw.get("available"):
        return True
    if mw.get("down_at_dusk"):
        return False
    if sets_soon_after_dusk(mw):
        return False
    illum = mw.get("illum_pct")
    # 0% is a real value (new moon up at dusk), not "unknown" (PS-106).
    return True if illum is None else illum >= BROADBAND_DEFER_ILLUM_PCT


def sets_soon_after_dusk(mw: dict, grace_h: float = BROADBAND_MOONSET_GRACE_H) -> bool:
    """The moon is up at dusk but sets within grace_h of astro dusk: the rest
    of the night is moonless, so broadband is not deferred."""
    if not mw or mw.get("down_at_dusk"):
        return False
    set_utc, dusk = mw.get("set_utc"), mw.get("dusk_utc")
    if not set_utc or not dusk:
        return False
    return (set_utc - dusk).total_seconds() <= grace_h * 3600


def describe_moon(mw: dict) -> str:
    """One line for the planner log: tonight's illumination, the moon's
    altitude at dusk and its rise or set time, and the broadband verdict."""
    if not mw or not mw.get("available"):
        return "moon geometry unavailable (broadband deferred to be safe)"
    illum = mw.get("illum_pct")
    lit = "illumination unknown" if illum is None else f"{illum}% lit"
    alt = mw.get("alt_at_dusk_deg")
    alt_txt = "" if alt is None else f" (alt {alt:.0f} deg)"
    if mw.get("down_at_dusk"):
        where = "down at dusk" + alt_txt
        if mw.get("rise_local_hh") is not None:
            where += (f", rises {int(mw['rise_local_hh']):02d}:"
                      f"{int(mw.get('rise_local_mm') or 0):02d} local")
        else:
            where += ", no moonrise before dawn"
    else:
        where = "up at dusk" + alt_txt
        if mw.get("set_local"):
            where += f", sets {mw['set_local']} local"
    if broadband_deferred(mw):
        verdict = ("broadband deferred: the moon is up at dusk and at least "
                   f"{BROADBAND_DEFER_ILLUM_PCT}% lit")
    elif sets_soon_after_dusk(mw):
        verdict = (f"broadband OK: the moon sets within "
                   f"{BROADBAND_MOONSET_GRACE_H:g} h of dusk")
    else:
        verdict = "broadband OK"
    return f"moon {lit}, {where}: {verdict}"
