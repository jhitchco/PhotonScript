"""PS-125 auto-arm status for the main dashboard checkboxes.

GET /api/auto-arm
    The evening auto-arm and noon re-arm switches (current values, what each
    does next), tonight's last auto-arm decision (armed / skipped and why) as
    chip text, and tonight's sideload (if any) so the manual Arm buttons can
    confirm before replacing it.

The checkboxes save through POST /api/config (PS_AUTO_ARM_ENABLED,
PS_NOON_ARM_ENABLED, PS_NOON_ARM_GUIDED): the same .env write the System
page does, so a dashboard change survives a restart exactly like a System
page edit. Pure helpers live in scheduler/auto_armer.py. Kept out of app.py
(PS-8).
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

from fastapi import APIRouter

router = APIRouter()

PLAN_TTL_S = 600.0
_plan_cache: dict = {}


def _app():
    from photonscript.scheduler import app
    return app


def _preconfig_utc(config) -> str | None:
    """Tonight's pre-config time from build_night_plan, cached 10 minutes
    (the dashboard polls every 30 s and the plan only moves day to day)."""
    from photonscript.scheduler.night_plan import build_night_plan
    hit = _plan_cache.get("plan")
    if hit and time.monotonic() - hit[0] < PLAN_TTL_S:
        return hit[1]
    try:
        plan = build_night_plan(config)
        pre = None if "error" in plan else plan.get("preconfig_utc")
    except Exception:  # noqa: BLE001 (the chip is cosmetic, never fatal)
        pre = None
    _plan_cache["plan"] = (time.monotonic(), pre)
    return pre


def auto_arm_status(config, armer_state: str, preconfig_utc: str | None,
                    now: datetime | None = None) -> dict:
    from photonscript.scheduler import auto_armer as aa
    from photonscript.shared.localtime import utc_offset_hours
    now = now or datetime.utcnow()
    out = aa.next_actions(config, preconfig_utc, now, armer_state)
    decisions = aa.decisions_tonight(config, now)
    last = decisions[-1] if decisions else None
    out["last_decision"] = last
    out["chip"] = aa.decision_chip(last, utc_offset_hours(config, now))
    out["sideload"] = aa.sideload_tonight(config, now)
    out["armer"] = armer_state
    return out


@router.get("/api/auto-arm")
async def api_auto_arm():
    app = _app()
    cfg = app.get_config()
    pre = await asyncio.to_thread(_preconfig_utc, cfg)
    return auto_arm_status(cfg, str(app.get_armer().state), pre)
