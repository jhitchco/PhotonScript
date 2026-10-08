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
    store_ledger(config, projects, data)     -> (project, Ledger, new_version)
    ledgers_for(config, project_id, rig="")  -> [Ledger], newest first
    summary(config, projects)                -> one row per goal + rig
    candidates(config, projects)             -> PS-31 watcher facts
    decide_ask(config, ask_id, decision)     status only, never the goal

PS-142 (campaign review):

    goal_status(config, projects)            Acquiring / Ready to process /
                                             Processing / Processed (vN) /
                                             Published (vN) per goal + rig
    mark_processing(config, projects, ...)   integrate-watch start / end
                                             notice (the desktop lock state)
    review(config, store, project_id)        latest verdict + notes, asks
    proposal(config, store, ask_id)          the PATCH /api/projects2 body a
                                             plan-changing ask maps to and
                                             the exposure-plan diff it makes
                                             (computed on a copy; nothing
                                             saved)
    new_version_message(led), morning_note(config)   the ready ping

"New data since the last integration" = approved subs of the goal and rig
(scheduler/sub_index) whose file is not in the ledger's sub list (every
Library light the run considered, used or not), in hours.

Nothing here touches projects.json: asks are proposals for the weekly
review. PS-142: an approved plan-changing ask is applied by the browser
through PATCH /api/projects2/{id} (ProjectStore.update) after a confirm,
then marked "applied" here.
"""

from __future__ import annotations

import copy
import json
import logging
import threading
from datetime import datetime, timezone
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
    for the campaign) or pydantic.ValidationError (bad payload). Returns
    (project, ledger, new_version): new_version is False for a re-post of
    a run already stored. A stored ledger ends that goal + rig's
    processing notice (PS-142)."""
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
        _proc_update(config, lambda m: m.pop(f"{p.id}|{led.rig}", None))
    return p, led, same is None


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
        "astrobin_url": astrobin_url(led),
        "imported": bool(m.get("imported")),
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


def _ask_files(config):
    root = ledgers_root(config)
    return sorted(root.glob("*/v*.json")) if root.is_dir() else []


def find_ask(config, ask_id: str):
    """(project_id, ledger file, Ledger, Ask) holding ask_id, else None."""
    for f in _ask_files(config):
        try:
            led = L.load(f)
        except Exception:  # noqa: BLE001
            continue
        for a in led.review.asks:
            if a.id == ask_id:
                return f.parent.name, f, led, a
    return None


def decide_ask(config, ask_id: str, decision: str, patch: dict | None = None) -> dict | None:
    """Mark one ask approved / declined / applied. Changes the ledger file
    only (never the goal). None when no ledger holds that ask. PS-142:
    `patch` (the PATCH /api/projects2 body the browser applied) is kept on
    the ask as applied_patch, with decided_at."""
    status = {"approve": "approved", "decline": "declined",
              "applied": "applied", "reopen": "open"}.get(decision)
    if status is None:
        raise ValueError("decision must be approve, decline, applied or reopen")
    with _lock:
        hit = find_ask(config, ask_id)
        if hit is None:
            return None
        pid, f, led, a = hit
        a.status = status
        setattr(a, "decided_at", L.now_iso())
        if patch:
            setattr(a, "applied_patch", dict(patch))
        L.save(f, led)
        return {"ok": True, "project_id": pid, "version": led.version,
                "ask": json.loads(a.model_dump_json())}


# --- PS-142: processing notice (integrate-watch lock state) ---------------------

PROCESSING_STALE_H = 12.0   # a notice older than this is ignored (crashed run)


def _proc_path(config) -> Path:
    return ledgers_root(config) / "processing.json"


