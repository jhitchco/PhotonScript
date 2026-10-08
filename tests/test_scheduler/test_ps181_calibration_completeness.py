"""PS-181: calibration completeness, reliable darks, bias when none, copy
priority.

Jeremy 2026-10-08: "let's make sure that we're capturing all of the biases
and calibration and copying them over please? the darks are the only ones
that have been tough". Live (read-only GETs, 21:32Z): the RC16 owed view
counted the 2026-07-31 bias (no READOUTM in the QA store, assumed HCG) while
the desktop found it LCG; HCG 600 s darks failed QA against an LCG
dark-current limit; a daytime capture job could sit on the cooler for its
whole budget; calibration sat behind 15 GB of lights in Syncthing."""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from astropy.io import fits

from photonscript.scheduler import calibration as cal
from photonscript.scheduler import calibration_capture as cc
from photonscript.scheduler import calibration_completeness as comp
from photonscript.scheduler import calibration_library as cl
from photonscript.scheduler import calibration_owed as co
from photonscript.scheduler import calibration_qa as cq
from photonscript.scheduler import calibration_window as cw
from photonscript.scheduler import cooler_history as ch
from photonscript.scheduler import runs
from photonscript.shared import rigs
from tests.test_scheduler.test_ps113_calibration import FakeIO, write_frame
from tests.test_scheduler.test_ps122_calibration_owed import (_ago, _cfg, _heart,
                                                             _m31, _pb_cfg, osc,
                                                             subs_log)

HCG, LCG = "High Conversion Gain", "Low Conversion Gain"


def _heart_light(**kw):
    r = {"rig": "rc16", "target": "Heart Nebula", "filter": "Ha", "exp_s": 600.0,
         "gain": 200, "offset": 256, "set_temp": 0.0, "xbin": 1, "readout": HCG}
    r.update(kw)
    return r


def cal_set(cfg, rig, typ, date, n, *, status="library", verdict="pass",
            reasons=None, prefix="f", **extra):
    """n calibration frames on disk (Library, quarantine or the watch dir)
    and their QA records."""
    view = cq.rig_view(cfg, rig)
    lib = runs.library_root(view)
    store = cq.load_store(cfg, rig)
    for i in range(n):
        name = f"{prefix}{typ.lower()}{i:03d}.fits"
        if status == "library":
            p = lib / "Calibration" / typ / date / name
        elif status == "quarantine":
            p = lib / "Calibration" / cq.QUARANTINE_DIR / typ / date / name
        else:
            p = Path(view.image_watch_dir) / date / typ / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        rec = {"type": typ, "date": date, "name": name, "verdict": verdict,
               "gain": 100 if rig == "piggyback" else 200, "offset": 256,
               "settemp": 0.0, "path": str(p), "reasons": reasons or [],
               "codes": [], "exptime": 0.001 if typ == "BIAS" else 600.0}
        rec.update(extra)
        store["frames"][cq.frame_key(typ, date, name)] = rec
    cq.save_store(cfg, rig, store)


# --- one rule for "usable" -----------------------------------------------------

def test_owed_and_library_report_agree_on_a_warm_bias(tmp_path):
    """The 2026-09-30 RC16 bias was shot at SET-TEMP 20 C: QA failed it for
    CCD-TEMP, but a passed bias at another SET-TEMP used to count in the
    owed view (passed_bias_sessions had no temperature check)."""
    cfg = _cfg(tmp_path)
    cal_set(cfg, "rc16", "BIAS", _ago(8), 50, settemp=20.0, readout="HCG")
    r = co.owed_report(cfg, "rc16", projects=[])["rigs"][0]
    assert r["bias"]["owed"] and r["bias"]["reasons"] == ["no bias"]
    rep = cl.library_report(cfg, "rc16")
    s = [x for x in rep["sets"] if x["type"] == "BIAS"][0]
    assert not s["usable"] and "SET-TEMP 20 C" in s["epoch_misses"][0]
    assert cal.days_since_last_bias(cfg) is None
    cal_set(cfg, "rc16", "BIAS", _ago(4), 50, prefix="g", readout="HCG")
    r = co.owed_report(cfg, "rc16", projects=[])["rigs"][0]
    assert not r["bias"]["owed"] and r["bias"]["last"] == _ago(4)
    assert cal.days_since_last_bias(cfg) == 4


