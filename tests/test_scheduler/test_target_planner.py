"""Tests for the target planner and astronomical calculations."""

from datetime import datetime

import pytest

from photonscript.shared.models import CelestialTarget, ObservatoryLocation, TargetTier
from photonscript.shared.astronomy import (
    compute_altitude,
    compute_visibility_window,
    get_seasonal_targets,
    rank_targets_for_night,
)
from photonscript.scheduler.target_planner import (
    suggest_exposure_plan,
    create_project_from_target,
)


@pytest.fixture
def new_mexico_obs():
    return ObservatoryLocation(
        name="Test Observatory NM",
        latitude=32.9,
        longitude=-105.5,
        elevation=2200.0,
        timezone="America/Denver",
    )


@pytest.fixture
def orion_nebula():
    return CelestialTarget(
        name="Orion Nebula",
        catalog_id="M 42",
        ra_hours=5.588,
        dec_degrees=-5.39,
        object_type="emission nebula",
    )


@pytest.fixture
def andromeda():
    return CelestialTarget(
        name="Andromeda Galaxy",
        catalog_id="M 31",
        ra_hours=0.712,
        dec_degrees=41.27,
        object_type="galaxy",
    )


class TestSeasonalTargets:
    def test_march_targets_exist(self):
        targets = get_seasonal_targets(3)
        assert len(targets) > 0
        names = [t.name for t in targets]
        assert "Leo Triplet" in names

    def test_summer_targets_exist(self):
        targets = get_seasonal_targets(7)
        assert len(targets) > 0
        names = [t.name for t in targets]
        assert "Eagle Nebula (Pillars of Creation)" in names

    def test_all_months_have_targets(self):
        for month in range(1, 13):
            targets = get_seasonal_targets(month)
            assert len(targets) > 0, f"No targets for month {month}"


class TestExposurePlanning:
    def test_nebula_gets_narrowband(self, orion_nebula):
        plans = suggest_exposure_plan(orion_nebula)
        filter_types = {p.filter_type.value for p in plans}
        assert "Ha" in filter_types
        assert "OIII" in filter_types

    def test_galaxy_gets_broadband(self, andromeda):
        plans = suggest_exposure_plan(andromeda)
        filter_types = {p.filter_type.value for p in plans}
        assert "L" in filter_types
        assert "R" in filter_types
        assert "G" in filter_types
        assert "B" in filter_types

    def test_create_project(self, orion_nebula):
        project = create_project_from_target(orion_nebula)
        assert project.id is not None
        assert project.target.name == "Orion Nebula"
        assert len(project.exposure_plans) > 0
        assert project.total_integration_hours > 0


class TestAltitudeComputation:
    def test_orion_altitude_winter_midnight(self, new_mexico_obs, orion_nebula):
        # Jan 15 at midnight UTC (roughly 5 PM MST) — Orion should be rising
        time = datetime(2025, 1, 16, 5, 0, 0)  # midnight MST
        alt = compute_altitude(orion_nebula, new_mexico_obs, time)
        # Orion should be well above horizon in winter from NM
        assert alt > 0, f"Orion should be above horizon in January, got {alt:.1f}°"

    def test_andromeda_altitude_autumn(self, new_mexico_obs, andromeda):
        # October midnight MST
        time = datetime(2025, 10, 16, 5, 0, 0)
        alt = compute_altitude(andromeda, new_mexico_obs, time)
        assert alt > 30, f"Andromeda should be high in October, got {alt:.1f}°"


class TestVisibility:
    def test_visibility_window(self, new_mexico_obs, orion_nebula):
        date = datetime(2025, 1, 15)
        vis = compute_visibility_window(orion_nebula, new_mexico_obs, date)
        assert vis["visible"] is True
        assert vis["hours"] > 0

    def test_ranking(self, new_mexico_obs):
        targets = get_seasonal_targets(3)
        ranked = rank_targets_for_night(targets, new_mexico_obs, datetime(2025, 3, 15))
        assert len(ranked) > 0
        # First target should have the most visibility hours
        if len(ranked) > 1:
            assert ranked[0]["visibility"]["hours"] >= ranked[1]["visibility"]["hours"]
