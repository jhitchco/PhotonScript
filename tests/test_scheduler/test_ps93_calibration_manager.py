"""PS-93 PHD2 calibration manager: grading (09-25 / 09-26 guide-log fixtures
and get_calibration_data), when (needs_calibration), where (the field
picker), the NINA slot + lint, the after-flip Dec runaway check, the armer
plan / mid-night re-dispatch, the API and the seed from the guide logs."""
import json
import math
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import phd2_analysis as pa
from photonscript.scheduler import phd2_calibration as pc
from photonscript.scheduler import phd2_logs as pl
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.sequence_lint import _exec_items, lint
from photonscript.scheduler.tracking_test import altitude, hour_angle
from photonscript.shared import guide_motion as gm
from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget

FIX = Path(__file__).parent / "fixtures" / "phd2"
N26 = "PHD2_GuideLog_2026-09-26_120453.txt"
N25 = "PHD2_GuideLog_2026-09-25_192414.txt"
FIELD = {"name": "NGC 6633", "ra_hours": 18.455, "dec_degrees": 6.57,
         "ha_hours": 0.6}


def _cfg(tmp_path=None, **kw):
    if tmp_path is not None:
        kw.setdefault("data_dir", tmp_path / "data")
    return PhotonScriptConfig(_env_file=None, **kw)


def _log_cals(name):
    secs = pl.parse_guide_log((FIX / name).read_text(encoding="utf-8"), name)
    out = {}
    for i, s in enumerate(x for x in secs if x["kind"] == "calibration"):
        c = pa._cal_record(i, s, _cfg())
        out[c["start_local"][11:16]] = pc.graded(pc.record_from_log(c, s["header"]))
    return out


# --- grading ----------------------------------------------------------------

def test_grades_of_the_0926_and_0925_calibrations():
    c26, c25 = _log_cals(N26), _log_cals(N25)
    a = c26["20:17"]                       # Dec 0, ortho 9.2, 3 steps of 250 ms
    assert a["grade"] == pc.FAIL and any("9.2 deg" in r for r in a["reasons"])
    assert any("few steps" in w and "50 ms" in w for w in a["warnings"])
    assert 45 <= a["recommended_step_ms"] <= 55
    b = c26["20:51"]                       # the night's best ortho (1.8) at Dec 66.7
    assert b["grade"] == pc.WARN and not b["reasons"]
    assert any("Dec 66.7" in w for w in b["warnings"])
    assert c26["21:42"]["grade"] == pc.FAIL           # the 15.1 deg one the night used
    assert any("15.1" in r for r in c26["21:42"]["reasons"])
    stuck = c25["21:18"]                   # 09-25: the star never moved
    assert stuck["grade"] == pc.FAIL
    assert any("calibration failed" in r for r in stuck["reasons"])
    assert any("moved only 1.7 px" in r for r in stuck["reasons"])
    assert c26["21:32"]["grade"] == pc.FAIL            # aborted, star standing still


def _api_cal(x_angle, x_rate, y_angle, y_rate, dec=0.0, **kw):
    d = {"calibrated": True, "xAngle": x_angle, "xRate": x_rate, "xParity": "+",
         "yAngle": y_angle, "yRate": y_rate, "yParity": "+", "declination": dec}
    d.update(kw)
    return d


def test_get_calibration_data_grades_like_the_log_record():
    log = _log_cals(N26)["20:17"]
    mount = {"SiderealTime": 19.35, "RightAscension": 19.0, "Declination": 0.0,
             "SideOfPier": "pierEast", "Altitude": 57.7}
    rec = pc.graded(pc.record_from_api(
        _api_cal(100.2, 41.896, 1.0, 44.377, 0.0), mount=mount,
        steps={"West": 3, "East": 3, "North": 3, "South": 3},
        moved_px={"West": 31.4, "North": 33.3},
        live={"cal_distance_px": 25.0, "scale_arcsec_px": log["scale_arcsec_px"]},
        speeds={"ra": log["ra_speed"], "dec": log["dec_speed"]}))
    assert rec["ortho_err_deg"] == log["ortho_err_deg"] == 9.2
    assert rec["ha_hr"] == 0.35 and rec["pier_side"] == "East"
    assert rec["grade"] == log["grade"] == pc.FAIL
    assert rec["reasons"] == log["reasons"]
    assert rec["recommended_step_ms"] == log["recommended_step_ms"]
    assert rec["ra"]["parity"] == "+"


