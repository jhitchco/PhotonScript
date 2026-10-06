"""Review viewer image endpoints (PS-6 loupe).

GET /api/runs/{date}/crop?file=&x=&y=&size=256&scale=1   PS-6 full-res crop
    (x, y native px; or fx, fy as frame fractions 0..1). PNG; the window
    is in the X-Crop-* headers (x0, y0, native edge, frame w / h, OSC).

The PS-80 star data the overlay draws is GET /api/runs/{date}/stars in
routers/review.py. Work lives in scheduler.sub_viewer; 503 when two crops
are already running (the loupe retries on the next pause).
"""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


def _busy():
    return JSONResponse(status_code=503, content={"detail": "viewer busy, retry"})


@router.get("/api/runs/{date}/crop")
def api_sub_crop(date: str, file: str, x: float | None = None,
                 y: float | None = None, fx: float | None = None,
                 fy: float | None = None, size: int = 256, scale: int = 1):
    from photonscript.scheduler import sub_viewer
    try:
        c = sub_viewer.crop(_cfg(), date, file, x=x, y=y, fx=fx, fy=fy,
                            size=size, scale=scale)
    except sub_viewer.Busy:
        return _busy()
    if c is None:
        return JSONResponse(status_code=404,
                            content={"detail": "FITS not found for this sub"})
    return Response(content=c["png"], media_type="image/png", headers={
        "Cache-Control": "private, max-age=86400",
        "X-Crop-X0": str(c["x0"]), "X-Crop-Y0": str(c["y0"]),
        "X-Crop-N": str(c["n"]), "X-Crop-Scale": str(c["scale"]),
        "X-Frame-W": str(c["w"]), "X-Frame-H": str(c["h"]),
        "X-Crop-OSC": "1" if c["osc"] else "0"})
