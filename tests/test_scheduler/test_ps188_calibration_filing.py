"""PS-188: calibration in the right place.

Jeremy 2026-10-10: "can we think about getting calibration pictures in the
right place?" Live (read-only GETs, ~16:30Z): GET /api/calibration/
completeness and /windows answered HTTP 500 (a NaN focuser temperature in
the cooler history); the RC16 2026-09-27 300 s darks (24, QA pass) sat in
the watch dir because the dawn sweep did not run after a disarmed night;
the HCG darks of 10-01 / 10-05 / 10-06 kept their pre-PS-182 verdicts in
the quarantine; the Piggy-600 owed 300 / 400 / 450 s darks."""

import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import calibration as calibration
from photonscript.scheduler import calibration_completeness as comp
from photonscript.scheduler import calibration_filing as cf
from photonscript.scheduler import calibration_qa as cq
from photonscript.scheduler import calibration_window as cw
from photonscript.scheduler import cooler_history as ch
from photonscript.scheduler import runs
from photonscript.shared import rigs
from tests.test_scheduler.test_ps113_calibration import write_frame
from tests.test_scheduler.test_ps122_calibration_owed import (_ago, _cfg, _heart,
                                                             _pb_cfg, subs_log)
from tests.test_scheduler.test_ps182_calibration_completeness import (
    _heart_light, _heart_setup, cal_set)


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


# --- (a) the 500 -------------------------------------------------------------------