def test_a_good_calibration_passes_and_a_parity_change_fails():
    good = {"result": "complete", "dec_deg": 5.0, "ha_hr": 0.5, "pier_side": "West",
            "ra": {"angle_deg": 10.0, "rate_px_s": 29.0, "parity": "+"},
            "dec": {"angle_deg": 100.5, "rate_px_s": 29.2, "parity": "+"},
            "steps": {"West": 12, "North": 12}, "moved_px": {"West": 25, "North": 25},
            "distance_px": 25, "scale_arcsec_px": 0.255, "ra_speed": 7.5,
            "dec_speed": 7.5}
    g = pc.grade(good)
    assert g["grade"] == pc.PASS, g
    flipped = dict(good, dec=dict(good["dec"], parity="-"))
    g2 = pc.grade(flipped, prev=dict(good, grade=pc.PASS))
    assert g2["grade"] == pc.FAIL and "parity" in g2["reasons"][0]
    # the other pier side may have the other parity
    g3 = pc.grade(flipped, prev=dict(good, grade=pc.PASS, pier_side="East"))
    assert g3["grade"] == pc.PASS
    # uncalibrated PHD2 is a failure
    r = pc.record_from_api({"calibrated": False})
    assert r["result"] == "failed" and pc.grade(r)["grade"] == pc.FAIL


def test_ratio_is_judged_only_near_the_equator():
    base = {"result": "complete", "ra": {"angle_deg": 0.0, "rate_px_s": 15.0},
            "dec": {"angle_deg": 90.0, "rate_px_s": 30.0},
            "steps": {"West": 12, "North": 12}}
    assert pc.grade(dict(base, dec_deg=0.0))["grade"] == pc.FAIL   # 0.5 vs 1.0
    g = pc.grade(dict(base, dec_deg=65.0))                         # high Dec: WARN
    assert g["grade"] == pc.WARN and any("Dec 65" in w for w in g["warnings"])


def test_recommended_step():
    assert pc.recommended_step_ms(25, 41.896) == 50
    assert pc.recommended_step_ms(None, 41.9) is None
    assert pc.recommended_step_ms(25, 0) is None


# --- when -------------------------------------------------------------------

def test_needs_calibration_matrix():
    now = datetime(2026, 10, 2, 1, 0)
    fresh = {"grade": pc.PASS, "t_utc": "2026-09-30T02:00:00Z",
             "profile": "OAG", "binning": 2, "scale_arcsec_px": 0.25}
    auto = _cfg()
    need = lambda rec, live=None, cfg=auto, req=None: pc.needs_calibration(  # noqa: E731
        rec, live, cfg, now, req)
    assert need(None)["needed"] and "no calibration" in need(None)["reason"]
    assert need(dict(fresh, grade=pc.FAIL, reasons=["axes 15 deg"]))["needed"]
    assert not need(fresh)["needed"]
    assert not need(dict(fresh, grade=pc.WARN))["needed"]
    old = dict(fresh, t_utc="2026-08-01T02:00:00Z")
    assert need(old)["needed"] and "days old" in need(old)["reason"]
    assert need(fresh, {"binning": 1})["needed"]
    assert "profile" in need(fresh, {"profile": "Piggy"})["reason"]
    assert need(fresh, {"scale_arcsec_px": 0.5})["needed"]
    assert not need(fresh, {"scale_arcsec_px": 0.251, "binning": 2})["needed"]
    assert need(fresh, {"calibrated": False})["needed"]
    assert need(fresh, req={"mode": "next", "t_utc": "x"})["needed"]
    assert not need(None, cfg=_cfg(phd2_cal_mode="never"))["needed"]
    assert need(fresh, cfg=_cfg(phd2_cal_mode="always"))["needed"]
    assert _cfg().phd2_cal_mode == "auto"


# --- where ------------------------------------------------------------------

