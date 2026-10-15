"""Sub doctor: why the last subs were bad and the fix (scheduler/sub_doctor.py,
scheduler/sub_pairs.py, telescope_agent/sub_doctor_live.py).

Fixtures (tests/test_scheduler/fixtures/sub_doctor), trimmed from the live
GET /api/runs/<night> on 2026-10-13 (subs with their scorecard rows, the plan
targets' guided flag) and GET /api/phd2/calibration (history):

  2026-10-12_subs.json  the night that started this: armed UNGUIDED, PHD2 in
      LostLock / Guiding on the 03:53Z calibration graded FAIL (axes 78 deg
      from perpendicular), RC16 subs trailing (ecc 0.63 to 0.79)
  2026-10-10_subs.json  the RC16 focuser off USB (FOCPOS 0 on every RC16 light)
  2026-10-05_subs.json  a healthy stretch (08:00 to 09:30Z, both rigs)
  phd2_calibration_history.json  the calibration records (grade, reasons)

The cloudy and seeing cases are built from the healthy stretch (star counts
cut on both rigs; both rigs soft with round stars), since no stored night has
both rigs graded through a clean cloud bank.
"""
import asyncio
import copy
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from photonscript.scheduler import sub_doctor as sd
from photonscript.scheduler import sub_pairs as sp
from photonscript.shared.config import PhotonScriptConfig

FIX = Path(__file__).parent / "fixtures" / "sub_doctor"


@pytest.fixture(autouse=True)
def _no_local_phd2_logs(monkeypatch):
    """The doctor reads the night's PHD2 guide log when this machine has one
    (2026-10-14); a desktop with real PHD2 logs must not change a test."""
    from photonscript.scheduler import phd2_analysis as pa
    monkeypatch.setattr(pa, "cached_timeline", lambda config, date: None)


def _cfg(tmp_path, **kw):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"),
                              observatory_tz="America/Denver",
                              piggyback_enabled=True, **kw)


def _night(name):
    return json.loads((FIX / f"{name}_subs.json").read_text())


def _cal():
    return json.loads((FIX / "phd2_calibration_history.json").read_text())["history"]


def _replay(cfg, name, **kw):
    d = _night(name)
    return sd.replay_night(cfg, d["night"], d["subs"], plan=d["plan"],
                           cal_history=_cal(), solves={"rc16": None, "piggyback": None},
                           **kw)


def _set_row(rec, check, status, value=None):
    rows = rec["scorecard"]["rows"]
    for r in rows:
        if r[0] == check:
            r[3] = status
            if value is not None:
                r[1] = value
            return
    rows.append([check, value, None, status])


# ------------------------------------------------------------- real nights

def test_tonight_guider_conflict_fires_with_stop_phd2(tmp_path):
    cfg = _cfg(tmp_path)
    rep = _replay(cfg, "2026-10-12")
    assert rep["armed"] == "unguided"
    rc = rep["rigs"]["rc16"]
    gc = [d for d in rc["fired"] if d["cause"] == "guider_conflict"]
    assert gc, rc["fired"]
    d = gc[0]
    assert d["confidence"] == "high"
    assert d["t"].startswith("2026-10-13T04:2")            # 04:27Z, 3rd trailed sub
    assert d["action"]["id"] == "stop_phd2" and d["action"]["auto"]
    assert d["action"]["enabled"] is False                 # default: alert only
    ev = d["evidence"]
    assert ev["armed"] == "unguided" and ev["calibration"]["grade"] == "FAIL"
    assert "78 deg" in ev["calibration"]["reasons"][0]
    assert ev["ecc"] >= 0.6
    assert "UNGUIDED" in d["headline"] and "Stop PHD2" in d["fix"]
    # the Piggy-600 trails too once PHD2 guides on the bad calibration
    pg = rep["rigs"]["piggyback"]["fired"]
    assert pg and pg[0]["cause"] == "guider_conflict"
    assert pg[0]["t"].startswith("2026-10-13T08:4")


def test_focuser_zero_night(tmp_path):
    rep = _replay(_cfg(tmp_path), "2026-10-10")
    f = rep["rigs"]["rc16"]["fired"]
    assert f and f[0]["cause"] == "focuser" and f[0]["confidence"] == "high"
    assert f[0]["action"]["id"] == "check_focuser"
    assert "position 0" in f[0]["headline"]
    assert not rep["rigs"]["piggyback"]["fired"]           # its focuser was fine


