"""TheSky / TPoint settings audit routes (PS-104). Report only.

GET  /api/thesky/audit?refresh=          the last audit; refresh=1 runs one now
GET  /api/thesky/imagelink-check?refresh=&thesky=0|1
                                         the newest Image Link check; refresh=1
                                         solves the newest RC16 L frame with
                                         ASTAP, thesky=1 also runs TheSky's own
                                         Image Link on a temporary copy (only
                                         while the armer is idle)
GET  /api/thesky/pointing?nights=14&refresh=   NINA first-slew error trend
GET  /api/thesky/manual                  the manual TPoint record
POST /api/thesky/manual {model_date, points, rms_arcsec, ...}   save it
GET  /api/thesky/onsite-script           the read-only script for TheSky's
                                         Run Java Script window (on-site check)

Nothing here writes TheSky, moves the mount or takes an image. Handlers
lazily import get_config to avoid an import cycle with app.py.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


def _armer_state() -> str:
    try:
        from photonscript.scheduler.app import get_armer
        return str(get_armer().state or "")
    except Exception:  # noqa: BLE001
        return ""


@router.get("/api/thesky/audit")
async def api_thesky_audit(refresh: bool = False):
    """The last TheSky / TPoint audit (arm or a refresh); refresh=1 runs a
    new one now (read-only TheSky scripts, stored checks, NINA logs)."""
    from photonscript.scheduler import thesky_audit as ta
    cfg = _cfg()
    out = None if refresh else ta.load_latest(cfg)
    if out is None:
        out = await asyncio.to_thread(ta.run_audit, cfg, "manual",
                                      armer_state=_armer_state())
        out["cached"] = False
    else:
        out["cached"] = True
    out["armer_state_now"] = _armer_state() or None
    out["imagelink_thesky_allowed"] = ta.armer_idle(_armer_state())
    from photonscript.scheduler import guiding_attention
    guiding_attention.annotate_thesky(out)         # PS-119: source and its age
    return out


@router.get("/api/thesky/imagelink-check")
async def api_thesky_imagelink_check(refresh: bool = False, thesky: bool = False):
    """The newest Image Link check. refresh=1 runs a new ASTAP check;
    thesky=1 adds TheSky's own Image Link on a copy of the frame, refused
    (409) unless the armer is idle (DISARMED / COMPLETE)."""
    from photonscript.scheduler import thesky_audit as ta
    from photonscript.shared import phd2_store as store
    cfg = _cfg()
    st = _armer_state()
    if thesky and not ta.armer_idle(st):
        return JSONResponse(status_code=409, content={
            "ok": False, "note": f"armer is {st or 'unknown'}: TheSky Image Link runs only "
                                 "while it is idle (DISARMED / COMPLETE)"})
    if refresh or thesky:
        out = await asyncio.to_thread(ta.imagelink_check, cfg, thesky=thesky,
                                      armer_state=st)
    else:
        out = store.read_json(ta.audit_dir(cfg) / "imagelink_latest.json") or {
            "ok": False, "note": "no Image Link check yet"}
    latest = ta.load_latest(cfg) or {}
    obs = {r["id"]: r for r in latest.get("rows") or []}
    ails = {"ails_image_scale": (obs.get("ails_image_scale") or {}).get("current"),
            "ails_position_angle": (obs.get("ails_position_angle") or {}).get("current")}
    rb = (latest.get("manual") or {}).get("run_binning") or 2
    out["compare"] = ta.compare_settings(out, ails, int(rb))
    out["armer_state_now"] = st or None
    return out


@router.get("/api/thesky/pointing")
async def api_thesky_pointing(nights: int = 14, refresh: bool = False):
    """NINA first-slew error over the last N nights by side of the meridian,
    Dec band and HA band, with PS-67's mount vs solve when on disk."""
    from photonscript.scheduler import nina_center_log as ncl
    return await asyncio.to_thread(ncl.summary, _cfg(), nights, None, refresh)


@router.get("/api/thesky/manual")
def api_thesky_manual():
    from photonscript.scheduler import thesky_audit as ta
    cfg = _cfg()
    rec = ta.load_manual(cfg)
    return {"record": rec, "fields": list(ta.MANUAL_FIELDS),
            "max_age_days": getattr(cfg, "thesky_manual_max_age_days", 30)}


@router.post("/api/thesky/manual")
async def api_thesky_manual_post(request: Request):
    """Save the manual TPoint record (after each TPoint session). Writes
    only PhotonScript's own <data_dir>/thesky/manual.json."""
    from photonscript.scheduler import thesky_audit as ta
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={
            "ok": False, "problems": ["body must be a JSON object"]})
    res = ta.save_manual(_cfg(), body)
    if not res.get("ok"):
        return JSONResponse(status_code=400, content=res)
    return res


@router.get("/api/thesky/onsite-script", response_class=PlainTextResponse)
def api_thesky_onsite_script():
    """The read-only check script for TheSky's Tools > Run Java Script."""
    from photonscript.telescope_agent.thesky_client import onsite_script
    return onsite_script()
