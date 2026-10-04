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
    """Scale a filter group's counts down to fit budget_s. Returns seconds used."""
    total = sum(e.exposure_seconds * e.count for e in group)
    if total <= budget_s or total == 0:
        return total
    scale = budget_s / total
    for e in group:
        e.count = max(1, int(e.count * scale))
    return sum(e.exposure_seconds * e.count for e in group)


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

        vis = compute_visibility_window(project.target, obs, date_utc)
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

    # Select by PRIORITY (highest wins scarce dark time); the chosen set is
    # transit-ordered at the end to minimize slewing.
    visible_projects.sort(key=lambda vp: -vp["project"].priority)

    # Build sequence targets
    sequence_targets = []
    remaining_hours = dark_hours
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
            if plan.count - plan.acquired > 0:
                remaining_exposures.append(remaining_copy(plan))

        if not remaining_exposures:
            continue

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

        alloc_time = sum(e.exposure_seconds * e.count for e in remaining_exposures) / 3600
        remaining_hours -= alloc_time * 1.15  # account for overhead

        seq_target = NinaSequenceTarget(
            name=proj.target.name,
            ra_hours=proj.target.ra_hours,
            dec_degrees=proj.target.dec_degrees,
            exposures=remaining_exposures,
            dither_every_n=5,
            auto_focus_interval_minutes=60,
            camera_temp_c=config.camera_setpoint_c,
        )
        sequence_targets.append(
            (vp["visibility"].get("transit_time") or datetime.max, seq_target))

    # Transit-order the selected targets (west-to-east through the night), with
    # a meridian guard so the run doesn't open on an immediate flip (see helper).
    sequence_targets, _deferred = meridian_safe_order(
        sequence_targets, dark_start,
        int(getattr(config, "meridian_guard_min", 20)))
    if _deferred:
        logger.info("Meridian guard: deferred %s past the meridian so the run "
                    "doesn't open on an immediate flip", ", ".join(_deferred))

    logger.info(
        "Night plan: %d targets, %.1f hours allocated",
        len(sequence_targets),
        dark_hours - remaining_hours,
    )
    return sequence_targets
