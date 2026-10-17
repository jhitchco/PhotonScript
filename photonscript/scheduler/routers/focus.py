"""PS-76 part 2: focus model endpoints.

POST /api/focus/ingest                 read NINA's AF reports now (the same
                                       ingest the background loop runs)
GET  /api/focus/predict?filter=&temp=  the lookup-table position for a filter
                                       at a temperature (read-only)
GET  /api/focus/table?rig=&temp=&payoff=  the per-filter focus table
                                       (focus_table.build_table), which
                                       filters are drive-ready and, with
                                       payoff=1, last week's AF replay
POST /api/focus/model-move?filter=     model-driven focus: read the RC16
                                       focuser (and NINA weather ambient)
                                       temperature, move to the table
                                       position (only with focus_model_drive
                                       "drive" and the filter drive-ready).
                                       NINA's ExternalScript reaches it
                                       through deploy\\focus-model-move.cmd

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


@router.get("/api/focus/table")
def api_focus_table(rig: str = "rc16", temp: Optional[float] = None,
                    payoff: bool = False, nights: int = 7):
    """The RC16 focus table at `temp` (default: the expected temperature):
    per filter the position, offset and its source, confidence and whether
    drive mode may use it. payoff=1 adds the replay of the last `nights`
    nights' AFs (focus_table.payoff). Read-only."""
    from photonscript.scheduler import focus_table as ft
    if (rig or "rc16").strip().lower() != "rc16":
        return {"error": "the focus table is RC16 only (the Piggy-600 has "
                         "one channel: see /api/focus piggyback.model)",
                "filters": {}, "drive_ready": []}
    cfg = _cfg()
    out = ft.build_table(cfg, temp=temp)
    if payoff:
        try:
            out["payoff"] = ft.payoff(cfg, nights=max(1, int(nights)))
        except Exception as e:  # noqa: BLE001
            out["payoff"] = {"error": str(e)}
    return out


async def _nina_weather(base: str) -> dict:
    """NINA weather device (the AARO station) info; {} when there is none."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(base + "/equipment/weather/info")
            body = r.json()
    except Exception as e:  # noqa: BLE001
        logger.debug("focus model-move: no weather info: %s", e)
        return {}
    p = body.get("Response", body) if isinstance(body, dict) else {}
    return p if isinstance(p, dict) else {}


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
                               label: str = "", trigger: bool = False):
    """Move the RC16 focuser to the lookup-table position for `filter` at the
    focuser's current temperature. Refuses (no move, verdict NO_MOVE) unless
    focus_model_drive is on, the model is trusted and the filter has enough
    AFs of its own; a move smaller than the deadband is skipped. Every call
    is logged to focus_model_moves.jsonl. Never raises: NINA keeps imaging
    whatever happens (the HFR trigger and the verify AF are the net).

    The model temperature is the focuser's unless it looks frozen
    (focus_table.choose_temperature: a known frozen value, or unchanged for
    an hour while the NINA weather ambient moved > 1 C); temp_source says
    which was used.

    trigger=true (the block's temperature trigger, not the block start):
    NINA's trigger measures from the last AF, so once crossed it re-fires
    before every sub; focus_model.trigger_gate holds (verdict HELD, no
    move) until the temperature is the focus table's step away from the
    last table move."""
    from photonscript.scheduler import focus_model as fm
    from photonscript.scheduler.focus_table import choose_temperature
    cfg = _cfg()
    base = str(getattr(cfg, "nina_base_url", "") or "").rstrip("/")
    out = {"filter": filter, "label": label, "dry_run": dry_run}
    try:
        info = await _nina_focuser(base) if base else {}
        wx = await _nina_weather(base) if base else {}
        pos = info.get("Position")
        ch = await asyncio.to_thread(choose_temperature, cfg,
                                     info.get("Temperature"),
                                     wx.get("Temperature"))
        temp = ch["temp"]
        out.update(temp=temp, temp_source=ch["source"],
                   temp_reason=ch["reason"], focuser_temp=ch["focuser"],
                   ambient_temp=ch["ambient"], position_before=pos,
                   trigger=bool(trigger))
        if trigger:
            from photonscript.scheduler.focus_table import build_table
            tab = await asyncio.to_thread(build_table, cfg, temp)
            gate = await asyncio.to_thread(fm.trigger_gate, cfg, temp,
                                           tab.get("move_temp_step_c"))
            if gate["hold"]:
                out.update(verdict="HELD", reason=gate["reason"])
                fm.record_move(cfg, out)
                return out
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
