"""PS-118: goal progress is seconds of integration, not a count of subs.

The M31 Piggy-600 goal (6 h of 120 s OSC subs) read "159 / 180 x 120s, 5.3 /
6 h" with 159 subs approved for 10.77 h: every 300 s and 400 s sub was
credited as one 120 s plan sub, because PS-66 credited a sub its own length
only on unguided, capped RC16 nights. Every long sub on both rigs is now
credited its own seconds, live and in sync_goal_progress; `acquired` is the
whole plan subs' worth of those seconds, so the planner's "still owed" stays
count - acquired (PS-105) and HDR shorts keep their own set (PS-47/PS-63).
"""
import json
from datetime import datetime

import pytest

from photonscript.scheduler import runs
from photonscript.scheduler.campaign import plan_seconds
from photonscript.scheduler.project_store import ProjectStore
from photonscript.scheduler.target_planner import plan_night_sequence
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (CelestialTarget, ExposurePlan,
                                        FilterType, ImagingProject)

M31 = CelestialTarget(name="Andromeda Galaxy", catalog_id="M 31",
                      ra_hours=0.712, dec_degrees=41.27, object_type="galaxy")
HEART = CelestialTarget(name="Heart Nebula", catalog_id="IC 1805",
                        ra_hours=2.55, dec_degrees=61.5,
                        object_type="emission nebula")
