"""PS-170 observatory app lifecycle endpoints.

GET  /api/apps/status   which apps answer (NINA #1 / #2, PHD2, TheSky and
                        its mount), whether closing them is safe now and
                        why not, the launch settings, the script's last runs
POST /api/apps/report   deploy\\observatory-apps.ps1 posts each run here;
                        a run that asks for it pages once per mode per day

The service never starts, closes or kills an app: the script does, in
jeremy's session. The logic lives in scheduler/app_lifecycle.py.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Request

router = APIRouter()
logger = logging.getLogger(__name__)


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


def _armer():
    from photonscript.scheduler.app import get_armer
    return get_armer()


@router.get("/api/apps/status")
async def api_apps_status():
    from photonscript.scheduler import app_lifecycle as al
    return await al.status(_cfg(), _armer())


@router.post("/api/apps/report")
async def api_apps_report(request: Request):
    from photonscript.scheduler import app_lifecycle as al
    from photonscript.shared.pushover import notify
    cfg = _cfg()
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    rec = al.record_report(cfg, body)
    paged = False
    if al.should_page(cfg, rec):
        paged = await notify(cfg, al.page_text(rec),
                             title="PhotonScript observatory apps", priority=1)
    logger.info("apps %s report: ok=%s acted=%s %s", rec["mode"], rec["ok"],
                rec["acted"], rec["message"])
    return {"recorded": rec, "paged": bool(paged)}
