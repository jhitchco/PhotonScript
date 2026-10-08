"""PS-136: the armer WATCHES a sideloaded night (and PS-132 load validation
in dispatch_raw).

2026-10-05: both NINAs ran hand-sideloaded sequences (PS-123) with the armer
DISARMED, so no dawn check, guiding watchdog, unsafe alerts or update
refusal covered them. WATCHING tracks such a night and never loads, starts,
stops or re-dispatches anything. These tests pin the state machine, the
no-dispatch rule, the dawn verify path, the deploy refusal and the chip.
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from photonscript.scheduler import armer as armer_mod
from photonscript.scheduler import sideload as sd
from photonscript.scheduler.armer import WATCH_STATE, Armer
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.night_events import events_path
from photonscript.shared.phd2_store import night_of, read_jsonl

ROOT = Path(__file__).resolve().parents[2]
# 2026-10-05 21:00 local (MDT) = 2026-10-06 03:00Z, mid-night
NOW = datetime(2026, 10, 6, 3, 0, 0)
DUSK = datetime(2026, 10, 6, 1, 30, 0)
DAWN = datetime(2026, 10, 6, 11, 0, 0)
DUE = DAWN + timedelta(minutes=30)   # dawn_flats_window_min=0: base rule

# Commands watch mode must never send (the guiding watchdog's guider
# restart is allowed, as on an armed night, and tested separately).
FORBIDDEN = {"sequence_load", "sequence_start", "sequence_stop", "mount_park",
             "camera_warm", "guider_stop", "guider_start"}

DSO = "NINA.Sequencer.Container.DeepSkyObjectContainer, NINA.Sequencer"
SEQ = "NINA.Sequencer.Container.SequentialContainer, NINA.Sequencer"
GUIDE = "NINA.Sequencer.SequenceItem.Guider.StartGuiding, NINA.Sequencer"
EXPOSE = "NINA.Sequencer.SequenceItem.Imaging.TakeExposure, NINA.Sequencer"


def _seq():
    """The sideloaded shape: an unguided tracking test, then two guided
    targets and one unguided one."""
    def dso(name, guided):
        items = ([{"$type": GUIDE}] if guided else []) + [{"$type": EXPOSE}]
        return {"$type": DSO, "Name": name, "Items": {"$values": items}}
    return {"$type": SEQ, "Name": "PhotonScript_20261005_TT_M_2_then_tonight",
            "Items": {"$values": [
                dso("Tracking test M 2", False),
                {"$type": SEQ, "Name": "TARGETS_CONTAINER", "Items": {"$values": [
                    dso("M 31", True), dso("Heart Nebula", True),
                    dso("Moon test", False)]}}]}}


def _cfg(tmp_path, **kw):
    kw.setdefault("dawn_flats_window_min", 0)
    kw.setdefault("connect_all_on_arm", False)
    kw.setdefault("image_watch_dir", str(tmp_path / "nina"))
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _plan(config, now=None):
    return {"night_of": "2026-10-05", "preconfig_utc": None,
            "dusk_utc": DUSK.isoformat() + "Z", "dawn_utc": DAWN.isoformat() + "Z",
            "naut_dawn_utc": None, "sunrise_utc": None, "dark_hours": 9.5,
            "targets": [], "watch": True}


def _state(*running):
    """A ninaAPI /sequence/state tree with these items RUNNING."""
    return [{"Name": "Targets_Container", "Status": "RUNNING" if running else "FINISHED",
             "Items": [{"Name": n, "Status": "RUNNING"} for n in running]}]


class _Frozen(datetime):
    @classmethod
    def utcnow(cls):
        return NOW


@pytest.fixture
def env(tmp_path, monkeypatch):
    from photonscript.scheduler import runs
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(armer_mod, "watch_plan", _plan)
    monkeypatch.setattr(armer_mod, "datetime", _Frozen)
    monkeypatch.setattr(runs, "post_night_warm", lambda c, **k: [])
    notes = []

    async def _notify(c, msg, **kw):
        notes.append((msg, kw.get("priority", 0)))
    monkeypatch.setattr(armer_mod, "notify", _notify)
    seq_file = tmp_path / "Sideload_rc16_x.json"
    seq_file.write_text(json.dumps(_seq()), encoding="utf-8")
    sd.record_event(cfg, "rc16", "PhotonScript_20261005_TT_M_2_then_tonight",
                    str(seq_file), "tracking_test_then_tonight", True,
                    "loaded (not started)", now=NOW - timedelta(hours=1))
    return SimpleNamespace(cfg=cfg, notes=notes, seq_file=seq_file,
                           tmp=tmp_path)


def _armer(env, safe=True, guider="Guiding", tree=None, mount=None):
    """An Armer whose ninaAPI and NINA state reads are fakes. a.calls holds
    every _nina key it asked for."""
    a = Armer(env.cfg)
    a.calls = []
    a.fake = {"safe": safe, "guider": guider, "tree": tree if tree is not None
              else (_state("Tracking test M 2_Container"), None),
              "mount": mount or {"Connected": True, "AtPark": True}}

    async def _nina(key, method="GET", json_body=None, **kw):
        a.calls.append(key)
        if key == "safety":
            s = a.fake["safe"]
            return None if s is None else {"Response": {"Connected": True, "IsSafe": s}}
        if key == "guider":
            return {"Response": {"Connected": True, "State": a.fake["guider"]}}
        if key == "mount_info":
            return {"Response": a.fake["mount"]}
        return {"Success": True}
    a._nina = _nina

    async def _read():
        return a.fake["tree"]
    a._watch_read_state = _read
    return a


def _events(env, kind="watch"):
    return [r for r in read_jsonl(events_path(env.cfg, night_of(env.cfg, NOW)))
            if r.get("kind") == kind]


async def _watching(env, **kw):
    a = _armer(env, **kw)
    res = await a.start_watch("button", now=NOW)
    assert res["ok"], res
    if a._task:
        a._task.cancel()   # the tests drive _watch_tick by hand
    return a


# --------------------------------------------------------------- helpers

def test_guided_targets_reads_startguiding_per_target():
    assert armer_mod.guided_targets(_seq()) == ["M 31", "Heart Nebula"]


def test_guided_targets_from_file_none_when_unreadable(tmp_path):
    assert armer_mod.guided_targets_from_file(None) is None
    assert armer_mod.guided_targets_from_file(tmp_path / "missing.json") is None


def test_watching_is_not_an_active_state():
    """Not in ACTIVE_STATES: armer_guided_now (PS-93 recalibration
    re-dispatch) and the sideload's own 409 must not see it."""
    assert WATCH_STATE not in armer_mod.ACTIVE_STATES
    assert WATCH_STATE in armer_mod.LIVE_STATES


