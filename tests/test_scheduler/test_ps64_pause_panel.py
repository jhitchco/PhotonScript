"""PS-64: operator Pause / Resume of the night and the live "where is it" panel.

2026-09-26: a PHD2 Calibration Assistant slew left the mount at Dec 0 while
the sequence kept shooting "Cat's Eye" subs, and the only ways to stop were
Remote Desktop or Stop & Make Safe (warm + park). These tests pin:
  1. night_pause: the stop waits for the current sub (mocked NINA camera),
     stops at once when nothing exposes, never waits past its bound;
  2. the armer's PAUSED_OPERATOR state machine: pause from RUNNING only,
     never parks / warms / stops the guider, resume = the mid-night
     re-dispatch, Piggy keep / pause, dawn and unsafe while paused, a
     watched night pauses alert-only, the busy-state gates see it;
  3. the /api/night/where panel data (tree parsing, mosaic panel, API);
  4. static checks on the dashboard JS.
"""
import asyncio
import functools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from photonscript.scheduler import armer as armer_mod
from photonscript.scheduler import night_pause as np_
from photonscript.scheduler import where_panel as wp
from photonscript.scheduler.armer import PAUSE_STATE, WATCH_STATE, Armer
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.night_events import events_path
from photonscript.shared.phd2_store import night_of, read_jsonl

ROOT = Path(__file__).resolve().parents[2]
# what a pause must never send to NINA #1 (resume re-dispatch is faked)
NEVER_ON_PAUSE = {"mount_park", "camera_warm", "guider_stop", "guider_start",
                  "sequence_load", "sequence_start"}


# ------------------------------------------------------------ 1. night_pause

class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def _cams(*payloads):
    """An async camera reader returning the payloads in turn (last repeats)."""
    seq = list(payloads)
    calls = []

    async def read():
        calls.append(1)
        return seq.pop(0) if len(seq) > 1 else seq[0]
    read.calls = calls
    return read


def _stopper(ok=True):
    calls = []

    async def stop():
        calls.append(1)
        return ok
    stop.calls = calls
    return stop


UTC0 = datetime(2026, 10, 7, 4, 0, 0, tzinfo=timezone.utc)


def _exposing(left_s, end_offset=0.0):
    end = UTC0 + timedelta(seconds=left_s + end_offset)
    return {"IsExposing": True, "CameraState": "Exposing",
            "ExposureEndTime": end.isoformat()}


IDLE = {"IsExposing": False, "CameraState": "Idle"}


def test_camera_busy_reads_exposing_download_and_idle():
    assert np_.camera_busy({"IsExposing": True}) is True
    assert np_.camera_busy({"IsExposing": False, "CameraState": "Download"}) is True
    assert np_.camera_busy(IDLE) is False
    assert np_.camera_busy({}) is None and np_.camera_busy(None) is None


def test_parse_nina_time_dotnet_offsets_and_empty():
    t = np_.parse_nina_time("2026-10-06T21:03:44.1234567-06:00")
    assert t == datetime(2026, 10, 7, 3, 3, 44, 123456, tzinfo=timezone.utc)
    assert np_.parse_nina_time("2026-10-07T03:03:44Z").tzinfo is not None
    assert np_.parse_nina_time("0001-01-01T00:00:00") is None
    assert np_.parse_nina_time("garbage") is None and np_.parse_nina_time(None) is None
    naive = np_.parse_nina_time("2026-10-06T21:03:44")   # local time of the scope PC
    assert naive is not None and naive.tzinfo is not None


async def test_stop_waits_for_the_sub_to_finish():
    clk = _Clock()
    read = _cams(_exposing(120), _exposing(120), _exposing(120),
                 {"IsExposing": False, "CameraState": "Download"}, IDLE)
    stop = _stopper()
    res = await np_.stop_after_exposure(read, stop, sleep=clk.sleep, clock=clk,
                                        utcnow=lambda: UTC0)
    assert res["ok"] and res["how"] == "after_exposure"
    assert stop.calls == [1]
    # 4 polls of 2 s + the save settle; downloading counted as busy
    assert res["waited_s"] == pytest.approx(4 * np_.POLL_S + np_.SAVE_SETTLE_S)


