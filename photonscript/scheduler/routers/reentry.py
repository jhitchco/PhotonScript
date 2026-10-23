"""Re-entry gate endpoints (2026-10-15, scheduler/reentry.py).

POST /api/reentry/gate?kind=af|slew|center&label=&filter=&ra=&dec=
     one gate call from NINA's ExternalScript (deploy\\reentry-gate.cmd ->
     `photonscript reentry-gate`). verdict SKIP = NINA skips the gated
     items (start AF / slew / center); anything else = run them.
GET  /api/reentry           mode, limits and tonight's decisions
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


@router.post("/api/reentry/gate")
async def api_reentry_gate(kind: str = "af", label: str = "",
                           filter: str = "", ra: Optional[float] = None,
                           dec: Optional[float] = None):
    from photonscript.scheduler import reentry
    return await reentry.run_gate(_cfg(), kind, label, filter, ra, dec)


@router.get("/api/reentry")
def api_reentry_status(night: Optional[str] = None):
    from photonscript.scheduler import reentry
    from photonscript.shared.night_events import events_path
    from photonscript.shared.phd2_store import night_of, read_jsonl
    cfg = _cfg()
    night = night or night_of(cfg)
    rows = [r for r in read_jsonl(events_path(cfg, night))
            if r.get("kind") == reentry.KIND]
    return {"mode": reentry.mode(cfg), "limits": reentry.limits(cfg),
            "script": reentry.gate_script(cfg),
            "kinds": sorted(reentry.gate_kinds(cfg)), "night": night,
            "decisions": rows[-50:],
            "lines": reentry.morning_lines(cfg, night)}
