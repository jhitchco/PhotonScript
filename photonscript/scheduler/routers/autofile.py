"""PS-157 endpoints: the dawn filing of a night (scheduler/dawn_autofile.py).

GET  /api/runs/{date}/autofile   the night's record (what was named,
                                 auto-approved and filed), 404 without one
POST /api/runs/{date}/autofile   run the dawn filing now (attribution, the
                                 pointing and slew passes, auto-approve when
                                 auto_approve_at_dawn is on, Library build);
                                 {"approve": false} files without approving

Handlers lazily import get_config to avoid an import cycle with app.py.
"""
from __future__ import annotations

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


@router.get("/api/runs/{date}/autofile")
def api_autofile_record(date: str):
    from photonscript.scheduler.dawn_autofile import load_record
    rec = load_record(_cfg(), date)
    if rec is None:
        return JSONResponse(status_code=404, content={
            "detail": f"no dawn filing record for {date}"})
    return rec


@router.post("/api/runs/{date}/autofile")
def api_autofile_run(date: str, payload: dict | None = Body(default=None)):
    """Plain def: FastAPI runs it in a worker thread (solves, Library)."""
    from photonscript.scheduler.dawn_autofile import file_night
    approve = (payload or {}).get("approve")
    return file_night(_cfg(), date, push=False,
                      approve=None if approve is None else bool(approve))
