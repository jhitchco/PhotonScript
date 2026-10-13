"""Settings ledger routes (scheduler/settings_ledger.py). Read only toward
every program: a snapshot only READS NINA, PHD2, TheSky and the config.

GET  /api/settings-ledger/status          the newest rule results, apps, last
                                          change times, the last good night
GET  /api/settings-ledger/nights          nights with ledger data
GET  /api/settings-ledger/night/{night}   that night's index lines (no
                                          snapshots: compact)
GET  /api/settings-ledger/at?night=YYYY-MM-DD&at=HH:MM   (local) or
     ?t_utc=...                           the snapshot in force then
GET  /api/settings-ledger/snapshot/{sid}  one stored snapshot
GET  /api/settings-ledger/diff?a=&b=      what changed between two refs: an id,
                                          latest, last_good, YYYY-MM-DD (that
                                          night's last), YYYY-MM-DD@HH:MM
GET  /api/settings-ledger/sub?file=&night=   the settings in force for a sub
POST /api/settings-ledger/snapshot        take one now (reason "manual")
POST /api/settings-ledger/backfill?dry_run=1&force=0   past nights from the
                                          stored audits, sub records and
                                          guide logs

Handlers lazily import get_config to avoid an import cycle with app.py.
"""
from __future__ import annotations

import asyncio
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


@router.get("/api/settings-ledger/status")
async def api_ledger_status():
    from photonscript.scheduler import settings_ledger as sl
    cfg = _cfg()
    out = await asyncio.to_thread(sl.status, cfg)
    out["snapshotter"] = sl.SNAPSHOTTER.last
    return out


@router.get("/api/settings-ledger/nights")
async def api_ledger_nights():
    from photonscript.scheduler import settings_ledger as sl
    return {"nights": sl.nights(_cfg())}


@router.get("/api/settings-ledger/night/{night}")
async def api_ledger_night(night: str):
    from photonscript.scheduler import settings_ledger as sl
    cfg = _cfg()
    return {"night": night, "lines": await asyncio.to_thread(sl.index, cfg, night),
            "status": sl.load_status(cfg, night) or None}


@router.get("/api/settings-ledger/at")
async def api_ledger_at(night: str = "", at: str = "", t_utc: str = ""):
    from photonscript.scheduler import settings_ledger as sl
    from photonscript.shared import phd2_store as store
    cfg = _cfg()
    try:
        when = (store.parse_z(t_utc) if t_utc else
                sl.local_to_utc(cfg, night, at) if night and at else datetime.utcnow())
    except (TypeError, ValueError) as e:
        return JSONResponse({"ok": False, "detail": f"bad time: {e}"}, status_code=400)
    if when is None:
        return JSONResponse({"ok": False, "detail": "bad t_utc"}, status_code=400)
    hit = await asyncio.to_thread(sl.at, cfg, when)
    if not hit:
        return {"ok": False, "t_utc": store.iso_z(when), "note": "no snapshot before then"}
    return {"ok": True, "t_utc": store.iso_z(when), **hit}


@router.get("/api/settings-ledger/snapshot/{sid}")
async def api_ledger_snapshot(sid: str):
    from photonscript.scheduler import settings_ledger as sl
    snap = sl.load_snapshot(_cfg(), sl.resolve_ref(_cfg(), sid) or sid)
    if not snap:
        return JSONResponse({"ok": False, "detail": f"no snapshot {sid}"}, status_code=404)
    return snap


@router.get("/api/settings-ledger/diff")
async def api_ledger_diff(a: str = "last_good", b: str = "latest"):
    from photonscript.scheduler import settings_ledger as sl
    return await asyncio.to_thread(sl.diff, _cfg(), a, b)


@router.get("/api/settings-ledger/sub")
async def api_ledger_sub(file: str, night: str = ""):
    from photonscript.scheduler import settings_ledger as sl
    return await asyncio.to_thread(sl.for_sub, _cfg(), file, night or None)


@router.post("/api/settings-ledger/snapshot")
async def api_ledger_take():
    from photonscript.scheduler import settings_ledger as sl
    res = await sl.take(_cfg(), "manual", armer_state=_armer_state())
    res.pop("snapshot", None)
    return res


@router.post("/api/settings-ledger/backfill")
async def api_ledger_backfill(dry_run: bool = True, force: bool = False):
    from photonscript.scheduler import settings_ledger as sl
    return await asyncio.to_thread(sl.backfill, _cfg(), None, dry_run=dry_run, force=force)
