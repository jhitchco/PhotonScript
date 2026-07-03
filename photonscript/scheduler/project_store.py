"""Persistent imaging-project store + time-budget filter allocation.

Projects live in <data_dir>/projects.json and survive restarts. Each project
carries a priority (0-100) and an hour budget; PhotonScript allocates the
budget across filters automatically based on the target type:

  narrowband (nebulae/remnants):  Ha 40% / OIII 30% / SII 30%  @ 300s
  broadband (galaxies/clusters):  L 50% / R 16.7% / G 16.7% / B 16.7% @ 180s

Changing the budget re-allocates counts while preserving acquired subs.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from photonscript.shared.models import (CelestialTarget, ExposurePlan,
                                        FilterType, ImagingProject)

logger = logging.getLogger(__name__)

NARROWBAND_MIX = [(FilterType.HA, 0.40, 300), (FilterType.OIII, 0.30, 300),
                  (FilterType.SII, 0.30, 300)]
BROADBAND_MIX = [(FilterType.LUMINANCE, 0.50, 180), (FilterType.RED, 1 / 6, 180),
                 (FilterType.GREEN, 1 / 6, 180), (FilterType.BLUE, 1 / 6, 180)]


def target_kind(target: CelestialTarget) -> str:
    obj = (target.object_type or "").lower()
    if any(kw in obj for kw in ("nebula", "remnant", "emission", "planetary")):
        return "narrowband"
    return "broadband"


def allocate_exposures(kind: str, budget_hours: float, config,
                       acquired: dict | None = None) -> list[ExposurePlan]:
    """Split an hour budget across filters. Preserves acquired counts."""
    mix = NARROWBAND_MIX if kind == "narrowband" else BROADBAND_MIX
    acquired = acquired or {}
    plans = []
    for ftype, frac, exp_s in mix:
        count = max(1, round(budget_hours * 3600 * frac / exp_s))
        plans.append(ExposurePlan(
            filter_type=ftype, exposure_seconds=exp_s, count=count,
            gain=config.default_gain, offset=config.default_offset,
            acquired=min(acquired.get(ftype.value, 0), count),
        ))
    return plans


class ProjectStore:
    def __init__(self, config):
        self.config = config
        self.path = Path(config.data_dir) / "projects.json"
        self.projects: dict[str, ImagingProject] = {}
        self.load()

    def load(self):
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.projects = {pid: ImagingProject(**p) for pid, p in raw.items()}
            logger.info("Loaded %d projects from %s", len(self.projects), self.path)
        except Exception as e:  # noqa: BLE001
            logger.error("Failed to load projects.json: %s", e)

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(
            {pid: p.model_dump(mode="json") for pid, p in self.projects.items()},
            indent=2), encoding="utf-8")

    def add_from_target(self, target: CelestialTarget,
                        budget_hours: float = 8.0) -> ImagingProject:
        from uuid import uuid4
        kind = target_kind(target)
        proj = ImagingProject(
            id=str(uuid4()), target=target, priority=50,
            budget_hours=budget_hours,
            exposure_plans=allocate_exposures(kind, budget_hours, self.config),
            total_integration_hours=budget_hours,
        )
        self.projects[proj.id] = proj
        self.save()
        return proj

    def update(self, project_id: str, priority: int | None = None,
               budget_hours: float | None = None,
               active: bool | None = None) -> ImagingProject | None:
        proj = self.projects.get(project_id)
        if proj is None:
            return None
        if priority is not None:
            proj.priority = max(0, min(100, priority))
        if active is not None:
            proj.active = active
        if budget_hours is not None and budget_hours > 0:
            proj.budget_hours = round(budget_hours, 1)
            acquired = {p.filter_type.value: p.acquired
                        for p in proj.exposure_plans}
            proj.exposure_plans = allocate_exposures(
                target_kind(proj.target), proj.budget_hours, self.config,
                acquired)
            proj.total_integration_hours = proj.budget_hours
        proj.compute_completion() if hasattr(proj, "compute_completion") else None
        self.save()
        return proj

    def delete(self, project_id: str) -> bool:
        if project_id in self.projects:
            del self.projects[project_id]
            self.save()
            return True
        return False
