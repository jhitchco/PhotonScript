"""PS-30: rig-aware goals: ExposurePlan.rig + FilterType.OSC, the store keeps
OSC plans when the RC16 mix is rebuilt, goal progress is keyed by rig, the
Piggy-600 readiness profile, and OSC plans never reach an RC16 sequence."""

import json
from datetime import datetime

from photonscript.scheduler import calibration as cal
from photonscript.scheduler import readiness, runs
from photonscript.scheduler.project_store import ProjectStore, osc_plan
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (CelestialTarget, ExposurePlan,
                                        FilterType, ImagingProject)

M31 = CelestialTarget(name="Andromeda Galaxy", catalog_id="M 31",
                      ra_hours=0.712, dec_degrees=41.27, object_type="galaxy")

# A trimmed GET /api/projects dump from before PS-30 (no rig / driving_rig /
# min_alt_deg / require_calibration anywhere).
LEGACY = {
    "m31": {
        "id": "m31", "priority": 50, "budget_hours": 10.0, "active": True,
        "target": {"name": "Andromeda Galaxy", "catalog_id": "M 31",
                   "ra_hours": 0.712, "dec_degrees": 41.27,
                   "object_type": "galaxy"},
        "exposure_plans": [
            {"filter_type": "L", "exposure_seconds": 180, "count": 100,
             "gain": 200, "offset": 256, "acquired": 0},
            {"filter_type": "R", "exposure_seconds": 180, "count": 33,
             "gain": 200, "offset": 256, "acquired": 0},
            {"filter_type": "G", "exposure_seconds": 180, "count": 33,
             "gain": 200, "offset": 256, "acquired": 0},
            {"filter_type": "B", "exposure_seconds": 180, "count": 33,
             "gain": 200, "offset": 256, "acquired": 0}]},
    "crescent": {
        "id": "crescent", "priority": 60, "budget_hours": 6.0, "active": True,
        "target": {"name": "Crescent Nebula", "catalog_id": "NGC 6888",
                   "ra_hours": 20.2, "dec_degrees": 38.35,
                   "object_type": "emission nebula"},
        "exposure_plans": [
            {"filter_type": "Ha", "exposure_seconds": 600, "count": 13,
             "gain": 200, "offset": 256, "acquired": 4}]},
}


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _legacy_store(tmp_path):
    (tmp_path / "projects.json").write_text(json.dumps(LEGACY),
                                            encoding="utf-8")
    return ProjectStore(_cfg(tmp_path))


# --- model + store ------------------------------------------------------------

def test_osc_filter_and_defaults_load_from_legacy_dump(tmp_path):
    store = _legacy_store(tmp_path)
    cres = store.projects["crescent"]
    assert cres.driving_rig == "rc16" and cres.min_alt_deg is None
    assert cres.require_calibration is True
    assert all(e.rig == "rc16" for e in cres.exposure_plans)
    assert FilterType("OSC") is FilterType.OSC
    # round trip keeps the new fields
    store.save()
    raw = json.loads((tmp_path / "projects.json").read_text())
    assert raw["crescent"]["driving_rig"] == "rc16"
    assert raw["crescent"]["exposure_plans"][0]["rig"] == "rc16"
    again = ProjectStore(_cfg(tmp_path))
    assert again.projects["crescent"].exposure_plans[0].acquired == 4


def test_m31_decision_applied_once(tmp_path):
    store = _legacy_store(tmp_path)
    m31 = store.projects["m31"]
    assert m31.driving_rig == "piggyback"
    assert [(e.filter_type.value, e.rig) for e in m31.exposure_plans] == [
        ("OSC", "piggyback")]
    osc = m31.exposure_plans[0]
    assert osc.exposure_seconds == 120 and osc.count == 180  # 6 h
    assert (osc.gain, osc.offset) == (100, 256)
    assert m31.budget_hours == 6.0
    assert json.loads((tmp_path / "project_migrations.json").read_text()) == {
        "ps30_m31_osc": True}
    # a later hand edit is never undone
    store.update("m31", driving_rig="rc16")
    assert ProjectStore(_cfg(tmp_path)).projects["m31"].driving_rig == "rc16"


