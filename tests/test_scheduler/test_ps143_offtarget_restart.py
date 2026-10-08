"""PS-143 (PS-64 follow-ups): the live panel's off-target alert and
"Restart tonight from now".

  1. separation math (haversine, RA wrap, cos(Dec)), J2000 -> JNow precession
     and the mount-epoch choice;
  2. the planned center: target, mosaic panel, PS-26 shifted RC16 center for
     a Piggy-driven target; the imaging gate (never slews, centering, AF,
     flats, darks);
  3. assess(): mount vs fresh plate solve, not imaging when paused;
  4. the alert debounce (more than N subs or M minutes, once per target per
     night, events, panel-only mode);
  5. the armer's restart state machine with a mocked NINA (from RUNNING,
     from a pause, refusals, failures, a restart mid-wait);
  6. API, where-panel payload, config fields and static checks.
"""
import asyncio
import functools
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from photonscript.scheduler import armer as armer_mod
from photonscript.scheduler import night_pause as np_
from photonscript.scheduler import off_target as ot
from photonscript.scheduler import where_panel as wp
from photonscript.scheduler.armer import PAUSE_STATE, WATCH_STATE, Armer
from photonscript.shared.night_events import events_path
from photonscript.shared.phd2_store import night_of, read_jsonl
from tests.test_scheduler.test_ps64_pause_panel import (  # noqa: F401 (fixture)
    IDLE, _armer, _cfg, _exposing, _tree, env)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 7, 4, 0, 0)


# ------------------------------------------------------------- 1. geometry

def test_separation_same_point_and_missing_input():
    assert ot.separation(10.0, 20.0, 10.0, 20.0)["arcmin"] == 0
    assert ot.separation(None, 20.0, 10.0, 20.0) is None


def test_separation_cos_dec_scales_ra():
    eq = ot.separation(100.0, 0.0, 101.0, 0.0)
    assert eq["arcmin"] == pytest.approx(60.0, abs=0.01)
    assert eq["east"] == pytest.approx(60.0, abs=0.01) and eq["dir"] == "E"
    hi = ot.separation(100.0, 60.0, 101.0, 60.0)
    assert hi["arcmin"] == pytest.approx(30.0, abs=0.05)   # cos 60 = 0.5
    assert hi["east"] == pytest.approx(30.0, abs=0.05)


def test_separation_wraps_ra_at_zero():
    s = ot.separation(359.9, 0.0, 0.1, 0.0)
    assert s["arcmin"] == pytest.approx(12.0, abs=0.01)
    assert s["east"] == pytest.approx(12.0, abs=0.01)        # not -21588'
    back = ot.separation(0.1, 0.0, 359.9, 0.0)
    assert back["east"] == pytest.approx(-12.0, abs=0.01) and back["dir"] == "W"
    hi = ot.separation(359.9, 60.0, 0.1, 60.0)
    assert hi["arcmin"] == pytest.approx(6.0, abs=0.01)


def test_separation_north_component_and_pole():
    s = ot.separation(50.0, 30.0, 50.0, 30.25)
    assert s["north"] == pytest.approx(15.0) and s["east"] == pytest.approx(0.0)
    assert s["arcmin"] == pytest.approx(15.0, abs=0.01) and s["dir"] == "N"
    # across the pole: 0.1 deg from it on opposite sides = 0.2 deg apart
    assert ot.separation(0.0, 89.9, 180.0, 89.9)["arcmin"] == pytest.approx(12.0, abs=0.01)


def test_precession_j2000_to_2026():
    ra, dec = ot.precess_from_j2000(0.0, 0.0, datetime(2000, 1, 1, 12))
    assert ra == pytest.approx(0.0, abs=1e-6) and dec == pytest.approx(0.0, abs=1e-6)
    ra, dec = ot.precess_from_j2000(0.0, 0.0, NOW)
    years = (NOW - datetime(2000, 1, 1, 12)).days / 365.25
    # m = 3.075 s/yr in RA and n = 20.04"/yr in Dec at RA 0, Dec 0
    assert ra * 60 == pytest.approx(46.1 * years / 60, abs=0.3)
    assert dec * 60 == pytest.approx(20.04 * years / 60, abs=0.3)
    assert ot.separation(0.0, 0.0, ra, dec)["arcmin"] > 20   # why it matters at 10'


