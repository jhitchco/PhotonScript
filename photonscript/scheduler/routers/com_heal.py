"""PS-256 COM heal routes.

GET  /api/com-heal       today's attempts, the task (registered or not, via
                         schtasks /query) and the mount port's status
POST /api/com-heal/run   dry_run=1 by default (says what it would do); only
                         dry_run=0 starts the task. Same gates as prearm:
                         never while a night runs, the daily limit, one page.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


def _armer():
    from photonscript.scheduler.app import get_armer
    return get_armer()


@router.get("/api/com-heal")
async def com_heal_status():
    from photonscript.scheduler import com_heal
    return await com_heal.status(_cfg())


@router.post("/api/com-heal/run")
async def com_heal_run(dry_run: int = 1):
    from photonscript.scheduler import com_heal
    return await com_heal.heal(_cfg(), _armer(), trigger="api", dry_run=bool(dry_run))
