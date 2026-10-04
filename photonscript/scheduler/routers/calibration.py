"""PS-113 calibration capture + QA endpoints (Calibration page).

GET  /api/calibration/plan?rig=              needs vs have vs bad per rig
GET  /api/calibration/qa?rig=&limit=         QA summary + failing frames
POST /api/calibration/qa/backfill            {"rig", "dry_run"}: QA the whole
                                             library in the background
GET  /api/calibration/qa/backfill            backfill progress / last report
GET  /api/calibration/capture-job?check=     job status; check=1 also lists
                                             today's refusals (read only)
POST /api/calibration/capture-job            {"rig", "exposures", "count",
                                             "bias", "budget_min"}: start
POST /api/calibration/capture-job/cancel     {"rig"}
POST /api/calibration/daytime/reset          {"rig"}: re-allow daytime capture

The older POST /api/calibration/capture (System page) runs the same guarded
job. Kept out of app.py (PS-8 router split).
"""
from __future__ import annotations

import threading
from datetime import datetime

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

router = APIRouter()

_backfill: dict = {"running": False, "progress": None, "report": None,
                   "error": None, "started": None}
_backfill_lock = threading.Lock()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


def _armer():
    from photonscript.scheduler.app import get_armer
    return get_armer()


def _armer_state() -> str:
    return str(_armer().state)


@router.get("/api/calibration/plan")
def api_calibration_plan(rig: str = ""):
    from photonscript.scheduler.calibration_plan import gap_report
    from photonscript.shared.rigs import rig_ids
    cfg = _cfg()
    if rig and rig not in rig_ids(cfg):
        return JSONResponse(status_code=404, content={"detail": f"unknown rig {rig}"})
    return gap_report(cfg, rig or None)


@router.get("/api/calibration/qa")
def api_calibration_qa(rig: str = "", limit: int = 200):
    from photonscript.scheduler import calibration_qa as cq
    from photonscript.shared.rigs import rig_ids
    cfg = _cfg()
    out = {"mode": cq.mode(cfg), "rigs": {}}
    for rg in ([rig] if rig else rig_ids(cfg)):
        store = cq.load_store(cfg, rg)
        recs = list(store["frames"].values())
        bad = sorted((r for r in recs if r.get("verdict") == "fail"),
                     key=lambda r: (r.get("date") or "", r.get("name") or ""),
                     reverse=True)
        out["rigs"][rg] = {
            "updated": store.get("updated"), **cq.summarize(recs),
            "daytime": cq.daytime_state(cfg, rg),
            "daytime_check": store.get("daytime"),
            "bad": [{"key": cq.frame_key(r["type"], r["date"], r["name"]),
                     "location": r.get("location"), "reasons": r.get("reasons"),
                     "ccdtemp": r.get("ccdtemp"), "median": r.get("median")}
                    for r in bad[:max(0, int(limit))]]}
    return out


@router.post("/api/calibration/qa/backfill")
def api_calibration_qa_backfill(payload: dict = Body(default={})):
    from photonscript.scheduler import calibration_qa as cq
    cfg = _cfg()
    state = _armer_state()
    if state in ("RUNNING", "PAUSED_UNSAFE"):
        return JSONResponse(status_code=409, content={
            "detail": f"armer is {state}: the backfill reads every frame and "
                      "would compete with tonight's grading; run it in the day"})
    with _backfill_lock:
        if _backfill["running"]:
            return JSONResponse(status_code=409, content={
                "detail": "a calibration QA backfill is already running",
                "progress": _backfill["progress"]})
        _backfill.update(running=True, progress=None, error=None,
                         started=datetime.utcnow().isoformat(timespec="seconds") + "Z")
    rig = payload.get("rig") or None
    dry = bool(payload.get("dry_run", True))

    def _work():
        try:
            rep = cq.backfill(cfg, rig, dry_run=dry,
                              progress=lambda i, n, k: _backfill.update(
                                  progress={"done": i, "of": n, "frame": k}))
            _backfill["report"] = rep
        except Exception as e:  # noqa: BLE001
            _backfill["error"] = f"{type(e).__name__}: {e}"
        finally:
            _backfill["running"] = False

    threading.Thread(target=_work, name="calibration-qa-backfill", daemon=True).start()
    return JSONResponse(status_code=202, content={"started": True, "dry_run": dry,
                                                  "rig": rig})


@router.get("/api/calibration/qa/backfill")
def api_calibration_qa_backfill_status():
    return dict(_backfill)


@router.get("/api/calibration/capture-job")
async def api_capture_job(check: bool = False, rig: str = ""):
    from photonscript.scheduler import calibration_capture as cc
    from photonscript.shared.rigs import rig_ids
    cfg = _cfg()
    out = cc.status(cfg)
    out["armer"] = _armer_state()
    if check:
        out["refusals"] = {}
        for rg in ([rig] if rig else rig_ids(cfg)):
            ref, seen = await cc.preflight(cfg, rg, out["armer"], connect=False)
            out["refusals"][rg] = {"refusals": ref, "read": seen}
    return out


@router.post("/api/calibration/capture-job")
async def api_capture_job_start(payload: dict = Body(default={})):
    from photonscript.scheduler import calibration_capture as cc
    rig = str(payload.get("rig") or "rc16")
    exposures = payload.get("exposures") or None
    if isinstance(exposures, str):
        exposures = [float(x) for x in exposures.split(",") if x.strip()]
    ok, body = await cc.start_job(
        _cfg(), rig, armer_state_fn=_armer_state,
        armer_status_fn=lambda: _armer().status(),
        exposures=exposures, count=payload.get("count"), bias=payload.get("bias"),
        budget=payload.get("budget_min"), source=str(payload.get("source") or "manual"))
    if not ok:
        return JSONResponse(status_code=409, content=body)
    return body


@router.post("/api/calibration/capture-job/cancel")
async def api_capture_job_cancel(payload: dict = Body(default={})):
    from photonscript.scheduler import calibration_capture as cc
    res = await cc.cancel(str(payload.get("rig") or "rc16"))
    return res if res.get("ok") else JSONResponse(status_code=409, content=res)


@router.post("/api/calibration/daytime/reset")
def api_daytime_reset(payload: dict = Body(default={})):
    from photonscript.scheduler import calibration_qa as cq
    rig = str(payload.get("rig") or "")
    if not rig:
        return JSONResponse(status_code=400, content={"detail": "rig required"})
    return cq.set_daytime_state(_cfg(), rig, "untested", note="reset by hand")