def test_mount_epoch_choice(tmp_path):
    cfg = _cfg(tmp_path)
    assert ot.mount_epoch(cfg, "J2000") == "J2000"
    assert ot.mount_epoch(cfg, "JNow") == "JNow"
    assert ot.mount_epoch(cfg, None) == "JNow"          # TheSky driver default
    assert ot.mount_epoch(_cfg(tmp_path, off_target_mount_epoch="j2000"), "JNow") == "J2000"
    assert ot.mount_epoch(_cfg(tmp_path, off_target_mount_epoch="jnow"), "J2000") == "JNow"


# --------------------------------------------- 2. planned center + gate

def _proj(name, ra_h, dec, mosaic=None, rig="rc16", cid=""):
    return SimpleNamespace(target=SimpleNamespace(name=name, ra_hours=ra_h,
                                                  dec_degrees=dec, catalog_id=cid),
                           mosaic=mosaic, driving_rig=rig,
                           frame_center_ra_hours=None, frame_center_dec_degrees=None)


PROJECTS = [_proj("Heart Nebula", 2.55, 61.45, cid="IC 1805"),
            _proj("M31 P2", 0.70, 41.30, mosaic={"id": "m1", "name": "M31", "panel": 2, "of": 4}),
            _proj("M 31", 0.712, 41.27, rig="piggyback")]


def test_expected_center_target_and_mosaic_panel(tmp_path):
    cfg = _cfg(tmp_path)
    e = ot.expected_center(cfg, PROJECTS, "Heart Nebula imaging (repeats while safe and up)")
    assert e["source"] == "target" and e["ra_deg"] == pytest.approx(2.55 * 15)
    assert ot.expected_center(cfg, PROJECTS, "ic1805")["name"] == "Heart Nebula"
    m = ot.expected_center(cfg, PROJECTS, "M31 P2")
    assert m["source"] == "mosaic panel" and m["dec_deg"] == pytest.approx(41.30)
    assert ot.expected_center(cfg, PROJECTS, "Unknown Thing") is None
    assert ot.expected_center(cfg, PROJECTS, None) is None


def test_expected_center_piggy_shift_by_pier(tmp_path, monkeypatch):
    from photonscript.scheduler import piggy_offset as po
    plan = {"applied": True, "by_pier": {
        "East": {"ra_hours": 0.700, "dec_degrees": 41.0},
        "West": {"ra_hours": 0.724, "dec_degrees": 41.5}}}
    monkeypatch.setattr(po, "center_plan", lambda *a, **k: plan)
    monkeypatch.setattr(po, "load", lambda cfg: {})
    on = _cfg(tmp_path, piggy_center_mode="on")
    w = ot.expected_center(on, PROJECTS, "M 31", pier="West")
    assert w["source"] == "piggy shift (PS-26)" and w["dec_deg"] == 41.5
    assert ot.expected_center(on, PROJECTS, "M 31", pier="East")["dec_deg"] == 41.0
    # pier unknown: the side nearer to the mount
    near = ot.expected_center(on, PROJECTS, "M 31", mount_radec=(0.701 * 15, 41.02))
    assert near["dec_deg"] == 41.0
    # preview mode: the RC16 centers on the target, no shift expected
    prev = ot.expected_center(_cfg(tmp_path), PROJECTS, "M 31", pier="West")
    assert prev["source"] == "target" and prev["dec_deg"] == 41.27
    plan["applied"] = False
    assert ot.expected_center(on, PROJECTS, "M 31", pier="West")["source"] == "target"


