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
    twilight = get_twilight_times(obs, date_utc)
    dark_start = twilight.get("astro_dark_start")
    dark_end = twilight.get("astro_dark_end")

    if not dark_start or not dark_end:
        logger.warning("Could not compute darkness window for %s", date_utc.date())
        return []

    dark_hours = (dark_end - dark_start).total_seconds() / 3600
    logger.info("Dark window: %s to %s (%.1f hours)", dark_start, dark_end, dark_hours)

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

    # Sort by transit time to minimize slewing
    visible_projects.sort(
        key=lambda vp: vp["visibility"].get("transit_time") or datetime.max
    )

    # Build sequence targets
    sequence_targets = []
    remaining_hours = dark_hours

    for vp in visible_projects:
        if remaining_hours <= 0.3:
            break

        proj: ImagingProject = vp["project"]
        vis_hours = min(vp["visibility"]["hours"], remaining_hours)

        # Figure out how many exposures we can fit
        remaining_exposures = []
        for plan in proj.exposure_plans:
            remaining = plan.count - plan.acquired
            if remaining > 0:
                remaining_exposures.append(plan.model_copy(update={"count": remaining}))

        if not remaining_exposures:
            continue

        # Scale down if we don't have enough time
        total_exp_time = sum(e.exposure_seconds * e.count for e in remaining_exposures)
        available_seconds = vis_hours * 3600 * 0.85  # 15% overhead for slewing/dithering
        if total_exp_time > available_seconds:
            scale = available_seconds / total_exp_time
            for e in remaining_exposures:
                e.count = max(1, int(e.count * scale))

        alloc_time = sum(e.exposure_seconds * e.count for e in remaining_exposures) / 3600
        remaining_hours -= alloc_time * 1.15  # account for overhead

        seq_target = NinaSequenceTarget(
            name=proj.target.name,
            ra_hours=proj.target.ra_hours,
            dec_degrees=proj.target.dec_degrees,
            exposures=remaining_exposures,
            dither_every_n=3,
            auto_focus_interval_minutes=60,
        )
        sequence_targets.append(seq_target)

    logger.info(
        "Night plan: %d targets, %.1f hours allocated",
        len(sequence_targets),
        dark_hours - remaining_hours,
    )
    return sequence_targets