async def test_stop_at_once_when_nothing_exposes():
    clk = _Clock()
    read, stop = _cams(IDLE), _stopper()
    res = await np_.stop_after_exposure(read, stop, sleep=clk.sleep, clock=clk)
    assert res["how"] == "idle" and res["waited_s"] == 0 and stop.calls == [1]


async def test_stop_now_reads_no_camera():
    read, stop = _cams(_exposing(500)), _stopper()
    res = await np_.stop_after_exposure(read, stop, when="now")
    assert res["how"] == "now" and read.calls == [] and stop.calls == [1]


async def test_stop_at_once_when_camera_unreadable():
    clk = _Clock()
    res = await np_.stop_after_exposure(_cams(None), _stopper(), sleep=clk.sleep, clock=clk)
    assert res["how"] == "unreadable" and res["ok"]


async def test_stop_when_a_new_sub_started():
    """The idle gap was missed: the next sub's end time jumped later."""
    clk = _Clock()
    read = _cams(_exposing(10), _exposing(10), _exposing(10, end_offset=600))
    res = await np_.stop_after_exposure(read, _stopper(), sleep=clk.sleep, clock=clk,
                                        utcnow=lambda: UTC0)
    assert res["how"] == "new_exposure"


async def test_stop_wait_is_bounded_by_exposure_end_plus_grace():
    clk = _Clock()
    res = await np_.stop_after_exposure(_cams(_exposing(30)), _stopper(),
                                        sleep=clk.sleep, clock=clk, utcnow=lambda: UTC0)
    assert res["how"] == "timeout"
    assert res["waited_s"] <= 30 + np_.EXPOSURE_END_GRACE_S + np_.POLL_S
    clk2 = _Clock()
    res = await np_.stop_after_exposure(_cams({"IsExposing": True}), _stopper(),
                                        sleep=clk2.sleep, clock=clk2)
    assert res["how"] == "timeout" and res["waited_s"] <= np_.MAX_WAIT_S + np_.POLL_S


async def test_cancelled_wait_sends_no_stop():
    read, stop = _cams(_exposing(300)), _stopper()
    task = asyncio.ensure_future(np_.stop_after_exposure(read, stop, poll_s=0.01,
                                                         utcnow=lambda: UTC0))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stop.calls == []


def test_describe():
    assert "after the current sub" in np_.describe({"ok": True, "how": "after_exposure",
                                                    "waited_s": 200})
    assert "3.3 min" in np_.describe({"ok": True, "how": "after_exposure", "waited_s": 200})
    assert np_.describe({"ok": False}) == "stop FAILED"


# ---------------------------------------------------------- 2. armer pause

def _cfg(tmp_path, **kw):
    kw.setdefault("dawn_flats_window_min", 0)
    kw.setdefault("connect_all_on_arm", False)
    kw.setdefault("image_watch_dir", str(tmp_path / "nina"))
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _plan(dawn_in_h=5.0):
    now = datetime.utcnow()
    z = lambda d: d.replace(microsecond=0).isoformat() + "Z"  # noqa: E731
    return {"night_of": night_of_today(), "preconfig_utc": z(now - timedelta(hours=3)),
            "dusk_utc": z(now - timedelta(hours=2)),
            "dawn_utc": z(now + timedelta(hours=dawn_in_h)),
            "naut_dawn_utc": None, "sunrise_utc": None, "dark_hours": 8.0,
            "targets": ["Cat's Eye"]}


def night_of_today():
    return datetime.utcnow().strftime("%Y-%m-%d")


@pytest.fixture
def env(tmp_path, monkeypatch):
    notes = []

    async def _notify(c, msg, **kw):
        notes.append((msg, kw.get("priority", 0)))
    monkeypatch.setattr(armer_mod, "notify", _notify)
    fast = _Clock()
    orig = np_.stop_after_exposure
    monkeypatch.setattr(np_, "stop_after_exposure",
                        functools.partial(orig, sleep=fast.sleep, clock=fast,
                                          utcnow=lambda: UTC0))
    return SimpleNamespace(tmp=tmp_path, notes=notes)