@pytest.mark.parametrize("path,leaf,slewing,ok", [
    (["Heart Nebula", "Smart Exposure"], "TakeExposure", False, True),
    (["Heart Nebula", "Smart Exposure"], "TakeExposure", True, False),
    (["Heart Nebula"], "Slew and center", False, False),
    (["Heart Nebula"], "Center", False, False),
    (["Heart Nebula", "AF"], "Run Autofocus", False, False),
    (["DUSK_FLATS", "Sky flats"], "TakeExposure", False, False),
    (["DARKS_IF_UNSAFE", "Smart Exposure"], "TakeExposure", False, False),
    (["BIAS_IF_STILL_UNSAFE"], "TakeExposure", False, False),
    (["PHD2_CALIBRATION"], "TakeExposure", False, False),
    (["Heart Nebula"], "Dither", False, False),
    ([], None, False, False),
])
def test_imaging_gate(path, leaf, slewing, ok):
    assert ot.is_imaging(path, leaf, slewing, "Heart Nebula")[0] is ok


def test_imaging_gate_skips_the_targets_own_name():
    path = ["Dark Shark Nebula", "Dark Shark Nebula imaging (repeats while safe and up)",
            "Smart Exposure"]
    assert ot.is_imaging(path, "TakeExposure", False, "Dark Shark Nebula")[0] is True
    assert ot.is_imaging(path, "TakeExposure", False, None)[0] is False


# ----------------------------------------------------------- 3. assess

def _rc16(target="Heart Nebula", n=3, leaf="TakeExposure", filt="Ha"):
    return {"target": target, "running": leaf, "filter": filt,
            "path": [target, "Smart Exposure"], "sub": {"n": n, "of": 40}}


def _mount_at(ra_deg, dec, epoch="J2000", slewing=False):
    return {"ra_hours": ra_deg / 15.0, "dec_deg": dec, "epoch": epoch,
            "pier": "West", "slewing": slewing}


def test_assess_mount_jnow_is_precessed(tmp_path):
    cfg = _cfg(tmp_path)
    ra, dec = ot.precess_from_j2000(2.55 * 15, 61.45, NOW)
    a = ot.assess(cfg, _rc16(), _mount_at(ra, dec, "JNow"), "RUNNING", PROJECTS, NOW,
                  night="2026-10-06")
    assert a["imaging"] and a["source"] == "mount" and a["sep_arcmin"] < 0.1
    assert a["mount"]["epoch"] == "JNow" and a["sub_key"] == "Heart Nebula|Ha|40|3"
    # the same JNow numbers read as J2000 would be 20' plus "off"
    j = ot.assess(_cfg(tmp_path, off_target_mount_epoch="j2000"), _rc16(),
                  _mount_at(ra, dec, "JNow"), "RUNNING", PROJECTS, NOW)
    assert j["sep_arcmin"] > 10


def _write_solve(cfg, night, start, ra, dec, solved=True):
    from photonscript.scheduler.solve_store import store_path
    p = store_path(cfg, night, "rc16")
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"file": "x.fits", "rig": "rc16", "solved": solved,
                             "start_utc": start, "ra": ra, "dec": dec}) + "\n")


def test_assess_fresh_solve_wins_and_stale_is_ignored(tmp_path):
    cfg = _cfg(tmp_path)
    night = "2026-10-06"
    far = _mount_at(2.55 * 15 + 1.0, 61.45)          # mount says 29' off
    _write_solve(cfg, night, "2026-10-07T03:30:00Z", 2.55 * 15, 61.45 + 0.05)
    a = ot.assess(cfg, _rc16(), far, "RUNNING", PROJECTS, NOW, night=night)
    assert a["solve"] is None and a["source"] == "mount" and a["sep_arcmin"] > 20
    _write_solve(cfg, night, "2026-10-07T03:52:00Z", 2.55 * 15, 61.45 + 0.05)
    _write_solve(cfg, night, "2026-10-07T03:55:00Z", 0, 0, solved=False)
    a = ot.assess(cfg, _rc16(), far, "RUNNING", PROJECTS, NOW, night=night)
    assert a["source"] == "plate solve" and a["sep_arcmin"] == pytest.approx(3.0, abs=0.01)
    assert a["solve"]["age_min"] == 8.0 and a["mount"]["arcmin"] > 20