def test_healthy_stretch_no_diagnosis_no_push(tmp_path):
    cfg = _cfg(tmp_path)
    rep = _replay(cfg, "2026-10-05")
    assert all(not r["timeline"] for r in rep["rigs"].values())
    d = _night("2026-10-05")
    for rig in ("rc16", "piggyback"):
        win = sp.rig_subs(d["subs"], rig)[-3:]
        diag = sd.diagnose(cfg, rig, win, subs=d["subs"])
        assert diag is None
        out = sd.record(cfg, d["night"], rig, diag)
        assert out == {"event": None, "push": None, "remedy": None}
    assert not (Path(cfg.data_dir) / "runs" / f"{d['night']}_events.jsonl").exists()


def test_clouds_both_rigs(tmp_path):
    cfg = _cfg(tmp_path)
    d = _night("2026-10-05")
    subs = copy.deepcopy(d["subs"])
    cut = "2026-10-06T09:00"
    for r in subs:
        if (r.get("start_utc") or "") >= cut:
            r["stars"] = max(3, int((r.get("stars") or 0) * 0.2))
            _set_row(r, "stars", "fail", r["stars"])
    rep = sd.replay_night(cfg, d["night"], subs, plan=d["plan"], solves={})
    for rig in ("rc16", "piggyback"):
        f = rep["rigs"][rig]["fired"]
        assert f and f[0]["cause"] == "clouds", (rig, f)
    rc = rep["rigs"]["rc16"]["fired"][0]
    assert rc["confidence"] == "high" and rc["evidence"]["both_rigs"]
    assert rc["action"]["id"] == "wait_clouds"


def _soften(subs, rigs, cut="2026-10-06T09:00"):
    for r in subs:
        if (r.get("start_utc") or "") >= cut and r["rig"] in rigs:
            r["hfr"] = (r.get("hfr") or 3) * 1.8
            _set_row(r, "hfr", "fail", r["hfr"])
            _set_row(r, "ecc", "pass")


def test_seeing_both_rigs_soft_round(tmp_path):
    cfg = _cfg(tmp_path)
    d = _night("2026-10-05")
    subs = copy.deepcopy(d["subs"])
    _soften(subs, ("rc16", "piggyback"))
    rep = sd.replay_night(cfg, d["night"], subs, plan=d["plan"], solves={})
    f = rep["rigs"]["rc16"]["fired"]
    assert f and f[0]["cause"] == "seeing" and f[0]["action"]["id"] == "none"


def test_focus_rc16_soft_piggy_sharp_suggests_af(tmp_path):
    cfg = _cfg(tmp_path)
    d = _night("2026-10-05")
    subs = copy.deepcopy(d["subs"])
    _soften(subs, ("rc16",))
    rep = sd.replay_night(cfg, d["night"], subs, plan=d["plan"], solves={})
    f = rep["rigs"]["rc16"]["fired"]
    assert f and f[0]["cause"] == "focus" and f[0]["confidence"] == "medium"
    assert f[0]["action"]["id"] == "run_af"
    assert f[0]["evidence"]["other_rig_sharp"]
    assert not rep["rigs"]["piggyback"]["fired"]


# ------------------------------------------------------------- pairs

def _rec(rig, start, end, ecc=0.4, hfr=3.0, stars=300, fails=()):
    rows = [["ecc", ecc, 0.6, "fail" if "ecc" in fails else "pass"],
            ["hfr", hfr, 10, "fail" if "hfr" in fails else "pass"],
            ["stars", stars, [5, 5000], "fail" if "stars" in fails else "pass"]]
    return {"rig": rig, "file": f"{rig}_{start}.fits", "filter": "Ha", "exp_s": 180,
            "start_utc": f"2026-10-13T{start}Z", "end_utc": f"2026-10-13T{end}Z",
            "ecc": ecc, "hfr": hfr, "stars": stars, "passed_qa": not fails,
            "scorecard": {"rows": rows}}


