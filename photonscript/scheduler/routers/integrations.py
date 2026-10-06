"""PS-33 integrator ledger + PS-31 watcher facts (scheduler side).

POST /api/integrations                  one ledger (shared/ledger.py) from the
                                        desktop integrator. 200 {ok,
                                        project_id, version, open_asks,
                                        headline}; 404 unknown campaign; 422
                                        bad payload. Idempotent per run.
GET  /api/integrations?name=&rig=       a goal's ledgers, newest first
GET  /api/integrations/summary          per goal + rig: latest version,
                                        headline, packet path, new data since
                                        (goal cards, Targets page)
GET  /api/integrations/candidates       PS-31: what the desktop watcher
                                        decides on (goal progress, last
                                        ledger, new data, readiness,
                                        calibration owed). Read-only.
POST /api/integrations/asks/{ask_id}    {decision: approve|decline|applied|
                                        reopen, patch?}: the ask's status
                                        only; the goal (projects.json) never
                                        changes here.

PS-142 (campaign review):
GET  /api/integrations/status           per goal: Acquiring / Ready to
     ?calibration=false                 process / Processing / Processed (vN)
                                        / Published (vN), one per rig + the
                                        goal's chip
GET  /api/integrations/review           the review panel of one goal: latest
     ?project_id=                       verdict + notes, every ask with what
                                        Approve would change
GET  /api/integrations/asks/{id}/proposal
                                        the PATCH /api/projects2 body a
                                        plan-changing ask maps to and the
                                        exposure-plan diff (preview on a
                                        copy; the browser applies it through
                                        PATCH /api/projects2 after a confirm)
POST /api/integrations/processing       {campaign, rig, state: start|end,
                                        reason?}: integrate-watch started /
                                        finished a run (status Processing)

A new ledger version (not a re-post, not an import) sends one Pushover
"M31 v3 integrated: 9.4 h, packet ready"; the dawn "Night complete" push
lists the last 24 h of them.

Storage and the summaries live in scheduler/integrations.py. Handlers are
plain defs (threadpool) and import app helpers lazily (PS-8 router split).
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Body
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from photonscript.scheduler import integrations as integ

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


def _projects() -> list:
    from photonscript.scheduler.app import get_store
    try:
        return list(get_store().projects.values())
    except Exception:  # noqa: BLE001
        return []


async def _ping(config, led) -> None:
    """PS-142: the ready ping for a new ledger version (never raises)."""
    try:
        from photonscript.shared.pushover import notify
        await notify(config, integ.new_version_message(led),
                     title=f"PhotonScript {led.campaign} integrated")
    except Exception:  # noqa: BLE001 - a push never fails the post
        pass


@router.post("/api/integrations")
def api_integrations_post(background: BackgroundTasks, payload: dict = Body(...)):
    try:
        p, led, new = integ.store_ledger(_cfg(), _projects(), payload)
    except ValidationError as e:
        return JSONResponse(status_code=422, content={
            "ok": False, "detail": e.errors(include_url=False, include_context=False,
                                            include_input=False)})
    except integ.UnknownCampaign as e:
        return JSONResponse(status_code=404, content={
            "ok": False, "detail": f"no goal for campaign {str(e)!r}"})
    if new and not led.machine.get("imported"):
        background.add_task(_ping, _cfg(), led)
    return {"ok": True, "project_id": p.id, "target": p.target.name,
            "version": led.version, "run": led.run, "new_version": new,
            "open_asks": len(led.open_asks()), "headline": integ.L.headline(led)}


@router.get("/api/integrations")
def api_integrations_list(name: str, rig: str = ""):
    projects = _projects()
    p = integ.resolve_project(name, projects)
    if p is None:
        return JSONResponse(status_code=404, content={
            "detail": f"no goal for {name!r}", "ledgers": []})
    return {"project_id": p.id, "target": p.target.name,
            "ledgers": [x.dump() for x in integ.ledgers_for(_cfg(), p.id, rig)]}


@router.get("/api/integrations/summary")
def api_integrations_summary():
    return {"integrations": integ.summary(_cfg(), _projects())}


@router.get("/api/integrations/candidates")
def api_integrations_candidates(calibration: bool = True):
    return integ.candidates(_cfg(), _projects(), with_calibration=calibration)


@router.post("/api/integrations/asks/{ask_id}")
def api_integrations_ask(ask_id: str, payload: dict = Body(default={})):
    try:
        patch = payload.get("patch")
        res = integ.decide_ask(_cfg(), ask_id, str(payload.get("decision") or ""),
                               patch=patch if isinstance(patch, dict) else None)
    except ValueError as e:
        return JSONResponse(status_code=422, content={"ok": False, "detail": str(e)})
    if res is None:
        return JSONResponse(status_code=404, content={"ok": False,
                                                      "detail": f"no ask {ask_id!r}"})
    return res


def _store():
    from photonscript.scheduler.app import get_store
    return get_store()


@router.get("/api/integrations/status")
def api_integrations_status(calibration: bool = False):
    return integ.goal_status(_cfg(), _projects(), with_calibration=calibration)


@router.get("/api/integrations/review")
def api_integrations_review(project_id: str):
    store = _store()
    if project_id not in store.projects:
        return JSONResponse(status_code=404, content={"detail": f"no goal {project_id!r}"})
    return integ.review(_cfg(), store, project_id)


@router.get("/api/integrations/asks/{ask_id}/proposal")
def api_integrations_ask_proposal(ask_id: str):
    res = integ.proposal(_cfg(), _store(), ask_id)
    if res is None:
        return JSONResponse(status_code=404, content={"ok": False,
                                                      "detail": f"no ask {ask_id!r}"})
    return res


@router.post("/api/integrations/processing")
def api_integrations_processing(payload: dict = Body(...)):
    try:
        return integ.mark_processing(_cfg(), _projects(), str(payload.get("campaign") or ""),
                                     str(payload.get("rig") or ""),
                                     str(payload.get("state") or ""),
                                     str(payload.get("reason") or ""))
    except integ.UnknownCampaign as e:
        return JSONResponse(status_code=404, content={
            "ok": False, "detail": f"no goal for campaign {str(e)!r}"})
    except ValueError as e:
        return JSONResponse(status_code=422, content={"ok": False, "detail": str(e)})
