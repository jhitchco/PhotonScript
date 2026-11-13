"""2026-10-25: RC16 narrowband needs guiding (rc16_narrowband_requires_guided).

2026-10-24 the RC16 shot 3 nm Ha at 120 s unguided all night: 126 rejects in
a row, too few stars for AF. A night planned unguided now schedules no RC16
Ha / OIII / SII, in split and in the deep pick; the plan names what it left
out and why. Moon-up time goes to broadband only if the moon rule allows,
else the RC16 idles. Piggy-600 plans are not touched.

Fixture and moon as in test_deep_night.py (2026-10-14 goal store; tonight's
moon sets soon after dusk, so broadband is allowed).
"""
from datetime import timedelta

from photonscript.scheduler import deep_night as dn
from photonscript.scheduler import moon
from photonscript.scheduler import target_planner as tp
from photonscript.shared.models import ExposurePlan, FilterType
from tests.test_scheduler.test_deep_night import NOW, _cfg, _goals, _id, _plan
from tests.test_scheduler.test_deep_night import _det_moon  # noqa: F401  (autouse fixture)

H = timedelta(hours=1)
NB = {"Ha", "OIII", "SII"}
CAT = "Cat's Eye Nebula"


def _filters(ts, name):
    t = next((t for t in ts if t.name == name), None)
    return None if t is None else {e.filter_type.value for e in t.exposures}


def _bright(monkeypatch, cfg):
    mw = moon.moon_window_tonight(cfg, now=NOW)
    bright = dict(mw, set_utc=None, set_local=None, illum_pct=60)
    monkeypatch.setattr(moon, "moon_window_tonight", lambda c, now=None: bright)


