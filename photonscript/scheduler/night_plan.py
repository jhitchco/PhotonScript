"""Tonight's plan: full timeline of events from pre-config to dawn shutdown.

Maximizing-imaging-time rules encoded here:
  - Equipment pre-config (connect, cool, unpark) happens during twilight,
    finishing before astro dark — no dark minutes wasted on cooling.
  - The generated sequence carries a WaitForTime so imaging starts the moment
    astro dark begins.
  - Targets are ordered by transit (planner already does this) to minimize
    slews and meridian flips.
  - Shutdown (warm, park) begins at astro dark end — dawn flats can be added
    after, in twilight.
"""

from __future__ import annotations

from datetime import datetime, timedelta


def compute_night_times(obs, date_utc: datetime) -> dict:
    """All twilight crossings for one night: sunset, nautical/astro dusk,
    astro/nautical dawn, sunrise. Sampled at ~3.6 min resolution."""
    import numpy as np
    from astropy import units as u
    from astropy.coordinates import AltAz, get_sun
    from astropy.time import Time
    from photonscript.shared.astronomy import get_earth_location

    evening = Time(date_utc.replace(hour=23, minute=0, second=0, microsecond=0))
    times = Time(np.linspace(evening.jd, (evening + 15 * u.hour).jd, 250),
                 format="jd")
    frame = AltAz(obstime=times, location=get_earth_location(obs))
    alts = get_sun(times).transform_to(frame).alt.deg

    out = {}
    thresholds = {"sunset": -0.833, "naut_dusk": -12.0, "astro_dusk": -18.0}
    rising = {"astro_dawn": -18.0, "naut_dawn": -12.0, "sunrise": -0.833}
    for i in range(len(alts) - 1):
        for name, th in thresholds.items():
            if name not in out and alts[i] > th >= alts[i + 1]:
                out[name] = times[i + 1].datetime
        for name, th in rising.items():
            if name not in out and alts[i] <= th < alts[i + 1]:
                out[name] = times[i + 1].datetime
    return out


