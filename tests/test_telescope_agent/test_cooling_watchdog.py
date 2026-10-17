"""Cooler-stall recovery (2026-10-17): "when it's not cooling after a bit,
disconnect and reconnect the camera".

2026-10-16 both cameras sat at 32 to 35 C with the cooler effectively off
after the evening Cool Camera failed (cooler gate SKIP "sensor 35.0 C vs
setpoint 0 C after 20 min"); a capture job deferred "cooler stalled at
23.4 C (TEC 0 %)". Earlier that week a Piggy cooler at 0 % power was fixed
by disconnecting, reconnecting and cooling the camera by hand."""

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from photonscript.shared import cooler_recovery as cr
from photonscript.shared.config import PhotonScriptConfig

T0 = 1_800_000_000.0     # a fixed epoch; the ledger night follows it


def _cfg(tmp_path, **kw):
    base = dict(_env_file=None, data_dir=str(tmp_path), camera_setpoint_c=0.0,
                cooling_tolerance_c=1.0, cooler_stall_reconnect_wait_s=0.0,
                observatory_tz="UTC")
    base.update(kw)
    return PhotonScriptConfig(**base)


class Clock:
    def __init__(self, t=T0):
        self.t = t

    def __call__(self):
        return self.t


class FakePort:
    """A fake NINA camera: `state` is what camera() returns; after a
    reconnect the cooler follows `after` (None = stays as it was)."""

    def __init__(self, state, after=None):
        self.state = dict(state)
        self.after = after
        self.calls = []

    async def camera(self):
        self.calls.append("camera")
        return dict(self.state)

    async def disconnect(self):
        self.calls.append("disconnect")

    async def connect(self):
        self.calls.append("connect")
        if self.after is not None:
            self.state.update(self.after)

    async def cool(self, setpoint, minutes):
        self.calls.append(("cool", setpoint, minutes))


async def _nosleep(_s):
    return None


def _watch(cfg, port, clock, sent, rig="piggyback"):
    async def notify_fn(config, msg, **kw):
        sent.append((msg, kw.get("priority", 0)))
    return cr.CoolerStallWatch(cfg, rig, port, setpoint=0.0, cool_minutes=0.0,
                               clock=clock, sleep=_nosleep, notify_fn=notify_fn)


def _feed(w, clock, cam, minutes, step_s=60, expected=True):
    """Poll every step_s for `minutes`; returns the non-None results."""
    out = []
    for _ in range(int(minutes * 60 / step_s)):
        clock.t += step_s
        r = asyncio.run(w.tick(dict(cam), expected))
        if r:
            out.append(r)
    return out


STALLED = {"CoolerOn": True, "CoolerPower": 0.0, "Temperature": 23.4,
           "IsExposing": False, "CameraState": "Idle"}
OFF = {"CoolerOn": False, "CoolerPower": 0.0, "Temperature": 35.0,
       "IsExposing": False, "CameraState": "NoState"}


def _events(cfg):
    from photonscript.shared.night_events import events_path
    p = events_path(cfg, cr._night(cfg, T0))
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


# ---- stall detected and recovered ------------------------------------------

def test_stall_detected_reconnected_and_recovered(tmp_path):
    cfg = _cfg(tmp_path)
    clock, sent = Clock(), []
    port = FakePort(STALLED, after={"CoolerPower": 85.0})
    w = _watch(cfg, port, clock, sent)
    res = _feed(w, clock, STALLED, 7)
    assert res == [] and port.calls == []          # not a full window yet
    res = _feed(w, clock, STALLED, 2)
    assert [r["action"] for r in res] == ["reconnected"]
    assert port.calls == ["camera", "disconnect", "connect", ("cool", 0.0, 0.0)]
    assert sent == []                               # result not known yet
    # the revived cooler pulls down: one "recovered" push
    clock.t += 300
    r = asyncio.run(w.tick({"CoolerOn": True, "CoolerPower": 90.0,
                            "Temperature": 0.6}, True))
    assert r == {"action": "recovered", "attempt": 1}
    assert len(sent) == 1 and "recovered" in sent[0][0] and sent[0][1] == 0
    rows = cr.attempts(cfg, "piggyback", clock.t)
    assert len(rows) == 1 and rows[0]["result"] == "recovered"
    steps = [e["value"] for e in _events(cfg) if e["kind"] == "cooler_recovery"]
    assert steps == ["stall", "disconnect", "connect", "cool", "recovered"]