def test_readout_since_stops_assuming_old_frames(tmp_path):
    """camera_readout_since: a dark / bias with no readout recorded and older
    than the HCG switch is not assumed HCG (the July bias and 600 s darks are
    LCG), in the quota, the owed view and the library report alike."""
    since = _ago(12)
    cfg = _cfg(tmp_path, camera_readout_since=since)
    cal_set(cfg, "rc16", "BIAS", _ago(40), 50)                       # no readout
    cal_set(cfg, "rc16", "DARK", _ago(40), 7, prefix="old")          # no readout
    cal_set(cfg, "rc16", "DARK", _ago(3), 4, prefix="new")           # no readout, after
    assert cal.dark_quota(cfg, "rc16", 600.0)["have"] == 4
    assert cq.count_passed_bias(cfg, "rc16", gain=200, offset=256) == 0
    r = co.owed_report(cfg, "rc16", projects=[])["rigs"][0]
    assert r["bias"]["owed"]
    assert r["frames_readout_assumed"] == 4          # only the frames after since
    rep = cl.library_report(cfg, "rc16")
    old = [s for s in rep["sets"] if s["type"] == "BIAS"][0]
    assert not old["usable"] and "readout unknown" in old["epoch_misses"][0]
    assert old["readout"] is None
    # blank = the PS-128 assumption (today's behavior)
    legacy = _cfg(tmp_path)
    assert cal.dark_quota(legacy, "rc16", 600.0)["have"] == 11
    assert rigs.rig_readout_since(legacy, "rc16") is None
    assert rigs.rig_readout_since(_cfg(tmp_path, camera_readout_since="bad"), "rc16") is None


def test_fill_missing_readouts_records_the_header(tmp_path):
    cfg = _cfg(tmp_path)
    d = _ago(40)
    lib = runs.library_root(cfg)
    store = cq.load_store(cfg, "rc16")
    for i in range(3):
        p = write_frame(lib / "Calibration" / "BIAS" / d / f"b{i}.fits", typ="BIAS",
                        exp=0.001, gain=200, seed=i)
        fits.setval(p, "READOUTM", value=LCG)
        store["frames"][cq.frame_key("BIAS", d, p.name)] = {
            "type": "BIAS", "date": d, "name": p.name, "verdict": "pass",
            "gain": 200, "offset": 256, "settemp": 0.0, "path": str(p)}
    cq.save_store(cfg, "rc16", store)
    assert cq.count_passed_bias(cfg, "rc16", gain=200, offset=256) == 3   # assumed HCG
    res = cq.fill_missing_readouts(cfg, "rc16", limit=2)
    assert res == {"checked": 2, "filled": 2, "left": 1}
    res = cq.fill_missing_readouts(cfg, "rc16", limit=10)
    assert res["filled"] == 1 and res["left"] == 0
    assert cq.count_passed_bias(cfg, "rc16", gain=200, offset=256) == 0
    assert cq.count_passed_bias(cfg, "rc16", gain=200, offset=256, readout="LCG") == 3
    assert cq.fill_missing_readouts(cfg, "rc16", limit=10)["checked"] == 0
    assert cq.fill_missing_readouts(cfg, "rc16", limit=0)["checked"] == 0


# --- darks: QA limit per readout ---------------------------------------------

def test_hcg_dark_limit_scales_with_conversion_gain(tmp_path):
    cfg = _cfg(tmp_path)
    assert cq.dark_excess_scales(cfg, "rc16") == {"HCG": 3.16, "LCG": 1.0}
    assert cq.dark_excess_scales(cfg, "piggyback") == {}
    assert cq.dark_excess_scales(_cfg(tmp_path, calibration_qa_dark_scale_by_readout=False),
                                 "rc16") == {}
    rec = {"type": "DARK", "exptime": 600.0, "gain": 200, "offset": 256,
           "ccdtemp": 0.0, "settemp": 0.0, "median": 290.0, "readout": "HCG"}
    fails, _w = cq.judge_frame(dict(rec), rig="rc16", bias_ref=(254.0, "bias"))
    assert [f["code"] for f in fails] == ["level"]              # 36 ADU > 22
    fails, _w = cq.judge_frame(dict(rec), rig="rc16", bias_ref=(254.0, "bias"),
                               excess_scale=3.16)
    assert fails == []                                          # limit 47.9
    rec["median"] = 400.0                                       # 146 ADU: still fails
    fails, _w = cq.judge_frame(dict(rec), rig="rc16", bias_ref=(254.0, "bias"),
                               excess_scale=3.16)
    assert fails and "x3.16 for HCG" in fails[0]["detail"]