def test_pair_for_overlap_and_verdicts():
    a = _rec("rc16", "04:00:00", "04:03:00", ecc=0.75, fails=("ecc",))
    b = _rec("piggyback", "03:58:00", "04:05:30", ecc=0.8, fails=("ecc",))
    c = _rec("piggyback", "04:05:40", "04:13:10")
    p = sp.pair_for("rc16", a, [a, b, c])
    assert [x["file"] for x in p["partners"]] == [b["file"]]
    assert p["verdict"] == "mount"
    b2 = _rec("piggyback", "03:58:00", "04:05:30")
    assert sp.pair_for("rc16", a, [a, b2])["verdict"] == "guider_rc16"
    assert sp.pair_for("piggyback", b2, [a, b2])["verdict"] == "guider_rc16"
    s1 = _rec("rc16", "04:00:00", "04:03:00", hfr=12, fails=("hfr",))
    s2 = _rec("piggyback", "03:58:00", "04:05:30", hfr=6, fails=("hfr",))
    assert sp.pair_for("rc16", s1, [s1, s2])["verdict"] == "seeing"
    assert sp.pair_for("rc16", s1, [s1, b2])["verdict"] == "focus_rc16"
    k1 = _rec("rc16", "04:00:00", "04:03:00", stars=4, fails=("stars",))
    k2 = _rec("piggyback", "03:58:00", "04:05:30", stars=3, fails=("stars",))
    assert sp.pair_for("rc16", k1, [k1, k2])["verdict"] == "clouds"
    assert sp.pair_for("rc16", a, [a])["verdict"] == "unpaired"
    summ = sp.paired_summary([a, b, c], "rc16", n=3)
    assert summ["dominant"] == "mount" and summ["n"] == 1


def test_excluded_subs_are_left_out():
    a = _rec("rc16", "04:00:00", "04:03:00")
    a["scorecard"]["rows"].append(["roof", "parked", "sky", "fail"])
    assert sp.sub_state(a)["excluded"] == "roof failed"
    t = _rec("rc16", "04:00:00", "04:03:00", fails=("ecc",))
    t["target"] = "TPoint point 01/45 alt 38.1 az 72.6 (east)"
    assert sp.sub_state(t)["excluded"] == "test sub"


# ------------------------------------------------------------- geometry

def test_trail_estimate_and_sky_direction():
    t = sd.trail_estimate(10.0, 0.75, 180, 0.236)
    # sigma 8.49 px, q 0.661, minor 6.90 px -> L = 3.464 * 6.90 * 0.75 / 0.661
    assert t["length_px"] == pytest.approx(27.1, abs=0.2)
    assert t["rate_arcsec_min"] == pytest.approx(27.1 * 0.236 / 3, abs=0.05)
    assert sd.trail_estimate(10.0, None, 180, 0.236) == {}
    # camera PA 0 (+y north), usual parity: +x points west, so a trail along
    # the image x axis runs along RA, along y along Dec
    solve = {"pa": 0.0, "parity": -1, "night": "2026-10-12"}
    assert sd.sky_direction(0.0, solve, 0.236)["along"] == "RA"
    assert sd.sky_direction(90.0, solve, 0.236)["along"] == "Dec"
    # the RC16 at PA 85.3: the image y axis is nearly east-west
    rc = sd.sky_direction(90.0, {"pa": 85.3, "parity": -1}, 0.236)
    assert rc["along"] == "RA" and rc["sky_pa_deg"] == pytest.approx(85.3, abs=0.1)
    assert sd.sky_direction(10.0, None, 0.236) == {}


def test_elongation_from_star_table():
    import math
    n = 40
    tbl = {"x": [10.0 * i for i in range(n)], "y": [5.0 * i for i in range(n)],
           "hfr": [3.0] * n, "ecc": [0.7] * n, "theta": [math.radians(30)] * n,
           "w": 400, "h": 200, "ecc_def": "sqrt(1-(b/a)^2)"}
    el = sd.elongation(tbl)
    assert el["axis_deg"] == pytest.approx(30.0, abs=0.1) and el["R"] == 1.0
    assert sd.elongation({"x": [1], "theta": None}) == {}


