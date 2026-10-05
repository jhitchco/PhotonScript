"""Field rotation report (PS-97). Report only.

GET /api/rotation/report?nights=14&split=&end=&ma=&me=&night_hours=6&refresh=
    measured field rotation per night, rig and target block (plate-solve
    position angles and star-sidecar registration) against the rotation the
    polar error predicts (MA / ME from the TPoint record, or ma / me
    arcmin here), a verdict, and the corner cost over a sub and a night.
    split = YYYY-MM-DD (a night) or a UTC timestamp: blocks before / after
    it are summarized separately (default the TPoint record's model date).
    refresh=1 re-measures the nights (stored solves only, never ASTAP).

Handlers lazily import get_config to avoid an import cycle with app.py.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


@router.get("/api/rotation/report")
async def api_rotation_report(nights: int = 14, split: str = "", end: str = "",
                              ma: float | None = None, me: float | None = None,
                              night_hours: float = 6.0, refresh: bool = False):
    from photonscript.scheduler import field_rotation as fr
    if not 0.5 <= float(night_hours) <= 14.0:
        return JSONResponse(status_code=400, content={
            "ok": False, "note": "night_hours must be 0.5 to 14"})
    return await asyncio.to_thread(
        fr.report, _cfg(), nights, end or None, split or None, ma, me,
        refresh, night_hours)