def _armer(env, cams=(IDLE,), safe=True, state="RUNNING", **cfg):
    a = Armer(_cfg(env.tmp, **cfg))
    a.state = state
    a.plan = _plan()
    a.calls = []
    a.fake = {"cams": list(cams), "safe": safe, "stop_ok": True}

    async def _nina(key, method="GET", json_body=None, **kw):
        a.calls.append(key)
        if key == "camera_info":
            c = a.fake["cams"]
            return {"Response": c.pop(0) if len(c) > 1 else c[0]}
        if key == "safety":
            s = a.fake["safe"]
            return None if s is None else {"Response": {"Connected": True, "IsSafe": s}}
        if key == "sequence_stop":
            return {"Success": True} if a.fake["stop_ok"] else None
        if key == "guider":
            return {"Response": {"Connected": True, "State": "Stopped"}}
        return {"Success": True}
    a._nina = _nina
    a.dispatched = []

    async def _dispatch(companion=True, fail_state="ERROR"):
        a.dispatched.append((companion, fail_state))
        return a.fake.get("dispatch_ok", True)
    a._dispatch_and_start = _dispatch
    a.piggy_dispatched = []

    async def _piggy():
        a.piggy_dispatched.append(1)
    a._dispatch_piggyback_companion = _piggy
    return a


def _events(a):
    p = events_path(a.config, night_of(a.config, datetime.utcnow()))
    return [r for r in read_jsonl(p) if r.get("kind") == "operator_pause"]


async def _paused(env, **kw):
    a = _armer(env, **kw)
    res = await a.pause()
    assert res["ok"], res
    await a._pause_task
    return a


async def test_pause_waits_for_the_sub_then_stops_nina1_only(env):
    a = await _paused(env, cams=(_exposing(200), _exposing(200), IDLE))
    assert a.state == PAUSE_STATE
    assert a.pause_info["phase"] == "paused" and a.pause_info["piggy"] == "keep"
    assert a.pause_info["rigs"]["rc16"]["how"] == "after_exposure"
    assert a.calls.count("sequence_stop") == 1
    assert not set(a.calls) & NEVER_ON_PAUSE
    assert "piggyback" not in a.pause_info["rigs"]
    assert [e["value"] for e in _events(a)] == ["request", "stopped"]
    assert len(env.notes) == 1 and "PAUSED by the operator" in env.notes[0][0]
    assert "Piggy-600 keeps imaging" in env.notes[0][0]
    # persisted for a restart
    saved = json.loads((env.tmp / "armer_state.json").read_text(encoding="utf-8"))
    assert saved["state"] == PAUSE_STATE and saved["pause"]["phase"] == "paused"
    st = a.status()
    assert st["state"] == PAUSE_STATE and st["pause"]["phase"] == "paused"


@pytest.mark.parametrize("state", ["DISARMED", "ARMED", "PAUSED_UNSAFE", "COMPLETE",
                                   "ERROR", PAUSE_STATE])
async def test_pause_refused_unless_running(env, state):
    a = _armer(env, state=state)
    res = await a.pause()
    assert res["ok"] is False and state in res["detail"]
    assert a.state == state and a.calls == []


async def test_pause_piggy_choice_stops_nina2_too(env, monkeypatch):
    piggy = []

    async def cam(base):
        piggy.append(("cam", base))
        return IDLE

    async def stop(base):
        piggy.append(("stop", base))
        return True
    monkeypatch.setattr(np_, "camera_info", cam)
    monkeypatch.setattr(np_, "sequence_stop", stop)
    a = _armer(env, piggyback_enabled=True)
    res = await a.pause(piggy="pause")
    await a._pause_task
    assert res["ok"] and a.pause_info["piggy"] == "pause"
    assert [k for k, _ in piggy] == ["cam", "stop"]
    assert a.pause_info["rigs"]["piggyback"]["ok"] is True


async def test_pause_piggy_choice_ignored_without_piggyback(env):
    a = await _paused(env)
    assert a.pause_info["piggy"] == "keep"


async def test_failed_stop_goes_back_to_running_with_priority_push(env):
    a = _armer(env)
    a.fake["stop_ok"] = False
    await a.pause()
    await a._pause_task
    assert a.state == "RUNNING" and a.pause_info is None
    assert env.notes[-1][1] == 1 and "FAILED" in env.notes[-1][0]
    assert [e["value"] for e in _events(a)] == ["request", "failed"]


