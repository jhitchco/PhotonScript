"""Tests for project store and budget allocation."""

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget
from photonscript.scheduler.project_store import (ProjectStore,
                                                  allocate_exposures,
                                                  target_kind)


def _config(tmp_path):
    return PhotonScriptConfig(data_dir=tmp_path)


def test_allocation_narrowband_budget_math(tmp_path):
    plans = allocate_exposures("narrowband", 10.0, _config(tmp_path))
    by = {p.filter_type.value: p for p in plans}
    assert by["Ha"].count == 42        # 10h * 35% / 300s
    assert by["OIII"].count == 36
    assert by["SII"].count == 42       # SII gets equal-or-more (faintest line)
    total_h = sum(p.exposure_seconds * p.count for p in plans) / 3600
    assert abs(total_h - 10.0) < 0.2


def test_allocation_broadband_l_heavy(tmp_path):
    plans = allocate_exposures("broadband", 6.0, _config(tmp_path))
    by = {p.filter_type.value: p.count for p in plans}
    assert by["L"] == 60               # 6h * 50% / 180s
    assert by["R"] == by["G"] == by["B"] == 20


def test_budget_change_preserves_acquired(tmp_path):
    config = _config(tmp_path)
    store = ProjectStore(config)
    t = CelestialTarget(name="Crescent Nebula", catalog_id="NGC 6888",
                        ra_hours=20.2, dec_degrees=38.35,
                        object_type="emission nebula")
    proj = store.add_from_target(t, budget_hours=8.0)
    proj.exposure_plans[0].acquired = 12  # some Ha already captured
    store.save()

    updated = store.update(proj.id, budget_hours=4.0)
    ha = next(p for p in updated.exposure_plans if p.filter_type.value == "Ha")
    assert ha.acquired == min(12, ha.count)

    # store persists across reload
    store2 = ProjectStore(config)
    assert proj.id in store2.projects
    assert store2.projects[proj.id].budget_hours == 4.0


def test_target_kind():
    neb = CelestialTarget(name="x", ra_hours=0, dec_degrees=0,
                          object_type="supernova remnant")
    gal = CelestialTarget(name="y", ra_hours=0, dec_degrees=0,
                          object_type="galaxy")
    assert target_kind(neb) == "narrowband"
    assert target_kind(gal) == "broadband"


def test_custom_mix_reallocates(tmp_path):
    """SII-heavy custom mix (e.g. 25/25/50) changes counts within same budget."""
    config = _config(tmp_path)
    store = ProjectStore(config)
    t = CelestialTarget(name="Soul Nebula", catalog_id="IC 1848",
                        ra_hours=2.85, dec_degrees=60.4,
                        object_type="emission nebula")
    proj = store.add_from_target(t, budget_hours=10.0)
    updated = store.update(proj.id, filter_mix={"Ha": 25, "OIII": 25, "SII": 50})
    by = {p.filter_type.value: p.count for p in updated.exposure_plans}
    assert by["SII"] == 60          # 10h * 50% / 300s
    assert by["Ha"] == by["OIII"] == 30
    assert updated.filter_mix == {"Ha": 25.0, "OIII": 25.0, "SII": 50.0}


def test_mix_normalizes_to_100(tmp_path):
    config = _config(tmp_path)
    store = ProjectStore(config)
    t = CelestialTarget(name="x", catalog_id="", ra_hours=1, dec_degrees=1,
                        object_type="galaxy")
    proj = store.add_from_target(t, budget_hours=6.0)
    updated = store.update(proj.id, filter_mix={"L": 6, "R": 2, "G": 2, "B": 2})
    assert updated.filter_mix == {"L": 50.0, "R": 16.7, "G": 16.7, "B": 16.7}


def test_default_narrowband_mix_favors_sii_equally():
    """Best practice: SII is faintest — never allocate it less than Ha."""
    from photonscript.scheduler.project_store import default_mix
    mix = default_mix("narrowband")
    assert mix["SII"] >= mix["Ha"] - 0.1
