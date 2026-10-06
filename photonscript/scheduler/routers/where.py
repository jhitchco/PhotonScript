"""PS-64 live "where is it" panel data.

GET /api/night/where   target (mosaic panel), filter, sub n of N, exposure
                       progress and seconds left, next action, guiding mode /
                       PHD2, cooler per rig, roof / safety, Piggy-600 (sub,
                       exposure, split guard), dawn / shutdown countdown.

Read only (ninaAPI GETs on both NINAs). Logic: scheduler/where_panel.py.
"""
from __future__ import annotations

from fastapi import APIRouter

router = APIRouter()


@router.get("/api/night/where")
async def api_night_where():
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler.where_panel import collect
    try:
        projects = list(app_mod._stored_projects().values())
    except Exception:  # noqa: BLE001
        projects = []
    tel = app_mod._telescope_state.model_dump(mode="json")
    return await collect(app_mod.get_config(), app_mod.get_armer(), tel=tel,
                         projects=projects)