# --- cooler history and reachability -------------------------------------------

def _samples(rows, sp=0.0):
    t0 = datetime(2026, 10, 7, 12, 0)
    return [{"t": (t0 + timedelta(minutes=10 * i)).isoformat() + "Z", "rig": "rc16",
             "temp": t, "setpoint": sp, "power": p, "cooler_on": True, "amb": a}
            for i, (t, p, a) in enumerate(rows)]


def test_cooler_record_throttles_and_loads(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, cooler_history_sample_s=300)
    monkeypatch.setattr(ch, "_last", {})
    cam = {"Temperature": -0.3, "TemperatureSetPoint": 0, "CoolerOn": True,
           "CoolerPower": 62.3}
    assert ch.record(cfg, "rc16", cam, focuser_temp=33.3)["power"] == 62.3
    assert ch.record(cfg, "rc16", cam, focuser_temp=33.3) is None        # throttled
    assert ch.record(cfg, "rc16", cam, force=True)["amb"] is None
    assert len(ch.load(cfg, "rc16")) == 2
    assert ch.record(_cfg(tmp_path, cooler_history_sample_s=0), "rc16", cam,
                     force=True) is None
    assert ch.record(cfg, "rc16", None, force=True) is None


def test_reachability_model(tmp_path):
    # 2026-10-08 RC16: 62 % at 33 C ambient held 0 C -> k ~ 1.9 %/C
    rows = [(0.1, 62.0, 33.0), (-0.2, 50.0, 27.0), (0.3, 40.0, 21.0),
            (0.0, 30.0, 16.0)]
    m = ch.fit(_samples(rows), 0.0)
    assert m["fit_samples"] == 4 and 1.8 < m["k_pct_per_c"] < 2.0
    p = ch.predict(m, ambient=30.0, max_power=90.0)
    assert p["reachable"] is True and p["power_pct"] < 90
    p = ch.predict(m, ambient=52.0, max_power=90.0)
    assert p["reachable"] is False and "over 90" in p["why"]
    # a saturated sample (TEC flat out, sensor above the setpoint) wins
    m2 = ch.fit(_samples(rows + [(9.0, 100.0, 36.0)]), 0.0)
    assert m2["saturated_amb_min_c"] == 36.0
    assert ch.predict(m2, ambient=35.5, max_power=90.0)["reachable"] is False
    assert ch.predict(ch.fit([], 0.0), ambient=30.0)["reachable"] is None
    assert ch.live_saturated({"Temperature": 9, "CoolerPower": 100, "CoolerOn": True}, 0, 1)
    assert not ch.live_saturated({"Temperature": 0.2, "CoolerPower": 100,
                                  "CoolerOn": True}, 0, 1)


def test_stall_detection():
    trace = [(0.0, 25.0), (60.0, 21.0), (120.0, 20.2), (240.0, 20.1), (400.0, 20.0)]
    assert not cc.stalled(trace, 0.0, 1.0, 400.0, 10.0, 0.0)      # too early
    assert cc.stalled(trace, 0.0, 1.0, 400.0, 6.0 / 60 * 5, 0.0)   # flat for 3 min
    falling = [(0.0, 25.0), (200.0, 15.0), (400.0, 8.0)]
    assert not cc.stalled(falling, 0.0, 1.0, 400.0, 1.0, 0.0)
    assert not cc.stalled([(0.0, 0.5), (400.0, 0.4)], 0.0, 1.0, 400.0, 1.0, 0.0)


# --- the capture job: refuse quickly, re-plan for dawn ---------------------------

def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def jobs(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cc, "_jobs", {})
    monkeypatch.setattr(cc, "_autofill_done", {})
    monkeypatch.setattr(ch, "_last", {})


class HotIO(FakeIO):
    def __init__(self, cfg, power=100.0, amb=None, **kw):
        super().__init__(cfg, **kw)
        self.power, self.amb = power, amb

    async def camera(self):
        d = await super().camera()
        d.update(CoolerOn=True, CoolerPower=self.power, TemperatureSetPoint=0.0)
        return d

    async def focuser_temp(self):
        return self.amb


