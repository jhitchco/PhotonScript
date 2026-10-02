"""PS-21 review endpoints: the QA scorecard, its thresholds, and the rescore.

GET  /api/qa/thresholds?rig=&target=&filter=   limits in force (legend)
GET  /api/runs/{date}/scorecard?file=          one sub's labeled scorecard
GET  /api/runs/{date}/rescore                  dry-run verdict diff
POST /api/runs/{date}/rescore                  {"apply": true} to write
GET  /api/runs/{date}/stars?file=&rig=         PS-80 star sidecar (stored)
GET  /api/qa/ecc-scale?date=&refresh=          PS-94 native vs binned ecc report

Kept out of app.py (PS-8 router split). Handlers lazily import get_config
to avoid an import cycle.
"""
from __future__ import annotations

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


@router.get("/api/qa/thresholds")
def api_qa_thresholds(rig: str = "rc16", target: str = "", filter: str = ""):  # noqa: A002
    from photonscript.shared import qa_rules
    return {"rules_version": qa_rules.RULES_VERSION,
            "thresholds": qa_rules.thresholds(_cfg(), rig, target or None,
                                              filter or None),
            "checks": [{"id": k, "name": v[0], "unit": v[1], "why": v[2]}
                       for k, v in qa_rules.CHECKS.items()]}


@router.get("/api/qa/ecc-scale")
def api_qa_ecc_scale(date: str, refresh: bool = False):
    """PS-94: eccentricity at 0.24"/px vs 0.48"/px for one night's RC16
    lights (dry run, never touches the subs log). Runs in the background
    (202 while it works; poll again); a saved report comes back at once
    unless refresh=true. Refused (409) while the armer is armed or running
    or a grading job is active."""
    from photonscript.scheduler import ecc_scale
    code, body = ecc_scale.request(_cfg(), date, refresh=refresh)
    return JSONResponse(status_code=code, content=body)


@router.get("/api/runs/{date}/scorecard")
def api_sub_scorecard(date: str, file: str):
    from photonscript.shared import qa_rules
    from photonscript.scheduler.runs import _load_subs, score_legacy_on_read
    cfg = _cfg()
    subs = _load_subs(cfg, date)
    hit = next((s for s in subs if s.get("file") == file), None)
    if hit is None:
        return JSONResponse(status_code=404, content={"detail": "sub not found"})
    if not hit.get("scorecard"):
        score_legacy_on_read(cfg, subs)
    card = hit.get("scorecard") or {}
    rows = qa_rules.expand(card)
    order = {"fail": 0, "warn": 1, "pass": 2, "skip": 3}
    return {"file": file, "rig": hit.get("rig", "rc16"),
            "rules_version": card.get("v"), "verdict": card.get("verdict"),
            "scored_on_read": bool(hit.get("scored_on_read")),
            "passed_qa": hit.get("passed_qa"), "reason": hit.get("reason"),
            "auto_verdict": hit.get("auto_verdict"),
            "auto_reason": hit.get("auto_reason"),
            "manual_qa": bool(hit.get("manual_qa")),
            "manual_reason": hit.get("manual_reason"),
            "review_source": hit.get("review_source"),
            "checks": sorted(rows, key=lambda r: order.get(r["status"], 9)),
            "thresholds": qa_rules.thresholds(
                cfg, hit.get("rig") or "rc16", hit.get("target"),
                hit.get("filter"))}


@router.get("/api/runs/{date}/rescore")
def api_rescore_preview(date: str):
    from photonscript.scheduler.runs import rescore_night
    return rescore_night(_cfg(), date, apply=False)


@router.post("/api/runs/{date}/rescore")
def api_rescore(date: str, payload: dict | None = Body(default=None)):
    """Re-grade stored metrics with the current rules. Dry run unless
    {"apply": true}; human verdicts are never touched."""
    from photonscript.scheduler.runs import rescore_night
    p = payload or {}
    return rescore_night(_cfg(), date, apply=bool(p.get("apply")),
                         allow_unreject=bool(p.get("allow_unreject")))


@router.get("/api/runs/{date}/stars")
def api_sub_stars(date: str, file: str, rig: str = "rc16"):
    from photonscript.shared.star_table import read
    t = read(_cfg(), date, file, rig)
    if t is None:
        return JSONResponse(status_code=404,
                            content={"detail": "no star sidecar for this sub"})
    return t
