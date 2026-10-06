"""PS-124 catalog endpoints: any target by RA/Dec, and the user catalog.

POST /api/projects2/custom   body {"text": "NGC 604 01:34:33 +30:47"} or
                             {"name", "ra_hours", "dec_degrees"} plus optional
                             "type", "size_arcmin", "months", "hours",
                             "budget_hours". Saves the target to
                             <data_dir>/user_catalog.json (same name replaces)
                             and creates its goal with the size-aware
                             defaults. 400 when the text has no usable
                             coordinates.
GET  /api/catalog/user       the user catalog rows
GET  /api/catalog/lookup?q=  what the Add box would do with q: the catalog
                             row and its creation defaults, or the parsed
                             coordinates (read-only)

POST /api/projects2/from_catalog stays in app.py and shares the helpers in
scheduler/catalog.py. Kept out of app.py (PS-8).
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from photonscript.scheduler import catalog

router = APIRouter()

COORD_HELP = ("Use a catalog name, or a name plus RA/Dec: "
              "'NGC 604 01:34:33 +30:47' or 'NGC 604 1.5758 30.783' "
              "(RA in hours, Dec in degrees)")


def _app():
    from photonscript.scheduler import app
    return app


@router.on_event("startup")
async def _load_user_catalog():
    """Hand the user catalog to astronomy before anything plans from it."""
    await asyncio.to_thread(catalog.load_user_catalog, _app().get_config())


def _entry_from_body(body: dict) -> dict:
    """The user catalog row a /custom body asks for (ValueError if none)."""
    text = str(body.get("text") or "").strip()
    if text:
        parsed = catalog.parse_target_text(text)
        if parsed is None:
            raise ValueError(f"no RA/Dec in '{text}'. {COORD_HELP}")
        name, ra, dec = parsed
    else:
        if body.get("ra_hours") is None or body.get("dec_degrees") is None:
            raise ValueError(COORD_HELP)
        name = str(body.get("name") or "").strip()
        ra, dec = float(body["ra_hours"]), float(body["dec_degrees"])
    return {"name": name or catalog.coord_name(ra, dec),
            "catalog_id": str(body.get("catalog_id") or ""),
            "ra": ra, "dec": dec,
            "type": body.get("type") or "custom",
            "size": body.get("size_arcmin"),
            "months": body.get("months"),
            "hours": body.get("hours")}


@router.post("/api/projects2/custom")
async def api_project_custom(request: Request):
    app = _app()
    body = await request.json()
    try:
        entry = _entry_from_body(body)
        cfg = app.get_config()
        row = await asyncio.to_thread(catalog.save_user_entry, cfg, entry)
    except (TypeError, ValueError) as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    store = app.get_store()
    proj, d = catalog.create_project(store, row, cfg,
                                     body.get("budget_hours"))
    app._projects[proj.id] = proj
    app._dashboard_cache.clear()     # tonight's picker picks the row up
    out = app._project_json(proj)
    out["catalog_defaults"] = d
    out["user_catalog_row"] = row
    return out


@router.get("/api/catalog/user")
def api_user_catalog():
    return {"targets": catalog.load_user_catalog(_app().get_config())}


@router.get("/api/catalog/lookup")
def api_catalog_lookup(q: str = ""):
    from photonscript.shared.astronomy import find_catalog_entry
    cfg = _app().get_config()
    entry = find_catalog_entry(q)
    if entry is not None:
        return {"match": "catalog", "entry": entry,
                "defaults": catalog.creation_defaults(entry, cfg)}
    try:
        parsed = catalog.parse_target_text(q)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})
    if parsed is None:
        return JSONResponse(status_code=404, content={
            "detail": f"'{q}' not in catalog. {COORD_HELP}"})
    name, ra, dec = parsed
    return {"match": "coordinates", "name": name or catalog.coord_name(ra, dec),
            "ra_hours": ra, "dec_degrees": dec}
