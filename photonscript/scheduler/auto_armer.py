"""Auto-arm supervisor (v2): re-arm every night for hands-off multi-day runs.

v1 armed one night at a time and needed a manual re-arm each evening, so a
COMPLETE night just sat idle. That is exactly why 2026-08-07..09 imaged
nothing despite an open roof: the armer finished the night of 08-06 and
nobody re-armed it. This loop watches the armer and, whenever it is idle
(DISARMED / COMPLETE / ERROR) and the next night's pre-config time is near,
arms it automatically. Because arm() rebuilds the plan from the project store
every time (remainder-aware), re-arming IS the nightly replan — the campaign
brain already exists; this just keeps pulling the trigger.

Controls (config / .env):
  PS_AUTO_ARM_ENABLED            master switch (default False — opt in)
  PS_AUTO_ARM_LEAD_HOURS         how long before pre-config the arm window
                                 opens (default 3.0h — recent enough that the
                                 preflight it runs reflects real equipment state)
  PS_AUTO_ARM_REQUIRE_PREFLIGHT  hard-gate on preflight go=true (default False)

Safety model: by default it ARMS AND NOTIFIES even when preflight fails
(Jeremy's call). The AARO site roof controller closes on weather independently
of NINA, so the worst case from a bad preflight is wasted frames that QA
rejects — not a soaked rig. Flip PS_AUTO_ARM_REQUIRE_PREFLIGHT=true to have it
skip-and-notify instead. Manual disarm always wins: the loop never touches an
armer that is ARMED / RUNNING / PAUSED_UNSAFE.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

TICK_SECONDS = 300  # 5 min
TERMINAL_STATES = ("DISARMED", "COMPLETE", "ERROR")


def auto_arm_decision(*, enabled: bool, state: str, now: datetime,
                      preconfig_utc: str | None, night_of: str | None,
                      last_armed_night: str | None,
                      lead_hours: float) -> tuple[bool, str]:
    """Pure, side-effect-free: should the loop arm right now?

    Returns (arm, reason). Kept separate from all the async plumbing so the
    gating logic can be unit-tested without asyncio / httpx / astropy.
    """
    if not enabled:
        return False, "auto-arm disabled"
    if state not in TERMINAL_STATES:
        return False, f"armer busy ({state})"
    if not preconfig_utc:
        return False, "no plan"
    if night_of and night_of == last_armed_night:
        return False, f"already auto-armed {night_of}"
    preconfig = datetime.fromisoformat(preconfig_utc.rstrip("Z"))
    window_open = preconfig - timedelta(hours=lead_hours)
    if now >= preconfig:
        # Past pre-config but still idle (e.g. a night nobody armed): arm so
        # the remaining dark is captured rather than skipped. The armer's
        # ARMED tick dispatches immediately when now >= preconfig.
        return True, "past pre-config — arming for the remaining night"
    if now < window_open:
        return False, f"too early (window opens {window_open:%Y-%m-%d %H:%MZ})"
    return True, "within arm window"


def _fail_summary(preflight: dict) -> str:
    fails = [c["name"] for c in preflight.get("checks", [])
             if c.get("status") == "fail"]
    return ", ".join(fails) if fails else "unknown"


async def run_auto_arm_loop(config, get_armer, *, tick_seconds: int = TICK_SECONDS):
    """Background supervisor. Start once at app startup (guarded by the flag,
    but it also re-checks the flag every tick so it can be toggled live)."""
    from photonscript.scheduler.night_plan import build_night_plan
    from photonscript.scheduler.preflight import run_preflight
    from photonscript.shared.pushover import notify

    last_armed_night: str | None = None
    last_skip_night: str | None = None
    logger.info("Auto-arm loop started (enabled=%s)",
                getattr(config, "auto_arm_enabled", False))

    while True:
        try:
            if getattr(config, "auto_arm_enabled", False):
                armer = get_armer()
                plan = build_night_plan(config)
                if "error" in plan:
                    logger.warning("auto-arm: no plan (%s)", plan["error"])
                else:
                    arm_now, reason = auto_arm_decision(
                        enabled=True,
                        state=armer.state,
                        now=datetime.utcnow(),
                        preconfig_utc=plan.get("preconfig_utc"),
                        night_of=plan.get("night_of"),
                        last_armed_night=last_armed_night,
                        lead_hours=float(getattr(config, "auto_arm_lead_hours", 3.0)),
                    )
                    logger.debug("auto-arm tick: %s (%s)", arm_now, reason)
                    if arm_now:
                        night = plan["night_of"]
                        pf = await run_preflight(config)
                        require = bool(getattr(config, "auto_arm_require_preflight", False))
                        if require and not pf.get("go", False):
                            if last_skip_night != night:
                                await notify(
                                    config,
                                    f"Auto-arm SKIPPED {night}: preflight go=false "
                                    f"({_fail_summary(pf)}). Fix it and it arms next tick.",
                                    title="PhotonScript auto-arm skipped", priority=1)
                                last_skip_night = night
                        else:
                            await armer.arm()  # sends its own ARMED Pushover
                            last_armed_night = night
                            if not pf.get("go", False):
                                await notify(
                                    config,
                                    f"Auto-armed {night} but preflight FAILED "
                                    f"({_fail_summary(pf)}) — imaging anyway per config. "
                                    f"Roof controller still closes on weather.",
                                    title="PhotonScript auto-arm warning", priority=1)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("auto-arm loop error: %s", e)
        await asyncio.sleep(tick_seconds)
