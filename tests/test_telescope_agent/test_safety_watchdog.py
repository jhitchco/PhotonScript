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

    async def get_safety_info(self):
        return {"Connected": self.connected}

    async def connect_safety(self):
        self.calls.append("connect")
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
    return a, sent


def test_grace_then_reconnect(monkeypatch):
    n = FakeNina(connected=False)
    a, _ = _agent(monkeypatch, n)
    asyncio.run(a._safety_monitor_watchdog())          # first drop: arm grace only
    assert n.calls == []
    assert a._safety_bad_since is not None
    a._safety_bad_since -= a.SAFETY_GRACE_S + 1         # grace expired
    asyncio.run(a._safety_monitor_watchdog())
    assert "connect" in n.calls
    assert a._safety_fix_attempts == 1


def test_reconnect_success_clears_state(monkeypatch):
    n = FakeNina(connected=False, reconnect_ok=True)
    a, _ = _agent(monkeypatch, n)
    asyncio.run(a._safety_monitor_watchdog())
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