def test_assess_not_imaging_when_paused_or_off(tmp_path):
    cfg = _cfg(tmp_path)
    m = _mount_at(2.55 * 15, 61.45)
    a = ot.assess(cfg, _rc16(), m, PAUSE_STATE, PROJECTS, NOW)
    assert a["imaging"] is False and "armer" in a["reason"]
    a = ot.assess(_cfg(tmp_path, off_target_mode="off"), _rc16(), m, "RUNNING", PROJECTS, NOW)
    assert a["imaging"] is False and a["sep_arcmin"] is None
    a = ot.assess(cfg, _rc16(target="Mystery"), m, "RUNNING", PROJECTS, NOW)
    assert a["expected"] is None and "not a goal" in a["reason"]


# ----------------------------------------------------------- 4. debounce

def _a(sep, n=1, target="Heart Nebula", imaging=True, night="2026-10-06"):
    return {"mode": "alert", "target": target, "sep_arcmin": sep, "imaging": imaging,
            "sub_key": f"{target}|Ha|40|{n}" if n is not None else None,
            "night": night, "source": "mount", "mount": {"arcmin": sep, "dir": "NE"},
            "expected": {"source": "target"}}


def test_monitor_alerts_after_more_than_two_subs(tmp_path):
    cfg, mon = _cfg(tmp_path), ot.OffTargetMonitor()
    t = NOW
    assert mon.update(_a(4.0), cfg, t)["status"] == "ok"
    r1 = mon.update(_a(12.0, n=1), cfg, t)
    r1b = mon.update(_a(12.5, n=1), cfg, t + timedelta(seconds=30))
    r2 = mon.update(_a(12.0, n=2), cfg, t + timedelta(seconds=60))
    assert [r["status"] for r in (r1, r1b, r2)] == ["watch"] * 3
    assert not any(r["alert"] for r in (r1, r1b, r2))
    r3 = mon.update(_a(12.0, n=3), cfg, t + timedelta(seconds=90))
    assert r3["status"] == "off" and r3["alert"] is True
    assert r3["streak"] == {"target": "Heart Nebula", "subs": 3, "minutes": 1.5,
                            "max_arcmin": 12.5}
    r4 = mon.update(_a(13.0, n=4), cfg, t + timedelta(seconds=120))
    assert r4["status"] == "off" and r4["alert"] is False      # once per target
    assert mon.view("Heart Nebula", t)["alerted"] is True


def test_monitor_alerts_after_five_minutes_on_one_long_sub(tmp_path):
    cfg, mon = _cfg(tmp_path), ot.OffTargetMonitor()
    for k in range(10):     # every 30 s, the same 600 s sub
        r = mon.update(_a(15.0, n=1), cfg, NOW + timedelta(seconds=30 * k))
        assert r["alert"] is False and r["status"] == "watch"
    r = mon.update(_a(15.0, n=1), cfg, NOW + timedelta(minutes=5))
    assert r["alert"] is True and r["status"] == "off"


def test_monitor_streak_resets_on_af_slew_or_recovery(tmp_path):
    cfg, mon = _cfg(tmp_path), ot.OffTargetMonitor()
    mon.update(_a(12.0, n=1), cfg, NOW)
    mon.update(_a(12.0, n=2), cfg, NOW + timedelta(minutes=2))
    r = mon.update(_a(12.0, n=None, imaging=False), cfg, NOW + timedelta(minutes=3))
    assert r["status"] == "idle" and mon.streak is None        # AF / slew: reset
    r = mon.update(_a(12.0, n=3), cfg, NOW + timedelta(minutes=4))
    assert r["status"] == "watch" and r["streak"]["subs"] == 1
    r = mon.update(_a(9.9, n=4), cfg, NOW + timedelta(minutes=5))
    assert r["status"] == "ok" and mon.streak is None
    r = mon.update(_a(None, n=5), cfg, NOW + timedelta(minutes=6))
    assert r["status"] == "unknown" and not r["alert"]


