"""Ha first for a two-rig narrowband blend (Heart Nebula, 2026-10-14).

The NB blend (integration/blend.py --mode nb) needs an RC16 Ha master as
its main input. The planner shared a night by the remaining counts (the
mix), so on 2026-10-13 the Heart pass was Ha 32 / OIII 37 / SII 38 at 180 s
with no Ha in the bank. With plan_nb_blend_ha_first (default off) or a
goal's ha_first_h, RC16 Ha gets the night first until the goal would hold
blend_nb_min_ha_h, then OIII / SII share what is left by the mix.

Pinned: the default is unchanged, the allocation rule, which goals it
applies to, the window rescale keeps the reserve, the 2026-10-13 Heart goal
through the planner, and ha_first_h through ProjectStore.update.
"""

from datetime import datetime

import pytest

from photonscript.scheduler.target_planner import (
    _fit_by_moon, _fit_ha_first, feeds_nb_blend, ha_first_seconds,
    plan_night_sequence)
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (CelestialTarget, ExposurePlan,
                                        FilterType, ImagingProject)

F = FilterType
TONIGHT = datetime(2026, 10, 14, 4, 0)   # 22:00 MDT, when /api/tonight was read


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _plan(f, exp, count, acquired=0, rig="rc16", acquired_s=0.0):
    return ExposurePlan(filter_type=f, exposure_seconds=exp, count=count,
                        acquired=acquired, acquired_s=acquired_s, gain=200,
                        offset=256, rig=rig)


def _heart(**kw):
    """The Heart goal as GET /api/projects2 had it on 2026-10-13."""
    return ImagingProject(
        id="71cb2775-b8bb-44c7-8ed5-03cf851fef99", priority=60,
        budget_hours=30.0, filter_mix={"Ha": 30, "OIII": 35, "SII": 35},
        exposure_overrides={"Ha": 180.0, "OIII": 180.0, "SII": 180.0},
        target=CelestialTarget(name="Heart Nebula", catalog_id="IC 1805",
                               ra_hours=2.555, dec_degrees=61.47,
                               object_type="emission nebula",
                               angular_size_arcmin=60.0),
        exposure_plans=[
            _plan(F.HA, 180, 180), _plan(F.OIII, 180, 210, 6, acquired_s=1200),
            _plan(F.SII, 180, 210),
            ExposurePlan(filter_type=F.OSC, exposure_seconds=120, count=360,
                         acquired=177, acquired_s=21330, gain=100, offset=256,
                         rig="piggyback")],
        **kw)


def _m31():
    return ImagingProject(
        id="m31", priority=50, target=CelestialTarget(
            name="Andromeda Galaxy", ra_hours=0.712, dec_degrees=41.27,
            object_type="galaxy"),
        exposure_plans=[_plan(F.LUMINANCE, 180, 60), _plan(F.RED, 180, 20),
                        _plan(F.GREEN, 180, 20), _plan(F.BLUE, 180, 20)])


def _counts(exps):
    return {e.filter_type.value: e.count for e in exps}


def _nb(ha=180, oiii=204, sii=210, exp=180):
    return [_plan(F.HA, exp, ha), _plan(F.OIII, exp, oiii),
            _plan(F.SII, exp, sii)]


# --- 1. the allocation rule ------------------------------------------------------

def test_ha_reserve_first_then_the_mix():
    out = _fit_ha_first(_nb(), 19500, "NB", 0.0, 7200)
    c = _counts(out)
    assert out[0].filter_type.value == "Ha"          # shot first
    assert c["Ha"] >= 40                              # 2 h at 180 s reserved
    assert c["OIII"] >= 1 and c["SII"] >= 1           # the rest by the mix
    assert abs(c["OIII"] - c["SII"]) <= 1
    assert sum(c.values()) * 180 <= 19500


def test_no_deficit_is_the_old_fit():
    a = _counts(_fit_ha_first(_nb(), 19500, "NB", 7200.0, 7200))
    b = _counts(_fit_by_moon(_nb(), 19500, "NB"))
    assert a == b


def test_partial_ha_counts_toward_the_minimum():
    out = _counts(_fit_ha_first(_nb(), 19500, "NB", 5400.0, 7200))
    legacy = _counts(_fit_by_moon(_nb(), 19500, "NB"))
    assert out["Ha"] >= 10 and out["OIII"] < legacy["OIII"]


