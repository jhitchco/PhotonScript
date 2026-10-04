"""PS-21 review endpoints: the QA scorecard, its thresholds, and the rescore.

GET  /api/qa/thresholds?rig=&target=&filter=   limits in force (legend)
GET  /api/runs/{date}/scorecard?file=          one sub's labeled scorecard,
                                               PS-108 score + side-panel rows
GET  /api/runs/{date}/rescore                  dry-run verdict diff
POST /api/runs/{date}/rescore                  {"apply": true} to write
GET  /api/runs/{date}/stars?file=&rig=         PS-80 star sidecar (stored)
GET  /api/qa/ecc-scale?date=&refresh=          PS-94 native vs binned ecc report
GET  /api/runs/{date}/score-report             PS-108 score vs today's verdicts
GET  /api/runs/{date}/hist?file=&refresh=      PS-5 histogram (cached)

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
    t = qa_rules.thresholds(cfg, hit.get("rig") or "rc16", hit.get("target"),
                            hit.get("filter"))
    sc = _sub_score(cfg, subs, hit, card)
    from photonscript.shared.qa_score import panel_rows
    from photonscript.scheduler.runs import today_state
    return {"file": file, "rig": hit.get("rig", "rc16"),
            # PS-108: the score, what it would do, and the side-panel rows
            "score": sc, "today": today_state(hit),
            "panel": panel_rows(hit, rows, t, sc),
            "rules_version": card.get("v"), "verdict": card.get("verdict"),
            "scored_on_read": bool(hit.get("scored_on_read")),
            "passed_qa": hit.get("passed_qa"), "reason": hit.get("reason"),
            "auto_verdict": hit.get("auto_verdict"),
            "auto_reason": hit.get("auto_reason"),
            "manual_qa": bool(hit.get("manual_qa")),
            "manual_reason": hit.get("manual_reason"),
            "review_source": hit.get("review_source"),
            "checks": sorted(rows, key=lambda r: order.get(r["status"], 9)),
            "thresholds": t}


def _sub_score(cfg, subs: list, hit: dict, card: dict) -> dict | None:
    """PS-108: the stored score (as graded or last rescored); a sub graded
    before PS-108 is scored on read from its stored metrics with the night
    medians (score_on_read, nothing written). Mode and thresholds are the
    ones in force now."""
    from photonscript.shared import qa_rules, qa_score
    t = qa_rules.thresholds(cfg, hit.get("rig") or "rc16")
    stored = qa_score.from_compact(card.get("score"))
    if stored is not None:
        stored.update(mode=t["score_mode"], approve_at=t["score_approve"],
                      reject_below=t["score_reject"],
                      on_read=bool(hit.get("scored_on_read")))
        stored["decision"] = qa_score.decide(stored["score"], t["score_approve"],
                                             t["score_reject"])
        stored["text"] = "; ".join(
            ([f"capped at {stored['cap']['cap']:g} by {stored['cap']['check']}"]
             if stored.get("cap") else [])
            + [f"{d['id']} -{d['points']:g}" for d in stored["deductions"]])
        return stored
    try:
        key = qa_rules.group_key(hit)
        ctx = qa_rules.context(cfg, key[0], key[1], key[2],
                               night=qa_rules.night_context(subs).get(key))
        c = qa_rules.evaluate(qa_rules.metrics_from_record(hit), ctx)
    except Exception:  # noqa: BLE001 - the panel still shows the metrics
        return None
    if c.score is None:
        return None
    return {**c.score.as_dict(), "on_read": True}


@router.get("/api/runs/{date}/score-report")
def api_score_report(date: str):
    """PS-108: how many subs the score would approve / review / reject vs
    today's verdicts (re-graded from stored metrics, writes nothing)."""
    from photonscript.scheduler.runs import score_report
    return score_report(_cfg(), date)


@router.get("/api/runs/{date}/hist")
def api_sub_histogram(date: str, file: str, refresh: bool = False):
    """PS-5 (in PS-108): full-resolution histogram of one sub, cached."""
    from photonscript.scheduler.runs import histogram
    h = histogram(_cfg(), date, file, refresh=refresh)
    if h is None:
        return JSONResponse(status_code=404,
                            content={"detail": "FITS not found for this sub"})
    return JSONResponse(content=h, headers={"Cache-Control": "max-age=86400"})


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
