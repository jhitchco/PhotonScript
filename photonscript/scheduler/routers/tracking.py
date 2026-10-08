"""PS-168 endpoint: the nightly tracking drift (scheduler/tracking_drift.py).

GET /api/tracking/drift?date=YYYY-MM-DD&nights=N   per window and night
    summary (RA / Dec drift "/min, detrended RMS, smear per sub length at
    each rig scale, mode, excluded windows and why), the trend over the last
    N nights keyed by the TPoint model, and the PS-169 recommended unguided
    sub length per rig. Default date: the newest guide log's night.

Plain def: FastAPI runs it in a worker thread (it parses guide logs).
Handlers lazily import get_config to avoid an import cycle with app.py.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


@router.get("/api/tracking/drift")
def api_tracking_drift(date: str = "", nights: int | None = None):
    from photonscript.scheduler.tracking_drift import report
    return report(_cfg(), date, nights)
