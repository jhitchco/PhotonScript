"""PS-76 part 2: focus model endpoints.

POST /api/focus/ingest                 read NINA's AF reports now (the same
                                       ingest the background loop runs)
GET  /api/focus/predict?filter=&temp=  the lookup-table position for a filter
                                       at a temperature (read-only)
POST /api/focus/model-move?filter=     model-driven focus: read the RC16
                                       focuser temperature from NINA, move to
                                       the table position (only with
                                       focus_model_drive on and the model
                                       trusted). NINA's ExternalScript reaches
                                       it through deploy\\focus-model-move.cmd

GET /api/focus itself (the read-out) stays in app.py. The logic lives in
scheduler/focus_model.py. Handlers lazily import get_config to avoid an
import cycle with app.py.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter

logger = logging.getLogger(__name__)

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


@router.post("/api/focus/ingest")
async def api_focus_ingest(dir: str = "", trigger: str = "api"):
    """Ingest NINA AF reports now (dir overrides nina_autofocus_reports_dir
    for this run). Returns the ingest result, also saved as the last-ingest
    status shown in GET /api/focus."""
    from photonscript.scheduler.focus_model import ingest_af_reports
    cfg = _cfg()
    return await asyncio.to_thread(ingest_af_reports, cfg, dir or None,
                                   trigger or "api")


@router.get("/api/focus/predict")
def api_focus_predict(filter: str = "L", temp: Optional[float] = None):
    """Lookup-table position for a filter at a focuser temperature (default:
    the expected temperature), with the model's confidence and the table
    seed focus_seeds would use. Read-only."""
    from photonscript.scheduler import focus_model as fm
    from photonscript.scheduler.focus_seeds import seed_for
    cfg = _cfg()
    filt = fm.norm_filter(filter, cfg)
    t = temp
    basis = "request"
    if t is None:
        et = fm.expected_temp(cfg)
        t, basis = (et["temp"], et["basis"]) if et else (None, "none")
    pts = fm._rc16_points(cfg)
    model = fm.fit(pts, ref_filter=fm._ref_filter(cfg))
    return {"filter": filt, "temp": t, "temp_basis": basis,
            "model": fm.predict(model, filt, t),
            "seed": seed_for(filt, t, cfg),
            "trust": fm.trust(cfg, model, pts)}


async def _nina_focuser(base: str) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=8) as client:
        r = await client.get(base + "/equipment/focuser/info")
        body = r.json()
    p = body.get("Response", body) if isinstance(body, dict) else {}
    return p if isinstance(p, dict) else {}


async def _nina_move(base: str, position: int) -> dict:
    import httpx
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.get(base + "/equipment/focuser/move",
                             params={"position": int(position)})
        try:
            return r.json()
        except ValueError:
            return {"status_code": r.status_code}


@router.post("/api/focus/model-move")
async def api_focus_model_move(filter: str = "L", dry_run: bool = False,
                               label: str = ""):
    """Move the RC16 focuser to the lookup-table position for `filter` at the
    focuser's current temperature. Refuses (no move, verdict NO_MOVE) unless
    focus_model_drive is on, the model is trusted and the filter has enough
    AFs of its own; a move smaller than the deadband is skipped. Every call
    is logged to focus_model_moves.jsonl. Never raises: NINA keeps imaging
    whatever happens (the HFR trigger and the verify AF are the net)."""
    from photonscript.scheduler import focus_model as fm
    cfg = _cfg()
    base = str(getattr(cfg, "nina_base_url", "") or "").rstrip("/")
    out = {"filter": filter, "label": label, "dry_run": dry_run}
    try:
        info = await _nina_focuser(base) if base else {}
        temp = info.get("Temperature")
        pos = info.get("Position")
        out.update(temp=temp, position_before=pos)
        tgt = fm.model_move_target(cfg, filter, temp)
        out.update(tgt)
        if not tgt.get("move"):
            out["verdict"] = "NO_MOVE"
        elif pos is not None and abs(int(pos) - tgt["position"]) \
                < fm._MOVE_DEADBAND_STEPS:
            out["verdict"] = "IN_PLACE"
        elif dry_run:
            out["verdict"] = "WOULD_MOVE"
        else:
            out["nina"] = await _nina_move(base, tgt["position"])
            out["verdict"] = "MOVED"
    except Exception as e:  # noqa: BLE001
        logger.warning("focus model-move failed: %s", e)
        out.update(verdict="ERROR", reason=str(e))
    fm.record_move(cfg, {k: v for k, v in out.items() if k != "nina"})
    return out