NIGHT = datetime(2026, 10, 2, 20)


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _m31_store(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    p = store.add_from_target(M31, budget_hours=6)
    store.update(p.id, osc_hours=6, drop_rc16=True)
    monkeypatch.setattr(app, "_store", store)
    osc = p.exposure_plans[0]
    assert (osc.rig, osc.filter_type.value, osc.exposure_seconds,
            osc.count) == ("piggyback", "OSC", 120, 180)
    return cfg, store, p


def _write_night(cfg, date, rows):
    rd = runs.runs_dir(cfg)
    rd.mkdir(parents=True, exist_ok=True)
    with open(rd / f"{date}_subs.jsonl", "w", encoding="utf-8") as fh:
        for i, (exp, ok) in enumerate(rows):
            fh.write(json.dumps({
                "rig": "piggyback", "file": f"LIGHT/{date}_{i:03d}.fits",
                "time": f"{date}T03:00:00", "target": "Andromeda Galaxy",
                "filter": "OSC", "exp_s": exp, "passed_qa": ok,
                "reviewed": ok, "reason": ""}) + "\n")


def _m31_history(cfg, n120=88):
    # 09-21: 120 s subs (plus a few rejected); 10-03: 2 x 300 s + 69 x 400 s
    _write_night(cfg, "2026-09-21", [(120, True)] * n120 + [(120, False)] * 4)
    _write_night(cfg, "2026-10-03", [(300, True)] * 2 + [(400, True)] * 69
                 + [(400, False)] * 11)


# ---- the M31 report ---------------------------------------------------------

def test_m31_mixed_lengths_sync_to_seconds(tmp_path, monkeypatch):
    from photonscript.scheduler import sub_index
    from photonscript.scheduler.app import _project_json
    cfg, store, p = _m31_store(tmp_path, monkeypatch)
    _m31_history(cfg)
    assert runs.sync_goal_progress(cfg) == ["Andromeda Galaxy"]
    osc = p.exposure_plans[0]
    # 88 x 120 + 2 x 300 + 69 x 400 = 38760 s = 10.77 h (the Targets totals)
    assert osc.acquired_s == 88 * 120 + 2 * 300 + 69 * 400 == 38760
    assert osc.acquired == 180                    # capped at the goal count
    # Targets page goal: 10.8 of 6 h, 100% (was 5.3 h, 88%)
    g = sub_index._goal(p)
    assert (g["hours_done"], g["hours_goal"], g["pct"]) == (10.8, 6.0, 100)
    assert g["plans"][0]["hours_done"] == 10.77
    assert g["plans"][0]["pct"] == 100
    # dashboard goal card
    d = _project_json(p)
    assert (d["hours_done"], d["completion_pct"]) == (10.8, 100)
    # campaign (PS-30): the goal is met, nothing left to schedule
    goal_s, done_s = plan_seconds(osc)
    assert done_s == goal_s == 6 * 3600
    # a second sync is a no-op
    assert runs.sync_goal_progress(cfg) == []


def test_m31_library_kept_set_still_passes_the_goal(tmp_path, monkeypatch):
    from photonscript.scheduler import sub_index
    cfg, store, p = _m31_store(tmp_path, monkeypatch)
    _m31_history(cfg, n120=41)        # only the 41 120 s subs the cull kept
    runs.sync_goal_progress(cfg)
    osc = p.exposure_plans[0]
    assert osc.acquired_s == 41 * 120 + 2 * 300 + 69 * 400 == 33120
    g = sub_index._goal(p)
    assert (g["hours_done"], g["pct"]) == (9.2, 100)


def test_partly_done_goal_counts_seconds_not_subs(tmp_path, monkeypatch):
    from photonscript.scheduler import sub_index
    cfg, store, p = _m31_store(tmp_path, monkeypatch)
    _write_night(cfg, "2026-10-03", [(400, True)] * 9 + [(120, True)])
    runs.sync_goal_progress(cfg)
    osc = p.exposure_plans[0]
    assert osc.acquired_s == 3720                 # 1.03 h, not 10 x 120 s
    assert osc.acquired == 31                     # floor(3720 / 120)
    g = sub_index._goal(p)
    assert (g["hours_done"], g["pct"]) == (1.0, 17)
    assert g["plans"][0]["acquired"] == 31


# ---- the live path ----------------------------------------------------------

def test_live_piggyback_sub_credits_its_seconds(tmp_path):
    store = ProjectStore(_cfg(tmp_path))
    p = store.add_from_target(M31, budget_hours=6)
    store.update(p.id, osc_hours=6, drop_rc16=True)
    osc = p.exposure_plans[0]
    assert store.record_accepted_sub("Andromeda Galaxy", "OSC", 400,
                                     rig="piggyback")
    assert (osc.acquired, osc.acquired_s) == (3, 400)
    store.record_accepted_sub("Andromeda Galaxy", "OSC", 300, rig="piggyback")
    assert (osc.acquired, osc.acquired_s) == (5, 700)
    store.record_accepted_sub("Andromeda Galaxy", "OSC", None, rig="piggyback")
    assert (osc.acquired, osc.acquired_s) == (6, 820)   # no length: one sub
    # live and resync agree
    again = ProjectStore(_cfg(tmp_path)).projects[p.id].exposure_plans[0]
    assert (again.acquired, again.acquired_s) == (6, 820)


def test_guided_rc16_sub_off_plan_length_credits_its_seconds(tmp_path):
    store = ProjectStore(_cfg(tmp_path))
    p = store.add_from_target(HEART, budget_hours=5.0)
    ha = next(e for e in p.exposure_plans if e.filter_type.value == "Ha")
    assert ha.exposure_seconds == 600
    store.record_accepted_sub("Heart Nebula", "Ha", 900)
    assert (ha.acquired, ha.acquired_s) == (1, 900)
    store.record_accepted_sub("Heart Nebula", "Ha", 300)
    assert (ha.acquired, ha.acquired_s) == (2, 1200)


@pytest.mark.parametrize("bad", [0, -5, "abc", None])
def test_credit_seconds_falls_back_to_one_plan_sub(bad):
    e = ExposurePlan(filter_type=FilterType.HA, exposure_seconds=600, count=5)
    assert e.credit_seconds(bad) == 600
    assert e.credit_seconds(450) == 450


# ---- HDR shorts (PS-47/PS-63) -----------------------------------------------

def test_hdr_shorts_keep_their_own_set(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    p = store.add_from_target(HEART, budget_hours=5.0)
    store.update(p.id, filter_mix={"Ha": 100}, hdr={"Ha": 60})
    monkeypatch.setattr(app, "_store", store)
    ha = p.exposure_plans[0]
    assert ha.exposure_seconds == 600 and ha.hdr_short_seconds == 60
    rd = runs.runs_dir(cfg)
    rows = [60] * 3 + [600] * 2 + [300, 900]
    (rd / "2026-10-03_subs.jsonl").write_text("".join(
        json.dumps({"rig": "rc16", "target": "Heart Nebula", "filter": "Ha",
                    "exp_s": x, "passed_qa": True}) + "\n" for x in rows),
        encoding="utf-8")
    runs.sync_goal_progress(cfg)
    assert ha.hdr_short_acquired == 3
    assert ha.acquired_s == 600 * 2 + 300 + 900
    assert ha.acquired == 4
    # the live path splits the same way
    store.record_accepted_sub("Heart Nebula", "Ha", 60)
    assert (ha.hdr_short_acquired, ha.acquired_s) == (4, 2400)


# ---- planner: still owed, no double subtract (PS-105) ------------------------

def test_planner_owes_the_remaining_seconds_once(tmp_path):
    plan = ExposurePlan(filter_type=FilterType.HA, exposure_seconds=600,
                        count=30, gain=200, offset=256)
    for _ in range(5):
        plan.credit_long(300)          # 1500 s accepted = 2.5 plan subs
    assert (plan.acquired, plan.acquired_s) == (2, 1500)
    proj = ImagingProject(id="m31", target=M31, priority=90,
                          exposure_plans=[plan])
    targets = plan_night_sequence([proj], _cfg(tmp_path), NIGHT)
    e = targets[0].exposures[0]
    # 18000 - 1500 = 16500 s owed = 27.5 subs, rounded up to 28, once
    assert (e.count, e.acquired, e.acquired_s) == (28, 0, 0.0)
    assert e.count - e.acquired == 28


# ---- a rebuilt plan keeps seconds when the sub length changes ---------------

def test_new_sub_length_keeps_seconds(tmp_path):
    store = ProjectStore(_cfg(tmp_path))
    p = store.add_from_target(HEART, budget_hours=5.0)
    store.update(p.id, filter_mix={"Ha": 100})
    ha = p.exposure_plans[0]
    for _ in range(4):
        store.record_accepted_sub("Heart Nebula", "Ha", 600)
    assert (ha.acquired, ha.acquired_s) == (4, 2400)
    store.update(p.id, exposure_overrides={"Ha": 300})
    ha = p.exposure_plans[0]
    assert ha.exposure_seconds == 300
    assert (ha.acquired, ha.acquired_s) == (8, 2400)    # was 4 x 300 s


def test_osc_resize_keeps_seconds(tmp_path):
    store = ProjectStore(_cfg(tmp_path))
    p = store.add_from_target(M31, budget_hours=6)
    store.update(p.id, osc_hours=6, drop_rc16=True)
    store.record_accepted_sub("Andromeda Galaxy", "OSC", 400, rig="piggyback")
    store.update(p.id, osc_hours=8)
    osc = p.exposure_plans[0]
    assert (osc.count, osc.acquired, osc.acquired_s) == (240, 3, 400)