def _proc_read(config) -> dict:
    try:
        d = json.loads(_proc_path(config).read_text(encoding="ascii"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _proc_update(config, fn) -> None:
    """Apply fn to the notice map and write it back (caller holds _lock)."""
    d = _proc_read(config)
    before = json.dumps(d, sort_keys=True)
    fn(d)
    if json.dumps(d, sort_keys=True) == before:
        return
    path = _proc_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="ascii")
    tmp.replace(path)


def _age_h(iso: str, now: datetime | None = None) -> float | None:
    try:
        t = datetime.strptime(str(iso)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None
    return ((now or datetime.now(timezone.utc)) - t).total_seconds() / 3600.0


def mark_processing(config, projects, campaign: str, rig: str, state: str,
                    reason: str = "") -> dict:
    """integrate-watch says it started ("start") or finished ("end") a run
    for this campaign + rig. Raises UnknownCampaign / ValueError."""
    p = resolve_project(campaign, projects)
    if p is None:
        raise UnknownCampaign(campaign)
    rig = str(rig or "").strip().lower()
    if rig not in ("piggyback", "rc16"):
        raise ValueError(f"unknown rig {rig!r}")
    if state not in ("start", "end"):
        raise ValueError("state must be start or end")
    key = f"{p.id}|{rig}"
    entry = {"project_id": p.id, "target": p.target.name, "rig": rig,
             "started_at": L.now_iso(), "reason": str(reason or "")[:300]}

    def _apply(m: dict) -> None:
        if state == "start":
            m[key] = entry
        else:
            m.pop(key, None)
    with _lock:
        _proc_update(config, _apply)
    return {"ok": True, "project_id": p.id, "rig": rig, "state": state}


def processing_for(config, project_id: str, rig: str,
                   now: datetime | None = None) -> dict | None:
    e = _proc_read(config).get(f"{project_id}|{rig}")
    if not isinstance(e, dict):
        return None
    age = _age_h(e.get("started_at", ""), now)
    if age is None or age > PROCESSING_STALE_H:
        return None
    return e


# --- PS-142: campaign status per goal --------------------------------------------

def astrobin_url(led: L.Ledger) -> str:
    """The AstroBin page recorded on a ledger (publish block, else machine)."""
    pub = led.publish or {}
    ab = pub.get("astrobin") if isinstance(pub.get("astrobin"), dict) else {}
    for v in (ab.get("url"), pub.get("astrobin_url"), pub.get("url"),
              (led.machine.get("astrobin") or {}).get("url")):
        if v and str(v).startswith("http"):
            return str(v)
    return ""


STATE_LABELS = {"acquiring": "Acquiring", "ready": "Ready to process",
                "processing": "Processing", "processed": "Processed",
                "published": "Published"}
STATE_RANK = {"processing": 4, "ready": 3, "published": 2, "processed": 1, "acquiring": 0}


def ready_to_process(c: dict, thr: dict) -> tuple[bool, str]:
    """The PS-31 watcher's rule without its desktop half (min interval,
    queued ledgers): goal met with no ledger yet, the first_h hours, or
    new_data_h new since the last ledger."""
    if not c.get("approved_subs"):
        return False, "no approved subs"
    goal = c.get("goal") or {}
    last = c.get("last")
    if last is None:
        if float(goal.get("pct") or 0) >= 100:
            return True, f"goal met ({goal.get('hours_done')} of {goal.get('hours_goal')} h)"
        first = float(thr.get("first_h") or 0)
        if first > 0 and float(c.get("approved_h") or 0) >= first:
            return True, f"{c.get('approved_h')} h approved (first run at {first:g} h)"
        return False, f"goal {float(goal.get('pct') or 0):.0f}%"
    need = float(thr.get("new_data_h") or 0)
    new_h = float(c.get("new_data_h") or 0)
    if need > 0 and new_h >= need:
        return True, f"{new_h:.1f} h new since v{last.get('version')}"
    return False, f"{new_h:.1f} h new since v{last.get('version')}"


def status_of(c: dict, thr: dict, processing: dict | None = None,
              published: dict | None = None) -> dict:
    """One goal + rig's state. Pure: c = a candidates() row (readiness
    optional), thr = thresholds(), processing = processing_for(),
    published = {version, url} of the newest ledger with an AstroBin URL."""
    last = c.get("last")
    v = last.get("version") if last else None
    missing = (c.get("readiness") or {}).get("calibration_missing") or []
    out = {"rig": c.get("rig"), "version": v, "calibration_missing": missing,
           "new_data_h": c.get("new_data_h"), "goal": c.get("goal") or {},
           "astrobin_url": (published or {}).get("url", ""),
           "published_version": (published or {}).get("version")}

    def st(state, detail, label=None):
        out.update(state=state, label=label or STATE_LABELS[state], detail=detail)
        return out
    if processing:
        return st("processing", "integrate-watch started a run at "
                  + str(processing.get("started_at", ""))[:16].replace("T", " ") + "Z"
                  + (f" ({processing['reason']})" if processing.get("reason") else ""))
    ready, why = ready_to_process(c, thr)
    if ready and thr.get("require_calibration") and missing:
        ready, why = False, why + "; waiting for calibration: " + ", ".join(missing)
    elif ready and missing:
        why += "; calibration missing: " + ", ".join(missing)
    if ready:
        return st("ready", why)
    if last and last.get("astrobin_url"):
        return st("published", (last.get("headline") or "") + "; " + why,
                  f"Published (v{v})")
    if last:
        pub = (f"; v{published['version']} published" if published and published.get("url")
               else "")
        return st("processed", (last.get("headline") or "") + pub + "; " + why,
                  f"Processed (v{v})")
    g = c.get("goal") or {}
    return st("acquiring", f"{g.get('hours_done', 0)} of {g.get('hours_goal', 0)} h "
                           f"({g.get('pct', 0)}%); {why}")


def _published(config, project_id: str, rig: str) -> dict | None:
    for led in ledgers_for(config, project_id, rig):
        url = astrobin_url(led)
        if url:
            return {"version": led.version, "url": url}
    return None


def goal_status(config, projects, *, with_calibration: bool = False) -> dict:
    """Per goal: one state per rig and the goal's chip (the most advanced
    rig: Processing > Ready > Published > Processed > Acquiring). Active
    goals come from candidates(); paused goals with ledgers are listed from
    their ledgers only."""
    projects = list(projects or ())
    data = candidates(config, projects, with_calibration=with_calibration
                      or thresholds(config)["require_calibration"])
    thr = data["thresholds"]
    rows = list(data["candidates"])
    seen = {(r["project_id"], r["rig"]) for r in rows}
    for p in projects:
        for led in ledgers_for(config, p.id):
            if (p.id, led.rig) in seen:
                continue
            seen.add((p.id, led.rig))
            rows.append({"project_id": p.id, "target": p.target.name, "rig": led.rig,
                         "goal": _rig_goal(p, led.rig), "approved_subs": 0,
                         "last": ledger_brief(led), "new_data_h": 0.0})
    by_pid: dict = {}
    for c in rows:
        pid, rig = c["project_id"], c["rig"]
        s = status_of(c, thr, processing_for(config, pid, rig), _published(config, pid, rig))
        g = by_pid.setdefault(pid, {"project_id": pid, "target": c.get("target"), "rigs": []})
        g["rigs"].append(s)
    for g in by_pid.values():
        top = max(g["rigs"], key=lambda s: STATE_RANK[s["state"]])
        g.update(state=top["state"], label=top["label"], rig=top["rig"], detail=top["detail"])
    return {"goals": list(by_pid.values()), "thresholds": thr}


# --- PS-142: review panel + ask proposals ---------------------------------------

_RC16_FILTERS = ("L", "R", "G", "B", "Ha", "OIII", "SII")


def _ask_rig(project, a: L.Ask) -> str | None:
    from photonscript.scheduler.readiness import project_rigs
    if a.rig in ("piggyback", "rc16"):
        return a.rig
    f = str(a.filter or "")
    if f.upper() == "OSC":
        return "piggyback"
    if f in _RC16_FILTERS:
        return "rc16"
    rigs = project_rigs(project)
    return rigs[0] if len(rigs) == 1 else None


def _plan_hours(e) -> float:
    s = e.count * e.exposure_seconds
    if e.hdr_short_seconds and e.hdr_short_count:
        s += e.hdr_short_count * e.hdr_short_seconds
    return s / 3600.0


def ask_patch(project, a: L.Ask) -> tuple[dict | None, str]:
    """The PATCH /api/projects2/{id} body a plan-changing ask maps to, or
    (None, why) for an ask that only changes status."""
    t = a.type
    if t == "more_hours":
        h = float(a.hours or 0)
        if h <= 0:
            return None, "no hours on the ask"
        rig = _ask_rig(project, a)
        if rig is None:
            return None, "the ask names no rig (goal has both): apply by hand"
        if rig == "piggyback":
            cur = sum(_plan_hours(e) for e in project.exposure_plans
                      if (e.rig or "rc16") == "piggyback")
            return {"osc_hours": round(cur + h, 2)}, f"Piggy-600 OSC goal +{h:g} h"
        rc = [e for e in project.exposure_plans if (e.rig or "rc16") == "rc16"]
        f = a.filter if a.filter in _RC16_FILTERS else None
        if not rc:
            body = {"rc16_hours": round(h, 2)}
            if f:
                body["filter_mix"] = {f: 100}
            return body, f"new RC16 plan of {h:g} h"
        if not f:
            return ({"budget_hours": round(float(project.budget_hours) + h, 1)},
                    f"RC16 budget +{h:g} h (same mix)")
        hours: dict = {}
        for e in rc:
            hours[e.filter_type.value] = hours.get(e.filter_type.value, 0.0) + _plan_hours(e)
        hours[f] = hours.get(f, 0.0) + h
        tot = sum(hours.values())
        mix = {k: round(v / tot * 100, 1) for k, v in hours.items() if v > 0}
        return ({"budget_hours": round(float(project.budget_hours) + h, 1), "filter_mix": mix},
                f"RC16 {f} +{h:g} h (budget +{h:g} h, mix re-weighted)")
    if t == "short_subs":
        rig = _ask_rig(project, a)
        f = a.filter if a.filter in _RC16_FILTERS else None
        if rig != "rc16" or not f or not a.exposure_s:
            return None, "short subs apply to an RC16 filter with exposure_s; apply by hand"
        hdr = {k: float(v) for k, v in (project.hdr or {}).items()}
        hdr[f] = float(a.exposure_s)
        return {"hdr": hdr}, (f"RC16 {f} HDR short set at {a.exposure_s:g} s "
                              "(the count is the fixed HDR short count)")
    if t == "reframe":
        dr = a.driving_rig
        if dr not in ("piggyback", "rc16"):
            return None, "no driving_rig on the ask"
        if dr == getattr(project, "driving_rig", None):
            return None, f"{dr} already drives the pointing"
        return {"driving_rig": dr}, f"{dr} centers the target"
    return None, "status only (no plan change)"


def _plans_view(project) -> dict:
    out = {}
    for e in project.exposure_plans:
        k = f"{e.rig or 'rc16'} {e.filter_type.value}"
        out[k] = {"count": e.count, "exposure_s": e.exposure_seconds,
                  "hours": round(_plan_hours(e), 2),
                  "short": (f"{e.hdr_short_count} x {e.hdr_short_seconds:g} s"
                            if e.hdr_short_seconds and e.hdr_short_count else "")}
    return out


def plan_diff(before, after) -> list[dict]:
    """Rows of plans that differ (added, removed or changed) plus the goal
    fields a PATCH may move (budget, driving rig)."""
    rows = []
    b, a = _plans_view(before), _plans_view(after)
    for k in sorted(set(b) | set(a)):
        if b.get(k) != a.get(k):
            rows.append({"plan": k, "before": b.get(k), "after": a.get(k)})
    for fld in ("budget_hours", "driving_rig"):
        x, y = getattr(before, fld, None), getattr(after, fld, None)
        if x != y:
            rows.append({"plan": fld, "before": x, "after": y})
    return rows


def preview_update(store, project_id: str, patch: dict):
    """ProjectStore.update on a deep copy of the goal, nothing saved: what
    PATCH /api/projects2/{id} with `patch` would make. (before, after)."""
    proj = store.projects[project_id]
    shadow = copy.copy(store)
    shadow.projects = {project_id: proj.model_copy(deep=True)}
    shadow.save = lambda: None
    return proj, shadow.update(project_id, **patch)


def proposal(config, store, ask_id: str) -> dict | None:
    """What Approve of one ask would do: the PATCH body and the plan diff
    (plan_change false = status only). None when no ledger holds it."""
    hit = find_ask(config, ask_id)
    if hit is None:
        return None
    pid, _f, led, a = hit
    proj = store.projects.get(pid)
    out = {"ask": json.loads(a.model_dump_json()), "project_id": pid,
           "version": led.version, "plan_change": False, "patch": None, "diff": []}
    if proj is None:
        out["note"] = "the goal no longer exists"
        return out
    patch, note = ask_patch(proj, a)
    out["note"] = note
    if patch:
        try:
            before, after = preview_update(store, pid, patch)
            out.update(plan_change=True, patch=patch, diff=plan_diff(before, after))
        except ValueError as e:
            out["note"] = f"cannot apply: {e}"
    return out


def review(config, store, project_id: str) -> dict:
    """The campaign review panel: per rig the latest ledger (headline,
    AstroBin URL), the newest verdict + notes (with the version they belong
    to), and every ask of every version (open first), each with what
    Approve would change."""
    proj = store.projects.get(project_id)
    out = {"project_id": project_id, "target": proj.target.name if proj else "", "rigs": []}
    all_l = ledgers_for(config, project_id)
    for rig in sorted({x.rig for x in all_l}, key=lambda r: (r != "piggyback", r)):
        mine = [x for x in all_l if x.rig == rig]
        rv = next((x for x in mine if x.review.verdict or x.review.notes), None)
        asks = []
        for x in mine:
            for a in x.review.asks:
                d = json.loads(a.model_dump_json())
                patch, note = ask_patch(proj, a) if proj else (None, "")
                d.update(version=x.version, plan_change=bool(patch), effect=note)
                asks.append(d)
        asks.sort(key=lambda d: (d["status"] != "open", -d["version"]))
        out["rigs"].append({
            "rig": rig, "latest": ledger_brief(mine[0]), "versions": len(mine),
            "verdict": rv.review.verdict if rv else "", "notes": rv.review.notes if rv else "",
            "review_version": rv.version if rv else None,
            "decided_by_jeremy": bool(rv and rv.review.decided_by_jeremy),
            "asks": asks, "open_asks": sum(1 for d in asks if d["status"] == "open")})
    return out


# --- PS-142: the ready ping --------------------------------------------------------

def new_version_message(led: L.Ledger) -> str:
    """'M31 v3 integrated: 9.4 h, packet ready' (ASCII)."""
    ok = (led.machine.get("integration") or {}).get("ok")
    head = f"{led.campaign} v{led.version}"
    if ok is False:
        return f"{head} integration FAILED"
    if ok is None:
        return f"{head} staged: {led.hours:.1f} h (not integrated)"
    packet = (led.machine.get("astrobin") or {}).get("packet")
    return f"{head} integrated: {led.hours:.1f} h" + (", packet ready" if packet else "")


def morning_note(config, hours: float = 24.0, now: datetime | None = None) -> str:
    """One line for the dawn summary push: ledgers stored in the last
    `hours` (imports excluded), '' when none."""
    items = []
    for f in _ask_files(config):
        try:
            led = L.load(f)
        except Exception:  # noqa: BLE001
            continue
        if led.machine.get("imported"):
            continue
        age = _age_h(led.reported_at, now)
        if age is not None and 0 <= age <= hours:
            items.append((led.reported_at, new_version_message(led)))
    if not items:
        return ""
    return "Integrations: " + "; ".join(m for _t, m in sorted(items))
