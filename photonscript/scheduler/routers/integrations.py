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
                                        reopen}: the ask's status only; the
                                        goal (projects.json) never changes
                                        here.

Storage and the summaries live in scheduler/integrations.py. Handlers are
plain defs (threadpool) and import app helpers lazily (PS-8 router split).
"""
from __future__ import annotations

from fastapi import APIRouter, Body
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


@router.post("/api/integrations")
def api_integrations_post(payload: dict = Body(...)):
    try:
        p, led = integ.store_ledger(_cfg(), _projects(), payload)
    except ValidationError as e:
        return JSONResponse(status_code=422, content={
            "ok": False, "detail": e.errors(include_url=False, include_context=False,
                                            include_input=False)})
    except integ.UnknownCampaign as e:
        return JSONResponse(status_code=404, content={
            "ok": False, "detail": f"no goal for campaign {str(e)!r}"})
    return {"ok": True, "project_id": p.id, "target": p.target.name,
            "version": led.version, "run": led.run,
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
        res = integ.decide_ask(_cfg(), ask_id, str(payload.get("decision") or ""))
    except ValueError as e:
        return JSONResponse(status_code=422, content={"ok": False, "detail": str(e)})
    if res is None:
        return JSONResponse(status_code=404, content={"ok": False,
                                                      "detail": f"no ask {ask_id!r}"})
    return res
