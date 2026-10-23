"""Night pilot state (scheduler/night_pilot.py).

GET /api/night/pilot   mode, tonight's actions (what, because, result),
                       the tonight-only goal changes (ledger), what was
                       learned, and the one-line summary the dashboard's
                       Tonight's Run card shows.

Read only: the background loop (night_pilot.run_pilot) decides and acts.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/api/night/pilot")
def api_night_pilot(date: str = ""):
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler import night_pilot as npil
    cfg = app_mod.get_config()
    night = date or None
    if not night:
        try:
            plan = getattr(app_mod.get_armer(), "plan", None) or {}
            night = plan.get("night_of") or None
        except Exception:  # noqa: BLE001
            night = None
    return npil.status(cfg, night)