def _nan_history(cfg, rig="rc16", hours=30):
    """Cooler history shaped like the live one: a sample every hour with
    the focuser temperature NaN (NINA: a focuser with no probe), written
    by the pre-fix record() (json.dumps writes NaN)."""
    d = Path(cfg.data_dir) / ch.LOG_DIR
    d.mkdir(parents=True, exist_ok=True)
    now = datetime.utcnow()
    rows = []
    for i in range(hours):
        t = now - timedelta(hours=i)
        rows.append({"t": t.isoformat(timespec="seconds") + "Z", "rig": rig,
                     "temp": 0.1, "setpoint": 0.0, "power": 41.0, "cooler_on": True,
                     "amb": float("nan"), "src": "agent"})
    with open(d / f"{rig}_{now:%Y-%m}.jsonl", "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def test_nan_focuser_temperature_never_reaches_the_model(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, cooler_history_sample_s=300)
    monkeypatch.setattr(ch, "_last", {})
    cam = {"Temperature": 0.1, "TemperatureSetPoint": 0, "CoolerOn": True,
           "CoolerPower": 40}
    assert ch.record(cfg, "rc16", cam, focuser_temp="NaN", force=True)["amb"] is None
    assert ch.record(cfg, "rc16", {**cam, "Temperature": float("nan")},
                     force=True) is None
    _nan_history(cfg)                                   # rows already on disk
    assert all(s["amb"] is None for s in ch.load(cfg, "rc16"))
    m = ch.reachability(cfg, "rc16")["model"]
    json.dumps(m, allow_nan=False)                      # used to raise: nan


def test_json_safe():
    out = comp.json_safe({"a": float("nan"), "b": (1, float("inf")), "c": {3, 1},
                          ("x", 1): datetime(2026, 10, 10), 5: object})
    json.dumps(out, allow_nan=False)
    assert out["a"] is None and out["b"] == [1, None] and out["c"] == [1, 3]
    assert out["('x', 1)"] == "2026-10-10T00:00:00" and 5 in out


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from photonscript.scheduler import app as app_mod
    cfg = _heart_setup(tmp_path, cooler_history_sample_s=300)
    _nan_history(cfg)
    monkeypatch.setattr(app_mod, "_config", cfg)
    monkeypatch.setattr(app_mod, "_remoteneed_cache", {"names": None, "capped": False})
    monkeypatch.setattr("photonscript.scheduler.calibration_plan.load_projects",
                        lambda c: [_heart()])
    return TestClient(app_mod.app), cfg


def test_endpoints_answer_with_the_live_shaped_history(client):
    c, _cfg_ = client
    for url in ("/api/calibration/completeness", "/api/calibration/completeness?names=true",
                "/api/calibration/windows?rig=rc16&minutes=200"):
        r = c.get(url)
        assert r.status_code == 200, (url, r.text[:300])
    body = c.get("/api/calibration/completeness?rig=rc16").json()
    assert body["rigs"][0]["rig"] == "rc16" and "error" not in body["rigs"][0]


def test_completeness_never_500s(client, monkeypatch):
    """A rig that raises or does not serialize becomes {"rig", "error"};
    the other rig and the line still come back."""
    c, cfg = client
    real = comp._rig

    def boom(config, rig, **kw):
        raise RuntimeError("store unreadable")
    monkeypatch.setattr(comp, "_rig", boom)
    r = c.get("/api/calibration/completeness")
    assert r.status_code == 200
    assert r.json()["rigs"][0] == {"rig": "rc16", "error": "RuntimeError: store unreadable"}
    assert "report failed" in r.json()["line"]

    def nan_rig(config, rig, **kw):
        out = real(config, rig, **kw)
        out["capture_minutes"] = float("nan")
        out["odd"] = {("tuple", "key"): {1, 2}}
        return out
    monkeypatch.setattr(comp, "_rig", nan_rig)
    r = c.get("/api/calibration/completeness?rig=rc16")
    assert r.status_code == 200 and r.json()["rigs"][0]["capture_minutes"] is None

    def bad_ids(config):
        raise ValueError("bad config")
    monkeypatch.setattr("photonscript.shared.rigs.rig_ids", bad_ids)
    r = c.get("/api/calibration/completeness")
    assert r.status_code == 200 and "bad config" in r.json()["error"]


def test_windows_endpoint_never_500s(client, monkeypatch):
    c, _ = client

    def boom(*a, **kw):
        raise TypeError("can't compare offset-naive and offset-aware datetimes")
    monkeypatch.setattr(cw, "windows", boom)
    r = c.get("/api/calibration/windows?rig=rc16")
    assert r.status_code == 200 and "offset-naive" in r.json()["error"]


# --- (b) file calibration now ---------------------------------------------------

def _darks(folder: Path, n, *, exp=300.0, level=262.0, prefix="d", gain=200):
    folder.mkdir(parents=True, exist_ok=True)
    return [write_frame(folder / f"{prefix}{i:03d}.fits", typ="DARK", exp=exp,
                        gain=gain, level=level, seed=i, instrume="AP26MC")
            for i in range(n)]


def _bias(folder: Path, n=6):
    folder.mkdir(parents=True, exist_ok=True)
    return [write_frame(folder / f"b{i:03d}.fits", typ="BIAS", exp=0.001, gain=200,
                        level=254.0, seed=100 + i, instrume="AP26MC") for i in range(n)]


def _lib_names(cfg, typ, date):
    p = runs.library_root(cfg) / "Calibration" / typ / date
    return sorted(f.name for f in p.glob("*.fits")) if p.exists() else []


def test_file_now_files_a_calibration_only_folder(tmp_path):
    """The 2026-09-27 case: a calibration-only folder 13 days old, never
    swept. file_now files it, says so, and is idempotent."""
    cfg = _cfg(tmp_path)
    d = _ago(13)
    _darks(Path(cfg.image_watch_dir) / d / "DARK", 6)
    dry = cf.file_now(cfg, dry_run=True)
    assert dry["rigs"]["rc16"]["n_filed"] == 6 and _lib_names(cfg, "DARK", d) == []
    assert not (Path(cfg.data_dir) / cf.REPORT_NAME).exists()
    rep = cf.file_now(cfg, source="test")
    r = rep["rigs"]["rc16"]
    assert r["n_filed"] == 6 and r["nights"] == [d] and len(_lib_names(cfg, "DARK", d)) == 6
    assert "rc16: 6 filed, 0 quarantined, 0 left" in rep["line"]
    assert cf.last_report(cfg)["source"] == "test"
    again = cf.file_now(cfg)["rigs"]["rc16"]
    assert again["n_filed"] == 0 and again["already_there"] == 6
    assert "6 filed" in cf.format_report(rep)


def test_file_now_quarantines_and_explains(tmp_path):
    cfg = _cfg(tmp_path)
    d = _ago(3)
    _bias(Path(cfg.image_watch_dir) / d / "BIAS")
    _darks(Path(cfg.image_watch_dir) / d / "DARK", 6, exp=60.0)
    write_frame(Path(cfg.image_watch_dir) / d / "DARK" / "lit.fits", typ="DARK",
                exp=60.0, gain=200, level=900.0, leak=40.0, instrume="AP26MC")
    r = cf.file_now(cfg)["rigs"]["rc16"]
    assert r["n_quarantined"] == 1 and r["quarantined"][0]["name"] == "lit.fits"
    assert "level" in r["quarantined"][0]["reason"]
    q = runs.library_root(cfg) / "Calibration" / cq.QUARANTINE_DIR / "DARK" / d
    assert (q / "lit.fits").exists() and "lit.fits" not in _lib_names(cfg, "DARK", d)


def test_file_now_while_imaging_leaves_new_frames_qa_pending(tmp_path):
    cfg = _cfg(tmp_path)
    d = _ago(1)
    _darks(Path(cfg.image_watch_dir) / d / "DARK", 3)
    r = cf.file_now(cfg, measure_new=False)["rigs"]["rc16"]
    assert r["n_left"] == 3 and r["n_filed"] == 0
    assert list(r["left_reasons"])[0].startswith("QA pending")
    assert cf.file_now(cfg)["rigs"]["rc16"]["n_filed"] == 3


def test_file_now_names_folders_outside_the_window(tmp_path):
    cfg = _cfg(tmp_path)
    d = _ago(20)
    _darks(Path(cfg.image_watch_dir) / d / "DARK", 2)
    r = cf.file_now(cfg)["rigs"]["rc16"]
    assert r["nights"] == [] and r["outside_window"][0]["date"] == d
    assert r["outside_window"][0]["action"].endswith("--days 21")
    assert cf.file_now(cfg, days=21)["rigs"]["rc16"]["n_filed"] == 2


def test_file_now_both_rigs_and_one_failing(tmp_path, monkeypatch):
    cfg = _pb_cfg(tmp_path)
    d = _ago(2)
    _darks(Path(cfg.piggyback_image_watch_dir) / d / "DARK", 3, gain=100, prefix="p")
    real = cf._file_rig

    def flaky(config, view, rig, **kw):
        if rig == "rc16":
            raise OSError("watch dir gone")
        return real(config, view, rig, **kw)
    monkeypatch.setattr(cf, "_file_rig", flaky)
    rep = cf.file_now(cfg)
    assert "watch dir gone" in rep["rigs"]["rc16"]["error"]
    assert rep["rigs"]["piggyback"]["n_filed"] == 3
    pb = rigs.rig_config(cfg, "piggyback")
    assert len(_lib_names(pb, "DARK", d)) == 3


def _hcg_setup(tmp_path):
    """The 2026-10-06 RC16 case: HCG 600 s darks 28 ADU over bias, judged
    against the LCG limit (22 ADU) before PS-182 and quarantined."""
    cfg = _cfg(tmp_path)
    d = _ago(4)
    watch = Path(cfg.image_watch_dir) / d
    _bias(watch / "BIAS")
    paths = _darks(watch / "DARK", 6, exp=600.0, level=282.0)
    for p in paths:
        from astropy.io import fits
        fits.setval(p, "READOUTM", value="High Conversion Gain")
    old = _cfg(tmp_path, calibration_qa_dark_scale_by_readout=False)
    cf.file_now(old)                                   # the pre-PS-182 rule
    q = runs.library_root(cfg) / "Calibration" / cq.QUARANTINE_DIR / "DARK" / d
    assert len(list(q.glob("*.fits"))) == 6
    return cfg, d, q


def test_rejudge_moves_frames_that_pass_today(tmp_path):
    cfg, d, q = _hcg_setup(tmp_path)
    rep = cf.file_now(cfg)                             # no --rejudge: left, explained
    r = rep["rigs"]["rc16"]
    assert r["n_filed"] == 0 and r["n_left"] == 6
    assert "--rejudge" in list(r["left_reasons"])[0]
    assert _lib_names(cfg, "DARK", d) == []
    dry = cq.rejudge(cfg, "rc16", dry_run=True)
    assert len(dry["restored"]) == 6 and len(list(q.glob("*.fits"))) == 6
    rep = cf.file_now(cfg, rejudge=True)
    rj = rep["rigs"]["rc16"]["rejudge"]
    assert rj["n_restored"] == 6 and "level" in rj["restored"][0][1]
    assert len(_lib_names(cfg, "DARK", d)) == 6 and not list(q.glob("*.fits"))
    store = cq.load_store(cfg, "rc16")["frames"]
    darks = [r for r in store.values() if r["type"] == "DARK"]
    assert all(r["verdict"] == "pass" and r["location"] == "library" for r in darks)
    assert "re-judge restored 6" in rep["line"]


def test_completeness_says_rejudge_and_not_swept(tmp_path):
    """(c): the reason is concrete and carries the action."""
    cfg, d, _q = _hcg_setup(tmp_path)
    subs_log(cfg, _ago(3), [_heart_light(readout="HCG")])
    rep = comp.completeness(cfg, "rc16", projects=[_heart()])
    dk = [x for x in rep["rigs"][0]["requirements"] if x["kind"] == "DARK"][0]
    assert dk["rejudge_would_pass"] == 6
    assert "--rejudge" in dk["action"] and "pass today's QA rule" in dk["reason"]
    assert any("older QA verdict" in x["reason"] for x in dk["not_filed"])
    # a passed folder not swept yet
    now = datetime.now()
    why, act = comp.frame_filing({"date": _ago(13), "status": "not_filed",
                                  "verdict": "pass", "verdict_now": "pass"},
                                 now=now, sweep_days=14, cal_days=120)
    assert "not swept yet" in why and "leaves the 14 d window in 1 d" in why
    assert act.startswith("photonscript calibration-file")
    why, act = comp.frame_filing({"date": _ago(20), "status": "not_filed",
                                  "verdict": "pass"}, now=now, sweep_days=14,
                                 cal_days=120)
    assert "outside the 14 d" in why and act.endswith("--days 21")
    why, _ = comp.frame_filing({"date": _ago(2), "status": "not_filed",
                                "verdict": None}, now=now, sweep_days=14, cal_days=120)
    assert why.startswith("QA pending")
    why, act = comp.frame_filing({"date": _ago(2), "status": "quarantine",
                                  "verdict": "fail", "verdict_now": "fail",
                                  "reason": "leak: corner vs center 5 ADU"},
                                 now=now, sweep_days=14, cal_days=120)
    assert why.startswith("quarantined because leak") and "roof closed" in act
    assert "action:" in comp.format_report(rep)


def test_file_endpoint(client, monkeypatch):
    c, cfg = client
    from photonscript.scheduler import app as app_mod

    class A:
        state = "IDLE"
    monkeypatch.setattr(app_mod, "get_armer", lambda: A)
    r = c.post("/api/calibration/file", json={"dry_run": True})
    assert r.status_code == 200 and r.json()["dry_run"] and "rc16" in r.json()["rigs"]
    assert c.post("/api/calibration/file", json={"rig": "nope"}).status_code == 404
    assert "no calibration filing" in c.get("/api/calibration/file").json()["detail"]
    c.post("/api/calibration/file", json={})
    assert c.get("/api/calibration/file").json()["source"] == "manual"
    A.state = "RUNNING"
    assert c.post("/api/calibration/file", json={"regrade": True}).status_code == 409
    seen = {}
    real = cf.file_now

    def spy(config, **kw):
        seen.update(kw)
        return real(config, **kw)
    monkeypatch.setattr(cf, "file_now", spy)
    assert c.post("/api/calibration/file", json={}).status_code == 200
    assert seen["measure_new"] is False


def test_calibration_file_cli(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from photonscript import cli
    cfg = _cfg(tmp_path)
    _darks(Path(cfg.image_watch_dir) / _ago(2) / "DARK", 2)
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)
    res = CliRunner().invoke(cli.app, ["calibration-file"])
    assert res.exit_code == 2 and "--url" in res.output
    res = CliRunner().invoke(cli.app, ["calibration-file", "--local", "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "2 filed" in res.output and _lib_names(cfg, "DARK", _ago(2)) == []


# --- automatic runs -----------------------------------------------------------------

def test_triggers():
    assert cf.trigger(None, "ARMED") is None            # a service start files nothing
    assert cf.trigger("IDLE", "ARMED") == "arm"
    assert cf.trigger("RUNNING", "COMPLETE") == "night ended"
    assert cf.trigger("PAUSED_UNSAFE", "IDLE") == "night ended"
    assert cf.trigger("ARMED", "RUNNING") is None
    assert cf.trigger("COMPLETE", "IDLE") is None


def test_run_auto_respects_the_switch(tmp_path):
    cfg = _cfg(tmp_path)
    _darks(Path(cfg.image_watch_dir) / _ago(2) / "DARK", 2)
    assert cf.run_auto(_cfg(tmp_path, calibration_file_auto=False), "arm") is None
    rep = cf.run_auto(cfg, "arm", "RUNNING")
    assert rep["measure_new"] is False and rep["rigs"]["rc16"]["n_left"] == 2
    assert cf.run_auto(cfg, "arm", "ARMED")["rigs"]["rc16"]["n_filed"] == 2


def test_filing_loop_files_on_arm(tmp_path, monkeypatch):
    import asyncio
    cfg = _cfg(tmp_path)
    states = iter(["IDLE", "ARMED", "ARMED"])
    calls = []

    class A:
        @property
        def state(self):
            try:
                return next(states)
            except StopIteration:
                raise asyncio.CancelledError
    monkeypatch.setattr(cf, "run_auto", lambda c, src, st, rigs=None: calls.append(src))

    async def go():
        try:
            await cf.filing_loop(lambda: cfg, lambda: A(), tick_s=0)
        except asyncio.CancelledError:
            pass
    asyncio.run(go())
    assert calls == ["arm"]


def test_config_keys_on_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    keys = {f[0] for f in _CONFIG_FIELDS}
    assert {"calibration_file_auto", "piggyback_dark_capture_window"} <= keys
    from photonscript.shared.config import PhotonScriptConfig
    c = PhotonScriptConfig(_env_file=None)
    assert c.calibration_file_auto is True
    assert c.piggyback_dark_capture_window == "unsafe-night"


# --- (d) Piggy-600 darks ---------------------------------------------------------------

def _gate_pb(tmp_path, **kw):
    script = tmp_path / "cooler-gate.cmd"
    script.write_text("@echo off\n")
    return _pb_cfg(tmp_path, cooler_gate_script=str(script),
                   piggyback_dark_exposures="120,300,400,450", **kw)


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv("PS_COOLER_GATE_MODE", "skip")


def test_companion_unsafe_branch_shoots_the_owed_lengths(tmp_path, gate_on):
    """The live Piggy-600 owes 300 / 400 / 450 s (0 of 30 each): the
    companion's OSC_DARKS_IF_UNSAFE carries a block per owed length, behind
    the cooler gate (PS-160), roof-gated (LoopWhileUnsafe)."""
    cfg = _gate_pb(tmp_path)
    pb = rigs.rig_config(cfg, "piggyback")
    root = json.loads(calibration.generate_piggyback_companion_json(pb, has_safety=True))
    darks = next(n for n in _walk(root) if n.get("Name") == "OSC_DARKS_IF_UNSAFE")
    items = darks["Items"]["$values"]
    assert "cooler-gate" in str(items[0].get("Script", "")).lower()
    names = [i.get("Name") for i in items[1:]]
    for e in (120, 300, 400, 450):
        assert f"OSC DARKS_{e}s (need 30 of 30)" in names, names
    conds = [c["$type"] for c in darks["Conditions"]["$values"]]
    assert any("LoopWhileUnsafe" in c for c in conds)
    exp = {n["ExposureTime"] for n in _walk(darks) if n.get("ImageType") == "DARK"}
    assert exp == {120.0, 300.0, 400.0, 450.0}


def test_companion_manual_window_has_no_darks(tmp_path, gate_on):
    cfg = _gate_pb(tmp_path, piggyback_dark_capture_window="manual")
    pb = rigs.rig_config(cfg, "piggyback")
    root = json.loads(calibration.generate_piggyback_companion_json(pb, has_safety=True))
    assert not any(n.get("ImageType") == "DARK" for n in _walk(root))
    assert any("piggyback_dark_capture_window is manual" in str(n.get("Text", ""))
               for n in _walk(root))
    assert any(n.get("ImageType") == "BIAS" for n in _walk(root))   # bias stays


def test_piggy_dark_action_and_dawn_plan(tmp_path):
    cfg = _pb_cfg(tmp_path, piggyback_dark_exposures="120,300")
    assert "OSC_DARKS_IF_UNSAFE" in cf.piggy_dark_action(cfg)
    assert "lens cap" in cf.piggy_dark_action(cfg)
    assert cf.dark_capture_window(_pb_cfg(tmp_path, piggyback_dark_capture_window="x")) \
        == "unsafe-night"
    assert cf.file_now(cfg)["dawn_plan"] is None
    dawn = _pb_cfg(tmp_path, piggyback_dark_exposures="120,300",
                   piggyback_dark_capture_window="dawn-roof-closed")
    assert "dawn plan" in cf.piggy_dark_action(dawn)
    rep = cf.file_now(dawn)
    assert rep["dawn_plan"]["deferred"] and [300.0, 30] in rep["dawn_plan"]["darks"]
    plan = cw.deferred(dawn, "piggyback")
    assert [120.0, 30] in plan["darks"] and "lens cap" in plan["reason"]
    assert cf.file_now(dawn)["dawn_plan"]["unchanged"]
    manual = _pb_cfg(tmp_path, piggyback_dark_capture_window="manual")
    assert "manual" in cf.piggy_dark_action(manual)


def test_completeness_piggy_darks_carry_the_action(tmp_path):
    from tests.test_scheduler.test_ps122_calibration_owed import _m31, osc
    cfg = _pb_cfg(tmp_path, piggyback_dark_exposures="120,300")
    subs_log(cfg, _ago(2), [osc(300.0, readout="LCG")])
    rep = comp.completeness(cfg, "piggyback", projects=[_m31()])
    dk = [x for x in rep["rigs"][0]["requirements"] if x["kind"] == "DARK"][0]
    assert dk["stage"] == "not_captured" and "lens cap" in dk["action"]
    assert not math.isnan(rep["rigs"][0]["capture_minutes"])