async def test_resume_redispatches_the_remainder(env):
    a = await _paused(env)
    res = await a.resume()
    assert res["ok"] and res["redispatched"] is True
    assert a.dispatched == [(False, None)]      # the PS-77 / PS-93 path
    assert a.piggy_dispatched == []              # Piggy kept: untouched
    assert a.state == "RUNNING" and a.pause_info is None
    assert [e["value"] for e in _events(a)][-1] == "resume"
    assert "RESUMED" in env.notes[-1][0]


async def test_resume_redispatches_a_paused_piggy_companion(env, monkeypatch):
    async def cam(base):
        return IDLE

    async def stop(base):
        return True
    monkeypatch.setattr(np_, "camera_info", cam)
    monkeypatch.setattr(np_, "sequence_stop", stop)
    a = _armer(env, piggyback_enabled=True)
    await a.pause(piggy="pause")
    await a._pause_task
    res = await a.resume()
    assert res["ok"] and a.piggy_dispatched == [1]


async def test_resume_before_the_stop_cancels_the_wait(env, monkeypatch):
    gate = asyncio.Event()

    async def slow(read_camera, stop, **kw):
        await gate.wait()        # the sub never ends in this test
        return {"ok": await stop(), "how": "after_exposure", "waited_s": 1}
    monkeypatch.setattr(np_, "stop_after_exposure", slow)
    a = _armer(env)
    await a.pause()
    await asyncio.sleep(0)
    assert a.pause_info["phase"] == "stopping"
    res = await a.resume()
    assert res["ok"] and res["redispatched"] is False
    assert a.state == "RUNNING" and a.dispatched == []
    gate.set()
    await asyncio.sleep(0)
    assert "sequence_stop" not in a.calls


async def test_resume_refused_with_too_little_dark(env):
    a = await _paused(env)
    a.plan = _plan(dawn_in_h=0.3)
    res = await a.resume()
    assert res["ok"] is False and "dark left" in res["detail"]
    assert a.state == PAUSE_STATE and a.dispatched == []


async def test_resume_dispatch_failure_stays_paused(env):
    a = await _paused(env)
    a.fake["dispatch_ok"] = False
    res = await a.resume()
    assert res["ok"] is False and a.state == PAUSE_STATE
    assert [e["value"] for e in _events(a)][-1] == "resume_failed"


async def test_resume_refused_when_not_paused(env):
    a = _armer(env)
    res = await a.resume()
    assert res["ok"] is False and a.state == "RUNNING"


async def test_paused_tick_skips_guiding_watchdog_and_keeps_cooler(env, monkeypatch):
    a = await _paused(env)
    cooler = []

    async def _cool(now):
        cooler.append(now)
    a._reconcile_cooler = _cool
    a.calls.clear()
    await a._tick()
    assert "guider" not in a.calls          # no not-guiding alerts while paused
    assert "safety" in a.calls and len(cooler) == 1
    assert not set(a.calls) & NEVER_ON_PAUSE
    assert a.state == PAUSE_STATE


async def test_unsafe_while_paused_parks_once_after_grace(env):
    a = await _paused(env, safe=False, unsafe_stop_grace_s=120)
    t0 = datetime.utcnow()
    await a._paused_tick(t0)
    assert "mount_park" not in a.calls
    await a._paused_tick(t0 + timedelta(seconds=130))
    await a._paused_tick(t0 + timedelta(seconds=160))
    assert a.calls.count("mount_park") == 1 and a.pause_info["parked"] is True
    assert "camera_warm" not in a.calls
    assert env.notes[-1][1] == 1


async def test_dawn_while_paused_runs_the_dawn_shutdown(env):
    a = await _paused(env)
    ran = []

    async def _shutdown(reason="dawn"):
        ran.append(reason)
        return "ok"
    a.dawn_shutdown = _shutdown
    await a._paused_tick(datetime.utcnow() + timedelta(hours=6))
    assert ran and a.state == "COMPLETE" and a.pause_info is None


async def test_disarm_while_paused_makes_safe(env):
    a = await _paused(env)
    safe = []

    async def _make_safe():
        safe.append(1)
        return "make-safe ok"
    a.make_safe = _make_safe
    await a.disarm()
    assert safe == [1] and a.state == "DISARMED" and a.pause_info is None


