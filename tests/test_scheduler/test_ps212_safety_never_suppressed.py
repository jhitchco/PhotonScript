"""PS-212 follow-up (2026-11-02): safety alerts are never suppressed, held
or batched.

A safety act (unsafe weather with the rig not parked or still imaging, roof
gating blind, a park failure, a cooling fault) must go out at once, at any
hour, whatever the storyline policy and the limiter would do to any other
push. These tests put every gate in its most hostile state at the same time
(pushover_kinds off, after the midnight cutoff, the plan heads-up window
open, the fact already told tonight, the sun up with the same title just
sent, the hourly cap full, the cap-exempt budget spent, the monthly cap
reached) and check each safety act is still sent on the spot, while the same
push without safety=True is held (so the test bites). Then the call sites:
cooling faults and a SAFETY STOP carry safety=True.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from photonscript.shared import pushover as po
from photonscript.shared.config import PhotonScriptConfig


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def sent(monkeypatch):
    out: list[dict] = []

    async def fake(config, message, title, priority, sound, **kw):
        out.append({"title": title, "message": message, "priority": priority})
        return True
    monkeypatch.setattr(po, "_send_raw", fake)
    return out


def _cfg(tmp_path, **kw):
    base = dict(_env_file=None, data_dir=str(tmp_path),
                pushover_user_key="u-test", pushover_api_token="t-test")
    base.update(kw)
    return PhotonScriptConfig(**base)


def _hostile(tmp_path, monkeypatch):
    """Every gate shut: kinds off, after the cutoff, plan window open, the sun
    up, the hourly cap 0, the monthly cap reached (and already announced)."""
    cfg = _cfg(tmp_path, pushover_kinds="", pushover_max_per_hour=0,
               pushover_monthly_cap=0, pushover_cap_exempt_max_per_hour=0,
               pushover_quiet_daytime=True)
    monkeypatch.setattr(po, "act_held_now", lambda c, now_utc=None: True)
    monkeypatch.setattr(po, "plan_window_open", lambda c, now_utc=None: True)
    monkeypatch.setattr(po, "_is_daytime", lambda c, when=None: True)
    po._save_state(po._state_path(cfg), {
        "month": time.strftime("%Y-%m", time.gmtime()), "month_count": 5,
        "month_capped": True, "hour_ts": [time.time()] * 50})
    return cfg


# The safety alerts by family: (title, message) as their call sites send them.
SAFETY_ALERTS = [
    # unsafe weather / roof
    ("PhotonScript safety", "Safety monitor UNREADABLE for ~3 min: roof gating is blind."),
    ("PhotonScript SAFETY STOP", "SAFETY STOP: unsafe for 4 min but NINA was still imaging."),
    ("PhotonScript watching", "Unsafe 6 min but the sideloaded sequence is still imaging."),
    # park failures
    ("PhotonScript paused", "Unsafe while paused: mount park FAILED."),
    ("PhotonScript mount after dawn", "Mount not parked 40 min after dawn."),
    # cooling faults
    ("PhotonScript cooler", "Cooler nanny: cool command to rc16 FAILED. Subs are being shot warm."),
    ("PhotonScript cooler gate", "Piggy-600: not imaging darks, sensor at 35.0 C vs setpoint 0 C."),
    ("PhotonScript cooler recovery", "Piggy-600: cooler STILL stalled after camera reconnect 1/2."),
]


@pytest.mark.parametrize("title,message", SAFETY_ALERTS)
def test_a_safety_act_goes_out_with_every_gate_shut(tmp_path, monkeypatch, sent,
                                                    title, message):
    cfg = _hostile(tmp_path, monkeypatch)
    # the same title earlier today (daytime window) and tonight (fact told)
    po.mark_facts(cfg, [po.fact_key("act", title, "earlier")])
    st = po._load_state(po._state_path(cfg))
    st["day_titles"] = {title: time.time()}
    po._save_state(po._state_path(cfg), st)

    assert run(po.notify(cfg, message, title=title, priority=1,
                         kind="act", safety=True)) is True
    assert len(sent) == 1 and sent[0]["title"] == title
    assert sent[0]["priority"] >= 1
    assert sent[0]["message"].startswith(message.rstrip("."))
    # a second, different safety push of the same title: also at once
    assert run(po.notify(cfg, message + " Still.", title=title, priority=1,
                         kind="act", safety=True)) is True
    assert len(sent) == 2
    story = [json.loads(x) for x in po.story_path(cfg, po.night_of(cfg))
             .read_text().splitlines()]
    assert [e["reason"] for e in story] == ["sent", "sent"]
    assert all(e["safety"] for e in story)


@pytest.mark.parametrize("title,message", SAFETY_ALERTS)
def test_without_safety_the_same_push_is_held(tmp_path, monkeypatch, sent,
                                             title, message):
    """Control: the hostile gates really do stop a non-safety act."""
    cfg = _hostile(tmp_path, monkeypatch)
    assert run(po.notify(cfg, message, title=title, priority=1, kind="act")) is False
    assert sent == []


def test_safety_acts_are_never_batched_into_the_plan_or_held_to_morning(
        tmp_path, monkeypatch, sent):
    """Ten different safety acts in a burst: ten pushes, in order, nothing
    folded into the plan heads-up or held for the end-of-night report."""
    cfg = _hostile(tmp_path, monkeypatch)
    for i in range(10):
        assert run(po.notify(cfg, f"Unsafe while paused: mount park FAILED (try {'x' * i}).",
                             title="PhotonScript paused", priority=1,
                             kind="act", safety=True)) is True
    assert len(sent) == 10
    held = [json.loads(x) for x in po.story_path(cfg, po.night_of(cfg))
            .read_text().splitlines() if not json.loads(x)["sent"]]
    assert held == []


def test_only_a_byte_identical_repeat_already_delivered_is_not_resent(
        tmp_path, monkeypatch, sent):
    """The one exception: the very same title and text, already delivered
    inside pushover_dedup_window_s, is not sent twice (it is on the phone).
    Different text goes; the same text goes again after the window."""
    cfg = _hostile(tmp_path, monkeypatch)
    clock = [time.time()]
    monkeypatch.setattr(po.time, "time", lambda: clock[0])
    kw = dict(title="PhotonScript safety", priority=1, kind="act", safety=True)
    assert run(po.notify(cfg, "Safety monitor UNREADABLE for ~3 min.", **kw)) is True
    assert run(po.notify(cfg, "Safety monitor UNREADABLE for ~3 min.", **kw)) is False
    assert run(po.notify(cfg, "Safety monitor UNREADABLE for ~6 min.", **kw)) is True
    clock[0] += 301
    assert run(po.notify(cfg, "Safety monitor UNREADABLE for ~3 min.", **kw)) is True
    assert len(sent) == 3


def test_a_failed_safety_send_is_retried_once(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    calls = []

    async def flaky(config, message, title, priority, sound, **kw):
        calls.append(title)
        return len(calls) > 1
    slept = []

    async def nosleep(s):
        slept.append(s)
    monkeypatch.setattr(po, "_send_raw", flaky)
    monkeypatch.setattr(po.asyncio, "sleep", nosleep)
    monkeypatch.setattr(po, "_is_daytime", lambda c, when=None: False)
    assert run(po.notify(cfg, "Mount not parked after dawn.",
                         title="PhotonScript mount after dawn", priority=1,
                         kind="act", safety=True)) is True
    assert len(calls) == 2 and slept == [po.SAFETY_RETRY_S]
    # a non-safety act is not retried
    calls.clear()
    assert run(po.notify(cfg, "Settings wrong.", title="PhotonScript settings",
                         priority=1, kind="act")) is False
    assert len(calls) == 1


def test_a_limiter_error_still_sends_a_safety_act(tmp_path, monkeypatch, sent):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(po, "_is_daytime", lambda c, when=None: False)

    def boom(path):
        raise RuntimeError("state unreadable")
    monkeypatch.setattr(po, "_load_state", boom)
    assert run(po.notify(cfg, "SAFETY STOP: park FAILED.",
                         title="PhotonScript SAFETY STOP", priority=1,
                         kind="act", safety=True)) is True
    assert len(sent) == 1


def test_gate_lets_a_safety_act_past_every_cap():
    now = time.time()
    month = time.strftime("%Y-%m", time.gmtime(now))
    full = {"month": month, "month_count": 99, "month_capped": True,
            "hour_ts": [now] * 99}
    args = (now, month, "k", 1, 300, 20, 50)
    assert po._gate(dict(full), *args)[:2] == (False, "monthly-capped")
    assert po._gate(dict(full), *args, safety=True)[:2] == (True, "ok")
    hourly = {"month": month, "month_count": 0, "hour_ts": [now] * 99}
    assert po._gate(dict(hourly), *args)[:2] == (False, "hourly-cap")
    assert po._gate(dict(hourly), *args, safety=True)[:2] == (True, "ok")
    ex = {"month": month, "month_count": 0, "exempt_ts": {"x": [now] * 5}}
    assert po._gate(dict(ex), *args, exempt_key="x")[:2] == (False, "exempt-cap")
    assert po._gate(dict(ex), *args, exempt_key="x", safety=True)[:2] == (True, "ok")


# ---------------------------------------------------------------------------
# call sites: cooling faults and the SAFETY STOP are safety acts
# ---------------------------------------------------------------------------

def _cooler_watch(cfg, port, clock, notes):
    from photonscript.shared import cooler_recovery as cr

    async def notify_fn(config, msg, **kw):
        notes.append((msg, kw))

    async def nosleep(_s):
        return None
    return cr.CoolerStallWatch(cfg, "piggyback", port, setpoint=0.0,
                               cool_minutes=0.0, clock=clock, sleep=nosleep,
                               notify_fn=notify_fn)


class _Port:
    def __init__(self, state, fail=False):
        self.state, self.fail = dict(state), fail

    async def camera(self):
        return dict(self.state)

    async def disconnect(self):
        return None

    async def connect(self):
        if self.fail:
            raise RuntimeError("camera not found")

    async def cool(self, setpoint, minutes):
        return None


class _Clock:
    def __init__(self):
        self.t = 1_760_000_000.0

    def __call__(self):
        return self.t


STALLED = {"CoolerOn": True, "CoolerPower": 0.0, "Temperature": 23.4,
           "IsExposing": False, "CameraState": "Idle"}


def _feed(w, clock, minutes):
    for _ in range(minutes):
        clock.t += 60
        asyncio.run(w.tick(dict(STALLED), True))


def _cooler_cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                              camera_setpoint_c=0.0, cooling_tolerance_c=1.0,
                              cooler_stall_reconnect_wait_s=0.0,
                              observatory_tz="UTC")


def test_every_cooler_stall_verdict_that_is_not_recovered_is_a_safety_act(tmp_path):
    cfg = _cooler_cfg(tmp_path)
    clock, notes = _Clock(), []
    w = _cooler_watch(cfg, _Port(STALLED), clock, notes)
    _feed(w, clock, 9 + 8)            # attempt 1 and its verdict
    assert notes and "STILL stalled" in notes[0][0]
    assert notes[0][1]["kind"] == "act" and notes[0][1]["safety"] is True
    _feed(w, clock, 5 + 3 + 8)        # spacing, attempt 2, its verdict
    assert "no more reconnects" in notes[-1][0]
    assert all(kw["kind"] == "act" and kw["safety"] is True for _m, kw in notes)


def test_a_cooler_reconnect_error_is_a_safety_act(tmp_path):
    cfg = _cooler_cfg(tmp_path)
    clock, notes = _Clock(), []
    w = _cooler_watch(cfg, _Port(STALLED, fail=True), clock, notes)
    _feed(w, clock, 9)
    assert len(notes) == 1 and "errored" in notes[0][0]
    assert notes[0][1]["kind"] == "act" and notes[0][1]["safety"] is True


def test_a_skipped_cooler_gate_block_is_a_safety_act(tmp_path):
    from photonscript.scheduler import cooler_gate as cg
    cg.LIVE.clear()
    cg._EPISODE.clear()
    cfg = PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                             cooler_gate_mode="skip", cooler_gate_timeout_min=20,
                             cooler_gate_poll_s=15)
    clock, notes = _Clock(), []

    async def read(_cfg, _rig):
        return 12.4

    async def sleep(s):
        clock.t += s

    async def notify(_cfg, msg, **kw):
        notes.append(kw)
    res = asyncio.run(cg.run_gate(cfg, "rc16", 0.0, "M 31 Ha", read=read,
                                  sleep=sleep, clock=clock, notify_fn=notify))
    assert res["verdict"] == "SKIP"
    assert notes and notes[0]["kind"] == "act" and notes[0]["safety"] is True


@pytest.mark.asyncio
async def test_safety_stop_is_a_safety_act_even_when_the_park_was_ok(tmp_path, monkeypatch):
    from datetime import datetime, timedelta

    import photonscript.scheduler.armer as armer_mod
    from photonscript.scheduler.armer import Armer
    a = Armer(PhotonScriptConfig(data_dir=str(tmp_path)))
    a.state = "PAUSED_UNSAFE"
    a.plan = {"dawn_utc": "2026-09-27T11:48:14Z", "dusk_utc": "2026-09-27T02:26:50Z",
              "night_of": "2026-09-26"}
    tree = [{"GlobalTriggers": []},
            {"Name": "Targets_Container", "Status": "RUNNING", "Items": [
                {"Name": "LOOP_ALL_NIGHT_Container", "Status": "RUNNING",
                 "Items": [{"Name": "SAFE_LOOP_Container", "Status": "RUNNING",
                            "Items": []},
                           {"Name": "UNSAFE_Container", "Status": "CREATED",
                            "Items": []}]}]}]
    notes = []

    async def fake_nina(key, method="GET", json_body=None, **kw):
        if key == "safety":
            return {"Response": {"Connected": True, "IsSafe": False}}
        if key == "sequence_json":
            return {"Response": tree}
        return {"Response": "ok"}            # every step ok: "park ok"

    async def fake_notify(cfg_, msg, **kw):
        notes.append((msg, kw))
    a._nina = fake_nina
    monkeypatch.setattr(armer_mod, "notify", fake_notify)
    t0 = datetime(2026, 9, 27, 11, 39, 56)
    a._unsafe_since = t0
    await a._maybe_stop_stuck_imaging(t0 + timedelta(seconds=121))
    stop = [(m, kw) for m, kw in notes if kw.get("title") == "PhotonScript SAFETY STOP"]
    assert stop and "park ok" in stop[0][0]
    assert stop[0][1]["kind"] == "act" and stop[0][1]["safety"] is True
