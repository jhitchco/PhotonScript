"""PS-30: campaign planner v2 (10-minute visibility slots, one moon rule,
rig-aware goals, season parking, calibration gate, completion alerts)."""

import json
import time
from datetime import datetime, timedelta

import numpy as np
import pytest
from astropy.coordinates import AltAz, get_body
from astropy.time import Time

from photonscript.scheduler import campaign
from photonscript.scheduler import readiness
from photonscript.scheduler.project_store import ProjectStore, osc_plan
from photonscript.shared.astronomy import (compute_visibility_window,
                                           dark_windows, get_earth_location)
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget

NOW = datetime(2026, 10, 2, 20, 0)     # 14:00 MDT, before the 2026-10-02 night
M31 = CelestialTarget(name="Andromeda Galaxy", catalog_id="M 31",
                      ra_hours=0.712, dec_degrees=41.27, object_type="galaxy")
M8 = CelestialTarget(name="Lagoon Nebula", catalog_id="M 8", ra_hours=18.063,
                     dec_degrees=-24.38, object_type="emission nebula")


def _cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"))


def _store(tmp_path):
    """M31: 6 h Piggy-600 OSC, piggyback driving. NGC 6543 (seeded): RC16
    Ha/OIII 6 h with HDR short subs, plus a 1 h OSC passenger plan."""
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    m31 = store.add_from_target(M31, budget_hours=10)
    store.update(m31.id, osc_hours=6, drop_rc16=True, driving_rig="piggyback")
    cat = next(p for p in store.projects.values()
               if p.target.catalog_id == "NGC 6543")
    store.update(cat.id, budget_hours=6, osc_hours=1)
    return cfg, store, m31.id, cat.id


def _ctx(rig, flats, bias=50, darks=32):
    c = readiness.CalibrationContext(rig=rig, lib=__import__("pathlib").Path("."),
                                     flats=flats, bias=bias)
    c._darks = {120.0: darks, 600.0: darks, 60.0: darks, 180.0: darks}
    return c


@pytest.fixture(scope="module")
def plan(tmp_path_factory):
    cfg, store, m31_id, cat_id = _store(tmp_path_factory.mktemp("camp"))
    c = campaign.build_campaign(cfg, store, now=NOW, with_calibration=False)
    return cfg, store, c, m31_id, cat_id


def _goal(c, name):
    return next(g for g in c["goals"] if g["name"] == name)


def test_assigned_hours_fit_inside_each_targets_visible_window(plan):
    cfg, store, c, *_ = plan
    obs = cfg.get_observatory()
    targets = {p.target.name: p.target for p in store.projects.values()}
    assert len(c["nights"]) == 14
    for n in c["nights"]:
        base = datetime.strptime(n["date"], "%Y-%m-%d")
        per_goal = {}
        for a in n["assigned"]:
            if not a["passenger"]:
                per_goal[a["goal"]] = per_goal.get(a["goal"], 0) + a["hours"]
        for name, h in per_goal.items():
            vis = compute_visibility_window(targets[name], obs, base,
                                            min_altitude=30.0)
            # assigned = slot hours x usable fraction <= hours above 30 deg
            assert h <= vis["hours"] * n["usable_frac"] + 0.2, (n["date"], name)
            assert h <= vis["hours"] + 0.2


def test_nightly_mount_time_never_exceeds_dark_times_usable(plan):
    _, _, c, *_ = plan
    for n in c["nights"]:
        cap = n["dark_h"] * n["usable_frac"] + 0.05
        assert n["mount_h"] <= cap
        drive = sum(a["hours"] for a in n["assigned"] if not a["passenger"])
        assert drive <= cap + 0.05