def test_cooler_off_while_expected_is_a_stall(tmp_path):
    """The 2026-10-16 evening: Cool Camera failed, cooler OFF at 35 C."""
    cfg = _cfg(tmp_path)
    clock = Clock()
    port = FakePort(OFF, after={"CoolerOn": True, "CoolerPower": 100.0})
    w = _watch(cfg, port, clock, [])
    res = _feed(w, clock, OFF, 9)
    assert res and res[0]["action"] == "reconnected"
    assert "cooler OFF" in res[0]["reason"]
    assert ("cool", 0.0, 0.0) in port.calls


def test_not_dropping_with_power_reported_is_a_stall(tmp_path):
    cfg = _cfg(tmp_path)
    clock = Clock()
    cam = dict(STALLED, CoolerPower=40.0, Temperature=20.0)
    w = _watch(cfg, FakePort(cam), clock, [])
    res = _feed(w, clock, cam, 9)
    assert res and "fell only" in res[0]["reason"]


# ---- never while exposing -------------------------------------------------

def test_no_action_while_exposing(tmp_path):
    cfg = _cfg(tmp_path)
    clock, sent = Clock(), []
    busy = dict(STALLED, IsExposing=True, CameraState="Exposing")
    port = FakePort(busy)
    w = _watch(cfg, port, clock, sent)
    res = _feed(w, clock, busy, 10)
    assert res and {r["action"] for r in res} == {"deferred"}
    assert "disconnect" not in port.calls and sent == []
    assert cr.attempts(cfg, "piggyback", clock.t) == []   # no budget used
    # idle again: the next stalled poll acts
    port.state = dict(STALLED)
    res = _feed(w, clock, STALLED, 1)
    assert res[-1]["action"] == "reconnected"


@pytest.mark.parametrize("cam,busy", [
    ({"IsExposing": True}, True),
    ({"IsExposing": False, "CameraState": "Download"}, True),
    ({"IsExposing": False, "CameraState": "Idle"}, False),
    ({"IsExposing": False, "CameraState": "NoState"}, False),
    ({"Temperature": 20.0}, True),          # unknown counts as busy
    (None, True),
])
def test_camera_busy(cam, busy):
    assert cr.camera_busy(cam) is busy


# ---- attempt cap and spacing -----------------------------------------------

def test_attempt_cap_and_spacing(tmp_path):
    cfg = _cfg(tmp_path)        # defaults: 2 attempts, 15 min apart
    clock, sent = Clock(), []
    port = FakePort(STALLED)    # a reconnect never helps
    w = _watch(cfg, port, clock, sent)
    res = _feed(w, clock, STALLED, 9)
    assert [r["action"] for r in res] == ["reconnected"]
    # 8 min later: the "still stalled" push (one per attempt result); still
    # stalled within 15 min of attempt 1: spacing holds the next one back
    res = _feed(w, clock, STALLED, 8)
    assert res[0]["action"] == "still_stalled"
    assert len(sent) == 1 and "STILL stalled" in sent[0][0] and sent[0][1] == 1
    assert "next reconnect" in sent[0][0]
    res = res[1:] + _feed(w, clock, STALLED, 5)
    assert res and {r["action"] for r in res} == {"capped"}
    assert "spacing" in res[0]["why"]
    assert port.calls.count("disconnect") == 1
    # past 15 min: attempt 2, then its result
    res = _feed(w, clock, STALLED, 3)
    assert res[-1]["action"] == "reconnected"
    assert port.calls.count("disconnect") == 2
    res = _feed(w, clock, STALLED, 8)
    assert res[0]["action"] == "still_stalled"
    assert "no more reconnects tonight" in sent[-1][0]
    # the cap: no third reconnect tonight, no more pushes
    res = _feed(w, clock, STALLED, 60)
    assert res and {r["action"] for r in res} == {"capped"}
    assert "2 of 2" in res[-1]["why"]
    assert port.calls.count("disconnect") == 2 and len(sent) == 2


