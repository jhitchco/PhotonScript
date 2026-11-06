"""Re-entry gate endpoints (2026-10-15, scheduler/reentry.py).

POST /api/reentry/gate?kind=af|slew|center&label=&filter=&ra=&dec=
     one gate call from NINA's ExternalScript (deploy\\reentry-gate.cmd ->
     `photonscript reentry-gate`). verdict SKIP = NINA skips the gated
     items (start AF / slew / center); anything else = run them.
GET  /api/reentry           mode, limits and tonight's decisions

PS-241: kind=piggy_af&trigger=time|temp|hfr is the Piggy-600 AF back-off
gate (scheduler/piggy_af_backoff.py) on the same script and endpoint.
PS-127: kind=tracking_test&label=&subs= is the sideloaded tracking test's
once-per-night gate (scheduler/tracking_test.py run_gate).
GET  /api/piggy-af/backoff  the back-off decision now and tonight's events
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
                           dec: Optional[float] = None, trigger: str = "",
                           subs: int = 0):
    if kind == "tracking_test":
        import asyncio
        from photonscript.scheduler import tracking_test
        return await asyncio.to_thread(tracking_test.run_gate, _cfg(), label,
                                       subs)
    if kind == "piggy_af":
        from photonscript.scheduler import piggy_af_backoff
        return await piggy_af_backoff.run_gate(_cfg(), trigger or "time", label)
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


@router.get("/api/piggy-af/backoff")
async def api_piggy_af_backoff(trigger: str = "time"):
    """PS-241: what the Piggy-600 AF back-off gate would answer now (not
    logged), its mode and limits, and tonight's gate events."""
    import asyncio
    from photonscript.scheduler import piggy_af_backoff as bo
    from photonscript.shared.phd2_store import night_of
    cfg = _cfg()
    night = night_of(cfg)
    now = await asyncio.to_thread(bo.evaluate, cfg, trigger)
    return {"mode": bo.mode(cfg), "limits": bo.limits(cfg),
            "script": bo.gate_script(cfg), "night": night, "now": now,
            "events": bo._events(cfg, night)[-50:],
            "lines": bo.morning_lines(cfg, night)}