@pytest.mark.parametrize("when", [datetime(2026, 10, 2, 1, 25),   # 10-01 dusk
                                  datetime(2026, 4, 15, 3, 0)])
@pytest.mark.parametrize("side_ha", [-2.0, 1.5, None])
def test_field_picker_window(when, side_ha):
    cfg = _cfg()
    f = pc.pick_calibration_field(cfg, when, side_ha)
    lat, lon = cfg.observatory_lat, cfg.observatory_lon
    ha = hour_angle(f["ra_hours"], when, lon)
    assert -5.0 <= f["dec_degrees"] <= 15.0
    assert 0.25 <= abs(ha) <= 1.0
    assert (ha < 0) == (side_ha is not None and side_ha < 0)
    assert f["side"] == ("east" if ha < 0 else "west")
    assert altitude(f["ra_hours"], f["dec_degrees"], when, lat, lon) >= 50.0
    # no meridian crossing within 15 min
    later = hour_angle(f["ra_hours"], when + timedelta(minutes=15), lon)
    assert (later < 0) == (ha < 0)


def test_field_picker_prefers_a_listed_cluster():
    f = pc.pick_calibration_field(_cfg(), datetime(2026, 10, 2, 1, 25), 1.0)
    assert f["source"] == "list" and f["name"] in ("NGC 6633", "IC 4756",
                                                   "NGC 6709", "IC 4665")


# --- the NINA slot ------------------------------------------------------------

def _seq(guided=True, dusk=True, n=2):
    targets = [NinaSequenceTarget(
        name=name, ra_hours=ra, dec_degrees=dec, start_guiding=guided,
        exposures=[ExposurePlan(filter_type=FilterType.HA, exposure_seconds=600,
                                count=10, gain=200, offset=256)])
        for name, ra, dec in (("Heart Nebula", 2.55, 61.5),
                              ("Cat's Eye Nebula", 17.98, 66.6))[:n]]
    s = build_sequence_for_night("PS93", targets)
    if dusk:
        s.wait_until_local = "19:30:00"
    return s


def _gen(cal_field=None, **kw):
    return json.loads(nsj.generate_nina_json(_seq(**kw), cal_field=cal_field))


def _short(it):
    return it.get("Name") or it.get("$type", "").split(",")[0].rsplit(".", 1)[-1]


def _slot(seq):
    return next(it for it in _exec_items(seq) if it.get("Name") == pc.CONTAINER_NAME)


def test_no_slot_without_a_field_and_the_sequence_is_unchanged():
    a = nsj.generate_nina_json(_seq())
    b = nsj.generate_nina_json(_seq(), cal_field=None)
    assert a == b and pc.CONTAINER_NAME not in a
    assert pc.CONTAINER_NAME not in nsj.generate_nina_json(_seq(guided=False),
                                                           cal_field=FIELD)


def test_slot_contents_and_placement_after_the_twilight_af(monkeypatch):
    monkeypatch.setenv("PS_PHD2_CAL_HOLD_S", "300")
    seq = _gen(FIELD)
    slot = _slot(seq)
    kinds = [_short(it) for it in _exec_items(slot)]
    assert kinds == ["SendToPushover", "SetTracking", "SwitchFilter",
                     "SlewScopeToRaDec", "Center", "StartGuiding", "StopGuiding",
                     "SendToPushover", "WaitForTimeSpan"]
    items = list(_exec_items(slot))
    sw = items[kinds.index("SwitchFilter")]
    assert sw["Filter"]["_name"] == "L"
    sg = items[kinds.index("StartGuiding")]
    assert sg["ForceCalibration"] is True and sg["ErrorBehavior"] == 0 and sg["Attempts"] == 1
    assert items[-1]["Time"] == 300
    assert items[kinds.index("SetTracking")]["TrackingMode"] == 0
    assert "MeridianFlip" not in json.dumps(slot)
    ra = items[kinds.index("SlewScopeToRaDec")]["Coordinates"]
    assert (ra["RAHours"], ra["DecDegrees"]) == (18, 6)
    # start area: twilight AF -> slot -> imaging gate
    start = [it for it in _exec_items(seq)]
    order = [_short(it) for it in start]
    i = order.index(pc.CONTAINER_NAME)
    assert "RunAutofocus" in order[:i]
    after = order[i + 1 + len(kinds):]
    assert after[0] == "WaitForTime"                     # the gate
    assert order.index(pc.CONTAINER_NAME) < order.index("Targets")
    r = lint(seq, guided=True)
    assert r.ok, [(f.rule, f.detail) for f in r.findings]


