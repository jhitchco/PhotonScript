"""PS-136 watch endpoints: the armer WATCHES a sideloaded night (no dispatch).

GET  /api/arm/watch        watch state, tonight's RC16 sideload, whether
                           NINA #1 runs anything (read only)
POST /api/arm/watch        start WATCHING now (the dashboard "Watch this
                           night" button); 409 when the armer is busy or
                           tonight's dawn shutdown time has passed
POST /api/arm/watch/stop   stop WATCHING (NINA keeps running the sequence;
                           the auto-adopt never picks that sideload again)

Armer.start_watch / Armer.maybe_adopt hold the logic. Kept out of app.py (PS-8).
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


def _app():
    from photonscript.scheduler import app
    return app


@router.get("/api/arm/watch")
async def api_watch_status():
    from photonscript.scheduler import sideload as sd
    from photonscript.scheduler.auto_armer import sideload_tonight
    armer = _app().get_armer()
    cfg = _app().get_config()
    tree, err = await sd.read_sequence_state(cfg.nina_base_url)
    return {"state": armer.state, "watch": armer.status().get("watch"),
            "auto": bool(getattr(cfg, "watch_sideload_auto", True)),
            "dawn_action": str(getattr(cfg, "watch_dawn_action", "verify")),
            "sideload": sideload_tonight(cfg, rig="rc16"),
            "nina_running": None if err else sd.nina_running(tree)[:5],
            "nina_error": err}


@router.post("/api/arm/watch")
async def api_watch_start():
    res = await _app().get_armer().start_watch("button")
    if not res.get("ok"):
        return JSONResponse(status_code=409, content=res)
    return res


@router.post("/api/arm/watch/stop")
async def api_watch_stop():
    from photonscript.scheduler.armer import WATCH_STATE
    armer = _app().get_armer()
    if armer.state != WATCH_STATE:
        return JSONResponse(status_code=409, content={
            "detail": f"armer is {armer.state}, not WATCHING"})
    return await armer.disarm()