def test_daytime_start_refused_when_tec_is_flat_out(tmp_path, jobs):
    cfg = _pb_cfg(tmp_path)
    io = HotIO(cfg, temps=[18.0])
    ok, body = _run(cc.start_job(cfg, "piggyback", armer_state_fn=lambda: "DISARMED",
                                 io=io, daytime=True, exposures=[300.0], count=2))
    assert not ok and any("TEC is flat out" in r for r in body["refusals"])
    assert "dispatch" not in io.calls and ("cool", 0.0) not in io.calls
    plan = cw.deferred(cfg, "piggyback")
    assert plan["darks"] == [[300.0, 2]] and "flat out" in plan["reason"]
    # warn mode: logged only, the job starts
    warn = _pb_cfg(tmp_path, calibration_cool_reach="warn")
    ref, _seen = _run(cc.preflight(warn, "piggyback", "DISARMED", HotIO(warn, temps=[18.0]),
                                   daytime=True))
    assert not any("flat out" in r for r in ref)
    # at night the check does not run
    ref, _seen = _run(cc.preflight(cfg, "piggyback", "DISARMED", HotIO(cfg, temps=[18.0]),
                                   daytime=False))
    assert not any("flat out" in r for r in ref)


def test_daytime_start_refused_by_history(tmp_path, jobs):
    cfg = _pb_cfg(tmp_path)
    for i, (t, p, a) in enumerate([(0.1, 62.0, 33.0), (-0.2, 50.0, 27.0),
                                   (0.3, 40.0, 21.0)]):
        ch.record(cfg, "piggyback", {"Temperature": t, "CoolerPower": p, "CoolerOn": True,
                                     "TemperatureSetPoint": 0.0},
                  focuser_temp=a, force=True)
    io = HotIO(cfg, power=0.0, amb=55.0, temps=[40.0])
    ok, body = _run(cc.start_job(cfg, "piggyback", armer_state_fn=lambda: "DISARMED",
                                 io=io, daytime=True, exposures=[300.0], count=2))
    assert not ok and any("cannot reach 0 C now" in r for r in body["refusals"])
    assert body["read"]["reach"]["reachable"] is False
    io = HotIO(cfg, power=0.0, amb=25.0)
    ref, seen = _run(cc.preflight(cfg, "piggyback", "DISARMED", io, daytime=True))
    assert seen["reach"]["reachable"] is True and not any("reach" in r for r in ref)


def test_cooling_stall_aborts_and_defers(tmp_path, jobs, monkeypatch):
    cfg = _pb_cfg(tmp_path, calibration_cool_stall_min=0.001)
    monkeypatch.setattr(cc, "STALL_WINDOW_S", 0.0)
    io = HotIO(cfg, power=99.0, temps=[12.0])

    async def go():
        ok, body = await cc.start_job(cfg, "piggyback", armer_state_fn=lambda: "DISARMED",
                                      io=io, daytime=False, poll_s=0.01,
                                      exposures=[300.0], count=2)
        assert ok, body
        j = cc._jobs["piggyback"]
        await asyncio.wait_for(j.task, 20)
        return j
    j = _run(go())
    assert j.state == "aborted" and "cooler stalled at 12" in j.detail
    assert "dispatch" not in io.calls
    assert cw.deferred(cfg, "piggyback")["darks"] == [[300.0, 2]]


def test_windows_and_dawn_tick(tmp_path, jobs, monkeypatch):
    cfg = _cfg(tmp_path)
    now = datetime(2026, 10, 8, 21, 40)                        # 15:40 MDT-ish day
    w = cw.windows(cfg, "rc16", minutes=200, now=now)
    names = [x["name"] for x in w["windows"]]
    assert "dawn after shutdown" in names and "now" in names
    dawn = [x for x in w["windows"] if x["name"] == "dawn after shutdown"][0]
    assert dawn["minutes"] == 120 and dawn["schedulable"]
    assert w["recommended"] in ("now", "dawn after shutdown")
    # the deferred plan starts in its window only when calibration_dawn_capture
    cw.defer(cfg, "rc16", [[600.0, 3]], 0, "test", dawn)
    assert _run(cw.dawn_tick(cfg, lambda: "COMPLETE", now=now))["skipped"]
    on = _cfg(tmp_path, calibration_dawn_capture=True)
    started = {}

    async def fake_start(config, rig, **kw):
        started.update(kw, rig=rig)
        return True, {"id": "x"}
    monkeypatch.setattr(cc, "start_job", fake_start)
    assert _run(cw.dawn_tick(on, lambda: "COMPLETE", now=now))["rc16"].startswith("waiting")
    inside = datetime.fromisoformat(dawn["start"].rstrip("Z")) + timedelta(minutes=5)
    res = _run(cw.dawn_tick(on, lambda: "COMPLETE", now=inside))
    assert res["rc16"] == {"started": "x"} and started["darks"] == [[600.0, 3]]
    assert started["source"] == "deferred: dawn" and cw.deferred(on, "rc16") is None


