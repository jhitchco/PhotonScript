"""Operator arm intent that must survive a restart (2026-10-16 fix).

The auto-arm loop's "manual disarm always wins" lived only in process memory
and in the armer's state (DISARMED looks the same as a night nobody armed).
On 2026-10-16 the operator disarmed at 20:26 MDT, a deploy restarted the
service, and the fresh loop auto-armed guided at 20:27 over that decision.

This small file next to armer_state.json (data_dir/arm_memory.json) keeps:

  manual_disarm     {"night", "t"}: written by a manual disarm (POST /api/arm
                    armed=false, the dashboard Disarm button). While its night
                    equals tonight's night key (the noon-to-noon key the
                    auto-arm loop uses), the evening window and the noon
                    re-arm skip. A manual Arm clears it; otherwise it simply
                    stops matching once the night rolls over at local noon.
  last_manual_arm   {"guiding", "t", "night"}: the guiding mode of the most
                    recent manual Arm. Automatic arms inherit it, falling back
                    to the config default when there is none.

Never raises: an unreadable or unwritable file degrades to "no record".
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

FILE_NAME = "arm_memory.json"


def _path(config) -> Path:
    return Path(config.data_dir) / FILE_NAME


def _night(config, now: datetime) -> str:
    from photonscript.shared.phd2_store import night_of
    return night_of(config, now)


def load(config) -> dict:
    try:
        data = json.loads(_path(config).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(config, data: dict) -> None:
    p = _path(config)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        logger.error("Could not write %s: %s", p, e)


def record_manual_disarm(config, now: datetime | None = None) -> dict:
    """Remember a manual disarm for tonight's night key."""
    from photonscript.shared.phd2_store import iso_z
    now = now or datetime.utcnow()
    rec = {"night": _night(config, now), "t": iso_z(now)}
    data = load(config)
    data["manual_disarm"] = rec
    _save(config, data)
    logger.info("manual disarm recorded for night %s: no auto-arm tonight",
                rec["night"])
    return rec


def clear_manual_disarm(config) -> None:
    data = load(config)
    if data.pop("manual_disarm", None) is not None:
        _save(config, data)
        logger.info("manual disarm record cleared (manual arm)")


def manual_disarm_tonight(config, now: datetime | None = None) -> dict | None:
    """The manual disarm record when it belongs to tonight, else None."""
    now = now or datetime.utcnow()
    rec = load(config).get("manual_disarm")
    if isinstance(rec, dict) and rec.get("night") == _night(config, now):
        return rec
    return None


def record_manual_arm(config, guiding: str,
                      now: datetime | None = None) -> dict:
    """Remember the (resolved) guiding mode of a manual Arm."""
    from photonscript.shared.phd2_store import iso_z
    now = now or datetime.utcnow()
    rec = {"guiding": guiding, "t": iso_z(now), "night": _night(config, now)}
    data = load(config)
    data["last_manual_arm"] = rec
    _save(config, data)
    return rec


def last_manual_arm(config) -> dict | None:
    rec = load(config).get("last_manual_arm")
    if isinstance(rec, dict) and rec.get("guiding") in ("guided", "unguided"):
        return rec
    return None


def auto_guiding(config, trigger: str) -> tuple[str | None, str]:
    """(guiding, why) for an automatic arm. trigger "auto-noon" or
    "auto-evening". The most recent manual Arm's mode wins; otherwise the
    config default: noon_arm_guided for the noon re-arm, None (the armer's
    guided_default) for the evening window."""
    rec = last_manual_arm(config)
    if rec:
        return rec["guiding"], (f"inherited from the last manual arm "
                                f"({rec.get('t')}, night {rec.get('night')})")
    if trigger == "auto-noon":
        g = "guided" if getattr(config, "noon_arm_guided", True) else "unguided"
        return g, f"no manual arm on record: config noon_arm_guided -> {g}"
    g = "guided" if getattr(config, "guided_default", True) else "unguided"
    return None, f"no manual arm on record: config guided_default -> {g}"
