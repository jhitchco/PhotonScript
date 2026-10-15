"""Deep night (2026-10-14): one goal for the whole dark window.

Jeremy: "it's kinda nice to be able to image a target a night and create
those deep, vivid images (lots of texture)". The planner splits each night
among the goals (PS-180 windows); plan_night_mode "deep" (or the
tonight-only override, POST /api/tonight/mode) gives the dark window to ONE
goal on both rigs and falls back to split once it sets or is done.

Fixture: the live goal store (GET /api/projects2, 2026-10-14, read-only,
trimmed to the planner's fields): Andromeda Galaxy (65, Piggy-driven, RC16
LRGB 1.4 h owed, OSC done), Heart Nebula (60, RC16 LRGB 8 h, OSC 3.1 h),
NGC 604 (55, Piggy-driven, RC16 LRGB 6 h, OSC 9.1 h), and two inactive
goals (Cat's Eye, Crescent). Tonight at 2026-10-15T02:10Z the moon is 20%
lit and up at dusk but sets an hour after it, so broadband is allowed
(moon.sets_soon_after_dusk, main 1063868).
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import deep_night as dn
from photonscript.scheduler import moon
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import target_planner as tp
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ImagingProject

FIX = Path(__file__).parent / "fixtures" / "deep_night" / "projects2_2026-10-14.json"
NOW = datetime(2026, 10, 15, 2, 10)        # 20:10 MDT 10-14
NIGHT = "2026-10-14"
H = timedelta(hours=1)


@pytest.fixture(autouse=True)
def _det_moon(monkeypatch):
    # the generator's moon window reads the wall clock; tonight's moon is up
    # at dusk (no moonrise cap), so {} plans the same windows
    monkeypatch.setattr(nsj, "_moon_window", lambda: {})
    moon._cache.clear()
    yield
    moon._cache.clear()


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _goals(activate=()):
    out = [ImagingProject(**p) for p in json.loads(FIX.read_text())]
    for p in out:
        if p.target.name in activate:
            p.active, p.priority = True, 50
    return out


def _id(goals, name):
    return next(p.id for p in goals if p.target.name == name)


def _plan(cfg, goals=None, now=NOW, **kw):
    info = {}
    ts = tp.plan_night_sequence(goals or _goals(), cfg, now, info=info, **kw)
    return ts, info


def _windows(ts):
    return [(t.name, t.window_start_utc, t.window_end_utc, t.handoff_utc)
            for t in ts]


# --- tonight's moon ---------------------------------------------------------------

def test_tonights_moon_sets_soon_after_dusk_so_broadband_is_allowed(tmp_path):
    mw = moon.moon_window_tonight(_cfg(tmp_path), now=NOW)
    assert not mw["down_at_dusk"] and mw["illum_pct"] >= 20
    assert moon.sets_soon_after_dusk(mw)
    assert not moon.broadband_deferred(mw)


# --- split (the default) vs deep -------------------------------------------------------

def test_split_is_the_default_and_shares_the_night(tmp_path):
    ts, info = _plan(_cfg(tmp_path))
    assert info["mode"] == "split" and info["mode_source"] == "config"
    assert info["deep_pick"] is None and info["deep_scores"] == []
    assert [t.name for t in ts] == ["Andromeda Galaxy", "NGC 604",
                                    "Heart Nebula"]


def test_deep_picks_one_goal_for_the_whole_night_with_the_breakdown(tmp_path):
    ts, info = _plan(_cfg(tmp_path, plan_night_mode="deep"))
    assert info["mode"] == "deep" and info["mode_source"] == "config"
    scores = info["deep_scores"]
    assert [s["target"] for s in scores] == ["NGC 604", "Heart Nebula",
                                             "Andromeda Galaxy"]
    assert all(s["eligible"] for s in scores)
    for s in scores:
        assert s["score"] == pytest.approx(
            sum(c["points"] for c in s["components"].values()), abs=0.15)
        assert s["components"]["moon"]["norm"] == 1.0   # moon sets soon
    pick = info["deep_pick"]
    assert pick["target"] == "NGC 604" and pick["score"] == scores[0]["score"]
    # NGC 604 wins on fill (Piggy owes 9.1 h) and transit near midnight;
    # Heart has the higher priority, M31 owes only 1.4 h
    c = {s["target"]: s["components"] for s in scores}
    assert c["NGC 604"]["fill"]["points"] > c["Heart Nebula"]["fill"]["points"]
    assert c["Heart Nebula"]["priority"]["points"] > c["NGC 604"]["priority"]["points"]
    assert c["Andromeda Galaxy"]["fill"]["value"]["piggy_owed_h"] == 0.0
    # one target from its rise to dawn, both rigs on it (Piggy-driven)
    assert [t.name for t in ts] == ["NGC 604"]
    t = ts[0]
    assert t.handoff_utc is None and t.driving_rig == "piggyback"
    assert (t.window_end_utc - t.window_start_utc) > 9 * H
    assert pick["fallback"] is None and pick["split_targets"] == []
    # the RC16 shoots its filters by the mix (2:1:1:1 L:R:G:B)
    n = {e.filter_type.value: e.count for e in t.exposures}
    assert n["L"] == 2 * n["R"] and n["R"] == n["G"] == n["B"]


def test_explicit_target_wins_whatever_its_score(tmp_path):
    goals = _goals()
    ts, info = _plan(_cfg(tmp_path), goals, night_mode="deep",
                     deep_target=_id(goals, "Heart Nebula"))
    assert info["deep_pick"]["target"] == "Heart Nebula"
    assert info["deep_pick"]["explicit"] is True
    # the Heart clears 30 deg at 02:45Z: the 35 min before it are split
    # (M31, the only goal up that can use them), then the Heart to dawn
    w = _windows(ts)
    assert [x[0] for x in w] == ["Andromeda Galaxy", "Heart Nebula"]
    assert w[0][1] == NOW and w[0][2] == w[1][1] == w[0][3]
    assert w[1][3] is None and w[1][2] - w[1][1] > 9 * H
    assert info["deep_pick"]["fallback"] is None


def test_pick_done_early_falls_back_to_split(tmp_path):
    goals = _goals()
    ts, info = _plan(_cfg(tmp_path), goals, night_mode="deep",
                     deep_target=_id(goals, "Andromeda Galaxy"))
    pick = info["deep_pick"]
    assert pick["target"] == "Andromeda Galaxy"
    assert "done" in pick["fallback"] and "split after" in pick["fallback"]
    w = _windows(ts)
    assert w[0][0] == "Andromeda Galaxy" and w[0][3] == w[0][2]
    # 1.4 h owed + overhead, then the others share the rest
    assert (w[0][2] - w[0][1]) == pytest.approx(1.4 * 1.15 * H, abs=timedelta(minutes=2))
    assert {x[0] for x in w[1:]} == {"NGC 604", "Heart Nebula"}
    for a, b in zip(w, w[1:]):
        assert a[2] == b[1]                   # back to back
    assert w[-1][3] is None


def test_pick_that_sets_before_dawn_falls_back_to_split(tmp_path):
    goals = _goals(activate=("Crescent Nebula",))
    ts, info = _plan(_cfg(tmp_path), goals, night_mode="deep",
                     deep_target=_id(goals, "Crescent Nebula"))
    pick = info["deep_pick"]
    assert "sets" in pick["fallback"]
    w = _windows(ts)
    assert w[0][0] == "Crescent Nebula" and w[0][1] == NOW
    assert w[0][2] < NOW + 5 * H              # the Crescent sets mid-night
    assert len(w) >= 3 and w[-1][2] > NOW + 9 * H
    assert pick["split_targets"] == [x[0] for x in w[1:]]


def test_moon_rule_bright_moon_makes_broadband_goals_ineligible(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    mw = moon.moon_window_tonight(cfg, now=NOW)
    bright = dict(mw, set_utc=None, set_local=None, illum_pct=60)
    monkeypatch.setattr(moon, "moon_window_tonight", lambda c, now=None: bright)
    goals = _goals(activate=("Crescent Nebula",))
    ts, info = _plan(cfg, goals, night_mode="deep")
    rows = {s["target"]: s for s in info["deep_scores"]}
    for name in ("NGC 604", "Heart Nebula", "Andromeda Galaxy"):
        assert not rows[name]["eligible"] and "moon" in rows[name]["why"]
    # the Crescent owes narrowband: picked, its broadband share discounted
    assert info["deep_pick"]["target"] == "Crescent Nebula"
    assert 0 < rows["Crescent Nebula"]["components"]["moon"]["norm"] < 1
    assert [t.name for t in ts] == ["Crescent Nebula"]


def test_moon_rule_a_moon_setting_soon_after_dusk_allows_broadband(tmp_path, monkeypatch):
    """The same 20% moon that does NOT set soon defers broadband: then no
    goal tonight is eligible and the night plans split (and empty, as the
    split planner skips broadband-only goals too)."""
    cfg = _cfg(tmp_path)
    mw = moon.moon_window_tonight(cfg, now=NOW)
    late = dict(mw, set_utc=mw["dusk_utc"] + 3 * H)
    monkeypatch.setattr(moon, "moon_window_tonight", lambda c, now=None: late)
    ts, info = _plan(cfg, night_mode="deep")
    assert info["deep_pick"] is None and "split" in info["deep_note"]
    assert not any(s["eligible"] for s in info["deep_scores"])
    monkeypatch.setattr(moon, "moon_window_tonight", lambda c, now=None: mw)
    _ts, info = _plan(cfg, night_mode="deep")
    assert all(s["eligible"] for s in info["deep_scores"])


def test_piggy_driven_goal_without_an_rc16_plan(tmp_path):
    goals = _goals()
    n604 = next(p for p in goals if p.target.name == "NGC 604")
    n604.exposure_plans = [e for e in n604.exposure_plans if e.rig != "rc16"]
    # split: a Piggy-only goal stays off the RC16 sequence (PS-134)
    ts, _ = _plan(_cfg(tmp_path), [p.model_copy(deep=True) for p in goals])
    assert "NGC 604" not in [t.name for t in ts]
    # deep: the mount points at it all night for the Piggy-600, the RC16
    # shoots bonus subs of the type's default plan
    ts, info = _plan(_cfg(tmp_path), goals, night_mode="deep",
                     deep_target=n604.id)
    pick = info["deep_pick"]
    assert pick["piggy_driven"] and not pick["has_rc16_plan"]
    assert [t.name for t in ts] == ["NGC 604"]
    t = ts[0]
    assert t.driving_rig == "piggyback" and t.handoff_utc is None
    assert [e.filter_type.value for e in t.exposures] == ["Ha"]
    assert t.exposures[0].rig == "rc16" and t.exposures[0].count >= 1
    # auto pick: still eligible on the Piggy's owed hours
    s = dn.score_goal(n604, {"rise_time": NOW, "set_time": NOW + 8 * H,
                             "hours": 8.0, "transit_time": NOW + 4 * H},
                      NOW, NOW + 9.8 * H, NOW, NOW + 9.8 * H)
    assert s["eligible"] and s["components"]["fill"]["value"]["rc16_owed_h"] == 0


# --- the tonight-only override ---------------------------------------------------------

def test_override_is_for_tonight_only_and_cleared_at_dawn(tmp_path):
    cfg = _cfg(tmp_path)
    goals = _goals()
    heart = _id(goals, "Heart Nebula")
    dn.set_override(cfg, NIGHT, "deep", heart, now=NOW)
    ts, info = _plan(cfg, goals)
    assert info["mode"] == "deep" and info["mode_source"] == "tonight"
    assert info["night"] == NIGHT
    assert info["deep_pick"]["target"] == "Heart Nebula"
    # the next night does not see it
    _ts, info2 = _plan(cfg, _goals(), now=NOW + 24 * H)
    assert info2["mode"] == "split" and info2["night"] == "2026-10-15"
    # the dawn shutdown clears it
    assert dn.clear_at_dawn(cfg, NIGHT) == "night mode override (deep) cleared"
    assert dn.override(cfg, NIGHT) is None
    _ts, info3 = _plan(cfg, _goals())
    assert info3["mode"] == "split"
    assert dn.clear_at_dawn(cfg, NIGHT) is None


def test_override_split_beats_a_deep_config_and_default_removes_it(tmp_path):
    cfg = _cfg(tmp_path, plan_night_mode="deep")
    dn.set_override(cfg, NIGHT, "split", now=NOW)
    assert dn.resolve(cfg, NIGHT) == ("split", None, "tonight")
    dn.set_override(cfg, NIGHT, "default", now=NOW)
    assert dn.resolve(cfg, NIGHT) == ("deep", None, "config")
    with pytest.raises(ValueError):
        dn.set_override(cfg, NIGHT, "sideways")


def test_api_tonight_mode_post_and_get(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import night_mode as r
    cfg = _cfg(tmp_path)
    goals = {p.id: p for p in _goals()}
    heart = _id(goals.values(), "Heart Nebula")
    monkeypatch.setattr(app, "_config", cfg)
    monkeypatch.setattr(app, "_stored_projects", lambda: goals)
    monkeypatch.setattr(r, "_tonight", lambda c, now=None: NIGHT)
    out = asyncio.run(r.api_set_tonight_mode({"mode": "deep", "target": heart}))
    assert out["ok"] and out["mode"] == "deep" and out["source"] == "tonight"
    assert out["target_name"] == "Heart Nebula"
    got = asyncio.run(r.api_tonight_mode())
    assert got["mode"] == "deep" and len(got["goals"]) == 3
    bad = asyncio.run(r.api_set_tonight_mode({"mode": "deep", "target": "nope"}))
    assert bad.status_code == 404
    bad = asyncio.run(r.api_set_tonight_mode({"mode": "split", "target": heart}))
    assert bad.status_code == 400
    out = asyncio.run(r.api_set_tonight_mode({"mode": "default"}))
    assert out["mode"] == "split" and out["source"] == "config"


def test_api_tonight_carries_mode_and_deep_pick(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path, plan_night_mode="deep")
    goals = {p.id: p for p in _goals()}
    monkeypatch.setattr(app, "_config", cfg)
    monkeypatch.setattr(app, "_stored_projects", lambda: goals)

    class _DT(datetime):
        @classmethod
        def utcnow(cls):
            return NOW
    monkeypatch.setattr(app, "datetime", _DT)
    body = TestClient(app.app).get("/api/tonight").json()
    assert body["mode"] == "deep" and body["deep_pick"]["target"] == "NGC 604"
    assert set(body["deep_pick"]["components"]) == set(dn.WEIGHTS)
    assert body["targets"][0]["window_end_utc"].endswith("Z")


# --- allocate_deep ------------------------------------------------------------------

T0 = datetime(2026, 10, 15, 2, 0)
T1 = T0 + 10 * H


def _e(name, prio=50, need_h=20.0, up_from=None, up_until=None):
    return {"name": name, "priority": prio, "need_s": need_h * 3600,
            "up_from": up_from, "up_until": up_until}


def test_allocate_deep_splits_before_the_pick_rises():
    a = tp.allocate_deep([_e("A"), _e("B"), _e("P", up_from=T0 + 3 * H)],
                         "P", T0, T1)
    assert a["pick_window"] == (T0 + 3 * H, T1)
    before = sorted((w for n, w in a["windows"].items() if n != "P"))
    assert before[0][0] == T0 and before[-1][1] == T0 + 3 * H
    assert a["handoff"]["A"] and a["handoff"]["B"] and "P" not in a["handoff"]
    assert a["fallback"] is None


def test_allocate_deep_keeps_the_pick_up_when_nothing_else_can_use_the_rest():
    a = tp.allocate_deep([_e("P", need_h=1.0)], "P", T0, T1)
    assert a["pick_window"] == (T0, T1) and a["fallback"] is None


def test_allocate_deep_none_when_the_pick_has_too_little_time():
    assert tp.allocate_deep([_e("A"), _e("P", up_until=T0 + 0.25 * H)],
                            "P", T0, T1) is None


# --- morning report -----------------------------------------------------------------

def test_morning_line_reports_both_rigs_on_the_pick(tmp_path):
    cfg = _cfg(tmp_path)
    dn.record_dispatch(cfg, NIGHT, {"mode": "deep",
                                    "deep_pick": {"target": "NGC 604"}})
    rigs = [{"rig": "rc16", "lights": {"hours": [
                {"target": "NGC 604", "hours": 6.2},
                {"target": "Heart Nebula", "hours": 0.5}]}},
            {"rig": "piggyback", "lights": {"hours": [
                {"target": "NGC 604", "hours": 8.4}]}}]
    assert dn.morning_line(cfg, NIGHT, rigs) == \
        "Deep night on NGC 604: 6.2 h RC16, 8.4 h Piggy"
    from photonscript.scheduler.morning_report import (PRI_RIG,
                                                       card_line_priority,
                                                       card_lines)
    assert card_line_priority("Deep night on NGC 604: 6.2 h RC16, 8.4 h Piggy") == PRI_RIG
    assert card_lines({"date": NIGHT, "rigs": [],
                       "deep_night": "Deep night on X: 1.0 h RC16, 2.0 h Piggy"}
                      )[0].startswith("Deep night on X")
    dn.record_dispatch(cfg, NIGHT, {"mode": "split"})
    assert dn.morning_line(cfg, NIGHT, rigs) is None


def test_new_sources_are_ascii():
    root = Path(__file__).resolve().parents[2]
    for p in ("photonscript/scheduler/deep_night.py",
              "photonscript/scheduler/routers/night_mode.py",
              "tests/test_scheduler/test_deep_night.py"):
        text = (root / p).read_text(encoding="ascii")
        assert chr(0x2014) not in text
