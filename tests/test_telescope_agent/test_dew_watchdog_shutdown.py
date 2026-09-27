"""PS-79: after the dawn shutdown the dew-heater and cooling watchdogs leave
the camera alone until the next arm.

2026-09-27: the dawn shutdown turned the dew heater off at 06:18; NINA's
WarmCamera kept the TEC on while warming, so at 06:33 the watchdog saw
"cooler on" and turned the heater back ON, and at 06:38 the nanny paged
"sensor 18.0C with cooler on for 20 min, setpoint 0C"."""
import asyncio
import json

import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import TelescopeState
from photonscript.telescope_agent import agent as agent_mod

WARMING = {"CoolerOn": True, "CoolerPower": 0.0, "Temperature": 18.0,
           "TemperatureSetPoint": 20, "HasDewHeater": True, "DewHeaterOn": False}


class FakeNina:
    def __init__(self, cam=None):
        self.calls = []
        self.cam = cam or WARMING

    async def set_dew_heater(self, power):
        self.calls.append(("dew", power))

    async def disconnect_camera(self):
        self.calls.append("disconnect")

    async def connect_camera(self):
        self.calls.append("connect")

    async def cool_camera(self, temperature, minutes=10.0):
        self.calls.append(("cool", temperature))

    async def get_camera_info(self):
        return self.cam

    async def get_mount_info(self):
        return {"RightAscension": 1.0, "Declination": 2.0, "Tracking": False}

    async def get_focuser_info(self):
        return {"Position": 1}

    async def get_sequence_status(self):
        return {"State": "IDLE", "CurrentTarget": None}


def _agent(tmp_path, monkeypatch, state):
    a = agent_mod.TelescopeAgent.__new__(agent_mod.TelescopeAgent)
    a.config = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                                  camera_setpoint_c=0.0, cooling_tolerance_c=1.0)
    a.rig = "rc16"
    a.nina = FakeNina()
    a.state = TelescopeState()
    a._alerted = set()
    a._cool_bad_since = None
    a._cool_fix_attempts = 0
    a._dew_last_set = 0.0
    a._dew_api_broken = False
    if state is not None:
        (tmp_path / "armer_state.json").write_text(json.dumps({"state": state}))
    sent = []

    async def fake_notify(config, msg, **kw):
        sent.append(msg)
    monkeypatch.setattr(agent_mod, "notify", fake_notify)

    async def no_sleep(_):
        pass
    monkeypatch.setattr(agent_mod.asyncio, "sleep", no_sleep)
    return a, sent


@pytest.mark.parametrize("state", ["COMPLETE", "DISARMED", "ERROR"])
def test_dew_heater_stays_off_after_shutdown(tmp_path, monkeypatch, state):
    a, _ = _agent(tmp_path, monkeypatch, state)
    asyncio.run(a._dew_heater_watchdog(WARMING))
    assert a.nina.calls == []
    assert "shut down until the next arm" in a._cooling_hold()


@pytest.mark.parametrize("state", ["ARMED", "RUNNING", "PAUSED_UNSAFE", None])
def test_dew_heater_still_asserted_while_armed(tmp_path, monkeypatch, state):
    a, _ = _agent(tmp_path, monkeypatch, state)
    asyncio.run(a._dew_heater_watchdog(dict(WARMING, Temperature=0.5,
                                            TemperatureSetPoint=0)))
    assert a.nina.calls == [("dew", True)]


def test_next_arm_releases_the_hold(tmp_path, monkeypatch):
    a, _ = _agent(tmp_path, monkeypatch, "COMPLETE")
    asyncio.run(a._dew_heater_watchdog(WARMING))
    assert a.nina.calls == []
    (tmp_path / "armer_state.json").write_text(json.dumps({"state": "ARMED"}))
    asyncio.run(a._dew_heater_watchdog(WARMING))
    assert a.nina.calls == [("dew", True)]


def test_warm_after_shutdown_is_not_a_dead_cooler(tmp_path, monkeypatch):
    """TEC on, ~0% power, sensor well above setpoint = the dead-cooler
    signature, but after the shutdown it is NINA warming: never reconnect
    and re-cool the camera."""
    a, sent = _agent(tmp_path, monkeypatch, "COMPLETE")
    for _ in range(3):
        asyncio.run(a._cooling_watchdog(WARMING))
        assert a._cool_bad_since is None       # the grace clock never starts
    assert a.nina.calls == [] and sent == []


def test_no_cooling_page_while_warming_after_shutdown(tmp_path, monkeypatch):
    a, sent = _agent(tmp_path, monkeypatch, "COMPLETE")
    clock = [1000.0]
    import time as _time
    monkeypatch.setattr(_time, "monotonic", lambda: clock[0])

    async def one_poll():
        a._running = True

        async def stop(_):
            a._running = False
        monkeypatch.setattr(agent_mod.asyncio, "sleep", stop)
        await a._nina_poll_loop()

    for _ in range(3):
        asyncio.run(one_poll())
        clock[0] += 900
    assert not any("Sensor at" in m for m in sent)
    assert a.nina.calls == []
    # same readings with a night in progress: the page still fires
    (tmp_path / "armer_state.json").write_text(json.dumps({"state": "RUNNING"}))
    for _ in range(3):
        asyncio.run(one_poll())
        clock[0] += 900
    assert any("Sensor at 18.0C" in m for m in sent)
