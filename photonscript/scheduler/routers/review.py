"""PS-21 review endpoints: the QA scorecard, its thresholds, and the rescore.

GET  /api/qa/thresholds?rig=&target=&filter=   limits in force (legend)
GET  /api/runs/{date}/scorecard?file=          one sub's labeled scorecard,
                                               PS-108 score + side-panel rows,
                                               PS-115 target / pointing block
GET  /api/runs/{date}/rescore                  dry-run verdict diff
POST /api/runs/{date}/rescore                  {"apply": true} to write
GET  /api/runs/{date}/remeasure                PS-130 re-measure job status
POST /api/runs/{date}/remeasure                PS-130 start it (dry run unless
                                               {"apply": true}; background)
GET  /api/runs/{date}/stars?file=&rig=         PS-80 star sidecar (measured
                                               once on view when missing)
GET  /api/qa/ecc-scale?date=&refresh=          PS-94 native vs binned ecc report
GET  /api/runs/{date}/score-report             PS-108 score vs today's verdicts
GET  /api/runs/{date}/hist?file=&refresh=      PS-5 histogram (cached)
GET  /api/qa/gates                             PS-114 every gate per rig
GET  /api/qa/baselines?rig=&nights=&k=         PS-114 proposed gates (report)

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


@router.get("/api/qa/gates")
def api_qa_gates():
    """PS-114: every QA gate for both rigs side by side, with the config key
    (and env var) that sets it on each rig (System page table)."""
    from photonscript.shared import qa_rules
    return qa_rules.rig_gates(_cfg())


@router.get("/api/qa/baselines")
def api_qa_baselines(rig: str = "", nights: int = 14, k: float | None = None):
    """PS-114: each rig's baseline per filter over its accepted subs of the
    last N nights and the gates it proposes, next to the gates in force.
    Report only: never changes a gate."""
    from photonscript.scheduler.qa_baselines import baselines
    return baselines(_cfg(), rig or None, max(1, min(int(nights), 365)), k)


@router.get("/api/qa/ecc-scale")
def api_qa_ecc_scale(date: str, refresh: bool = False):
    """PS-94: eccentricity at 0.236"/px vs 0.47"/px for one night's RC16
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
            "target": target_block(cfg, date, hit),       # PS-115
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


def _src_label(src) -> tuple[str, bool]:
    """PS-107 source tag: only a plate solve confirms where a sub pointed."""
    from photonscript.shared.qa_rules import _SRC_LABEL, pointing_confirmed
    if pointing_confirmed(src):
        return "plate solve", True
    return _SRC_LABEL.get(str(src or ""), "header") + ", unconfirmed", False


def target_block(cfg, date: str, rec: dict) -> dict:
    """PS-115: what the sub was filed under and where the scope pointed, for
    the side panel's Target section: name (Targets page link, PS-81),
    filter / exposure, this rig's frame (refimage.rig_fov), and from the
    PS-67 pointing record the offset, direction and source, alt, pier, HA
    and the pointed position (plate solve center when solved, else the
    mount position). Never raises; pointing is None without a record."""
    from urllib.parse import quote

    from photonscript.scheduler.refimage import rig_fov
    rig = rec.get("rig") or "rc16"
    name = rec.get("target") or ""
    named = bool(name) and name != "?"
    out = {"name": name if named else None,
           "link": f"/target?name={quote(name)}" if named else None,
           "rig": rig, "filter": rec.get("filter"), "exp_s": rec.get("exp_s"),
           "frame": None, "pointing": None}
    try:
        f = rig_fov(cfg, rig)
        out["frame"] = {k: f[k] for k in ("label", "w_arcmin", "h_arcmin")}
    except Exception:  # noqa: BLE001 - the section still shows the text
        pass
    p: dict = {}
    try:
        from photonscript.shared import pointing
        p = pointing.load(cfg, date).get((rig, rec.get("file"))) or {}
    except Exception:  # noqa: BLE001
        p = {}
    src = p.get("src") or rec.get("pointing_src")
    off = p.get("off_target_arcmin")
    if off is None:
        off = rec.get("pointing_offset_arcmin")
    solved = p.get("solved_ra") is not None and p.get("solved_dec") is not None
    if not p and off is None:
        return out
    label, confirmed = _src_label(src)
    out["pointing"] = {
        "src": src, "src_label": label, "confirmed": confirmed,
        "off_arcmin": off, "off_dir": p.get("off_target_dir"),
        "off_pa": p.get("off_target_pa"), "flag": p.get("flag") or "",
        "alt": p.get("alt"), "pier": p.get("pier"), "ha_h": p.get("ha_h"),
        "ra": p.get("solved_ra") if solved else p.get("mount_ra"),
        "dec": p.get("solved_dec") if solved else p.get("mount_dec"),
        "at": "solve" if solved else ("mount" if p.get("mount_ra") is not None
                                      else None),
        "target_ra": p.get("target_ra"), "target_dec": p.get("target_dec")}
    return out


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


@router.get("/api/runs/{date}/remeasure")
def api_remeasure_status(date: str):
    """PS-130: the night's re-measure job (idle / running / done with the
    result / error)."""
    from photonscript.scheduler.qa_remeasure import status
    return status(date)


@router.post("/api/runs/{date}/remeasure")
def api_remeasure(date: str, payload: dict | None = Body(default=None)):
    """PS-130: re-measure the night's pre-PS-83 backfill records from their
    FITS and re-judge the night, in the background (202; poll the GET).
    Dry run unless {"apply": true}; human verdicts are never touched. 409
    while armed / running or another grading job is active."""
    from photonscript.scheduler.qa_remeasure import request
    p = payload or {}
    code, body = request(_cfg(), date, apply=bool(p.get("apply")),
                         allow_unreject=bool(p.get("allow_unreject")))
    return JSONResponse(status_code=code, content=body)


@router.get("/api/runs/{date}/stars")
def api_sub_stars(date: str, file: str, rig: str = "", compute: bool = True):
    """PS-80: the sub's star sidecar for the viewer overlay (rig defaults
    to the record's). A sub without one is measured once on view with the
    backfill grader's function and cached ("on_view": true); compute=false
    only reads. Every ecc in sqrt(1-(b/a)^2) form."""
    from photonscript.scheduler.sub_viewer import stars_for_view
    t = stars_for_view(_cfg(), date, file, rig or None, compute=compute)
    if t is None:
        return JSONResponse(status_code=404,
                            content={"detail": "no star sidecar for this sub"})
    return t
