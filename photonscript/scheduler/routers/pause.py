"""PS-64 operator Pause / Resume of tonight's night.

POST /api/arm/pause    {"piggy": "keep"|"pause", "when": "after_exposure"|"now"}
                       RUNNING: PAUSED_OPERATOR; NINA #1's sequence is
                       stopped after its current sub (tracking, cooler and
                       PHD2 stay on, nothing parks); the Piggy-600 keeps
                       imaging unless piggy="pause". WATCHING: alert-only
                       (guiding alerts muted, nothing sent to NINA).
                       409 in any other state.
POST /api/arm/resume   PAUSED_OPERATOR: re-dispatch the remainder (the
                       armer's mid-night re-dispatch); before the stop
                       happened it only cancels the wait. WATCHING: alerts
                       back on. 409 when not paused or too little dark left.
POST /api/arm/restart  PS-143 Restart tonight from now {"when"}: re-plan the
                       remainder from the current goals and re-dispatch it
                       (RUNNING: after the current sub; PAUSED_OPERATOR: now).
                       409 while WATCHING or a calibration capture runs.

Armer.pause / resume / restart hold the logic. Kept out of app.py (PS-8).
"""
from __future__ import annotations

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

router = APIRouter()


def _armer():
    from photonscript.scheduler.app import get_armer
    return get_armer()


@router.post("/api/arm/pause")
async def api_pause(payload: dict | None = Body(default=None)):
    body = payload or {}
    res = await _armer().pause(piggy=str(body.get("piggy") or "keep"),
                               when=str(body.get("when") or "after_exposure"))
    if not res.get("ok"):
        return JSONResponse(status_code=409, content=res)
    return res


@router.post("/api/arm/resume")
async def api_resume():
    res = await _armer().resume()
    if not res.get("ok"):
        return JSONResponse(status_code=409, content=res)
    return res


@router.post("/api/arm/restart")
async def api_restart(payload: dict | None = Body(default=None)):
    """PS-143 Restart tonight from now: {"when": "after_exposure"|"now"}.
    RUNNING: NINA #1 stops after its current sub, then the remainder is
    re-planned from the current goals and re-dispatched (the Resume path;
    nothing warms, parks or turns a cooler off). PAUSED_OPERATOR: re-plans
    and re-dispatches now. 409 while WATCHING (use the sideload preview),
    while a calibration capture job runs, in any other state, or with too
    little dark left."""
    body = payload or {}
    res = await _armer().restart(when=str(body.get("when") or "after_exposure"))
    if not res.get("ok"):
        return JSONResponse(status_code=409, content=res)
    return res
