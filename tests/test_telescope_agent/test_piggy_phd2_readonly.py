"""The Piggy-600 agent's PHD2 is read-only (2026-10-13).

Both rigs' agents watch the one PHD2 (its guide camera is on the RC16's
OAG). The Piggy-600 agent used to open a second connection to it and redo
the pixel-scale / RMS work; now the orchestrator hands it the RC16 agent's
client, seen through ReadOnlyPHD2: reads and listeners pass, every control
call raises, and it never connects, refreshes the scale or runs an event
loop of its own."""
import asyncio

import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent.agent import TelescopeAgent
from photonscript.telescope_agent.phd2_client import (PHD2Client, PHD2ReadOnly,
                                                      ReadOnlyPHD2)


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "d"), **kw)


@pytest.mark.parametrize("name", sorted(ReadOnlyPHD2.CONTROL))
def test_every_control_call_is_refused(name):
    inner = PHD2Client()
    sent = []

    async def spy(*a, **k):
        sent.append(name)
    setattr(inner, name, spy)
    ro = ReadOnlyPHD2(inner)
    with pytest.raises(PHD2ReadOnly):
        asyncio.run(getattr(ro, name)())
    assert sent == []


def test_reads_and_listeners_pass_rpc_commands_do_not():
    inner = PHD2Client()
    ro = ReadOnlyPHD2(inner)
    assert ro.inner is inner
    assert ro.connected is False and ro.app_state == "Unknown"
    assert ro.metrics is inner.metrics and ro.pixel_scale is None
    with pytest.raises(PHD2ReadOnly):
        asyncio.run(ro.call("stop_capture"))
    with pytest.raises(PHD2ReadOnly):
        asyncio.run(ro.call("set_exposure", [2000]))
    with pytest.raises(ConnectionError):      # a read goes to the real client
        asyncio.run(ro.call("get_app_state"))
    with pytest.raises(PHD2ReadOnly):
        ro.app_state = "Guiding"

    async def listener(m):
        pass
    ro.on_update(listener)
    assert listener in inner._listeners


def test_piggy_agent_shares_the_rc16_connection(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    rc = TelescopeAgent(cfg, rig="rc16")
    pb = TelescopeAgent(cfg, rig="piggyback", phd2=rc._phd2_client)
    assert rc.phd2 is rc._phd2_client and rc._phd2_owned
    assert isinstance(pb.phd2, ReadOnlyPHD2) and pb.phd2.inner is rc.phd2
    assert pb._phd2_owned is False
    connects = []

    async def connect():
        connects.append(1)
        return False
    monkeypatch.setattr(rc.phd2, "connect", connect)
    pb._running = True
    asyncio.run(pb._phd2_monitor())          # returns at once
    assert connects == []
    # the RC16 agent's events reach the Piggy-600 agent's state
    pb.phd2.on_update(pb._on_guiding_update)
    asyncio.run(rc.phd2._handle_event({"Event": "GuideStep", "RADistanceRaw": 0.2,
                                       "DECDistanceRaw": 0.1, "SNR": 30}))
    assert pb.state.guiding is rc.phd2.metrics
    # a scheduler guiding command never goes out through the Piggy-600 agent
    from photonscript.shared.models import AgentMessage, AgentRole
    sent = []

    async def start_guiding(*a, **k):
        sent.append("start_guiding")
    monkeypatch.setattr(rc.phd2, "start_guiding", start_guiding)
    msg = AgentMessage(sender=AgentRole.SCHEDULER, recipient=AgentRole.TELESCOPE,
                       msg_type="command", payload={"action": "start_guiding"})
    asyncio.run(pb._on_command(msg))
    assert sent == []
    asyncio.run(rc._on_command(msg))
    assert sent == ["start_guiding"]


def test_piggy_agent_alone_is_still_read_only(tmp_path):
    pb = TelescopeAgent(_cfg(tmp_path), rig="piggyback")
    assert pb._phd2_owned is True and isinstance(pb.phd2, ReadOnlyPHD2)
    with pytest.raises(PHD2ReadOnly):
        asyncio.run(pb.phd2.stop_capture())


def test_orchestrator_hands_the_rc16_client_to_the_piggy_agent():
    from photonscript import orchestrator as o
    ag = o._telescope_agents(PhotonScriptConfig(
        _env_file=None, piggyback_enabled=True, piggyback_image_watch_dir=r"C:\pb"))
    assert len(ag) == 2
    assert ag[1].phd2.inner is ag[0].phd2 and ag[1]._phd2_owned is False
