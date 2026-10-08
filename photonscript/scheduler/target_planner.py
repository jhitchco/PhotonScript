"""Target planning engine — selects optimal targets for a given night.

Considers: seasonal visibility, project completion, priority, and
produces a time-ordered imaging plan that maximizes telescope utilization.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

from photonscript.shared.models import (
    CelestialTarget, ExposurePlan, FilterType, ImagingProject,
    NinaSequenceTarget, TargetTier,
)
from photonscript.shared.astronomy import (
    compute_visibility_window, get_seasonal_targets, rank_targets_for_night,
    get_twilight_times,
)
from photonscript.shared.config import PhotonScriptConfig

logger = logging.getLogger(__name__)


# Default narrowband exposure plan for emission nebulae
NARROWBAND_PLAN = [
    ExposurePlan(filter_type=FilterType.HA, exposure_seconds=300, count=40, gain=200, offset=50),
    ExposurePlan(filter_type=FilterType.OIII, exposure_seconds=300, count=30, gain=200, offset=50),
    ExposurePlan(filter_type=FilterType.SII, exposure_seconds=300, count=30, gain=200, offset=50),
]

# Default broadband exposure plan for galaxies / clusters
BROADBAND_PLAN = [
    ExposurePlan(filter_type=FilterType.LUMINANCE, exposure_seconds=180, count=60, gain=200, offset=50),
    ExposurePlan(filter_type=FilterType.RED, exposure_seconds=180, count=20, gain=200, offset=50),
    ExposurePlan(filter_type=FilterType.GREEN, exposure_seconds=180, count=20, gain=200, offset=50),
    ExposurePlan(filter_type=FilterType.BLUE, exposure_seconds=180, count=20, gain=200, offset=50),
]


def suggest_exposure_plan(target: CelestialTarget) -> list[ExposurePlan]:
    """Suggest a default exposure plan based on the target type."""
    obj_type = target.object_type.lower()
    if any(kw in obj_type for kw in ["nebula", "remnant", "emission", "planetary"]):
        return [p.model_copy() for p in NARROWBAND_PLAN]
    else:
        return [p.model_copy() for p in BROADBAND_PLAN]


def create_project_from_target(target: CelestialTarget) -> ImagingProject:
    """Create a new imaging project from a target with suggested exposures."""
    plans = suggest_exposure_plan(target)
    total_secs = sum(p.exposure_seconds * p.count for p in plans)
    return ImagingProject(
        id=str(uuid4()),
        target=target,
        exposure_plans=plans,
        priority=50,
        total_integration_hours=round(total_secs / 3600, 1),
    )


_NB_FILTERS = {"Ha", "OIII", "SII"}
# On a dark (moonless) night, cap broadband at this share of the night so it
# is protected from the uniform time-scaling that otherwise crushes a
# minority filter set to a single sub — but doesn't hog the whole night.
BB_SHARE_DARK = 0.5


def _scale_group(group, budget_s):
    """Scale a filter group's long counts down to fit budget_s. Returns
    seconds used. PS-112: a plan whose long set is done (count 0, only HDR
    shorts owed) stays at 0; the max(1, ...) floor is only for a set that
    still owes subs."""
    total = sum(e.exposure_seconds * e.count for e in group)
    if total <= budget_s or total == 0:
        return total
    scale = budget_s / total
    for e in group:
        if e.count > 0:
            e.count = max(1, int(e.count * scale))
    return sum(e.exposure_seconds * e.count for e in group)


def owed_seconds(e) -> float:
    """PS-112: seconds a per-night copy still owes, long set plus HDR shorts
    (the copy's acquired fields are 0, see remaining_copy)."""
    return (e.exposure_seconds * max(0, e.count - e.acquired)
            + (e.hdr_short_seconds or 0) * e.short_remaining())


def _fit_by_moon(exposures, available_seconds, moon_tag):
    """Fit a target's remaining exposures into tonight's time, weighting
    broadband vs narrowband by the night's moon tag (from scheduler/moon.py):

      "BB"       dark / moonless -> protect a broadband set (BB_SHARE_DARK of
                 the night), narrowband fills the rest
      "NB"       bright moon     -> narrowband only; broadband deferred to a
                 darker night (returns [] for a broadband-only target)
      "NB+OIII"  intermediate    -> narrowband priority, broadband fills leftover
      None       moon-aware off  -> legacy uniform scale-down

    Returns the exposures to shoot tonight (order is cosmetic; the sequence
    generator re-sorts broadband-first by the live moon window).
    """
    bb = [e for e in exposures if e.filter_type.value not in _NB_FILTERS]
    nb = [e for e in exposures if e.filter_type.value in _NB_FILTERS]

    if moon_tag == "NB":
        _scale_group(nb, available_seconds)
        return nb
    if moon_tag == "BB":
        bb_used = _scale_group(bb, available_seconds * BB_SHARE_DARK)
        _scale_group(nb, max(0.0, available_seconds - bb_used))
        return bb + nb
    if moon_tag == "NB+OIII":
        nb_used = _scale_group(nb, available_seconds)
        _scale_group(bb, max(0.0, available_seconds - nb_used))
        return nb + bb
    # moon-aware disabled / unknown tag: preserve the legacy uniform behavior
    _scale_group(exposures, available_seconds)
    return exposures


def remaining_copy(plan: ExposurePlan) -> ExposurePlan:
    """PS-105: tonight's copy of a plan carries only what is still owed.

    count = long subs still owed, hdr_short_count = short subs still owed,
    and every acquired field is reset to 0, so the planner is the ONE owner
    of "remaining": the sequence generator's `count - acquired`, the plan
    snapshot, the night plan text and cap_unguided all read the copy as is.
    The old copy kept `acquired`, so the generator subtracted it a second
    time: 30 planned with 10 accepted shot 10, and a project at least half
    done dropped out of the night."""
    return plan.model_copy(update={
        "count": max(0, plan.count - plan.acquired),
        "acquired": 0,
        "acquired_s": 0.0,
        "hdr_short_count": plan.short_remaining(),
        "hdr_short_acquired": 0,
    })


def piggy_hold_exposure(proj: ImagingProject) -> Optional[ExposurePlan]:
    """PS-134: a goal the Piggy-600 drives is planned while EITHER rig owes
    time. The Piggy companion shoots OSC on whatever the mount points at, so
    once the RC16 plan is done the RC16 keeps the pointing with extra subs of
    its largest plan (bonus core data), sized to the OSC seconds still owed.
    None when the RC16 drives, the OSC owes nothing, or the goal has no RC16
    plan (a Piggy-only goal stays off the RC16 sequence, as before)."""
    import math
    if (getattr(proj, "driving_rig", "rc16") or "rc16") == "rc16":
        return None
    rc16 = [e for e in proj.exposure_plans
            if (getattr(e, "rig", "rc16") or "rc16") == "rc16"]
    owed_s = sum(max(0.0, e.count * e.exposure_seconds - e.long_seconds_done())
                 for e in proj.exposure_plans
                 if (getattr(e, "rig", "rc16") or "rc16") != "rc16")
    if not rc16 or owed_s <= 0:
        return None
    base = max(rc16, key=lambda e: e.count * e.exposure_seconds)
    if not base.exposure_seconds or base.exposure_seconds <= 0:
        return None
    return base.model_copy(update={
        "count": max(1, math.ceil(owed_s / base.exposure_seconds - 1e-9)),
        "acquired": 0, "acquired_s": 0.0, "hdr_short_seconds": None,
        "hdr_short_count": 0, "hdr_short_acquired": 0})


def cap_unguided(targets, cap_s) -> list[str]:
    """PS-66: cap sub length on every UNGUIDED target (start_guiding False) at
    cap_s seconds (config unguided_max_exposure_s; 0 or None = off), keeping
    the same integration: a long set over the cap becomes cap_s x
    ceil(owed * old / cap_s). HDR short sets under the cap are untouched.
    Guided targets, focus-calibration and tracking-test targets are left
    alone. Edits the targets' exposure lists in place (they are per-night
    copies from plan_night_sequence) and returns one note per capped set."""
    import math
    try:
        cap = float(cap_s or 0)
    except (TypeError, ValueError):
        cap = 0.0
    notes: list[str] = []
    if cap <= 0:
        return notes
    for t in targets or []:
        if (getattr(t, "start_guiding", False) or getattr(t, "focus_calibration", False)
                or getattr(t, "tracking_test", False)):
            continue
        capped = []
        for e in t.exposures:
            upd = {}
            owed = e.count - e.acquired
            if e.exposure_seconds > cap and owed > 0:
                upd.update(exposure_seconds=cap, acquired=0, acquired_s=0.0,
                           count=math.ceil(owed * e.exposure_seconds / cap - 1e-9))
                notes.append(f"{t.name} {e.filter_type.value}: {owed}x"
                             f"{e.exposure_seconds:.0f}s -> {upd['count']}x{cap:.0f}s")
            short_owed = e.short_remaining()
            if e.hdr_short_seconds and e.hdr_short_seconds > cap and short_owed > 0:
                upd.update(hdr_short_seconds=cap, hdr_short_acquired=0,
                           hdr_short_count=math.ceil(
                               short_owed * e.hdr_short_seconds / cap - 1e-9))
            capped.append(e.model_copy(update=upd) if upd else e)
        t.exposures = capped
    if notes:
        logger.info("Unguided cap %.0fs: %s", cap, "; ".join(notes))
    return notes


def meridian_safe_order(pairs, dark_start, guard_min: int = 20):
    """Transit-order targets west->east, but push any target crossing the
    meridian within `guard_min` minutes of dark-start PAST the meridian so the
    run doesn't open on an immediate flip + recenter failure (the 2026-07-07
    dead-night). `pairs` = [(transit_time | datetime.max, target), ...].
    Returns (ordered_targets, deferred_names)."""
    from datetime import timedelta
    guard = timedelta(minutes=int(guard_min))
    deferred: list[str] = []

    def _key(item):
        transit, tgt = item
        if (isinstance(transit, datetime) and dark_start is not None
                and dark_start - guard <= transit <= dark_start + guard):
            deferred.append(getattr(tgt, "name", None))
            return transit + guard * 2
        return transit

    ordered = [t for _, t in sorted(pairs, key=_key)]
    return ordered, deferred


def allocate_windows(entries: list[dict], start: datetime, end: datetime,
                     min_window_s: float = 1800.0) -> dict:
    """PS-179: split tonight's dark time into one window per target.

    entries, in run order (transit order), each {"name", "up_from",
    "up_until" (datetimes: when the target is usable, above the altitude
    floor and, for a broadband-only target, before moonrise), "priority",
    "need_s" (goal seconds still owed, overhead included)}.

    Rule (Needs decision): walk the targets in run order from `start`. Each
    gets a priority-weighted share of the time left, among itself and the
    later targets that can still use some of it, capped by what its goal
    still needs and by its own usable time, never shorter than min_window_s.
    A window is stretched to the next target's rise so the mount never sits
    idle between two targets. A target that cannot get min_window_s gets no
    window. Leftover time at the end of the night goes to the
    highest-priority target still up: the last target's window just grows,
    any other target gets a final fill window.

    Returns {"windows": {name: (start, end)}, "handoff": {name: datetime}
    (the window end of every windowed target except the one that keeps the
    mount to the end of the night), "fill": None or (name, start, end),
    "dropped": [names]}. Windows never overlap and lie inside [start, end]."""
    min_win = timedelta(seconds=max(60.0, float(min_window_s or 0)))
    rows = []
    for e in entries:
        lo = max(start, e.get("up_from") or start)
        hi = min(end, e.get("up_until") or end)
        rows.append(dict(e, lo=lo, hi=hi,
                         w=max(1.0, float(e.get("priority") or 0)),
                         need=timedelta(seconds=max(0.0, float(
                             e.get("need_s") or 0)))))

    def usable_from(r, t):
        return r["hi"] - max(t, r["lo"]) >= min_win

    windows: dict = {}
    order: list[str] = []
    dropped: list[str] = []
    cursor = start
    for i, r in enumerate(rows):
        s = max(cursor, r["lo"])
        if r["hi"] - s < min_win:
            dropped.append(r["name"])
            continue
        rest = [x for x in rows[i + 1:] if usable_from(x, s)]
        share = (end - s) * (r["w"] / (r["w"] + sum(x["w"] for x in rest)))
        e_end = s + max(min_win, min(share, r["need"]))
        if rest and rest[0]["lo"] > e_end:
            e_end = rest[0]["lo"]   # no idle gap before the next one rises
        e_end = min(e_end, r["hi"])
        windows[r["name"]] = (s, e_end)
        order.append(r["name"])
        cursor = e_end

    fill = None
    if order and end - cursor >= min_win:
        rank = {r["name"]: (-r["w"], k) for k, r in enumerate(rows)}
        cands = sorted((r for r in rows if r["name"] in windows
                        and usable_from(r, cursor)),
                       key=lambda r: rank[r["name"]])
        if cands:
            c = cands[0]
            if c["name"] == order[-1]:
                windows[c["name"]] = (windows[c["name"]][0], c["hi"])
            else:
                fill = (c["name"], max(cursor, c["lo"]), c["hi"])
    handoff = {n: windows[n][1] for n in order
               if fill is not None or n != order[-1]}
    return {"windows": windows, "handoff": handoff, "fill": fill,
            "dropped": dropped}


def _apply_windows(sequence_targets, vis_by_name: dict, need_by_name: dict,
                   prio_by_name: dict, config, dark_start: datetime,
                   dark_end: datetime, now: datetime, moon: dict | None):
    """PS-179: allocate_windows over tonight's ordered targets, put the
    windows on them, drop the targets that get none, and fit each target's
    per-pass counts into its window (filter balance: every filter of a pass
    is shot inside the window; the loop repeats the pass until the
    handoff). Returns the targets that keep a window."""
    start = max(dark_start, now)
    moon = moon or {}
    rise = moon.get("rise_utc") if moon.get("down_at_dusk") else None
    entries = []
    for t in sequence_targets:
        vis = vis_by_name.get(t.name) or {}
        up_until = vis.get("set_time")
        if up_until is not None:
            up_until = up_until + timedelta(minutes=10)   # 10-min samples
        bb_only = all(e.filter_type.value not in _NB_FILTERS
                      for e in t.exposures)
        if bb_only and rise is not None:
            # broadband before moonrise (the generator ends it there too)
            up_until = min(up_until or dark_end, rise)
        entries.append({"name": t.name, "up_from": vis.get("rise_time"),
                        "up_until": up_until,
                        "priority": prio_by_name.get(t.name, 50),
                        "need_s": need_by_name.get(t.name, 0.0)})
    alloc = allocate_windows(
        entries, start, dark_end,
        60.0 * float(getattr(config, "plan_window_min_minutes", 30) or 30))
    kept = []
    for t in sequence_targets:
        win = alloc["windows"].get(t.name)
        if win is None:
            logger.info("Target windows: %s gets no window tonight (less "
                        "than the minimum usable time left)", t.name)
            continue
        t.window_start_utc, t.window_end_utc = win
        t.handoff_utc = alloc["handoff"].get(t.name)
        span_s = (win[1] - win[0]).total_seconds()
        if alloc["fill"] and alloc["fill"][0] == t.name:
            t.fill_from_utc, t.fill_end_utc = alloc["fill"][1:]
        if getattr(t, "repeat_while_up", True):
            # a run-once mosaic panel (PS-111) keeps its owed subs: the
            # handoff still bounds it
            longs = [e for e in t.exposures if e.count - e.acquired > 0]
            _scale_group(longs, span_s * 0.85)
        kept.append(t)
    logger.info("Target windows: %s%s", "; ".join(
        f"{t.name} {t.window_start_utc:%H:%M}-{t.window_end_utc:%H:%M}Z"
        + (" then hand off" if t.handoff_utc else "") for t in kept),
        (f"; fill {alloc['fill'][0]} from {alloc['fill'][1]:%H:%M}Z"
         if alloc["fill"] else ""))
    return kept


def _panel_note(proj, held: list) -> str:
    m = proj.mosaic
    note = (f"Mosaic {m.get('name')}: panel {m.get('panel')} of {m.get('of')} "
            f"(row {m.get('row')}, col {m.get('col')}), finished in order. "
            "The RC16 centers on this panel; the Piggy-600 rides along, its "
            "frame shifted by the panel offset from the mosaic center.")
    if held:
        note += " Held for a later night: " + ", ".join(
            p.target.name for p in held) + "."
    return note


def _mosaic_gate(visible_projects: list) -> list:
    """PS-111: per mosaic, keep the first unfinished panels (capture order)
    whose owed RC16 hours fill what the night gives the mosaic (its first
    panel's visible hours x 0.85, the planner's overhead factor), hold the
    rest. The admitted panels are kept together at the position of the
    first one; each carries the held names and the shared transit time."""
    from photonscript.scheduler.mosaic import mosaic_of, tonight_panels
    groups: dict[str, list] = {}
    for vp in visible_projects:
        m = mosaic_of(vp["project"])
        if m:
            groups.setdefault(m["id"], []).append(vp)
    if not groups:
        return visible_projects
    keep: dict[str, list] = {}
    for mid, vps in groups.items():
        vps.sort(key=lambda vp: int(mosaic_of(vp["project"]).get("panel") or 0))
        usable = vps[0]["visibility"]["hours"] * 0.85
        admitted, held = tonight_panels([vp["project"] for vp in vps], usable)
        ids = {id(p) for p in admitted}
        kept = [vp for vp in vps if id(vp["project"]) in ids]
        transit = kept[0]["visibility"].get("transit_time") if kept else None
        for vp in kept:
            vp["held"] = held
            vp["mosaic_transit"] = transit
        keep[mid] = kept
        if held:
            logger.info("Mosaic %s: tonight %s; held for later nights (in "
                        "order): %s", mosaic_of(vps[0]["project"]).get("name"),
                        ", ".join(p.target.name for p in admitted),
                        ", ".join(p.target.name for p in held))
    out, placed = [], set()
    for vp in visible_projects:
        m = mosaic_of(vp["project"])
        if not m:
            out.append(vp)
        elif m["id"] not in placed:
            placed.add(m["id"])
            out.extend(keep[m["id"]])
    return out


def plan_night_sequence(
    projects: list[ImagingProject],
    config: PhotonScriptConfig,
    date_utc: Optional[datetime] = None,
) -> list[NinaSequenceTarget]:
    """Build an ordered list of NINA sequence targets for tonight.

    Strategy:
    1. Compute visibility window for each active project's target
    2. Filter to targets visible tonight (> 30° altitude)
    3. Order by transit time so the scope moves west-to-east through the night
    4. Allocate exposures proportionally to remaining needs
    """
    if date_utc is None:
        date_utc = datetime.utcnow()

    obs = config.get_observatory()
    # Anchor to the LOCAL evening date — after ~6 PM local the UTC date has
    # already rolled over and a UTC anchor plans TOMORROW's night (the same
    # bug fixed in night_plan; this was the last copy).
    from photonscript.shared.localtime import utc_offset_hours as _tz_off
    _local = date_utc + timedelta(hours=_tz_off(config, date_utc))
    _base = datetime(_local.year, _local.month, _local.day)
    twilight = get_twilight_times(obs, _base)
    if twilight.get("astro_dark_end") and twilight["astro_dark_end"] < date_utc:
        twilight = get_twilight_times(obs, _base + timedelta(days=1))
    dark_start = twilight.get("astro_dark_start")
    dark_end = twilight.get("astro_dark_end")

    if not dark_start or not dark_end:
        logger.warning("Could not compute darkness window for %s", date_utc.date())
        return []

    dark_hours = (dark_end - dark_start).total_seconds() / 3600
    logger.info("Dark window: %s to %s (%.1f hours)", dark_start, dark_end, dark_hours)

    # Moon tag for the whole night (BB=dark, NB=bright, NB+OIII=intermediate).
    moon_tag = None
    if getattr(config, "moon_aware_planning", True):
        try:
            from photonscript.scheduler.moon import night_moon
            moon_tag = night_moon(config, dark_start.strftime("%Y-%m-%d"),
                                  dark_start, dark_end).get("tag")
            logger.info("Moon-aware planning: night tag = %s", moon_tag)
        except Exception as e:  # noqa: BLE001
            logger.warning("moon-aware planning unavailable, using uniform "
                           "scale: %s", e)
            moon_tag = None

    # Compute visibility for each project
    visible_projects = []
    for project in projects:
        if not project.active:
            continue
        project.compute_completion()
        if project.completion_pct >= 100:
            continue

        # PS-179: tonight's twilight, so rise/set (the target windows) are
        # tonight's even after the UTC date rolled over in the evening
        vis = compute_visibility_window(project.target, obs, date_utc,
                                        twilight=twilight)
        if not vis["visible"] or vis["hours"] < 0.5:
            continue

        visible_projects.append({
            "project": project,
            "visibility": vis,
        })

    if not visible_projects:
        logger.info("No targets visible tonight, checking seasonal catalog")
        # Fall back to seasonal suggestions
        month = date_utc.month
        seasonal = get_seasonal_targets(month)
        ranked = rank_targets_for_night(seasonal, obs, date_utc)
        for r in ranked[:5]:
            proj = create_project_from_target(r["target"])
            vis = compute_visibility_window(proj.target, obs, date_utc)
            if vis["visible"] and vis["hours"] >= 0.5:
                visible_projects.append({"project": proj, "visibility": vis})

    # PS-111: mosaic panels are finished in order; only the first unfinished
    # panels that fill the mosaic's visible time come along tonight.
    visible_projects = _mosaic_gate(visible_projects)

    # Select by PRIORITY (highest wins scarce dark time); the chosen set is
    # transit-ordered at the end to minimize slewing.
    visible_projects.sort(key=lambda vp: -vp["project"].priority)

    # Build sequence targets
    sequence_targets = []
    remaining_hours = dark_hours
    # PS-179: per target, for the window allocation
    vis_by_name, need_by_name, prio_by_name = {}, {}, {}
    _gen_moon = None  # generator's moon window, fetched only if needed

    for vp in visible_projects:
        if remaining_hours <= 0.3:
            break

        proj: ImagingProject = vp["project"]
        vis_hours = min(vp["visibility"]["hours"], remaining_hours)

        # Figure out how many exposures we can fit
        remaining_exposures = []
        for plan in proj.exposure_plans:
            if getattr(plan, "rig", "rc16") != "rc16":
                continue  # PS-30: a Piggy-600 OSC plan is never an RC16 filter
            # PS-112: keep a plan while EITHER set is owed; an HDR plan whose
            # long set is done still has its short subs to shoot
            if plan.count - plan.acquired > 0 or plan.short_remaining() > 0:
                remaining_exposures.append(remaining_copy(plan))
        if not remaining_exposures:
            # PS-134: the Piggy-600 drives and still owes time
            hold = piggy_hold_exposure(proj)
            if hold is not None:
                remaining_exposures.append(hold)

        if not remaining_exposures:
            continue
        # PS-179: what the goal still needs (before tonight's scale-down)
        need_s = sum(owed_seconds(e) for e in remaining_exposures) * 1.15

        # Fit into tonight's time, weighting broadband/narrowband by the moon.
        available_seconds = vis_hours * 3600 * 0.85  # 15% overhead for slewing/dithering
        remaining_exposures = _fit_by_moon(
            remaining_exposures, available_seconds, moon_tag)
        if not remaining_exposures:
            # e.g. a broadband-only target on a bright-moon night -> skip tonight
            continue
        # Same moon rule as the sequence generator (moon.broadband_deferred):
        # a broadband-only target the generator would empty is dropped here, so
        # the plan and the sequence agree (PS-27).
        if (getattr(config, "moon_aware_planning", True)
                and all(e.filter_type.value not in _NB_FILTERS
                        for e in remaining_exposures)):
            if _gen_moon is None:
                try:
                    from photonscript.scheduler.moon import moon_window_tonight
                    _gen_moon = moon_window_tonight(config)
                except Exception as e:  # noqa: BLE001
                    logger.warning("moon window unavailable: %s", e)
                    _gen_moon = {}
            if _gen_moon.get("available"):
                from photonscript.scheduler.moon import broadband_deferred
                if broadband_deferred(_gen_moon):
                    logger.info("Skipping %s tonight: broadband-only and the "
                                "moon defers broadband", proj.target.name)
                    continue

        alloc_time = sum(owed_seconds(e) for e in remaining_exposures) / 3600
        remaining_hours -= alloc_time * 1.15  # account for overhead

        seq_target = NinaSequenceTarget(
            name=proj.target.name,
            ra_hours=proj.target.ra_hours,
            dec_degrees=proj.target.dec_degrees,
            exposures=remaining_exposures,
            dither_every_n=5,
            auto_focus_interval_minutes=60,
            camera_temp_c=config.camera_setpoint_c,
            # PS-26: a Piggy-600-driven target centers for the 600 mm frame
            driving_rig=getattr(proj, "driving_rig", "rc16") or "rc16",
            frame_center_ra_hours=getattr(proj, "frame_center_ra_hours", None),
            frame_center_dec_degrees=getattr(proj, "frame_center_dec_degrees",
                                             None),
            transit_utc=vp["visibility"].get("transit_time"),
        )
        transit = vp["visibility"].get("transit_time") or datetime.max
        m = getattr(proj, "mosaic", None)
        if m and m.get("id"):
            # PS-111: the panels of one mosaic stay together, in capture
            # order, at the transit of the mosaic's first panel tonight
            seq_target.mosaic_id = m["id"]
            seq_target.mosaic_note = _panel_note(proj, vp.get("held") or [])
            transit = vp.get("mosaic_transit") or transit
            if transit != datetime.max:
                transit = transit + timedelta(seconds=int(m.get("panel") or 0))
        sequence_targets.append((transit, seq_target))
        vis_by_name[seq_target.name] = vp["visibility"]
        need_by_name[seq_target.name] = need_s
        prio_by_name[seq_target.name] = proj.priority

    # Transit-order the selected targets (west-to-east through the night), with
    # a meridian guard so the run doesn't open on an immediate flip (see helper).
    sequence_targets, _deferred = meridian_safe_order(
        sequence_targets, dark_start,
        int(getattr(config, "meridian_guard_min", 20)))
    if _deferred:
        logger.info("Meridian guard: deferred %s past the meridian so the run "
                    "doesn't open on an immediate flip", ", ".join(_deferred))
    # PS-111: every panel but a mosaic's last one tonight shoots its owed
    # subs once and hands the mount to the next panel
    last = {t.mosaic_id: i for i, t in enumerate(sequence_targets)
            if t.mosaic_id}
    for i, t in enumerate(sequence_targets):
        if t.mosaic_id and last[t.mosaic_id] != i:
            t.repeat_while_up = False

    # PS-179: each target gets a time window and hands the mount on at its
    # end, so the first repeating target no longer holds the RC16 all night
    # (2026-10-07: 281 M31 subs, NGC 604 and the Heart never ran)
    if sequence_targets and bool(getattr(config, "plan_target_windows", True)):
        moon = None
        if getattr(config, "moon_aware_planning", True):
            # the generator's own moon window: the window cap and the
            # sequence's "until moonrise" conditions use the same moonrise
            try:
                from photonscript.scheduler.nina_sequence_json import (
                    _moon_window)
                moon = _moon_window()
            except Exception as e:  # noqa: BLE001
                logger.warning("moon window unavailable: %s", e)
                moon = {}
        sequence_targets = _apply_windows(
            sequence_targets, vis_by_name, need_by_name, prio_by_name, config,
            dark_start, dark_end, date_utc, moon)

    logger.info(
        "Night plan: %d targets, %.1f hours allocated",
        len(sequence_targets),
        dark_hours - remaining_hours,
    )
    return sequence_targets