def test_tracking_unguided_suggests_drift_cap(tmp_path):
    cfg = _cfg(tmp_path)
    subs = []
    for i, (a, b) in enumerate((("04:00:00", "04:05:00"), ("04:05:10", "04:10:10"),
                                ("04:10:20", "04:15:20"))):
        r = _rec("rc16", a, b, ecc=0.72, hfr=6.0, fails=("ecc",))
        r.update(exp_s=300, guide_state="stopped", elong_axis_deg=88.0, elong_r=0.8)
        subs.append(r)
        p = _rec("piggyback", a, b, ecc=0.7, hfr=3.0, fails=("ecc",))
        p.update(exp_s=300, guide_state="stopped")
        subs.append(p)
    rc = sp.rig_subs(subs, "rc16")
    d = sd.diagnose(cfg, "rc16", rc, subs=subs, ctx={
        "armed": "unguided", "solve": {"pa": 0.0, "parity": -1, "night": "x"}})
    assert d["cause"] == "tracking" and d["confidence"] == "high"
    ev = d["evidence"]
    assert ev["shared_mount"] and ev["sky"]["along"] == "Dec"
    assert ev["kind"] == "Dec drift"
    assert d["action"]["id"].startswith("shorten_subs:")
    assert float(d["action"]["id"].split(":")[1]) < 300
    # guided night: no shortening, check the guiding
    g = sd.diagnose(cfg, "rc16", rc, subs=subs, ctx={"armed": "guided",
                                                     "phd2": {"state": "guiding",
                                                              "rms_arcsec": 0.9}})
    assert g["cause"] == "tracking" and g["action"]["id"] == "check_tracking"


def test_tiny_rms_while_trailing_is_a_non_star_lock(tmp_path):
    cfg = _cfg(tmp_path)
    subs = [_rec("rc16", a, b, ecc=0.75, fails=("ecc",))
            for a, b in (("04:00:00", "04:03:00"), ("04:03:10", "04:06:10"))]
    d = sd.diagnose(cfg, "rc16", subs, subs=subs, ctx={
        "armed": "guided", "phd2": {"state": "guiding", "rms_arcsec": 0.22},
        "calibration": {"grade": "PASS"}})
    assert d["cause"] == "guider_conflict" and d["action"]["id"] == "reselect_guide_star"
    subs[0]["guide_lock"] = "non-star"
    d = sd.diagnose(cfg, "rc16", subs, subs=subs, ctx={
        "armed": "guided", "phd2": {"state": "guiding", "rms_arcsec": 1.5},
        "calibration": {"grade": "PASS"}})
    assert d["confidence"] == "high" and d["evidence"]["nonstar_guard"]


def test_one_bad_sub_is_not_a_diagnosis(tmp_path):
    cfg = _cfg(tmp_path)
    a = _rec("rc16", "04:00:00", "04:03:00", ecc=0.75, fails=("ecc",))
    b = _rec("rc16", "04:03:10", "04:06:10")
    assert sd.diagnose(cfg, "rc16", [a], subs=[a]) is None
    assert sd.diagnose(cfg, "rc16", [a, b], subs=[a, b]) is None


# ------------------------------------------------------------- record / events

def _diag(cfg):
    d = _night("2026-10-12")
    rep = sd.replay_night(cfg, d["night"], d["subs"], plan=d["plan"],
                          cal_history=_cal(), solves={}, rig="rc16")
    return d["night"], rep["rigs"]["rc16"]["fired"][-1]


def test_record_pushes_once_per_cause_and_writes_events(tmp_path):
    from photonscript.shared import phd2_store as store
    from photonscript.shared.night_events import events_path
    cfg = _cfg(tmp_path)
    night, diag = _diag(cfg)
    assert diag["cause"] == "guider_conflict"
    t0 = datetime(2026, 10, 13, 4, 28)
    o1 = sd.record(cfg, night, "rc16", diag, now=t0)
    assert o1["push"] and "Fix:" in o1["push"] and "stop_phd2" in o1["push"]
    assert o1["remedy"] is None                        # the doctor never acts
    assert "the night pilot's" in o1["push"]
    o2 = sd.record(cfg, night, "rc16", diag, now=t0)
    assert o2["push"] is None and o2["event"]          # every diagnosis: an event
    o3 = sd.record(cfg, night, "rc16", None, now=t0)
    assert o3["event"]["cause"] == "none" and o3["event"]["value"] == "none"
    assert sd.record(cfg, night, "rc16", None, now=t0)["event"] is None
    evs = [e for e in store.read_jsonl(events_path(cfg, night))
           if e.get("kind") == sd.EVENT_KIND]
    assert len(evs) == 3
    for e in evs:
        assert set(sd.EVENT_FIELDS) <= set(e)
    e = evs[0]
    assert e["cause"] == "guider_conflict" and e["confidence"] == "high"
    assert e["suggested_action"]["id"] == "stop_phd2"
    assert e["evidence"]["calibration"]["grade"] == "FAIL"
    # back again later the same night: no second push for the same cause
    assert sd.record(cfg, night, "rc16", diag, now=t0)["push"] is None
    st = sd.load_state(cfg, night)["rigs"]["rc16"]
    assert st["pushed"] == ["guider_conflict"] and st["current"]["cause"] == "guider_conflict"