async def test_restore_reattaches_a_paused_night(env):
    a = await _paused(env)
    a._persist()
    b = Armer(a.config)
    assert b.restore() is True
    try:
        assert b.state == PAUSE_STATE and b.pause_info["phase"] == "paused"
        assert b._pause_task is None          # already stopped: nothing to finish
    finally:
        b._task.cancel()


# ------------------------------------------------- watched night (alert-only)

def _watching(env, running=("M 31_Container",)):
    a = _armer(env, state=WATCH_STATE)
    a.watch = {"since": "2026-10-07T03:00:00Z", "trigger": "button",
               "sequence": "x", "guided_targets": ["M 31"], "paused": False,
               "pauses": 0, "idle_ticks": 0, "running": []}

    async def _read():
        return [{"Name": "Targets_Container", "Status": "RUNNING",
                 "Items": [{"Name": n, "Status": "RUNNING"} for n in running]}], None
    a._watch_read_state = _read
    return a


async def test_watched_night_pause_is_alert_only(env):
    a = _watching(env)
    res = await a.pause(piggy="pause", when="now")
    assert res["ok"] and a.state == WATCH_STATE
    assert a.watch["operator_paused"]
    assert a.calls == []                      # nothing sent to NINA
    guider_reads = []
    a._maybe_warn_not_guiding = lambda now: guider_reads.append(now) or asyncio.sleep(0)
    await a._watch_tick(datetime.utcnow())
    assert guider_reads == []                 # guiding watchdog muted
    res = await a.resume()
    assert res["ok"] and "operator_paused" not in a.watch
    await a._watch_tick(datetime.utcnow())
    assert len(guider_reads) == 1
    assert a.dispatched == [] and not set(a.calls) & {"sequence_stop", "sequence_load",
                                                      "sequence_start"}
    assert [e["value"] for e in _events(a)] == ["pause", "resume"]


# ---------------------------------------------------------- busy-state gates

def test_paused_operator_is_a_night_in_progress():
    from photonscript.shared import updater
    from photonscript.shared.autostart_check import ARMER_ACTIVE
    from photonscript.telescope_agent.agent import TelescopeAgent
    assert PAUSE_STATE == "PAUSED_OPERATOR"
    assert PAUSE_STATE in armer_mod.ACTIVE_STATES and PAUSE_STATE in armer_mod.LIVE_STATES
    assert PAUSE_STATE in updater._NIGHT_RUNNING
    assert PAUSE_STATE in ARMER_ACTIVE
    assert PAUSE_STATE in TelescopeAgent._ARMER_ACTIVE_STATES


async def test_dispatch_raw_refused_while_paused(env):
    a = _armer(env, state=PAUSE_STATE)
    assert await a.dispatch_raw({"$type": "x"}, "flats") is False
    assert PAUSE_STATE in a.detail and a.calls == []


