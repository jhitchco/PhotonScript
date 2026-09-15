"""Pushover notifications for supervisor/nanny alerts.

(In-sequence notifications come from NINA's GroundStation plugin; these are
the out-of-band alerts from the telescope agent itself.)

Rate limiting (added 2026-09-12 after a safe/unsafe flap emitted a push on
every 30 s tick and burned through the 10,000/mo Pushover cap):

  * identical-message dedup   - drop the same (title, message) inside
                                pushover_dedup_window_s (default 300 s).
                                This alone collapses a RUNNING<->PAUSED flap.
  * rolling hourly burst cap  - at most pushover_max_per_hour messages in any
                                60-minute window; excess is suppressed and
                                counted, then summarised on the next message
                                that gets through ("[+N suppressed]").
  * hard monthly cap          - stop sending once pushover_monthly_cap is
                                reached in the calendar month (default 9000,
                                headroom under Pushover's 10k). One final
                                high-priority notice is sent when the cap trips.

Emergency messages (priority >= 2) bypass the hourly burst cap but still count
toward — and are still stopped by — the monthly cap. Everything is best-effort:
any limiter error falls through to sending. Set pushover_ratelimit_enabled
False to restore the old unconditional behaviour.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

API = "https://api.pushover.net/1/messages.json"

# Defaults used when the config object does not define the knob.
_DEFAULTS = {
    "pushover_ratelimit_enabled": True,
    "pushover_dedup_window_s": 300,
    "pushover_max_per_hour": 20,
    "pushover_monthly_cap": 9000,
}

_lock = asyncio.Lock()


def _cfg(config, key):
    return getattr(config, key, _DEFAULTS[key])


def _state_path(config) -> Path:
    data_dir = Path(getattr(config, "data_dir", Path.home() / ".photonscript"))
    return data_dir / "pushover_state.json"


def _load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:  # noqa: BLE001 - missing/corrupt => fresh state
        return {}


def _save_state(path: Path, state: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
    except Exception as e:  # noqa: BLE001
        logger.warning("Pushover state save failed: %s", e)


async def _send_raw(config, message: str, title: str, priority: int,
                    sound: str) -> bool:
    """The actual POST. No-op (logged) if keys unset."""
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
                "sound": sound,
            })
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        logger.error("Pushover send failed: %s", e)
        return False


def _gate(state: dict, now: float, month: str, key: str, priority: int,
          dedup_window_s: int, max_per_hour: int, monthly_cap: int):
    """Pure decision function. Mutates `state`, returns (allow, reason, note).

    `note` is text to append to an allowed message (e.g. suppression summary).
    Kept side-effect-free besides `state` so it is unit-testable.
    """
    # --- monthly rollover / hard cap ---
    if state.get("month") != month:
        state["month"] = month
        state["month_count"] = 0
        state["month_capped"] = False
    if state["month_count"] >= monthly_cap:
        if not state.get("month_capped"):
            state["month_capped"] = True
            # Let this one final notice through (as an emergency), then clamp.
            state["month_count"] += 1
            return (True, "monthly-cap-final",
                    f"\n[PhotonScript: monthly Pushover cap of {monthly_cap} "
                    f"reached — further alerts suppressed until next month.]")
        return (False, "monthly-capped", "")

    # --- identical-message dedup (collapses flaps) ---
    recent = state.setdefault("recent", {})  # key -> last_ts
    last = recent.get(key)
    if last is not None and (now - last) < dedup_window_s and priority < 2:
        state["suppressed"] = state.get("suppressed", 0) + 1
        return (False, "dedup", "")

    # --- rolling hourly burst cap (priority>=2 bypasses) ---
    window = [t for t in state.get("hour_ts", []) if now - t < 3600]
    if priority < 2 and len(window) >= max_per_hour:
        state["hour_ts"] = window
        state["suppressed"] = state.get("suppressed", 0) + 1
        return (False, "hourly-cap", "")

    # --- allowed: record it ---
    window.append(now)
    state["hour_ts"] = window
    recent[key] = now
    # prune dedup map to keep the file small
    for k in [k for k, ts in recent.items() if now - ts > dedup_window_s * 4]:
        recent.pop(k, None)
    state["month_count"] = state.get("month_count", 0) + 1
    note = ""
    supp = state.get("suppressed", 0)
    if supp:
        note = f"\n[+{supp} alert(s) suppressed]"
        state["suppressed"] = 0
    return (True, "ok", note)


async def notify(config, message: str, title: str = "PhotonScript",
                 priority: int = 0, sound: str = "none") -> bool:
    """Send a Pushover notification, rate-limited. No-op (logged) if keys unset."""
    if not _cfg(config, "pushover_ratelimit_enabled"):
        return await _send_raw(config, message, title, priority, sound)

    try:
        now = time.time()
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        key = f"{title}\x1f{message}"
        path = _state_path(config)
        async with _lock:
            state = _load_state(path)
            allow, reason, note = _gate(
                state, now, month, key, priority,
                int(_cfg(config, "pushover_dedup_window_s")),
                int(_cfg(config, "pushover_max_per_hour")),
                int(_cfg(config, "pushover_monthly_cap")),
            )
            _save_state(path, state)
    except Exception as e:  # noqa: BLE001 - never let the limiter drop a real alert
        logger.warning("Pushover limiter error (%s) — sending unthrottled", e)
        return await _send_raw(config, message, title, priority, sound)

    if not allow:
        logger.info("[pushover suppressed:%s] %s: %s", reason, title, message)
        return False
    if reason == "monthly-cap-final":
        priority = max(priority, 1)
    return await _send_raw(config, message + note, title, priority, sound)