def test_record_observe_and_off(tmp_path):
    cfg = _cfg(tmp_path, sub_doctor_mode="observe", sub_doctor_stop_phd2_unguided=True)
    night, diag = _diag(cfg)
    o = sd.record(cfg, night, "rc16", diag)
    assert o["event"] and o["push"] is None and o["remedy"] is None
    off = _cfg(tmp_path / "off", sub_doctor_mode="off")
    assert sd.record(off, night, "rc16", diag) == {"event": None, "push": None,
                                                   "remedy": None}


def test_record_never_returns_a_remedy_even_with_the_old_keys(tmp_path):
    """One actor (2026-10-13): the retired remedy keys are readable but do
    nothing; the suggested action stays in the diagnosis for the pilot."""
    cfg = _cfg(tmp_path, sub_doctor_stop_phd2_unguided=True,
               sub_doctor_request_af=True, sub_doctor_shorten_subs=True)
    night, diag = _diag(cfg)
    assert diag["action"]["id"] == "stop_phd2" and diag["action"]["auto"]
    assert diag["action"]["enabled"] is False
    o = sd.record(cfg, night, "rc16", diag)
    assert o["remedy"] is None and o["event"]["suggested_action"]["id"] == "stop_phd2"


def test_caps_json_cap_is_retired():
    for name in ("apply_caps", "set_cap", "cap_for", "caps_path", "note_remedy"):
        assert not hasattr(sd, name), name
    src = (Path(__file__).resolve().parents[2] / "photonscript/scheduler/armer.py"
           ).read_text(encoding="utf-8")
    assert "apply_caps" not in src and "sub_doctor import" not in src


# ------------------------------------------------------------- live agent side

class _FakePHD2:
    def __init__(self, state="Guiding"):
        self.state, self.stopped = state, 0

    async def get_app_state(self):
        return self.state

    async def stop_capture(self):
        self.stopped += 1


def _agent(cfg, rig="rc16", state="lost_star"):
    from photonscript.shared.models import GuidingMetrics
    g = GuidingMetrics()
    g.state = state
    return SimpleNamespace(config=cfg, rig=rig, phd2=_FakePHD2(),
                           state=SimpleNamespace(guiding=g),
                           _af_reports={}, _focuser_info={})


def _seed_subs(cfg, upto="2026-10-13T04:28"):
    from photonscript.scheduler.runs import append_sub_record
    d = _night("2026-10-12")
    for r in d["subs"]:
        if (r.get("start_utc") or "") <= upto:
            append_sub_record(cfg, d["night"], r)
    return d["night"]


def _arm_unguided(cfg, monkeypatch):
    """Armed unguided: the persisted armer state, and no in-process armer
    left over from another test (armed_mode asks that one first)."""
    import sys
    p = Path(cfg.data_dir) / "armer_state.json"
    p.write_text(json.dumps({"state": "RUNNING", "guiding_override": "unguided"}))
    app_mod = sys.modules.get("photonscript.scheduler.app")
    if app_mod is not None:
        monkeypatch.setattr(app_mod, "_armer", None, raising=False)
    assert sd.armed_mode(cfg) == "unguided"


def test_after_sub_pushes_but_never_commands_phd2(tmp_path, monkeypatch):
    from photonscript.scheduler import phd2_calibration as pc
    from photonscript.telescope_agent import phd2_ops, sub_doctor_live as live
    cfg = _cfg(tmp_path, sub_doctor_stop_phd2_unguided=True)
    night = _seed_subs(cfg)
    _arm_unguided(cfg, monkeypatch)
    fail = next(r for r in _cal() if r["t_utc"] == "2026-10-13T03:53:42Z")
    monkeypatch.setattr(pc, "load_active", lambda config, seed=False: fail)
    pushes = []

    async def fake_notify(config, message, title="", priority=0, **kw):
        pushes.append((title, message, priority))
        return True
    monkeypatch.setattr("photonscript.shared.pushover.notify", fake_notify)
    phd2_ops._reset_for_tests()
    ag = _agent(cfg)
    d = asyncio.run(live.after_sub(ag, night))
    assert d["cause"] == "guider_conflict"
    assert pushes[0][0].startswith("PhotonScript sub doctor") and pushes[0][2] == 1
    assert ag.phd2.stopped == 0                    # the night pilot's job
    st = sd.load_state(cfg, night)["rigs"]["rc16"]
    assert not st.get("remedies")
    # the next sub: no second push
    n = len(pushes)
    asyncio.run(live.after_sub(ag, night))
    assert ag.phd2.stopped == 0 and len(pushes) == n
    for name in ("remedy", "_stop_phd2", "_run_af"):
        assert not hasattr(live, name), name


