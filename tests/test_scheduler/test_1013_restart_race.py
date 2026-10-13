"""2026-10-13 restart race: POST /api/arm/restart {"when": "after_exposure"}
answered ok, then the armer went PAUSED_OPERATOR with "Restart FAILED:
re-dispatch failed (sequence_start: Sequence is already running)". The
service log shows sequence_load and sequence_start both refused at 22:29:33
local; NINA #1 logged SEQUENCE-FINISHED at 22:29:34. ninaAPI's stop only
asks NINA to stop, so the re-dispatch must wait for NINA #1 to read not
running (bounded), and retry a load/start that still says "already
running" (stop again, backoff) before failing.

A fake NINA #1 that keeps its sequence RUNNING for a few state polls after
the stop drives _dispatch_and_start, Resume and Restart tonight from now.
"""
from datetime import datetime, timedelta

import pytest

from photonscript.scheduler import armer as armer_mod
from photonscript.scheduler.armer import PAUSE_STATE
from tests.test_scheduler.test_ps181_arm_recipe import armer  # noqa: F401 - fixture

IDLE = {"IsExposing": False, "CameraState": "Idle"}
BUSY = "Sequence is already running"


class FakeNina1:
    """ninaAPI on NINA #1. After each /sequence/stop the sequence keeps
    RUNNING for `wind_down` state polls; load/start answer "already
    running" while it runs (and `lie` more times after it reads idle)."""

    def __init__(self, a, wind_down=0, lie=0, unreadable=False):
        self.a, self.wind_down, self.lie = a, wind_down, lie
        self.unreadable = unreadable
        self.left = 0          # state polls still RUNNING
        self.loaded = False

    def _err(self, key):
        self.a.detail = f"{key}: {BUSY}"
        return None

    async def __call__(self, key, method="GET", json_body=None, **kw):
        a = self.a
        a.calls.append(key)
        if key == "camera_info":
            return {"Response": IDLE}
        if key == "sequence_stop":
            if self.left == 0 and self.wind_down:
                self.left = self.wind_down
                self.wind_down = 0
            return {"Success": True}
        if key == "sequence_state":
            if self.unreadable:
                a.detail = "sequence_state: ConnectError"
                return None
            if self.left > 0:
                self.left -= 1
                return {"Response": [{"Name": "Targets_Container",
                                      "Status": "RUNNING"}]}
            return {"Response": [{"Name": "Targets_Container",
                                  "Status": "FINISHED"}]}
        if key in ("sequence_load", "sequence_start"):
            if self.left > 0:
                return self._err(key)
            if self.lie > 0:
                self.lie -= 1
                return self._err(key)
            if key == "sequence_load":
                self.loaded = True
            return {"Success": True}
        if key == "safety":
            return {"Response": {"Connected": True, "IsSafe": True}}
        return {"Success": True}


@pytest.fixture
def race(armer, monkeypatch):  # noqa: F811
    a = armer
    a.sleeps = []

    async def _sleep(s):
        a.sleeps.append(s)
    monkeypatch.setattr(armer_mod, "_armer_sleep", _sleep)
    # dark left for a restart / resume (they read the real clock)
    now = datetime.utcnow()
    a.plan = {**a.plan, "night_of": "2026-10-12",
              "dusk_utc": (now - timedelta(hours=2)).isoformat() + "Z",
              "dawn_utc": (now + timedelta(hours=6)).isoformat() + "Z"}

    def use(**kw):
        fake = FakeNina1(a, **kw)
        monkeypatch.setattr(a, "_nina", fake)
        return fake
    a.use = use
    return a


def _polls(a):
    return a.calls.count("sequence_state")


async def test_dispatch_waits_for_nina1_to_wind_down_before_loading(race):
    a = race
    a.use(wind_down=4)
    assert await a._dispatch_and_start(companion=False, fail_state=None) is True
    # stop, 4 RUNNING polls + 1 idle poll, THEN load and start (once each)
    assert a.calls[0] == "sequence_stop"
    assert _polls(a) == 5
    first_load = a.calls.index("sequence_load")
    assert first_load > max(i for i, k in enumerate(a.calls) if k == "sequence_state")
    assert a.calls.count("sequence_load") == 1 and a.calls.count("sequence_start") == 1
    assert a.sleeps == [armer_mod.STOP_IDLE_POLL_S] * 4


