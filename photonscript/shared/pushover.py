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

  * quiet daytime (PS-50)     - while the sun is up at the observatory,
                                heartbeats are dropped and each title sends at
                                most once per pushover_daytime_title_window_h.

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
    "pushover_quiet_daytime": True,
    "pushover_daytime_title_window_h": 4.0,
    "pushover_daytime_sun_alt_deg": -3.0,
    "pushover_emergency_retry_s": 300,
    "pushover_emergency_expire_s": 1800,
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


def _audit_path(config) -> Path:
    data_dir = Path(getattr(config, "data_dir", Path.home() / ".photonscript"))
    return data_dir / "notifications.jsonl"


def _audit(config, title: str, message: str, priority: int, sent: bool,
           reason: str) -> None:
    """Append every notification DECISION (sent or suppressed) to
    notifications.jsonl so alert volume is auditable later — nothing was logged
    for *sent* alerts before, so there was no trail. Best-effort; append-only,
    trimmed to the last ~5000 lines when it grows past ~4 MB."""
    try:
        p = _audit_path(config)
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": datetime.now(timezone.utc).isoformat(),
               "title": title, "message": (message or "")[:500],
               "priority": int(priority), "sent": bool(sent), "reason": reason}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
        try:
            if p.stat().st_size > 4_000_000:
                tail = p.read_text(encoding="utf-8",
                                   errors="replace").splitlines()[-5000:]
                p.write_text("\n".join(tail) + "\n", encoding="utf-8")
        except OSError:
            pass
    except Exception as e:  # noqa: BLE001 - auditing must never break an alert
        logger.debug("notification audit-log write failed: %s", e)


def record(config, message: str, title: str = "PhotonScript",
           priority: int = 0, reason: str = "held") -> None:
    """Audit a notification that the CALLER decided not to push (e.g. the
    armer collapsing a guiding flap, PS-66), so notifications.jsonl and
    /api/notifications still show every event with sent=false."""
    logger.info("[pushover held:%s] %s: %s", reason, title, message)
    _audit(config, title, message, priority, False, reason)


def _payload(config, message: str, title: str, priority: int,
             sound: str) -> dict:
    """POST body. Pushover REJECTS priority 2 (emergency) without retry and
    expire: both 2026-09-26 "STILL not guiding" escalations came back
    send-failed for that reason (PS-66)."""
    data = {
        "token": config.pushover_api_token,
        "user": config.pushover_user_key,
        "title": title,
        "message": message,
        "priority": priority,
        "sound": sound,
    }
    if int(priority) >= 2:
        retry = int(_cfg(config, "pushover_emergency_retry_s"))
        expire = int(_cfg(config, "pushover_emergency_expire_s"))
        data["retry"] = max(30, retry)              # Pushover minimum 30 s
        data["expire"] = max(data["retry"], min(10800, expire))  # max 3 h
    return data


def _attachment_file(path) -> tuple | None:
    """PS-161: (name, bytes, mime) for a Pushover image attachment, None
    when unreadable or over Pushover's 5 MB limit."""
    try:
        p = Path(path)
        data = p.read_bytes()
    except (OSError, TypeError, ValueError):
        return None
    if len(data) > 5 * 1024 * 1024:
        logger.warning("Pushover attachment %s over 5 MB: sent without it", path)
        return None
    mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    return (p.name, data, mime)


async def _send_raw(config, message: str, title: str, priority: int,
                    sound: str, attachment=None) -> bool:
    """The actual POST. No-op (logged) if keys unset. attachment (PS-161):
    an image path sent as Pushover's attachment (multipart)."""
    if not config.pushover_user_key or not config.pushover_api_token:
        logger.info("[pushover disabled] %s: %s", title, message)
        return False
    try:
        files = None
        if attachment:
            f = _attachment_file(attachment)
            files = {"attachment": f} if f else None
        async with httpx.AsyncClient(timeout=30 if files else 10) as client:
            r = await client.post(API, data=_payload(config, message, title,
                                                     priority, sound),
                                  files=files)
        if r.status_code != 200:
            logger.error("Pushover rejected (%s): %s", r.status_code,
                         (r.text or "")[:200])
        return r.status_code == 200
    except Exception as e:  # noqa: BLE001
        logger.error("Pushover send failed: %s", e)
        return False