def test_late_arm_slot_follows_the_unpark():
    seq = _gen(FIELD, dusk=False)
    order = [_short(it) for it in _exec_items(seq)]
    i = order.index(pc.CONTAINER_NAME)
    assert "UnparkScope" in order[:i] and "RunAutofocus" not in order[:i]
    assert i < order.index("Targets")
    assert lint(seq, guided=True).ok


def test_slot_replaces_forced_first_target_calibration(monkeypatch):
    monkeypatch.setenv("PS_GUIDING_FORCE_FIRST_CALIBRATION", "true")
    plain = _gen()
    starts = [it for it in _exec_items(plain) if "StartGuiding" in it["$type"]]
    assert starts[0]["ForceCalibration"] is True          # PS-72 as before
    seq = _gen(FIELD)
    slot_ids = {id(x) for x in _exec_items(_slot(seq))}
    outside = [it for it in _exec_items(seq) if "StartGuiding" in it["$type"]
               and id(it) not in slot_ids]
    assert len(outside) == 2 and not any(it["ForceCalibration"] for it in outside)
    r = lint(seq, guided=True)
    assert r.ok, [(f.rule, f.detail) for f in r.findings]


def test_lint_calibration_rules(monkeypatch):
    seq = _gen(FIELD)
    # ForceCalibration outside the slot
    outside = next(it for it in _exec_items(seq) if "StartGuiding" in it["$type"]
                   and it is not next(x for x in _exec_items(_slot(seq))
                                      if "StartGuiding" in x["$type"]))
    outside["ForceCalibration"] = True
    r = lint(seq, guided=True)
    assert any(f.rule == "phd2-calibration" and "outside" in f.detail
               for f in r.findings) and not r.ok
    # the slot must stop guiding again
    seq = _gen(FIELD)
    slot = _slot(seq)
    slot["Items"]["$values"] = [it for it in slot["Items"]["$values"]
                                if "StopGuiding" not in it["$type"]]
    r = lint(seq, guided=True)
    assert any(f.rule == "phd2-calibration" and "stop guiding" in f.detail
               for f in r.findings)


def test_selftest_rule_skips_the_slot(tmp_path, monkeypatch):
    script = tmp_path / "phd2-selftest.cmd"
    script.write_text("@echo off\n", encoding="ascii")
    monkeypatch.setenv("PS_PHD2_SELFTEST_ENABLED", "true")
    monkeypatch.setenv("PS_PHD2_SELFTEST_SCRIPT", str(script))
    seq = _gen(FIELD)
    order = [_short(it) for it in _exec_items(seq)]
    tw = next(i for i, it in enumerate(_exec_items(seq))
              if "ExternalScript" in it["$type"] and it["Script"].endswith("twilight"))
    assert tw < order.index(pc.CONTAINER_NAME)        # self-test before calibrating
    r = lint(seq, guided=True)
    assert r.ok, [(f.rule, f.detail) for f in r.findings]


def test_standalone_calibration_sequence():
    seq = json.loads(nsj.generate_phd2_calibration_json(FIELD, 200))
    order = [_short(it) for it in _exec_items(seq)]
    assert pc.CONTAINER_NAME in order and "UnparkScope" in order
    assert order.index("UnparkScope") < order.index(pc.CONTAINER_NAME)


# --- after a meridian flip ------------------------------------------------------

def _frames(errors, dirs, ms=300):
    return [{"t": 2.0 * i, "ra": 0.1, "dec": e, "ra_ms": 0, "ra_dir": "",
             "dec_ms": ms if d else 0, "dec_dir": d, "drop": False,
             "settling": False, "epoch": 0, "output": True}
            for i, (e, d) in enumerate(zip(errors, dirs))]