def test_monitor_per_target_and_per_night(tmp_path):
    cfg, mon = _cfg(tmp_path), ot.OffTargetMonitor()
    for n in (1, 2, 3):
        r = mon.update(_a(20.0, n=n), cfg, NOW + timedelta(seconds=n))
    assert r["alert"]
    # a new target starts its own streak and may alert once too
    for n in (1, 2):
        r = mon.update(_a(20.0, n=n, target="M31 P2"), cfg, NOW + timedelta(seconds=10 + n))
        assert r["alert"] is False and r["streak"]["target"] == "M31 P2"
    r = mon.update(_a(20.0, n=3, target="M31 P2"), cfg, NOW + timedelta(seconds=20))
    assert r["alert"] is True
    # the next night the latch is clear again
    for n in (1, 2, 3):
        r = mon.update(_a(20.0, n=n, night="2026-10-07"), cfg, NOW + timedelta(days=1, seconds=n))
    assert r["alert"] is True


def test_monitor_uses_config_limits(tmp_path):
    cfg = _cfg(tmp_path, off_target_arcmin=20.0, off_target_subs=0, off_target_minutes=60)
    mon = ot.OffTargetMonitor()
    assert mon.update(_a(15.0), cfg, NOW)["status"] == "ok"
    assert mon.update(_a(25.0), cfg, NOW)["alert"] is True     # > 0 subs


def _ot_events(cfg):
    p = events_path(cfg, night_of(cfg, NOW))
    return [r for r in read_jsonl(p) if r.get("kind") == "off_target"]


async def test_handle_pushes_once_and_logs_alert_and_clear(tmp_path):
    cfg, mon = _cfg(tmp_path), ot.OffTargetMonitor()
    sent = []

    async def _notify(c, msg, **kw):
        sent.append((msg, kw.get("priority")))
    for n in (1, 2, 3, 4):
        await ot.handle(cfg, _a(14.0, n=n), mon, NOW + timedelta(seconds=n), notify=_notify)
    await ot.handle(cfg, _a(3.0, n=5), mon, NOW + timedelta(seconds=9), notify=_notify)
    assert len(sent) == 1 and sent[0][1] == 1
    assert "OFF TARGET: Heart Nebula" in sent[0][0] and "14.0'" in sent[0][0]
    assert sent[0][0].isascii()
    assert [e["value"] for e in _ot_events(cfg)] == ["alert", "clear"]


async def test_handle_panel_mode_never_pushes(tmp_path):
    cfg, mon = _cfg(tmp_path, off_target_mode="panel"), ot.OffTargetMonitor()
    sent = []

    async def _notify(c, msg, **kw):
        sent.append(msg)
    for n in (1, 2, 3):
        r = await ot.handle(cfg, _a(14.0, n=n), mon, NOW, notify=_notify)
    assert r["status"] == "off" and sent == []
    assert [e["value"] for e in _ot_events(cfg)] == ["alert"]


# ----------------------------------------------- 5. restart state machine

NEVER_ON_RESTART = {"mount_park", "camera_warm", "guider_stop", "camera_cool_off"}


def _restart_events(a):
    p = events_path(a.config, night_of(a.config, datetime.utcnow()))
    return [r["value"] for r in read_jsonl(p) if r.get("kind") == "restart"]


