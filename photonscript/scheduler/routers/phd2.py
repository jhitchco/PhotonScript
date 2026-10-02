"""PhotonScript's own PHD2 records and actions (PS-91, PS-92).

GET  /api/phd2/guard?date=         non-star lock guard episodes for a night
GET  /api/phd2/hotpix              guide-camera hot-pixel map status
POST /api/phd2/hotpix/capture      capture a new map now (roof closed!)

The PHD2 guide-log endpoints (/api/phd2/log, /logs, /summary, /analysis)
stay in routers/triage.py. Handlers lazily import get_config to avoid an
import cycle with app.py.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter
from fastapi.responses import JSONResponse

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


def guard_summary(config, date: str) -> dict:
    """The guard block for a night (API, runs page, morning report)."""
    from photonscript.shared import phd2_store as store
    eps = store.guard_episodes(config, date)
    mins = 0.0
    for e in eps:
        a, b = store.parse_z(e.get("start_utc")), store.parse_z(e.get("end_utc"))
        if a and b:
            mins += max(0.0, (b - a).total_seconds() / 60)
    recs = [r for e in eps for r in e.get("recoveries", []) if r.get("ok") is not None]
    return {"date": date,
            "enabled": bool(getattr(config, "guard_enabled", True)),
            "auto_recover": bool(getattr(config, "guard_auto_recover", False)),
            "episodes": len(eps),
            "non_star": sum(1 for e in eps if e.get("kind") == "non_star"),
            "impossible_state": sum(1 for e in eps if e.get("kind") == "impossible_state"),
            "closed_minutes": round(mins, 1),
            "recoveries_ok": sum(1 for r in recs if r.get("ok")),
            "recoveries_failed": sum(1 for r in recs if r.get("ok") is False),
            "list": eps}


@router.get("/api/phd2/guard")
def api_phd2_guard(date: str = ""):
    """Guard episodes of a night (default: tonight / the night in progress)."""
    from photonscript.shared import phd2_store as store
    from photonscript.telescope_agent import phd2_ops
    cfg = _cfg()
    date = date or store.night_of(cfg, datetime.utcnow())
    out = guard_summary(cfg, date)
    out["phd2_ops"] = phd2_ops.status()
    return out


@router.get("/api/phd2/hotpix")
def api_phd2_hotpix(pixels: bool = False):
    from photonscript.telescope_agent import guide_hotpix
    cfg = _cfg()
    out = guide_hotpix.status(cfg)
    if pixels:
        out["pixels"] = (guide_hotpix.load(cfg) or {}).get("pixels", [])
    return out


@router.post("/api/phd2/hotpix/capture")
async def api_phd2_hotpix_capture():
    """Build the guide-camera hot-pixel map now. Take it with the roof
    closed (or the guide camera capped): stars that hold still in 8 frames
    are not hot pixels, but faint ones can be mistaken for them. Refused
    while a night is running and whenever PHD2 is guiding."""
    from photonscript.telescope_agent import guide_hotpix
    if _armer_state() == "RUNNING":
        return JSONResponse(status_code=409, content={
            "ok": False, "note": "a night is running: the map is built "
                                 "automatically while the roof is closed"})
    return await guide_hotpix.capture(_cfg(), "manual")