def test_nb_needs_guiding_switch(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.rc16_narrowband_requires_guided is True
    assert tp.nb_needs_guiding(cfg, False)
    assert not tp.nb_needs_guiding(cfg, True) and not tp.nb_needs_guiding(cfg, None)
    off = _cfg(tmp_path, rc16_narrowband_requires_guided=False)
    assert not tp.nb_needs_guiding(off, False)


def test_split_unguided_drops_rc16_narrowband_and_says_why(tmp_path):
    goals = _goals(activate=("Crescent Nebula",))
    ts, info = _plan(_cfg(tmp_path), goals, guided=False)
    f = _filters(ts, "Crescent Nebula")
    assert f and not (f & NB) and f <= {"L", "R", "G", "B"}     # broadband only
    rows = {r["target"]: r for r in info["rc16_nb_unguided"]}
    assert rows["Crescent Nebula"]["filters"] == ["Ha", "OIII", "SII"]
    assert rows["Crescent Nebula"]["owed_h"] > 0
    assert "needs guiding" in rows["Crescent Nebula"]["reason"]
    assert "Crescent Nebula Ha/OIII/SII not scheduled" in info["rc16_nb_note"]
    assert all(not t.start_guiding for t in ts)


def test_guided_night_keeps_narrowband(tmp_path):
    goals = _goals(activate=("Crescent Nebula",))
    ts, info = _plan(_cfg(tmp_path), goals, guided=True)
    assert _filters(ts, "Crescent Nebula") & NB
    assert info["rc16_nb_unguided"] == [] and "rc16_nb_note" not in info


def test_switch_off_keeps_the_old_unguided_narrowband(tmp_path):
    goals = _goals(activate=("Crescent Nebula",))
    ts, info = _plan(_cfg(tmp_path, rc16_narrowband_requires_guided=False), goals,
                     guided=False)
    assert _filters(ts, "Crescent Nebula") & NB and info["rc16_nb_unguided"] == []


def test_narrowband_only_goal_is_not_scheduled_unguided(tmp_path):
    ts, info = _plan(_cfg(tmp_path), _goals(activate=(CAT,)), guided=False)
    assert CAT not in [t.name for t in ts]
    assert any(r["target"] == CAT for r in info["rc16_nb_unguided"])
    ts, _ = _plan(_cfg(tmp_path), _goals(activate=(CAT,)), guided=True)
    assert CAT in [t.name for t in ts]


def test_bright_moon_unguided_rc16_idles(tmp_path, monkeypatch):
    """The moon defers broadband and the narrowband needs guiding: nothing
    is left for the RC16 (it idles / shoots owed calibration as today)."""
    cfg = _cfg(tmp_path)
    _bright(monkeypatch, cfg)
    ts, info = _plan(cfg, _goals(activate=("Crescent Nebula",)), guided=False)
    assert ts == []
    assert [r["target"] for r in info["rc16_nb_unguided"]] == ["Crescent Nebula"]
    # guided, the same moon: the Crescent's narrowband runs
    ts, _ = _plan(cfg, _goals(activate=("Crescent Nebula",)), guided=True)
    assert _filters(ts, "Crescent Nebula") & NB


def test_deep_picker_does_not_pick_for_unguided_narrowband(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _bright(monkeypatch, cfg)
    # guided: the Crescent wins the bright-moon deep pick on its narrowband
    _ts, info = _plan(cfg, _goals(activate=("Crescent Nebula",)), night_mode="deep",
                      guided=True)
    assert info["deep_pick"]["target"] == "Crescent Nebula"
    # unguided: its narrowband counts for nothing, the moon defers the rest
    ts, info = _plan(cfg, _goals(activate=("Crescent Nebula",)), night_mode="deep",
                     guided=False)
    rows = {s["target"]: s for s in info["deep_scores"]}
    assert not rows["Crescent Nebula"]["eligible"]
    assert info["deep_pick"] is None and ts == []


def test_score_goal_reason_for_a_narrowband_only_goal():
    cat = next(p for p in _goals(activate=(CAT,)) if p.target.name == CAT)
    vis = {"rise_time": NOW, "set_time": NOW + 8 * H, "hours": 8.0,
           "transit_time": NOW + 4 * H}
    s = dn.score_goal(cat, vis, NOW, NOW + 9.8 * H, NOW, NOW + 9.8 * H)
    assert s["eligible"]
    s = dn.score_goal(cat, vis, NOW, NOW + 9.8 * H, NOW, NOW + 9.8 * H, nb_ok=False)
    assert not s["eligible"] and "needs guiding" in s["why"]
    assert s["components"]["fill"]["value"]["rc16_owed_h"] == 0
    assert dn.owed_by_kind(cat, nb_ok=False)["NB"] == 0 < dn.owed_by_kind(cat)["NB"]


def test_deep_pick_runs_broadband_only_unguided(tmp_path):
    """Moon allows broadband: the explicit Crescent deep pick runs its
    broadband only."""
    goals = _goals(activate=("Crescent Nebula",))
    ts, info = _plan(_cfg(tmp_path), goals, night_mode="deep",
                     deep_target=_id(goals, "Crescent Nebula"), guided=False)
    assert info["deep_pick"]["target"] == "Crescent Nebula"
    f = _filters(ts, "Crescent Nebula")
    assert f and not (f & NB)


def test_piggy_plans_unaffected(tmp_path):
    goals = _goals()
    a, _ = _plan(_cfg(tmp_path), [p.model_copy(deep=True) for p in goals], guided=True)
    b, info = _plan(_cfg(tmp_path), [p.model_copy(deep=True) for p in goals], guided=False)
    # the fixture's active goals are broadband on the RC16: same plan either way
    assert [(t.name, t.driving_rig) for t in a] == [(t.name, t.driving_rig) for t in b]
    assert info["rc16_nb_unguided"] == []


def test_piggy_hold_avoids_narrowband_unguided(tmp_path):
    goals = _goals()
    n604 = next(p for p in goals if p.target.name == "NGC 604")
    n604.exposure_plans = [
        ExposurePlan(filter_type=FilterType.HA, exposure_seconds=120, count=10,
                     acquired=10, rig="rc16"),
        *[e for e in n604.exposure_plans if e.rig != "rc16"]]
    assert tp.piggy_hold_exposure(n604).filter_type.value == "Ha"
    hold = tp.piggy_hold_exposure(n604, nb_ok=False)
    assert hold.filter_type.value == "L" and hold.exposure_seconds == 120
    # in the plan: the Piggy-driven goal keeps its pointing for the Piggy-600
    ts, _ = _plan(_cfg(tmp_path), [p.model_copy(deep=True) for p in goals], guided=False)
    assert _filters(ts, "NGC 604") == {"L"}
    # a broadband RC16 plan is preferred over an L copy of the narrowband
    n604.exposure_plans.insert(0, ExposurePlan(filter_type=FilterType.RED, exposure_seconds=180,
                                               count=4, acquired=4, rig="rc16"))
    assert tp.piggy_hold_exposure(n604, nb_ok=False).filter_type.value == "R"


def test_deep_rc16_hold_is_broadband_unguided(tmp_path):
    goals = _goals()
    n604 = next(p for p in goals if p.target.name == "NGC 604")
    n604.exposure_plans = [e for e in n604.exposure_plans if e.rig != "rc16"]
    assert dn.rc16_hold(n604, 3600).filter_type.value == "Ha"
    assert dn.rc16_hold(n604, 3600, nb_ok=False).filter_type.value == "L"
    ts, _ = _plan(_cfg(tmp_path), goals, night_mode="deep", deep_target=n604.id,
                  guided=False)
    assert [e.filter_type.value for e in ts[0].exposures] == ["L"]


def test_ha_first_is_off_unguided(tmp_path):
    goals = _goals(activate=("Crescent Nebula",))
    cres = next(p for p in goals if p.target.name == "Crescent Nebula")
    cres.ha_first_h = 2.0
    ts, _ = _plan(_cfg(tmp_path), goals, guided=False)
    assert not (_filters(ts, "Crescent Nebula") & NB)


def test_api_tonight_uses_the_armed_mode(tmp_path, monkeypatch):
    from datetime import datetime as _datetime

    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    goals = {p.id: p for p in _goals(activate=("Crescent Nebula",))}
    monkeypatch.setattr(app, "_config", cfg)
    monkeypatch.setattr(app, "_stored_projects", lambda: goals)

    class _DT(_datetime):
        @classmethod
        def utcnow(cls):
            return NOW
    monkeypatch.setattr(app, "datetime", _DT)

    class _Arm:
        def _use_guiding(self):
            return False
    monkeypatch.setattr(app, "_armer", _Arm())
    body = TestClient(app.app).get("/api/tonight").json()
    assert [r["target"] for r in body["rc16_nb_unguided"]] == ["Crescent Nebula"]
    assert "needs guiding" in body["rc16_nb_note"]
    monkeypatch.setattr(app, "_armer", None)
    assert app.armed_guided() is None


def test_config_on_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    assert "rc16_narrowband_requires_guided" in {f[0] for f in _CONFIG_FIELDS}
