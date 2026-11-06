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


def _owed_sets(t) -> list[dict]:
    """PS-112: one {f, exp, n} per set a target still owes tonight, the HDR
    short set as its own entry (short: True) ahead of its long set, the order
    the sequence shoots them. A plan whose long set is done shows only its
    shorts."""
    out = []
    for e in t.exposures:
        short_n = e.short_remaining()
        if short_n > 0:
            out.append({"f": e.filter_type.value,
                        "exp": round(e.hdr_short_seconds), "n": short_n,
                        "short": True})
        if e.count - e.acquired > 0:
            out.append({"f": e.filter_type.value,
                        "exp": round(e.exposure_seconds),
                        "n": e.count - e.acquired})
    return out


def _shutter_estimate(config, windows: list, dusk, dawn, cursor) -> dict | None:
    """PS-176: shutter_efficiency.estimate for tonight's plan, both AF
    policies, overheads from the last nights' timelines. The plan's
    windows, the last one carried on to dawn (the imaging loop repeats
    while the target is up). None on any error: an estimate never breaks
    the plan."""
    try:
        from photonscript.scheduler import shutter_efficiency as se
        from photonscript.scheduler.af_policy import policy, smart_spec
        from photonscript.scheduler.nina_sequence_json import _af_filter_type
        if windows and cursor < dawn:
            windows[-1][1] += (dawn - cursor).total_seconds()
        night = (dusk - timedelta(hours=12)).strftime("%Y-%m-%d")
        oh = se.measured_overheads(config, before=night)
        spec = smart_spec(config, _af_filter_type(config), force=True)
        return se.estimate([tuple(w) for w in windows],
                           (dawn - dusk).total_seconds(), oh, spec,
                           policy(config))
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("shutter estimate failed: %s", e)
        return None


def _window(t, dawn, fill: bool = False):
    """PS-180: (start, end) UTC of a target's planned window, or of its fill
    window; None when the planner set none (plan_target_windows off)."""
    if fill:
        f0 = getattr(t, "fill_from_utc", None)
        return (f0, getattr(t, "fill_end_utc", None) or dawn) if f0 else None
    w0 = getattr(t, "window_start_utc", None)
    w1 = getattr(t, "window_end_utc", None)
    return (w0, w1) if w0 and w1 else None


def _schedule_rows(config, obs, targets, dusk, dawn, now, _fmt) -> dict:
    """The plan's per-target schedule cards (window, sets owed, transit)
    for planner targets; also the windows for the shutter estimate and the
    planned subs / integration. Shared by build_night_plan and
    dispatched_view (the targets a dispatch actually loaded)."""
    from photonscript.shared.astronomy import compute_visibility_window
    from photonscript.scheduler.target_planner import owed_seconds
    schedule = []
    windows = []   # PS-176: (target, seconds) for the shutter estimate
    cursor2 = dusk
    planned_subs = 0
    integ_s = 0.0
    rows = [(t, False) for t in targets] + [
        (t, True) for t in targets
        if getattr(t, "fill_from_utc", None) is not None]   # PS-180
    for t, is_fill in rows:
        active = [e for e in t.exposures
                  if e.count - e.acquired > 0 or e.short_remaining() > 0]
        sets = _owed_sets(t)
        total_s = sum(owed_seconds(e) for e in active) * 1.15
        win = _window(t, dawn, fill=is_fill)
        if total_s <= 0 or (win is None and cursor2 >= dawn):
            continue
        if win is None:
            end = min(cursor2 + timedelta(seconds=total_s), dawn)
        else:
            cursor2, end = win   # PS-180: the planner's window
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
        carrier = bool(getattr(t, "carrier", False))
        if carrier:
            kind = "pointing carrier for the Piggy-600 (RC16 Ha, moon fill)"
        rah = int(t.ra_hours)
        ram = int((t.ra_hours - rah) * 60)
        dec_sign = "+" if t.dec_degrees >= 0 else "-"
        decd = int(abs(t.dec_degrees))
        decm = int((abs(t.dec_degrees) - decd) * 60)
        schedule.append({
            "name": t.name,
            "kind": kind + (", fill (rest of the night)" if is_fill else ""),
            "window_start": _fmt(cursor2)["local"],
            "window_end": _fmt(end)["local"],
            # PS-180: the window in UTC; handoff = the loop ends there and the
            # next target runs (None: it keeps the mount to the loop end)
            "window_start_utc": _fmt(cursor2)["utc"],
            "window_end_utc": _fmt(end)["utc"],
            "handoff_utc": (_fmt(t.handoff_utc)["utc"]
                            if getattr(t, "handoff_utc", None) and not is_fill
                            else None),
            "fill": is_fill,
            "carrier": carrier,
            "transit_local": _fmt(transit)["local"] if transit else None,
            "transit_alt": transit_alt,
            "coords": f"{rah:02d}h{ram:02d}m / {dec_sign}{decd:02d}d{decm:02d}m",
            "ra_hours": t.ra_hours,
            "dec_degrees": t.dec_degrees,
            "filters": sets,
        })
        if not is_fill:
            planned_subs += sum(x["n"] for x in sets)
            integ_s += sum(owed_seconds(e) for e in active)
        windows.append([t, (end - cursor2).total_seconds()])
        cursor2 = end

    return {"schedule": schedule, "windows": windows, "cursor": cursor2,
            "planned_subs": planned_subs, "integ_s": integ_s}