async def test_idle_nina1_costs_one_state_read(race):
    a = race
    a.use()
    assert await a._dispatch_and_start(companion=False, fail_state=None) is True
    assert a.calls[:4] == ["sequence_stop", "sequence_state", "sequence_load",
                           "sequence_start"]
    assert a.sleeps == []


async def test_stop_is_resent_while_nina1_keeps_running(race):
    a = race
    every = int(armer_mod.STOP_RESEND_S / armer_mod.STOP_IDLE_POLL_S)
    a.use(wind_down=every + 2)
    assert await a._dispatch_and_start(companion=False, fail_state=None) is True
    stops = a.calls[:a.calls.index("sequence_load")].count("sequence_stop")
    assert stops == 2   # the dispatch's stop + one re-send


async def test_wait_is_bounded_by_poll_count(race):
    a = race
    a.use().left = 10_000   # never winds down
    res = await a._wait_nina1_stopped(max_wait_s=30.0, poll_s=3.0)
    assert res is False
    assert _polls(a) == 11 and len(a.sleeps) == 10


async def test_unreadable_state_goes_on_after_a_few_polls(race):
    a = race
    a.use(unreadable=True)
    a.detail = "before"
    assert await a._wait_nina1_stopped() is None
    assert _polls(a) == armer_mod.STOP_UNREADABLE_POLLS
    assert a.detail == "before"   # a state read never clobbers the detail


async def test_no_sequence_loaded_reads_idle(race, monkeypatch):
    a = race

    async def _nina(key, **kw):
        a.calls.append(key)
        if key == "sequence_state":
            a.detail = "sequence_state: No sequence initialized"
            return None
        return {"Success": True}
    monkeypatch.setattr(a, "_nina", _nina)
    assert await a._nina1_running() == []


async def test_already_running_load_is_stopped_and_retried(race):
    """NINA reads idle but load still says already running twice: two
    stop + backoff retries, then the dispatch goes through."""
    a = race
    a.use(lie=2)
    assert await a._dispatch_and_start(companion=False, fail_state=None) is True
    assert a.calls.count("sequence_load") == 3
    assert a.calls.count("sequence_start") == 1   # never started after a failed load
    backoff = list(armer_mod.DISPATCH_BUSY_BACKOFF_S[:2])
    assert [s for s in a.sleeps if s in backoff] == backoff


async def test_already_running_gives_up_after_the_retries(race):
    a = race
    a.use(lie=99)
    assert await a._dispatch_and_start(companion=False, fail_state=None) is False
    assert a.calls.count("sequence_load") == len(armer_mod.DISPATCH_BUSY_BACKOFF_S) + 1
    assert a.calls.count("sequence_start") == 0
    assert BUSY in a.detail
    assert any("Dispatch failed" in m for m, _p in a.pushes)


async def test_other_load_errors_are_not_retried(race, monkeypatch):
    a = race
    fake = a.use()

    async def _nina(key, **kw):
        if key == "sequence_load":
            a.calls.append(key)
            a.detail = "sequence_load: HTTP 400 bad sequence"
            return None
        return await fake(key, **kw)
    monkeypatch.setattr(a, "_nina", _nina)
    assert await a._dispatch_and_start(companion=False, fail_state=None) is False
    assert a.calls.count("sequence_load") == 1
    assert a.calls.count("sequence_start") == 0


async def test_restart_after_exposure_replays_10_13(race):
    """The 10-13 shape: RUNNING, Restart after the current sub, NINA #1
    still winding down for a few polls after the stop: RUNNING again."""
    a = race
    a.state = "RUNNING"
    a.use(wind_down=3)
    res = await a.restart(when="after_exposure")
    assert res["ok"] and res.get("pending")
    await a._pause_task
    assert a.state == "RUNNING", a.detail
    assert "Restarted tonight from now" in a.detail
    assert a.last_restart["ok"] is True
    assert not any("Restart FAILED" in m for m, _p in a.pushes)


async def test_resume_waits_for_the_wind_down_too(race):
    a = race
    a.state = "RUNNING"
    fake = a.use()
    res = await a.pause()
    assert res["ok"]
    await a._pause_task
    assert a.state == PAUSE_STATE
    fake.left = 2          # NINA #1 still finishing when Resume is pressed
    res = await a.resume()
    assert res["ok"] and res["redispatched"], res
    assert a.state == "RUNNING"
