"""PS-27 part 2 endpoints: the Piggy-600 settle gate and split pointing.

POST /api/piggyback/settle-gate?label=   hold until the RC16 mount is still
                                         and PHD2 is not settling (at most
                                         piggyback_settle_timeout_s); NINA #2's
                                         ExternalScript reaches it through
                                         deploy\\settle-gate.cmd
GET  /api/piggyback/split-guard          config, the live motion tracker, the
                                         last gate result, tonight's numbers
GET  /api/runs/{date}/split              the night's split-pointing rate

The logic lives in scheduler/split_guard.py. Handlers lazily import
get_config to avoid an import cycle with app.py.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


@router.post("/api/piggyback/settle-gate")
async def api_settle_gate(request: Request, label: str = ""):
    """Held while the mount moves (bounded). Every verdict means "shoot":
    PASS, TIMEOUT, UNKNOWN (NINA #1 unreadable), OFF."""
    from photonscript.scheduler import split_guard as sg
    return await sg.run_settle_gate(_cfg(), label,
                                    disconnected=request.is_disconnected)


@router.get("/api/piggyback/split-guard")
def api_split_guard_status():
    from photonscript.scheduler import split_guard as sg
    from photonscript.shared.mount_motion import TRACKER
    from photonscript.shared.phd2_store import night_of
    cfg = _cfg()
    out = {"settle_gate": sg.gate_enabled(cfg),
           "script": str(getattr(cfg, "piggyback_settle_script", "")),
           "script_found": sg.gate_script(cfg) is not None,
           "timeout_s": float(getattr(cfg, "piggyback_settle_timeout_s", 90.0)),
           "still_s": float(getattr(cfg, "piggyback_settle_still_s", 6.0)),
           "abort_on_move": sg.abort_enabled(cfg),
           "abort_move_arcmin": float(getattr(cfg, "piggyback_abort_move_arcmin", 0.5)),
           "motion": TRACKER.snapshot(time.time()),
           "last_gate": dict(sg.LAST)}
    try:
        out["tonight"] = sg.night_split_summary(cfg, night_of(cfg))
    except Exception as e:  # noqa: BLE001
        out["tonight"] = {"error": str(e)}
    return out


@router.get("/api/runs/{date}/split")
def api_run_split(date: str):
    from photonscript.scheduler import split_guard as sg
    return sg.night_split_summary(_cfg(), date)