def test_cap_is_shared_through_the_ledger(tmp_path):
    """The agent and a capture job share one budget (cooler_recovery.json)."""
    cfg = _cfg(tmp_path, cooler_stall_max_attempts=1)
    clock = Clock()
    w1 = _watch(cfg, FakePort(STALLED), clock, [])
    _feed(w1, clock, STALLED, 9)
    port2 = FakePort(STALLED)
    w2 = _watch(cfg, port2, clock, [])
    clock.t += 3600
    r = asyncio.run(w2.recover("job stall"))
    assert r["action"] == "capped" and "disconnect" not in port2.calls


def test_reconnect_error_is_one_push(tmp_path):
    cfg = _cfg(tmp_path)
    clock, sent = Clock(), []

    class Broken(FakePort):
        async def connect(self):
            raise RuntimeError("camera not found")
    w = _watch(cfg, Broken(STALLED), clock, sent)
    res = _feed(w, clock, STALLED, 9)
    assert res[-1]["action"] == "error"
    assert len(sent) == 1 and "errored" in sent[0][0]
    assert cr.attempts(cfg, "piggyback", clock.t)[0]["result"] == "error"


# ---- a cooling camera is not flagged ---------------------------------------

def test_cooling_camera_not_flagged(tmp_path):
    cfg = _cfg(tmp_path)
    clock, sent = Clock(), []
    port = FakePort(STALLED)
    w = _watch(cfg, port, clock, sent)
    t = 35.0
    for _ in range(40):                  # 1 C a minute, TEC working
        clock.t += 60
        t = max(0.3, t - 1.0)
        assert asyncio.run(w.tick({"CoolerOn": True, "CoolerPower": 80.0,
                                   "Temperature": t}, True)) is None
    assert port.calls == [] and sent == []


def test_flat_out_tec_not_flagged(tmp_path):
    """A TEC at 100 % that cannot reach the setpoint (hot day) is not a
    stall a reconnect fixes."""
    cfg = _cfg(tmp_path)
    clock = Clock()
    cam = {"CoolerOn": True, "CoolerPower": 100.0, "Temperature": 9.0}
    port = FakePort(cam)
    w = _watch(cfg, port, clock, [])
    assert _feed(w, clock, cam, 20) == [] and port.calls == []
    r = asyncio.run(w.recover("job says stalled"))
    assert r["action"] == "flat_out" and "disconnect" not in port.calls


def test_at_setpoint_not_flagged(tmp_path):
    cfg = _cfg(tmp_path)
    clock = Clock()
    cam = {"CoolerOn": True, "CoolerPower": 2.0, "Temperature": 0.4}
    w = _watch(cfg, FakePort(cam), clock, [])
    assert _feed(w, clock, cam, 20) == []


# ---- no action when disarmed -----------------------------------------------

def test_no_action_when_not_expected(tmp_path):
    cfg = _cfg(tmp_path)
    clock = Clock()
    port = FakePort(STALLED)
    w = _watch(cfg, port, clock, [])
    assert _feed(w, clock, STALLED, 30, expected=False) == []
    assert port.calls == []


def test_disabled_does_nothing(tmp_path):
    cfg = _cfg(tmp_path, cooler_stall_recovery=False)
    clock = Clock()
    port = FakePort(STALLED)
    w = _watch(cfg, port, clock, [])
    assert _feed(w, clock, STALLED, 30) == [] and port.calls == []