# --------------------------------------------------------- entering watch

async def test_watch_from_disarmed(env):
    a = await _watching(env)
    assert a.state == WATCH_STATE
    assert a.watch["guided_targets"] == ["M 31", "Heart Nebula"]
    assert a.watch["sequence"].startswith("PhotonScript_20261005_TT")
    assert a.guiding_override == "guided"
    assert a.sequence_path == env.seq_file
    assert a.plan["dawn_utc"].startswith("2026-10-06T11:00")
    ev = _events(env)
    assert [e["value"] for e in ev] == ["start"]
    assert any("WATCHING" in m for m, _ in env.notes)
    assert not set(a.calls) & FORBIDDEN
    # persisted, so a restart reattaches
    saved = json.loads((env.tmp / "armer_state.json").read_text(encoding="utf-8"))
    assert saved["state"] == WATCH_STATE and saved["watch"]["trigger"] == "button"


@pytest.mark.parametrize("state", ["ARMED", "RUNNING", "PAUSED_UNSAFE", WATCH_STATE])
async def test_watch_refused_while_busy(env, state):
    a = _armer(env)
    a.state = state
    res = await a.start_watch("button", now=NOW)
    assert res["ok"] is False and state in res["detail"]
    assert a.state == state


async def test_watch_refused_after_dawn_shutdown_time(env):
    a = _armer(env)
    res = await a.start_watch("button", now=DUE + timedelta(minutes=1))
    assert res["ok"] is False and "passed" in res["detail"]
    assert a.state == "DISARMED"


async def test_watch_without_a_sideload_uses_config_guiding(env, tmp_path):
    cfg = _cfg(tmp_path / "other")
    a = Armer(cfg)
    a._nina = _armer(env)._nina
    res = await a.start_watch("button", now=NOW)
    if a._task:
        a._task.cancel()
    assert res["ok"] and a.watch["guided_targets"] is None
    assert a.guiding_override is None


# ------------------------------------------------------------- auto adopt

def _reader(tree):
    async def read(base):
        return tree
    return read