def test_after_sub_off_does_nothing(tmp_path):
    from photonscript.telescope_agent import sub_doctor_live as live
    cfg = _cfg(tmp_path, sub_doctor_mode="off")
    night = _seed_subs(cfg)
    assert asyncio.run(live.after_sub(_agent(cfg), night)) is None
    assert not sd.state_path(cfg, night).exists()


# ------------------------------------------------------------- reports

def test_morning_line_and_night_lines(tmp_path):
    cfg = _cfg(tmp_path)
    night = _seed_subs(cfg, upto="2026-10-13T12")
    from photonscript.scheduler.runs import runs_dir
    (runs_dir(cfg) / f"{night}_plan.json").write_text(json.dumps(
        {"targets": [{"name": "Heart Nebula", "guided": False}]}))
    line = sd.morning_line(cfg, night)
    assert line.startswith("What went wrong and the fix: RC16: ")
    assert "guider_conflict" in line
    rep = sd.local_night(cfg, night)
    lines = sd.night_lines(cfg, rep)
    assert any("guider_conflict" in x and "Fix:" in x for x in lines)
    assert sd.morning_line(cfg, "2026-01-01") is None
    from photonscript.scheduler.morning_report import card_line_priority, PRI_RIG
    assert card_line_priority(line) == PRI_RIG


