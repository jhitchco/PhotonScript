"""PS-254 after-action report routes.

GET  /report/{date}                 the night's report page (images inline)
GET  /api/runs/{date}/timeline.png  the timeline bars alone
POST /api/report/{date}/send        build and send it now (push / email per
                                    after_action_mode; ignores the latch)

PS-260 desktop pickup (the desktop sends the email through Gmail):
GET  /api/report/{date}/email-bundle[?early=1]
        {date, subject, text, html, images, report_url, sent, early}; html
        uses cid:att0..attN in images order (timeline first); early=1 is the
        astro-dawn build without the dawn flats / shutdown lines
GET  /api/report/{date}/email-bundle/image/{n}[?early=1]   image n, raw bytes
POST /api/report/{date}/sent  {"via": "desktop-gmail", "message_id": "..."}
        records the send in the latch: the dawn loop will not email again
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Body
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
    return await aa.send(_cfg(), date, force=True)


def _public(b: dict) -> dict:
    return {k: v for k, v in b.items() if not k.startswith("_")}


@router.get("/api/report/{date}/email-bundle")
async def report_email_bundle(date: str, early: bool = False):
    from photonscript.scheduler import after_action as aa
    b = await asyncio.to_thread(aa.email_bundle, _cfg(), date, early)
    return JSONResponse(_public(b))


@router.get("/api/report/{date}/email-bundle/image/{n}")
async def report_email_bundle_image(date: str, n: int, early: bool = False):
    from photonscript.scheduler import after_action as aa
    hit = await asyncio.to_thread(aa.bundle_image, _cfg(), date, n, early)
    if not hit:
        return JSONResponse(status_code=404, content={"detail": f"no image {n} in this bundle"})
    data, mime = hit
    return Response(data, media_type=mime, headers={"Cache-Control": "no-store"})


@router.post("/api/report/{date}/sent")
async def report_sent(date: str, body: dict[str, Any] | None = Body(None)):
    from photonscript.scheduler import after_action as aa
    body = body if isinstance(body, dict) else {}
    rec = await asyncio.to_thread(aa.mark_email, _cfg(), date,
                                  str(body.get("via") or "desktop-gmail"),
                                  str(body.get("message_id") or ""), bool(body.get("early")))
    return {"date": date, "sent": rec}