# --- bias when none is usable -----------------------------------------------------

def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _names(seq):
    return [d.get("Name") for d in _walk(seq) if isinstance(d, dict) and d.get("Name")]


def test_companion_shoots_bias_at_setpoint_when_none(tmp_path):
    cfg = _pb_cfg(tmp_path)
    view = cq.rig_view(cfg, "piggyback")
    seq = json.loads(cal.generate_piggyback_companion_json(view, has_safety=True))
    names = _names(seq)
    assert "OSC_BIAS_AT_SETPOINT" in names and "OSC_BIAS_IF_UNSAFE" not in names
    blk = [d for d in _walk(seq) if d.get("Name") == "OSC_BIAS_AT_SETPOINT"][0]
    shots = [d for d in _walk(blk) if "TakeExposure" in str(d.get("$type", ""))]
    assert shots and shots[0]["Gain"] == 100 and shots[0]["ImageType"] == "BIAS"
    off = _pb_cfg(tmp_path, calibration_bias_when_missing=False)
    names = _names(json.loads(cal.generate_piggyback_companion_json(
        cq.rig_view(off, "piggyback"), has_safety=True)))
    assert "OSC_BIAS_AT_SETPOINT" not in names and "OSC_BIAS_IF_UNSAFE" in names
    # a usable bias: neither block on a fresh one
    cal_set(cfg, "piggyback", "BIAS", _ago(3), 50, readout="LCG")
    names = _names(json.loads(cal.generate_piggyback_companion_json(view, has_safety=True)))
    assert "OSC_BIAS_AT_SETPOINT" not in names


# --- the completeness model ----------------------------------------------------------

def _heart_setup(tmp_path, **kw):
    cfg = _cfg(tmp_path, **kw)
    subs_log(cfg, _ago(3), [_heart_light(), _heart_light()])
    subs_log(cfg, _ago(2), [_heart_light(filter="OIII", exp_s=600.0)])
    cal_set(cfg, "rc16", "BIAS", _ago(5), 50, readout="HCG")
    cal_set(cfg, "rc16", "DARK", _ago(4), 10, readout="HCG", prefix="ok")
    cal_set(cfg, "rc16", "DARK", _ago(4), 3, readout="HCG", prefix="bad",
            status="quarantine", verdict="fail",
            reasons=["level: median 8611 is 8357.0 ADU over bias"])
    cal_set(cfg, "rc16", "FLAT", _ago(2), 15, filter="Ha", exptime=2.0, prefix="ha")
    cal_set(cfg, "rc16", "FLAT", _ago(2), 15, filter="OIII", exptime=2.0, prefix="o3",
            status="watch")
    return cfg


def _req(rep, label_start):
    return [q for q in rep["rigs"][0]["requirements"] if q["label"].startswith(label_start)][0]


def test_completeness_stages_and_reasons(tmp_path):
    cfg = _heart_setup(tmp_path)
    rep = comp.completeness(cfg, "rc16", projects=[_heart()])
    r = rep["rigs"][0]
    bias = _req(rep, "bias")
    assert bias["stage"] == "complete" and bias["in_library"] == 50
    assert bias["on_desktop"] is None
    dark = _req(rep, "darks 600 s")
    assert dark["stage"] == "short" and dark["qa_passed"] == 10 and dark["need"] == 30
    assert "3 failed QA" in dark["reason"] and dark["captured"] == 13
    ha = _req(rep, "flats Ha")
    assert ha["stage"] == "complete"
    o3 = _req(rep, "flats OIII")
    assert o3["stage"] == "not_in_library" and o3["qa_passed"] == 15
    sets = {(s["filter"], s["exp_s"]): s for s in r["light_sets"]}
    assert sets[("Ha", 600.0)]["missing"] == ["darks 600 s 10/30"]
    assert sets[("Ha", 600.0)]["lights"] == 2
    assert "flats OIII not filed" in sets[("OIII", 600.0)]["missing"]
    assert not r["complete"]
    assert rep["line"].startswith("Calibration: missing RC16 Heart Nebula (darks 600 s 10/30")
    assert r["windows"] is not None and r["capture_minutes"] > 0
    txt = comp.format_report(rep)
    assert "darks 600 s (gain 200, offset 256, 0 C, HCG)" in txt and "short" in txt