def test_dec_runaway_synthetic_reversed_and_normal():
    n = 90
    run = _frames([1.0 + 0.3 * i for i in range(n)], ["S"] * n)
    r = pc.dec_runaway(run, 20.0, 0.25)
    assert r["runaway"] is True
    import random
    rnd = random.Random(4)
    errs = [rnd.gauss(0, 0.6) for _ in range(n)]
    ok = _frames(errs, ["S" if e > 0 else "N" for e in errs], ms=60)
    assert pc.dec_runaway(ok, 20.0, 0.25)["runaway"] is False
    assert pc.dec_runaway(ok[:5], 20.0, 0.25)["runaway"] is None


def test_dec_runaway_on_the_0926_west_sessions():
    secs = pl.parse_guide_log((FIX / N26).read_text(encoding="utf-8"), N26)

    def first4(start):
        s = next(x for x in secs if x["kind"] == "guiding" and x["start"].endswith(start))
        scale, _ = pl._scale_for(s["header"], _cfg())
        fr = s["frames"]
        return ([f for f in fr if f["t"] - fr[0]["t"] <= 240],
                gm.pulse_rates(s["header"], scale)[1], scale)
    fr, yr, sc = first4("21:30:08")          # West pier on the East calibration
    r = pc.dec_runaway(fr, yr, sc)
    assert r["runaway"] is True and "grew" in r["reason"]
    fr, yr, sc = first4("20:18:08")          # the working Dec 0 session
    assert pc.dec_runaway(fr, yr, sc)["runaway"] is False


# --- store and seed -----------------------------------------------------------------

def test_seed_from_logs_takes_the_newest_completed_calibration(tmp_path, monkeypatch):
    logs = tmp_path / "logs"
    logs.mkdir()
    for n in (N25, N26):
        shutil.copy(FIX / n, logs / n)
    import os
    import time
    os.utime(logs / N25, (time.time() - 86400, time.time() - 86400))
    cfg = _cfg(tmp_path, phd2_logs_dir=str(logs))
    rec = pc.load_active(cfg)                    # empty store -> seed
    assert rec["source"] == "log" and rec["context"] == "seed"
    assert rec["ortho_err_deg"] == 15.1 and rec["grade"] == pc.FAIL
    assert rec["profile"] and rec["binning"] == 2
    assert pc.load_active(cfg, seed=False)["t_utc"] == rec["t_utc"]
    assert len(pc.history(cfg)) == 1
    assert pc.needs_calibration(rec, {}, cfg)["needed"]


def test_flip_marks_and_summary(tmp_path):
    cfg = _cfg(tmp_path)
    assert pc.set_flip(cfg, "West", True, "x") is None        # nothing on record
    pc.save_record(cfg, {"t_utc": store.iso_z(datetime.utcnow()), "grade": pc.WARN,
                         "night": "2026-10-01", "recommended_step_ms": 50})
    pc.set_flip(cfg, "West", False, "Dec response reversed")
    s = pc.summary(cfg, "2026-10-01")
    assert s["flip"]["West"]["ok"] is False and s["recommended_step_ms"] == 50
    assert s["stale"] is False and s["poor"] is False and len(s["tonight"]) == 1


# --- armer --------------------------------------------------------------------

def _armer(tmp_path, **kw):
    from photonscript.scheduler.armer import Armer
    a = Armer(_cfg(tmp_path, **kw))
    a.plan = {"night_of": "2026-10-01", "dusk_utc": "2026-10-02T01:55:00Z",
              "dawn_utc": "2026-10-02T11:30:00Z"}
    return a


def test_armer_plans_a_slot_only_when_needed(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "seed_from_logs", lambda config, max_files=5: None)
    a = _armer(tmp_path)
    now = datetime(2026, 10, 2, 1, 0)
    pc.set_request(a.config, "next")
    f = a._calibration_slot([SimpleNamespace(ra_hours=19.0)], now)
    assert f and -5 <= f["dec_degrees"] <= 15
    plan = pc.load_plan(a.config)
    assert plan["status"] == "pending" and plan["hold_s"] == 240
    assert plan["night"] == "2026-10-01" and "manual request" in plan["reason"]
    assert pc.pending_request(a.config) is None               # consumed
    pc.save_record(a.config, {"t_utc": "2026-10-01T02:00:00Z", "grade": pc.PASS})
    assert a._calibration_slot([SimpleNamespace(ra_hours=19.0)], now) is None
    assert pc.load_plan(a.config) is None
    a._cal_force = "guide binning changed (2 -> 1)"
    assert a._calibration_slot([], now) is not None
    a._cal_force = None
    never = _armer(tmp_path / "n", phd2_cal_mode="never")
    assert never._calibration_slot([], now) is None