def test_m31_decision_keeps_rc16_plans_that_hold_subs(tmp_path):
    legacy = json.loads(json.dumps(LEGACY))
    legacy["m31"]["exposure_plans"][0]["acquired"] = 7
    (tmp_path / "projects.json").write_text(json.dumps(legacy))
    m31 = ProjectStore(_cfg(tmp_path)).projects["m31"]
    assert [e.filter_type.value for e in m31.exposure_plans] == ["L", "OSC"]
    assert m31.exposure_plans[0].acquired == 7


def test_budget_change_keeps_osc_plan_and_hdr(tmp_path):
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    cat = next(p for p in store.projects.values()
               if p.target.catalog_id == "NGC 6543")
    store.update(cat.id, osc_hours=2)
    osc = next(e for e in cat.exposure_plans if e.rig == "piggyback")
    osc.acquired = 9
    store.update(cat.id, budget_hours=8)
    plans = store.projects[cat.id].exposure_plans
    osc2 = [e for e in plans if e.rig == "piggyback"]
    assert len(osc2) == 1 and osc2[0].acquired == 9 and osc2[0].count == 60
    ha = next(e for e in plans if e.filter_type.value == "Ha")
    assert ha.rig == "rc16" and ha.hdr_short_seconds == 60  # HDR kept
    store.update(cat.id, filter_mix={"Ha": 60, "OIII": 40})
    assert any(e.rig == "piggyback" for e in store.projects[cat.id].exposure_plans)
    store.update(cat.id, osc_hours=3)  # resize keeps acquired
    osc3 = next(e for e in store.projects[cat.id].exposure_plans
                if e.rig == "piggyback")
    assert (osc3.count, osc3.acquired) == (90, 9)
    store.update(cat.id, osc_hours=0)  # remove
    assert all(e.rig == "rc16" for e in store.projects[cat.id].exposure_plans)


def test_piggyback_only_goal_budget_sizes_the_osc_plan(tmp_path):
    store = ProjectStore(_cfg(tmp_path))
    p = store.add_from_target(M31, budget_hours=10)
    store.update(p.id, osc_hours=6, drop_rc16=True, driving_rig="piggyback")
    assert [e.filter_type.value for e in p.exposure_plans] == ["OSC"]
    assert p.budget_hours == 6.0
    store.update(p.id, budget_hours=8)
    assert [e.filter_type.value for e in p.exposure_plans] == ["OSC"]
    assert p.exposure_plans[0].count == 240 and p.budget_hours == 8.0


def test_record_accepted_sub_matches_rig(tmp_path):
    store = ProjectStore(_cfg(tmp_path))
    p = store.add_from_target(M31, budget_hours=6)
    store.update(p.id, osc_hours=6, drop_rc16=True)
    assert not store.record_accepted_sub("Andromeda Galaxy", "OSC", 120)
    assert store.record_accepted_sub("Andromeda Galaxy", "OSC", 120,
                                     rig="piggyback")
    assert p.exposure_plans[0].acquired == 1


