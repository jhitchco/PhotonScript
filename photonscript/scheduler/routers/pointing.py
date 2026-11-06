"""PS-67 pointing record and night timeline.

GET  /api/runs/{date}/pointing          per-sub pointing (latest record per
                                        rig + file) and the night summary
                                        (off-target counts, RC16 mount vs
                                        plate-solve offset by pier / Dec / HA)
POST /api/runs/{date}/pointing          {"solve": false} re-run the pass
                                        (header / mount-log positions, the
                                        "On target" check); {"solve": true}
                                        also runs sampled ASTAP solves in the
                                        background (202). Refused (409) while
                                        armed / running or a grading job runs.
GET  /api/runs/{date}/timeline          night timeline rows (mount, guider,
                                        each rig, safety) + sub spans
GET  /api/status/mount-log?date=&tail=  the raw mount log lines
GET  /api/tonight/strip                 the dashboard "Tonight so far" strip:
                                        roof + one row per rig, collapsed to
                                        exposing / busy / idle, sunset to now
                                        (night_timeline.tonight_strip; a sync
                                        handler, so it runs in a worker
                                        thread; shared for STRIP_TTL_S)

merge_night(config, date, detail) is called by GET /api/runs/{date} to add
`pointing` to each sub and the night summary. Handlers lazily import
get_config to avoid an import cycle with app.py.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime

from fastapi import APIRouter, Body
from photonscript.shared.json_safe import SafeJSONResponse as JSONResponse

logger = logging.getLogger(__name__)
router = APIRouter()
_jobs: dict[str, dict] = {}


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


def merge_night(config, date: str, detail: dict) -> None:
    from photonscript.shared import pointing
    pts = pointing.load(config, date)
    for s in detail.get("subs") or []:
        s["pointing"] = pointing.compact(pts.get((s.get("rig") or "rc16",
                                                  s.get("file"))))
    detail["pointing"] = pointing.summarize(pts.values())


@router.get("/api/runs/{date}/pointing")
def api_pointing(date: str):
    from photonscript.shared import pointing
    cfg = _cfg()
    pts = pointing.load(cfg, date)
    recs = sorted(pts.values(), key=lambda r: (str(r.get("t") or ""),
                                               str(r.get("rig") or "")))
    try:
        summary = pointing.summarize(recs)
    except Exception as e:  # noqa: BLE001 - an odd record degrades the summary
        logger.warning("pointing summary %s failed: %s", date, e)
        summary = {"error": f"{type(e).__name__}: {e}"}
    return pointing._finite({"date": date, "records": recs, "summary": summary,
                             "job": _jobs.get(date)})


@router.post("/api/runs/{date}/pointing")
def api_pointing_run(date: str, payload: dict | None = Body(default=None)):
    from photonscript.scheduler.ecc_scale import blockers
    from photonscript.scheduler.pointing_record import night_pass
    cfg = _cfg()
    solve = bool((payload or {}).get("solve"))
    why = blockers(cfg)
    if why:
        return JSONResponse(status_code=409,
                            content={"status": "refused", "detail": " ".join(why)})
    if not solve:
        return night_pass(cfg, date, solve=False)
    if (_jobs.get(date) or {}).get("running"):
        return JSONResponse(status_code=202, content={"status": "running",
                                                      **_jobs[date]})
    _jobs[date] = {"running": True,
                   "started": datetime.now().isoformat(timespec="seconds")}

    def _work():
        try:
            res = night_pass(cfg, date, solve=True)
            _jobs[date] = {"running": False, "result": {
                k: v for k, v in res.items() if k != "summary"}}
        except Exception as e:  # noqa: BLE001
            logger.warning("pointing pass %s failed: %s", date, e)
            _jobs[date] = {"running": False, "error": str(e)}

    threading.Thread(target=_work, daemon=True, name=f"pointing-{date}").start()
    return JSONResponse(status_code=202, content={"status": "started", "date": date})


@router.get("/api/runs/{date}/timeline")
def api_timeline(date: str):
    from photonscript.scheduler.night_timeline import timeline
    return timeline(_cfg(), date)


STRIP_TTL_S = 45.0
_strip_cache: dict = {}
_strip_lock = threading.Lock()


@router.get("/api/tonight/strip")
def api_tonight_strip(date: str = ""):
    """Read-only. Tonight's strip (date= picks another night, uncached)."""
    import time
    from photonscript.scheduler.night_timeline import tonight_strip
    if date:
        return tonight_strip(_cfg(), date)
    with _strip_lock:   # one computation per TTL however many tabs poll
        hit = _strip_cache.get("v")
        if hit and time.monotonic() - hit[0] < STRIP_TTL_S:
            return hit[1]
        val = tonight_strip(_cfg())
        _strip_cache["v"] = (time.monotonic(), val)
        return val


@router.get("/api/status/mount-log")
def api_mount_log(date: str = "", tail: int = 200):
    from photonscript.shared import mount_log
    from photonscript.shared.phd2_store import night_of
    cfg = _cfg()
    d = date or night_of(cfg)
    lines = mount_log.load(cfg, d)
    tail = max(1, min(int(tail or 200), 5000))
    return {"date": d, "count": len(lines),
            "lines": [{k: v for k, v in r.items() if k != "dt"}
                      for r in lines[-tail:]]}
