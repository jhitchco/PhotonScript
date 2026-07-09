"""ARM state machine — hands-off nightly operation.

States:
  DISARMED       nothing scheduled
  ARMED          waiting for pre-config time (dusk - lead)
  RUNNING        sequence dispatched & started (NINA cools, waits for dark,
                 images; its own SafetyMonitor conditions are the backstop)
  PAUSED_UNSAFE  safety monitor went unsafe mid-night; sequence stopped;
                 waiting for safe-again (smart resume) or dawn (make safe)
  COMPLETE       night over
  ERROR          dispatch or lint failure — human needed

Resilience:
  - State persists to <data_dir>/armer_state.json on every transition and is
    restored on startup, so a PhotonScript restart mid-night reattaches.
  - Resume after a weather pause RE-DISPATCHES: the planner subtracts subs
    already accepted tonight, so only the remainder is re-run (no repeated
    slews through completed work).
  - make_safe(): stop -> warm camera -> park mount via ninaAPI, used by every
    abort path and exposed as a dashboard button.

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
ACTIVE_STATES = ("ARMED", "RUNNING", "PAUSED_UNSAFE")

# ninaAPI endpoint candidates (paths vary slightly across plugin versions;
# we try in order until one doesn't 404)
NINA_PATHS = {
    "sequence_load": ["/sequence/load"],
    "sequence_start": ["/sequence/start"],
    "sequence_stop": ["/sequence/stop"],
    "safety": ["/equipment/safetymonitor/info"],
    "mount_park": ["/equipment/mount/park"],
    "camera_warm": ["/equipment/camera/warm"],
    "mount_connect": ["/equipment/mount/connect"],
    "camera_connect": ["/equipment/camera/connect"],
}


class Armer:
    def __init__(self, config):
        self.config = config
        self.state = "DISARMED"
        self.last_raw = None
        self.detail = ""
        self.plan: dict = {}
        self.sequence_path: Path | None = None
        self.guiding_override: str | None = None  # "guided" | "encoders" | None
        self._task: asyncio.Task | None = None

    # -- persistence ----------------------------------------------------------

    @property
    def _state_path(self) -> Path:
        return Path(self.config.data_dir) / "armer_state.json"

    def _persist(self):
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps({
                "state": self.state, "detail": self.detail, "plan": self.plan,
                "last_raw": getattr(self, "last_raw", None),
                "guiding_override": getattr(self, "guiding_override", None),
                "sequence_path": str(self.sequence_path) if self.sequence_path else None,
            }, indent=1), encoding="utf-8")
        except OSError as e:
            logger.error("Could not persist armer state: %s", e)

    def restore(self) -> bool:
        """Reattach to a night in progress after a restart. Returns True if resumed."""
        if not self._state_path.exists():
            return False
        try:
            saved = json.loads(self._state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return False
        if saved.get("state") not in ACTIVE_STATES:
            return False
        dawn = saved.get("plan", {}).get("dawn_utc")
        if dawn and datetime.fromisoformat(dawn.rstrip("Z")) < datetime.utcnow():
            return False  # that night is over
        self.state = saved["state"]
        self.detail = saved.get("detail", "") + " (restored after restart)"
        self.plan = saved.get("plan", {})
        # Preserve the armed guiding mode across restarts, so a mid-night
        # dashboard restart doesn't silently revert to the config default.
        self.guiding_override = saved.get("guiding_override")
        self.sequence_path = (Path(saved["sequence_path"])
                              if saved.get("sequence_path") else None)
        self._task = asyncio.create_task(self._run())
        logger.info("Armer restored: %s for %s", self.state,
                    self.plan.get("night_of"))
        asyncio.create_task(notify(
            self.config, f"PhotonScript restarted mid-night — reattached in "
            f"state {self.state}.", title="PhotonScript restored"))
        return True

    def _set_state(self, state: str, detail: str = ""):
        self.state = state
        if detail:
            self.detail = detail
        self._persist()

    # -- public API ------------------------------------------------------------

    def status(self) -> dict:
        return {"state": self.state, "detail": self.detail,
                "guiding": "guided" if self._use_guiding() else "encoders",
                "night_of": self.plan.get("night_of"),
                "preconfig_utc": self.plan.get("preconfig_utc"),
                "dusk_utc": self.plan.get("dusk_utc"),
                "dawn_utc": self.plan.get("dawn_utc")}

    def _use_guiding(self) -> bool:
        """Resolve this night's guiding mode. An explicit arm-time choice
        ('guided' / 'encoders') wins; otherwise fall back to config default."""
        override = getattr(self, "guiding_override", None)
        if override == "guided":
            return True
        if override == "encoders":
            return False
        return bool(self.config.guided_default)

    async def arm(self, guiding: str | None = None) -> dict:
        """guiding: 'guided' (PHD2) or 'encoders' (unguided, CEM70G encoders).
        None => use config.guided_default."""
        from photonscript.scheduler.night_plan import build_night_plan
        self.guiding_override = guiding
        self.plan = build_night_plan(self.config)
        if "error" in self.plan:
            self._set_state("ERROR", self.plan["error"])
            return self.status()
        mode = "guided (PHD2)" if self._use_guiding() else "unguided (encoders)"
        self.last_raw = None; self._set_state("ARMED",
                        f"Pre-config at {self.plan['preconfig_utc']}, "
                        f"{len(self.plan['targets'])} targets, "
                        f"{self.plan['dark_hours']}h dark — {mode}")
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        await notify(self.config,
                     f"ARMED for {self.plan['night_of']} [{mode}]: "
                     f"{', '.join(self.plan['targets'][:4])} — "
                     f"{self.plan['dark_hours']}h dark window.",
                     title="PhotonScript armed")
        return self.status()

    async def disarm(self) -> dict:
        prev = self.state
        self._set_state("DISARMED", "")
        if self._task and not self._task.done():
            self._task.cancel()
        if prev in ("RUNNING", "PAUSED_UNSAFE"):
            report = await self.make_safe()
            await notify(self.config, f"Disarmed — {report}",
                         title="PhotonScript disarmed", priority=1)
        return self.status()

    async def make_safe(self) -> str:
        """Stop the sequence, warm the camera, park the mount.

        Works from any state: if a device is disconnected, connect it and
        retry; if it stays disconnected that is benign (nothing to make
        safe), reported as 'skipped' rather than FAILED.
        """
        steps = []
        ok = await self._nina("sequence_stop") is not None
        steps.append(f"stop:{'ok' if ok else 'FAILED'}")
        for label, key, connect_key in (
                ("warm", "camera_warm", "camera_connect"),
                ("park", "mount_park", "mount_connect")):
            ok = await self._nina(key) is not None
            if not ok and "not connected" in (self.detail or "").lower():
                # Try connecting the device, then retry once
                if await self._nina(connect_key) is not None:
                    ok = await self._nina(key) is not None
                if not ok and "not connected" in (self.detail or "").lower():
                    steps.append(f"{label}:skipped (not connected)")
                    continue
            steps.append(f"{label}:{'ok' if ok else f'FAILED ({self.detail})'}")
        self.last_raw = None
        report = "make-safe " + " · ".join(steps)
        logger.warning(report)
        return report

    # -- ninaAPI helpers ---------------------------------------------------------

    async def _nina(self, key: str, method: str = "GET",
                    json_body=None, **params):
        base = self.config.nina_base_url.rstrip("/")
        for path in NINA_PATHS[key]:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    if method == "POST":
                        r = await client.post(base + path, json=json_body,
                                              params=params or None)
                    else:
                        r = await client.get(base + path, params=params or None)
                    if r.status_code == 404:
                        continue  # try next candidate path
                    r.raise_for_status()
                    data = r.json()
                    if isinstance(data, dict) and data.get("Success") is False:
                        logger.error("ninaAPI %s: %s", key, data.get("Error"))
                        self.detail = f"{key}: {data.get('Error')}"
                        return None
                    return data
            except Exception as e:  # noqa: BLE001
                logger.error("ninaAPI %s (%s) failed: %s", key, path, e)
                self.detail = f"{key}: {e}"
                return None
        logger.error("ninaAPI %s: no endpoint candidate worked", key)
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

    # -- dispatch -----------------------------------------------------------------

    def _dispatch(self) -> bool:
        """Generate, lint, write tonight's sequence (remainder-aware).

        The planner reads each project's acquired counts, so a re-dispatch
        after a pause only schedules what's still missing.
        """
        from photonscript.shared.astronomy import get_seasonal_targets
        from photonscript.shared.localtime import to_local
        from photonscript.scheduler.target_planner import (
            create_project_from_target, plan_night_sequence)
        from photonscript.scheduler.nina_sequence import build_sequence_for_night
        from photonscript.scheduler.nina_sequence_json import generate_nina_json
        from photonscript.scheduler.sequence_lint import lint

        now = datetime.utcnow()

        # Prefer stored projects (priority + budgets); fall back to seasonal
        try:
            from photonscript.scheduler.app import get_store
            projects = [p for p in get_store().projects.values() if p.active]
        except Exception:  # noqa: BLE001
            projects = []
        if not projects:
            projects = [create_project_from_target(t)
                        for t in get_seasonal_targets(now.month)]

        targets = plan_night_sequence(projects, self.config, now)
        if not targets:
            self.detail = "No targets with remaining subs visible tonight"
            return False
        use_guiding = self._use_guiding()
        for t in targets:
            t.start_guiding = use_guiding
        seq = build_sequence_for_night(
            f"PhotonScript_{self.plan['night_of'].replace('-', '')}", targets)

        # Dusk gate in local time (DST-aware); skip if dusk already past
        dusk = datetime.fromisoformat(self.plan["dusk_utc"].rstrip("Z"))
        if now < dusk:
            seq.wait_until_local = to_local(self.config, dusk).strftime("%H:%M:%S")

        content = generate_nina_json(seq)
        result = lint(json.loads(content), guided=use_guiding)
        if not result.ok:
            self.detail = "; ".join(f.detail for f in result.findings
                                    if f.level == "ERROR")
            return False
        out = (Path.cwd() / "sequences"
               / f"PhotonScript_{self.plan['night_of']}_{now:%H%M}.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content)
        self.sequence_path = out
        self._persist()
        try:
            from photonscript.scheduler.runs import save_plan_snapshot
            save_plan_snapshot(self.config, self.plan["night_of"],
                               self.plan, targets)
        except Exception as e:  # noqa: BLE001
            logger.warning("Plan snapshot failed: %s", e)
        return True

    async def dispatch_raw(self, seq: dict, label: str) -> bool:
        """Load + start an arbitrary sequence (calibration). Refused while a
        night is active."""
        if self.state in ("RUNNING", "PAUSED_UNSAFE"):
            self.detail = f"armer is {self.state} — not interrupting"
            return False
        await self._nina("sequence_stop")
        loaded = await self._nina("sequence_load", method="POST",
                                  json_body=seq)
        started = await self._nina("sequence_start", skipValidation="true")
        ok = loaded is not None and started is not None
        if ok:
            self.last_raw = {"label": label,
                             "at": datetime.utcnow().isoformat() + "Z"}
        logger.info("dispatch_raw %s: %s", label, "started" if ok
                    else f"FAILED ({self.detail})")
        return ok

    async def _dispatch_and_start(self) -> bool:
        if not self._dispatch():
            self._set_state("ERROR")
            await notify(self.config, f"Dispatch FAILED: {self.detail}",
                         title="PhotonScript ERROR", priority=1)
            return False
        # Per ninaAPI spec: POST /sequence/load with the sequence JSON as the
        # request body; load 400s if a sequence is running, so stop first.
        await self._nina("sequence_stop")  # harmless if nothing running
        content = json.loads(self.sequence_path.read_text(encoding="utf-8"))
        loaded = await self._nina("sequence_load", method="POST",
                                  json_body=content)
        # skipValidation: our sequence connects equipment in its start area,
        # so pre-start validation ('camera not connected') is expected noise
        started = await self._nina("sequence_start", skipValidation="true")
        if loaded is None or started is None:
            self._set_state("ERROR", f"ninaAPI load/start failed "
                            f"({self.detail})")
            await notify(self.config, f"Dispatch failed: {self.detail}",
                         title="PhotonScript ERROR", priority=1)
            return False
        return True

    # -- state machine loop ----------------------------------------------------

    async def _run(self):
        try:
            while self.state in ACTIVE_STATES:
                await self._tick()
                await asyncio.sleep(TICK_SECONDS)
        except asyncio.CancelledError:
            pass

    async def _tick(self):
        now = datetime.utcnow()

        if self.state == "ARMED":
            preconfig = datetime.fromisoformat(
                self.plan["preconfig_utc"].rstrip("Z"))
            if now >= preconfig:
                if await self._dispatch_and_start():
                    self._set_state("RUNNING",
                                    "Sequence started — cooling, imaging at dark")
                    await notify(self.config,
                                 "Sequence dispatched and started. Cooling now; "
                                 "imaging begins at astro dark.",
                                 title="PhotonScript running")

        elif self.state == "RUNNING":
            if now >= self._dawn() + timedelta(minutes=30):
                self._set_state("COMPLETE")
                await notify(self.config, "Night complete — sequence end-area "
                             "handled warm & park. Morning report at 9.",
                             title="PhotonScript complete")
                return
            safe = await self._is_safe()
            if safe is False:
                # The sequence's own night loop parks and holds via
                # WaitUntilSafe — we observe and notify, we don't interfere.
                self._set_state("PAUSED_UNSAFE",
                                f"Unsafe at {now:%H:%M}Z — NINA night loop "
                                "parked, waiting for safe")
                await notify(self.config,
                             "PAUSED: unsafe — NINA's night loop parked the "
                             "scope and is waiting. Auto-resumes when safe.",
                             title="PhotonScript paused", priority=1)

        elif self.state == "PAUSED_UNSAFE":
            if now >= self._dawn() + timedelta(minutes=30):
                self._set_state("COMPLETE", "Dawn while paused — night loop "
                                "exited; End area handled shutdown")
                await notify(self.config,
                             "Night ended while paused. NINA's End area "
                             "handled park & warm.",
                             title="PhotonScript complete")
                return
            safe = await self._is_safe()
            if safe is True:
                remaining = (self._dawn() - now).total_seconds() / 3600
                self._set_state("RUNNING", "Safe again — night loop resuming")
                await notify(self.config,
                             f"RESUMED: safe again, {remaining:.1f}h of dark "
                             "left. NINA night loop re-entering targets.",
                             title="PhotonScript resumed")
