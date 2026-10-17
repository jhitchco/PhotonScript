"""Tests for project store and budget allocation."""

import json

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget, ImagingProject
from photonscript.scheduler.project_store import (ProjectStore,
                                                  allocate_exposures,
                                                  target_kind, HDR_SHORT_COUNT)


def _config(tmp_path):
    return PhotonScriptConfig(data_dir=tmp_path)


def test_allocation_narrowband_budget_math(tmp_path):
    plans = allocate_exposures("narrowband", 10.0, _config(tmp_path))
    by = {p.filter_type.value: p for p in plans}
    assert by["Ha"].exposure_seconds == 600   # first-night data: 300s was RN-limited
    assert by["Ha"].count == 21               # 10h * 35% / 600s
    assert by["OIII"].count == 18
    assert by["SII"].count == 21              # SII gets equal-or-more (faintest line)
    total_h = sum(p.exposure_seconds * p.count for p in plans) / 3600
    assert abs(total_h - 10.0) < 0.2


def test_allocation_broadband_l_heavy(tmp_path):
    plans = allocate_exposures("broadband", 6.0, _config(tmp_path))
    by = {p.filter_type.value: p.count for p in plans}
    assert by["L"] == 60               # 6h * 50% / 180s
    # PS-176: R/G/B default to rc16_rgb_exposure_s (120 s): 6h / 6 / 120 s
    assert by["R"] == by["G"] == by["B"] == 30


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
    assert by["SII"] == 30          # 10h * 50% / 600s
    assert by["Ha"] == by["OIII"] == 15
    assert updated.filter_mix == {"Ha": 25.0, "OIII": 25.0, "SII": 50.0}


def test_mix_normalizes_to_100(tmp_path):
    config = _config(tmp_path)
    store = ProjectStore(config)
    t = CelestialTarget(name="x", catalog_id="", ra_hours=1, dec_degrees=1,
                        object_type="galaxy")
    proj = store.add_from_target(t, budget_hours=6.0)
    updated = store.update(proj.id, filter_mix={"L": 6, "R": 2, "G": 2, "B": 2})
    assert updated.filter_mix == {"L": 50, "R": 17, "G": 17, "B": 17}


def test_default_narrowband_mix_favors_sii_equally():
    """Best practice: SII is faintest — never allocate it less than Ha."""
    from photonscript.scheduler.project_store import default_mix
    mix = default_mix("narrowband")
    assert mix["SII"] >= mix["Ha"] - 0.1


# --- HDR ("special plan per target") allocation ------------------------------

def test_hdr_allocation_adds_short_set_preserving_deep_budget(tmp_path):
    """An HDR request ADDS a fixed short set as a light time overhead: the short
    set's time is subtracted from the budget and the remainder stays as the deep
    long set. Total integration stays ~= budget (the long/deep set is preserved,
    NOT gutted by carving sub count)."""
    config = _config(tmp_path)
    mix = {"Ha": 50, "OIII": 50, "SII": 0}
    plain = {p.filter_type.value: p
             for p in allocate_exposures("narrowband", 5.0, config,
                                         custom_mix=mix)}
    hdr = {p.filter_type.value: p
           for p in allocate_exposures("narrowband", 5.0, config,
                                       custom_mix=mix,
                                       hdr={"Ha": 60, "OIII": 60})}
    for f in ("Ha", "OIII"):
        # short set: fixed count at the requested short length
        assert hdr[f].hdr_short_count == HDR_SHORT_COUNT
        assert hdr[f].hdr_short_seconds == 60
        assert hdr[f].exposure_seconds == 600  # long subs unchanged length
        # deep long set preserved: within ~2 long subs of the non-HDR count,
        # NOT reduced by the full short count (the old carve-out bug).
        assert hdr[f].count >= plain[f].count - 2
        # total integration time stays ~= budget (within one long sub)
        total = hdr[f].count * 600 + hdr[f].hdr_short_count * 60
        assert abs(total - plain[f].count * 600) <= 600
    # a filter NOT in the HDR request is untouched
    assert "SII" not in hdr  # 0% -> not allocated at all


def test_hdr_skipped_when_allocation_too_small(tmp_path):
    """If a filter's allocated count can't be split (would leave no long sub),
    HDR is skipped for it rather than going all-short."""
    config = _config(tmp_path)
    # tiny budget -> Ha allocates just 1 sub
    plans = {p.filter_type.value: p
             for p in allocate_exposures("narrowband", 0.05, config,
                                         custom_mix={"Ha": 100},
                                         hdr={"Ha": 60})}
    assert plans["Ha"].hdr_short_count == 0
    assert plans["Ha"].hdr_short_seconds is None
    assert plans["Ha"].count >= 1


def test_exposure_overrides_change_long_length(tmp_path):
    config = _config(tmp_path)
    plans = {p.filter_type.value: p
             for p in allocate_exposures("narrowband", 5.0, config,
                                         custom_mix={"Ha": 100},
                                         overrides={"Ha": 300})}
    assert plans["Ha"].exposure_seconds == 300  # override honored (not 600)