def dispatched_view(config, plan: dict, targets, now: datetime | None = None) -> dict:
    """2026-10-26: the plan's target view (targets, schedule, stats) rebuilt
    from planner targets: the ones a dispatch loaded, or a re-plan after a
    goal changed. The arm's plan is computed once at arm; a goal patched
    after it (or the dispatch's own planning) left the evening heads-up and
    "imaging now" on the arm's list (2026-10-25: "0 target(s) ... on ?"
    while the RC16 imaged Heart Nebula). Keys merged into plan by the
    caller; the other stats (dark hours, shutter %) are kept."""
    from photonscript.shared.localtime import utc_offset_hours as _tz_off
    now = now or datetime.utcnow()
    dusk = datetime.fromisoformat(str(plan["dusk_utc"]).rstrip("Z"))
    dawn = datetime.fromisoformat(str(plan["dawn_utc"]).rstrip("Z"))
    utc_off = _tz_off(config, dusk)

    def _fmt(dt):
        return {"utc": dt.isoformat() + "Z",
                "local": (dt + timedelta(hours=utc_off)).strftime("%I:%M %p")}

    sv = _schedule_rows(config, config.get_observatory(), list(targets or []),
                        dusk, dawn, now, _fmt)
    sched = sv["schedule"]
    stats = dict(plan.get("stats") or {})
    stats.update({"targets": sum(1 for r in sched if not r["fill"]),
                  "planned_subs": sv["planned_subs"],
                  "est_integration_h": round(sv["integ_s"] / 3600, 1)})
    return {"targets": [r["name"] for r in sched if not r["fill"]],
            "schedule": sched, "stats": stats}


