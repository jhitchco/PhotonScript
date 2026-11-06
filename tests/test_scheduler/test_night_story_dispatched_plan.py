"""2026-10-26: the evening heads-up and "imaging now" follow the plan that
is actually loaded, not the arm's snapshot.

2026-10-25: auto-armed 22:24Z with no target tonight; Heart Nebula's goal
was patched at ~22:50Z (filter_mix Ha:100). The 23:53Z heads-up still said
"no targets planned" and "Piggy-600: OSC 450s" (the arm's plan), and at
astro dark "Imaging now (19:53): RC16 unguided on ?. ... 0 target(s) until
06:09, ~0.0 h planned." while the dispatch at 01:23Z had loaded Heart
Nebula (Ha 180 s). The dispatch now rebuilds the plan's target view from
the targets it loaded, and a goal changed while ARMED re-plans the evening
(and re-sends the heads-up when it changed).

Fixture: the 10-14 goal store (tests/test_scheduler/fixtures/deep_night),
as in test_dispatch_gate.
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import armer as armer_mod
from photonscript.scheduler import moon
from photonscript.scheduler import night_story as ns
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler.armer import Armer
from photonscript.shared import pushover as po
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ImagingProject

FIX = Path(__file__).parent / "fixtures" / "deep_night" / "projects2_2026-10-14.json"
NOW = datetime(2026, 10, 15, 0, 30)      # 18:30 MDT, an hour before dusk
NIGHT = "2026-10-14"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _det(monkeypatch):
    monkeypatch.setattr(nsj, "_moon_window",
                        lambda: {"available": True, "down_at_dusk": True})
    moon._cache.clear()
    yield
    moon._cache.clear()


class _FrozenDT(datetime):
    @classmethod
    def utcnow(cls):
        return NOW


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"),
                              observatory_tz="America/Denver", **kw)


def _armer(tmp_path, monkeypatch, goals, **cfg):
    from photonscript.scheduler import app as app_mod

    class _Store:
        projects = {p.id: p for p in goals}
    monkeypatch.setattr(app_mod, "_store", _Store())
    monkeypatch.setattr(armer_mod, "datetime", _FrozenDT)
    monkeypatch.chdir(tmp_path)
    a = Armer(_cfg(tmp_path, **cfg))
    # the arm's plan: nothing planned tonight (2026-10-25 at 22:24Z)
    a.plan = {"night_of": NIGHT, "preconfig_utc": "2026-10-15T01:00:00Z",
              "dusk_utc": "2026-10-15T01:30:00Z", "dawn_utc": "2026-10-15T11:30:00Z",
              "dark_hours": 10.0, "targets": [], "schedule": [],
              "stats": {"dark_hours": 10.0, "targets": 0, "planned_subs": 0,
                        "est_integration_h": 0.0, "est_shutter_pct": 71}}
    a.guiding_override = "unguided"
    monkeypatch.setattr(a, "_calibration_slot", lambda targets, now: None)
    return a


def _goals():
    return [ImagingProject(**p) for p in json.loads(FIX.read_text())]


def _by(goals, name):
    return next(p for p in goals if p.target.name == name)


def test_dispatch_rebuilds_the_plan_view_from_the_loaded_targets(tmp_path, monkeypatch):
    a = _armer(tmp_path, monkeypatch, _goals())
    assert a._dispatch() is True, a.detail
    names = a.last_dispatch_targets
    assert names
    assert a.plan["targets"] == [n for n in names
                                 if n in {r["name"] for r in a.plan["schedule"]}]
    assert a.plan["targets"], a.plan["schedule"]
    assert a.plan["stats"]["est_integration_h"] > 0
    assert a.plan["stats"]["est_shutter_pct"] == 71      # kept
    assert a.plan["view_source"]["source"] == "dispatch"
    first = a.plan["schedule"][0]
    assert first["filters"] and first["window_start_utc"]
    # "imaging now" names the loaded target and its sets
    text = ns.imaging_now_text(a.config, a.plan, guided=False,
                               now=datetime(2026, 10, 15, 1, 31))
    assert "on ?" not in text and "0 target(s)" not in text
    assert f"RC16 unguided on {first['name']} (" in text
    assert "~0.0 h planned" not in text
    # the plan snapshot the runs page reads carries the same view
    snap = tmp_path / "data" / "runs"
    assert any(snap.glob(f"{NIGHT}*"))


def test_goal_changed_after_the_arm_replans_the_evening(tmp_path, monkeypatch):
    goals = _goals()
    a = _armer(tmp_path, monkeypatch, goals)
    a.plan["goals_fp"] = "stale"          # built from other goals
    assert run(a._refresh_plan_if_goals_changed(NOW)) is True
    assert a.plan["targets"], "re-plan found tonight's targets"
    assert a.plan["schedule"] and a.plan["view_source"]["source"] == "replan"
    assert a.plan["goals_fp"] == armer_mod.goals_fingerprint()
    assert "piggy_length" in a.plan
    # unchanged goals: no second re-plan
    assert run(a._refresh_plan_if_goals_changed(NOW)) is False
    # a goal patched (filter mix) re-plans again
    _by(goals, "Heart Nebula").filter_mix = {"Ha": 100}
    assert run(a._refresh_plan_if_goals_changed(NOW)) is True


def test_goals_fingerprint_moves_with_a_patch_and_ignores_inactive(tmp_path, monkeypatch):
    goals = _goals()
    _armer(tmp_path, monkeypatch, goals)
    fp = armer_mod.goals_fingerprint()
    assert fp and fp == armer_mod.goals_fingerprint()
    _by(goals, "Heart Nebula").filter_mix = {"Ha": 100}
    fp2 = armer_mod.goals_fingerprint()
    assert fp2 != fp
    _by(goals, "NGC 604").active = False
    assert armer_mod.goals_fingerprint() != fp2
    # an inactive goal's edits do not count
    fp3 = armer_mod.goals_fingerprint()
    _by(goals, "Crescent Nebula").priority = 99
    assert armer_mod.goals_fingerprint() == fp3


# --- the heads-up ------------------------------------------------------------

@pytest.fixture
def sent(monkeypatch):
    out: list[dict] = []

    async def fake(config, message, title, priority, sound, **kw):
        out.append({"title": title, "message": message})
        return True
    monkeypatch.setattr(po, "_send_raw", fake)
    monkeypatch.setattr(po, "_is_daytime", lambda c, when=None: False)
    monkeypatch.setattr(moon, "moon_window_tonight", lambda c, now=None: {"available": False})
    monkeypatch.setattr(ns, "_owed_line", lambda c: None)
    return out


class _A:
    def __init__(self, cfg, plan):
        self.config, self.plan = cfg, plan
        self.recipe, self.tpoint_pf, self.protrack_state = None, None, "on"

    def _use_guiding(self):
        return False


def _hplan(now, schedule, **kw):
    dusk = now + timedelta(hours=2)
    p = {"night_of": NIGHT, "preconfig_utc": (dusk - timedelta(minutes=30)).isoformat() + "Z",
         "dusk_utc": dusk.isoformat() + "Z",
         "dawn_utc": (dusk + timedelta(hours=10)).isoformat() + "Z",
         "dark_hours": 10.3, "targets": [r["name"] for r in schedule],
         "schedule": schedule}
    p.update(kw)
    return p


def _heart(dusk, sets):
    return {"name": "Heart Nebula", "fill": False,
            "window_start_utc": dusk.isoformat() + "Z",
            "window_end_utc": (dusk + timedelta(hours=10)).isoformat() + "Z",
            "filters": sets}


def test_plan_signature_changes_with_the_sets():
    dusk = datetime(2026, 10, 26, 1, 53)
    a = {"night_of": NIGHT, "schedule": [_heart(dusk, [{"f": "Ha", "n": 80, "exp": 300}])]}
    b = {"night_of": NIGHT, "schedule": [_heart(dusk, [{"f": "Ha", "n": 160, "exp": 180}])]}
    assert ns.plan_signature(a, False) != ns.plan_signature(b, False)
    assert ns.plan_signature(a, False) == ns.plan_signature(dict(a), False)


def test_headsup_goes_out_again_when_the_plan_changes_before_preconfig(tmp_path, sent):
    cfg = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                             observatory_tz="America/Denver",
                             piggyback_enabled=True, piggyback_exposure_s=450)
    now = datetime.utcnow()
    empty = _hplan(now, [])
    armer = _A(cfg, empty)
    assert run(ns.maybe_plan_headsup(armer, now)) is True
    assert "no targets planned" in sent[-1]["message"]
    assert "OSC 450s" in sent[-1]["message"]
    # the goal patched; the armer re-planned (Heart, Ha 180 s, Piggy 180 s)
    dusk = now + timedelta(hours=2)
    armer.plan = _hplan(now, [_heart(dusk, [{"f": "Ha", "n": 160, "exp": 180}])],
                        piggy_length={"targets": [{"target": "Heart Nebula", "exp_s": 180}]})
    assert run(ns.maybe_plan_headsup(armer, now + timedelta(minutes=10))) is True
    msg = sent[-1]["message"]
    assert "Heart Nebula" in msg and "Ha 160x180s" in msg
    assert "OSC 180s" in msg and "no targets planned" not in msg
    # the same plan again: no third push
    assert run(ns.maybe_plan_headsup(armer, now + timedelta(minutes=20))) is False
