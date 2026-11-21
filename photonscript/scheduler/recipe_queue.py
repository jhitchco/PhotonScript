"""Queue an arm recipe for a future night (2026-11-20).

PS-181 recipes ride on one arm (POST /api/arm {"recipe"}), and the noon /
evening auto-arms never carry one, so a test planned for tomorrow needed a
hand arm after the dawn shutdown. This file (data_dir/recipe_queue.json)
holds {"nights": {"YYYY-MM-DD": {"recipe", "opts", "label", "queued_at"}}}.
Any arm of that night without its own recipe (manual, auto-noon,
auto-evening) takes the queued one and removes it from the queue (one arm,
one recipe, as PS-181). Nights already past are pruned on every write.

Never raises on file trouble: an unreadable file is an empty queue.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.shared.fileops import replace_retry

logger = logging.getLogger(__name__)

FILE_NAME = "recipe_queue.json"
_NIGHT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _path(config) -> Path:
    return Path(config.data_dir) / FILE_NAME


def load(config) -> dict:
    try:
        data = json.loads(_path(config).read_text(encoding="utf-8"))
        nights = data.get("nights") if isinstance(data, dict) else None
        return {"nights": nights if isinstance(nights, dict) else {}}
    except (OSError, ValueError):
        return {"nights": {}}


def _save(config, data: dict) -> bool:
    p = _path(config)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        replace_retry(tmp, p)
        return True
    except OSError as e:
        logger.warning("recipe queue not saved: %s", e)
        return False


def _prune(data: dict, today: str) -> dict:
    data["nights"] = {n: e for n, e in data["nights"].items() if n >= today}
    return data


def _today(now: datetime | None = None) -> str:
    # a night key is the evening date; anything two days back is over
    return ((now or datetime.utcnow()) - timedelta(days=2)).strftime("%Y-%m-%d")


def queue(config, night: str, recipe, opts=None,
          now: datetime | None = None) -> tuple[dict | None, str | None]:
    """Validate (sideload.arm_recipe) and store the recipe for night.
    Returns (entry, None) or (None, error). A new entry replaces the night's
    old one."""
    night = str(night or "").strip()
    if not _NIGHT_RE.match(night):
        return None, "night must be YYYY-MM-DD (the evening date)"
    if night < _today(now):
        return None, f"night {night} is over"
    from photonscript.scheduler.sideload import arm_recipe
    rec, err = arm_recipe(recipe, opts)
    if err:
        return None, err
    if rec is None:
        return None, "recipe required (use DELETE to clear a night)"
    entry = {"recipe": rec["id"], "opts": dict(rec.get("opts") or {}),
             "label": rec["label"],
             "queued_at": (now or datetime.utcnow()).isoformat(timespec="seconds") + "Z"}
    data = _prune(load(config), _today(now))
    data["nights"][night] = entry
    if not _save(config, data):
        return None, "could not write the recipe queue"
    return {"night": night, **entry}, None


def remove(config, night: str) -> bool:
    data = load(config)
    if night not in data["nights"]:
        return False
    data["nights"].pop(night, None)
    _save(config, data)
    return True


def take(config, night: str | None) -> dict | None:
    """The validated recipe queued for night, removed from the queue; None
    when nothing is queued (or it no longer validates, logged)."""
    if not night:
        return None
    data = load(config)
    entry = data["nights"].get(night)
    if not isinstance(entry, dict):
        return None
    from photonscript.scheduler.sideload import arm_recipe
    rec, err = arm_recipe(entry.get("recipe"), entry.get("opts") or None)
    data["nights"].pop(night, None)
    _save(config, data)
    if err or rec is None:
        logger.warning("queued recipe for %s dropped: %s", night, err)
        return None
    return rec


def listing(config) -> list[dict]:
    return [{"night": n, **e} for n, e in sorted(load(config)["nights"].items())]
