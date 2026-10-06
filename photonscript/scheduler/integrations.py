"""PS-33 / PS-31: integration ledgers on the scheduler (store + summaries).

The desktop integrator posts one ledger per processing version
(shared/ledger.py). They are kept as files:

    <data_dir>/ledgers/<project_id>/v<NNN>.json

Keyed by the run name: a re-post of the same run replaces its file and
keeps its version (the review half can be added later this way); a new run
takes the version it asks for when that is free, else the next free one.
A re-post with an empty review keeps the stored review, and an ask the
scheduler already decided keeps its status.

    resolve_project(name, projects)          goal for a campaign name
    store_ledger(config, projects, data)     -> (project, Ledger)
    ledgers_for(config, project_id, rig="")  -> [Ledger], newest first
    summary(config, projects)                -> one row per goal + rig
    candidates(config, projects)             -> PS-31 watcher facts
    decide_ask(config, ask_id, decision)     status only, never the goal

"New data since the last integration" = approved subs of the goal and rig
(scheduler/sub_index) whose file is not in the ledger's sub list (every
Library light the run considered, used or not), in hours.

Nothing here touches projects.json: asks are proposals for the weekly
review (Jeremy applies them through the goal editor).
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

from photonscript.shared import ledger as L

logger = logging.getLogger(__name__)

_lock = threading.Lock()


class UnknownCampaign(LookupError):
    pass


def ledgers_root(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "ledgers"


def resolve_project(name: str, projects):
    """The goal whose target is `name` (catalog ids and aliases count)."""
    from photonscript.scheduler.sub_index import _match_target
    from photonscript.shared.target_names import target_key
    projects = list(projects or ())
    k = _match_target(name, projects)
    hit = next((p for p in projects if target_key(p.target.name) == k), None)
    if hit is None:
        n = str(name or "").strip().lower()
        hit = next((p for p in projects
                    if n and n in (p.target.name.lower(),
                                   str(p.target.catalog_id or "").lower())), None)
    return hit


def _pdir(config, project_id: str) -> Path:
    return ledgers_root(config) / str(project_id)


def _read_dir(d: Path) -> list[L.Ledger]:
    out = []
    if not d.is_dir():
        return out
    for f in sorted(d.glob("v*.json")):
        try:
            out.append(L.load(f))
        except Exception as e:  # noqa: BLE001 - one bad file never hides the rest
            logger.warning("ledger %s unreadable: %s", f, e)
    return out


def ledgers_for(config, project_id: str, rig: str = "") -> list[L.Ledger]:
    """Stored ledgers of a goal (one rig, or all), newest version first."""
    out = [x for x in _read_dir(_pdir(config, project_id)) if not rig or x.rig == rig]
    return sorted(out, key=lambda x: (x.version, x.created_at), reverse=True)


def _merge_review(new: L.Ledger, old: L.Ledger | None) -> None:
    if old is None:
        return
    if new.review.is_empty():
        new.review = old.review
        return
    decided = {a.id: a.status for a in old.review.asks if a.status != "open"}
    for a in new.review.asks:
        if a.status == "open" and a.id in decided:
            a.status = decided[a.id]


def store_ledger(config, projects, data: dict):
    """Validate and store one posted ledger. Raises UnknownCampaign (no goal
    for the campaign) or pydantic.ValidationError (bad payload)."""
    led = L.parse(data)
    p = resolve_project(led.campaign, projects)
    if p is None:
        raise UnknownCampaign(led.campaign)
    d = _pdir(config, p.id)
    with _lock:
        stored = [x for x in _read_dir(d) if x.rig == led.rig]
        same = next((x for x in stored if x.run == led.run), None)
        used = {x.version for x in stored if x is not same}
        if same is not None:
            led.version = same.version
        elif led.version <= 0 or led.version in used:
            led.version = max(used | {0}) + 1
        _merge_review(led, same)
        led.reported = True
        led.reported_at = led.reported_at or L.now_iso()
        led.machine.setdefault("project_id", p.id)
        fname = f"v{led.version:03d}.json" if led.rig == "piggyback" \
            else f"v{led.version:03d}_{led.rig}.json"
        L.save(d / fname, led)
    return p, led


# --- summaries ----------------------------------------------------------------

def _approved_rows(config, projects, name: str, rig: str) -> list[dict]:
    from photonscript.scheduler import sub_index
    try:
        return sub_index.rows(config, projects, target=name, rig=rig,
                              verdict=sub_index.APPROVED)
    except Exception as e:  # noqa: BLE001 - summaries never break a page
        logger.debug("integrations: sub rows for %s unavailable: %s", name, e)
        return []


def new_data_s(led: L.Ledger | None, rows: list[dict]) -> float:
    """Seconds of approved subs not in the ledger's sub list. Without a
    ledger every approved sub is new. A run limited with --since ignores
    nights before it."""
    if led is None:
        return float(sum(r.get("exp_s") or 0 for r in rows))
    have = led.sub_files
    since = str(((led.machine.get("options") or {}).get("since")) or "")
    tot = 0.0
    for r in rows:
        if since and str(r.get("date") or "") < since:
            continue
        if Path(str(r.get("file") or "")).name in have:
            continue
        tot += float(r.get("exp_s") or 0)
    return tot


def ledger_brief(led: L.Ledger) -> dict:
    m = led.machine
    ab = m.get("astrobin") or {}
    integ = m.get("integration") or {}
    return {
        "version": led.version, "run": led.run, "rig": led.rig,
        "created_at": led.created_at, "date": (led.created_at or "")[:10],
        "hours": round(led.hours, 2), "headline": L.headline(led),
        "integrated_ok": integ.get("ok"),
        "finish_ok": (m.get("finish") or {}).get("ok"),
        "run_dir": m.get("run_dir", ""),
        "packet": ab.get("packet", ""), "csv": ab.get("csv", ""),
        "final": m.get("final") or "",
        "calibration": (m.get("calibration") or {}).get("status", ""),
        "verdict": led.review.verdict,
        "open_asks": len(led.open_asks()),
        "trigger": (m.get("trigger") or {}).get("reason", ""),
    }


def _rig_goal(project, rig: str) -> dict:
    goal = done = 0.0
    for e in project.exposure_plans:
        if (getattr(e, "rig", "rc16") or "rc16") != rig:
            continue
        g = e.count * e.exposure_seconds
        goal += g
        done += min(e.long_seconds_done(), g)
        if e.hdr_short_seconds and e.hdr_short_count:
            gs = e.hdr_short_count * e.hdr_short_seconds
            goal += gs
            done += min(e.hdr_short_acquired * e.hdr_short_seconds, gs)
    return {"hours_goal": round(goal / 3600, 2), "hours_done": round(done / 3600, 2),
            "pct": round(done / goal * 100) if goal else 0}


def summary(config, projects) -> list[dict]:
    """Goal cards and the Targets page: per goal + rig with a ledger, the
    latest version and the new data since it."""
    from photonscript.scheduler.readiness import project_rigs
    projects = list(projects or ())
    out = []
    for p in projects:
        all_l = ledgers_for(config, p.id)
        if not all_l:
            continue
        for rig in sorted({x.rig for x in all_l} | set(project_rigs(p))):
            mine = [x for x in all_l if x.rig == rig]
            if not mine:
                continue
            last = mine[0]
            nd = new_data_s(last, _approved_rows(config, projects, p.target.name, rig))
            out.append({"project_id": p.id, "target": p.target.name, "rig": rig,
                        "versions": len(mine), "latest": ledger_brief(last),
                        "new_data_h": round(nd / 3600, 2)})
    return out


def _owed_items(config, rig: str, projects) -> list[str]:
    try:
        from photonscript.scheduler.calibration_owed import owed_report
        rep = owed_report(config, rig, projects=projects)
        return list((rep.get("rigs") or [{}])[0].get("items") or [])
    except Exception as e:  # noqa: BLE001 - report only
        logger.debug("integrations: calibration owed (%s) unavailable: %s", rig, e)
        return []


def candidates(config, projects, *, with_calibration: bool = True) -> dict:
    """PS-31 watcher facts, one row per ACTIVE goal + rig: goal progress of
    that rig's plans, approved hours, the last ledger, new data since it,
    readiness (calibration missing for the planned filters) and the rig's
    calibration owed list. The watcher decides; this only reports."""
    from photonscript.scheduler.readiness import (calibration_context,
                                                  calibration_missing,
                                                  project_rigs, target_readiness)
    projects = list(projects or ())
    ctxs: dict = {}
    owed: dict = {}
    rows_out = []
    for p in projects:
        if not p.active:
            continue
        for rig in project_rigs(p):
            rows = _approved_rows(config, projects, p.target.name, rig)
            mine = ledgers_for(config, p.id, rig)
            last = mine[0] if mine else None
            ready: dict = {}
            if with_calibration:
                try:
                    if rig not in ctxs:
                        ctxs[rig] = calibration_context(config, rig, max_age_s=300)
                    r = target_readiness(config, p, ctxs[rig])
                    ready = {"ready": r["ready"], "lights_in_library": r["lights_in_library"],
                             "calibration_missing": calibration_missing(r)}
                except Exception as e:  # noqa: BLE001
                    ready = {"error": str(e)}
                if rig not in owed:
                    owed[rig] = _owed_items(config, rig, projects)
            rows_out.append({
                "project_id": p.id, "target": p.target.name,
                "catalog_id": p.target.catalog_id, "rig": rig,
                "goal": _rig_goal(p, rig),
                "approved_h": round(sum(r.get("exp_s") or 0 for r in rows) / 3600, 2),
                "approved_subs": len(rows),
                "last": ledger_brief(last) if last else None,
                "new_data_h": round(new_data_s(last, rows) / 3600, 2),
                "readiness": ready,
                "calibration_owed": owed.get(rig, []),
            })
    return {"candidates": rows_out, "thresholds": thresholds(config)}


def thresholds(config) -> dict:
    """The watcher thresholds set on the scope (System page, Integration)."""
    return {
        "rigs": [r.strip() for r in str(getattr(config, "integrate_watch_rigs", "piggyback")
                                        or "").split(",") if r.strip()],
        "new_data_h": float(getattr(config, "integrate_watch_new_data_h", 1.0)),
        "first_h": float(getattr(config, "integrate_watch_first_h", 0.0)),
        "min_interval_h": float(getattr(config, "integrate_watch_min_interval_h", 12.0)),
        "require_calibration": bool(getattr(config, "integrate_watch_require_calibration",
                                            False)),
    }


def decide_ask(config, ask_id: str, decision: str) -> dict | None:
    """Mark one ask approved / declined / applied. Changes the ledger file
    only (never the goal). None when no ledger holds that ask."""
    status = {"approve": "approved", "decline": "declined",
              "applied": "applied", "reopen": "open"}.get(decision)
    if status is None:
        raise ValueError("decision must be approve, decline, applied or reopen")
    root = ledgers_root(config)
    if not root.is_dir():
        return None
    with _lock:
        for f in sorted(root.glob("*/v*.json")):
            try:
                led = L.load(f)
            except Exception:  # noqa: BLE001
                continue
            for a in led.review.asks:
                if a.id == ask_id:
                    a.status = status
                    L.save(f, led)
                    return {"ok": True, "project_id": f.parent.name, "version": led.version,
                            "ask": json.loads(a.model_dump_json())}
    return None