async def test_restart_from_running_waits_for_the_sub_then_redispatches(env):
    a = _armer(env, cams=(_exposing(200), _exposing(200), IDLE))
    res = await a.restart()
    assert res["ok"] and res["pending"] and a.state == PAUSE_STATE
    assert a.status()["restart"]["pending"] is True
    await a._pause_task
    assert a.state == "RUNNING" and a.pause_info is None
    assert a.calls.count("sequence_stop") == 1
    assert a.dispatched == [(False, None)]          # the resume path, no companion
    assert a.piggy_dispatched == []
    assert not set(a.calls) & NEVER_ON_RESTART
    assert _restart_events(a) == ["request", "stopped", "dispatched"]
    assert len(env.notes) == 1 and "RESTARTED tonight from now" in env.notes[0][0]
    assert "nothing parked or warmed" in env.notes[0][0]
    assert a.last_restart["ok"] is True and a.status()["restart"]["ok"] is True


async def test_restart_now_skips_the_wait(env):
    a = _armer(env, cams=(_exposing(500),))
    await a.restart(when="now")
    await a._pause_task
    assert "camera_info" not in a.calls and a.dispatched == [(False, None)]


async def test_restart_from_a_pause_redispatches_at_once(env):
    a = _armer(env)
    await a.pause()
    await a._pause_task
    stops = a.calls.count("sequence_stop")
    env.notes.clear()
    res = await a.restart()
    assert res["ok"] and res["redispatched"] is True and a.state == "RUNNING"
    assert a.calls.count("sequence_stop") == stops    # nothing stopped again
    assert a.dispatched == [(False, None)]
    assert _restart_events(a) == ["request", "dispatched"]
    assert "RESTARTED" in env.notes[-1][0]


async def test_restart_from_a_pause_returns_a_paused_piggy(env, monkeypatch):
    async def cam(base):
        return IDLE

    async def stop(base):
        return True
    monkeypatch.setattr(np_, "camera_info", cam)
    monkeypatch.setattr(np_, "sequence_stop", stop)
    a = _armer(env, piggyback_enabled=True)
    await a.pause(piggy="pause")
    await a._pause_task
    res = await a.restart()
    assert res["ok"] and a.piggy_dispatched == [1]


async def test_restart_while_the_pause_still_waits(env, monkeypatch):
    gate = asyncio.Event()

    async def slow(read_camera, stop, **kw):
        await gate.wait()
        return {"ok": await stop(), "how": "after_exposure", "waited_s": 1}
    monkeypatch.setattr(np_, "stop_after_exposure", slow)
    a = _armer(env)
    await a.pause()
    await asyncio.sleep(0)
    res = await a.restart()
    assert res["ok"] and res["pending"] and a.dispatched == []
    gate.set()
    await a._pause_task
    assert a.state == "RUNNING" and a.dispatched == [(False, None)]
    assert _restart_events(a) == ["request", "stopped", "dispatched"]


async def test_restart_refused_while_watching(env):
    a = _armer(env, state=WATCH_STATE)
    res = await a.restart()
    assert res["ok"] is False and "sideload preview" in res["detail"]
    assert a.state == WATCH_STATE and a.calls == [] and a.dispatched == []
    assert _restart_events(a) == ["refused"]


async def test_restart_refused_while_a_calibration_capture_runs(env, monkeypatch):
    from photonscript.scheduler import calibration_capture as cc
    monkeypatch.setattr(cc, "busy", lambda rig: rig == "rc16")
    a = _armer(env)
    res = await a.restart()
    assert res["ok"] is False and "calibration capture" in res["detail"]
    assert a.state == "RUNNING" and a.calls == []


@pytest.mark.parametrize("state", ["DISARMED", "ARMED", "PAUSED_UNSAFE", "COMPLETE", "ERROR"])
async def test_restart_refused_in_other_states(env, state):
    a = _armer(env, state=state)
    res = await a.restart()
    assert res["ok"] is False and state in res["detail"]
    assert a.state == state and a.calls == [] and a.dispatched == []


async def test_restart_refused_with_too_little_dark(env):
    a = _armer(env)
    a.plan = {**a.plan, "dawn_utc": (datetime.utcnow() + timedelta(minutes=20))
              .replace(microsecond=0).isoformat() + "Z"}
    res = await a.restart()
    assert res["ok"] is False and "min of dark left" in res["detail"]
    assert a.state == "RUNNING" and a.calls == []


