"""Queue an arm recipe for a future night (2026-11-20, scheduler/recipe_queue.py).

GET    /api/arm/queue                 the queued nights
POST   /api/arm/queue {"night", "recipe", "recipe_opts"}
                                      validate (as POST /api/arm) and queue;
                                      400 on a bad night or recipe
DELETE /api/arm/queue?night=YYYY-MM-DD

The first arm of that night without its own recipe (manual, noon or evening
auto-arm) takes it. Kept out of app.py (PS-8).
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Query
from photonscript.shared.json_safe import SafeJSONResponse as JSONResponse

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


@router.get("/api/arm/queue")
def api_queue_list():
    from photonscript.scheduler import recipe_queue
    return {"queue": recipe_queue.listing(_cfg())}


@router.post("/api/arm/queue")
def api_queue_add(payload: dict = Body(...)):
    from photonscript.scheduler import recipe_queue
    entry, err = recipe_queue.queue(_cfg(), payload.get("night"),
                                    payload.get("recipe"), payload.get("recipe_opts"))
    if err:
        return JSONResponse(status_code=400, content={"detail": err})
    return {"ok": True, "queued": entry, "queue": recipe_queue.listing(_cfg())}


@router.delete("/api/arm/queue")
def api_queue_remove(night: str = Query(...)):
    from photonscript.scheduler import recipe_queue
    removed = recipe_queue.remove(_cfg(), night)
    return {"ok": True, "removed": removed, "queue": recipe_queue.listing(_cfg())}
