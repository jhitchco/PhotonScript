"""ARM state machine — hands-off nightly operation.

States:
  DISARMED       nothing scheduled
  ARMED          waiting for pre-config time (dusk - lead)
  RUNNING        sequence dispatched & started (NINA cools, waits for dark,
                 images; its own SafetyMonitor conditions are the backstop)
  PAUSED_UNSAFE  safety monitor went unsafe mid-night; sequence stopped;
                 waiting for safe-again (resume) or dawn (shutdown)
  COMPLETE       past dawn; sequence end-area handled warm/park
  ERROR          dispatch or lint failure — human needed

Transitions send Pushover notifications. Re-arm daily is manual (v1).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from photonscript.shared.pushover import notify

logger = logging.getLogger(__name__)

TICK_SECONDS = 30
RESUME_MIN_REMAINING_MIN = 40  # don't resume with < this much dark left


class Armer:
    def __init__(self, config):
        self.config = config
        self.state = "DISARMED"
        self.detail = ""
        self.plan: dict = {}
        self.sequence_path: Path | None = None
        self._task: asyncio.Task | None = None

    # -- public API ---------------------------------------------------------

    def status(self) -> dict:
        return {"state": self.state, "detail": self.detail,
                "night_of": self.plan.get("night_of"),
                "preconfig_utc": self.plan.get("preconfig_utc"),
                "dusk_utc": self.plan.get("dusk_utc"),
                "dawn_utc": self.plan.get("dawn_utc")}

    async def arm(self) -> dict:
        from photonscript.scheduler.night_plan import build_night_plan
        self.plan = build_night_plan(self.config)
        if "error" in self.plan:
            self.state, self.detail = "ERROR", self.plan["error"]
            return self.status()
        self.state = "ARMED"
        self.detail = (f"Pre-config at {self.plan['preconfig_utc']}, "
                       f"{len(self.plan['targets'])} targets, "
                       f"{self.plan['dark_hours']}h dark")
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        await notify(self.config,
                     f"ARMED for {self.plan['night_of']}: "
                     f"{', '.join(self.plan['targets'][:4])} — "
                     f"{self.plan['dark_hours']}h dark window.",
                     title="PhotonScript armed")
        return self.status()

    async def disarm(self) -> dict:
        prev = self.state
        self.state, self.detail = "DISARMED", ""
        if self._task and not self._task.done():
            self._task.cancel()
        if prev in ("RUNNING", "PAUSED_UNSAFE"):
            await self._nina("sequence_stop")
            await notify(self.config, "Disarmed — sequence stopped. "
                         "Check scope state (may not be parked).",
                         title="PhotonScript disarmed", priority=1)
        return self.status()

    # -- helpers -------------------------------------------------------------

    async def _nina(self, key: str, **params):
        base = self.config.nina_base_url.rstrip("/")
        paths = {"sequence_load": "/sequence/load",
                 "sequence_start": "/sequence/start",
                 "sequence_stop": "/sequence/stop",
                 "safety": "/equipment/safetymonitor/info"}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(base + paths[key], params=params or None)
                r.raise_for_status()
                return r.json()
        except Exception as e:  # noqa: BLE001
            logger.error("ninaAPI %s failed: %s", key, e)
            return None

    async def _is_safe(self) -> bool | None:
        data = await self._nina("safety")
        if data is None:
            return None
        payload = data.get("Response", data)
        if not payload.get("Connected", False):
            return None
        return bool(payload.get("IsSafe", False))

    def _dawn(self) -> datetime:
        return datetime.fromisoformat(self.plan["dawn_utc"].rstrip("Z"))

    def _dispatch(self) -> bool:
        """Generate, lint, write tonight's sequence. Returns lint-ok."""
        from photonscript.shared.astronomy import get_seasonal_targets
        from photonscript.scheduler.target_planner import (
            create_project_from_target, plan_night_sequence)
        from photonscript.scheduler.nina_sequence import build_sequence_for_night
        from photonscript.scheduler.nina_sequence_json import generate_nina_json
        from photonscript.scheduler.sequence_lint import lint

        now = datetime.utcnow()
        seasonal = get_seasonal_targets(now.month)
        projects = [create_project_from_target(t) for t in seasonal]
        targets = plan_night_sequence(projects, self.config, now)
        for t in targets:
            t.start_guiding = self.config.guided_default
        seq = build_sequence_for_night(
            f"PhotonScript_{self.plan['night_of'].replace('-', '')}", targets)

        # Dusk gate in local time
        dusk = datetime.fromisoformat(self.plan["dusk_utc"].rstrip("Z"))
        local_dusk = dusk + timedelta(hours=self.config.utc_offset_hours)
        seq.wait_until_local = local_dusk.strftime("%H:%M:%S")

        content = generate_nina_json(seq)
        result = lint(json.loads(content), guided=self.config.guided_default)
        if not result.ok:
            self.detail = "; ".join(f.detail for f in result.findings
                                    if f.level == "ERROR")
            return False
        out = Path.cwd() / "sequences" / f"PhotonScript_{self.plan['night_of']}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content)
        self.sequence_path = out
        return True

    # -- state machine loop ---------------------------------------------------

    async def _run(self):
        try:
            while self.state not in ("DISARMED", "COMPLETE", "ERROR"):
                await self._tick()
                await asyncio.sleep(TICK_SECONDS)
        except asyncio.CancelledError:
            pass

    async def _tick(self):
        now = datetime.utcnow()

        if self.state == "ARMED":
            preconfig = datetime.fromisoformat(self.plan["preconfig_utc"].rstrip("Z"))
            if now >= preconfig:
                if not self._dispatch():
                    self.state = "ERROR"
                    await notify(self.config, f"Dispatch FAILED lint: {self.detail}",
                                 title="PhotonScript ERROR", priority=1)
                    return
                loaded = await self._nina("sequence_load",
                                          sequencePath=str(self.sequence_path))
                started = await self._nina("sequence_start")
                if loaded is None or started is None:
                    self.state = "ERROR"
                    self.detail = "ninaAPI load/start failed"
                    await notify(self.config, "Dispatch failed: ninaAPI "
                                 "load/start error.", title="PhotonScript ERROR",
                                 priority=1)
                    return
                self.state = "RUNNING"
                self.detail = "Sequence started — cooling, imaging at dark"
                await notify(self.config,
                             "Sequence dispatched and started. Cooling now; "
                             "imaging begins at astro dark.",
                             title="PhotonScript running")

        elif self.state == "RUNNING":
            if now >= self._dawn() + timedelta(minutes=30):
                self.state = "COMPLETE"
                await notify(self.config, "Night complete — sequence end-area "
                             "handled warm & park. Morning report at 9.",
                             title="PhotonScript complete")
                return
            safe = await self._is_safe()
            if safe is False:
                await self._nina("sequence_stop")
                self.state = "PAUSED_UNSAFE"
                self.detail = f"Unsafe at {now:%H:%M}Z — sequence stopped"
                await notify(self.config,
                             "PAUSED: safety monitor unsafe — sequence stopped. "
                             "Will auto-resume if safe again tonight.",
                             title="PhotonScript paused", priority=1)

        elif self.state == "PAUSED_UNSAFE":
            remaining = (self._dawn() - now).total_seconds() / 60
            if remaining < RESUME_MIN_REMAINING_MIN:
                self.state = "COMPLETE"
                await notify(self.config,
                             "Dawn reached while paused. Sequence was stopped "
                             "mid-run — VERIFY the scope is warm and parked "
                             "(end-area did not run).",
                             title="PhotonScript: verify scope", priority=1)
                return
            safe = await self._is_safe()
            if safe is True:
                await self._nina("sequence_load",
                                 sequencePath=str(self.sequence_path))
                await self._nina("sequence_start")
                self.state = "RUNNING"
                await notify(self.config,
                             f"RESUMED: safe again, {remaining / 60:.1f}h of "
                             "dark remaining. Sequence restarted.",
                             title="PhotonScript resumed")
