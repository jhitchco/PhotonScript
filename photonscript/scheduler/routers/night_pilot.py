"""Night pilot state (scheduler/night_pilot.py).

GET /api/night/pilot   mode, tonight's actions (what, because, result),
                       the tonight-only goal changes (ledger), what was
                       learned, and the one-line summary the dashboard's
                       Tonight's Run card shows.
GET /api/night/focuser-guard  the focuser position-loss guard's state.

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


@router.get("/api/night/focuser-guard")
def api_focuser_guard(date: str = ""):
    """The focuser position-loss guard (scheduler/focuser_guard.py): mode,
    per rig the last connected reading, tonight's resets and the steps."""
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler import focuser_guard as fg
    return fg.status(app_mod.get_config(), date or None)