def build_night_plan(config, preconfig_lead_min: int | None = None) -> dict:
    """Compute tonight's timeline. All times UTC ISO + local strings."""
    from photonscript.shared.astronomy import (get_seasonal_targets,
                                               get_twilight_times)
    from photonscript.scheduler.target_planner import (
        create_project_from_target, owed_seconds, plan_night_sequence)

    obs = config.get_observatory()
    now = datetime.utcnow()
    lead = preconfig_lead_min or getattr(config, "arm_preconfig_lead_min", 30)

    # Anchor to the LOCAL date: after ~6 PM local, UTC has already rolled to
    # tomorrow, and a UTC-date anchor plans the wrong night entirely.
    # 2026-10-13: noon to noon, so after local midnight it is still the
    # night that began yesterday evening (night_base_date).
    from photonscript.shared.localtime import night_base_date
    base = night_base_date(config, now)

    tw = get_twilight_times(obs, base)
    dusk, dawn = tw.get("astro_dark_start"), tw.get("astro_dark_end")
    if not dusk or not dawn:
        return {"error": "Could not compute darkness window"}
    if dawn < now:  # tonight's window already over; plan tomorrow
        tw = get_twilight_times(obs, base + timedelta(days=1))
        dusk, dawn = tw.get("astro_dark_start"), tw.get("astro_dark_end")

    preconfig = dusk - timedelta(minutes=lead)
    dark_hours = (dawn - dusk).total_seconds() / 3600

    # Planned targets with estimated windows (exposure + 15% overhead).
    # Use the goal store (priorities, budgets, 600s plans) — the seasonal
    # catalog is only a fallback when no goals exist. This was the source
    # of the stale-looking 300s Lagoon/Eagle plan: the card planned from
    # the catalog while dispatch planned from the store.
    projects = []
    try:
        from photonscript.scheduler.app import get_store
        projects = [p for p in get_store().projects.values() if p.active]
    except Exception:  # noqa: BLE001
        pass
    if not projects:
        seasonal = get_seasonal_targets(dusk.month)
        projects = [create_project_from_target(t) for t in seasonal]
    from photonscript.scheduler.target_planner import plan_with_info
    try:
        from photonscript.scheduler.app import armed_guided
        guided = armed_guided()
    except Exception:  # noqa: BLE001
        guided = None
    targets, plan_info = plan_with_info(plan_night_sequence, projects, config,
                                        now, guided=guided)

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
        # PS-112: HDR short subs count too (a plan can owe only shorts)
        total_s = sum(owed_seconds(e) for e in t.exposures) * 1.15
        if total_s <= 0:
            continue
        win = _window(t, dawn)
        if win is None:
            end = min(cursor + timedelta(seconds=total_s), dawn)
            if cursor >= dawn:
                break
        else:
            cursor, end = win   # PS-180: the planner's window
        events.append({
            "time": _fmt(cursor), "event": f"Target: {t.name}",
            "detail": ", ".join(
                f"{s['f']}×{s['n']}@{s['exp']}s" + (" HDR short" if s.get("short") else "")
                for s in _owed_sets(t)) +
            f" (~{(end - cursor).total_seconds() / 3600:.1f}h)"
            + (" per pass, repeats until the window ends"
               if win is not None else "")})
        target_names.append(t.name)
        cursor = end
    for t in targets:
        if getattr(t, "fill_from_utc", None) is not None:
            f0, f1 = t.fill_from_utc, t.fill_end_utc or dawn
            events.append({
                "time": _fmt(f0), "event": f"Fill: {t.name}",
                "detail": "rest of the night back to the highest-priority "
                          f"target still up (~{(f1 - f0).total_seconds() / 3600:.1f}h)"})

    events.append({"time": _fmt(dawn), "event": "Astro dark ends — shutdown",
                   "detail": "Warm camera, slew to park, stop tracking"})

    # --- Rich view: twilight timeline, stats, per-target schedule cards ----
    from photonscript.scheduler.project_store import target_kind  # noqa: F401

    night_date = (dusk - timedelta(hours=12)).replace(hour=0, minute=0,
                                                      second=0, microsecond=0)
    tw_all = compute_night_times(obs, night_date)
    twilight = {k: _fmt(v)["local"] if v else None for k, v in tw_all.items()}

    sv = _schedule_rows(config, obs, targets, dusk, dawn, now, _fmt)
    schedule, windows, cursor2 = sv["schedule"], sv["windows"], sv["cursor"]
    planned_subs, integ_s = sv["planned_subs"], sv["integ_s"]

    efficiency = _shutter_estimate(config, windows, dusk, dawn, cursor2)

    return {
        # NINA files the night under the LOCAL evening date — use the same
        "night_of": (dusk + timedelta(hours=utc_off)).strftime("%Y-%m-%d"),
        "preconfig_utc": preconfig.isoformat() + "Z",
        "dusk_utc": dusk.isoformat() + "Z",
        "dawn_utc": dawn.isoformat() + "Z",
        # PS-36: the armer times the dawn shutdown after the flat window
        # (nautical dawn + 5 + dawn_flats_window_min, capped at sunrise).
        "naut_dawn_utc": (tw_all["naut_dawn"].isoformat() + "Z"
                          if tw_all.get("naut_dawn") else None),
        "sunrise_utc": (tw_all["sunrise"].isoformat() + "Z"
                        if tw_all.get("sunrise") else None),
        "dark_hours": round(dark_hours, 1),
        # deep night (scheduler/deep_night.py): the dashboard timeline's
        # "Deep night: <target>" line
        "night_mode": plan_info.get("mode"),
        "night_mode_source": plan_info.get("mode_source"),
        "deep_pick": plan_info.get("deep_pick"),
        "deep_note": plan_info.get("deep_note"),
        # 2026-10-25: RC16 narrowband left out of an unguided night
        "rc16_nb_unguided": plan_info.get("rc16_nb_unguided") or [],
        "rc16_nb_note": plan_info.get("rc16_nb_note"),
        # 2026-10-25 moon fill (scheduler/moon_fill.py): why the night is
        # Piggy-driven and which goals carry the pointing
        "moon_fill": plan_info.get("moon_fill"),
        "moon_fill_note": plan_info.get("moon_fill_note"),
        "targets": target_names,
        "events": events,
        "twilight": twilight,
        "stats": {"dark_hours": round(dark_hours, 1),
                  "targets": sum(1 for r in schedule if not r["fill"]),
                  "planned_subs": planned_subs,
                  "est_integration_h": round(integ_s / 3600, 1),
                  # PS-176: predicted RC16 shutter-open % of the dark time
                  # under the configured AF policy
                  "est_shutter_pct": (efficiency["policies"][
                      efficiency["active_policy"]]["pct"]
                      if efficiency else None)},
        "efficiency": efficiency,
        "schedule": schedule,
    }
