"""PS-111 mosaic planner v2: one mosaic goal made of panel goals.

GET    /mosaic                       the Mosaic page (define, preview, create,
                                     list)
GET    /api/mosaics                  every mosaic goal: definition, per-panel
                                     progress, next panel, companion goal,
                                     preview geometry (scheduler/mosaic.py
                                     summaries)
GET    /api/mosaics/suggest?name=    a starting definition (the approved M31
                                     2 x 2 LRGB recipe for M31)
GET    /api/mosaics/preview?name=&ra_hours=&dec_degrees=&rows=&cols=
         &overlap_pct=&rotation=camera|<deg>&major_axis_pa=
                                     panel layout + rotation verdict + a DSS2
                                     cutout URL to draw it over (read-only)
POST   /api/mosaics                  create: body = the definition (see
                                     mosaic.build_panels) plus "companion"
                                     (name, catalog id or project id of the
                                     Piggy-600 goal) and "dry_run"; 400 on
                                     errors, nothing saved on a dry run
PATCH  /api/mosaics/{id}             hours_per_panel, filter_mix, priority,
                                     active: applied to every panel
DELETE /api/mosaics/{id}             removes the panel goals (subs, Library
                                     and history are untouched)

v1, kept for old links: GET /api/mosaic/plan, POST /api/mosaic/create (one
unrelated goal per panel). Moved here from app.py (PS-8 router split).
"""
from __future__ import annotations

import asyncio
import math

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from photonscript.scheduler import mosaic as mz

router = APIRouter()


def _app():
    from photonscript.scheduler import app
    return app


def _store():
    return _app().get_store()


@router.get("/mosaic", response_class=HTMLResponse)
async def mosaic_page(request: Request):
    a = _app()
    return a.templates.TemplateResponse(request, "mosaic.html", {
        "version": a.VERSION, "observatory": a.get_config().get_observatory()})


# --- v2 -------------------------------------------------------------------------

def _summaries() -> list[dict]:
    return mz.summaries(_app().get_config(), _store().projects.values())


@router.get("/api/mosaics")
async def api_mosaics():
    return {"mosaics": await asyncio.to_thread(_summaries)}


@router.get("/api/mosaics/suggest")
def api_mosaics_suggest(name: str = "M31"):
    s = mz.suggest(_app().get_config(), name)
    if s is None:
        return JSONResponse(status_code=404, content={
            "detail": f"{name!r} is not in the catalog; give RA/Dec on the "
                      "Mosaic page instead"})
    return s


def _cutout(ra_hours: float, dec: float, span_w: float, span_h: float) -> dict:
    fov = max(span_w * 1.35, span_h * 1.35 * 4 / 3)
    return {"url": ("https://alasky.cds.unistra.fr/hips-image-services/"
                    "hips2fits?hips=CDS%2FP%2FDSS2%2Fcolor&width=800"
                    f"&height=600&fov={fov:.4f}&projection=TAN&coordsys=icrs"
                    f"&ra={ra_hours * 15.0:.5f}&dec={dec:.5f}&format=jpg"),
            "fov_w_deg": round(fov, 4), "fov_h_deg": round(fov * 0.75, 4),
            "credit": "DSS2 color via CDS hips2fits"}


@router.get("/api/mosaics/preview")
def api_mosaics_preview(name: str = "Mosaic", ra_hours: float = 0.0,
                        dec_degrees: float = 0.0, rows: int = 2,
                        cols: int = 2, overlap_pct: float = 15.0,
                        rotation: str = "camera",
                        major_axis_pa: float | None = None):
    from photonscript.scheduler.refimage import rig_fov
    cfg = _app().get_config()
    try:
        rot_in = "camera" if rotation in ("", "camera") else float(rotation)
    except ValueError:
        return JSONResponse(status_code=400, content={
            "detail": "rotation is 'camera' or an angle in degrees"})
    rot = mz.resolve_rotation(cfg, rot_in, major_axis_pa)
    fov = rig_fov(cfg, "rc16")
    rows = max(1, min(int(rows), 4))
    cols = max(1, min(int(cols), 4))
    lay = mz.layout(name, ra_hours, dec_degrees, rows, cols,
                    max(0.0, min(50.0, overlap_pct)), rot["pa_deg"],
                    fov["w_arcmin"] / 60.0, fov["h_arcmin"] / 60.0)
    piggy = rig_fov(cfg, "piggyback")
    ppa = mz.camera_pa(cfg, "piggyback")["pa_deg"] or 0.0
    return {"layout": lay, "rotation": rot,
            "rc16_fov_arcmin": [fov["w_arcmin"], fov["h_arcmin"]],
            "piggy": {"corners": mz.frame_corners(
                0.0, 0.0, piggy["w_arcmin"] / 60.0, piggy["h_arcmin"] / 60.0,
                ppa), "w_arcmin": piggy["w_arcmin"],
                "h_arcmin": piggy["h_arcmin"], "pa_deg": ppa},
            "preview": _cutout(ra_hours, dec_degrees, lay["span_w_deg"],
                               lay["span_h_deg"])}


def _plan_json(p) -> dict:
    return {"name": p.target.name, "ra_hours": p.target.ra_hours,
            "dec_degrees": p.target.dec_degrees,
            "plans": [{"filter": e.filter_type.value, "count": e.count,
                       "exposure_s": e.exposure_seconds, "rig": e.rig}
                      for e in p.exposure_plans]}