async def test_armer_recalibrate_once_with_an_hour_left(tmp_path, monkeypatch):
    import photonscript.scheduler.armer as armer_mod
    notes = []

    async def _notify(cfg, msg, **kw):
        notes.append(msg)
    monkeypatch.setattr(armer_mod, "notify", _notify)
    a = _armer(tmp_path)
    a.state = "RUNNING"
    a.plan["dawn_utc"] = (datetime.utcnow() + timedelta(hours=5)).isoformat() + "Z"
    calls, forced = [], []

    async def _nina(key, *x, **k):
        calls.append(key)
        return {"ok": True}

    async def _dispatch(companion=True, fail_state="ERROR"):
        forced.append((a._cal_force, companion, fail_state))
        return True
    monkeypatch.setattr(a, "_nina", _nina)
    monkeypatch.setattr(a, "_dispatch_and_start", _dispatch)
    assert await a.recalibrate("guide binning changed") is True
    assert calls == ["sequence_stop", "guider_stop"]
    assert forced == [("guide binning changed", False, None)] and a._cal_force is None
    assert a.state == "RUNNING" and len(notes) == 1
    assert await a.recalibrate("again") is False             # once per night
    b = _armer(tmp_path / "b")
    b.state = "RUNNING"
    b.plan["dawn_utc"] = (datetime.utcnow() + timedelta(minutes=40)).isoformat() + "Z"
    monkeypatch.setattr(b, "_dispatch_and_start", _dispatch)
    assert await b.recalibrate("late") is False               # under 1 h of dark


# --- API + config ----------------------------------------------------------------

async def test_api_calibration_and_request(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import phd2 as r
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    pc.save_record(cfg, {"t_utc": store.iso_z(datetime.utcnow()), "grade": pc.PASS,
                         "night": "2026-10-01"})
    out = r.api_phd2_calibration(date="2026-10-01")
    assert out["grade"] == pc.PASS and out["mode"] == "auto"
    assert len(out["history"]) == 1 and out["phd2_ops"]["owner"] is None

    class _Req:
        async def json(self):
            return {"mode": "next"}
    monkeypatch.setattr(r, "_armer_state", lambda: "DISARMED")
    res = await r.api_phd2_calibrate(_Req())
    assert res["ok"] and pc.pending_request(cfg)["mode"] == "next"
    bad = await r.api_phd2_calibrate(_Req(), mode="sometime")
    assert bad.status_code == 400
    seq = r.api_phd2_calibration_sequence()
    assert pc.CONTAINER_NAME in json.dumps(seq["sequence"])
    s = r.calibration_summary(cfg, "2026-10-01")
    assert s["record"]["grade"] == pc.PASS and s["tonight"]
    by_env = {f[1]: f for f in app._CONFIG_FIELDS}
    for env in ("PS_PHD2_CAL_MODE", "PS_PHD2_CAL_MAX_AGE_DAYS", "PS_PHD2_CAL_HOLD_S",
                "PS_PHD2_CAL_FAIL_ACTION", "PS_PHD2_FLIP_ACTION"):
        assert hasattr(cfg, by_env[env][0])
    d = PhotonScriptConfig(_env_file=None)
    assert (d.phd2_cal_mode, d.phd2_cal_fail_action, d.phd2_flip_action,
            d.phd2_cal_hold_s, d.phd2_cal_max_age_days) == ("auto", "keep", "alert",
                                                            240, 30.0)


def test_sources_have_no_em_dashes():
    root = Path(__file__).resolve().parents[2] / "photonscript"
    for rel in ("scheduler/phd2_calibration.py", "telescope_agent/phd2_calmanager.py"):
        text = (root / rel).read_bytes()
        assert all(b < 128 for b in text), rel
    assert math.isclose(pc.ORTHO_MAX_DEG, 5.0)