async def test_restart_dispatch_failure_stays_paused(env):
    a = _armer(env)
    a.fake["dispatch_ok"] = False
    await a.restart()
    await a._pause_task
    assert a.state == PAUSE_STATE and a.pause_info["phase"] == "paused"
    assert "restart" not in a.pause_info
    assert "Restart FAILED" in a.detail and a.last_restart["ok"] is False
    assert _restart_events(a) == ["request", "stopped", "failed"]
    assert not set(a.calls) & NEVER_ON_RESTART
    # Resume (or another restart) can still pick it up
    a.fake["dispatch_ok"] = True
    assert (await a.resume())["ok"] and a.state == "RUNNING"


async def test_restart_stop_failure_goes_back_to_running(env):
    a = _armer(env)
    a.fake["stop_ok"] = False
    await a.restart()
    await a._pause_task
    assert a.state == "RUNNING" and a.dispatched == []
    assert env.notes[-1][1] == 1 and "Restart FAILED" in env.notes[-1][0]
    assert _restart_events(a) == ["request", "failed"]


async def test_restart_survives_a_photonscript_restart_mid_wait(env):
    a = _armer(env, state=PAUSE_STATE)
    a.pause_info = {"since": "2026-10-07T04:00:00Z", "phase": "stopping",
                    "when": "after_exposure", "piggy": "keep", "rigs": {},
                    "parked": False, "restart": {"requested": "x", "when": "after_exposure"}}
    a._persist()
    b = Armer(a.config)
    assert b.restore() is True
    try:
        b._nina, b.calls, b.fake = a._nina, a.calls, a.fake
        b.dispatched = []

        async def _dispatch(companion=True, fail_state="ERROR"):
            b.dispatched.append((companion, fail_state))
            return True
        b._dispatch_and_start = _dispatch
        await b._pause_task
        assert b.state == "RUNNING" and b.dispatched == [(False, None)]
    finally:
        b._task.cancel()


# ------------------------------------------- 6. API, panel, config, static

@pytest.fixture
def api(env, monkeypatch):
    import photonscript.scheduler.app as app
    from photonscript.scheduler import sideload as sd
    a = _armer(env)
    monkeypatch.setattr(app, "_config", a.config)
    monkeypatch.setattr(app, "get_armer", lambda: a)

    async def _read(base, client=None):
        return [], None
    monkeypatch.setattr(sd, "read_sequence_state", _read)

    async def _get(base, path):
        return None
    monkeypatch.setattr(wp, "_get", _get)
    return SimpleNamespace(app=app, armer=a)


