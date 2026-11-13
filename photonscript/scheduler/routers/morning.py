"""PS-166 endpoint: the morning report card (scheduler/morning_report.py).

GET /api/morning/report?date=YYYY-MM-DD   the card for that night (default:
                                         the newest night with a subs log)

Plain def: FastAPI runs it in a worker thread (the calibration owed report
scans the subs logs). Handlers lazily import get_config to avoid an import
cycle with app.py.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


@router.get("/api/morning/report")
def api_morning_report(date: str = ""):
    from photonscript.scheduler.morning_report import report_card
    return report_card(_cfg(), date or None)
