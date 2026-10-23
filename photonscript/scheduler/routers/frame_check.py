"""Frame check endpoint (read only, nothing sent to NINA).

GET /api/frame-check?rig=rc16&last=2&index=&type=&pa=&compare=1&control=
    NINA's last frames (snapshots too) from its image history, found under
    NINA's save path on this PC and measured with the sub star QA (HFR,
    FWHM, ecc, stars, background, elongation axis, trail and its rate), plus
    the comparison verdict. The desktop CLI (photonscript frame-check --url)
    reads this small JSON instead of pulling raw FITS over Tailscale.

Pure logic lives in scheduler/frame_check.py. Kept out of app.py (PS-8).
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from photonscript.shared.json_safe import SafeJSONResponse as JSONResponse

router = APIRouter()


def _app():
    from photonscript.scheduler import app
    return app


@router.get("/api/frame-check")
def api_frame_check(rig: str = "rc16", last: int = 2,
                    index: list[int] | None = Query(None), type: str = "",  # noqa: A002
                    pa: float | None = None, compare: bool = True,
                    control: str = ""):
    """A sync handler: FastAPI runs it in its thread pool, so measuring a
    full frame (seconds) never blocks the event loop."""
    from photonscript.scheduler import frame_check as fc
    if rig not in ("rc16", "piggyback"):
        return JSONResponse({"ok": False, "error": "rig must be rc16 or piggyback"},
                            status_code=400)
    last = max(1, min(int(last or 2), 10))
    if control not in ("", "rc16", "piggyback"):
        return JSONResponse({"ok": False, "error": "control must be rc16 or piggyback"},
                            status_code=400)
    return fc.check(_app().get_config(), rig=rig, last=last, index=index or None,
                    image_type=type, pa=pa, compare=compare, control_rig=control)