def _armer(tmp_path, state, dusk=None, dawn=None):
    plan = {}
    if dusk:
        plan = {"dusk_utc": dusk.isoformat() + "Z", "dawn_utc": dawn.isoformat() + "Z"}
    (tmp_path / "armer_state.json").write_text(json.dumps({"state": state,
                                                           "plan": plan}))


@pytest.mark.parametrize("state", ["DISARMED", "COMPLETE", "ERROR"])
def test_cooling_not_expected_when_disarmed(tmp_path, state):
    cfg = _cfg(tmp_path)
    _armer(tmp_path, state)
    assert cr.cooling_expected(cfg)[0] is False


def test_cooling_expected_window(tmp_path):
    cfg = _cfg(tmp_path, cool_lead_minutes=30)
    now = datetime(2026, 10, 17, 2, 0)
    dusk, dawn = now + timedelta(hours=1), now + timedelta(hours=10)
    for st in ("ARMED", "RUNNING", "PAUSED_UNSAFE", "PAUSED_OPERATOR"):
        _armer(tmp_path, st, dusk, dawn)
        assert cr.cooling_expected(cfg, now)[0] is False      # before pre-cool
        assert cr.cooling_expected(cfg, dusk - timedelta(minutes=20))[0] is True
        assert cr.cooling_expected(cfg, dawn)[0] is False
    _armer(tmp_path, "RUNNING")                                # no plan times
    assert cr.cooling_expected(cfg, now)[0] is True
    _armer(tmp_path, "ARMED")
    assert cr.cooling_expected(cfg, now)[0] is False
    (tmp_path / "armer_state.json").unlink()
    assert cr.cooling_expected(cfg, now)[0] is False


# ---- the telescope agent's watchdog ----------------------------------------

class FakeNina:
    def __init__(self, cam):
        self.cam = dict(cam)
        self.calls = []

    async def get_camera_info(self):
        return dict(self.cam)

    async def disconnect_camera(self):
        self.calls.append("disconnect")
        return {"Success": True}

    async def connect_camera(self):
        self.calls.append("connect")
        return {"Success": True}

    async def cool_camera(self, temperature, minutes=10.0):
        self.calls.append(("cool", temperature, minutes))
        return {"Success": True}


def _agent(tmp_path, monkeypatch, state, rig="piggyback"):
    from photonscript.telescope_agent import agent as agent_mod
    a = agent_mod.TelescopeAgent.__new__(agent_mod.TelescopeAgent)
    a.config = _cfg(tmp_path)
    a.rig = rig
    a.nina = FakeNina(STALLED)
    a._alerted = set()
    if state is not None:
        _armer(tmp_path, state)
    sent = []

    async def fake_notify(config, msg, **kw):
        sent.append(msg)
    monkeypatch.setattr(agent_mod, "notify", fake_notify)
    return a, sent


def _agent_feed(a, clock, minutes):
    for _ in range(minutes):
        clock.t += 60
        w = getattr(a, "_cool_watch", None)
        if w is not None:
            w.clock, w.sleep = clock, _nosleep
        asyncio.run(a._cooling_watchdog(dict(STALLED)))


@pytest.mark.parametrize("state", ["DISARMED", "COMPLETE", None])
def test_agent_no_action_when_disarmed(tmp_path, monkeypatch, state):
    a, sent = _agent(tmp_path, monkeypatch, state)
    _agent_feed(a, Clock(), 30)
    assert a.nina.calls == [] and sent == []


def test_agent_reconnects_only_its_camera_while_running(tmp_path, monkeypatch):
    a, sent = _agent(tmp_path, monkeypatch, "RUNNING")
    _agent_feed(a, Clock(), 10)
    assert a.nina.calls == ["disconnect", "connect", ("cool", 0.0, 0.0)]


# ---- the capture job and the cooler gate benefit ---------------------------