def test_api_endpoints(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    night = _seed_subs(cfg)
    client = TestClient(app.app)
    r = client.get(f"/api/runs/{night}/sub-doctor").json()
    assert "rc16" in r["rigs"] and "timeline" not in r["rigs"]["rc16"]
    assert isinstance(r["lines"], list)
    p = client.get(f"/api/sub-doctor/pairs?date={night}&rig=rc16&n=3").json()
    assert p["rig"] == "rc16" and p["n"] == 3
    s = client.get(f"/api/sub-doctor?date={night}").json()
    assert s["mode"] == "alert" and set(s["rigs"]) == {"rc16", "piggyback"}


# ------------------------------------------------------------- 2026-10-14

def _trailing_pair(guide_state="lost_star"):
    """Both rigs trailing together after the meridian flip (2026-10-14):
    unguided, PHD2 open but LostStar / Looping, no guiding session."""
    subs = []
    for a, b in (("06:00:00", "06:05:00"), ("06:05:10", "06:10:10"),
                 ("06:10:20", "06:15:20")):
        r = _rec("rc16", a, b, ecc=0.72, hfr=6.0, fails=("ecc",))
        r.update(exp_s=300, guide_state=guide_state, guide_rms=34.8,
                 elong_axis_deg=88.0, elong_r=0.8)
        q = _rec("piggyback", a, b, ecc=0.7, hfr=3.0, fails=("ecc",))
        q.update(exp_s=300, guide_state=guide_state, guide_rms=34.8)
        subs += [r, q]
    return subs


def test_1014_lost_star_no_guide_log_session_is_tracking_not_guider(tmp_path):
    cfg = _cfg(tmp_path)
    subs = _trailing_pair()
    old_cal = {"grade": "FAIL", "t_utc": "2026-10-10T03:00:00Z",
               "reasons": ["axes 78 deg from perpendicular"]}
    for rig in ("rc16", "piggyback"):
        win = sp.rig_subs(subs, rig)
        d = sd.diagnose(cfg, rig, win, subs=subs, ctx={
            "armed": "unguided", "phd2": {"state": "lost_star"},
            "calibration": old_cal,
            "guide_log": {"frames": 0, "pulses": 0},
            "pointing": {"pier": "West", "ha_h": 1.23}})
        assert d["cause"] == "tracking", (rig, d["headline"])
        assert "guider_conflict" not in [x["cause"] for x in d["secondary"]]
        ev = d["evidence"]
        assert ev["shared_mount"] and ev["pier"] == "West" and ev["ha_h"] == 1.23
        assert ev["phd2_guiding"] is False
        assert "pier West, HA +1.2 h" in d["headline"]
        assert "TPoint model has samples on that side" in d["fix"]
        assert "not the guider" in d["fix"]


def test_1014_lost_star_without_a_log_needs_tonight_evidence(tmp_path):
    cfg = _cfg(tmp_path)
    subs = _trailing_pair()
    win = sp.rig_subs(subs, "rc16")
    base = {"armed": "unguided", "phd2": {"state": "lost_star"}}
    # no log, no calibration / guider start tonight: the mount, not PHD2
    d = sd.diagnose(cfg, "rc16", win, subs=subs, ctx=dict(base))
    assert d["cause"] == "tracking"
    # Looping reads as stopped: never the guider
    d = sd.diagnose(cfg, "rc16", win, subs=subs,
                    ctx={**base, "phd2": {"state": "stopped"}})
    assert d["cause"] == "tracking"
    # a calibration tonight (the 2026-10-12 shape): PHD2 was guiding
    d = sd.diagnose(cfg, "rc16", win, subs=subs, ctx={
        **base, "calibration": {"grade": "FAIL", "t_utc": "2026-10-13T05:30:00Z"}})
    assert d["cause"] == "guider_conflict" and d["confidence"] == "high"
    # the guide log shows a guiding session over the window: guiding
    d = sd.diagnose(cfg, "rc16", win, subs=subs, ctx={
        **base, "guide_log": {"frames": 40, "pulses": 31}})
    assert d["cause"] == "guider_conflict"
    assert d["evidence"]["phd2_guiding"] and d["evidence"]["guide_log"]["pulses"] == 31
    # Guiding needs no log
    d = sd.diagnose(cfg, "rc16", win, subs=subs, ctx={
        **base, "phd2": {"state": "guiding"}, "guide_log": {"frames": 0, "pulses": 0}})
    assert d["cause"] == "guider_conflict"


def test_guide_activity_counts_frames_and_pulses():
    from datetime import timedelta
    from photonscript.scheduler import phd2_analysis as pa
    assert pa.guide_activity(None, datetime(2026, 10, 15, 6), datetime(2026, 10, 15, 7)) is None
    empty = pa.GuideTimeline([], SimpleNamespace(observatory_tz="UTC"))
    assert pa.guide_activity(empty, datetime(2026, 10, 15, 6),
                             datetime(2026, 10, 15, 7)) == {"frames": 0, "pulses": 0}
    t0 = datetime(2026, 10, 15, 6)
    tl = pa.GuideTimeline([], SimpleNamespace(observatory_tz="UTC"))
    fr = [{"drop": False, "ra_ms": 120, "dec_ms": 0},
          {"drop": False, "ra_ms": 0, "dec_ms": 0},
          {"drop": True, "ra_ms": 0, "dec_ms": 0}]
    tl.frames = [(t0 + timedelta(seconds=10 * i), f, 1.0, 1) for i, f in enumerate(fr)]
    tl._keys = [x[0] for x in tl.frames]
    a = pa.guide_activity(tl, t0, t0 + timedelta(minutes=5))
    assert a == {"frames": 3, "pulses": 1}


def test_after_sub_1014_reads_the_guide_log_and_pier(tmp_path, monkeypatch):
    from photonscript.scheduler import phd2_calibration as pc
    from photonscript.scheduler.runs import append_sub_record
    from photonscript.telescope_agent import sub_doctor_live as live
    cfg = _cfg(tmp_path)
    night = "2026-10-12"
    for r in _trailing_pair():
        append_sub_record(cfg, night, r)
    _arm_unguided(cfg, monkeypatch)
    monkeypatch.setattr(pc, "load_active", lambda config, seed=False: None)
    seen = {}

    def fake_log(config, n, window, timeline=None):
        seen["n"] = len(window)
        return {"frames": 0, "pulses": 0}
    monkeypatch.setattr(sd, "guide_log", fake_log)

    async def fake_notify(*a, **k):
        return True
    monkeypatch.setattr("photonscript.shared.pushover.notify", fake_notify)
    ag = _agent(cfg, state="lost_star")
    ag.state.mount_side_of_pier = "West"
    d = asyncio.run(live.after_sub(ag, night))
    assert seen["n"] == 3
    assert d["cause"] == "tracking" and d["evidence"]["pier"] == "West"
