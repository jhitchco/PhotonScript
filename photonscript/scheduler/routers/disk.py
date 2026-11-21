"""Disk tab: where the image drive goes and what could be freed.

GET /disk                     the Disk page
GET /api/disk                 the cached scan (scheduler/disk_usage.py):
                              drive, areas (hardlink-aware unique bytes),
                              rejected totals, nights x rig (kept / rejected /
                              unjudged / calibration, synced, integrated,
                              archived), reclaimable estimates and the
                              PowerShell commands. ?refresh=1 starts a new
                              scan in the background; the first call with no
                              cache starts one too. Never waits for a scan.
GET /api/disk/paths?kind=     text/plain, one path per line: every link of
                              each file in a cleanup list (rejected |
                              thesky_old | old_synced_nights | night with
                              date= and optional rig=)

Read only: nothing here deletes, moves or renames a file. The commands are
for Jeremy to review and run himself.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from photonscript.shared.json_safe import SafeJSONResponse as JSONResponse

router = APIRouter()


def _app():
    from photonscript.scheduler import app
    return app


def _sync():
    """(pending basenames or None, capped) from the Syncthing remoteneed
    cache; never blocks on Syncthing."""
    a = _app()
    try:
        pending = a._syncthing_pending_names()
    except Exception:  # noqa: BLE001 - sync state is optional here
        pending = None
    return pending, bool(a._remoteneed_cache.get("capped"))


@router.get("/disk", response_class=HTMLResponse)
async def disk_page(request: Request):
    a = _app()
    return a.templates.TemplateResponse(request, "disk.html", {
        "version": a.VERSION, "observatory": a.get_config().get_observatory()})


@router.get("/api/disk")
def api_disk(refresh: bool = False):
    from photonscript.scheduler import disk_usage as du
    cfg = _app().get_config()
    started = False
    if refresh or du.cached(cfg) is None:
        started = du.refresh_bg(cfg)
    pending, capped = _sync()
    out = du.report(cfg, pending=pending, capped=capped)
    out["scan_started"] = started
    if started:
        out["scanning"] = True
    return out


@router.get("/api/disk/paths")
def api_disk_paths(kind: str, date: str = "", rig: str = ""):
    from photonscript.scheduler import disk_usage as du
    cfg = _app().get_config()
    pending, capped = _sync()
    try:
        out = du.paths(cfg, kind, pending=pending, capped=capped,
                       date=date, rig=rig)
    except ValueError as e:
        return JSONResponse(status_code=400, content={"detail": str(e)})
    if out is None:
        return JSONResponse(status_code=409, content={
            "detail": "no disk scan yet: open the Disk tab or GET "
                      "/api/disk?refresh=1 and wait for it to finish"})
    return PlainTextResponse("\n".join(out) + ("\n" if out else ""))
