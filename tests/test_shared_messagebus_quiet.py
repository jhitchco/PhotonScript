"""2026-10-16: routine telescope_state_update messages log at DEBUG; INFO
only when a rig's session_state or current_target changes; other
messages stay INFO."""
import asyncio
import logging

from photonscript.shared.messagebus import MessageBus
from photonscript.shared.models import AgentMessage, AgentRole

LOG = "photonscript.shared.messagebus"


def _state(rig, state="imaging", target="M31"):
    return AgentMessage(sender=AgentRole.TELESCOPE, recipient=AgentRole.SCHEDULER,
                        msg_type="telescope_state_update",
                        payload={"rig": rig, "session_state": state,
                                 "current_target": target})


def _levels(caplog):
    return [r.levelno for r in caplog.records if r.name == LOG]


def test_state_updates_info_only_on_change(caplog):
    bus = MessageBus()
    with caplog.at_level(logging.DEBUG, LOG):
        for m in (_state("rc16"), _state("rc16"), _state("piggyback"),
                  _state("piggyback"), _state("rc16", target="Heart"),
                  _state("rc16", target="Heart"),
                  _state("rc16", state="idle", target="Heart")):
            asyncio.run(bus.publish(m))
        asyncio.run(bus.publish(AgentMessage(
            sender=AgentRole.TELESCOPE, recipient=AgentRole.SCHEDULER,
            msg_type="image_captured", payload={})))
    I, D = logging.INFO, logging.DEBUG
    assert _levels(caplog) == [I, D, I, D, I, D, I, I]
