"""PS-134: add an RC16 plan to a Piggy-only goal (and remove it again).

M31 is a Piggy-600 OSC goal with the piggyback driving (PS-30). Once a goal
was OSC-only, ProjectStore.update never allocated RC16 plans again, so the
only way to add the RC16 core LRGB was delete and re-add. PATCH rc16_hours
now adds / resizes (> 0) or removes (0) the RC16 plan and keeps driving_rig
and the OSC plan; the planner points at the goal while either rig owes time;
goal progress is still credited per rig (PS-118 seconds)."""

import json
import math
from datetime import datetime

import pytest

from photonscript.scheduler import runs
from photonscript.scheduler.campaign import build_campaign
from photonscript.scheduler.project_store import ProjectStore
from photonscript.scheduler.target_planner import (piggy_hold_exposure,
                                                   plan_night_sequence)
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget, FilterType

M31 = CelestialTarget(name="Andromeda Galaxy", catalog_id="M 31",
                      ra_hours=0.712, dec_degrees=41.27, object_type="galaxy")
NIGHT = datetime(2026, 10, 2, 20)   # M31 up most of the night from AARO

# The Before / after deploy PATCH body for the live M31 goal
M31_BODY = {"rc16_hours": 8, "osc_hours": 20,
            "filter_mix": {"L": 50, "R": 17, "G": 17, "B": 17},
            "exposure_overrides": {"L": 300, "R": 300, "G": 300, "B": 300},
            "hdr": {"L": 30, "R": 30, "G": 30, "B": 30}}


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _piggy_only(store, osc_h=6.0):
    """The live M31 shape: one piggyback OSC plan, the piggyback driving."""
    p = store.add_from_target(M31, budget_hours=10)
    store.update(p.id, osc_hours=osc_h, drop_rc16=True,
                 driving_rig="piggyback")
    assert [e.rig for e in p.exposure_plans] == ["piggyback"]
    return p


def _by(p):
    return {(e.rig, e.filter_type.value): e for e in p.exposure_plans}


# --- store --------------------------------------------------------------------

def test_budget_alone_still_sizes_the_osc_plan_on_a_piggy_only_goal(tmp_path):
    """The PS-30 rule is unchanged without rc16_hours."""
    store = ProjectStore(_cfg(tmp_path))
    p = _piggy_only(store)
    store.update(p.id, budget_hours=8, filter_mix={"L": 100})
    assert [e.filter_type.value for e in p.exposure_plans] == ["OSC"]
    assert p.budget_hours == 8.0


def test_rc16_hours_adds_an_rc16_plan_and_keeps_driver_and_osc(tmp_path):
    store = ProjectStore(_cfg(tmp_path))
    p = _piggy_only(store)
    osc = p.exposure_plans[0]
    osc.acquired, osc.acquired_s = 30, 30 * 120.0
    store.update(p.id, rc16_hours=8,
                 filter_mix={"L": 50, "R": 17, "G": 17, "B": 17},
                 exposure_overrides={"L": 300, "R": 300, "G": 300, "B": 300})
    by = _by(p)
    assert set(by) == {("rc16", "L"), ("rc16", "R"), ("rc16", "G"),
                       ("rc16", "B"), ("piggyback", "OSC")}
    assert p.driving_rig == "piggyback"
    assert p.budget_hours == 8.0 and p.total_integration_hours == 8.0
    rc16_s = sum(e.count * e.exposure_seconds for e in p.exposure_plans
                 if e.rig == "rc16")
    assert abs(rc16_s - 8 * 3600) <= 300 * 2
    assert by[("rc16", "L")].exposure_seconds == 300
    assert by[("rc16", "L")].count == 48        # 50% of 8 h at 300 s
    # the OSC plan is untouched: same goal, progress kept
    o = by[("piggyback", "OSC")]
    assert (o.count, o.acquired, o.acquired_s) == (180, 30, 3600.0)
    # stored, so it survives a restart
    again = ProjectStore(_cfg(tmp_path)).projects[p.id]
    assert {(e.rig, e.filter_type.value) for e in again.exposure_plans} == set(by)


