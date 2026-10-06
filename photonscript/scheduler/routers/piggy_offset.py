"""PS-26 Piggy-600 boresight offset and centering.

GET /api/piggy-offset?refresh=&nights=
    the stored RC16-to-Piggy-600 offset per pier side (median, scatter,
    rotation, per-night medians, recent pairs) and, for every Piggy-600-
    driven project, the center the generator would use tonight.
    refresh=true re-measures from the pointing sidecars (stored solves
    only, never ASTAP) and saves <data_dir>/piggy_offset.json.
PUT /api/piggy-offset/frame-center/{project_id}
    {"ra_hours": h, "dec_degrees": d} sets the point the Piggy-600 frame is
    centered on for that project (e.g. between M31 and M110); {} clears it
    (center on the target).

Handlers lazily import get_config / get_store to avoid an import cycle
with app.py.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


def _projects_view(cfg, store) -> list[dict]:
    from photonscript.scheduler import piggy_offset as po
    try:
        from photonscript.scheduler.app import get_store
        projs = list(get_store().projects.values())
    except Exception:  # noqa: BLE001
        projs = []
    out = []
    for p in projs:
        if getattr(p, "driving_rig", po.RC16) != po.PIGGYBACK:
            continue
        fc = po.frame_center_of(p)
        plan = po.center_plan(cfg, p.target.ra_hours, p.target.dec_degrees,
                              store, fc)
        out.append({"id": p.id, "name": p.target.name, "active": p.active,
                    "ra_hours": p.target.ra_hours,
                    "dec_degrees": p.target.dec_degrees,
                    "frame_center": ({"ra_hours": fc[0], "dec_degrees": fc[1]}
                                     if fc else None),
                    "plan": plan})
    return out


@router.get("/api/piggy-offset")
async def api_piggy_offset(refresh: bool = False, nights: int | None = None):
    from photonscript.scheduler import piggy_offset as po
    cfg = _cfg()
    if refresh:
        store = await asyncio.to_thread(po.refresh, cfg, nights)
    else:
        store = po.load(cfg)
    return {"ok": True, "mode": po.mode(cfg),
            "min_pairs": int(getattr(cfg, "piggy_center_min_pairs", 6)),
            "max_shift_arcmin": float(getattr(cfg, "piggy_center_max_shift_arcmin", 90.0)),
            "store": store, "projects": _projects_view(cfg, store)}


@router.put("/api/piggy-offset/frame-center/{project_id}")
async def api_piggy_frame_center(project_id: str, body: dict = Body(default={})):
    from photonscript.scheduler.app import get_store
    store = get_store()
    proj = store.projects.get(project_id)
    if proj is None:
        return JSONResponse(status_code=404, content={"ok": False,
                                                      "note": "project not found"})
    ra, dec = (body or {}).get("ra_hours"), (body or {}).get("dec_degrees")
    if ra is None and dec is None:
        proj.frame_center_ra_hours = proj.frame_center_dec_degrees = None
    else:
        try:
            ra, dec = float(ra), float(dec)
        except (TypeError, ValueError):
            return JSONResponse(status_code=400, content={
                "ok": False, "note": "ra_hours and dec_degrees must be numbers"})
        if not (0.0 <= ra < 24.0 and -90.0 <= dec <= 90.0):
            return JSONResponse(status_code=400, content={
                "ok": False, "note": "ra_hours 0..24, dec_degrees -90..90"})
        proj.frame_center_ra_hours, proj.frame_center_dec_degrees = ra, dec
    store.save()
    return {"ok": True, "id": project_id,
            "frame_center_ra_hours": proj.frame_center_ra_hours,
            "frame_center_dec_degrees": proj.frame_center_dec_degrees}
