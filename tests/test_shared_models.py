"""Tests for shared data models."""

from photonscript.shared.models import (
    CelestialTarget, ExposurePlan, FilterType, ImagingProject, TargetTier,
    ObservatoryLocation, TransferWindow, NinaSequenceFile,
)


class TestImagingProject:
    def test_compute_completion_empty(self):
        project = ImagingProject(
            target=CelestialTarget(name="Test", ra_hours=0, dec_degrees=0),
        )
        assert project.compute_completion() == 0.0

    def test_compute_completion_partial(self):
        project = ImagingProject(
            target=CelestialTarget(name="Test", ra_hours=0, dec_degrees=0),
            exposure_plans=[
                ExposurePlan(filter_type=FilterType.HA, count=20, acquired=10),
                ExposurePlan(filter_type=FilterType.OIII, count=20, acquired=0),
            ],
        )
        pct = project.compute_completion()
        assert pct == 25.0  # 10 of 40

    def test_compute_completion_full(self):
        project = ImagingProject(
            target=CelestialTarget(name="Test", ra_hours=0, dec_degrees=0),
            exposure_plans=[
                ExposurePlan(filter_type=FilterType.HA, count=10, acquired=10),
            ],
        )
        assert project.compute_completion() == 100.0


class TestObservatory:
    def test_default_location(self):
        obs = ObservatoryLocation()
        assert obs.latitude == 32.9
        assert obs.longitude == -105.5
        assert obs.timezone == "America/Denver"
        assert obs.bortle_class == 2


class TestFilterType:
    def test_all_filters(self):
        assert FilterType.LUMINANCE.value == "L"
        assert FilterType.HA.value == "Ha"
        assert FilterType.OIII.value == "OIII"
        assert FilterType.SII.value == "SII"


class TestTargetTier:
    def test_tiers(self):
        assert TargetTier.BEST.value == "best"
        assert TargetTier.BETTER.value == "better"
        assert TargetTier.GOOD.value == "good"
