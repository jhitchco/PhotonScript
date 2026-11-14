"""PS-255 pre-arm readiness routes.

GET  /prearm             the latest check as a page (Run now button)
GET  /api/prearm         the latest check (JSON)
POST /api/prearm/run     run it now (push=0 by default; connect=1 connects
                         every device first, like the scheduled runs)
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


def _armer():
    from photonscript.scheduler.app import get_armer
    return get_armer()


@router.get("/prearm", response_class=HTMLResponse)
async def prearm_page():
    from photonscript.scheduler import prearm
    return HTMLResponse(prearm.page_html(prearm.latest(_cfg())))


@router.get("/api/prearm")
async def prearm_latest():
    from photonscript.scheduler import prearm
    return prearm.latest(_cfg()) or {"ok": False, "detail": "no pre-arm check yet"}


@router.post("/api/prearm/run")
async def prearm_run(push: int = 0, connect: int = 1):
    from photonscript.scheduler import prearm
    return await prearm.run(_cfg(), _armer(), trigger="manual", push=bool(push),
                            connect=bool(connect))
