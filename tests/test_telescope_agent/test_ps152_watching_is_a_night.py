"""PS-152: a watched sideloaded night (armer WATCHING, PS-136) is a night in
progress for the telescope agent: the dew heater is still asserted, the
cooling watchdog still alerts, the safety watchdog runs in daylight and the
guide guard does not read it as "no night armed". Paused by the operator
(PS-64) likewise."""
import asyncio
import json

import pytest

from photonscript.shared import pushover
from photonscript.telescope_agent import agent as agent_mod
from tests.test_telescope_agent.test_dew_watchdog_shutdown import WARMING, _agent


@pytest.mark.parametrize("state", ["WATCHING", "PAUSED_OPERATOR"])
def test_dew_heater_asserted(tmp_path, monkeypatch, state):
    a, _ = _agent(tmp_path, monkeypatch, state)
    assert a._cooling_hold() is None
    asyncio.run(a._dew_heater_watchdog(dict(WARMING, Temperature=0.5,
                                            TemperatureSetPoint=0)))
    assert a.nina.calls == [("dew", True)]


@pytest.mark.parametrize("state", ["WATCHING", "PAUSED_OPERATOR"])
def test_cooling_alert_active(tmp_path, monkeypatch, state):
    a, sent = _agent(tmp_path, monkeypatch, state)
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
    assert any("Sensor at 18.0C" in m for m in sent)


@pytest.mark.parametrize("state", ["WATCHING", "PAUSED_OPERATOR"])
def test_safety_watch_and_armer_active_in_daylight(tmp_path, monkeypatch, state):
    a, _ = _agent(tmp_path, monkeypatch, state)
    monkeypatch.setattr(pushover, "sun_altitude_deg", lambda *a_, **k: 30.0)
    assert a._armer_active() is True
    assert a._safety_watch_needed() is True
    (tmp_path / "armer_state.json").write_text(json.dumps({"state": "COMPLETE"}))
    assert a._armer_active() is False
    assert a._safety_watch_needed() is False


def test_scheduler_armer_active_counts_watching(monkeypatch):
    from photonscript.scheduler import app as app_mod

    class _A:
        state = "WATCHING"
    monkeypatch.setattr(app_mod, "_armer", _A())
    assert app_mod._armer_active() is True
    _A.state = "COMPLETE"
    assert app_mod._armer_active() is False


def test_autostart_kill_guard_counts_watching():
    from photonscript.shared import autostart_check as ac
    assert "WATCHING" in ac.ARMER_ACTIVE and "PAUSED_OPERATOR" in ac.ARMER_ACTIVE