async def test_auto_adopt_when_nina_runs_the_sideload(env):
    a = _armer(env)
    res = await a.maybe_adopt(now=NOW, reader=_reader(
        (_state("Tracking test M 2_Container"), None)))
    if a._task:
        a._task.cancel()
    assert res and res["ok"] and a.state == WATCH_STATE
    assert a.watch["trigger"] == "auto"


@pytest.mark.parametrize("tree", [([], None), (_state(), None),
                                  (None, "ConnectError: refused")])
async def test_no_adopt_when_nina_idle_or_unreadable(env, tree):
    a = _armer(env)
    assert await a.maybe_adopt(now=NOW, reader=_reader(tree)) is None
    assert a.state == "DISARMED"


async def test_no_adopt_without_tonights_rc16_sideload(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(armer_mod, "watch_plan", _plan)
    # a Piggy-600 load alone is not the RC16 night
    sd.record_event(cfg, "piggyback", "Companion", "c.json", None, True, "loaded",
                    now=NOW - timedelta(hours=1))
    a = Armer(cfg)
    assert await a.maybe_adopt(now=NOW, reader=_reader(
        (_state("Lights"), None))) is None


async def test_no_adopt_when_disabled_or_armed(env, monkeypatch):
    run = _reader((_state("M 31_Container"), None))
    a = _armer(env)
    a.state = "ARMED"
    assert await a.maybe_adopt(now=NOW, reader=run) is None
    cfg = _cfg(env.tmp, watch_sideload_auto=False)
    b = Armer(cfg)
    assert await b.maybe_adopt(now=NOW, reader=run) is None


async def test_stop_watching_never_readopts_that_sideload(env):
    a = await _watching(env)
    await a.disarm()
    assert a.state == "DISARMED"
    assert not set(a.calls) & FORBIDDEN        # no make-safe for a watch
    assert [e["value"] for e in _events(env)] == ["start", "stop"]
    run = _reader((_state("M 31_Container"), None))
    assert await a.maybe_adopt(now=NOW, reader=run) is None
    # a fresh sideload later that night is adopted again
    sd.record_event(env.cfg, "rc16", "Second", str(env.seq_file), None, True,
                    "loaded", now=NOW + timedelta(minutes=5))
    res = await a.maybe_adopt(now=NOW + timedelta(minutes=6), reader=run)
    if a._task:
        a._task.cancel()
    assert res and a.watch["sequence"] == "Second"


# ------------------------------------------------- the watch state machine

async def test_tick_sends_no_dispatch_command(env):
    a = await _watching(env)
    for i in range(5):
        await a._watch_tick(NOW + timedelta(minutes=i))
    assert a.state == WATCH_STATE
    assert not set(a.calls) & FORBIDDEN
    assert a.watch["running"][-1] == "Tracking test M 2_Container"


async def test_dispatch_and_arm_refused_while_watching(env, monkeypatch):
    a = await _watching(env)
    assert await a.dispatch_raw({"$type": SEQ}, "dusk flats") is False
    assert "WATCHING" in a.detail
    out = await a.arm(guiding="guided")
    assert "refused" in out and a.state == WATCH_STATE
    monkeypatch.setattr(a, "_dispatch", lambda: pytest.fail("dispatched"))
    await a._tick()     # the frozen clock: NOW, mid-night
    assert not set(a.calls) & FORBIDDEN


async def test_sequence_end_completes_after_idle_ticks(env, monkeypatch):
    a = await _watching(env)
    verify = []

    async def _verify(delay_s=0):
        verify.append(delay_s)
    monkeypatch.setattr(a, "_verify_watched", _verify)
    a.fake["tree"] = (_state(), None)
    for i in range(armer_mod.WATCH_END_IDLE_TICKS - 1):
        await a._watch_tick(NOW + timedelta(minutes=i))
        assert a.state == WATCH_STATE
    await a._watch_tick(NOW + timedelta(minutes=5))
    await asyncio.sleep(0)
    assert a.state == "COMPLETE"
    assert a.shutdown["watched"] and a.shutdown["reason"] == "watched night: sequence ended"
    assert a.shutdown["steps"] == ["watch only: no commands sent"]
    assert verify == [a._shutdown_verify_delay_s()]
    assert [e["value"] for e in _events(env)][-1] == "end"
    assert any("Watched night complete" in m for m, _ in env.notes)
    assert not set(a.calls) & FORBIDDEN


async def test_unreadable_nina_never_ends_the_watch(env):
    a = await _watching(env)
    a.fake["tree"] = (None, "ConnectError")
    for i in range(6):
        await a._watch_tick(NOW + timedelta(minutes=i))
    assert a.state == WATCH_STATE


async def test_pause_and_resume(env):
    a = await _watching(env)
    a.fake["safe"] = False
    await a._watch_tick(NOW)
    await a._watch_tick(NOW + timedelta(seconds=30))
    assert a.watch["paused"] and a.watch["pauses"] == 1
    assert sum("PAUSED (watching)" in m and p == 1 for m, p in env.notes) == 1
    a.fake["safe"] = True
    await a._watch_tick(NOW + timedelta(minutes=10))
    assert not a.watch["paused"] and a.state == WATCH_STATE
    assert any("RESUMED (watching)" in m for m, _ in env.notes)
    assert [e["value"] for e in _events(env)] == ["start", "pause", "resume"]
    assert not set(a.calls) & FORBIDDEN


async def test_unsafe_but_still_imaging_alerts_and_never_stops(env):
    a = await _watching(env)
    a.fake["tree"] = ([{"Name": "SAFE_LOOP_Container", "Status": "RUNNING",
                        "Items": [{"Name": "M 31_Container", "Status": "RUNNING"}]}],
                      None)
    a.fake["safe"] = False
    await a._watch_tick(NOW)
    await a._watch_tick(NOW + timedelta(minutes=5))
    await a._watch_tick(NOW + timedelta(minutes=6))
    stuck = [m for m, p in env.notes if "still imaging" in m]
    assert len(stuck) == 1
    assert "stuck_imaging" in [e["value"] for e in _events(env)]
    assert not set(a.calls) & FORBIDDEN


# ----------------------------------------------------------- guiding watchdog

async def test_guiding_watchdog_off_during_tracking_test(env):
    a = await _watching(env, guider="Stopped")
    await a._watch_tick(NOW)
    assert "guider" not in a.calls
    assert not any("not guiding" in m.lower() for m, _ in env.notes)


async def test_guiding_watchdog_on_guided_target(env):
    a = await _watching(env, guider="Stopped",
                        tree=(_state("M 31_Container"), None))
    await a._watch_tick(NOW)
    assert "guider" in a.calls
    msgs = [m for m, _ in env.notes if "not guiding" in m.lower()]
    assert len(msgs) == 1 and msgs[0].startswith("Watching a guided")


async def test_guiding_watchdog_off_on_unguided_target_and_when_unsafe(env):
    a = await _watching(env, guider="Stopped",
                        tree=(_state("Moon test_Container"), None))
    await a._watch_tick(NOW)
    assert "guider" not in a.calls
    a.fake["tree"] = (_state("M 31_Container"), None)
    a.fake["safe"] = False
    await a._watch_tick(NOW + timedelta(minutes=1))
    assert "guider" not in a.calls


def test_guiding_gate_without_file_uses_config(env):
    a = _armer(env)
    a.watch = {"guided_targets": None}
    assert a._watch_guiding_active(["M 31_Container"]) is True
    assert a._watch_guiding_active(["Tracking test M 2_Container"]) is False
    b = Armer(_cfg(env.tmp, guided_default=False))
    b.watch = {"guided_targets": None}
    assert b._watch_guiding_active(["M 31_Container"]) is False


# ---------------------------------------------------------------- dawn

async def test_dawn_verify_path_sends_no_commands(env, monkeypatch):
    a = await _watching(env, tree=(_state("WAIT_SAFE_Container"), None))
    verify = []

    async def _verify(delay_s=0):
        verify.append(delay_s)
    monkeypatch.setattr(a, "_verify_watched", _verify)
    await a._watch_tick(DUE)
    await asyncio.sleep(0)
    assert a.state == "COMPLETE"
    assert a.shutdown["reason"] == "watched night: dawn"
    assert any("still running" in s for s in a.shutdown["steps"])
    assert any("STILL running" in m and p == 1 for m, p in env.notes)
    assert verify and not set(a.calls) & FORBIDDEN


async def test_dawn_shutdown_action_runs_dawn_shutdown(env, monkeypatch):
    a = await _watching(env)
    a.config.watch_dawn_action = "shutdown"
    ran = []

    async def _ds(reason="dawn"):
        ran.append(reason)
        return "stop ok"
    monkeypatch.setattr(a, "dawn_shutdown", _ds)
    await a._watch_tick(DUE)
    assert ran == ["watched night: dawn"] and a.state == "COMPLETE"


async def test_verify_watched_reads_only_and_alerts(env, monkeypatch):
    from photonscript.shared import rigs
    a = await _watching(env, mount={"Connected": True, "AtPark": False})
    a.shutdown = {"at": "x", "steps": [], "verify": None, "watched": True}
    warmed = []

    async def _info(base):
        return {"CoolerOn": True, "Temperature": -10.0}

    async def _warm(*a_, **k):
        warmed.append(a_)
        return {"ok": True}
    monkeypatch.setattr(rigs, "rig_ids", lambda c: ["rc16"])
    monkeypatch.setattr(rigs, "nina_camera_info", _info)
    monkeypatch.setattr(rigs, "nina_warm", _warm)
    res = await a._verify_watched(delay_s=0)
    assert res["ok"] is False and res["parked"] is False
    assert a.shutdown["verify"]["ok"] is False
    assert warmed == []                     # watch mode never retries a warm
    msg = [m for m, p in env.notes if "Watched night check" in m]
    assert msg and "cooler STILL ON" in msg[0] and "NOT parked" in msg[0]


async def test_verify_watched_ok(env, monkeypatch):
    from photonscript.shared import rigs
    a = await _watching(env)
    a.shutdown = {"at": "x", "steps": [], "verify": None, "watched": True}

    async def _info(base):
        return {"CoolerOn": False, "Temperature": 18.0}
    monkeypatch.setattr(rigs, "rig_ids", lambda c: ["rc16"])
    monkeypatch.setattr(rigs, "nina_camera_info", _info)
    res = await a._verify_watched(delay_s=0)
    assert res["ok"] and res["parked"] is True
    assert not any("Watched night check" in m for m, _ in env.notes)


# ------------------------------------------------------------ restart

async def test_restore_reattaches_a_watch(env):
    a = await _watching(env)
    b = _armer(env)
    assert b.restore() is True
    if b._task:
        b._task.cancel()
    assert b.state == WATCH_STATE and b.watch["sequence"] == a.watch["sequence"]


# ---------------------------------------------------- deploy refusal (PS-58)

async def test_update_refused_while_watching(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    import photonscript.scheduler.runs as runs
    monkeypatch.setattr(app, "_config", PhotonScriptConfig(data_dir=tmp_path))
    monkeypatch.setattr(runs, "_backfill_state", {})
    monkeypatch.setattr(runs, "_regrade_all", {"running": False})
    monkeypatch.setattr(app, "get_armer", lambda: SimpleNamespace(state=WATCH_STATE))
    transport = httpx.ASGITransport(app=app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/api/update?allow_armed=true")
    assert r.status_code == 409 and WATCH_STATE in r.json()["detail"]


def test_self_update_sees_a_watched_night(tmp_path):
    from photonscript.shared.updater import night_active
    cfg = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path))
    dawn = (datetime.utcnow() + timedelta(hours=3)).isoformat() + "Z"
    (tmp_path / "armer_state.json").write_text(json.dumps(
        {"state": WATCH_STATE, "plan": {"dawn_utc": dawn}}), encoding="utf-8")
    assert night_active(cfg) == WATCH_STATE


# --------------------------------------------------------- API + chip

@pytest.fixture
def api(env, monkeypatch):
    import photonscript.scheduler.app as app
    a = _armer(env)
    monkeypatch.setattr(app, "_config", env.cfg)
    monkeypatch.setattr(app, "get_armer", lambda: a)

    async def _read(base, client=None):
        return a.fake["tree"]
    monkeypatch.setattr(sd, "read_sequence_state", _read)
    return SimpleNamespace(app=app, armer=a)


async def _call(app, method, path, body=None):
    transport = httpx.ASGITransport(app=app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.request(method, path, json=body)


async def test_api_watch_start_stop_and_arm_conflict(api):
    r = await _call(api.app, "GET", "/api/arm/watch")
    assert r.status_code == 200 and r.json()["auto"] is True
    assert r.json()["nina_running"] == ["Targets_Container", "Tracking test M 2_Container"]
    r = await _call(api.app, "POST", "/api/arm/watch")
    if api.armer._task:
        api.armer._task.cancel()
    assert r.status_code == 200 and r.json()["state"] == WATCH_STATE
    st = (await _call(api.app, "GET", "/api/arm")).json()
    assert st["watch"]["trigger"] == "button"
    r = await _call(api.app, "POST", "/api/arm", {"armed": True, "guiding": "guided"})
    assert r.status_code == 409 and "WATCHING" in r.json()["detail"]
    r = await _call(api.app, "POST", "/api/arm/watch")
    assert r.status_code == 409
    r = await _call(api.app, "POST", "/api/arm/watch/stop")
    assert r.status_code == 200 and r.json()["state"] == "DISARMED"
    assert (await _call(api.app, "GET", "/api/arm")).json()["watch"] is None
    r = await _call(api.app, "POST", "/api/arm/watch/stop")
    assert r.status_code == 409


def test_dashboard_has_watch_button_and_chip():
    html = (ROOT / "photonscript/scheduler/templates/dashboard.html").read_text(
        encoding="utf-8")
    assert 'id="watchBtn"' in html
    assert "Watching sideloaded night" in html
    assert "WATCHING:'#a78bfa'" in html
    assert "/api/arm/watch/stop" in html


def test_status_watch_only_while_watching(env):
    a = _armer(env)
    a.watch = {"sequence": "x"}
    assert a.status()["watch"] is None
    a.state = WATCH_STATE
    assert a.status()["watch"] == {"sequence": "x"}


# ------------------------------------- PS-132 load validation in dispatch_raw

@pytest.fixture
def raw(env, monkeypatch):
    from photonscript.scheduler import calibration_capture, nina_validation as nv
    monkeypatch.setattr(calibration_capture, "busy", lambda rig: False)
    a = _armer(env)
    seen = SimpleNamespace(checks=[], alerts=[], result={"ok": True, "issues": [],
                                                         "errors": [], "detail": "ok"})

    async def _check(base, config, rig, since=None, **kw):
        seen.checks.append((base, rig, since))
        return seen.result

    async def _alert(config, rig, res, what):
        seen.alerts.append(what)
    monkeypatch.setattr(nv, "check_loaded", _check)
    monkeypatch.setattr(nv, "alert", _alert)
    seen.armer = a
    return seen


async def test_dispatch_raw_validates_between_load_and_start(raw):
    a = raw.armer
    assert await a.dispatch_raw({"$type": SEQ}, "dusk flats x") is True
    assert a.calls == ["sequence_stop", "sequence_load", "sequence_start"]
    assert len(raw.checks) == 1 and raw.checks[0][1] == "rc16"
    assert raw.alerts == []


async def test_dispatch_raw_alert_mode_pushes_and_starts(raw):
    raw.result = {"ok": False, "issues": [], "errors": ["boom (in SkyFlat.Validate)"],
                  "detail": "1 validation error(s)"}
    a = raw.armer
    assert await a.dispatch_raw({"$type": SEQ}, "dusk flats x") is True
    assert raw.alerts == ["armer dispatch: dusk flats x"]
    assert "sequence_start" in a.calls and a.last_validation is raw.result


async def test_dispatch_raw_refuse_mode_skips_start(raw):
    raw.result = {"ok": False, "issues": [], "errors": ["boom"], "detail": "1 error"}
    a = raw.armer
    a.config.nina_load_validation = "refuse"
    assert await a.dispatch_raw({"$type": SEQ}, "dusk flats x") is False
    assert "sequence_start" not in a.calls and "NOT started" in a.detail


async def test_dispatch_raw_refuse_mode_issues_alone_still_start(raw):
    raw.result = {"ok": False, "issues": ["Camera: not connected"], "errors": [],
                  "detail": "1 issue"}
    a = raw.armer
    a.config.nina_load_validation = "refuse"
    assert await a.dispatch_raw({"$type": SEQ}, "x") is True
    assert "sequence_start" in a.calls and raw.alerts == ["armer dispatch: x"]


async def test_dispatch_raw_validation_off(raw):
    a = raw.armer
    a.config.nina_load_validation = "off"
    assert await a.dispatch_raw({"$type": SEQ}, "x") is True
    assert raw.checks == []


def test_config_defaults_keep_live_behavior():
    cfg = PhotonScriptConfig(_env_file=None)
    assert cfg.watch_sideload_auto is True
    assert cfg.watch_dawn_action == "verify"
    assert cfg.nina_load_validation == "alert"


def test_system_page_fields():
    import photonscript.scheduler.app as app
    keys = {f[0] for f in app._CONFIG_FIELDS}
    assert {"watch_sideload_auto", "watch_dawn_action", "nina_load_validation",
            "nina_load_validation_settle_s"} <= keys


def test_new_sources_are_ascii():
    for rel in ("photonscript/scheduler/routers/watch.py",
                "photonscript/scheduler/nina_validation.py",
                "tests/test_scheduler/test_ps136_watch_sideload.py"):
        (ROOT / rel).read_bytes().decode("ascii")
