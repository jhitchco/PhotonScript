"""PS-148 through-focus optics test endpoints (read only, nothing to NINA).

GET /api/optics-test/target?at=
    the field the test would use now (or at ?at=<UTC ISO>), its altitude,
    hour angle and length, plus the configured offsets / filters / lengths
GET /api/optics-test/sequence?name=&ra=&dec=&at=
    download the standalone night sequence (lint-gated). The usual way to
    run it is the sideload recipe optics_through_focus (routers/sideload.py)
GET /api/optics-test/report?date=
    that night's optics-test subs per filter and focuser offset: median ecc
    and HFR of the bright stars, the stretch axis overall and per 3x3 zone,
    and the verdict (astigmatism / constant axis / defocus only, tilt)

Pure logic lives in scheduler/optics_test.py. Kept out of app.py (PS-8).
"""
from __future__ import annotations

import json

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

router = APIRouter()


def _app():
    from photonscript.scheduler import app
    return app


def _field(name: str, ra: float | None, dec: float | None, at: str) -> dict:
    from photonscript.scheduler import optics_test as ot
    cfg = _app().get_config()
    if ra is not None and dec is not None:
        return {"name": name or "Custom field", "ra_hours": float(ra),
                "dec_degrees": float(dec), "source": "request",
                "est_minutes": round(ot.duration_s(cfg) / 60),
                "reason": "coordinates given in the request"}
    projects = [p for p in _app()._stored_projects().values()
                if getattr(p, "active", False)]
    return ot.pick_field(cfg, ot.parse_at(at), projects)


@router.get("/api/optics-test/target")
def api_optics_test_target(name: str = "", ra: float | None = None,
                           dec: float | None = None, at: str = ""):
    from photonscript.scheduler import optics_test as ot
    return {**_field(name, ra, dec, at),
            "params": ot.params(_app().get_config())}


@router.get("/api/optics-test/sequence")
def api_optics_test_sequence(name: str = "", ra: float | None = None,
                             dec: float | None = None, at: str = ""):
    from photonscript.scheduler import optics_test as ot
    from photonscript.scheduler.sequence_lint import format_result, lint
    field = _field(name, ra, dec, at)
    body = ot.generate_sequence(_app().get_config(), field)
    result = lint(json.loads(body), guided=False)
    if not result.ok:
        return JSONResponse(status_code=500, content={
            "detail": "Lint FAILED - refusing to serve the optics test",
            "findings": format_result(result), "field": field})
    safe = "".join(c if c.isalnum() else "_" for c in field["name"])
    return Response(body, media_type="application/json", headers={
        "Content-Disposition":
            f'attachment; filename="optics_test_{safe}.json"',
        "X-PhotonScript-Field": safe})


@router.get("/api/optics-test/report")
def api_optics_test_report(date: str = ""):
    from photonscript.scheduler import optics_test as ot
    return ot.build_report(_app().get_config(), date or None)