def test_hdr_fields_round_trip_through_projects_json(tmp_path):
    """A project carrying the new HDR fields serializes to projects.json and
    loads back byte-for-byte identical."""
    config = _config(tmp_path)
    store = ProjectStore(config)
    t = CelestialTarget(name="Cat's Eye", catalog_id="NGC 6543",
                        ra_hours=17.976, dec_degrees=66.633,
                        object_type="planetary nebula")
    proj = store.add_from_target(t, budget_hours=5.0)
    proj = store.update(proj.id, filter_mix={"Ha": 50, "OIII": 50})
    proj.hdr = {"Ha": 60, "OIII": 60}
    proj = store.update(proj.id, budget_hours=5.0)  # re-allocate WITH hdr
    ha = next(p for p in proj.exposure_plans if p.filter_type.value == "Ha")
    assert ha.hdr_short_count == HDR_SHORT_COUNT and ha.hdr_short_seconds == 60

    # reload from disk -> identical dump
    before = store.projects[proj.id].model_dump(mode="json")
    store2 = ProjectStore(config)
    after = store2.projects[proj.id].model_dump(mode="json")
    assert before == after


# --- Committed seed (non-destructive) ----------------------------------------

def test_load_seeds_catseye_when_absent(tmp_path):
    """A fresh store (no projects.json) picks up Cat's Eye from the committed
    seed and persists it to projects.json."""
    config = _config(tmp_path)
    store = ProjectStore(config)
    cats = [p for p in store.projects.values()
            if p.target.catalog_id.replace(" ", "") == "NGC6543"]
    assert cats, "Cat's Eye (NGC 6543) should be seeded"
    proj = cats[0]
    assert proj.priority == 70
    by = {p.filter_type.value: p for p in proj.exposure_plans}
    assert by["Ha"].hdr_short_count == HDR_SHORT_COUNT
    assert by["Ha"].hdr_short_seconds == 60
    # persisted so it's there on the next load
    assert (tmp_path / "projects.json").exists()
    store2 = ProjectStore(config)
    assert any(p.target.catalog_id.replace(" ", "") == "NGC6543"
               for p in store2.projects.values())


def test_seed_never_overwrites_existing_target(tmp_path):
    """If the user already has an NGC 6543 project, the seed must NOT touch it
    (non-destructive: only ADD missing targets)."""
    config = _config(tmp_path)
    mine = ImagingProject(
        id="my-own-catseye",
        target=CelestialTarget(name="My Cat's Eye", catalog_id="NGC 6543",
                               ra_hours=17.976, dec_degrees=66.633,
                               object_type="planetary nebula"),
        priority=11, budget_hours=99.0)
    (tmp_path / "projects.json").write_text(
        json.dumps({"my-own-catseye": mine.model_dump(mode="json")}),
        encoding="utf-8")

    store = ProjectStore(config)
    ngc = [p for p in store.projects.values()
           if p.target.catalog_id.replace(" ", "") == "NGC6543"]
    assert len(ngc) == 1              # not duplicated
    assert ngc[0].id == "my-own-catseye"
    assert ngc[0].priority == 11      # untouched
    assert ngc[0].budget_hours == 99.0
    assert "seed-ngc6543-catseye" not in store.projects


# --- HDR progress accounting (finish pass) -------------------------------------

def _hdr_store(tmp_path):
    config = _config(tmp_path)
    store = ProjectStore(config)
    t = CelestialTarget(name="HDR Test", catalog_id="NGC 9999",
                        ra_hours=17.976, dec_degrees=66.633,
                        object_type="planetary nebula")
    proj = store.add_from_target(t, budget_hours=5.0)
    proj = store.update(proj.id, filter_mix={"Ha": 100},
                        hdr={"Ha": 60})
    return store, proj


def test_short_subs_count_toward_short_set_not_long(tmp_path):
    store, proj = _hdr_store(tmp_path)
    ha = next(p for p in proj.exposure_plans if p.filter_type.value == "Ha")
    assert ha.hdr_short_count == HDR_SHORT_COUNT
    assert store.record_accepted_sub("HDR Test", "Ha", 60.0)
    assert store.record_accepted_sub("HDR Test", "Ha", 600.0)
    assert store.record_accepted_sub("HDR Test", "Ha")      # unknown length -> long
    assert (ha.hdr_short_acquired, ha.acquired) == (1, 2)
    assert ha.short_remaining() == HDR_SHORT_COUNT - 1


def test_update_hdr_via_api_fields_and_keeps_short_progress(tmp_path):
    store, proj = _hdr_store(tmp_path)
    store.record_accepted_sub("HDR Test", "Ha", 60.0)
    proj = store.update(proj.id, budget_hours=6.0)
    ha = next(p for p in proj.exposure_plans if p.filter_type.value == "Ha")
    assert ha.hdr_short_acquired == 1                       # survived reallocation
    proj = store.update(proj.id, hdr={})                    # clear HDR
    ha = next(p for p in proj.exposure_plans if p.filter_type.value == "Ha")
    assert proj.hdr is None and ha.hdr_short_count == 0


def test_completion_includes_short_set(tmp_path):
    store, proj = _hdr_store(tmp_path)
    ha = next(p for p in proj.exposure_plans if p.filter_type.value == "Ha")
    ha.hdr_short_acquired = ha.hdr_short_count
    total = ha.count + ha.hdr_short_count
    assert proj.compute_completion() == round(ha.hdr_short_count / total * 100, 1)