def build_night_plan(config, preconfig_lead_min: int | None = None) -> dict:
    """Compute tonight's timeline. All times UTC ISO + local strings."""
    from photonscript.shared.astronomy import (get_seasonal_targets,
                                               get_twilight_times)
    from photonscript.scheduler.target_planner import (
        create_project_from_target, plan_night_sequence)

    obs = config.get_observatory()
    now = datetime.utcnow()
    lead = preconfig_lead_min or getattr(config, "arm_preconfig_lead_min", 30)

    # Anchor to the LOCAL date: after ~6 PM local, UTC has already rolled to
    # tomorrow, and a UTC-date anchor plans the wrong night entirely.
    from photonscript.shared.localtime import utc_offset_hours as _tz_off
    local_now = now + timedelta(hours=_tz_off(config, now))
    base = datetime(local_now.year, local_now.month, local_now.day)

    tw = get_twilight_times(obs, base)
    dusk, dawn = tw.get("astro_dark_start"), tw.get("astro_dark_end")
    if not dusk or not dawn:
        return {"error": "Could not compute darkness window"}
    if dawn < now:  # tonight's window already over; plan tomorrow
        tw = get_twilight_times(obs, base + timedelta(days=1))
        dusk, dawn = tw.get("astro_dark_start"), tw.get("astro_dark_end")

    preconfig = dusk - timedelta(minutes=lead)
    dark_hours = (dawn - dusk).total_seconds() / 3600

    # Planned targets with estimated windows (exposure + 15% overhead)
    seasonal = get_seasonal_targets(dusk.month)
    projects = [create_project_from_target(t) for t in seasonal]
    targets = plan_night_sequence(projects, config, now)

    from photonscript.shared.localtime import utc_offset_hours as _tz_off
    utc_off = _tz_off(config, dusk)  # DST-aware (MDT -6 / MST -7)

    def _fmt(dt):
        return {"utc": dt.isoformat() + "Z",
                "local": (dt + timedelta(hours=utc_off)).strftime("%I:%M %p")}

    events = [
        {"time": _fmt(preconfig), "event": "Pre-config",
         "detail": f"Dispatch sequence to NINA and start: connect equipment, "
                   f"unpark, cool camera to {config.camera_setpoint_c}°C "
                   f"during twilight"},
        {"time": _fmt(dusk), "event": "Astro dark — imaging begins",
         "detail": "Sequence WaitForTime releases; slew to first target, "
                   "autofocus, plate solve, expose"},
    ]

    cursor = dusk
    target_names = []
    for t in targets:
        total_s = sum(e.exposure_seconds * (e.count - e.acquired)
                      for e in t.exposures) * 1.15
        if total_s <= 0:
            continue
        end = min(cursor + timedelta(seconds=total_s), dawn)
        if cursor >= dawn:
            break
        events.append({
            "time": _fmt(cursor), "event": f"Target: {t.name}",
            "detail": ", ".join(
                f"{e.filter_type.value}×{e.count - e.acquired}@"
                f"{e.exposure_seconds:.0f}s" for e in t.exposures
                if e.count - e.acquired > 0) +
            f" (~{(end - cursor).total_seconds() / 3600:.1f}h)"})
        target_names.append(t.name)
        cursor = end

    events.append({"time": _fmt(dawn), "event": "Astro dark ends — shutdown",
                   "detail": "Warm camera, slew to park, stop tracking"})

    # --- Rich view: twilight timeline, stats, per-target schedule cards ----
    from photonscript.shared.astronomy import compute_visibility_window
    from photonscript.scheduler.project_store import target_kind

    night_date = (dusk - timedelta(hours=12)).replace(hour=0, minute=0,
                                                      second=0, microsecond=0)
    tw_all = compute_night_times(obs, night_date)
    twilight = {k: _fmt(v)["local"] if v else None for k, v in tw_all.items()}

    schedule = []
    cursor2 = dusk
    planned_subs = 0
    integ_s = 0.0
    for t in targets:
        active = [e for e in t.exposures if e.count - e.acquired > 0]
        total_s = sum(e.exposure_seconds * (e.count - e.acquired)
                      for e in active) * 1.15
        if total_s <= 0 or cursor2 >= dawn:
            continue
        end = min(cursor2 + timedelta(seconds=total_s), dawn)
        # transit info
        from photonscript.shared.models import CelestialTarget
        ct = CelestialTarget(name=t.name, ra_hours=t.ra_hours,
                             dec_degrees=t.dec_degrees)
        vis = compute_visibility_window(ct, obs, now)
        transit = vis.get("transit_time")
        # Meridian transit altitude is analytic: 90 - |lat - dec|
        transit_alt = round(90 - abs(obs.latitude - t.dec_degrees))
        kind = "narrowband sho" if any(
            e.filter_type.value in ("Ha", "OIII", "SII") for e in active)             else "broadband lrgb"
        rah = int(t.ra_hours)
        ram = int((t.ra_hours - rah) * 60)
        dec_sign = "+" if t.dec_degrees >= 0 else "-"
        decd = int(abs(t.dec_degrees))
        decm = int((abs(t.dec_degrees) - decd) * 60)
        schedule.append({
            "name": t.name,
            "kind": kind,
            "window_start": _fmt(cursor2)["local"],
            "window_end": _fmt(end)["local"],
            "transit_local": _fmt(transit)["local"] if transit else None,
            "transit_alt": transit_alt,
            "coords": f"{rah:02d}h{ram:02d}m / {dec_sign}{decd:02d}d{decm:02d}m",
            "ra_hours": t.ra_hours,
            "dec_degrees": t.dec_degrees,
            "filters": [{"f": e.filter_type.value,
                         "exp": round(e.exposure_seconds),
                         "n": e.count - e.acquired} for e in active],
        })
        planned_subs += sum(e.count - e.acquired for e in active)
        integ_s += sum(e.exposure_seconds * (e.count - e.acquired)
                       for e in active)
        cursor2 = end

    return {
        "night_of": dusk.strftime("%Y-%m-%d"),
        "preconfig_utc": preconfig.isoformat() + "Z",
        "dusk_utc": dusk.isoformat() + "Z",
        "dawn_utc": dawn.isoformat() + "Z",
        "dark_hours": round(dark_hours, 1),
        "targets": target_names,
        "events": events,
        "twilight": twilight,
        "stats": {"dark_hours": round(dark_hours, 1),
                  "targets": len(schedule),
                  "planned_subs": planned_subs,
                  "est_integration_h": round(integ_s / 3600, 1)},
        "schedule": schedule,
    }
