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


def build_night_plan(config, preconfig_lead_min: int | None = None) -> dict:
    """Compute tonight's timeline. All times UTC ISO + local strings."""
    from photonscript.shared.astronomy import (get_seasonal_targets,
                                               get_twilight_times)
    from photonscript.scheduler.target_planner import (
        create_project_from_target, plan_night_sequence)

    obs = config.get_observatory()
    now = datetime.utcnow()
    lead = preconfig_lead_min or getattr(config, "arm_preconfig_lead_min", 30)

    tw = get_twilight_times(obs, now.replace(hour=0, minute=0, second=0,
                                             microsecond=0))
    dusk, dawn = tw.get("astro_dark_start"), tw.get("astro_dark_end")
    if not dusk or not dawn:
        return {"error": "Could not compute darkness window"}
    if dawn < now:  # tonight's window already over; plan tomorrow
        tw = get_twilight_times(obs, (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0))
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

    return {
        "night_of": dusk.strftime("%Y-%m-%d"),
        "preconfig_utc": preconfig.isoformat() + "Z",
        "dusk_utc": dusk.isoformat() + "Z",
        "dawn_utc": dawn.isoformat() + "Z",
        "dark_hours": round(dark_hours, 1),
        "targets": target_names,
        "events": events,
    }