async def test_update_refused_while_paused(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    import photonscript.scheduler.runs as runs
    monkeypatch.setattr(app, "_config", PhotonScriptConfig(data_dir=tmp_path))
    monkeypatch.setattr(runs, "_backfill_state", {})
    monkeypatch.setattr(runs, "_regrade_all", {"running": False})
    monkeypatch.setattr(app, "get_armer", lambda: SimpleNamespace(state=PAUSE_STATE))
    transport = httpx.ASGITransport(app=app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/update?allow_armed=true")
    assert r.status_code == 409 and PAUSE_STATE in r.json()["detail"]


# -------------------------------------------------------------- 3. the panel

def _tree(done=5, leaf="TakeExposure"):
    """The generator's RC16 night with the Heart Nebula OIII loop running."""
    from tests.test_scheduler.test_ps77_safety_stop import _gen
    seq = _gen()

    def walk(n, path):
        path = path + [n]
        if n.get("Name") == "Smart Exposure" and not any(
                p.get("_mark") for p in path):
            for p in path:
                p["Status"] = "RUNNING"
            n["_mark"] = True
            for c in n["Conditions"]["$values"]:
                if "Iterations" in c:
                    c["CompletedIterations"] = done
            for it in n["Items"]["$values"]:
                if leaf in it["$type"]:
                    it["Status"] = "RUNNING"
                    it["ExposureTime"] = 300.0
                elif "SwitchFilter" in it["$type"]:
                    it["Status"] = "FINISHED"
            return True
        for key in ("Items",):
            for ch in (n.get(key) or {}).get("$values", []) or []:
                if walk(ch, path):
                    return True
        return False
    walk(seq, [])
    return seq


def test_sequence_position_target_sub_and_next():
    pos = wp.sequence_position(_tree(done=5))
    assert pos["running"] and pos["target"] == "Heart Nebula"
    assert pos["sub"] == {"n": 6, "of": 40}
    assert pos["exposure_s"] == 300.0
    assert pos["leaf"] == "TakeExposure"
    # after this loop: the next filter block (switch, AF, focus offset, OIII->Ha)
    assert pos["next"][0] == "SwitchFilter" and len(pos["next"]) == 3


def test_sequence_position_idle_and_envelope():
    assert wp.sequence_position([])["running"] is False
    tree = {"Response": [{"Name": "Targets_Container", "Status": "RUNNING", "Items": [
        {"Name": "TARGETS_CONTAINER_Container", "Status": "RUNNING", "Items": [
            {"Name": "M 31_Container", "Status": "RUNNING", "Items": [
                {"Name": "Slew and center", "Status": "RUNNING"},
                {"Name": "Send to Pushover", "Status": "CREATED"},
                {"Name": "Run Autofocus", "Status": "CREATED"}]}]}]}]}
    pos = wp.sequence_position(tree)
    assert pos["target"] == "M 31" and pos["leaf"] == "Slew and center"
    assert pos["next"] == ["Run Autofocus"] and pos["sub"] is None


def test_exposure_view_progress_and_left():
    cam = _exposing(120)
    e = wp.exposure_view(cam, 300.0, now_utc=UTC0)
    assert e["exposing"] and e["left_s"] == 120 and e["elapsed_s"] == 180
    assert e["progress"] == pytest.approx(0.6)
    assert wp.exposure_view(None, 300.0) is None
    assert wp.exposure_view(IDLE, None)["exposing"] is False


def test_mosaic_panel_matches_the_running_target():
    proj = [SimpleNamespace(target=SimpleNamespace(name="M31 P2"),
                            mosaic={"id": "m1", "name": "M31", "panel": 2, "of": 4}),
            SimpleNamespace(target=SimpleNamespace(name="Heart Nebula"), mosaic=None)]
    assert wp.mosaic_panel(proj, "m31  p2") == {"name": "M31", "panel": 2, "of": 4}
    assert wp.mosaic_panel(proj, "Heart Nebula") is None
    assert wp.mosaic_panel(proj, None) is None


def test_countdowns():
    now = datetime(2026, 10, 7, 4, 0, 0)
    c = wp.countdowns({"night_of": "2026-10-06", "dusk_utc": "2026-10-07T01:30:00Z",
                       "dawn_utc": "2026-10-07T11:00:00Z"}, "2026-10-07T11:30:00Z", now)
    assert c["dawn_in_s"] == 7 * 3600 and c["shutdown_in_s"] == 7.5 * 3600
    assert c["dusk_in_s"] < 0


async def test_collect_assembles_both_rigs(env, monkeypatch):
    from photonscript.scheduler import sideload as sd
    cfg = _cfg(env.tmp, piggyback_enabled=True)
    tree1 = _tree(done=2)

    async def _read(base, client=None):
        return (tree1, None) if base == cfg.nina_base_url else ([], None)
    monkeypatch.setattr(sd, "read_sequence_state", _read)
    piggy_base = None

    async def _get(base, path):
        if base == cfg.nina_base_url:
            return {"/equipment/camera/info": {**_exposing(100), "Temperature": 0.2,
                                               "TemperatureSetPoint": 0, "CoolerOn": True,
                                               "CoolerPower": 41},
                    "/equipment/filterwheel/info": {"SelectedFilter": {"Name": "O"}},
                    "/equipment/safetymonitor/info": {"Connected": True, "IsSafe": True},
                    }.get(path)
        return {**IDLE, "Temperature": 0.1, "CoolerOn": True} if path.endswith("camera/info") else None
    a = _armer(env, piggyback_enabled=True)
    a.config = cfg
    tel = {"guiding": {"state": "Guiding", "rms_total_arcsec": 0.41, "units": "arcsec"},
           "mount_ra": 5.5, "mount_dec": 22.0}
    proj = [SimpleNamespace(target=SimpleNamespace(name="Heart Nebula"),
                            mosaic={"id": "h", "name": "Heart", "panel": 1, "of": 2})]
    d = await wp.collect(cfg, a, tel=tel, projects=proj, get=_get)
    r = d["rc16"]
    assert r["target"] == "Heart Nebula" and r["mosaic"]["panel"] == 1
    assert r["filter"] == "OIII" and r["sub"] == {"n": 3, "of": 40}
    assert r["exposure"]["exposing"] and r["exposure"]["total_s"] == 300.0
    assert d["guiding"]["mode"] == "guided" and d["guiding"]["rms"] == 0.41
    assert d["cooler"]["rc16"]["cooler_on"] is True and d["cooler"]["piggyback"]["temp_c"] == 0.1
    assert d["safety"] == {"is_safe": True, "roof": "open"}
    assert d["piggy"]["sequence_running"] is False and "split" in d["piggy"]
    assert d["armer"]["can_pause"] is True and d["armer"]["can_resume"] is False
    assert d["dawn"]["dawn_in_s"] > 0
    json.dumps(d)   # serialisable


async def test_collect_fails_soft_when_nina_is_down(env, monkeypatch):
    from photonscript.scheduler import sideload as sd

    async def _read(base, client=None):
        return None, "ConnectError"
    monkeypatch.setattr(sd, "read_sequence_state", _read)

    async def _get(base, path):
        return None
    a = _armer(env, state="DISARMED")
    d = await wp.collect(a.config, a, tel={"current_target": None}, get=_get)
    assert d["rc16"]["target"] is None and d["rc16"]["nina_error"] == "ConnectError"
    assert d["rc16"]["exposure"] is None and d["safety"]["roof"] == "unknown"
    assert d["piggy"] is None and d["armer"]["can_pause"] is False


# ------------------------------------------------------------------ API

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


async def test_api_pause_resume_and_where(api):
    r = await _call(api.app, "GET", "/api/night/where")
    assert r.status_code == 200 and r.json()["armer"]["can_pause"] is True
    r = await _call(api.app, "POST", "/api/arm/resume")
    assert r.status_code == 409
    r = await _call(api.app, "POST", "/api/arm/pause", {"piggy": "keep"})
    assert r.status_code == 200 and r.json()["state"] == PAUSE_STATE
    await api.armer._pause_task
    r = await _call(api.app, "POST", "/api/arm/pause")
    assert r.status_code == 409
    r = await _call(api.app, "GET", "/api/night/where")
    assert r.json()["armer"]["can_resume"] is True
    r = await _call(api.app, "POST", "/api/arm/resume")
    assert r.status_code == 200 and r.json()["state"] == "RUNNING"


# ------------------------------------------------------- 4. static checks

def test_dashboard_panel_wiring_and_ascii():
    from tests.test_dashboard_js_strings import _raw_newline_strings
    dash = (ROOT / "photonscript/scheduler/templates/dashboard.html").read_text(encoding="utf-8")
    js = (ROOT / "photonscript/scheduler/static/js/night_panel.js").read_text(encoding="utf-8")
    for el in ('id="wherePanel"', 'id="pauseBtn"', 'id="resumeBtn"', 'id="pauseConfirm"',
               'id="pausePiggyKeep" checked', 'id="pauseAfter" checked', "night_panel.js",
               "window.loadWherePanel", "window.tickWherePanel", "PAUSED_OPERATOR"):
        assert el in dash, el
    for s in ("/api/night/where", "/api/arm/pause", "/api/arm/resume", "confirm("):
        assert s in js, s
    # rides the existing polls: no timers of its own
    assert "setInterval" not in js and "setTimeout" not in js
    assert _raw_newline_strings(js) == []
    for p in ("photonscript/scheduler/static/js/night_panel.js",
              "photonscript/scheduler/night_pause.py",
              "photonscript/scheduler/where_panel.py",
              "photonscript/scheduler/routers/pause.py",
              "photonscript/scheduler/routers/where.py",
              "tests/test_scheduler/test_ps64_pause_panel.py"):
        b = (ROOT / p).read_bytes()
        assert b.isascii(), p
    src = (ROOT / "photonscript/scheduler/armer.py").read_text(encoding="utf-8")
    block = src.split("# -- PS-64: operator Pause / Resume", 1)[1].split(
        "# -- state machine loop", 1)[0]
    assert block.isascii()
