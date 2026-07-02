"""Pushover notifications for supervisor/nanny alerts.

(In-sequence notifications come from NINA's GroundStation plugin; these are
the out-of-band alerts from the telescope agent itself.)
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

API = "https://api.pushover.net/1/messages.json"


async def notify(config, message: str, title: str = "PhotonScript",
                 priority: int = 0) -> bool:
    """Send a Pushover notification. No-op (logged) if keys unset."""
    if not config.pushover_user_key or not config.pushover_api_token:
        logger.info("[pushover disabled] %s: %s", title, message)
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(API, data={
                "token": config.pushover_api_token,
                "user": config.pushover_user_key,
                "title": title,
                "message": message,
                "priority": priority,
            })
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        logger.error("Pushover send failed: %s", e)
        return False
