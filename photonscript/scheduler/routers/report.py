"""PS-254 after-action report routes.

GET  /report/{date}                 the night's report page (images inline)
GET  /api/runs/{date}/timeline.png  the timeline bars alone
POST /api/report/{date}/send        build and send it now (push / email per
                                    after_action_mode; ignores the latch)
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, Response

from photonscript.shared.json_safe import SafeJSONResponse as JSONResponse

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


@router.get("/report/{date}", response_class=HTMLResponse)
async def report_page(date: str):
    from photonscript.scheduler import after_action as aa
    rep = await asyncio.to_thread(aa.build, _cfg(), date)
    return HTMLResponse(aa.page_html(rep))


@router.get("/api/runs/{date}/timeline.png")
async def timeline_png(date: str, w: int = 1400):
    from photonscript.scheduler import after_action as aa
    png = await asyncio.to_thread(aa.timeline_png, _cfg(), date, min(max(int(w), 600), 2400))
    if not png:
        return JSONResponse(status_code=404, content={"detail": "no timeline for this night"})
    return Response(png, media_type="image/png", headers={"Cache-Control": "max-age=300"})


@router.post("/api/report/{date}/send")
async def report_send(date: str):
    from photonscript.scheduler import after_action as aa
    return await aa.send(_cfg(), date)