def test_completeness_missing_bias_explains_and_plans(tmp_path):
    cfg = _cfg(tmp_path)
    subs_log(cfg, _ago(3), [_heart_light()])
    cal_set(cfg, "rc16", "BIAS", _ago(8), 50, settemp=20.0, readout="HCG")
    cal_set(cfg, "rc16", "BIAS", _ago(60), 50, readout="LCG", prefix="lcg")
    rep = comp.completeness(cfg, "rc16", projects=[_heart()])
    b = _req(rep, "bias")
    assert b["stage"] == "not_captured"
    assert "SET-TEMP 20 C" in b["reason"] and "readout LCG vs HCG" in b["reason"]
    assert "BIAS_AT_SETPOINT" in rep["rigs"][0]["bias_plan"]
    assert "bias HCG 0 C 0/50" in rep["line"]


def test_completeness_desktop_from_syncthing_and_mirror(tmp_path):
    cfg = _heart_setup(tmp_path)
    rep = comp.completeness(cfg, "rc16", projects=[_heart()],
                            pending={"fbias000.fits"}, names=True)
    b = _req(rep, "bias")
    assert b["stage"] == "syncing" and b["on_desktop"] == 49
    # capped cache and the file not in it: unknown, not claimed transferred
    rep2 = comp.completeness(cfg, "rc16", projects=[_heart()], pending=set(),
                             pending_capped=True)
    assert _req(rep2, "bias")["on_desktop"] is None
    # the desktop mirror: 49 of 50 bias there
    mirror = tmp_path / "mirror"
    for t, d, n in b["library_frames"][1:]:
        p = mirror / "Calibration" / t / d / n
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    comp.apply_mirror(rep, mirror)
    b = _req(rep, "bias")
    assert b["stage"] == "syncing" and b["on_desktop"] == 49
    p = mirror / "Calibration" / "BIAS" / b["library_frames"][0][1] / b["library_frames"][0][2]
    p.write_bytes(b"x")
    comp.apply_mirror(rep, mirror)
    assert _req(rep, "bias")["stage"] == "complete"
    ha = _req(rep, "flats Ha")
    assert ha["on_desktop"] == 0 and ha["stage"] == "syncing"


def test_completeness_complete_line(tmp_path):
    cfg = _cfg(tmp_path, dark_target_count=10)
    subs_log(cfg, _ago(3), [_heart_light()])
    cal_set(cfg, "rc16", "BIAS", _ago(5), 50, readout="HCG")
    cal_set(cfg, "rc16", "DARK", _ago(4), 10, readout="HCG")
    cal_set(cfg, "rc16", "FLAT", _ago(2), 15, filter="Ha", exptime=2.0)
    rep = comp.completeness(cfg, "rc16", projects=[_heart()])
    assert rep["rigs"][0]["complete"]
    assert rep["line"] == "Calibration: complete for RC16 Heart Nebula"
    assert comp.morning_line({"rigs": []}) == "Calibration: no lights in the window"


def test_piggy_light_tightness(tmp_path):
    cfg = _pb_cfg(tmp_path)
    cal_set(cfg, "piggyback", "DARK", _ago(4), 2, verdict="fail", codes=["level"],
            prefix="lit", status="quarantine", exptime=400.0, readout="LCG")
    cal_set(cfg, "piggyback", "DARK", _ago(3), 10, exptime=120.0, readout="LCG")
    lt = comp.light_tightness(cq.rig_view(cfg, "piggyback"), "piggyback")
    assert lt["verdict"] == "warn" and lt["light_failures"] == 2
    assert "no filter wheel" in lt["text"]
    subs_log(cfg, _ago(2), [osc(120.0, readout=LCG)])
    rep = comp.completeness(cfg, "piggyback", projects=[_m31()])
    r = rep["rigs"][0]
    assert r["light_tight"]["verdict"] == "warn"
    assert [s["filter"] for s in r["light_sets"]][0] == "OSC"
    assert any(q["label"] == "flats OSC" for q in r["requirements"])