def test_capture_job_cooling_stall_recovers(tmp_path, monkeypatch):
    """A capture job stalled in "cooling" gets the same reconnect, then
    reaches the setpoint and dispatches instead of aborting."""
    from photonscript.scheduler import calibration_capture as cc
    from photonscript.scheduler import cooler_history as ch
    from tests.test_scheduler.test_ps113_calibration import FakeIO
    from tests.test_scheduler.test_ps122_calibration_owed import _pb_cfg
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cc, "_jobs", {})
    monkeypatch.setattr(cc, "_autofill_done", {})
    monkeypatch.setattr(ch, "_last", {})
    monkeypatch.setattr(cc, "STALL_WINDOW_S", 0.0)
    cfg = _pb_cfg(tmp_path, calibration_cool_stall_min=0.001,
                  cooler_stall_reconnect_wait_s=0.0)

    class StuckIO(FakeIO):
        async def camera(self):
            d = await super().camera()
            d.update(CoolerOn=True, CoolerPower=0.0, IsExposing=False,
                     CameraState="Idle")
            return d

        async def disconnect_camera(self):
            self.calls.append("disconnect_camera")
            self.temps = [0.2]           # the cooler comes back
            return {"ok": True}
    io = StuckIO(cfg, temps=[23.4])

    async def go():
        ok, body = await cc.start_job(cfg, "piggyback", armer_state_fn=lambda: "DISARMED",
                                      io=io, daytime=False, poll_s=0.01,
                                      exposures=[300.0], count=2)
        assert ok, body
        j = cc._jobs["piggyback"]
        await asyncio.wait_for(j.task, 30)
        return j
    j = asyncio.run(go())
    i = io.calls.index("disconnect_camera")
    assert io.calls[i + 1] == "connect_camera" and io.calls[i + 2] == ("cool", 0.0)
    assert "dispatch" in io.calls
    assert any("camera reconnected and cooling again" in x for x in j.events)
    rows = cr.attempts(cfg, "piggyback")
    assert len(rows) == 1 and rows[0]["result"] == "recovered"
    assert rows[0]["source"].startswith("capture job")


def test_gate_extends_after_a_reconnect(tmp_path, monkeypatch):
    monkeypatch.setenv("PS_COOLER_GATE_MODE", "skip")
    from photonscript.scheduler import cooler_gate as cg
    cfg = _cfg(tmp_path, cooler_gate_timeout_min=20, cooler_gate_poll_s=60,
               cooler_stall_gate_extend_min=15)
    clock = Clock()
    t0 = clock.t
    # a reconnect 10 min into the wait; the sensor reaches 0.5 C at 25 min
    cr._record(cfg, "rc16", t0 + 600, {"epoch": t0 + 600, "result": "pending"})

    async def read(_cfg, _rig):
        return 0.5 if clock.t - t0 >= 25 * 60 else 35.0

    async def sleep(s):
        clock.t += s

    async def no_notify(*a, **k):
        return None
    res = asyncio.run(cg.run_gate(cfg, "rc16", 0.0, "M31", read=read, sleep=sleep,
                                  clock=clock, notify_fn=no_notify))
    assert res["verdict"] == "PASS" and res["waited_s"] >= 25 * 60


def test_gate_skips_without_a_reconnect(tmp_path, monkeypatch):
    monkeypatch.setenv("PS_COOLER_GATE_MODE", "skip")
    from photonscript.scheduler import cooler_gate as cg
    cfg = _cfg(tmp_path, cooler_gate_timeout_min=20, cooler_gate_poll_s=60)
    clock = Clock()

    async def read(_cfg, _rig):
        return 35.0

    async def sleep(s):
        clock.t += s

    async def no_notify(*a, **k):
        return None
    res = asyncio.run(cg.run_gate(cfg, "rc16", 0.0, "M31", read=read, sleep=sleep,
                                  clock=clock, notify_fn=no_notify))
    assert res["verdict"] == "SKIP" and res["waited_s"] <= 20 * 60 + 1
