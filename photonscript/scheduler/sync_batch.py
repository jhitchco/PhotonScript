"""Batch progress for the desktop transfer.

Syncthing only reports whole-folder completion, which sits at 98-99% forever
because a handful of new files barely move a huge archive. This tracks a
*batch* instead: the high-water mark of pending work since the last reset, so
the dashboard can show "transferred N of M this batch" draining to 100%.

Model (no Syncthing calls here — the caller passes the current pending counts):
  * mark_reset() zeroes the baseline and timestamps it — a fresh batch starts.
  * annotate(current_items, current_bytes) grows the baseline to the peak seen
    since reset (so linking 150 files makes the batch 150), then reports how much
    of that peak has drained. Called on every /api/sync read.

State lives in data_dir/sync_batch.json. Everything is best-effort: any I/O
error degrades to an inert (empty) batch so the sync endpoint never fails.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def _state_path(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "sync_batch.json"


def _load(config) -> dict:
    try:
        p = _state_path(config)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(config, state: dict) -> None:
    try:
        p = _state_path(config)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state), encoding="utf-8")
    except OSError as e:  # noqa: BLE001
        logger.warning("sync_batch: save failed: %s", e)


def mark_reset(config) -> dict:
    """Start a new batch: zero the baseline so the next annotate() rebuilds the
    high-water mark from the freshly-queued work. Called by the Reset button and
    automatically after a library rebuild / night approval."""
    state = {"baseline_items": 0, "baseline_bytes": 0,
             "reset_at": datetime.now(timezone.utc).isoformat(),
             "last_pending_items": None, "last_progress_at": None,
             "stall_alerted": False}
    _save(config, state)
    return state


def annotate(config, current_items, current_bytes, now=None) -> dict:
    """Grow the baseline to the peak pending since reset, then report drain.

    Returns a batch dict for the sync payload. `active` is True while there's a
    non-empty batch still transferring. Also tracks whether the batch has
    STALLED — pending hasn't dropped in `sync_stall_min` minutes while still
    non-empty (a wedged Syncthing/librarian loop that only *reports* the backlog
    and silently re-queues forever). `stall_new` is True on the single read that
    first crosses the threshold, so the caller can Pushover exactly once.
    """
    now = now or datetime.now(timezone.utc)
    try:
        ci = int(current_items or 0)
        cb = int(current_bytes or 0)
    except (TypeError, ValueError):
        ci, cb = 0, 0
    state = _load(config)
    b_items = int(state.get("baseline_items", 0) or 0)
    b_bytes = int(state.get("baseline_bytes", 0) or 0)
    if ci > b_items:
        b_items = ci
    if cb > b_bytes:
        b_bytes = cb
    state.update(baseline_items=b_items, baseline_bytes=b_bytes)

    # --- stall tracking: "progress" = pending dropped (or first-ever sample) ---
    prev = state.get("last_pending_items")
    if prev is None or ci < int(prev):
        state["last_progress_at"] = now.isoformat()
        state["stall_alerted"] = False
    state["last_pending_items"] = ci

    active = b_items > 0 and ci > 0
    stall_win = int(getattr(config, "sync_stall_min", 30))
    stalled, stall_new, stalled_min = False, False, 0
    lp = state.get("last_progress_at")
    if active and lp:
        try:
            stalled_min = int((now - datetime.fromisoformat(lp)).total_seconds() // 60)
        except (ValueError, TypeError):
            stalled_min = 0
        stalled = stall_win > 0 and stalled_min >= stall_win
        if stalled and not state.get("stall_alerted"):
            stall_new = True
            state["stall_alerted"] = True
    _save(config, state)

    done_items = max(0, b_items - ci)
    done_bytes = max(0, b_bytes - cb)
    if b_bytes > 0:
        pct = round(done_bytes / b_bytes * 100, 1)
    elif b_items > 0:
        pct = round(done_items / b_items * 100, 1)
    else:
        pct = 100.0
    return {
        "baseline_items": b_items,
        "baseline_bytes": b_bytes,
        "transferred_items": done_items,
        "transferred_bytes": done_bytes,
        "pending_items": ci,
        "pending_bytes": cb,
        "pct": pct,
        "reset_at": state.get("reset_at"),
        "active": active,
        "stalled": stalled,
        "stalled_min": stalled_min,
        "stall_new": stall_new,
    }