def test_m31_patch_body_gives_the_planned_goal(tmp_path):
    """The exact PATCH body in the build notes, through the endpoint."""
    import asyncio
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    p = _piggy_only(store)

    class Req:
        async def json(self):
            return M31_BODY
    app_store = app._store
    app._store = store
    try:
        out = asyncio.run(app.api_project_update(p.id, Req()))
    finally:
        app._store = app_store
    plans = {(e["rig"], e["filter_type"]): e for e in out["exposure_plans"]}
    assert out["driving_rig"] == "piggyback" and out["budget_hours"] == 8.0
    o = plans[("piggyback", "OSC")]
    assert o["exposure_seconds"] == cfg.piggyback_exposure_s
    assert o["count"] * o["exposure_seconds"] == 20 * 3600
    for f in "LRGB":
        e = plans[("rc16", f)]
        assert e["exposure_seconds"] == 300
        assert (e["hdr_short_seconds"], e["hdr_short_count"]) == (30, 12)
    assert out["hours_by_rig"]["piggyback"]["goal_h"] == 20.0
    assert abs(out["hours_by_rig"]["rc16"]["goal_h"] - 8.0) <= 0.2
    assert out["mix"] == {"L": 50, "R": 17, "G": 17, "B": 17}


def test_budget_after_adding_is_the_rc16_budget(tmp_path):
    store = ProjectStore(_cfg(tmp_path))
    p = _piggy_only(store)
    store.update(p.id, rc16_hours=8, filter_mix={"L": 100})
    store.update(p.id, budget_hours=4)
    by = _by(p)
    assert by[("rc16", "L")].count * by[("rc16", "L")].exposure_seconds \
        == 4 * 3600
    assert by[("piggyback", "OSC")].count == 180      # 6 h untouched
    store.update(p.id, osc_hours=20)
    assert _by(p)[("piggyback", "OSC")].count == 600
    assert p.budget_hours == 4.0


def test_rc16_hours_zero_removes_the_rc16_plan(tmp_path):
    store = ProjectStore(_cfg(tmp_path))
    p = _piggy_only(store)
    store.update(p.id, rc16_hours=8, osc_hours=20, filter_mix={"L": 100})
    store.update(p.id, rc16_hours=0)
    assert [e.rig for e in p.exposure_plans] == ["piggyback"]
    assert p.driving_rig == "piggyback"
    assert p.budget_hours == 20.0          # back to the OSC goal
    # ...and it can be added again
    store.update(p.id, rc16_hours=2, filter_mix={"L": 100})
    assert ("rc16", "L") in _by(p)


def test_removing_the_only_plan_is_refused(tmp_path):
    store = ProjectStore(_cfg(tmp_path))
    p = store.add_from_target(M31, budget_hours=4)
    with pytest.raises(ValueError):
        store.update(p.id, rc16_hours=0)
    assert all(e.rig == "rc16" for e in p.exposure_plans) and p.exposure_plans


def test_endpoint_refusal_is_a_400(tmp_path):
    import asyncio
    from photonscript.scheduler import app
    store = ProjectStore(_cfg(tmp_path))
    p = store.add_from_target(M31, budget_hours=4)

    class Req:
        async def json(self):
            return {"rc16_hours": 0}
    app_store = app._store
    app._store = store
    try:
        r = asyncio.run(app.api_project_update(p.id, Req()))
    finally:
        app._store = app_store
    assert r.status_code == 400


# --- per-rig crediting (PS-118) -----------------------------------------------