def test_reserve_can_take_the_whole_night():
    out = _fit_ha_first(_nb(), 3000, "NB", 0.0, 7200)
    assert _counts(out) == {"Ha": 16}                 # 3000 // 180


def test_reserve_never_exceeds_what_ha_still_owes():
    out = _counts(_fit_ha_first(_nb(ha=10), 19500, "NB", 0.0, 7200))
    assert out["Ha"] == 10 and out["OIII"] > 30


# --- 2. which goals ---------------------------------------------------------------

def test_default_off(tmp_path):
    assert ha_first_seconds(_heart(), _cfg(tmp_path)) == 0.0


def test_config_on_for_a_two_rig_nb_goal_only(tmp_path):
    cfg = _cfg(tmp_path, plan_nb_blend_ha_first=True, blend_nb_min_ha_h=2.5)
    assert feeds_nb_blend(_heart())
    assert ha_first_seconds(_heart(), cfg) == 2.5 * 3600
    rc16_only = _heart()
    rc16_only.exposure_plans = [e for e in rc16_only.exposure_plans
                                if e.rig == "rc16"]
    assert not feeds_nb_blend(rc16_only)
    assert ha_first_seconds(rc16_only, cfg) == 0.0
    assert ha_first_seconds(_m31(), cfg) == 0.0       # no Ha plan


def test_goal_override_wins(tmp_path):
    on = _cfg(tmp_path, plan_nb_blend_ha_first=True)
    off = _cfg(tmp_path)
    assert ha_first_seconds(_heart(ha_first_h=0), on) == 0.0
    assert ha_first_seconds(_heart(ha_first_h=3), off) == 3 * 3600


# --- 3. the 2026-10-13 Heart plan through the planner ------------------------------

def _tonight(tmp_path, **kw):
    ts = plan_night_sequence([_m31(), _heart()], _cfg(tmp_path, **kw), TONIGHT)
    return next(t for t in ts if t.name == "Heart Nebula")


def test_tonight_off_is_the_live_plan(tmp_path):
    h = _tonight(tmp_path)
    assert [e.filter_type.value for e in h.exposures] == ["Ha", "OIII", "SII"]
    c = _counts(h.exposures)
    # /api/tonight 2026-10-13 22:00 MDT: Ha 32, OIII 37, SII 38
    assert c["Ha"] < c["OIII"] <= c["SII"]


def test_tonight_on_fills_ha_first_inside_the_window(tmp_path):
    off = _counts(_tonight(tmp_path).exposures)
    t = _tonight(tmp_path, plan_nb_blend_ha_first=True)
    on = _counts(t.exposures)
    assert t.exposures[0].filter_type.value == "Ha"
    assert on["Ha"] * 180 >= 2 * 3600                # 2 h of Ha per pass
    assert on["Ha"] > off["Ha"]
    assert on["OIII"] < off["OIII"] and on["SII"] < off["SII"]
    assert on["OIII"] >= 1 and on["SII"] >= 1
    if t.window_start_utc is not None:              # PS-180 window rescale
        win = (t.window_end_utc - t.window_start_utc).total_seconds()
        assert sum(on.values()) * 180 <= win * 0.85 + 180


# --- 4. the goal field ------------------------------------------------------------

def test_store_update_ha_first_h(tmp_path):
    from photonscript.scheduler.project_store import ProjectStore
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    p = store.add_from_target(_heart().target, budget_hours=10)
    before = [(e.filter_type, e.count) for e in p.exposure_plans]
    store.update(p.id, ha_first_h=2)
    assert store.projects[p.id].ha_first_h == 2.0
    assert [(e.filter_type, e.count)
            for e in store.projects[p.id].exposure_plans] == before
    store.update(p.id, priority=70)                  # left out: kept
    assert store.projects[p.id].ha_first_h == 2.0
    store.update(p.id, ha_first_h=None)
    assert store.projects[p.id].ha_first_h is None
    again = ProjectStore(cfg)                        # persisted
    assert again.projects[p.id].ha_first_h is None
    store.update(p.id, ha_first_h=0)
    assert ProjectStore(cfg).projects[p.id].ha_first_h == 0.0


def test_store_update_rejects_garbage(tmp_path):
    from photonscript.scheduler.project_store import ProjectStore
    store = ProjectStore(_cfg(tmp_path))
    p = store.add_from_target(_heart().target, budget_hours=10)
    with pytest.raises(ValueError):
        store.update(p.id, ha_first_h="lots")
