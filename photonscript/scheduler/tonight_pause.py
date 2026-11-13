"""Tonight-only goal pause (Notion P2, 2026-10-15).

PATCH /api/projects2/{id} {"paused_tonight": true} sets the goal inactive
for the current local night only; {"paused_tonight": false} brings it back
now. The dawn shutdown (Armer.dawn_shutdown -> dawn_restore, next to the
deep night override clear and the night pilot's dawn revert) sets every
goal paused that night active again, writes a run event (kind
"tonight_pause") and the morning report lists what came back.

State: <data_dir>/tonight_pause/<night>.json
  {"night", "goals": {project_id: {"target", "paused_at", "restored",
   "restored_at", "restored_by" (dawn | user | night over)}}}
The project store keeps the active flag itself, so a restart mid-night
keeps the goal paused; a night that ended without a dawn shutdown (a
disarm, a crash) is restored by restore_stale, run on the next pause / goal
list / dawn. A goal set active by hand in the meantime is left as it is;
a goal that was already inactive is never paused (and so never woken).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

KIND = "tonight_pause"


def _iso(t: datetime) -> str:
    return t.replace(microsecond=0).isoformat() + "Z"


def state_dir(cfg) -> Path:
    return Path(getattr(cfg, "data_dir", ".")) / "tonight_pause"


def state_path(cfg, night: str) -> Path:
    return state_dir(cfg) / f"{night}.json"


def load_state(cfg, night: str) -> dict:
    try:
        st = json.loads(state_path(cfg, night).read_text(encoding="utf-8"))
        if isinstance(st, dict):
            st.setdefault("night", night)
            st.setdefault("goals", {})
            return st
    except (OSError, ValueError):
        pass
    return {"night": night, "goals": {}}


def save_state(cfg, st: dict) -> None:
    from photonscript.shared.phd2_store import write_json
    write_json(state_path(cfg, st["night"]), st)


def current_night(cfg, armer=None, now: datetime | None = None) -> str:
    """The armed night (the armer's plan) when there is one, else the local
    evening date (noon to noon)."""
    plan = getattr(armer, "plan", None) if armer is not None else None
    n = plan.get("night_of") if isinstance(plan, dict) else None
    if n:
        return str(n)
    from photonscript.shared.phd2_store import night_of
    return night_of(cfg, now)


def _event(cfg, night: str, value: str, detail: str, now: datetime, **extra) -> None:
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl
        append_jsonl(events_path(cfg, night),
                     {"t": _iso(now), "rig": "rc16", "src": "photonscript",
                      "kind": KIND, "value": value, "detail": detail, **extra})
    except Exception as e:  # noqa: BLE001
        logger.warning("tonight pause event (%s) not logged: %s", value, e)


def paused_ids(cfg, night: str) -> set[str]:
    """Goals paused for `night` and not restored yet."""
    return {pid for pid, e in (load_state(cfg, night).get("goals") or {}).items()
            if not e.get("restored")}


def pause(cfg, store, project_id: str, night: str,
          now: datetime | None = None) -> tuple[bool, str]:
    """Set the goal inactive for `night`. (ok, detail). Idempotent."""
    now = now or datetime.utcnow()
    proj = store.projects.get(project_id)
    if proj is None:
        return False, "not found"
    st = load_state(cfg, night)
    e = st["goals"].get(project_id)
    if e and not e.get("restored"):
        if proj.active is not False:
            store.update(project_id, active=False)
        return True, f"{proj.target.name} already paused for tonight"
    if proj.active is False:
        return False, f"{proj.target.name} is inactive already: nothing to pause"
    store.update(project_id, active=False)
    st["goals"][project_id] = {"target": proj.target.name, "paused_at": _iso(now),
                               "restored": False}
    save_state(cfg, st)
    txt = f"{proj.target.name} paused for tonight ({night})"
    _event(cfg, night, "pause", txt, now, project=project_id)
    logger.info("tonight pause: %s", txt)
    return True, txt


def _restore_one(cfg, st: dict, pid: str, store, now: datetime, by: str) -> str:
    e = st["goals"][pid]
    proj = store.projects.get(pid)
    if proj is None:
        bit = "goal gone, nothing to restore"
    elif proj.active is False:
        store.update(pid, active=True)
        bit = "active again"
    else:
        bit = "already active again"
    e.update(restored=True, restored_at=_iso(now), restored_by=by, result=bit)
    txt = f"{e.get('target')}: {bit}"
    _event(cfg, st["night"], "restore", txt, now, project=pid, by=by)
    logger.info("tonight pause restore (%s): %s", by, txt)
    return txt


def unpause(cfg, store, project_id: str, night: str,
            now: datetime | None = None) -> tuple[bool, str]:
    """Bring a goal paused tonight back now. (ok, detail)."""
    now = now or datetime.utcnow()
    st = load_state(cfg, night)
    e = st["goals"].get(project_id)
    if not e or e.get("restored"):
        proj = store.projects.get(project_id)
        name = proj.target.name if proj else project_id
        return False, f"{name} is not paused for tonight"
    txt = _restore_one(cfg, st, project_id, store, now, "user")
    save_state(cfg, st)
    return True, txt


def restore(cfg, night: str, store, now: datetime | None = None,
            by: str = "dawn") -> list[str]:
    """Every goal paused for `night` active again. One text per goal."""
    now = now or datetime.utcnow()
    st = load_state(cfg, night)
    out = [_restore_one(cfg, st, pid, store, now, by)
           for pid, e in list(st["goals"].items()) if not e.get("restored")]
    if out:
        save_state(cfg, st)
    return out


def restore_stale(cfg, tonight: str | None, store, now: datetime | None = None) -> list[str]:
    """Restore the pauses of nights before `tonight` the dawn shutdown did
    not (a disarm, a crash). Never raises."""
    out: list[str] = []
    d = state_dir(cfg)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        if tonight and p.stem >= tonight:
            continue
        try:
            out += restore(cfg, p.stem, store, now, by="night over")
        except Exception as e:  # noqa: BLE001
            logger.warning("tonight pause stale restore %s failed: %s", p.stem, e)
    return out


def dawn_restore(cfg, night: str | None, store=None,
                 now: datetime | None = None) -> str | None:
    """Armer.dawn_shutdown hook: the night's pauses (and any older ones)
    restored. A shutdown step line, None when nothing was paused. Never
    raises."""
    if not night or not any(state_dir(cfg).glob("*.json")):
        return None
    try:
        if store is None:
            from photonscript.scheduler.app import get_store
            store = get_store()
        done = restore_stale(cfg, night, store, now) + restore(cfg, night, store, now)
        return ("tonight-only pauses restored: " + "; ".join(done)) if done else None
    except Exception as e:  # noqa: BLE001
        logger.warning("tonight pause dawn restore failed: %s", e)
        return "tonight-only pause restore FAILED (see the log)"


def morning_lines(cfg, date: str) -> list[str]:
    """The morning report: the goals paused that night that came back (and
    any not restored yet). Empty when nothing was paused."""
    goals = (load_state(cfg, date).get("goals") or {}) if date else {}
    back = [f"{e.get('target')}" + (f" ({e['result']})" if e.get("result")
                                    and e["result"] != "active again" else "")
            for e in goals.values()
            if e.get("restored") and e.get("restored_by") != "user"]
    hand = [str(e.get("target")) for e in goals.values()
            if e.get("restored") and e.get("restored_by") == "user"]
    left = [str(e.get("target")) for e in goals.values() if not e.get("restored")]
    out = []
    if back:
        out.append("Paused for tonight only, back at dawn: " + ", ".join(back))
    if hand:
        out.append("Paused for tonight, un-paused by hand: " + ", ".join(hand))
    if left:
        out.append("Paused for tonight only, NOT restored yet: " + ", ".join(left))
    return out