async def _call(app, method, path, body=None):
    transport = httpx.ASGITransport(app=app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.request(method, path, json=body)


async def test_api_restart_and_where_flags(api):
    r = await _call(api.app, "GET", "/api/night/where")
    d = r.json()
    assert d["armer"]["can_restart"] is True and "off_target" in d
    api.armer.state = WATCH_STATE
    r = await _call(api.app, "POST", "/api/arm/restart", {})
    assert r.status_code == 409 and "sideload preview" in r.json()["detail"]
    api.armer.state = "RUNNING"
    r = await _call(api.app, "POST", "/api/arm/restart", {"when": "after_exposure"})
    assert r.status_code == 200 and r.json()["state"] == PAUSE_STATE
    await api.armer._pause_task
    assert api.armer.state == "RUNNING" and api.armer.dispatched == [(False, None)]


async def test_collect_reports_off_target_with_the_mount_epoch(env, monkeypatch):
    from photonscript.scheduler import sideload as sd
    cfg = _cfg(env.tmp)
    tree = _tree(done=2)

    async def _read(base, client=None):
        return tree, None
    monkeypatch.setattr(sd, "read_sequence_state", _read)
    ra, dec = ot.precess_from_j2000(2.55 * 15, 61.45 + 0.25, datetime.utcnow())

    async def _get(base, path):
        return {"/equipment/camera/info": _exposing(100),
                "/equipment/mount/info": {"RightAscension": ra / 15.0, "Declination": dec,
                                          "EquatorialSystem": 1, "Slewing": False},
                }.get(path)
    a = _armer(env)
    a.config = cfg
    d = await wp.collect(cfg, a, tel={"mount_side_of_pier": "West"},
                         projects=PROJECTS, get=_get, piggy=False)
    o = d["off_target"]
    assert o["target"] == "Heart Nebula" and o["imaging"] is True
    assert o["mount"]["epoch"] == "JNow" and o["sep_arcmin"] == pytest.approx(15.0, abs=0.2)
    assert o["mount"]["dir"] == "N" and o["status"] in ("idle", "ok", "watch", "unknown")
    assert d["piggy"] is None
    json.dumps(d)


async def test_run_monitor_feeds_the_monitor_only_while_imaging(env, monkeypatch):
    seen = []

    async def _collect(cfg, armer, **kw):
        seen.append(kw.get("piggy"))
        return {"off_target": _a(30.0, n=len(seen))}
    monkeypatch.setattr(wp, "collect", _collect)
    handled = []

    async def _handle(cfg, a):
        handled.append(a["sub_key"])
    monkeypatch.setattr(ot, "handle", _handle)
    a = _armer(env, state="DISARMED")
    calls = {"n": 0}

    async def _sleep(s):
        calls["n"] += 1
        if calls["n"] == 1:
            a.state = "RUNNING"
        elif calls["n"] >= 3:
            raise asyncio.CancelledError
    monkeypatch.setattr(ot.asyncio, "sleep", _sleep)
    with pytest.raises(asyncio.CancelledError):
        await ot.run_monitor(lambda: a.config, lambda: a, lambda: {}, lambda: [])
    assert seen == [False, False] and len(handled) == 2


def test_config_fields_and_safe_defaults(tmp_path):
    from photonscript.scheduler.app import _CONFIG_FIELDS
    cfg = _cfg(tmp_path)
    assert (cfg.off_target_mode, cfg.off_target_arcmin, cfg.off_target_subs,
            cfg.off_target_minutes, cfg.off_target_mount_epoch) == ("alert", 10.0, 2, 5.0, "auto")
    keys = {f[0]: f for f in _CONFIG_FIELDS}
    for k in ("off_target_mode", "off_target_arcmin", "off_target_subs",
              "off_target_minutes", "off_target_solve_max_age_min",
              "off_target_mount_epoch"):
        assert keys[k][1] == "PS_" + k.upper()


def test_dashboard_wiring_and_ascii():
    from tests.test_dashboard_js_strings import _raw_newline_strings
    dash = (ROOT / "photonscript/scheduler/templates/dashboard.html").read_text(encoding="utf-8")
    js = (ROOT / "photonscript/scheduler/static/js/night_panel.js").read_text(encoding="utf-8")
    assert 'id="restartBtn"' in dash and ".wp-ot-off" in dash
    for s in ("/api/arm/restart", "doRestart", "can_restart", "offTargetHtml",
              "sideloadBox", "On target"):
        assert s in js, s
    assert js.count("confirm(") >= 2
    assert "setInterval" not in js and "setTimeout" not in js
    assert _raw_newline_strings(js) == []
    for p in ("photonscript/scheduler/off_target.py",
              "photonscript/scheduler/where_panel.py",
              "photonscript/scheduler/routers/pause.py",
              "photonscript/scheduler/static/js/night_panel.js",
              "tests/test_scheduler/test_ps143_offtarget_restart.py"):
        assert (ROOT / p).read_bytes().isascii(), p
    src = (ROOT / "photonscript/scheduler/armer.py").read_text(encoding="utf-8")
    block = src.split("# -- PS-143: Restart tonight from now", 1)[1].split(
        "# -- state machine loop", 1)[0]
    assert block.isascii()