def sun_altitude_deg(lat_deg: float, lon_deg: float, when: datetime) -> float:
    """Approximate solar altitude (NOAA low-precision formulas, ~0.1 deg).
    Pure math so the Pushover path never pays for an astropy import."""
    import math
    t = when.astimezone(timezone.utc)
    doy = t.timetuple().tm_yday
    hour = t.hour + t.minute / 60 + t.second / 3600
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    tst = hour * 60 + eqtime + 4 * lon_deg          # true solar time, minutes
    ha = math.radians(tst / 4 - 180)                 # hour angle
    lat = math.radians(lat_deg)
    cos_zen = (math.sin(lat) * math.sin(decl)
               + math.cos(lat) * math.cos(decl) * math.cos(ha))
    return 90 - math.degrees(math.acos(max(-1.0, min(1.0, cos_zen))))


def _is_daytime(config, when: datetime | None = None) -> bool:
    try:
        lat = float(getattr(config, "observatory_lat"))
        lon = float(getattr(config, "observatory_lon"))
    except (AttributeError, TypeError, ValueError):
        return False  # unknown site: never quiet (fail loud)
    alt = sun_altitude_deg(lat, lon, when or datetime.now(timezone.utc))
    return alt > float(_cfg(config, "pushover_daytime_sun_alt_deg"))


def _daytime_gate(state: dict, now: float, title: str, priority: int,
                  window_s: float) -> tuple[bool, str]:
    """Daytime quieting (PS-50). Only called while the sun is up.

    priority <= -1 (heartbeats): suppressed.
    priority 0..1: at most one per TITLE per window (a repeating
      "DISCONNECTED for 32/62/92 min" sends once, not every 30 min).
    priority >= 2 (emergency): always passes.
    Mutates `state`; returns (allow, reason)."""
    if priority >= 2:
        return True, "ok"
    if priority <= -1:
        return False, "quiet-daytime"
    by_title = state.setdefault("day_titles", {})
    last = by_title.get(title)
    if last is not None and (now - last) < window_s:
        return False, "quiet-daytime"
    by_title[title] = now
    for k in [k for k, ts in by_title.items() if now - ts > window_s * 2]:
        by_title.pop(k, None)
    return True, "ok"


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
                 priority: int = 0, sound: str = "none", attachment=None) -> bool:
    """Send a Pushover notification, rate-limited. No-op (logged) if keys unset.
    attachment (PS-161): an image path to attach (desktop review JPG)."""
    att = {"attachment": attachment} if attachment else {}
    if not _cfg(config, "pushover_ratelimit_enabled"):
        sent = await _send_raw(config, message, title, priority, sound, **att)
        _audit(config, title, message, priority, sent, "sent" if sent else "not-sent")
        return sent

    try:
        now = time.time()
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        key = f"{title}\x1f{message}"
        path = _state_path(config)
        async with _lock:
            state = _load_state(path)
            if _cfg(config, "pushover_quiet_daytime") and _is_daytime(config):
                ok, why = _daytime_gate(
                    state, now, title, priority,
                    float(_cfg(config, "pushover_daytime_title_window_h")) * 3600)
                if not ok:
                    _save_state(path, state)
                    logger.info("[pushover suppressed:%s] %s: %s", why, title, message)
                    _audit(config, title, message, priority, False, why)
                    return False
            allow, reason, note = _gate(
                state, now, month, key, priority,
                int(_cfg(config, "pushover_dedup_window_s")),
                int(_cfg(config, "pushover_max_per_hour")),
                int(_cfg(config, "pushover_monthly_cap")),
            )
            _save_state(path, state)
    except Exception as e:  # noqa: BLE001 - never let the limiter drop a real alert
        logger.warning("Pushover limiter error (%s) — sending unthrottled", e)
        sent = await _send_raw(config, message, title, priority, sound, **att)
        _audit(config, title, message, priority, sent, "limiter-error")
        return sent

    if not allow:
        logger.info("[pushover suppressed:%s] %s: %s", reason, title, message)
        _audit(config, title, message, priority, False, reason)
        return False
    if reason == "monthly-cap-final":
        priority = max(priority, 1)
    sent = await _send_raw(config, message + note, title, priority, sound, **att)
    reason_out = ("sent" if reason == "ok" else reason) if sent else "send-failed"
    _audit(config, title, message, priority, sent, reason_out)
    return sent