def test_sync_goal_progress_keys_on_rig(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    p = store.add_from_target(M31, budget_hours=2)
    store.update(p.id, filter_mix={"L": 100}, osc_hours=1)
    monkeypatch.setattr(app, "_store", store)
    rows = ([{"rig": "rc16", "target": "Andromeda Galaxy", "filter": "L",
              "exp_s": 180, "passed_qa": True}] * 4
            + [{"rig": "piggyback", "target": "Andromeda Galaxy",
                "filter": "OSC", "exp_s": 120, "passed_qa": True}] * 6
            # a piggyback sub mis-filed under an RC16 filter is not an L sub
            + [{"rig": "piggyback", "target": "Andromeda Galaxy",
                "filter": "L", "exp_s": 120, "passed_qa": True}] * 2
            + [{"rig": "piggyback", "target": "?", "filter": "OSC",
                "exp_s": 120, "passed_qa": True}] * 3)
    (runs.runs_dir(cfg) / "2026-10-03_subs.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    runs.sync_goal_progress(cfg)
    by = {(e.rig, e.filter_type.value): e.acquired for e in p.exposure_plans}
    assert by == {("rc16", "L"): 4, ("piggyback", "OSC"): 6}


# --- readiness: the piggyback profile -----------------------------------------

def test_piggyback_readiness_profile(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    seen = {}

    def health(c):
        seen["lib"] = c.library_dir
        seen["watch"] = c.image_watch_dir
        return {"FLAT": {"count_latest": 25, "detail": {"?": 25}},
                "BIAS": {"count_latest": 50}}

    def darks(c, e, **kw):
        seen["darks"] = (e, kw, c.library_dir)
        return 30
    monkeypatch.setattr(cal, "calibration_health", health)
    monkeypatch.setattr(cal, "count_matching_darks", darks)
    ctx = readiness.calibration_context(cfg, "piggyback")
    lib = runs.library_root(cfg)
    assert ctx.lib == lib  # OSC lights sit in the main Library
    assert seen["lib"] == str(lib / "piggyback")
    assert seen["watch"] == str(lib / "piggyback")  # never the RC16 tree
    assert ctx.flats == {"OSC": 25} and ctx.bias == 50
    assert ctx.darks(120) == 30
    assert seen["darks"][1] == {"gain": 100, "offset": 256, "setpoint": 0.0}
    assert seen["darks"][2] == str(lib / "piggyback")

    (lib / "Andromeda Galaxy" / "OSC").mkdir(parents=True)
    (lib / "Andromeda Galaxy" / "OSC" / "a.fits").write_bytes(b"x")
    proj = ImagingProject(id="m31", target=M31, driving_rig="piggyback",
                          exposure_plans=[osc_plan(6, cfg)])
    r = readiness.target_readiness(cfg, proj, ctx)
    assert r["rig"] == "piggyback" and r["ready"] is True
    assert r["filters"]["OSC"]["accepted_in_library"] == 1
    assert "prepare-integration-osc.ps1" in r["command"]
    assert readiness.calibration_missing(r) == []
    # the RC16 context ignores the OSC plan
    rc = readiness.calibration_context(cfg, "rc16", health={
        "FLAT": {"detail": {}}, "BIAS": {"count_latest": 0}})
    r16 = readiness.target_readiness(cfg, proj, rc)
    assert r16["filters"] == {} and "rig" not in r16
    # the endpoint lists M31 once, for its piggyback rig
    rep = readiness.readiness_report(cfg, [proj], rc)
    assert [t.get("rig") for t in rep["targets"]] == ["piggyback"]


def test_calibration_missing_lists_planned_gaps():
    r = {"bias": 0, "darks_by_exposure": {"600s": 0, "60s": 4},
         "filters": {"Ha": {"flats": 0}, "OIII": {"flats": 3}}}
    assert readiness.calibration_missing(r) == ["bias", "darks 600s",
                                                "flats Ha"]


# --- the RC16 sequence never gets an OSC plan --------------------------------

def test_plan_night_sequence_skips_piggyback_plans(tmp_path):
    from photonscript.scheduler.target_planner import plan_night_sequence
    cfg = _cfg(tmp_path)
    proj = ImagingProject(
        id="x", target=M31, priority=90,
        exposure_plans=[ExposurePlan(filter_type=FilterType.HA,
                                     exposure_seconds=600, count=10),
                        osc_plan(6, cfg)])
    only_osc = ImagingProject(id="y", priority=95, target=CelestialTarget(
        name="Triangulum", catalog_id="M 33", ra_hours=1.564,
        dec_degrees=30.66, object_type="galaxy"),
        driving_rig="piggyback", exposure_plans=[osc_plan(6, cfg)])
    targets = plan_night_sequence([proj, only_osc], cfg,
                                  datetime(2026, 10, 2, 20))
    names = [t.name for t in targets]
    assert "Triangulum" not in names
    for t in targets:
        assert all(e.filter_type != FilterType.OSC for e in t.exposures)
