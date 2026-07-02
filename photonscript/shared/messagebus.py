"""Simple async message bus for inter-agent communication."""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
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

    def __init__(self):
        self._listeners: dict[str, list[Listener]] = defaultdict(list)
        self._global_listeners: list[Listener] = []
        self._history: list[AgentMessage] = []

    def subscribe(self, msg_type: str, listener: Listener) -> None:
        self._listeners[msg_type].append(listener)

    def subscribe_all(self, listener: Listener) -> None:
        self._global_listeners.append(listener)

    async def publish(self, message: AgentMessage) -> None:
        if message.id is None:
            message.id = str(uuid4())
        self._history.append(message)
        logger.info(
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

    async def _safe_call(self, listener: Listener, message: AgentMessage) -> None:
        try:
            await listener(message)
        except Exception:
            logger.exception("Error in message listener for %s", message.msg_type)

    def get_history(self, limit: int = 100) -> list[AgentMessage]:
        return self._history[-limit:]


# Singleton for the application
_bus: MessageBus | None = None


def get_message_bus() -> MessageBus:
    global _bus
    if _bus is None:
        _bus = MessageBus()
    return _bus
