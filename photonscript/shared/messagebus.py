"""Simple async message bus for inter-agent communication."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict, deque
from datetime import datetime
from typing import Callable, Awaitable
from uuid import uuid4

from photonscript.shared.models import AgentMessage, AgentRole

logger = logging.getLogger(__name__)

Listener = Callable[[AgentMessage], Awaitable[None]]


class MessageBus:
    """In-process async pub/sub for agent communication.

    Agents subscribe to message types and receive callbacks when messages
    matching their role arrive.
    """

    # PS-74: history used to be an unbounded list. Two rigs publish a state
    # update every 10 s, so a multi-day uptime grew it by ~17k messages a day
    # (each a full TelescopeState payload). Only the recent tail is ever read.
    HISTORY_MAX = 500

    def __init__(self, history_max: int = HISTORY_MAX):
        self._listeners: dict[str, list[Listener]] = defaultdict(list)
        self._global_listeners: list[Listener] = []
        self._history: deque[AgentMessage] = deque(maxlen=max(1, history_max))
        self._state_keys: dict[str, tuple] = {}   # rig -> last logged state

    def subscribe(self, msg_type: str, listener: Listener) -> None:
        self._listeners[msg_type].append(listener)

    def subscribe_all(self, listener: Listener) -> None:
        self._global_listeners.append(listener)

    async def publish(self, message: AgentMessage) -> None:
        if message.id is None:
            message.id = str(uuid4())
        self._history.append(message)
        logger.log(
            self._log_level(message),
            "MSG [%s] %s -> %s: %s",
            message.msg_type, message.sender.value, message.recipient.value,
            str(message.payload)[:200],
        )

        tasks = []
        for listener in self._listeners.get(message.msg_type, []):
            tasks.append(asyncio.create_task(self._safe_call(listener, message)))
        for listener in self._global_listeners:
            tasks.append(asyncio.create_task(self._safe_call(listener, message)))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # 2026-10-16: two rigs x one state update every 10 s filled the log at
    # INFO. Routine state updates log at DEBUG; INFO only when a rig's
    # session state or current target changes.
    ROUTINE_TYPES = ("telescope_state_update",)

    def _log_level(self, message: AgentMessage) -> int:
        if message.msg_type not in self.ROUTINE_TYPES:
            return logging.INFO
        p = message.payload if isinstance(message.payload, dict) else {}
        rig = str(p.get("rig") or message.sender.value)
        key = (p.get("session_state"), p.get("current_target"))
        if self._state_keys.get(rig) == key:
            return logging.DEBUG
        self._state_keys[rig] = key
        return logging.INFO

    async def _safe_call(self, listener: Listener, message: AgentMessage) -> None:
        try:
            await listener(message)
        except Exception:
            logger.exception("Error in message listener for %s", message.msg_type)

    def get_history(self, limit: int = 100) -> list[AgentMessage]:
        if limit <= 0:
            return []
        return list(self._history)[-limit:]


# Singleton for the application
_bus: MessageBus | None = None


def get_message_bus() -> MessageBus:
    global _bus
    if _bus is None:
        _bus = MessageBus()
    return _bus
