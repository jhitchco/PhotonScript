"""Scope PC desktop screenshot endpoints (READ ONLY; scheduler/screen_snapshot.py).

GET /api/screen/snapshot?scale=0.5&monitor=all&quality=70
    A JPEG of the interactive desktop (image/jpeg). Headers X-Screen-Width,
    X-Screen-Height, X-Screen-Scale, X-Screen-Quality, X-Screen-Session.
GET /api/screen/files/{name}
    A snapshot `photonscript screen-snapshot` saved under <data_dir>/screen
    (the remote CLI path: `photonscript remote -- screen-snapshot --json`
    prints the name).

Both need the X-PhotonScript-Token header equal to PS_REMOTE_CLI_TOKEN, the
same check as POST /api/cli/run (remote_cli.token_ok): no token configured =
off (403). One capture per 5 s (429 + Retry-After), refused in session 0
(409). Every capture and refusal goes to the remote CLI audit log. Nothing
is ever sent to the desktop (no input, no clicks).
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Header, Request
from fastapi.responses import FileResponse, Response

from photonscript.scheduler import remote_cli as rc
from photonscript.scheduler import screen_snapshot as ss
from photonscript.shared.json_safe import SafeJSONResponse as JSONResponse

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


def _client(request: Request) -> dict:
    h = request.headers
    return {"ip": request.client.host if request.client else None,
            "forwarded_for": h.get("x-forwarded-for"),
            "tailscale_user": h.get("tailscale-user-login")}


# seams for the tests (no real capture)
def _sid():
    from photonscript.app_start import session_id
    return session_id()


def _grab(monitor: str):
    return ss.grab_pil(monitor)


def _refuse(cfg, status: int, detail: str, client: dict,
            retry_after: Optional[float] = None) -> JSONResponse:
    rc.audit(cfg, {"event": "screen_refused", **client, "status_code": status,
                   "detail": detail})
    headers = {"Retry-After": str(int(retry_after + 0.999))} if retry_after else None
    return JSONResponse(status_code=status, content={"detail": detail}, headers=headers)


@router.get("/api/screen/snapshot")
def api_screen_snapshot(request: Request, scale: Optional[str] = None,
                        monitor: Optional[str] = None, quality: Optional[str] = None,
                        x_photonscript_token: Optional[str] = Header(None)):
    """A sync handler: the capture and the JPEG encode run in the thread pool."""
    cfg = _cfg()
    client = _client(request)
    ok, status, detail = rc.token_ok(getattr(cfg, "remote_cli_token", ""),
                                     x_photonscript_token or "")
    if not ok:
        return _refuse(cfg, status, detail, client)
    try:
        shot = ss.take(cfg, scale, monitor, quality, sid_fn=_sid, grab_fn=_grab)
    except ss.SnapshotRefused as e:
        return _refuse(cfg, e.status, e.detail, client, e.retry_after)
    rc.audit(cfg, {"event": "screen", **client, **ss.meta(shot)})
    return Response(content=shot.jpeg, media_type="image/jpeg", headers={
        "Cache-Control": "no-store",
        "X-Screen-Width": str(shot.width), "X-Screen-Height": str(shot.height),
        "X-Screen-Scale": f"{shot.scale:g}", "X-Screen-Quality": str(shot.quality),
        "X-Screen-Monitor": shot.monitor, "X-Screen-Taken": shot.taken,
        "X-Screen-Session": "" if shot.session_id is None else str(shot.session_id)})


@router.get("/api/screen/files/{name}")
def api_screen_file(name: str, request: Request,
                    x_photonscript_token: Optional[str] = Header(None)):
    cfg = _cfg()
    client = _client(request)
    ok, status, detail = rc.token_ok(getattr(cfg, "remote_cli_token", ""),
                                     x_photonscript_token or "")
    if not ok:
        return _refuse(cfg, status, detail, client)
    p = ss.saved_file(cfg, name)
    if p is None:
        return JSONResponse(status_code=404, content={"detail": "no such snapshot"})
    return FileResponse(str(p), media_type="image/jpeg",
                        headers={"Cache-Control": "no-store"})