def test_broadband_and_osc_only_in_moon_down_slots_on_bright_nights(plan):
    cfg, _, c, *_ = plan
    loc = get_earth_location(cfg.get_observatory())
    checked = 0
    for n in c["nights"]:
        if n["moon"]["illum_pct"] < 20:
            continue
        for a in n["assigned"]:
            if a["kind"] not in ("BB", "OSC"):
                continue
            t0 = datetime.fromisoformat(a["start"][:-1])
            t1 = datetime.fromisoformat(a["end"][:-1])
            k = int((t1 - t0).total_seconds() // 600)
            tt = Time([t0 + timedelta(minutes=10 * i + 5) for i in range(k)])
            alt = get_body("moon", tt, loc).transform_to(
                AltAz(obstime=tt, location=loc)).alt.deg
            # a run spans first..last slot; the slots it used are moon-down,
            # so at least its first and last slot must be
            assert alt[0] < 1.0 and alt[-1] < 1.0, (n["date"], a)
            checked += 1
    assert checked  # 2026-10 has bright-moon nights with M31 OSC in them


def test_rig_aware_credit_and_shape(plan):
    _, _, c, *_ = plan
    m31 = _goal(c, "Andromeda Galaxy")
    cat = _goal(c, "Cat's Eye Nebula")
    assert m31["driving_rig"] == "piggyback"
    assert set(m31["by_rig"]) == {"piggyback"}
    assert m31["by_rig"]["piggyback"]["passenger"] is False
    assert set(cat["by_rig"]) == {"rc16", "piggyback"}
    assert cat["by_rig"]["piggyback"]["passenger"] is True
    rides = [a for n in c["nights"] for a in n["assigned"]
             if a["goal"] == "Cat's Eye Nebula" and a["rig"] == "piggyback"]
    assert rides and all(a["passenger"] for a in rides)
    m31_runs = [a for n in c["nights"] for a in n["assigned"]
                if a["goal"] == "Andromeda Galaxy"]
    assert m31_runs and all(a["kind"] == "OSC" and not a["passenger"]
                            for a in m31_runs)
    for g in (m31, cat):
        for k in ("visible_h_14n", "by_rig", "season", "calibration",
                  "status", "eta", "hours_done", "goal_hours"):
            assert k in g
    assert m31["goal_hours"] == 6.0 and m31["status"] == "active"
    # M31 is visible ~9 h a night in October: the ETA is a real night
    assert m31["eta"] is not None and m31["visible_h_14n"] > 100
    for k in ("nb_capacity_h", "nb_demand_h", "bb_capacity_h", "bb_demand_h"):
        assert k in c["totals"]


def test_eta_no_earlier_than_visible_hours_allow(plan):
    _, _, c, *_ = plan
    for g in c["goals"]:
        if not g["eta"]:
            continue
        need = g["goal_hours"] - g["hours_done"]
        got = 0.0
        for n in c["nights"]:
            got += sum(a["hours"] for a in n["assigned"]
                       if a["goal"] == g["name"] and not a["passenger"])
            if n["date"] == g["eta"]:
                break
        # mount hours through the ETA night cover the driving rig's need
        drive = g["by_rig"][g["driving_rig"]]
        assert got + 0.2 >= drive["remaining_h"] - 0.01 or need <= 0.2


def test_off_season_goal_is_parked_with_return_date_not_deactivated(tmp_path):
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    m8 = store.add_from_target(M8, budget_hours=6)
    c = campaign.build_campaign(cfg, store, now=datetime(2026, 11, 15, 20),
                                with_calibration=False)
    g = _goal(c, "Lagoon Nebula")
    assert g["visible_h_14n"] == 0
    assert g["status"] == "parked"
    assert g["returns"] and g["returns"] > "2027-01-01"
    assert not any(a["goal"] == "Lagoon Nebula"
                   for n in c["nights"] for a in n["assigned"])
    assert store.projects[m8.id].active is True
    # the yearly sweep is cached by RA/Dec/floor
    cache = json.loads((tmp_path / campaign.SEASON_FILE).read_text())
    assert any(k.startswith("18.0630,-24.3800,30") for k in cache)


def test_calibration_gate_on_complete(tmp_path):
    cfg, store, m31_id, _ = _store(tmp_path)
    for e in store.projects[m31_id].exposure_plans:
        e.acquired = e.count  # lights done
    ctxs = {"piggyback": _ctx("piggyback", {"OSC": 0}),
            "rc16": _ctx("rc16", {"Ha": 10, "OIII": 10})}
    c = campaign.build_campaign(cfg, store, now=NOW, cal_contexts=ctxs,
                                with_season=False)
    g = _goal(c, "Andromeda Galaxy")
    assert g["status"] == "lights_done_needs_calibration"
    assert g["calibration"]["ok"] is False
    assert "piggyback: flats OSC" in g["calibration"]["missing"]
    ctxs["piggyback"] = _ctx("piggyback", {"OSC": 25})
    c = campaign.build_campaign(cfg, store, now=NOW, cal_contexts=ctxs,
                                with_season=False)
    assert _goal(c, "Andromeda Galaxy")["status"] == "complete"
    # require_calibration False: lights alone complete it
    store.projects[m31_id].require_calibration = False
    ctxs["piggyback"] = _ctx("piggyback", {"OSC": 0}, bias=0)
    c = campaign.build_campaign(cfg, store, now=NOW, cal_contexts=ctxs,
                                with_season=False)
    assert _goal(c, "Andromeda Galaxy")["status"] == "complete"


def test_notify_transitions_baseline_then_once(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    sent = []
    monkeypatch.setattr(campaign, "_fire", lambda c, m: sent.extend(m))
    g = {"id": "x", "name": "M31", "status": "active", "hours_done": 5.0,
         "calibration": {"missing": []}}
    assert campaign.notify_transitions(cfg, [g]) == []  # baseline only
    g["status"] = "lights_done_needs_calibration"
    g["calibration"] = {"missing": ["piggyback: flats OSC"]}
    msgs = campaign.notify_transitions(cfg, [g])
    assert len(msgs) == 1 and "flats OSC" in msgs[0]
    assert campaign.notify_transitions(cfg, [g]) == []  # no repeat
    g["status"] = "complete"
    assert "complete" in campaign.notify_transitions(cfg, [g])[0]
    assert len(sent) == 2
    cfg.campaign_notify = False
    g["status"] = "active"
    campaign.notify_transitions(cfg, [g])
    g["status"] = "complete"
    assert campaign.notify_transitions(cfg, [g])  # recorded, not sent
    assert len(sent) == 2


def test_goal_sync_triggers_completion_check(tmp_path, monkeypatch):
    from photonscript.scheduler import app, runs
    cfg, store, m31_id, _ = _store(tmp_path)
    monkeypatch.setattr(app, "_store", store)
    monkeypatch.setattr(campaign, "_calibration",
                        lambda c, goals, ctx: [g.__setitem__(
                            "calibration", {"ok": False,
                                            "missing": ["piggyback: bias"]})
                            for g in goals])
    sent = []
    monkeypatch.setattr(campaign, "_fire", lambda c, m: sent.extend(m))
    campaign.check_goal_transitions(cfg, store)  # baseline
    n = store.projects[m31_id].exposure_plans[0].count
    rows = [{"rig": "piggyback", "target": "Andromeda Galaxy", "filter": "OSC",
             "exp_s": 120, "passed_qa": True}] * n
    (runs.runs_dir(cfg) / "2026-10-03_subs.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    runs.sync_goal_progress(cfg)
    assert len(sent) == 1 and "Andromeda Galaxy" in sent[0]
    assert "bias" in sent[0]


def test_build_campaign_is_fast(tmp_path):
    cfg, store, *_ = _store(tmp_path)
    for t in (M8, CelestialTarget(name="Crescent Nebula", catalog_id="NGC 6888",
                                  ra_hours=20.2, dec_degrees=38.35,
                                  object_type="emission nebula"),
              CelestialTarget(name="Pinwheel", catalog_id="M 33",
                              ra_hours=1.564, dec_degrees=30.66,
                              object_type="galaxy")):
        store.add_from_target(t, budget_hours=8)
    assert len([p for p in store.projects.values() if p.active]) == 5
    campaign.build_campaign(cfg, store, now=NOW, days=2,
                            with_calibration=False)  # warm imports + season
    t0 = time.perf_counter()
    c = campaign.build_campaign(cfg, store, now=NOW, with_calibration=False)
    assert time.perf_counter() - t0 < 3.0
    assert len(c["nights"]) == 14


def test_dark_windows_match_get_twilight_times():
    from photonscript.shared.astronomy import get_twilight_times
    obs = PhotonScriptConfig(_env_file=None).get_observatory()
    d = datetime(2026, 10, 2)
    (s, e), = dark_windows(obs, [d])
    tw = get_twilight_times(obs, d)
    # get_twilight_times reports the sample before each crossing (~4 min grid)
    assert abs((s - tw["astro_dark_start"]).total_seconds()) < 300
    assert abs((e - tw["astro_dark_end"]).total_seconds()) < 300


def test_plan_seconds_honors_accepted_seconds_when_present():
    """PS-66 adds acquired_s (accepted seconds); without it seconds are sub
    length x count."""
    p = osc_plan(1.0, PhotonScriptConfig(_env_file=None), acquired=10)
    assert campaign.plan_seconds(p) == (3600.0, 1200.0)
    from types import SimpleNamespace
    q = SimpleNamespace(**p.model_dump(), acquired_s=900.0)
    assert campaign.plan_seconds(q) == (3600.0, 900.0)