def test_completeness_endpoint_and_morning_card(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler import morning_report as mr
    cfg = _heart_setup(tmp_path)
    monkeypatch.setattr(app_mod, "_config", cfg)
    monkeypatch.setattr(app_mod, "_remoteneed_cache", {"names": {"fbias000.fits"},
                                                       "capped": False})
    monkeypatch.setattr("photonscript.scheduler.calibration_plan.load_projects",
                        lambda c: [_heart()])
    c = TestClient(app_mod.app)
    r = c.get("/api/calibration/completeness?rig=rc16&names=true")
    assert r.status_code == 200
    body = r.json()
    assert body["rigs"][0]["desktop"] == "syncthing remoteneed cache"
    assert any(q["stage"] == "syncing" for q in body["rigs"][0]["requirements"])
    assert "library_frames" in body["rigs"][0]["requirements"][0]
    assert c.get("/api/calibration/completeness?rig=nope").status_code == 404
    w = c.get("/api/calibration/windows?rig=rc16&minutes=60")
    assert w.status_code == 200 and "model" in w.json()
    card = mr.report_card(cfg, _ago(3), projects=[_heart()])
    assert card["completeness"].startswith("Calibration: missing RC16 Heart Nebula")
    assert card["completeness"] in mr.card_lines(card)


def test_calibration_status_cli(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from photonscript import cli
    cfg = _heart_setup(tmp_path)
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)
    monkeypatch.setattr("photonscript.scheduler.calibration_plan.load_projects",
                        lambda c: [_heart()])
    res = CliRunner().invoke(cli.app, ["calibration-status", "--rig", "rc16"])
    assert res.exit_code == 1, res.output
    assert "Calibration completeness" in res.output and "darks 600 s" in res.output


def test_config_fields_on_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    keys = {f[0] for f in _CONFIG_FIELDS}
    for k in ("calibration_completeness_nights", "cooler_history_sample_s",
              "calibration_cool_reach", "calibration_dawn_capture",
              "calibration_bias_when_missing", "camera_readout_since",
              "piggyback_readout_since", "calibration_qa_dark_scale_by_readout",
              "calibration_readout_fill_at_dawn", "cooler_reach_max_power_pct"):
        assert k in keys, k


def test_calibration_folder_need_reads_the_split_folders(tmp_path):
    from photonscript.scheduler.routers.calibration import calibration_folder_need
    cfg = _cfg(tmp_path, syncthing_url="http://st:8384", syncthing_api_key="k",
               syncthing_device_id="DESK")
    calls = []

    class R:
        def __init__(self, files):
            self.files = files

        def json(self):
            return {"files": self.files}

    def get(url, params=None):
        calls.append((url, params["folder"]))
        return R([{"name": r"BIAS\2026-10-09\b1.fits"}] if params["folder"] == "cal-rc16"
                 else [{"name": "DARK/2026-10-09/d1.fits"}])
    got = calibration_folder_need(cfg, ["cal-rc16", "cal-piggy"], get=get)
    assert got == {"b1.fits", "d1.fits"}
    assert calls[0] == ("http://st:8384/rest/db/remoteneed", "cal-rc16")
    assert calibration_folder_need(_cfg(tmp_path), ["x"], get=get) is None

    def boom(url, params=None):
        raise OSError("down")
    assert calibration_folder_need(cfg, ["cal-rc16"], get=boom) is None


def test_judge_all_applies_the_readout_scale():
    def rec(typ, med, ro, i):
        return {"type": typ, "date": "2026-10-06", "name": f"{typ}{i}.fits",
                "exptime": 600.0 if typ == "DARK" else 0.001, "gain": 200,
                "offset": 256, "ccdtemp": 0.0, "settemp": 0.0, "median": med,
                "readout": ro, "instrume": "AP26MC"}
    store = {"frames": {f"BIAS/{i}": rec("BIAS", 254.0, "HCG", i) for i in range(3)}}
    store["frames"]["DARK/hcg"] = rec("DARK", 290.0, "HCG", 0)
    store["frames"]["DARK/none"] = rec("DARK", 290.0, None, 1)
    cq.judge_all(store, rig="rc16", tol=1.0, setpoint=0.0,
                 scales={"HCG": 3.16, "LCG": 1.0}, default_readout="HCG")
    assert store["frames"]["DARK/hcg"]["verdict"] == "pass"
    assert store["frames"]["DARK/none"]["verdict"] == "pass"     # rig default HCG
    cq.judge_all(store, rig="rc16", tol=1.0, setpoint=0.0)
    assert store["frames"]["DARK/hcg"]["verdict"] == "fail"
