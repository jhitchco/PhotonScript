"""Moon fill (2026-10-25): a bright-moon night is never left empty.

2026-10-25, 100% moon up all night, armed unguided: GET /api/nightplan had
zero targets. The NB moon tag deferred all broadband, the unguided night
removed RC16 narrowband (rc16_narrowband_requires_guided), every active goal
was LRGB only and the Piggy-600 OSC goals of M31 and NGC 604 were met, so
neither rig had a pointing. The moon fill (plan_moon_piggy_fill, default on)
makes it a Piggy-driven night: pointing carriers (RC16 Ha) on the best goals
for the whole dark window, Piggy OSC owed first, away from the moon.

Fixture: the live goal store (GET /api/projects2, 2026-10-25, read-only,
trimmed to the planner's fields) with the Heart Nebula put back to its
LRGB mix before that night's live patch (filter_mix Ha:100, osc 20 h): RC16
L/R/G/B 96/48/48/48 x 120 s, OSC 360 x 120 s with 32040 s done (3.1 h owed).
M31 and NGC 604 OSC are met.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import moon
from photonscript.scheduler import moon_fill as mf
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import target_planner as tp
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ImagingProject

FIX = Path(__file__).parent / "fixtures" / "moon_fill" / "projects2_2026-10-25.json"
NOW = datetime(2026, 10, 26, 1, 0)          # 19:00 MDT 10-25, before dusk
H = timedelta(hours=1)


@pytest.fixture(autouse=True)
def _det_moon(monkeypatch):
    monkeypatch.setattr(nsj, "_moon_window", lambda: {})
    moon._cache.clear()
    yield
    moon._cache.clear()


def _cfg(tmp_path, **kw):
    kw.setdefault("piggyback_enabled", True)
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _goals():
    return [ImagingProject(**p) for p in json.loads(FIX.read_text())]


def _goal(goals, name):
    return next(p for p in goals if p.target.name == name)


def _plan(cfg, goals=None, now=NOW, **kw):
    info = {}
    ts = tp.plan_night_sequence(goals or _goals(), cfg, now, info=info, **kw)
    return ts, info


def _dark(cfg, now=NOW):
    mw = moon.moon_window_tonight(cfg, now=now)
    return mw["dusk_utc"], mw["dawn_utc"]


def _covered_h(ts):
    return sum((t.window_end_utc - t.window_start_utc).total_seconds()
               for t in ts) / 3600


# --- the 2026-10-25 night ------------------------------------------------------

def test_tonights_moon_is_full_and_defers_broadband(tmp_path):
    mw = moon.moon_window_tonight(_cfg(tmp_path), now=NOW)
    assert mw["illum_pct"] >= 95 and not mw["down_at_dusk"]
    assert moon.broadband_deferred(mw)


def test_without_the_fill_the_night_is_empty(tmp_path):
    """The bug, kept as a record."""
    ts, info = _plan(_cfg(tmp_path, plan_moon_piggy_fill=False), guided=False)
    assert ts == [] and info["moon_fill"] is None


def test_2026_10_25_unguided_is_a_full_piggy_driven_night(tmp_path):
    cfg = _cfg(tmp_path)
    ts, info = _plan(cfg, guided=False)
    dusk, dawn = _dark(cfg)
    assert ts, "the moon fill must plan the night"
    assert all(t.carrier for t in ts)
    # the Heart: its Piggy OSC still owes 3.1 h and it is far from the moon
    assert ts[0].name == "Heart Nebula"
    assert _covered_h(ts) >= 0.9 * (dawn - dusk).total_seconds() / 3600
    assert ts[0].window_start_utc - dusk < 0.5 * H
    assert dawn - ts[-1].window_end_utc < 0.5 * H
    assert ts[-1].handoff_utc is None          # keeps the mount to the loop end
    for t in ts:
        assert [e.filter_type.value for e in t.exposures] == ["Ha"]
        e = t.exposures[0]
        assert e.rig == "rc16" and e.gain == 200 and e.offset == 256
        # the whole window, not budget x filter share
        span = (t.window_end_utc - t.window_start_utc).total_seconds()
        assert e.count * e.exposure_seconds >= 0.8 * span
        assert not t.start_guiding
        assert "pointing carrier" in t.carrier_note
        assert "expected to be rejected" in t.carrier_note
    fill = info["moon_fill"]
    assert fill["active"] and fill["filled_pct"] >= 90 and fill["used_pct"] == 0
    assert "100% moon defers broadband" in fill["reason"]
    assert "nothing planned" in fill["reason"]
    row = fill["carriers"][0]
    assert row["target"] == "Heart Nebula" and row["piggy_owed_h"] == pytest.approx(3.1, abs=0.05)
    assert row["moon_sep_deg"] >= 30 and row["expect_rejected"]
    assert {s.split(":")[0] for s in fill["skipped"]} == {
        "Andromeda Galaxy", "Heart Nebula", "NGC 604"}
    assert info["moon_fill_note"].startswith("Moon fill (Piggy-driven night)")
    assert "Heart Nebula" in info["moon_fill_note"]
    assert chr(0x2014) not in info["moon_fill_note"]     # no em dash


def test_guided_the_carrier_is_bonus_ha(tmp_path):
    ts, info = _plan(_cfg(tmp_path), guided=True)
    assert ts and ts[0].name == "Heart Nebula" and ts[0].start_guiding
    assert "bonus data" in ts[0].carrier_note
    assert not info["moon_fill"]["carriers"][0]["expect_rejected"]


def test_all_piggy_goals_met_still_fills_the_night(tmp_path):
    """A met OSC goal is no reason to idle the Piggy-600: the best goal by
    moon separation / hours up carries."""
    goals = _goals()
    heart = _goal(goals, "Heart Nebula")
    for e in heart.exposure_plans:
        if e.rig != "rc16":
            e.acquired, e.acquired_s = e.count, e.count * e.exposure_seconds
    cfg = _cfg(tmp_path)
    ts, info = _plan(cfg, goals, guided=False)
    dusk, dawn = _dark(cfg)
    assert ts and all(t.carrier for t in ts)
    assert _covered_h(ts) >= 0.9 * (dawn - dusk).total_seconds() / 3600
    assert all(r["piggy_owed_h"] == 0 for r in info["moon_fill"]["carriers"])
    assert "OSC goal met" in ts[0].carrier_note


def test_moon_separation_beats_osc_owed(tmp_path):
    """NGC 604 owes OSC but sits near the full moon: the Heart (met, far
    from the moon) carries while it is up."""
    goals = _goals()
    cfg = _cfg(tmp_path)
    dusk, dawn = _dark(cfg)
    m = mf.moon_radec(cfg, dusk + (dawn - dusk) / 2)
    n604 = _goal(goals, "NGC 604")
    heart = _goal(goals, "Heart Nebula")
    assert mf.separation_deg(n604.target.ra_hours, n604.target.dec_degrees, m) < 30
    assert mf.separation_deg(heart.target.ra_hours, heart.target.dec_degrees, m) >= 30
    for e in n604.exposure_plans:
        if e.rig != "rc16":
            e.acquired, e.acquired_s = 0, 0.0
    for e in heart.exposure_plans:
        if e.rig != "rc16":
            e.acquired, e.acquired_s = e.count, e.count * e.exposure_seconds
    ts, _ = _plan(cfg, goals, guided=False)
    assert ts[0].name == "Heart Nebula"
    assert "NGC 604" not in [t.name for t in ts if t.window_start_utc < ts[0].window_end_utc]


def test_near_moon_goal_used_when_nothing_else_is_up(tmp_path):
    goals = [p for p in _goals() if p.target.name == "NGC 604"]
    ts, info = _plan(_cfg(tmp_path), goals, guided=False)
    assert [t.name for t in ts] == ["NGC 604"]
    assert info["moon_fill"]["carriers"][0]["moon_sep_deg"] < 30


def test_switch_off_and_piggy_off(tmp_path):
    ts, info = _plan(_cfg(tmp_path, plan_moon_piggy_fill=False), guided=False)
    assert ts == [] and info["moon_fill_note"] is None
    ts, info = _plan(_cfg(tmp_path, piggyback_enabled=False), guided=False)
    assert ts == [] and info["moon_fill"] is None


def test_config_keys_and_system_page():
    c = PhotonScriptConfig(_env_file=None)
    assert c.plan_moon_piggy_fill is True
    assert c.plan_moon_fill_min_used_pct == 50.0
    assert c.plan_moon_fill_min_sep_deg == 30.0
    assert c.plan_moon_fill_carrier_s == 300.0
    from photonscript.scheduler.app import _CONFIG_FIELDS
    names = {f[0] for f in _CONFIG_FIELDS}
    assert {"plan_moon_piggy_fill", "plan_moon_fill_min_used_pct",
            "plan_moon_fill_min_sep_deg", "plan_moon_fill_carrier_s"} <= names


# --- the rule's pieces -----------------------------------------------------------

def test_bright_moon():
    assert mf.bright_moon("NB", {})
    assert not mf.bright_moon("BB", {"available": True, "down_at_dusk": True})
    assert mf.bright_moon("NB+OIII", {"available": True, "down_at_dusk": False,
                                      "set_utc": None, "illum_pct": 40})
    assert not mf.bright_moon(None, None)


def test_gaps():
    t0 = datetime(2026, 10, 26, 2)
    g = mf.gaps([(t0 + H, t0 + 3 * H), (t0 + 2 * H, t0 + 4 * H)], t0, t0 + 8 * H, 1800)
    assert g == [(t0, t0 + H), (t0 + 4 * H, t0 + 8 * H)]
    assert mf.gaps([], t0, t0 + H, 1800) == [(t0, t0 + H)]
    assert mf.gaps([(t0 + 10 * timedelta(minutes=1), t0 + H)], t0, t0 + H, 1800) == []


class _P:
    def __init__(self, name, prio=50):
        self.priority = prio
        self.target = type("T", (), {"name": name})()


def test_choose_chains_carriers_over_the_gap():
    t0 = datetime(2026, 10, 26, 2)
    a = {"project": _P("A"), "lo": t0, "hi": t0 + 4 * H, "sep": 60, "osc_owed_s": 3600}
    b = {"project": _P("B"), "lo": t0, "hi": t0 + 10 * H, "sep": 50, "osc_owed_s": 0}
    c = {"project": _P("C"), "lo": t0, "hi": t0 + 10 * H, "sep": 10, "osc_owed_s": 9e4}
    picks = mf.choose([a, b, c], [(t0, t0 + 10 * H)], 1800, 30)
    assert [(p[0]["project"].target.name, p[1], p[2]) for p in picks] == [
        ("A", t0, t0 + 4 * H), ("B", t0 + 4 * H, t0 + 10 * H)]


def test_a_well_used_plan_is_left_alone(tmp_path):
    """Windows over plan_moon_fill_min_used_pct: no fill."""
    from photonscript.shared.models import NinaSequenceTarget
    cfg = _cfg(tmp_path)
    dusk, dawn = _dark(cfg)
    from photonscript.shared.astronomy import get_twilight_times
    from photonscript.shared.localtime import night_base_date
    tw = get_twilight_times(cfg.get_observatory(), night_base_date(cfg, NOW))
    t = NinaSequenceTarget(name="X", ra_hours=1, dec_degrees=40,
                           window_start_utc=dusk, window_end_utc=dusk + (dawn - dusk) * 0.7)
    info = {}
    out = mf.apply([t], projects=_goals(), visible_projects=[], config=cfg,
                   date_utc=NOW, twilight=tw, dark_start=dusk, dark_end=dawn,
                   moon_tag="NB", moon_window={}, guided=False, skipped={}, info=info)
    assert out == [t] and info["moon_fill"] is None
    # under the threshold the gap after it is filled, and X hands off
    t.window_end_utc = dusk + (dawn - dusk) * 0.3
    out = mf.apply([t], projects=_goals(), visible_projects=[], config=cfg,
                   date_utc=NOW, twilight=tw, dark_start=dusk, dark_end=dawn,
                   moon_tag="NB", moon_window={}, guided=False, skipped={}, info=info)
    assert out[0] is t and t.handoff_utc == t.window_end_utc
    assert len(out) > 1 and all(o.carrier for o in out[1:])
    assert out[1].window_start_utc >= t.window_end_utc
    assert info["moon_fill"]["used_pct"] == pytest.approx(30, abs=1)


# --- the sequence and the surfaces ---------------------------------------------------

def test_the_carrier_night_generates_and_lints(tmp_path, monkeypatch):
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    from photonscript.scheduler.sequence_lint import lint
    cfg = _cfg(tmp_path)
    mw = moon.moon_window_tonight(cfg, now=NOW)
    monkeypatch.setattr(nsj, "_moon_window", lambda: mw)
    ts, _ = _plan(cfg, guided=False)
    text = nsj.generate_nina_json(build_sequence_for_night("MoonFill", ts))
    res = lint(json.loads(text))
    assert res.ok, [f.detail for f in res.findings if f.level == "ERROR"]
    assert "pointing carrier for the Piggy-600" in text
    assert "Heart Nebula" in text


def test_heads_up_names_the_piggy_driven_night(tmp_path):
    from photonscript.scheduler.night_story import plan_headsup_text
    cfg = _cfg(tmp_path)
    plan = {"night_of": "2026-10-25", "dark_hours": 10.3,
            "preconfig_utc": "2026-10-26T01:23:00Z", "dusk_utc": "2026-10-26T01:53:00Z",
            "dawn_utc": "2026-10-26T12:09:00Z",
            "schedule": [{"name": "Heart Nebula", "carrier": True,
                          "window_start_utc": "2026-10-26T02:03:00Z",
                          "window_end_utc": "2026-10-26T12:09:00Z",
                          "filters": [{"f": "Ha", "n": 104, "exp": 300}]}],
            "moon_fill_note": "Moon fill (Piggy-driven night): 100% moon defers broadband"}
    text = plan_headsup_text(cfg, plan, guided=False)
    assert "Piggy carrier" in text and "Moon fill (Piggy-driven night)" in text


def test_api_tonight_and_nightplan_show_the_fill(tmp_path, monkeypatch):
    from datetime import datetime as _datetime

    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    from photonscript.scheduler import night_plan
    cfg = _cfg(tmp_path)
    goals = {p.id: p for p in _goals()}
    monkeypatch.setattr(app, "_config", cfg)
    monkeypatch.setattr(app, "_stored_projects", lambda: goals)

    class _DT(_datetime):
        @classmethod
        def utcnow(cls):
            return NOW
    monkeypatch.setattr(app, "datetime", _DT)
    monkeypatch.setattr(night_plan, "datetime", _DT)

    class _Arm:
        def _use_guiding(self):
            return False
    monkeypatch.setattr(app, "_armer", _Arm())
    body = TestClient(app.app).get("/api/tonight").json()
    assert body["moon_fill"]["active"] and body["moon_fill_note"]
    assert body["targets"][0]["name"] == "Heart Nebula"
    assert body["targets"][0]["carrier"] is True
    assert "pointing carrier" in body["targets"][0]["carrier_note"]

    class _Store:
        projects = goals
    monkeypatch.setattr(app, "get_store", lambda: _Store())
    plan = night_plan.build_night_plan(cfg)
    assert plan["moon_fill"]["active"] and plan["moon_fill_note"]
    assert plan["schedule"][0]["carrier"] is True
    assert "pointing carrier" in plan["schedule"][0]["kind"]
