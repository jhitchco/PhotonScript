"""PS-74: a stalled dashboard WebSocket can't delay publishers, and the
MessageBus history is bounded."""

from __future__ import annotations

import asyncio
import time

import pytest

from photonscript.shared.messagebus import MessageBus
from photonscript.shared.models import AgentMessage, AgentRole


class FakeWS:
    def __init__(self, delay: float = 0.0, fail: bool = False):
        self.delay = delay
        self.fail = fail
        self.sent: list[str] = []
        self.closed = False

    async def send_text(self, msg: str) -> None:
        if self.fail:
            raise RuntimeError("socket gone")
        if self.delay:
            await asyncio.sleep(self.delay)
        self.sent.append(msg)

    async def close(self, code: int = 1000) -> None:
        self.closed = True


class StalledWS(FakeWS):
    """send_text never returns (a tab that stopped reading)."""

    async def send_text(self, msg: str) -> None:
        await asyncio.Event().wait()


@pytest.fixture
def appmod(monkeypatch):
    import photonscript.scheduler.app as app
    monkeypatch.setattr(app, "_ws_clients", [])
    monkeypatch.setattr(app, "_broadcast_task", None)
    monkeypatch.setattr(app, "_broadcast_dirty", False)
    monkeypatch.setattr(app, "_WS_SEND_TIMEOUT_S", 0.2)
    return app


async def _drain(app, limit: float = 3.0):
    t0 = time.monotonic()
    while app._broadcast_task is not None and time.monotonic() - t0 < limit:
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.02)   # let background closes run


def _state_msg(i: int = 0) -> AgentMessage:
    return AgentMessage(sender=AgentRole.TELESCOPE,
                        recipient=AgentRole.SCHEDULER,
                        msg_type="telescope_state_update",
                        payload={"connected": True, "focuser_position": i})


async def test_stalled_client_cannot_delay_publish(appmod):
    stalled, good = StalledWS(), FakeWS()
    appmod._ws_clients.extend([stalled, good])
    bus = MessageBus()
    bus.subscribe("telescope_state_update", appmod.on_agent_message)
    t0 = time.monotonic()
    await bus.publish(_state_msg())
    assert time.monotonic() - t0 < 0.1          # publisher never waits on sends
    await _drain(appmod)
    assert good.sent and '"focuser_position": 0' in good.sent[-1]
    assert stalled not in appmod._ws_clients     # dropped after the timeout
    assert good in appmod._ws_clients
    assert stalled.closed


async def test_broadcast_round_is_bounded_and_concurrent(appmod):
    """Five slow-but-alive clients cost one timeout, not five sends in a row."""
    slow = [FakeWS(delay=0.1) for _ in range(5)]
    appmod._ws_clients.extend(slow)
    t0 = time.monotonic()
    await appmod._broadcast_now()
    took = time.monotonic() - t0
    assert took < 0.35, took                     # sequential would be 0.5 s
    assert all(len(w.sent) == 1 for w in slow)
    assert len(appmod._ws_clients) == 5


async def test_broadcast_round_capped_by_timeout(appmod):
    appmod._ws_clients.extend([StalledWS(), StalledWS(), FakeWS()])
    t0 = time.monotonic()
    await appmod._broadcast_now()
    assert time.monotonic() - t0 < 0.5           # one 0.2 s timeout
    assert len(appmod._ws_clients) == 1


async def test_failed_client_dropped(appmod):
    dead, good = FakeWS(fail=True), FakeWS()
    appmod._ws_clients.extend([dead, good])
    await appmod._broadcast_now()
    assert appmod._ws_clients == [good]


async def test_requests_coalesce(appmod):
    """A burst of state updates while a round is in flight collapses into
    one follow-up round carrying the latest state."""
    ws = FakeWS(delay=0.05)
    appmod._ws_clients.append(ws)
    bus = MessageBus()
    bus.subscribe("telescope_state_update", appmod.on_agent_message)
    for i in range(20):
        await bus.publish(_state_msg(i))
    await _drain(appmod)
    assert 1 <= len(ws.sent) <= 2
    assert '"focuser_position": 19' in ws.sent[-1]


async def test_state_loop_cadence_with_stalled_client(appmod):
    """The telescope state loop (publish, then sleep) keeps its cadence with
    a stalled tab connected: the 2026-09-26 symptom was 22 s instead of 10 s."""
    appmod._ws_clients.extend([StalledWS(), FakeWS()])
    bus = MessageBus()
    bus.subscribe("telescope_state_update", appmod.on_agent_message)
    stamps = []
    for i in range(5):
        stamps.append(time.monotonic())
        await bus.publish(_state_msg(i))
        await asyncio.sleep(0.05)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert max(gaps) < 0.15, gaps
    await _drain(appmod)


def test_request_broadcast_outside_loop_is_harmless(appmod):
    appmod.request_broadcast()
    assert appmod._broadcast_task is None


# --- MessageBus history -------------------------------------------------------

async def test_history_is_capped():
    bus = MessageBus(history_max=50)
    for i in range(120):
        await bus.publish(_state_msg(i))
    assert len(bus._history) == 50
    h = bus.get_history(limit=10)
    assert [m.payload["focuser_position"] for m in h] == list(range(110, 120))
    assert len(bus.get_history(limit=1000)) == 50
    assert bus.get_history(limit=0) == []


def test_default_history_cap():
    assert MessageBus()._history.maxlen == MessageBus.HISTORY_MAX == 500
