"""Safety-monitor watchdog: keep reconnecting a flaky Alpaca safety monitor.

Regression guard for 2026-09-13: the AARO Alpaca safety monitor dropped mid-night
and nothing reattempted it. The watchdog must keep retrying at a steady cadence
(not give up / back off to 30-min gaps) and cycle disconnect->connect to clear a
wedged ASCOM handle.
"""

import asyncio

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import TelescopeState


class FakeNina:
    def __init__(self, connected=False, reconnect_ok=False):
        self.connected = connected
        self.reconnect_ok = reconnect_ok
        self.calls = []

    device_id = ""
    fail_ids = ()

    async def get_safety_info(self):
        info = {"Connected": self.connected}
        if self.connected and self.device_id:
            info["DeviceId"] = self.device_id
        return info

    async def connect_safety(self, device_id=None):
        self.calls.append(f"connect:{device_id}" if device_id else "connect")
        if device_id and device_id in self.fail_ids:
            raise RuntimeError("no such device")
        if self.reconnect_ok:
            self.connected = True

    async def disconnect_safety(self):
        self.calls.append("disconnect")

    async def stop_sequence(self):
        self.calls.append("stop")


def _agent(monkeypatch, nina):
    from photonscript.telescope_agent import agent as agent_mod
    cfg = PhotonScriptConfig(_env_file=None, safety_disconnect_aborts=False,
                             auto_abort_on_severe=False)
    a = agent_mod.TelescopeAgent.__new__(agent_mod.TelescopeAgent)
    a.config = cfg
    a.nina = nina
    a.state = TelescopeState()
    a._safety_bad_since = None
    a._safety_bad_reads = 0
    a._safety_fix_attempts = 0
    a._safety_last_attempt = 0.0
    a._safety_last_escalate = 0.0
    a._safety_aborted = False
    a._alerted = set()
    sent = []

    async def fake_notify(config, msg, **kw):
        sent.append(msg)
    monkeypatch.setattr(agent_mod, "notify", fake_notify)

    async def no_sleep(_):
        pass
    monkeypatch.setattr(agent_mod.asyncio, "sleep", no_sleep)
    # Night / armed by default so these tests don't depend on the wall clock;
    # the idle gate has its own tests below.
    monkeypatch.setattr(agent_mod.TelescopeAgent, "_safety_watch_needed",
                        lambda self: True)
    return a, sent


def test_grace_then_reconnect(monkeypatch):
    n = FakeNina(connected=False)
    a, _ = _agent(monkeypatch, n)
    asyncio.run(a._safety_monitor_watchdog())          # 1st bad read: debounce only
    assert n.calls == []
    assert a._safety_bad_since is None                 # one blip must NOT arm
    asyncio.run(a._safety_monitor_watchdog())          # 2nd bad read: arm grace
    assert n.calls == []
    assert a._safety_bad_since is not None
    a._safety_bad_since -= a.SAFETY_GRACE_S + 1         # grace expired
    asyncio.run(a._safety_monitor_watchdog())
    assert "connect" in n.calls
    assert a._safety_fix_attempts == 1


def test_reconnect_success_clears_state(monkeypatch):
    n = FakeNina(connected=False, reconnect_ok=True)
    a, _ = _agent(monkeypatch, n)
    asyncio.run(a._safety_monitor_watchdog())          # 1st bad read: debounce only
    asyncio.run(a._safety_monitor_watchdog())          # 2nd bad read: arm grace
    a._safety_bad_since -= a.SAFETY_GRACE_S + 1
    asyncio.run(a._safety_monitor_watchdog())
    assert n.connected is True
    assert a._safety_bad_since is None
    assert a._safety_fix_attempts == 0


def test_keeps_retrying_after_fast_burst(monkeypatch):
    # the core fix: past the fast burst it must STILL retry (steady 60s), and
    # cycle disconnect->connect rather than a bare connect on a wedged handle.
    n = FakeNina(connected=False)
    a, _ = _agent(monkeypatch, n)
    a._safety_bad_since = -1e9
    a._safety_bad_reads = 2                             # debounce already satisfied
    a._safety_last_escalate = -1e9
    a._safety_fix_attempts = a.SAFETY_FAST_ATTEMPTS     # burst exhausted
    a._safety_last_attempt = -1e9                       # retry interval elapsed
    asyncio.run(a._safety_monitor_watchdog())
    assert a._safety_fix_attempts == a.SAFETY_FAST_ATTEMPTS + 1   # did not give up
    assert "disconnect" in n.calls and "connect" in n.calls


def test_connected_is_noop(monkeypatch):
    n = FakeNina(connected=True)
    a, _ = _agent(monkeypatch, n)
    asyncio.run(a._safety_monitor_watchdog())
    assert n.calls == []
    assert a._safety_bad_since is None


def test_safety_disconnect_repeat_is_hourly_by_default():
    """PS-50: repeat the DISCONNECTED push hourly, not every 30 min."""
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.telescope_agent.agent import TelescopeAgent
    assert TelescopeAgent.SAFETY_SLOW_RETRY_S == 3600
    assert PhotonScriptConfig(_env_file=None).safety_disconnect_repeat_min == 60


# --- 2026-09-26: correct endpoint, named device, idle when not needed --------

def _down(a):
    """Put the watchdog past debounce + grace with a retry due."""
    a._safety_bad_since = -1e9
    a._safety_bad_reads = 2
    a._safety_last_escalate = -1e9
    a._safety_last_attempt = -1e9


