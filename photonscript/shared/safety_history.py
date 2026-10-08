"""Safety-monitor state history (PS-71): when was the roof unsafe?

The armer reads NINA's safety monitor every tick while a night is active.
Each CHANGE (safe / unsafe / unknown) is appended to
<data_dir>/safety_history.jsonl, so grading can ask "was this exposure shot
while the monitor read unsafe?" after the fact. Nights before this file
existed fall back to the Pushover audit (notifications.jsonl), whose
"PhotonScript paused" / "resumed" / "complete" records bracket the same
windows to within one armer tick (30 s).

Best-effort by design: a failed write is logged and ignored; readers treat a
missing or corrupt file as "no history".
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_last: dict[str, str] = {}   # path -> last recorded state


def history_path(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "safety_history.jsonl"


def _state(safe) -> str:
    return "safe" if safe is True else "unsafe" if safe is False else "unknown"


def _utc(ts) -> datetime | None:
    """Parse an ISO timestamp to naive UTC ('Z', offsets and naive accepted)."""
    if isinstance(ts, datetime):
        dt = ts
    else:
        try:
            dt = datetime.fromisoformat(str(ts).strip().replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _last_recorded(p: Path) -> str | None:
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            return json.loads(line).get("state")
        except (json.JSONDecodeError, AttributeError):
            continue
    return None


def record(config, safe, now: datetime | None = None,
           source: str = "armer") -> bool:
    """Append the monitor state if it changed since the last record.
    Returns True when a line was written."""
    p = history_path(config)
    st = _state(safe)
    key = str(p)
    try:
        with _lock:
            if key not in _last:
                prev = _last_recorded(p)
                if prev is not None:
                    _last[key] = prev
            if _last.get(key) == st:
                return False
            now = now or datetime.utcnow()
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": now.isoformat() + "Z", "state": st,
                                    "source": source}) + "\n")
            _last[key] = st
            return True
    except Exception as e:  # noqa: BLE001 - history must never break a tick
        logger.debug("safety history write failed: %s", e)
        return False


def _windows_from_events(events: list[tuple[datetime, str]],
                         start: datetime, end: datetime) -> list[tuple]:
    """[(from, to)] unsafe intervals clipped to [start, end] from a sorted
    list of (time, state) transitions."""
    out = []
    cur = None
    for t, st in sorted(events):
        if st == "unsafe":
            if cur is None:
                cur = t
        elif cur is not None:
            out.append((cur, t))
            cur = None
    if cur is not None:
        out.append((cur, end))
    return [(max(a, start), min(b, end)) for a, b in out
            if b > start and a < end]


def _history_events(config) -> list[tuple[datetime, str]]:
    try:
        lines = history_path(config).read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    ev = []
    for line in lines:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        t = _utc(r.get("ts"))
        if t is not None and r.get("state") in ("safe", "unsafe", "unknown"):
            ev.append((t, r["state"]))
    return ev


# Pushover audit titles the armer uses around an unsafe pause
_PAUSE_TITLES = {"PhotonScript paused": "unsafe",
                 "PhotonScript SAFETY STOP": "unsafe",
                 "PhotonScript resumed": "safe",
                 "PhotonScript complete": "safe"}


def _notification_events(config) -> list[tuple[datetime, str]]:
    from photonscript.shared.pushover import _audit_path
    try:
        lines = _audit_path(config).read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    ev = []
    for line in lines:
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        st = _PAUSE_TITLES.get(str(r.get("title", "")))
        t = _utc(r.get("ts"))
        if st and t is not None:
            ev.append((t, st))
    return ev


def unsafe_windows(config, start: datetime, end: datetime,
                   extra: list[tuple] | None = None) -> tuple[list, str]:
    """Unsafe windows overlapping [start, end] (naive UTC) and their source:
    'history' (safety_history.jsonl), 'notifications' (Pushover audit
    fallback) or 'none'. `extra` windows (e.g. from a CLI flag) are added."""
    windows, source = [], "none"
    ev = _history_events(config)
    if ev:
        windows = _windows_from_events(ev, start, end)
        source = "history"
    else:
        ev = _notification_events(config)
        if ev:
            windows = _windows_from_events(ev, start, end)
            source = "notifications"
    for a, b in extra or []:
        a, b = _utc(a), _utc(b)
        if a and b and b > start and a < end:
            windows.append((max(a, start), min(b, end)))
            source = source if source != "none" else "manual"
    return sorted(windows), source


def overlap_seconds(windows: list[tuple], start: datetime,
                    end: datetime) -> float:
    return sum(max(0.0, (min(b, end) - max(a, start)).total_seconds())
               for a, b in windows)
