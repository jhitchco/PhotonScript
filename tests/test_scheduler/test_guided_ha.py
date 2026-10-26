"""Guided Ha (2026-10-26): the guided-night readiness gate, the PS-93
far-Dec / camera-change triggers, the first guided night's guiding test,
the guided narrowband ladder and its darks, the trial night.

Fixture numbers are the live 2026-10-25 PHD2 audit: GP678C, profile 2,
calibration FAIL (axes 78 deg from perpendicular, Dec 61.6, HA -5.82 h,
West), dark library 1 to 6 s, 27.7 d old, no dark at the 7 s exposure,
binning 3 (want 2), exposure 7000 ms (want 2 to 4 s).
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from photonscript.scheduler import guided_commission as gc
from photonscript.scheduler import guided_readiness as gr
from photonscript.scheduler import phd2_calibration as pc
from photonscript.scheduler import sub_ladder as sl
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget

NIGHT = "2026-10-26"
NOW = datetime(2026, 10, 27, 3, 0, 0)          # 21:00 local
LIB = [1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 6000]


def _cfg(tmp_path, **kw):
    kw.setdefault("observatory_tz", "America/Denver")
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _row(rid, status, current, desired, **kw):
    r = {"id": rid, "status": status, "current": current, "desired": desired,
         "label": kw.pop("label", rid), "fix": kw.pop("fix", f"fix {rid}"),
         "apply": kw.pop("apply", "manual"), "applicable": kw.pop("applicable", False),
         "note": kw.pop("note", None), "target": kw.pop("target", None)}
    r.update(kw)
    return r


def live_audit(t="2026-10-27T01:00:00Z", **over):
    rows = [
        _row("camera", "pass", "OGMA GP678C", "the OAG camera"),
        _row("calibration_record", "fail", "FAIL, 0.0 d old", "graded PASS or WARN",
             note="axes 77.97 deg from perpendicular (max 5)"),
        _row("dark_library", "fail", "10 darks 1 to 6 s, 27.7 d old",
             "covers 1 to 4 s (and the current exposure), under 30 days old",
             note="no dark at the current 7 s exposure"),
        _row("binning", "warn", "3", "2"),
        _row("exposure_ms", "warn", "7000", "2000 to 4000", apply="api",
             applicable=True, target=3000, label="Guide exposure (ms)"),
        _row("search_region_px", "warn", "20", "30 to 40", apply="profile"),
        _row("calibration_step_ms", "warn", "250", "about 180 ms", apply="profile"),
        _row("silenced_alerts", "warn", "PulseGuideFailedAlert off", "none silenced"),
        _row("darks_or_defects", "warn", "darks off, defect map off", "auto-loaded"),
        _row("min_snr", "pass", "10", "8 to 20"),
    ]
    a = {"t_utc": t, "profile_id": 2, "rows": rows,
         "camera": {"id": "gp678c", "name": "OGMA GP678C", "reported": "GP678C",
                    "detected": True},
         "source_times": {"file_age_days": 27.7}}
    a.update(over)
    return a


def fail_cal():
    rec = {"t_utc": "2026-10-25T22:30:46Z", "grade": "FAIL",
           "reasons": ["axes 78 deg from perpendicular (max 5)"],
           "dec_deg": 61.6, "ha_hr": -5.82, "pier_side": "West"}
    return {"mode": "auto", "record": rec, "grade": "FAIL", "age_days": 1.2,
            "stale": False, "poor": True, "plan": None}


def good_cal(dec=5.0, t="2026-10-26T03:00:00Z"):
    rec = {"t_utc": t, "grade": "PASS", "reasons": [], "dec_deg": dec, "ha_hr": 0.5,
           "pier_side": "West"}
    return {"mode": "auto", "record": rec, "grade": "PASS", "age_days": 1.0,
            "stale": False, "poor": False, "plan": None}


def _build(cfg, **kw):
    kw.setdefault("audit", live_audit())
    kw.setdefault("cal", fail_cal())
    kw.setdefault("selftests", [{"verdict": "PASS", "t_utc": "2026-10-20T04:00:00Z"}])
    kw.setdefault("lib_ms", LIB)
    kw.setdefault("commission", {})
    kw.setdefault("night", NIGHT)
    return gr.build(cfg, NOW, **kw)


# --------------------------------------------------------------------------
# 1. the readiness gate
# --------------------------------------------------------------------------

def test_live_audit_blocks_with_the_exposure_fast_path_first(tmp_path):
    r = _build(_cfg(tmp_path, guided_default=False))
    assert r["status"] == "blocked" and not r["ready"]
    blockers = [i for i in r["items"] if i["level"] == gr.BLOCKER]
    assert [b["id"] for b in blockers] == ["exposure_ms"]
    b = blockers[0]
    assert "3 s" in b["fix"] and b["apply_id"] == "exposure_ms"
    assert r["fast_path"]["target_ms"] == 3000
    # the dark library FAIL is folded into the fast path (rebuild later)
    assert not any(i["id"] == "dark_library" and i["level"] == gr.BLOCKER for i in r["items"])
    assert any(i["id"] == "dark_library_age" for i in r["items"])
    # the FAIL calibration is fixed by PS-93 on the guided night
    cal = next(i for i in r["items"] if i["id"] == "calibration")
    assert cal["level"] == gr.AUTO and "Dec 61.6" in cal["current"]
    # order: binning (frame geometry) before the exposure, before the calibration
    ids = [i["id"] for i in r["items"]]
    assert ids.index("binning") < ids.index("exposure_ms") < ids.index("calibration")
    # unguided by config: says how to get a guided night
    assert any(i["id"] == "mode" and "guided_trial_night" in i["fix"] for i in r["items"])
    assert "BLOCKED (1)" in r["line"]
    assert "\u2014" not in json.dumps(r)


def test_library_without_the_target_needs_a_rebuild(tmp_path):
    r = _build(_cfg(tmp_path), lib_ms=[1000, 1500, 6000])
    blk = [i for i in r["items"] if i["level"] == gr.BLOCKER]
    assert [i["id"] for i in blk] == ["dark_library"]
    assert "GUI only" in blk[0]["fix"] and r["fast_path"] is None


def test_only_auto_items_is_ready(tmp_path):
    a = live_audit(rows=[_row("camera", "pass", "OGMA GP678C", "-"),
                         _row("calibration_record", "fail", "FAIL", "PASS")])
    r = _build(_cfg(tmp_path), audit=a)
    assert r["status"] == "auto" and r["ready"]
    assert "READY (auto: PHD2 calibration" in r["line"]


def test_cal_mode_never_makes_the_calibration_a_blocker(tmp_path):
    a = live_audit(rows=[_row("camera", "pass", "x", "-")])
    r = _build(_cfg(tmp_path, phd2_cal_mode="never"), audit=a)
    assert r["status"] == "blocked"
    assert next(i for i in r["items"] if i["id"] == "calibration")["level"] == gr.BLOCKER


def test_far_dec_pass_calibration_is_auto_recalibrated(tmp_path):
    a = live_audit(rows=[_row("camera", "pass", "x", "-")])
    r = _build(_cfg(tmp_path), audit=a, cal=good_cal(dec=45.0))
    cal = next(i for i in r["items"] if i["id"] == "calibration")
    assert cal["level"] == gr.AUTO and "Dec 45" in cal["current"]
    r = _build(_cfg(tmp_path), audit=a, cal=good_cal(dec=5.0))
    assert not any(i["id"] == "calibration" for i in r["items"]) and r["status"] == "ready"


def test_camera_fail_selftest_fail_and_missing_audit_block(tmp_path):
    a = live_audit()
    a["rows"][0] = _row("camera", "fail", "AP26CC", "the OAG camera")
    r = _build(_cfg(tmp_path), audit=a,
               selftests=[{"verdict": "FAIL", "t_utc": "2026-10-25T04:00:00Z",
                           "reasons": ["no RA motion"]}])
    ids = [i["id"] for i in r["items"] if i["level"] == gr.BLOCKER]
    assert ids[0] == "camera" and "selftest" in ids
    r = _build(_cfg(tmp_path), audit=None)
    assert r["items"][0]["id"] == "audit" and r["status"] == "blocked"


def test_camera_swap_pending_and_darks_older_than_the_swap(tmp_path):
    cfg = _cfg(tmp_path, guide_camera_expected="asi174mm_mini")
    r = _build(cfg)
    assert r["camera"]["swap_pending"]
    assert any(i["id"] == "camera_swap" and "swap checklist" in i["fix"] for i in r["items"])
    # the swap happens: the new camera is seen, the old dark library predates it
    a = live_audit(t="2026-11-02T01:00:00Z")
    a["camera"] = {"id": "asi174mm_mini", "name": "ZWO ASI174MM Mini", "detected": True}
    a["rows"] = [_row("camera", "pass", "ZWO ASI174MM Mini", "-"),
                 _row("dark_library", "pass", "10 darks", "-")]
    a["source_times"] = {"file_age_days": 8.0}
    r = gr.build(cfg, datetime(2026, 11, 2, 2, 0), audit=a, cal=good_cal(), selftests=[{"verdict": "PASS"}],
                 lib_ms=LIB, commission={}, night="2026-11-01")
    assert r["camera"]["changed_utc"] and not r["camera"]["swap_pending"]
    dl = next(i for i in r["items"] if i["id"] == "dark_library")
    assert dl["level"] == gr.BLOCKER and "before the guide camera changed" in dl["current"]
    cal = next(i for i in r["items"] if i["id"] == "calibration")
    assert cal["level"] == gr.AUTO and "before the guide camera changed" in cal["current"]
    # PS-93 sees it too
    need = pc.needs_calibration(good_cal()["record"], {}, cfg, datetime(2026, 11, 2, 2, 0))
    assert need["needed"] and "guide camera changed" in need["reason"]


def test_headsup_line(tmp_path):
    cfg = _cfg(tmp_path)
    r = _build(cfg)
    line = gr.headsup_line(cfg, False, r=r)
    assert line.startswith("Guided night: BLOCKED (1)") and "First fix" in line
    ready = _build(cfg, audit=live_audit(rows=[_row("camera", "pass", "x", "-")]),
                   cal=good_cal())
    assert gr.headsup_line(cfg, True, r=ready) is None
    from photonscript.scheduler import night_story as ns
    plan = {"night_of": NIGHT, "preconfig_utc": "2026-10-27T00:00:00Z",
            "dusk_utc": "2026-10-27T01:30:00Z", "dawn_utc": "2026-10-27T12:00:00Z",
            "dark_hours": 10.5, "schedule": [], "targets": ["Heart Nebula"]}
    txt = ns.plan_headsup_text(cfg, plan, guided=False, guiding=line)
    assert "Guided night: BLOCKED" in txt


def test_nb_ladder_darks(tmp_path):
    cfg = _cfg(tmp_path)
    nb = gr.nb_ladder_darks(cfg, status=lambda s: {"match": "matched" if s != 450 else "none"})
    assert nb["missing"] == [450.0] and "rc16_guided_nb_ladder_darks" in nb["fix"]


# --------------------------------------------------------------------------
# 2. PS-93: far from Dec 0
# --------------------------------------------------------------------------

def test_needs_calibration_far_dec(tmp_path):
    cfg = _cfg(tmp_path)
    now = datetime(2026, 10, 27, 3, 0)
    need = pc.needs_calibration(good_cal(dec=45.0)["record"], {}, cfg, now)
    assert need["needed"] and "Dec 45" in need["reason"]
    assert not pc.needs_calibration(good_cal(dec=25.0)["record"], {}, cfg, now)["needed"]
    off = _cfg(tmp_path, phd2_cal_max_abs_dec=0)
    assert not pc.needs_calibration(good_cal(dec=45.0)["record"], {}, off, now)["needed"]


# --------------------------------------------------------------------------
# 3. the guiding test
# --------------------------------------------------------------------------

_N = [0]


def _sub(start: datetime, exp=300.0, rms=0.6, state="Guiding", ecc=0.45, filt="Ha"):
    _N[0] += 1
    return {"rig": "rc16", "file": f"rc16/Heart/LIGHT/s{_N[0]:04d}.fits",
            "target": "Heart Nebula", "filter": filt, "exp_s": float(exp),
            "start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_utc": (start + timedelta(seconds=exp)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "guide_state": state, "guide_rms": rms, "guide_rms_frames": 140,
            "ecc": ecc, "stars": 300, "passed_qa": True}


def _run(n, t0=datetime(2026, 10, 27, 2, 0), **kw):
    out, t = [], t0
    for _ in range(n):
        out.append(_sub(t, **kw))
        t += timedelta(seconds=kw.get("exp", 300.0) + 15)
    return out


def test_judge_wait_pass_fail(tmp_path):
    cfg = _cfg(tmp_path)
    now = datetime(2026, 10, 27, 2, 20)
    assert gc.judge(cfg, [], now)["verdict"] == "wait"
    assert gc.judge(cfg, _run(2), now)["verdict"] == "wait"            # 10 of 15 min
    p = gc.judge(cfg, _run(3), now)
    assert p["verdict"] == "pass" and p["rms_arcsec"] == 0.6 and p["n_guided"] == 3
    f = gc.judge(cfg, _run(3, rms=1.8), now)
    assert f["verdict"] == "fail" and "1.8" in f["why"]
    lost = _run(3) + _run(4, t0=datetime(2026, 10, 27, 2, 16), state="LostLock")
    f = gc.judge(cfg, lost, now)
    assert f["verdict"] == "fail" and "guided only 3 of 7" in f["why"]
    stopped = _run(5, state="Stopped", rms=None)
    assert gc.judge(cfg, stopped, datetime(2026, 10, 27, 2, 30))["verdict"] == "wait"
    t = gc.judge(cfg, stopped, datetime(2026, 10, 27, 2, 50))
    assert t["verdict"] == "fail" and "PHD2 is not guiding" in t["why"]


def _targets():
    plan = ExposurePlan(filter_type=FilterType.HA, exposure_seconds=600, count=20,
                        gain=200, offset=256)
    lum = ExposurePlan(filter_type=FilterType.LUMINANCE, exposure_seconds=180, count=30,
                       gain=200, offset=256)
    return [NinaSequenceTarget(name="Heart Nebula", ra_hours=2.55, dec_degrees=61.5,
                               start_guiding=True, exposures=[plan, lum])]


def test_cap_for_test(tmp_path):
    cfg = _cfg(tmp_path, guided_commission_mode="act")
    ts = _targets()
    notes = gc.cap_for_test(cfg, ts, NIGHT, True, "gp678c")
    ha = ts[0].exposures[0]
    assert notes and ha.exposure_seconds == 300 and ha.count == 40
    assert ts[0].exposures[1].exposure_seconds == 180
    assert gc.load_night(cfg, NIGHT)["capped"]
    # unguided, advise, or commissioned: no cap
    assert gc.cap_for_test(cfg, _targets(), NIGHT, False, "gp678c") == []
    assert gc.cap_for_test(_cfg(tmp_path / "a", guided_commission_mode="advise"),
                           _targets(), NIGHT, True, "gp678c") == []
    c2 = _cfg(tmp_path / "b", guided_commission_mode="act")
    gc._record(c2, "gp678c", "2026-10-20", {"verdict": "pass", "rms_arcsec": 0.5}, NOW)
    assert gc.cap_for_test(c2, _targets(), NIGHT, True, "gp678c") == []
    assert gc.cap_for_test(c2, _targets(), NIGHT, True, "asi174mm_mini")   # new camera


def test_default_advise_caps_nothing_but_a_trial_night_tests_in_act(tmp_path):
    cfg = _cfg(tmp_path)
    assert gc.mode(cfg, NIGHT) == "advise"
    assert gc.cap_for_test(cfg, _targets(), NIGHT, True, "gp678c") == []
    assert gc.allows_long_nb(cfg, NIGHT, "gp678c")          # advise gates nothing
    trial = _cfg(tmp_path / "t", guided_trial_night=NIGHT)
    assert gc.mode(trial, NIGHT) == "act" and gc.mode(trial, "2026-10-27") == "advise"
    assert gc.cap_for_test(trial, _targets(), NIGHT, True, "gp678c")
    assert not gc.allows_long_nb(trial, NIGHT, "gp678c")   # until the test passes
    off = _cfg(tmp_path / "o", guided_trial_night=NIGHT, guided_commission_mode="off")
    assert gc.mode(off, NIGHT) == "off"


class FakeArmer:
    def __init__(self, guided=True, state="RUNNING"):
        self.state = state
        self.plan = {"night_of": NIGHT}
        self.guided = guided
        self.calls = []
        self.recipe = None

    def _use_guiding(self, trial=True):
        return self.guided

    def _dawn(self):
        return datetime(2026, 10, 27, 12, 0)

    def _recipe_tonight(self):
        return None

    async def restart(self, when="after_exposure", now=None):
        self.calls.append(("restart", when))
        return {"ok": True, "detail": "restarting"}

    async def fallback_unguided(self, reason):
        self.calls.append(("fallback", reason))
        return True


def _tick(cfg, armer, subs, now=datetime(2026, 10, 27, 2, 20)):
    pushes = []

    async def notify(c, msg, priority=0, kind="story"):
        pushes.append((msg, priority, kind))
    res = asyncio.run(gc.tick(cfg, armer, now, subs=subs, notify=notify))
    return res, pushes


def test_tick_pass_restarts_at_planned_lengths(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, guided_commission_mode="act")
    monkeypatch.setattr(gc, "current_camera", lambda c: "gp678c")
    gc.cap_for_test(cfg, _targets(), NIGHT, True)
    a = FakeArmer()
    res, pushes = _tick(cfg, a, _run(3))
    assert res["step"] == "pass" and a.calls == [("restart", "after_exposure")]
    assert "PASSED" in pushes[0][0] and "planned lengths" in pushes[0][0]
    assert gc.commissioned(cfg, "gp678c")["rms_arcsec"] == 0.6
    assert gc.allows_long_nb(cfg, NIGHT, "gp678c")
    assert _tick(cfg, a, _run(3))[0]["step"] == "decided"
    # the re-dispatch after the pass is not capped
    assert gc.cap_for_test(cfg, _targets(), NIGHT, True) == []


def test_tick_fail_falls_back_unguided(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, guided_commission_mode="act")
    monkeypatch.setattr(gc, "current_camera", lambda c: "gp678c")
    a = FakeArmer()
    res, pushes = _tick(cfg, a, _run(3, rms=2.4))
    assert res["step"] == "fail" and a.calls[0][0] == "fallback"
    assert "FAILED" in pushes[0][0] and pushes[0][1] == 1
    assert not gc.allows_long_nb(cfg, NIGHT, "gp678c")
    st = gc.status(cfg, "2026-10-27", "gp678c")
    assert st["needed"] and "FAILED" in st["text"]


def test_tick_advise_off_unguided(tmp_path, monkeypatch):
    monkeypatch.setattr(gc, "current_camera", lambda c: "gp678c")
    cfg = _cfg(tmp_path, guided_commission_mode="advise")
    a = FakeArmer()
    res, pushes = _tick(cfg, a, _run(3, rms=2.4))
    assert res["step"] == "fail" and a.calls == [] and "advise" in pushes[0][0]
    cfg2 = _cfg(tmp_path / "x", guided_commission_mode="act")
    assert _tick(cfg2, FakeArmer(guided=False), _run(3))[0]["step"] is None
    assert _tick(cfg2, FakeArmer(state="ARMED"), _run(3))[0]["step"] is None


# --------------------------------------------------------------------------
# 4. the guided narrowband ladder and its darks
# --------------------------------------------------------------------------

def _ladder_cfg(tmp_path):
    return _cfg(tmp_path, night_pilot_ladder_up_subs=3, night_pilot_ladder_nb_min_stars=50)


def test_guided_nb_ladder_skips_a_rung_without_darks(tmp_path):
    cfg = _ladder_cfg(tmp_path)
    subs = _run(3, exp=300.0)
    darks = lambda s: {"match": "matched" if s in (300, 600, 900) else "none", "text": ""}
    d = sl.evaluate(cfg, "rc16", subs, dark_status=darks, guided=True)
    assert d["kind"] == "up" and d["to_s"] == 600 and d["skipped"] == [450.0]
    assert "skipping 450 s" in d["why"]
    # with 450 s darks it takes the next rung
    d = sl.evaluate(cfg, "rc16", subs, dark_status=lambda s: {"match": "matched"}, guided=True)
    assert d["to_s"] == 450
    # not guided: the RC16's own rungs (300 is the top)
    d = sl.evaluate(cfg, "rc16", subs, dark_status=darks, guided=False)
    assert d["kind"] == "none"
    # broadband on a guided night: the RC16's own rungs too
    d = sl.evaluate(cfg, "rc16", _run(3, exp=300.0, filt="L"), dark_status=darks, guided=True)
    assert d["kind"] == "none"
    # 600 -> 900
    d = sl.evaluate(cfg, "rc16", _run(3, exp=600.0), dark_status=darks, guided=True)
    assert d["kind"] == "up" and d["to_s"] == 900


def test_guided_nb_rungs_join_the_dark_quota_only_when_on(tmp_path):
    from photonscript.scheduler.nina_sequence_json import dark_library_exposures
    off = dark_library_exposures(_cfg(tmp_path))
    assert 450.0 not in off and {300.0, 600.0, 900.0} <= set(off)
    on = dark_library_exposures(_cfg(tmp_path, rc16_guided_nb_ladder_darks=True))
    assert 450.0 in on and on.index(450.0) < on.index(750.0)


# --------------------------------------------------------------------------
# 5. the trial night, the fix-ups, config
# --------------------------------------------------------------------------

def test_trial_night(tmp_path, monkeypatch):
    from photonscript.scheduler import arm_memory
    from photonscript.scheduler.armer import Armer
    cfg = _cfg(tmp_path, guided_default=False, guided_trial_night=NIGHT)
    monkeypatch.setattr("photonscript.shared.phd2_store.night_of", lambda c, w=None: NIGHT)
    g, why = arm_memory.auto_guiding(cfg, "auto-evening")
    assert g == "guided" and "trial" in why
    fake = SimpleNamespace(config=cfg, guiding_override=None, plan={"night_of": NIGHT})
    assert Armer._use_guiding(fake) is True
    assert Armer._use_guiding(fake, trial=False) is False
    fake.guiding_override = "unguided"
    assert Armer._use_guiding(fake) is False
    fake.guiding_override, fake.plan = None, {"night_of": "2026-10-27"}
    assert Armer._use_guiding(fake) is False


def test_prep_tick(tmp_path):
    cfg = _cfg(tmp_path, guided_prep_mode="act")
    r = _build(cfg)
    calls = []

    async def apply_fn():
        calls.append("apply")
        return {"ok": True, "results": [{"id": "exposure_ms", "ok": True}]}

    async def audit_fn():
        calls.append("audit")
    running = SimpleNamespace(state="RUNNING", plan={})
    out = asyncio.run(gr.prep_tick(cfg, running, NOW, readiness=r, apply_fn=apply_fn,
                                   audit_fn=audit_fn))
    assert out["did"] is None and "never during a night" in out["held"] and not calls
    idle = SimpleNamespace(state="DISARMED", plan={})
    out = asyncio.run(gr.prep_tick(cfg, idle, NOW, readiness=r, apply_fn=apply_fn,
                                   audit_fn=audit_fn))
    assert out["did"] == "applied" and calls == ["apply", "audit"]
    out = asyncio.run(gr.prep_tick(cfg, idle, NOW, readiness=r, apply_fn=apply_fn))
    assert out["did"] is None                                  # once per night
    adv = _cfg(tmp_path / "adv")
    out = asyncio.run(gr.prep_tick(adv, idle, NOW, readiness=_build(adv), apply_fn=apply_fn))
    assert out["did"] == "advised" and calls == ["apply", "audit"]


def test_config_fields():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    c = PhotonScriptConfig(_env_file=None)
    for env in ("PS_GUIDED_PREP_MODE", "PS_GUIDED_COMMISSION_MODE",
                "PS_GUIDED_COMMISSION_TEST_MIN", "PS_GUIDED_COMMISSION_TEST_EXPOSURE_S",
                "PS_GUIDED_COMMISSION_MIN_SUBS", "PS_GUIDED_COMMISSION_RMS_MAX_ARCSEC",
                "PS_GUIDED_COMMISSION_MIN_GUIDED_FRAC", "PS_GUIDED_COMMISSION_TIMEOUT_MIN",
                "PS_GUIDED_TRIAL_NIGHT", "PS_GUIDED_AUTO_ARM_WHEN_READY",
                "PS_GUIDE_CAMERA_EXPECTED", "PS_PHD2_CAL_MAX_ABS_DEC",
                "PS_NIGHT_PILOT_LADDER_RC16_GUIDED_NB", "PS_RC16_GUIDED_NB_LADDER_DARKS"):
        assert env in by_env, env
        assert hasattr(c, by_env[env][0])
    assert (c.guided_prep_mode, c.guided_commission_mode) == ("advise", "advise")
    assert c.night_pilot_ladder_rc16_guided_nb == "300,450,600,900"
    assert c.rc16_guided_nb_ladder_darks is False and c.phd2_cal_max_abs_dec == 30.0
    assert c.guided_trial_night == "" and c.guided_auto_arm_when_ready is False


# --------------------------------------------------------------------------
# 6. the routes and the pages
# --------------------------------------------------------------------------

def test_routes_and_pages(tmp_path, monkeypatch):
    import re
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    from photonscript.shared import phd2_store as store
    cfg = _cfg(tmp_path / "data")
    monkeypatch.setattr(app, "_config", cfg)
    store.write_json(Path(cfg.data_dir) / "phd2_audit" / "latest.json", live_audit())
    c = TestClient(app.app)
    r = c.get("/api/guiding/readiness?darks=0")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] in ("blocked", "auto", "ready") and d["items"]
    assert d["prep"]["mode"] == "advise"
    r = c.get("/api/guiding/commission")
    assert r.status_code == 200 and r.json()["status"]["mode"] == "advise"
    html = c.get("/guiding").text
    for i in ("guidedReady", "grLine", "grItems", "grCommission", "grRefresh", "grStatus"):
        assert f'id="{i}"' in html, i
    assert "GUIDEDREADY.init()" in html
    js = (Path(__file__).resolve().parents[2] / "photonscript" / "scheduler" / "static"
          / "js" / "guided_readiness_panel.js").read_text(encoding="utf-8")
    assert set(re.findall(r"\$\('(\w+)'\)", js)) <= set(re.findall(r'id="(\w+)"', html))
    assert "\u2014" not in js
    dash = (Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
            / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="guidedReadyLine"' in dash and "GUIDEDREADY.line('guidedReadyLine')" in dash
