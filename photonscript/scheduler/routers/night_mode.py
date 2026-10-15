"""Deep night: tonight's night mode (scheduler/deep_night.py).

GET  /api/tonight/mode   {"night", "mode", "source", "config_mode",
                          "override", "target", "goals"}: cheap (no planner
                          run); the dashboard's control reads it.
POST /api/tonight/mode   {"mode": "deep" | "split" | "default",
                          "target": optional project id (deep only)}: the
                          override for tonight only, stored with the night
                          (<data_dir>/night_mode/<night>.json) and cleared
                          at the dawn shutdown. "default" removes it (config
                          plan_night_mode again). It is planned at the next
                          dispatch (arm, Resume, Restart tonight from now);
                          nothing is sent to NINA here.

GET /api/tonight (app.py) carries "mode" and "deep_pick" (the score
breakdown) from the planner itself.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

router = APIRouter()


def _tonight(cfg, now: datetime | None = None) -> str | None:
    from photonscript.scheduler.deep_night import night_label
    from photonscript.shared.localtime import night_window
    _base, tw = night_window(cfg, now or datetime.utcnow())
    dusk = tw.get("astro_dark_start")
    return night_label(cfg, dusk) if dusk else None


def _goals() -> list[dict]:
    from photonscript.scheduler.app import _stored_projects
    return [{"id": p.id, "name": p.target.name, "priority": p.priority}
            for p in _stored_projects().values() if p.active]


def mode_status(cfg, now: datetime | None = None) -> dict:
    from photonscript.scheduler import deep_night as dn
    night = _tonight(cfg, now)
    ov = dn.override(cfg, night) if night else None
    mode, target, source = dn.resolve(cfg, night) if night else (
        dn.config_mode(cfg), None, "config")
    names = {g["id"]: g["name"] for g in _goals()}
    return {"night": night, "mode": mode, "source": source,
            "config_mode": dn.config_mode(cfg), "override": ov,
            "target": target, "target_name": names.get(target) if target else None}


@router.get("/api/tonight/mode")
async def api_tonight_mode():
    from photonscript.scheduler.app import get_config
    out = mode_status(get_config())
    out["goals"] = _goals()
    return out


@router.post("/api/tonight/mode")
async def api_set_tonight_mode(payload: dict | None = Body(default=None)):
    from photonscript.scheduler import deep_night as dn
    from photonscript.scheduler.app import get_config
    cfg = get_config()
    body = payload or {}
    mode = str(body.get("mode") or "").strip().lower() or None
    target = str(body.get("target") or "").strip() or None
    if mode not in (None, "default", "config") + dn.MODES:
        return JSONResponse(status_code=400, content={
            "ok": False, "detail": "mode must be deep, split or default"})
    if target and mode != "deep":
        return JSONResponse(status_code=400, content={
            "ok": False, "detail": "a target is only for mode deep"})
    if target and target not in {g["id"] for g in _goals()}:
        return JSONResponse(status_code=404, content={
            "ok": False, "detail": f"no active goal with id {target}"})
    night = _tonight(cfg)
    if not night:
        return JSONResponse(status_code=409, content={
            "ok": False, "detail": "no dark window tonight"})
    dn.set_override(cfg, night, mode, target)
    out = mode_status(cfg)
    out.update(ok=True, note="planned at the next dispatch (arm, Resume or "
                             "Restart tonight from now); nothing sent to NINA")
    return out