def test_goal_sync_credits_each_rig_by_seconds(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    p = _piggy_only(store)
    store.update(p.id, **M31_BODY)
    monkeypatch.setattr(app, "_store", store)
    rows = ([{"rig": "rc16", "target": "Andromeda Galaxy", "filter": "L",
              "exp_s": 300, "passed_qa": True}] * 4
            + [{"rig": "rc16", "target": "Andromeda Galaxy", "filter": "L",
                "exp_s": 30, "passed_qa": True}] * 3          # HDR shorts
            + [{"rig": "piggyback", "target": "M 31", "filter": "OSC",
                "exp_s": 400, "passed_qa": True}] * 2
            + [{"rig": "piggyback", "target": "Andromeda Galaxy",
                "filter": "OSC", "exp_s": 120, "passed_qa": True}] * 5)
    (runs.runs_dir(cfg) / "2026-10-07_subs.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    runs.sync_goal_progress(cfg)
    by = _by(p)
    L, o = by[("rc16", "L")], by[("piggyback", "OSC")]
    assert (L.acquired, L.acquired_s, L.hdr_short_acquired) == (4, 1200.0, 3)
    assert o.acquired_s == 2 * 400 + 5 * 120 and o.acquired == 11
    assert all(e.acquired == 0 for k, e in by.items()
               if k[0] == "rc16" and k[1] != "L")


# --- planner ------------------------------------------------------------------

def test_hold_exposure_rules(tmp_path):
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    p = _piggy_only(store)
    assert piggy_hold_exposure(p) is None          # no RC16 plan: as before
    store.update(p.id, **M31_BODY)
    for e in p.exposure_plans:                     # the RC16 plan is done
        if e.rig == "rc16":
            e.acquired, e.hdr_short_acquired = e.count, e.hdr_short_count
    o = _by(p)[("piggyback", "OSC")]
    o.acquired_s = 10 * 3600.0                     # 10 h of 20 h shot
    h = piggy_hold_exposure(p)
    assert h.filter_type == FilterType.LUMINANCE and h.rig == "rc16"
    assert h.exposure_seconds == 300 and h.count == math.ceil(10 * 3600 / 300)
    assert (h.acquired, h.hdr_short_count, h.hdr_short_seconds) == (0, 0, None)
    p.driving_rig = "rc16"                         # an RC16-driven goal
    assert piggy_hold_exposure(p) is None
    p.driving_rig = "piggyback"
    o.acquired_s = 20 * 3600.0                     # OSC done too
    assert piggy_hold_exposure(p) is None


def test_planner_plans_the_target_for_either_rig(tmp_path):
    cfg = _cfg(tmp_path, moon_aware_planning=False)   # no moon in the way
    store = ProjectStore(cfg)
    p = _piggy_only(store)
    # Piggy-only: still off the RC16 sequence (unchanged)
    assert plan_night_sequence([p], cfg, NIGHT) == []
    store.update(p.id, **M31_BODY)
    # RC16 owes: the sequence shoots the RC16 plan, never the OSC plan
    t = plan_night_sequence([p], cfg, NIGHT)
    assert [x.name for x in t] == ["Andromeda Galaxy"]
    assert {e.filter_type.value for e in t[0].exposures} <= set("LRGB")
    assert all(e.rig == "rc16" for e in t[0].exposures)
    # RC16 done, Piggy still owes: the target stays planned (RC16 holds)
    for e in p.exposure_plans:
        if e.rig == "rc16":
            e.acquired, e.hdr_short_acquired = e.count, e.hdr_short_count
    t = plan_night_sequence([p], cfg, NIGHT)
    assert [x.name for x in t] == ["Andromeda Galaxy"]
    assert [e.filter_type.value for e in t[0].exposures] == ["L"]
    # both done: the goal drops out (an empty night then falls back to the
    # seasonal catalog, so check with a second goal that still owes time)
    o = _by(p)[("piggyback", "OSC")]
    o.acquired, o.acquired_s = o.count, o.count * o.exposure_seconds
    other = store.add_from_target(CelestialTarget(
        name="Triangulum", catalog_id="M 33", ra_hours=1.564,
        dec_degrees=30.66, object_type="galaxy"), budget_hours=2)
    t = plan_night_sequence([p, other], cfg, NIGHT)
    assert [x.name for x in t] == ["Triangulum"]


def test_campaign_credits_both_rigs_on_the_goal(tmp_path):
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    p = _piggy_only(store)
    store.update(p.id, **M31_BODY)
    c = build_campaign(cfg, store, now=NIGHT, with_calibration=False,
                       with_season=False)
    g = next(x for x in c["goals"] if x["name"] == "Andromeda Galaxy")
    assert g["driving_rig"] == "piggyback"
    assert set(g["by_rig"]) == {"rc16", "piggyback"}
    assert g["by_rig"]["piggyback"]["passenger"] is False
    assert g["by_rig"]["rc16"]["passenger"] is True
    kinds = {(a["kind"], a["rig"]) for n in c["nights"] for a in n["assigned"]
             if a["goal"] == "Andromeda Galaxy"}
    assert ("BB", "rc16") in kinds and ("OSC", "piggyback") in kinds


# --- goal card ----------------------------------------------------------------

def test_goal_card_add_and_remove_controls():
    from pathlib import Path
    s = (Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
         / "templates" / "dashboard.html").read_text(encoding="utf-8")
    i = s.index("PS-134: add an RC16 plan to a Piggy-only goal")
    block = s[i:s.index("async function removeRc16", i)]
    block.encode("ascii")
    assert "patchProject(id, {rc16_hours: h, filter_mix: mix})" in s
    assert "patchProject(id, {rc16_hours: 0})" in s
    assert "LRGB: {L: 50, R: 17, G: 17, B: 17}" in block
    k = s.index("filterRows + oscRows +")
    tail = s[k:s.index("accepted lights in library", k)]
    assert "addRc16Controls(p)" in tail
    assert "(bothRigs ? removeRc16Button(p) : '')" in tail
    assert "patchProject(inp.dataset.osc, {osc_hours: v})" in s
    assert "(bothRigs ? 'RC16 goal' : 'Goal')" in s
