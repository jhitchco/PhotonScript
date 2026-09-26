"""ARM state machine — hands-off nightly operation.

States:
  DISARMED       nothing scheduled
  ARMED          waiting for pre-config time (dusk - lead)
  RUNNING        sequence dispatched & started (NINA cools, waits for dark,
                 images; its own SafetyMonitor conditions are the backstop)
  PAUSED_UNSAFE  safety monitor went unsafe mid-night; sequence stopped;
                 waiting for safe-again (smart resume) or dawn (make safe)
  COMPLETE       night over — dawn_shutdown() has stopped the sequence,
                 warmed + dew-off'd every rig, parked, and verifies coolers
                 (never assume NINA's End area ran: an all-night-unsafe
                 night leaves the loop wedged in WaitUntilSafe — 2026-09-15
                 both coolers ran at 0°C all day)
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
    "guider": ["/equipment/guider/info"],
    "mount_park": ["/equipment/mount/park"],
    "camera_warm": ["/equipment/camera/warm"],
    "mount_connect": ["/equipment/mount/connect"],
    "camera_connect": ["/equipment/camera/connect"],
    "guider_start": ["/equipment/guider/start"],
    "guider_stop": ["/equipment/guider/stop"],
}

# Guiding watchdog escalation ladder (in TICK_SECONDS units, once past the
# config grace window). A "working" state (calibrating/looping/settling) is
# tolerated briefly to avoid false-firing on a normal dither settle; a hard
# idle/stopped state trips on the first tick.
GUIDING_WORKING_WARN_TICKS = 4      # ~2 min stuck calibrating/looping -> warn
GUIDING_RECOVER_AFTER_TICKS = 6     # ~3 min unlocked -> one auto guider restart
GUIDING_ESCALATE_AFTER_TICKS = 10   # ~5 min unlocked -> priority escalation
SAFETY_NONE_ALERT_TICKS = 6         # ~3 min of an unreadable safety monitor -> alert


class Armer:
    def __init__(self, config):
        self.config = config
        self.state = "DISARMED"
        self.last_raw = None
        self.detail = ""
        self.plan: dict = {}
        self.sequence_path: Path | None = None
        self.guiding_override: str | None = None  # "guided" | "encoders" | None
        self._guiding_alerted = False  # once-per-episode "guided but not guiding"
        self._not_locked_ticks = 0     # consecutive watchdog ticks PHD2 not locked
        self._guiding_recovered = False  # auto guider-restart tried this episode
        self._guiding_escalated = False  # priority escalation sent this episode
        self._cooler_alerted: dict[str, bool] = {}  # per-rig cooler-nanny alert latch
        self._cooler_warm_since: dict[str, datetime] = {}  # per-rig first warm tick
        self._cooler_stuck_alerted: dict[str, bool] = {}   # per-rig "still warm" latch
        self._cooler_cmd_alerted: dict[str, bool] = {}     # per-rig "cool cmd failed" latch
        self._safety_none_ticks = 0    # consecutive ticks safety monitor unreadable
        self._safety_alerted = False   # safety-monitor-blind alert latch (episode)
        self.shutdown: dict | None = None  # last dawn_shutdown record (UX chip)
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
                "shutdown": getattr(self, "shutdown", None),
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
        # Keep the last dawn-shutdown record across restarts regardless of
        # state, so the dashboard chip survives a morning dashboard restart.
        self.shutdown = saved.get("shutdown")
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
        # Reconnect everything (esp. safety) after a restart so equipment that
        # dropped while we were down comes back without waiting for the next
        # sequence phase. Connect-only; safety still gates imaging.
        if getattr(self.config, "connect_all_on_arm", True):
            asyncio.create_task(self.connect_all_rigs())
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
        # When the cooler + dew heater turn ON: cool_lead minutes before astro
        # dusk. Surfaced so the dashboard can show a live countdown to it.
        cool_lead = int(getattr(self.config, "cool_lead_minutes", 30))
        cooler_on_utc = None
        dusk = self.plan.get("dusk_utc")
        if dusk:
            try:
                cooler_on_utc = (datetime.fromisoformat(dusk.rstrip("Z"))
                                 - timedelta(minutes=cool_lead)).isoformat() + "Z"
            except Exception:  # noqa: BLE001
                cooler_on_utc = None
        return {"state": self.state, "detail": self.detail,
                "guiding": "guided" if self._use_guiding() else "encoders",
                "night_of": self.plan.get("night_of"),
                "preconfig_utc": self.plan.get("preconfig_utc"),
                "dusk_utc": self.plan.get("dusk_utc"),
                "dawn_utc": self.plan.get("dawn_utc"),
                "cool_lead_min": cool_lead,
                "cooler_on_utc": cooler_on_utc,
                "shutdown": getattr(self, "shutdown", None),
                "noon_arm": self._noon_arm_status()}

    def _noon_arm_status(self) -> dict:
        """Next noon auto re-arm, surfaced for the dashboard countdown chip."""
        enabled = bool(getattr(self.config, "noon_arm_enabled", False))
        out = {"enabled": enabled,
               "guiding": ("guided" if getattr(self.config, "noon_arm_guided",
                                                True) else "encoders")}
        if not enabled:
            return out
        try:
            from photonscript.shared.localtime import utc_offset_hours
            now = datetime.utcnow()
            off = utc_offset_hours(self.config, now)
            local = now + timedelta(hours=off)
            noon = local.replace(hour=12, minute=0, second=0, microsecond=0)
            if local >= noon:
                noon += timedelta(days=1)
            out["at_utc"] = (noon - timedelta(hours=off)).isoformat() + "Z"
        except Exception:  # noqa: BLE001 — chip is cosmetic, never fatal
            pass
        return out

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
        self._guiding_alerted = False  # fresh night — re-arm the guiding watchdog
        self.shutdown = None  # a fresh arm starts a new night — clear the chip
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
        # Connect everything now (esp. the safety monitor) so a dead/slow
        # device shows up at arm time — hours before dark — not silently at
        # runtime. Connect-only; the night loop still gates imaging on safety.
        conn = ""
        if getattr(self.config, "connect_all_on_arm", True):
            try:
                res = await self.connect_all_rigs()
                conn = " · safety: " + res.get("rc16", {}).get("safetymonitor", "?")
            except Exception as e:  # noqa: BLE001
                logger.warning("connect_all at arm failed: %s", e)
        # Pre-imaging state: force the cooler + dew heater OFF now, so they stay
        # off from arm until the sequence turns them on cool_lead min before dark.
        # ONLY here in the fresh-arm path — never in connect_all/restore, which
        # also run mid-night, so a restart while imaging can't kill cooling.
        if getattr(self.config, "cooler_off_until_precool", True):
            try:
                await self._cooler_dew_off_all()
            except Exception as e:  # noqa: BLE001
                logger.warning("pre-imaging cooler/dew off at arm failed: %s", e)
        await notify(self.config,
                     f"ARMED for {self.plan['night_of']} [{mode}]: "
                     f"{', '.join(self.plan['targets'][:4])} — "
                     f"{self.plan['dark_hours']}h dark window.{conn}",
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
        # Make-safe is an abort: warm instantly (minutes=0 by default) so cutting
        # the TEC isn't held up by a ramp. gradual_warm_minutes can restore one.
        warm_min = float(getattr(self.config, "gradual_warm_minutes", 0.0))
        step_params = {"camera_warm": {"minutes": warm_min}}
        for label, key, connect_key in (
                ("warm", "camera_warm", "camera_connect"),
                ("park", "mount_park", "mount_connect")):
            kw = step_params.get(key, {})
            ok = await self._nina(key, **kw) is not None
            if not ok and "not connected" in (self.detail or "").lower():
                # Try connecting the device, then retry once
                if await self._nina(connect_key) is not None:
                    ok = await self._nina(key, **kw) is not None
                if not ok and "not connected" in (self.detail or "").lower():
                    steps.append(f"{label}:skipped (not connected)")
                    continue
            steps.append(f"{label}:{'ok' if ok else f'FAILED ({self.detail})'}")
        self.last_raw = None
        report = "make-safe " + " · ".join(steps)
        logger.warning(report)
        return report

    async def connect_all(self, rig: str = "rc16") -> dict:
        """Actively connect every device a rig owns — for RC16 that's camera,
        filter wheel, focuser, mount, guider, weather, and ESPECIALLY the safety
        monitor; for the piggyback just its camera + focuser. Reuses preflight's
        connect-with-retry against the rig's NINA instance.

        Connect-only: nothing slews, cools, or opens the roof, and the night
        loop's SafetyMonitorCondition still gates all imaging. This just makes
        equipment (e.g. the AARO Alpaca safety monitor that intermittently times
        out) come up on arm/restart.
        """
        from photonscript.scheduler.preflight import _ensure_connected
        from photonscript.shared.rigs import rig_config, rig_devices
        cfg = rig_config(self.config, rig)
        results = {}
        for dev in rig_devices(rig):
            try:
                connected, _payload, err = await _ensure_connected(
                    cfg, dev, attempts=2)
                results[dev] = "connected" if connected else (err or "not connected")
            except Exception as e:  # noqa: BLE001
                results[dev] = f"{type(e).__name__}: {e}"
        logger.info("connect_all (%s): %s", rig, results)
        return results

    async def connect_all_rigs(self) -> dict:
        """Connect every enabled rig (main + piggyback). Returns {rig: {...}}."""
        from photonscript.shared.rigs import rig_ids
        out = {}
        for rig in rig_ids(self.config):
            out[rig] = await self.connect_all(rig)
        return out

    async def _cooler_dew_off_all(self) -> None:
        """Force the cooler (warm) + dew heater OFF on every enabled rig. Called
        at fresh arm so both stay off until the night sequence turns them on
        cool_lead min before astro dark. Best-effort per rig; never raises."""
        from photonscript.shared.rigs import (rig_ids, rig_config, nina_warm,
                                              nina_dew_heater)
        warm_min = float(getattr(self.config, "gradual_warm_minutes", 0.0))
        for rig in rig_ids(self.config):
            rc = rig_config(self.config, rig)
            try:
                await nina_warm(rc.nina_base_url, minutes=warm_min)
                await nina_dew_heater(rc.nina_base_url, False)
            except Exception as e:  # noqa: BLE001
                logger.warning("cooler/dew off (%s) failed: %s", rig, e)

    # -- dawn shutdown -----------------------------------------------------------

    async def dawn_shutdown(self, reason: str = "dawn") -> str:
        """Positive end-of-night shutdown — never assume NINA's End area ran.

        An all-night-unsafe night leaves the sequence wedged inside
        WaitUntilSafe until the NEXT evening's dispatch replaces it, so the
        End area (dew off + warm) runs ~24 h late and both coolers hold
        setpoint all day (seen 2026-09-15). This stops the sequence, warms +
        dew-offs EVERY rig, parks the mount, then verifies cooler state once
        the warm ramp is done and alerts if anything is still cooling.
        """
        from photonscript.shared.rigs import (rig_ids, rig_config, nina_warm,
                                              nina_dew_heater)
        steps = []
        ok = await self._nina("sequence_stop") is not None
        steps.append(f"stop {'ok' if ok else 'FAILED'}")
        warm_min = float(getattr(self.config, "gradual_warm_minutes", 0.0))
        for rig in rig_ids(self.config):
            rc = rig_config(self.config, rig)
            w = await nina_warm(rc.nina_base_url, minutes=warm_min)
            d = await nina_dew_heater(rc.nina_base_url, False)
            steps.append(f"{rig} warm {'ok' if w.get('ok') else 'FAILED'}"
                         f"/dew {'ok' if d.get('ok') else 'FAILED'}")
        ok = await self._nina("mount_park") is not None
        steps.append(f"park {'ok' if ok else 'FAILED'}")
        self.shutdown = {"at": datetime.utcnow().isoformat() + "Z",
                         "reason": reason, "steps": steps, "verify": None}
        self._persist()
        # Instant warm cuts the TEC now (a ramp would take minutes); still verify
        # + alert after a delay that the cooler actually went off.
        asyncio.create_task(self._verify_shutdown(delay_s=300))
        # Grade + thumbnail the night now (background threads), so the Runs
        # page opens instantly in the morning instead of starting the work on
        # first view. Best-effort: never blocks or fails the shutdown.
        try:
            from photonscript.scheduler.runs import post_night_warm
            nights = post_night_warm(self.config)
            if nights:
                steps.append("prewarm " + ",".join(nights))
        except Exception as e:  # noqa: BLE001
            logger.warning("post-night grade/thumbnail warm failed: %s", e)
        report = " · ".join(steps)
        logger.warning("dawn shutdown (%s): %s", reason, report)
        return report

    async def _verify_shutdown(self, delay_s: int = 300):
        """Post-shutdown check: is every cooler actually OFF? One retry, then a
        priority alert — a cooler at setpoint all day is exactly the failure
        dawn_shutdown exists to prevent, so the check is not optional."""
        from photonscript.shared.rigs import (rig_ids, rig_config, nina_warm,
                                              nina_dew_heater, nina_camera_info)
        await asyncio.sleep(delay_s)
        rigs: dict = {}
        still_on: list[str] = []
        for rig in rig_ids(self.config):
            rc = rig_config(self.config, rig)
            info = await nina_camera_info(rc.nina_base_url)
            if not info:
                rigs[rig] = "unreachable"
                continue
            on = bool(info.get("CoolerOn", False))
            rigs[rig] = {"cooler_on": on,
                         "temp_c": info.get("Temperature"),
                         "dew_on": info.get("DewHeaterOn")}
            if on:
                still_on.append(rig)
                await nina_warm(rc.nina_base_url,
                                minutes=float(getattr(
                                    self.config, "gradual_warm_minutes", 0.0))
                                )  # one retry
                await nina_dew_heater(rc.nina_base_url, False)
        ok = not still_on
        if self.shutdown is not None:
            self.shutdown["verify"] = {"at": datetime.utcnow().isoformat() + "Z",
                                       "ok": ok, "rigs": rigs}
            self._persist()
        if not ok:
            await notify(self.config,
                         "Dawn-shutdown check: cooler STILL ON on "
                         f"{', '.join(still_on)} — retried warm + dew-off once. "
                         "If it persists use Stop & Make Safe and check NINA.",
                         title="PhotonScript shutdown warning", priority=1)

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

    async def _maybe_warn_not_guiding(self, now: datetime) -> None:
        """Guided-but-not-guiding watchdog with escalation + auto-recovery.

        Armed guided but PHD2 isn't actually locked-and-guiding well past the
        grace window? Escalate along a ladder instead of firing once and moving
        on (the 2026-09-24 silent failure was armed-guided/PHD2-idle → every
        900s sub trailed; the 2026-09-26 failure was PHD2 stuck recalibrating,
        "star did not move enough"):
          1. warn once (Pushover) — a hard idle/stopped trips on the first tick;
             a "working" state (calibrating/looping/settling) only after it
             persists (GUIDING_WORKING_WARN_TICKS), so a normal dither settle
             never false-fires;
          2. still unlocked after GUIDING_RECOVER_AFTER_TICKS → ONE automatic
             PHD2 guider restart (opt out with guiding_auto_recover=false) to
             break a stuck loop; the restart doesn't force calibration, so
             Auto-restore reuses a good calibration when one exists;
          3. still unlocked after GUIDING_ESCALATE_AFTER_TICKS → a priority
             escalation Pushover asking for hands-on intervention.
        Recovering to a locked 'Guiding' state resets the episode so the
        watchdog re-arms for a later failure the same night. Fails safe: NINA
        unreachable never alarms or acts.
        """
        if not self._use_guiding():
            return
        dusk = self.plan.get("dusk_utc")
        if not dusk:
            return
        try:
            dusk_dt = datetime.fromisoformat(dusk.rstrip("Z"))
        except Exception:  # noqa: BLE001
            return
        grace = int(getattr(self.config, "guiding_watchdog_grace_min", 20))
        # Give guiding time to start after dark (slew → center → AF → cal → settle).
        if now < dusk_dt + timedelta(minutes=grace):
            return
        data = await self._nina("guider")
        if data is None:
            return  # NINA unreachable — don't false-alarm or act
        payload = data.get("Response", data)
        connected = bool(payload.get("Connected", False))
        state = str(payload.get("State", "") or "") if connected else "disconnected"
        s = state.lower()
        # Locked-and-guiding is the only fully-healthy state. calibrating/looping/
        # settling are "working" — fine transiently (the grace covers real
        # startup), but past the grace, persistent working == a stuck cal loop.
        healthy = s in ("guiding", "settledone")
        working = s in ("calibrating", "looping", "settling")

        if healthy:
            if self._guiding_alerted or self._not_locked_ticks:
                await notify(self.config,
                             "Guiding recovered — PHD2 is locked and guiding "
                             "again.", title="PhotonScript guiding")
            self._not_locked_ticks = 0
            self._guiding_alerted = False
            self._guiding_recovered = False
            self._guiding_escalated = False
            return

        # --- not locked: climb the escalation ladder --------------------------
        self._not_locked_ticks += 1
        ticks = self._not_locked_ticks
        mins = int((now - dusk_dt).total_seconds() // 60)
        what = (f"stuck in '{state}' — calibrating/looping but never locking "
                "(calibration likely failing, e.g. 'star did not move enough': "
                "check mount tracking, or recalibrate near Dec 0 at the meridian)"
                if working else
                f"not guiding (state: {state or 'unknown'})")

        # 1) First warning. Idle trips immediately; a working state must persist.
        warn_ready = (not working) or ticks >= GUIDING_WORKING_WARN_TICKS
        if warn_ready and not self._guiding_alerted:
            self._guiding_alerted = True
            await notify(self.config,
                         f"Armed GUIDED but PHD2 is {what} — {mins} min into "
                         "dark, subs are likely trailing.",
                         title="PhotonScript guiding", priority=1)

        # 2) One automatic guider restart to break a stuck loop.
        if (self._guiding_alerted and ticks >= GUIDING_RECOVER_AFTER_TICKS
                and not self._guiding_recovered
                and getattr(self.config, "guiding_auto_recover", True)):
            self._guiding_recovered = True
            ok = await self._restart_guiding()
            await notify(self.config,
                         "Auto-recovery: restarted PHD2 guiding "
                         f"({'sent' if ok else 'FAILED — guider unreachable'}). "
                         "If it doesn't lock, calibrate at Dec 0 / the meridian "
                         "by hand.", title="PhotonScript guiding", priority=1)

        # 3) Priority escalation — auto-recovery didn't take.
        if (self._guiding_alerted and ticks >= GUIDING_ESCALATE_AFTER_TICKS
                and not self._guiding_escalated):
            self._guiding_escalated = True
            await notify(self.config,
                         f"STILL not guiding {mins} min into dark after auto-"
                         "recovery — every long sub is trailing. Intervene: "
                         "check the PHD2 guide star + mount tracking, "
                         "recalibrate at the meridian, or re-arm.",
                         title="PhotonScript guiding", priority=2)

    async def _restart_guiding(self) -> bool:
        """Best-effort stop→start of PHD2 guiding to break a stuck/idle loop.
        Start does NOT force calibration, so PHD2 Auto-restore reuses a good
        calibration when one exists. Never raises."""
        try:
            await self._nina("guider_stop")
            await asyncio.sleep(2)
            return await self._nina("guider_start", calibrate=False) is not None
        except Exception as e:  # noqa: BLE001
            logger.warning("guider restart failed: %s", e)
            return False

    async def _reconcile_cooler(self, now: datetime) -> None:
        """Camera-temperature nanny. During the imaging window (cool_lead before
        dark → dawn) every rig's cooler should be ON and targeting the setpoint.
        Only called on a safe RUNNING tick, so "safe → the camera is cold" holds.

        Every tick it RE-ASSERTS the correct setpoint with an instant cool when a
        rig is off or above setpoint — idempotent while a cooler is normally
        pulling down, and the fix for a cooler left ON at the WRONG setpoint (the
        2026-09-26 stuck-at-20°C night). But it only ALERTS when the cooler is
        flat OFF — an unambiguous fault — so it never false-alarms during a normal
        cooldown, and never double-alerts with the telescope-agent cooling
        watchdog, which owns the 'cooler on but 0% power / not cooling' case.
        Fails safe: a rig NINA can't be read is left alone. Disable with
        cooler_nanny=false."""
        if not getattr(self.config, "cooler_nanny", True):
            return
        dusk = self.plan.get("dusk_utc")
        if not dusk:
            return
        try:
            dusk_dt = datetime.fromisoformat(dusk.rstrip("Z"))
        except Exception:  # noqa: BLE001
            return
        lead = int(getattr(self.config, "cool_lead_minutes", 30))
        cold_from = dusk_dt - timedelta(minutes=lead)
        if not (cold_from <= now < self._dawn()):
            return  # outside the cold window — cooler is meant to be off
        tol = float(getattr(self.config, "cooling_tolerance_c", 3.0))
        ramp = float(getattr(self.config, "cool_ramp_minutes", 0.0))
        from photonscript.shared.rigs import (rig_ids, rig_config, rig_setpoint,
                                              nina_cool, nina_camera_info)
        for rig in rig_ids(self.config):
            rc = rig_config(self.config, rig)
            info = await nina_camera_info(rc.nina_base_url)
            if not info:
                continue  # unreachable — don't act or alarm
            temp = info.get("Temperature")
            on = bool(info.get("CoolerOn", False))
            if temp is None:
                continue
            setp = rig_setpoint(self.config, rig)
            too_warm = (float(temp) - setp) > tol
            if not on or too_warm:
                # Re-assert the correct setpoint (idempotent; corrects a wrong one).
                res = await nina_cool(rc.nina_base_url, setp, minutes=ramp)  # instant
                if not (res or {}).get("ok", False):
                    # The command itself failed (NINA error / camera gone):
                    # re-asserting silently would hide it all night.
                    if not self._cooler_cmd_alerted.get(rig):
                        self._cooler_cmd_alerted[rig] = True
                        await notify(
                            self.config,
                            f"Cooler nanny: cool command to {rig} FAILED "
                            f"({(res or {}).get('detail', 'no response')}). Sensor "
                            f"{float(temp):.1f}°C, setpoint {setp:.0f}°C. Subs "
                            "are being shot warm.",
                            title="PhotonScript cooler", priority=1)
                else:
                    self._cooler_cmd_alerted[rig] = False
                # Still warm this long into the window, cooler on or not: the
                # re-assert isn't taking. Alert once per episode.
                since = self._cooler_warm_since.setdefault(rig, now)
                stuck_min = int(getattr(self.config, "cooler_stuck_minutes", 20))
                if (too_warm and (now - since) >= timedelta(minutes=stuck_min)
                        and not self._cooler_stuck_alerted.get(rig)):
                    self._cooler_stuck_alerted[rig] = True
                    await notify(
                        self.config,
                        f"Cooler nanny: {rig} still {float(temp):.1f}°C after "
                        f"{stuck_min} min (setpoint {setp:.0f}°C, cooler "
                        f"{'ON' if on else 'OFF'}). Re-asserting isn't working; "
                        "subs above the limit are rejected at grading.",
                        title="PhotonScript cooler", priority=1)
                if not on and not self._cooler_alerted.get(rig):
                    self._cooler_alerted[rig] = True
                    await notify(
                        self.config,
                        f"Cooler nanny: {rig} cooler was OFF in the imaging "
                        f"window — turning it on to {setp:.0f}°C now (instant). "
                        "Subs shot warm won't match the dark library.",
                        title="PhotonScript cooler", priority=1)
            elif not too_warm:
                self._cooler_alerted[rig] = False  # at setpoint — re-arm the latches
                self._cooler_stuck_alerted[rig] = False
                self._cooler_cmd_alerted[rig] = False
                self._cooler_warm_since.pop(rig, None)

    async def _watch_safety_monitor(self, now: datetime, safe: bool | None) -> None:
        """Safety-monitor watchdog. `safe` is True/False when the monitor reads
        cleanly and None when it's UNREADABLE (disconnected/erroring/NINA blip).
        A readable monitor — even reading False (unsafe) — is fine; the danger is
        a monitor that has gone dark, because then nothing here can tell whether
        the roof is open (the 2026-09-26 OSC Alpaca sim that "came off"). Alert
        once after it's been unreadable a few ticks (transient blips filtered),
        reset when it reads cleanly again. Imaging itself still rides NINA's own
        SafetyMonitorCondition regardless."""
        if not getattr(self.config, "safety_monitor_watchdog", True):
            return
        if safe is None:
            self._safety_none_ticks += 1
            if (self._safety_none_ticks >= SAFETY_NONE_ALERT_TICKS
                    and not self._safety_alerted):
                self._safety_alerted = True
                mins = self._safety_none_ticks * TICK_SECONDS // 60
                await notify(
                    self.config,
                    f"Safety monitor UNREADABLE for ~{mins} min — roof gating is "
                    "blind (driver likely disconnected, e.g. the Alpaca monitor "
                    "'came off'). NINA's own SafetyMonitorCondition still gates "
                    "imaging; reconnect the safety monitor.",
                    title="PhotonScript safety", priority=1)
            return
        # Readable again (True or False) — recover the episode.
        if self._safety_alerted:
            await notify(self.config,
                         "Safety monitor readable again — roof gating restored.",
                         title="PhotonScript safety")
        self._safety_none_ticks = 0
        self._safety_alerted = False

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
        # One arm covers both scopes: fire a calibration companion at NINA #2.
        # Best-effort — a piggyback problem never fails the RC16 night.
        await self._dispatch_piggyback_companion()
        return True

    async def _dispatch_piggyback_companion(self) -> None:
        """Dispatch the piggyback calibration companion to NINA #2 alongside the
        RC16 night, so a single arm covers both scopes' calibration. OSC dawn
        flats always; roof-closed OSC darks/bias only when NINA #2 can see the
        shared safety monitor. Never raises — logged + noted, never fatal."""
        cfg = self.config
        if not (getattr(cfg, "piggyback_enabled", False)
                and getattr(cfg, "piggyback_calibrate_on_arm", True)):
            return
        try:
            from photonscript.shared.rigs import (rig_config, nina_dispatch,
                                                  PIGGYBACK)
            from photonscript.scheduler.calibration import (
                generate_piggyback_companion_json)
            pcfg = rig_config(cfg, PIGGYBACK)
            # Auto-detect whether NINA #2 can see the shared safety monitor:
            # actively connect it, then read state. If it's in the NINA #2
            # profile it comes up and the companion gates roof-closed darks/bias
            # on it; if not, the companion is dawn-flats-only. No manual flag.
            from photonscript.scheduler.preflight import _ensure_connected
            # The Alpaca safety-monitor connect on NINA #2 is flaky — a single
            # miss dropped the OSC to flats-only (and left it with no darks/bias).
            # Retry once before giving up.
            has_safety = False
            for _attempt in range(2):
                try:
                    has_safety, _sm_payload, _sm_err = await _ensure_connected(
                        pcfg, "safetymonitor")
                except Exception:  # noqa: BLE001
                    has_safety = False
                if has_safety:
                    break
                await asyncio.sleep(3)
            # Shoot OSC lights while the roof is open, but only when NINA #2 can
            # see the shared safety monitor (has_safety) — lights must be roof-gated.
            want_lights = bool(getattr(cfg, "piggyback_image_lights", True)
                               and has_safety)
            seq_text = generate_piggyback_companion_json(
                pcfg, has_safety=has_safety, with_lights=want_lights)
            seq_dir = self.sequence_path.parent
            seq_dir.mkdir(exist_ok=True)
            path = seq_dir / f"piggyback_companion_{datetime.now():%Y%m%d_%H%M}.json"
            path.write_text(seq_text, encoding="utf-8")
            res = await nina_dispatch(pcfg.nina_base_url, json.loads(seq_text))
            if res.get("ok"):
                logger.info("Piggyback companion dispatched to NINA #2 (%s)",
                            ("lights+flats+darks/bias" if want_lights else
                             ("flats+darks/bias" if has_safety else "flats only")))
                if has_safety:
                    await notify(cfg, "Piggyback companion started on NINA #2 — "
                                 "sees the roof: dawn flats + roof-closed darks/bias"
                                 + (" + OSC lights" if want_lights else ""),
                                 title="PhotonScript piggyback")
                else:
                    # Darks/bias now run UNCONDITIONALLY (time-capped at dusk)
                    # when NINA #2 can't see the safety monitor — the OSC would
                    # otherwise have zero matching calibration. Flag it so the
                    # (slightly-riskier) mode is visible, and name the real fix.
                    await notify(cfg, "OSC darks/bias running UNCONDITIONALLY "
                                 "tonight — NINA #2 can't see the safety monitor, "
                                 "so they're time-capped at dusk instead of "
                                 "roof-gated (a few frames may be junked if the "
                                 "roof opens early). Add the safety monitor to the "
                                 "NINA #2 profile to roof-gate them.",
                                 title="PhotonScript piggyback", priority=1)
            else:
                logger.warning("Piggyback companion dispatch failed: %s",
                               res.get("detail"))
                await notify(cfg, "Piggyback calibration companion did NOT start "
                             f"(NINA #2: {res.get('detail')}). RC16 night is "
                             "unaffected.", title="PhotonScript piggyback",
                             priority=1)
        except Exception as e:  # noqa: BLE001
            logger.warning("Piggyback companion dispatch error: %s", e)

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
                self._set_state("COMPLETE", "Night over — running dawn shutdown")
                report = await self.dawn_shutdown(reason="dawn")
                self._set_state("COMPLETE", f"Dawn shutdown: {report}")
                await notify(self.config,
                             f"Night complete — dawn shutdown ran ({report}). "
                             "Cooler check in 5 min; morning report at 9.",
                             title="PhotonScript complete")
                return
            safe = await self._is_safe()
            await self._watch_safety_monitor(now, safe)
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
            else:
                # Imaging (or looping to safe) — verify guiding actually runs,
                # and that the cooler is actually holding setpoint (nanny).
                await self._maybe_warn_not_guiding(now)
                await self._reconcile_cooler(now)

        elif self.state == "PAUSED_UNSAFE":
            if now >= self._dawn() + timedelta(minutes=30):
                # Do NOT assume the night loop exited: unsafe-at-dawn leaves
                # NINA wedged in WaitUntilSafe and the End area never runs.
                self._set_state("COMPLETE",
                                "Dawn while paused — running dawn shutdown")
                report = await self.dawn_shutdown(reason="dawn while paused")
                self._set_state("COMPLETE", f"Dawn shutdown: {report}")
                await notify(self.config,
                             "Night ended while paused — dawn shutdown "
                             f"stopped the night loop and shut down: {report}",
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
