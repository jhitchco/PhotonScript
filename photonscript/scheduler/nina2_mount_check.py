"""PS-139: read-only check of what NINA #2 (Piggy-600) has loaded.

On 2026-10-03 a NINA #2 sequence hand-built from NINA's stock deep-sky
template ran a Center inside its per-sub loop (slew, solve, refused sync,
~58' offset slew, solve: 34.6 s median gap per sub). The Piggy-600 rides
the RC16 mount, which NINA #1 owns (PS-25): on a dual-rig night every Piggy
sub would pull the RC16 off target. The lint (sequence_lint rule
piggy-mount) covers what PhotonScript loads; a sequence loaded by hand in
NINA #2 is only visible through ninaAPI.

check() reads NINA #2's loaded sequence (GET /sequence/state, falls back to
/sequence/json; sideload.read_sequence_state) and finds every mount-moving
instruction or trigger in it (sequence_lint.mount_items). It runs when the
RC16 is about to image: at arm, and when the armer starts WATCHING a
sideloaded night (PS-136). It never loads, stops or starts anything.

A finding is recorded in <data_dir>/nina2_mount/latest.json (the Tonight's
Run chip via the armer status, and the Guiding tab's "What to change" list
read it) and pushed once per night per finding set. Config
piggyback_mount_check: "alert" (default) | "off". Never raises.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

MODES = ("alert", "off")
TITLE = "PhotonScript NINA #2 mount"


def mode(config) -> str:
    m = str(getattr(config, "piggyback_mount_check", "alert") or "alert")
    m = m.strip().lower()
    return m if m in MODES else "alert"


def enabled(config) -> bool:
    return (bool(getattr(config, "piggyback_enabled", False))
            and mode(config) != "off")


def _dir(config) -> Path:
    return Path(config.data_dir) / "nina2_mount"


def latest_path(config) -> Path:
    return _dir(config) / "latest.json"


def load_latest(config) -> dict | None:
    try:
        return json.loads(latest_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def tonight(config, now: datetime | None = None) -> dict | None:
    """The latest record when it is for tonight's night and found mount
    instructions, else None (what the chips show)."""
    from photonscript.shared.phd2_store import night_of
    rec = load_latest(config)
    if not rec or not rec.get("items"):
        return None
    try:
        if rec.get("night") != night_of(config, now or datetime.utcnow()):
            return None
    except Exception:  # noqa: BLE001
        return None
    return rec


def _save(config, rec: dict) -> None:
    try:
        d = _dir(config)
        d.mkdir(parents=True, exist_ok=True)
        latest_path(config).write_text(json.dumps(rec, indent=1), encoding="utf-8")
    except OSError as e:
        logger.warning("NINA #2 mount check not saved: %s", e)


def _signature(items: list[dict]) -> str:
    return "|".join(sorted(f"{i['label']}@{i['where']}" for i in items))


def evaluate(tree, reason: str, config, now: datetime,
             replaced: bool = False) -> dict:
    """The record for a NINA #2 tree (pure; tree None = not read)."""
    from photonscript.scheduler.sequence_lint import mount_items, mount_summary
    from photonscript.shared.phd2_store import iso_z, night_of
    items = mount_items(tree) if tree is not None else []
    looped = [i for i in items if i["in_loop"]]
    rec = {"t_utc": iso_z(now), "night": night_of(config, now),
           "reason": reason, "read": tree is not None, "items": items,
           "in_loop": len(looped), "replaced": bool(replaced),
           "severity": ("fail" if looped else "warn") if items else "pass"}
    if not items:
        rec["detail"] = ("NINA #2 holds no mount instruction" if tree is not None
                         else "NINA #2 sequence not read")
    else:
        rec["detail"] = (f"NINA #2's loaded sequence has {len(items)} mount "
                         f"instruction(s) ({len(looped)} inside a loop): "
                         + mount_summary(items))
    return rec


def message(rec: dict) -> str:
    when = {"arm": "at arm", "watch": "as the armer watches tonight's night"}.get(
        rec.get("reason"), f"({rec.get('reason')})")
    tail = (" PhotonScript's companion replaces it at pre-config "
            "(piggyback_calibrate_on_arm)." if rec.get("replaced") else
            " Remove them in NINA #2 (center once before the loop at most, "
            "never while the RC16 images) and reload.")
    head = ("moves the RC16 mount on every pass" if rec.get("in_loop")
            else "runs once: only safe while the RC16 is not imaging")
    return f"{rec['detail']} {when}: it {head}.{tail}"


async def check(config, reason: str, now: datetime | None = None,
                reader=None, push=None) -> dict | None:
    """Read NINA #2's loaded sequence and record + push mount instructions.
    reason "arm" | "watch". None when the check is off or the Piggy-600 is
    not enabled. Never raises; never writes to NINA."""
    try:
        if not enabled(config):
            return None
        from photonscript.scheduler.sideload import read_sequence_state
        from photonscript.shared.rigs import PIGGYBACK, rig_config
        now = now or datetime.utcnow()
        base = rig_config(config, PIGGYBACK).nina_base_url
        tree, err = await (reader or read_sequence_state)(base)
        replaced = (reason == "arm"
                    and bool(getattr(config, "piggyback_calibrate_on_arm", True)))
        rec = evaluate(None if err else (tree or []), reason, config, now,
                       replaced)
        if err:
            # unreadable (NINA #2 down or not started): keep the last record
            rec["error"] = err
            logger.info("NINA #2 mount check (%s): not read (%s)", reason, err)
            return rec
        prev = load_latest(config) or {}
        sig = _signature(rec["items"])
        rec["pushed"] = bool(prev.get("pushed")) and (
            prev.get("night") == rec["night"] and prev.get("signature") == sig)
        rec["signature"] = sig
        if rec["items"] and not rec["pushed"]:
            if push is None:
                from photonscript.shared.pushover import notify as push
            await push(config, message(rec), title=TITLE,
                       priority=1 if rec["in_loop"] and not replaced else 0)
            rec["pushed"] = True
        _save(config, rec)
        logger.info("NINA #2 mount check (%s): %s", reason, rec["detail"])
        return rec
    except Exception as e:  # noqa: BLE001
        logger.warning("NINA #2 mount check failed: %s", e)
        return None


def attention_item(config, now: datetime | None = None) -> dict | None:
    """The Guiding tab's "What to change" fields for tonight's finding, or
    None: severity, setting, current, desired, fix, where."""
    rec = tonight(config, now)
    if not rec:
        return None
    return {"severity": rec.get("severity") or "warn",
            "setting": "NINA #2 sequence: mount instructions",
            "current": rec.get("detail"),
            "desired": "no slew / center / park / tracking / flip in NINA #2",
            "fix": ("Remove them from NINA #2's sequence and reload (center "
                    "once before the loop at most, never while the RC16 "
                    "images). NINA #1 owns the mount."),
            "where": "NINA #2"}
