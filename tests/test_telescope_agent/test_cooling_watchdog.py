"""Cooling watchdog: detect 'CoolerOn but 0% power' and cycle the camera."""

import asyncio

import numpy as np
import pytest

from photonscript.shared.config import PhotonScriptConfig


class FakeNina:
    def __init__(self):
        self.calls = []

    async def disconnect_camera(self):
        self.calls.append("disconnect")

    async def connect_camera(self):
        self.calls.append("connect")

    async def cool_camera(self, temperature, minutes=10.0):
        self.calls.append(("cool", temperature))


def _agent(monkeypatch):
    from photonscript.telescope_agent import agent as agent_mod
    cfg = PhotonScriptConfig(_env_file=None, camera_setpoint_c=0.0,
                             cooling_tolerance_c=1.0)
    a = agent_mod.TelescopeAgent.__new__(agent_mod.TelescopeAgent)
    a.config = cfg
    a.nina = FakeNina()
    a._cool_bad_since = None
    a._cool_fix_attempts = 0
    a._alerted = set()
    sent = []

    async def fake_notify(config, msg, **kw):
        sent.append(msg)
    monkeypatch.setattr(agent_mod, "notify", fake_notify)

    async def no_sleep(_):
        pass
    monkeypatch.setattr(agent_mod.asyncio, "sleep", no_sleep)
    return a, sent


BAD = {"CoolerOn": True, "CoolerPower": 0.0, "Temperature": 43.9}
GOOD = {"CoolerOn": True, "CoolerPower": 85.0, "Temperature": 5.0}


def test_watchdog_cycles_camera_after_grace(monkeypatch):
    a, sent = _agent(monkeypatch)
    asyncio.run(a._cooling_watchdog(BAD))       # arms the grace timer
    assert a.nina.calls == []
    a._cool_bad_since -= a.COOL_FAIL_GRACE_S + 1  # grace expired
    asyncio.run(a._cooling_watchdog(BAD))
    assert a.nina.calls == ["disconnect", "connect", ("cool", 0.0)]
    assert a._cool_fix_attempts == 1
    assert sent  # pushover went out


def test_watchdog_ignores_healthy_cooling(monkeypatch):
    a, _ = _agent(monkeypatch)
    asyncio.run(a._cooling_watchdog(GOOD))      # cooling hard, far from setpoint
    a._cool_bad_since = None
    asyncio.run(a._cooling_watchdog({"CoolerOn": True, "CoolerPower": 30.0,
                                     "Temperature": 0.4}))
    assert a.nina.calls == []
    assert a._cool_fix_attempts == 0


def test_watchdog_gives_up_and_escalates(monkeypatch):
    a, sent = _agent(monkeypatch)
    a._cool_fix_attempts = a.COOL_FIX_MAX
    a._cool_bad_since = -1e9
    asyncio.run(a._cooling_watchdog(BAD))
    assert a.nina.calls == []  # no more cycling
    assert "cooling-dead" in a._alerted
