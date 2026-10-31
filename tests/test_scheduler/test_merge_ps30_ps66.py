"""Merge of PS-30 (rig-aware goals) and PS-66 (unguided seconds credit):
both rules hold together. Rig filtering decides WHICH plan a sub counts
against; seconds credit (RC16, unguided nights only) decides HOW MUCH."""

import json

from photonscript.scheduler import campaign, runs
from photonscript.scheduler.project_store import ProjectStore
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget, ExposurePlan, FilterType

HEART = CelestialTarget(name="Heart Nebula", catalog_id="IC 1805",
                        ra_hours=2.55, dec_degrees=61.5,
                        object_type="emission nebula")


def _cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"))


def _mixed(tmp_path):
    store = ProjectStore(_cfg(tmp_path))
    proj = store.add_from_target(HEART, budget_hours=5.0)
    store.update(proj.id, osc_hours=1)
    return store, proj


def _plan(proj, rig, f):
    return next(e for e in proj.exposure_plans
                if e.rig == rig and e.filter_type.value == f)


def test_unguided_capped_rc16_sub_credits_half(tmp_path):
    store, proj = _mixed(tmp_path)
    ha = _plan(proj, "rc16", "Ha")
    assert ha.exposure_seconds == 600
    assert store.record_accepted_sub("Heart Nebula", "Ha", 300, rig="rc16")
    assert (ha.acquired, ha.acquired_s) == (0, 300)
    store.record_accepted_sub("Heart Nebula", "Ha", 300)
    assert (ha.acquired, ha.acquired_s) == (1, 600)


def test_piggyback_sub_never_credits_an_rc16_plan(tmp_path):
    store, proj = _mixed(tmp_path)
    ha = _plan(proj, "rc16", "Ha")
    osc = _plan(proj, "piggyback", "OSC")
    # a piggyback sub filed under an RC16 filter matches nothing
    assert not store.record_accepted_sub("Heart Nebula", "Ha", 300,
                                         rig="piggyback")
    assert (ha.acquired, ha.acquired_s) == (0, 0)
    # an OSC sub never matches the RC16 (rig None = RC16)
    assert not store.record_accepted_sub("Heart Nebula", "OSC", 120)
    # PS-118: a piggyback sub is credited by its own seconds too
    assert osc.exposure_seconds == 120
    assert store.record_accepted_sub("Heart Nebula", "OSC", 60,
                                     rig="piggyback")
    assert (osc.acquired, osc.acquired_s) == (0, 60)
    assert store.record_accepted_sub("Heart Nebula", "OSC", 400,
                                     rig="piggyback")
    assert (osc.acquired, osc.acquired_s) == (3, 460)


def test_sync_keys_on_rig_and_credits_seconds_per_snapshot(tmp_path,
                                                           monkeypatch):
    from photonscript.scheduler import app as app_mod
    cfg = _cfg(tmp_path)
    store, proj = _mixed(tmp_path)
    monkeypatch.setattr(app_mod, "_store", store)
    rd = runs.runs_dir(cfg)
    (rd / "2026-10-03_plan.json").write_text(json.dumps({"targets": [
        {"name": "Heart Nebula", "guided": False, "exposures": []}]}))
    rows = ([{"rig": "rc16", "target": "Heart Nebula", "filter": "Ha",
              "exp_s": 300, "passed_qa": True}] * 3
            + [{"rig": "piggyback", "target": "Heart Nebula", "filter": "Ha",
                "exp_s": 300, "passed_qa": True}] * 2
            + [{"rig": "piggyback", "target": "Heart Nebula", "filter": "OSC",
                "exp_s": 60, "passed_qa": True}] * 4)
    (rd / "2026-10-03_subs.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    runs.sync_goal_progress(cfg)
    ha = _plan(proj, "rc16", "Ha")
    osc = _plan(proj, "piggyback", "OSC")
    assert (ha.acquired, ha.acquired_s) == (1, 900)
    # PS-118: 4 x 60 s piggyback subs are 240 s (2 x 120 s), not 4 subs
    assert (osc.acquired, osc.acquired_s) == (2, 240)


def test_update_on_mixed_project_keeps_osc_and_rc16_partial_seconds(tmp_path):
    store, proj = _mixed(tmp_path)
    store.record_accepted_sub("Heart Nebula", "Ha", 600)
    store.record_accepted_sub("Heart Nebula", "Ha", 300)
    store.record_accepted_sub("Heart Nebula", "OSC", 120, rig="piggyback")
    osc_before = _plan(proj, "piggyback", "OSC").model_dump()
    store.update(proj.id, budget_hours=8.0)   # RC16 plans rebuilt
    ha = _plan(proj, "rc16", "Ha")
    assert (ha.acquired, ha.acquired_s) == (1, 900)
    assert _plan(proj, "piggyback", "OSC").model_dump() == osc_before
    # and both survive a reload
    again = ProjectStore(_cfg(tmp_path)).projects[proj.id]
    assert _plan(again, "rc16", "Ha").acquired_s == 900
    assert _plan(again, "piggyback", "OSC").acquired == 1


def test_plan_seconds_adds_hdr_shorts_to_accepted_seconds():
    e = ExposurePlan(filter_type=FilterType.HA, exposure_seconds=600, count=10,
                     acquired=1, acquired_s=900, hdr_short_seconds=60,
                     hdr_short_count=10, hdr_short_acquired=5)
    assert campaign.plan_seconds(e) == (6600.0, 900.0 + 300.0)