def test_reconnect_names_the_device_last_seen(monkeypatch):
    n = FakeNina(connected=True)
    n.device_id = "ASCOM.AlpacaDynamic4.SafetyMonitor"
    a, _ = _agent(monkeypatch, n)
    asyncio.run(a._safety_monitor_watchdog())            # learns the Id
    assert a._safety_device_id == "ASCOM.AlpacaDynamic4.SafetyMonitor"
    n.connected = False
    _down(a)
    asyncio.run(a._safety_monitor_watchdog())
    assert n.calls == ["connect:ASCOM.AlpacaDynamic4.SafetyMonitor"]


def test_configured_pin_wins_and_falls_back(monkeypatch):
    n = FakeNina(connected=False)
    n.fail_ids = ("ASCOM.Gone.SafetyMonitor",)
    a, _ = _agent(monkeypatch, n)
    a.config = a.config.model_copy(
        update={"safety_monitor_device_id": "ASCOM.Gone.SafetyMonitor"})
    a._safety_device_id = "ASCOM.AlpacaDynamic3.SafetyMonitor"
    _down(a)
    asyncio.run(a._safety_monitor_watchdog())
    assert n.calls == ["connect:ASCOM.Gone.SafetyMonitor", "connect"]


def test_idle_in_daylight_when_not_armed(monkeypatch):
    from photonscript.telescope_agent import agent as agent_mod
    n = FakeNina(connected=False)
    a, sent = _agent(monkeypatch, n)
    monkeypatch.setattr(agent_mod.TelescopeAgent, "_safety_watch_needed",
                        lambda self: False)
    _down(a)
    asyncio.run(a._safety_monitor_watchdog())
    assert n.calls == [] and sent == []
    assert a._safety_bad_since is None and a._safety_idle is True


def test_watch_needed_follows_armer_and_sun(tmp_path, monkeypatch):
    import json
    from datetime import datetime, timezone
    from photonscript.shared import pushover
    from photonscript.telescope_agent import agent as agent_mod
    a = agent_mod.TelescopeAgent.__new__(agent_mod.TelescopeAgent)
    a.config = PhotonScriptConfig(_env_file=None, data_dir=tmp_path)
    monkeypatch.setattr(pushover, "sun_altitude_deg", lambda *a_, **k: 30.0)
    assert a._safety_watch_needed() is False              # day, unarmed
    (tmp_path / "armer_state.json").write_text(json.dumps({"state": "ARMED"}))
    assert a._safety_watch_needed() is True               # day, armed
    (tmp_path / "armer_state.json").write_text(json.dumps({"state": "IDLE"}))
    monkeypatch.setattr(pushover, "sun_altitude_deg", lambda *a_, **k: -10.0)
    assert a._safety_watch_needed() is True               # night, unarmed


def test_armer_active_states_in_sync():
    from photonscript.scheduler.armer import ACTIVE_STATES
    from photonscript.telescope_agent.agent import TelescopeAgent
    assert tuple(TelescopeAgent._ARMER_ACTIVE_STATES) == tuple(ACTIVE_STATES)


def test_nina_client_uses_v2_info_endpoint_and_to_param():
    import httpx
    from photonscript.telescope_agent.nina_client import NinaClient
    seen = []

    def handler(request):
        seen.append((request.url.path, dict(request.url.params)))
        if request.url.path.endswith("/equipment/safetymonitor/info"):
            return httpx.Response(200, json={"Response": {
                "Connected": True, "IsSafe": False,
                "DeviceId": "ASCOM.AlpacaDynamic3.SafetyMonitor"},
                "Success": True, "StatusCode": 200})
        if request.url.path.endswith("/equipment/safetymonitor/connect"):
            return httpx.Response(200, json={"Response": "Connected", "Success": True})
        return httpx.Response(404)

    async def go():
        c = NinaClient("http://nina/v2/api")
        c._client = httpx.AsyncClient(base_url=c.base_url,
                                      transport=httpx.MockTransport(handler))
        info = await c.get_safety_info()
        await c.connect_safety("ASCOM.AlpacaDynamic3.SafetyMonitor")
        await c.close()
        return info

    info = asyncio.run(go())
    assert info["Connected"] is True
    assert info["DeviceId"] == "ASCOM.AlpacaDynamic3.SafetyMonitor"
    assert seen[0][0] == "/v2/api/equipment/safetymonitor/info"
    assert seen[1] == ("/v2/api/equipment/safetymonitor/connect",
                       {"to": "ASCOM.AlpacaDynamic3.SafetyMonitor"})


def test_piggyback_rig_config_pins_its_own_monitor():
    from photonscript.shared.rigs import rig_config, PIGGYBACK
    cfg = PhotonScriptConfig(_env_file=None, piggyback_enabled=True,
                             safety_monitor_device_id="ASCOM.AlpacaDynamic3.SafetyMonitor",
                             piggyback_safety_monitor_device_id="ASCOM.AlpacaDynamic4.SafetyMonitor")
    assert rig_config(cfg, PIGGYBACK).safety_monitor_device_id == \
        "ASCOM.AlpacaDynamic4.SafetyMonitor"
    assert cfg.safety_monitor_device_id == "ASCOM.AlpacaDynamic3.SafetyMonitor"


def test_preflight_connect_pins_safety_monitor(monkeypatch):
    from photonscript.scheduler import preflight
    got = []

    async def fake_get(config, endpoint, timeout=5):
        got.append(endpoint)
        return {}

    monkeypatch.setattr(preflight, "_nina_get", fake_get)
    cfg = PhotonScriptConfig(_env_file=None,
                             safety_monitor_device_id="ASCOM.AlpacaDynamic3.SafetyMonitor")
    assert asyncio.run(preflight._nina_connect(cfg, "safetymonitor")) == ""
    assert asyncio.run(preflight._nina_connect(cfg, "camera")) == ""
    assert got == ["/equipment/safetymonitor/connect?to=ASCOM.AlpacaDynamic3.SafetyMonitor",
                   "/equipment/camera/connect"]