def _create(body: dict) -> tuple[int, dict]:
    a = _app()
    cfg = a.get_config()
    store = _store()
    projects = list(store.projects.values())
    spec = dict(body)
    comp = None
    if body.get("companion"):
        comp = mz.find_companion(projects, body["companion"])
    spec["companion_id"] = comp.id if comp is not None else None
    if body.get("rotation") not in (None, "", "camera"):
        try:
            spec["rotation"] = float(body["rotation"])
        except (TypeError, ValueError):
            return 400, {"errors": ["rotation is 'camera' or an angle in "
                                    "degrees"], "warnings": []}
    try:
        built = mz.build_panels(cfg, spec, existing_names=[
            p.target.name for p in projects])
    except (TypeError, ValueError) as e:
        return 400, {"errors": [f"bad definition: {e}"], "warnings": []}
    warnings = list(built["warnings"])
    if body.get("companion") and comp is None:
        warnings.append(f"companion {body['companion']!r} is not a goal with "
                        "a Piggy-600 plan: passenger subs will credit nothing")
    out = {"id": built["id"], "dry_run": bool(body.get("dry_run")),
           "errors": built["errors"], "warnings": warnings,
           "rotation": built["rotation"],
           "layout": {k: v for k, v in built["layout"].items()
                      if k != "panels"},
           "companion": ({"id": comp.id, "name": comp.target.name}
                         if comp is not None else None),
           "panels": [_plan_json(p) for p in built["panels"]]}
    if built["errors"]:
        return 400, out
    if body.get("dry_run"):
        return 200, out
    for p in built["panels"]:
        store.projects[p.id] = p
        a._projects[p.id] = p
    store.save()
    out["created"] = [p.id for p in built["panels"]]
    return 200, out


@router.post("/api/mosaics")
async def api_mosaics_create(request: Request):
    body = await request.json()
    code, out = await asyncio.to_thread(_create, body)
    return JSONResponse(status_code=code, content=out)


def _panels(mid: str) -> list:
    return mz.panels_by_mosaic(_store().projects.values()).get(mid, [])


def _patch(mid: str, body: dict) -> tuple[int, dict]:
    panels = _panels(mid)
    if not panels:
        return 404, {"detail": f"no mosaic {mid}"}
    store = _store()
    hours = body.get("hours_per_panel")
    for p in panels:
        store.update(p.id,
                     priority=(int(body["priority"])
                               if body.get("priority") is not None else None),
                     budget_hours=float(hours) if hours else None,
                     active=(bool(body["active"])
                             if body.get("active") is not None else None),
                     filter_mix=body.get("filter_mix"))
        if hours:
            p.mosaic = {**p.mosaic, "layout": {
                **(p.mosaic.get("layout") or {}),
                "hours_per_panel": float(hours)}}
    store.save()
    return 200, {"ok": True, "panels": [p.target.name for p in panels]}


@router.patch("/api/mosaics/{mid}")
async def api_mosaics_patch(mid: str, request: Request):
    body = await request.json()
    code, out = await asyncio.to_thread(_patch, mid, body)
    return JSONResponse(status_code=code, content=out)


@router.delete("/api/mosaics/{mid}")
def api_mosaics_delete(mid: str):
    panels = _panels(mid)
    if not panels:
        return JSONResponse(status_code=404, content={"detail": f"no mosaic {mid}"})
    store = _store()
    for p in panels:
        store.delete(p.id)
        _app()._projects.pop(p.id, None)
    return {"ok": True, "removed": [p.target.name for p in panels]}


# --- v1 (moved from app.py unchanged) --------------------------------------------

@router.get("/api/mosaic/plan")
async def api_mosaic_plan(name: str = "Mosaic", ra_hours: float = 0.0,
                          dec_degrees: float = 0.0, rows: int = 2,
                          cols: int = 2, overlap_pct: float = 15.0,
                          rotation_deg: float = 0.0,
                          focal_length_mm: float = 3248.0):
    """Panel grid + a DSS2 sky cutout (CDS hips2fits) to draw it over.

    Panel FOV is derived from the ASI2600/IMX571 sensor (23.5 x 15.7 mm) at
    the requested focal length, so native (3248 mm) vs reducer (600 mm) framing
    can be previewed. Default is the RC16 native focal length.
    """
    fl = max(50.0, float(focal_length_mm))
    SENSOR_W_MM, SENSOR_H_MM = 23.5, 15.7
    fov_w_panel = math.degrees(2 * math.atan(SENSOR_W_MM / (2 * fl)))
    fov_h_panel = math.degrees(2 * math.atan(SENSOR_H_MM / (2 * fl)))
    plan = mz.plan_panels(name, ra_hours, dec_degrees, rows, cols,
                          overlap_pct, rotation_deg,
                          fov_w=fov_w_panel, fov_h=fov_h_panel)
    plan["focal_length_mm"] = fl
    plan["panel_fov_deg"] = {"w": round(fov_w_panel, 4),
                             "h": round(fov_h_panel, 4)}
    plan["preview"] = _cutout(ra_hours, dec_degrees, plan["span_w_deg"],
                              plan["span_h_deg"])
    return plan


@router.post("/api/mosaic/create")
async def api_mosaic_create(request: Request):
    """v1: one imaging project per panel (no grouping, no order)."""
    from photonscript.shared.models import CelestialTarget
    a = _app()
    body = await request.json()
    store = _store()
    budget = float(body.get("budget_hours_per_panel", 8.0))
    created = []
    for p in body.get("panels", []):
        target = CelestialTarget(
            name=p["name"], ra_hours=float(p["ra_hours"]),
            dec_degrees=float(p["dec_degrees"]),
            object_type=body.get("object_type", "nebula"))
        proj = store.add_from_target(target, budget_hours=budget)
        a._projects[proj.id] = proj
        created.append({"id": proj.id, "name": p["name"]})
    return {"ok": True, "created": created}
