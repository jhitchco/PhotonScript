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
  - A weather pause is normally NINA's own: the night loop leaves SAFE_LOOP,
    parks and waits (WaitUntilSafe), then re-enters targets by itself. PS-77
    defense in depth: if the monitor has read unsafe for unsafe_stop_grace_s
    and NINA's tree still shows SAFE_LOOP running, the armer stops the
    sequence, stops guiding and parks (cooler kept on), and when it has been
    safe for safety_confirm_seconds it RE-DISPATCHES: the planner subtracts
    subs already accepted tonight, so only the remainder is re-run.
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

from photonscript.scheduler.guiding_alerts import GuidingAlertGate
from photonscript.shared.pushover import notify, record

logger = logging.getLogger(__name__)

TICK_SECONDS = 30
RESUME_MIN_REMAINING_MIN = 40  # don't resume with < this much dark left
ACTIVE_STATES = ("ARMED", "RUNNING", "PAUSED_UNSAFE")

# PS-66: the unguided mode is "unguided" (Paramount MX, TPoint + ProTrack).
# "encoders" was its old name and stays accepted everywhere a mode comes in
# (arm, POST /api/arm, a persisted armer_state.json, auto-arm).
GUIDING_MODE_ALIASES = {"guided": "guided", "unguided": "unguided",
                        "encoders": "unguided"}


def is_direct_guider(name) -> bool:
    """PS-66: NINA's built-in Direct Guider (dithers by pulsing the mount,
    no guide camera). Matches its Name / DisplayName / DeviceId loosely."""
    s = "".join(ch for ch in str(name or "").lower() if ch.isalnum())
    return "directguider" in s


def norm_guiding_mode(value) -> str | None:
    """'guided' | 'unguided' for a known mode name (case-insensitive,
    'encoders' -> 'unguided'), else None (= the config default)."""
    if not isinstance(value, str):
        return None
    return GUIDING_MODE_ALIASES.get(value.strip().lower())

# ninaAPI endpoint candidates (paths vary slightly across plugin versions;
# we try in order until one doesn't 404)
NINA_PATHS = {
    "sequence_load": ["/sequence/load"],
    "sequence_start": ["/sequence/start"],
    "sequence_stop": ["/sequence/stop"],
    "sequence_json": ["/sequence/json"],
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
        self.guiding_override: str | None = None  # "guided" | "unguided" | None
        self._guiding_alerted = False  # once-per-episode "guided but not guiding"
        self._not_locked_ticks = 0     # consecutive watchdog ticks PHD2 not locked
        self._guiding_recovered = False  # auto guider-restart tried this episode
        self._guiding_escalated = False  # priority escalation sent this episode
        self._not_locked_since: datetime | None = None  # start of current streak
        self._guiding_gate = GuidingAlertGate(config)  # PS-66 flood collapse
        self._cooler_alerted: dict[str, bool] = {}  # per-rig cooler-nanny alert latch
        self._cooler_warm_since: dict[str, datetime] = {}  # per-rig first warm tick
        self._cooler_stuck_alerted: dict[str, bool] = {}   # per-rig "still warm" latch
        self._cooler_cmd_alerted: dict[str, bool] = {}     # per-rig "cool cmd failed" latch
        self._safety_none_ticks = 0    # consecutive ticks safety monitor unreadable
        self._safety_alerted = False   # safety-monitor-blind alert latch (episode)
        self.shutdown: dict | None = None  # last dawn_shutdown record (UX chip)
        # PS-77 unsafe-stop episode: when the monitor first read unsafe, when
        # it first read safe again (confirm window), whether the armer had to
        # stop NINA itself (then resume = re-dispatch), and whether this
        # episode's stuck-imaging check is settled.
        self._unsafe_since: datetime | None = None
        self._safe_since: datetime | None = None
        self._unsafe_stopped = False
        self._unsafe_check_done = False
        self._unsafe_tree_misses = 0
        self._hotpix_tried: str | None = None   # PS-91: night of the last map try
        self._fallback_night: str | None = None  # PS-91/92: unguided fallback done
        self._recal_night: str | None = None  # PS-93: mid-night recalibration done
        self._cal_force: str | None = None     # PS-93: reason forcing the slot
        self._audit_task: asyncio.Task | None = None  # PS-89: audit at arm
        self._thesky_task: asyncio.Task | None = None  # PS-104: TheSky audit
        self._tune_night: str | None = None    # PS-90: pre-dusk tune done
        self._tune_note: str | None = None
        self.guider_name: str | None = None  # PS-66: NINA's guider at arm
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
                "unsafe_stopped": bool(getattr(self, "_unsafe_stopped", False)),
                "fallback_night": getattr(self, "_fallback_night", None),
                "recal_night": getattr(self, "_recal_night", None),
                "guider_name": getattr(self, "guider_name", None),
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
        if dawn:
            # The night is over once its dawn shutdown is due (PS-36: that is
            # after the dawn flat window, not astro dawn), so a restart during
            # the flat window still reattaches and still runs the shutdown.
            try:
                over_at = self._shutdown_due_at(saved.get("plan", {}))
            except Exception:  # noqa: BLE001
                over_at = datetime.fromisoformat(dawn.rstrip("Z"))
            if over_at < datetime.utcnow():
                return False  # that night is over
        self.state = saved["state"]
        self.detail = saved.get("detail", "") + " (restored after restart)"
        self.plan = saved.get("plan", {})
        # Preserve the armed guiding mode across restarts, so a mid-night
        # dashboard restart doesn't silently revert to the config default.
        self.guiding_override = norm_guiding_mode(saved.get("guiding_override"))
        self.sequence_path = (Path(saved["sequence_path"])
                              if saved.get("sequence_path") else None)
        # PS-77: an armer safety stop survives a restart, so the resume still
        # re-dispatches instead of waiting on a sequence that is not running.
        self._unsafe_stopped = bool(saved.get("unsafe_stopped", False))
        self._fallback_night = saved.get("fallback_night")
        self._recal_night = saved.get("recal_night")
        self.guider_name = saved.get("guider_name")
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
                "guiding": "guided" if self._use_guiding() else "unguided",
                "night_of": self.plan.get("night_of"),
                "preconfig_utc": self.plan.get("preconfig_utc"),
                "dusk_utc": self.plan.get("dusk_utc"),
                "dawn_utc": self.plan.get("dawn_utc"),
                "shutdown_due_utc": self._shutdown_due_iso(),
                "cool_lead_min": cool_lead,
                "cooler_on_utc": cooler_on_utc,
                "shutdown": getattr(self, "shutdown", None),
                "noon_arm": self._noon_arm_status()}

    def _shutdown_due_iso(self) -> str | None:
        """Planned dawn-shutdown time for the dashboard (None when unarmed)."""
        if self.state not in ACTIVE_STATES or not self.plan.get("dawn_utc"):
            return None
        try:
            return self._shutdown_due_at().isoformat() + "Z"
        except Exception:  # noqa: BLE001 (cosmetic, never fatal)
            return None

    def _noon_arm_status(self) -> dict:
        """Next noon auto re-arm, surfaced for the dashboard countdown chip."""
        enabled = bool(getattr(self.config, "noon_arm_enabled", False))
        out = {"enabled": enabled,
               "guiding": ("guided" if getattr(self.config, "noon_arm_guided",
                                                True) else "unguided")}
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
        ('guided' / 'unguided', alias 'encoders') wins; otherwise fall back to
        config default."""
        override = norm_guiding_mode(getattr(self, "guiding_override", None))
        if override == "guided":
            return True
        if override == "unguided":
            return False
        return bool(self.config.guided_default)

    async def arm(self, guiding: str | None = None) -> dict:
        """guiding: 'guided' (PHD2) or 'unguided' (Paramount MX on TPoint +
        ProTrack; 'encoders' is the old name and still accepted). None or an
        unknown value => use config.guided_default. A guided arm also
        runs the PS-89 PHD2 settings audit in the background (never blocks
        the arm; one push only on a FAIL)."""
        from photonscript.scheduler.night_plan import build_night_plan
        self.guiding_override = norm_guiding_mode(guiding)
        self._guiding_alerted = False  # fresh night — re-arm the guiding watchdog
        self._guiding_gate = GuidingAlertGate(self.config)  # fresh flap history
        self.shutdown = None  # a fresh arm starts a new night — clear the chip
        self.plan = build_night_plan(self.config)
        if "error" in self.plan:
            self._set_state("ERROR", self.plan["error"])
            return self.status()
        mode = ("guided (PHD2)" if self._use_guiding()
                else "unguided (TPoint + ProTrack)")
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
        # PS-66: which guider NINA has (Direct Guider vs PHD2), for the
        # unguided dither choice and a guided-arm sanity alert.
        await self._check_guider_at_arm()
        # PS-89: audit PHD2's settings against the desired state now that the
        # equipment is connected. In the background: it never blocks the arm.
        if self._use_guiding() and getattr(self.config, "phd2_audit_enabled", True):
            self._audit_task = asyncio.create_task(self._phd2_audit_at_arm())
        # PS-104: read-only TheSky / TPoint audit (no push, report only)
        if getattr(self.config, "thesky_audit_enabled", True):
            self._thesky_task = asyncio.create_task(self._thesky_audit_at_arm())
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

    def _unguided_dither(self) -> bool:
        """PS-66: dither the unguided targets tonight? Only on an unguided
        night with config unguided_dither on and NINA's guider (read at arm)
        being Direct Guider. A guided night's unguided fallback never does."""
        return (not self._use_guiding()
                and bool(getattr(self.config, "unguided_dither", False))
                and is_direct_guider(getattr(self, "guider_name", None)))

    async def _check_guider_at_arm(self) -> None:
        """PS-66: read NINA's guider name. Unguided arm with unguided_dither
        on but no Direct Guider: dithers stay off, one note. Guided arm with
        Direct Guider connected: one priority-1 alert (PHD2 StartGuiding,
        the calibration slot and the self-test would all fail). Never raises."""
        detail = self.detail          # _nina overwrites it on a failure
        try:
            data = await self._nina("guider")
        except Exception:  # noqa: BLE001
            data = None
        self.detail = detail
        payload = (data or {}).get("Response", data or {}) if isinstance(data, dict) else {}
        if not isinstance(payload, dict):
            payload = {}
        name = (payload.get("Name") or payload.get("DisplayName")
                or payload.get("DeviceId") or None)
        self.guider_name = str(name) if name else None
        self._persist()
        direct = is_direct_guider(self.guider_name)
        if self._use_guiding():
            if direct:
                await notify(self.config,
                             "Armed GUIDED but NINA's guider is Direct Guider: "
                             "StartGuiding (PHD2) will fail tonight. Switch "
                             "NINA's guider back to PHD2, or re-arm unguided.",
                             title="PhotonScript guider", priority=1)
        elif getattr(self.config, "unguided_dither", False) and not direct:
            await notify(self.config,
                         f"Unguided dithers are OFF tonight: NINA's guider is "
                         f"{self.guider_name or 'unknown'}, not Direct Guider "
                         f"(unguided_dither needs it).",
                         title="PhotonScript guider")

    async def _phd2_audit_at_arm(self) -> None:
        """PS-89 settings audit at arm. Never raises."""
        try:
            from photonscript.scheduler import phd2_audit
            a = await asyncio.wait_for(phd2_audit.at_arm(self.config), 180)
            logger.info("PHD2 settings audit at arm: %s", a.get("counts"))
        except Exception as e:  # noqa: BLE001
            logger.warning("PHD2 settings audit at arm failed: %s", e)

    async def _thesky_audit_at_arm(self) -> None:
        """PS-104 TheSky / TPoint audit at arm (read only, no push). Never
        raises."""
        try:
            from photonscript.scheduler import thesky_audit
            a = await asyncio.wait_for(
                thesky_audit.at_arm(self.config, armer_state=str(self.state or "")), 180)
            logger.info("TheSky audit at arm: %s", (a or {}).get("counts"))
        except Exception as e:  # noqa: BLE001
            logger.warning("TheSky audit at arm failed: %s", e)

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
        """Stop the sequence, stop the guider, warm the camera, park the mount.

        Works from any state: if a device is disconnected, connect it and
        retry; if it stays disconnected that is benign (nothing to make
        safe), reported as 'skipped' rather than FAILED.
        """
        steps = []
        ok = await self._nina("sequence_stop") is not None
        steps.append(f"stop:{'ok' if ok else 'FAILED'}")
        # PS-91: stop PHD2 before parking (NINA's End-area StopGuiding never
        # runs once the sequence is stopped). Never blocks the park.
        steps.append(f"guider stop:{await self._stop_guider_best_effort()}")
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
        await self.cooler_dew_off()

    async def cooler_dew_off(self, skip_rigs: dict | None = None) -> dict:
        """PS-131: the cooler-off step on its own (no arm), so the noon re-arm
        can force the coolers off even when the PS-125 guard skips the arm.
        skip_rigs {rig: why} leaves those rigs alone. Best-effort per rig,
        never raises; returns {rig: short result text}."""
        from photonscript.shared.rigs import (rig_ids, rig_config, nina_warm,
                                              nina_dew_heater)
        warm_min = float(getattr(self.config, "gradual_warm_minutes", 0.0))
        out: dict = {}
        for rig in rig_ids(self.config):
            if skip_rigs and rig in skip_rigs:
                out[rig] = f"left alone ({skip_rigs[rig]})"
                continue
            rc = rig_config(self.config, rig)
            try:
                w = await nina_warm(rc.nina_base_url, minutes=warm_min)
                d = await nina_dew_heater(rc.nina_base_url, False)
                out[rig] = (f"cooler {'off' if w.get('ok') else 'FAILED'}, "
                            f"dew {'off' if d.get('ok') else 'FAILED'}")
            except Exception as e:  # noqa: BLE001
                logger.warning("cooler/dew off (%s) failed: %s", rig, e)
                out[rig] = f"FAILED ({type(e).__name__})"
        return out

    async def _stop_guider_best_effort(self) -> str:
        """PS-91: ask NINA to stop the guider. Returns 'ok' or 'FAILED'.
        Never raises: every shutdown path must still reach the park."""
        try:
            return "ok" if await self._nina("guider_stop") is not None else "FAILED"
        except Exception as e:  # noqa: BLE001
            logger.warning("guider stop failed: %s", e)
            return "FAILED"

    # -- dawn shutdown -----------------------------------------------------------

    async def dawn_shutdown(self, reason: str = "dawn") -> str:
        """Positive end-of-night shutdown — never assume NINA's End area ran.

        An all-night-unsafe night leaves the sequence wedged inside
        WaitUntilSafe until the NEXT evening's dispatch replaces it, so the
        End area (dew off + warm) runs ~24 h late and both coolers hold
        setpoint all day (seen 2026-09-15). This stops the sequence and the
        guider, warms + dew-offs EVERY rig, parks the mount, then verifies cooler state once
        the warm ramp is done and alerts if anything is still cooling.
        """
        from photonscript.shared.rigs import (rig_ids, rig_config, nina_warm,
                                              nina_dew_heater, nina_sequence_stop,
                                              RC16)
        steps = []
        ok = await self._nina("sequence_stop") is not None
        steps.append(f"stop {'ok' if ok else 'FAILED'}")
        # PS-91: stopping the sequence skips its End-area StopGuiding, so PHD2
        # kept "guiding" a parked scope after the roof closed (2026-09-26
        # session 21). Stop it here, before the park; a failure never blocks.
        steps.append(f"guider stop {await self._stop_guider_best_effort()}")
        warm_min = float(getattr(self.config, "gradual_warm_minutes", 0.0))
        for rig in rig_ids(self.config):
            rc = rig_config(self.config, rig)
            if rig != RC16:
                # PS-36: also stop the other rig's sequence (the NINA #2
                # companion). By now its dawn flats are done or impossible; a
                # companion left waiting for safe could otherwise resume
                # lights/flats after sunrise if the roof reopens.
                st = await nina_sequence_stop(rc.nina_base_url)
                steps.append(f"{rig} stop {'ok' if st.get('ok') else 'FAILED'}")
            w = await nina_warm(rc.nina_base_url, minutes=warm_min)
            d = await nina_dew_heater(rc.nina_base_url, False)
            steps.append(f"{rig} warm {'ok' if w.get('ok') else 'FAILED'}"
                         f"/dew {'ok' if d.get('ok') else 'FAILED'}")
        ok = await self._nina("mount_park") is not None
        steps.append(f"park {'ok' if ok else 'FAILED'}")
        self.shutdown = {"at": datetime.utcnow().isoformat() + "Z",
                         "reason": reason, "steps": steps, "verify": None}
        self._persist()
        # Verify + alert once NINA's warm has had time to finish (PS-77: the
        # 5 min check fired mid-warm on 2026-09-27, see _shutdown_verify_delay_s).
        asyncio.create_task(self._verify_shutdown(
            delay_s=self._shutdown_verify_delay_s()))
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

    async def _notify_complete(self, msg: str) -> None:
        """The dawn "Night complete" push. PS-27: adds the Piggy-600 split
        rate when the Piggy took lights; over piggyback_split_alert_pct the
        push goes out at priority 1 with a short hint."""
        from photonscript.scheduler.split_guard import morning_split_note
        line, alert = morning_split_note(self.config, self.plan.get("night_of"))
        if line:
            msg = f"{msg}\n{line}"
        await notify(self.config, msg, title="PhotonScript complete",
                     priority=1 if alert else 0)

    def _shutdown_verify_delay_s(self) -> int:
        """When to check that every cooler really went off after a warm.

        NINA's WarmCamera (CameraVM) sets the setpoint to +20 C and keeps the
        TEC ON until the sensor reaches 19 C; if it cannot (ambient below
        ~19 C, the AARO dawn case) it gives up after a 2 min stall, or after
        its duration + 15 min timeout when the camera keeps reporting cooler
        power, then waits 20 s and turns the cooler off. A 5 min check lands
        mid-warm (2026-09-27 12:23Z: 17.9 C, cooler on) and its retry restarts
        NINA's clock. Wait out the whole window: warm minutes + 17 min."""
        warm_min = float(getattr(self.config, "gradual_warm_minutes", 0.0))
        return max(300, int((warm_min + 17.0) * 60))

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

        PS-66 (2026-09-26: PHD2 flapped LostLock/Guiding all night, 77 guiding
        pushes): pushes go through GuidingAlertGate. Lost / flapping /
        auto-recovery pushes share one budget of one per
        guiding_alert_repeat_min; a flap becomes one "guiding flapping: N
        losses" push; held events are audited; and
        "recovered" pushes only after a loss of guiding_recovered_push_min or
        more. Every held event is still audited (sent=false). The priority-2
        escalation (lost continuously for the whole ladder) still goes out, at
        most once per window.
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
            if self._guiding_alerted:
                # PS-66: a short blip's "recovered" is audit-only (priority -1);
                # only a real outage earns a push.
                push, lost_min = self._guiding_gate.on_recovered(now)
                msg = ("Guiding recovered: PHD2 is locked and guiding again "
                       f"(lost for {lost_min:.0f} min).")
                if push:
                    await notify(self.config, msg, title="PhotonScript guiding")
                else:
                    record(self.config, msg, title="PhotonScript guiding",
                           priority=-1, reason="guiding-short-loss")
            self._not_locked_ticks = 0
            self._not_locked_since = None
            self._guiding_alerted = False
            self._guiding_recovered = False
            self._guiding_escalated = False
            return

        # --- not locked: climb the escalation ladder --------------------------
        self._not_locked_ticks += 1
        ticks = self._not_locked_ticks
        if self._not_locked_since is None:
            self._not_locked_since = now
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
            msg = (f"Armed GUIDED but PHD2 is {what}, {mins} min into "
                   "dark, subs are likely trailing.")
            # PS-66: first loss pushes; repeats inside the window are held
            # (audited) and a flap turns into one "flapping: N losses" push.
            action = self._guiding_gate.on_lost(now, self._not_locked_since)
            if action == "push":
                await notify(self.config, msg, title="PhotonScript guiding",
                             priority=1)
            else:
                record(self.config, msg, title="PhotonScript guiding",
                       priority=1, reason="guiding-repeat-held")
                if action == "flap":
                    await notify(self.config,
                                 self._guiding_gate.flap_message(now),
                                 title="PhotonScript guiding", priority=1)

        # 2) One automatic guider restart to break a stuck loop.
        if (self._guiding_alerted and ticks >= GUIDING_RECOVER_AFTER_TICKS
                and not self._guiding_recovered
                and getattr(self.config, "guiding_auto_recover", True)):
            self._guiding_recovered = True
            ok = await self._restart_guiding()
            msg = ("Auto-recovery: restarted PHD2 guiding "
                   f"({'sent' if ok else 'FAILED, guider unreachable'}). "
                   "If it doesn't lock, calibrate at Dec 0 / the meridian "
                   "by hand.")
            if self._guiding_gate.on_auto_recover(now, ok):
                await notify(self.config, msg, title="PhotonScript guiding",
                             priority=1)
            else:
                record(self.config, msg, title="PhotonScript guiding",
                       priority=1, reason="guiding-repeat-held")

        # 3) Priority escalation — auto-recovery didn't take.
        if (self._guiding_alerted and ticks >= GUIDING_ESCALATE_AFTER_TICKS
                and not self._guiding_escalated):
            self._guiding_escalated = True
            msg = (f"STILL not guiding {mins} min into dark after auto-"
                   "recovery; every long sub is trailing. Intervene: "
                   "check the PHD2 guide star + mount tracking, "
                   "recalibrate at the meridian, or re-arm.")
            if self._guiding_gate.on_escalate(now):
                await notify(self.config, msg, title="PhotonScript guiding",
                             priority=2)
            else:
                record(self.config, msg, title="PhotonScript guiding",
                       priority=2, reason="guiding-repeat-held")

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

    def _record_safety(self, safe, now: datetime) -> None:
        """PS-71: keep a transition log of the monitor so grading can reject
        lights shot while it read unsafe. Never raises."""
        try:
            from photonscript.shared.safety_history import record
            record(self.config, safe, now=now)
        except Exception as e:  # noqa: BLE001
            logger.debug("safety history: %s", e)

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

    # -- dawn flat window (PS-36) ----------------------------------------------

    def _dawn_flats_expected(self) -> bool:
        """Will either rig shoot dawn sky flats? RC16: its End-area flats
        (dawn_flats_enabled). Piggyback: the NINA #2 companion always carries
        an OSC dawn-flat set when it is dispatched."""
        cfg = self.config
        rc16 = bool(getattr(cfg, "dawn_flats_enabled", True))
        pb = bool(getattr(cfg, "piggyback_enabled", False)
                  and getattr(cfg, "piggyback_calibrate_on_arm", True))
        return rc16 or pb

    def _morning_twilight(self, plan: dict) -> tuple:
        """(nautical dawn, sunrise) as naive-UTC datetimes for the armed night.
        From the plan when present; plans saved before PS-36 lack them, so
        compute once (cached per dawn). Either may be None."""
        def _parse(v):
            try:
                return datetime.fromisoformat(v.rstrip("Z")) if v else None
            except (TypeError, ValueError):
                return None
        naut, rise = _parse(plan.get("naut_dawn_utc")), _parse(plan.get("sunrise_utc"))
        if naut is not None:
            return naut, rise
        dawn_s = plan.get("dawn_utc")
        cache = getattr(self, "_twilight_cache", None)
        if cache and cache[0] == dawn_s:
            return cache[1], cache[2]
        naut = rise = None
        try:
            from photonscript.scheduler.night_plan import compute_night_times
            dawn = datetime.fromisoformat(dawn_s.rstrip("Z"))
            # compute_night_times scans 23:00Z on the given date + 15 h
            tw = compute_night_times(self.config.get_observatory(),
                                     dawn - timedelta(days=1))
            naut, rise = tw.get("naut_dawn"), tw.get("sunrise")
            if naut is not None and not (dawn < naut < dawn + timedelta(hours=2)):
                naut = None  # wrong night: fall back to the astro-dawn rule
        except Exception as e:  # noqa: BLE001
            logger.warning("nautical dawn lookup failed: %s", e)
        self._twilight_cache = (dawn_s, naut, rise)
        return naut, rise

    def _shutdown_due_at(self, plan: dict | None = None) -> datetime:
        """When the positive dawn shutdown fires.

        Base rule: astro dawn + 30 min. But both rigs' dawn flats open at
        nautical dawn + 5, and at AARO nautical dawn is 28-37 min after astro
        dawn, so the base rule ALWAYS parked the mount and cut both coolers
        before a single flat (2026-09-26: shutdown 12:18Z, flat window 12:20Z).
        When dawn flats are expected, wait until nautical dawn + 5 +
        dawn_flats_window_min (capped at sunrise). The RC16 End area parks and
        warms itself when its flats finish; this is the backstop."""
        plan = self.plan if plan is None else plan
        dawn = datetime.fromisoformat(plan["dawn_utc"].rstrip("Z"))
        base = dawn + timedelta(minutes=30)
        win = int(getattr(self.config, "dawn_flats_window_min", 40))
        if win <= 0 or not self._dawn_flats_expected():
            return base
        naut, rise = self._morning_twilight(plan)
        if naut is None:
            return base
        end = naut + timedelta(minutes=5 + win)
        if rise is not None and rise > naut:
            end = min(end, rise)
        return max(base, end)

    # -- dispatch -----------------------------------------------------------------

    def _dispatch(self) -> bool:
        """Generate, lint, write tonight's sequence (remainder-aware).

        The planner reads each project's acquired counts, so a re-dispatch
        after a pause only schedules what's still missing.
        """
        from photonscript.shared.astronomy import get_seasonal_targets
        from photonscript.shared.localtime import to_local
        from photonscript.scheduler.target_planner import (
            cap_unguided, create_project_from_target, plan_night_sequence)
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
        # PS-66: unguided targets get the sub-length cap (same integration).
        # Here, after guiding is resolved, so fallback_unguided's re-dispatch
        # is capped too.
        cap_unguided(targets, getattr(self.config, "unguided_max_exposure_s", 300))
        seq = build_sequence_for_night(
            f"PhotonScript_{self.plan['night_of'].replace('-', '')}", targets)

        # Dusk gate in local time (DST-aware); skip if dusk already past
        dusk = datetime.fromisoformat(self.plan["dusk_utc"].rstrip("Z"))
        if now < dusk:
            seq.wait_until_local = to_local(self.config, dusk).strftime("%H:%M:%S")

        cal_field = self._calibration_slot(targets, now) if use_guiding else None
        u_dither = self._unguided_dither()   # PS-66: Direct Guider dithers
        content = generate_nina_json(seq, cal_field=cal_field,
                                     unguided_dither=u_dither)
        result = lint(json.loads(content), guided=use_guiding,
                      unguided_dither=u_dither)
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

    def _calibration_slot(self, targets, now: datetime) -> dict | None:
        """PS-93: the PHD2 calibration field when tonight needs a calibration
        (phd2_calibration.needs_calibration, or a mid-night invalidation in
        _cal_force), else None and the sequence is exactly as before. Saves
        the plan (field, hold) the RC16 agent grades and retries against, and
        consumes a pending manual request. Never raises: a planning error
        means no slot."""
        try:
            from photonscript.scheduler import phd2_calibration as pc
            from photonscript.scheduler.tracking_test import hour_angle
            forced = getattr(self, "_cal_force", None)
            request = pc.pending_request(self.config)
            need = ({"needed": True, "reason": forced} if forced else
                    pc.needs_calibration(pc.load_active(self.config),
                                         pc.load_live(self.config), self.config,
                                         now, request))
            if not need["needed"]:
                logger.info("PHD2 calibration slot: not needed (%s)", need["reason"])
                pc.save_plan(self.config, None)
                return None
            dusk = datetime.fromisoformat(self.plan["dusk_utc"].rstrip("Z"))
            # the slot runs after the twilight AF (about 20 min before astro
            # dusk) or, on a late arm / re-dispatch, a few minutes from now
            when = max(now + timedelta(minutes=5), dusk - timedelta(minutes=20))
            first = targets[0] if targets else None
            ha = (hour_angle(first.ra_hours, max(now, dusk),
                             float(getattr(self.config, "observatory_lon", -109.0)))
                  if first is not None else None)
            field = pc.pick_calibration_field(self.config, when, ha)
            hold = int(getattr(self.config, "phd2_cal_hold_s", 240) or 240)
            pc.save_plan(self.config, {
                "night": self.plan.get("night_of"), "created_utc": f"{now:%Y-%m-%dT%H:%M:%S}Z",
                "reason": need["reason"], "field": field, "hold_s": hold,
                "status": "pending", "attempts": 0, "forced": bool(forced)})
            if request:
                pc.clear_request(self.config)
            logger.info("PHD2 calibration slot: %s at %s (%s)", field.get("name"),
                        field.get("for_utc"), need["reason"])
            return field
        except Exception as e:  # noqa: BLE001 - never fail a dispatch over it
            logger.warning("PHD2 calibration slot skipped: %s", e)
            return None

    async def recalibrate(self, reason: str) -> bool:
        """PS-93 option C, the mid-night fallback: the calibration became
        invalid (guide binning / profile changed, PHD2 uncalibrated, or a
        manual 'now'). Stop the sequence and the guider, then re-dispatch the
        remainder with a PHD2_CALIBRATION slot (companion untouched). Once per
        night, RUNNING only, and only with at least 1 h of dark left (each
        costs 5 to 10 min and repeats the start area)."""
        from photonscript.scheduler import phd2_calibration as pc
        night = self.plan.get("night_of")
        if self.state != "RUNNING" or not night or self._recal_night == night:
            return False
        if pc.cfg_mode(self.config) == "never":
            return False
        if not self._use_guiding():
            # PS-66: an unguided night has no PHD2 calibration to fix; a
            # re-dispatch would only cost 5 to 10 min of dark
            logger.info("recalibration (%s) skipped: night is unguided", reason)
            return False
        now = datetime.utcnow()
        left_h = (self._dawn() - now).total_seconds() / 3600.0
        if left_h < 1.0:
            logger.info("recalibration (%s) skipped: %.1f h of dark left", reason, left_h)
            return False
        self._recal_night = night
        self._persist()
        steps = []
        for label, key in (("stop", "sequence_stop"), ("guider stop", "guider_stop")):
            ok = await self._nina(key) is not None
            steps.append(f"{label} {'ok' if ok else 'FAILED'}")
        self._cal_force = reason
        try:
            ok = await self._dispatch_and_start(companion=False, fail_state=None)
        finally:
            self._cal_force = None
        steps.append(f"re-dispatch {'ok' if ok else 'FAILED'}")
        report = "; ".join(steps)
        self._set_state("RUNNING", f"PHD2 recalibration ({reason}): {report}")
        logger.warning("PHD2 recalibration re-dispatch (%s): %s", reason, report)
        await notify(self.config,
                     f"PHD2 calibration invalid ({reason}): the remainder is "
                     f"re-dispatched with a calibration slot first ({report}).",
                     title="PhotonScript PHD2 calibration", priority=1)
        return ok

    async def dispatch_raw(self, seq: dict, label: str) -> bool:
        """Load + start an arbitrary sequence (calibration). Refused while a
        night is active."""
        if self.state in ("RUNNING", "PAUSED_UNSAFE"):
            self.detail = f"armer is {self.state} — not interrupting"
            return False
        # PS-113: never stop a calibration capture job's sequence on NINA #1
        from photonscript.scheduler.calibration_capture import busy as _cal_busy
        if _cal_busy("rc16"):
            self.detail = "a calibration capture job is running on the RC16"
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

    async def _dispatch_and_start(self, companion: bool = True,
                                  fail_state: str | None = "ERROR") -> bool:
        """companion=False skips the NINA #2 companion (a mid-night re-dispatch
        must not restart the Piggy-600's running sequence). fail_state=None
        leaves the armer state alone on failure (the caller handles it)."""
        if not self._dispatch():
            if fail_state:
                self._set_state(fail_state)
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
            if fail_state:
                self._set_state(fail_state, f"ninaAPI load/start failed "
                                f"({self.detail})")
            await notify(self.config, f"Dispatch failed: {self.detail}",
                         title="PhotonScript ERROR", priority=1)
            return False
        # One arm covers both scopes: fire a calibration companion at NINA #2.
        # Best-effort — a piggyback problem never fails the RC16 night.
        if companion:
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

    # -- PS-77: unsafe but still imaging ------------------------------------------

    @staticmethod
    def _safe_loop_running(tree) -> bool:
        """True if NINA's /sequence/json tree shows SAFE_LOOP (the imaging
        half of the night loop) RUNNING. ninaAPI names containers
        '<Name>_Container', so match the prefix."""
        found = False

        def walk(node):
            nonlocal found
            if found or not isinstance(node, dict):
                return
            if (str(node.get("Name", "")).startswith("SAFE_LOOP")
                    and str(node.get("Status", "")).upper() == "RUNNING"):
                found = True
                return
            for child in node.get("Items") or []:
                walk(child)

        for top in tree if isinstance(tree, list) else [tree]:
            walk(top)
        return found

    async def _maybe_stop_stuck_imaging(self, now: datetime) -> None:
        """Defense in depth for 2026-09-26 (roof closed 11:39Z, RC16 shot OIII
        lights until the dawn shutdown's stop at 12:18Z). Once the monitor has
        read unsafe for unsafe_stop_grace_s, look at NINA's tree: if SAFE_LOOP
        is still RUNNING the night loop never left imaging, so stop the
        sequence, stop guiding and park. The cooler is left at setpoint (no
        warm) so a re-dispatch can image straight away. A tree that cannot be
        read three ticks running is treated as imaging (fail safe). One
        verdict per unsafe episode."""
        if (not getattr(self.config, "unsafe_stop_enabled", True)
                or self._unsafe_check_done or self._unsafe_since is None):
            return
        grace = int(getattr(self.config, "unsafe_stop_grace_s", 120))
        if (now - self._unsafe_since).total_seconds() < grace:
            return
        data = await self._nina("sequence_json")
        tree = None if data is None else data.get("Response", data) \
            if isinstance(data, dict) else data
        if tree is None:
            self._unsafe_tree_misses += 1
            if self._unsafe_tree_misses < 3:
                return
            imaging, why = True, "NINA sequence tree unreadable"
        else:
            imaging = self._safe_loop_running(tree)
            why = "SAFE_LOOP still running"
        self._unsafe_check_done = True
        if not imaging:
            logger.info("unsafe-stop check: NINA night loop left SAFE_LOOP "
                        "on its own; nothing to do")
            return
        steps = []
        for label, key in (("stop", "sequence_stop"), ("guider stop", "guider_stop"),
                           ("park", "mount_park")):
            ok = await self._nina(key) is not None
            steps.append(f"{label} {'ok' if ok else 'FAILED'}")
        self._unsafe_stopped = True
        mins = int((now - self._unsafe_since).total_seconds() // 60)
        report = " · ".join(steps)
        self._set_state("PAUSED_UNSAFE",
                        f"Unsafe {mins} min and NINA kept imaging ({why}): "
                        f"armer stopped the sequence ({report}); cooler kept "
                        "on, re-dispatch when safe")
        logger.warning("PS-77 unsafe stop after %d min (%s): %s", mins, why,
                       report)
        await notify(self.config,
                     f"SAFETY STOP: unsafe for {mins} min but NINA was still "
                     f"imaging ({why}). Armer stopped the sequence, stopped "
                     f"guiding and parked: {report}. Cooler kept at setpoint; "
                     "the remainder is re-dispatched once it has been safe "
                     f"for {int(getattr(self.config, 'safety_confirm_seconds', 120))} s.",
                     title="PhotonScript SAFETY STOP", priority=1)

    async def _resume_after_safety_stop(self, now: datetime) -> None:
        """Safe again after an armer safety stop: nothing is running in NINA,
        so resuming means re-dispatching. Same confirm-safe debounce as the
        sequence's own UNSAFE branch, and no resume with too little dark
        left (the dawn shutdown still runs from PAUSED_UNSAFE)."""
        if self._safe_since is None:
            self._safe_since = now
        confirm = int(getattr(self.config, "safety_confirm_seconds", 120))
        held = (now - self._safe_since).total_seconds()
        if held < confirm:
            msg = f"Safe again, confirming ({int(held)}/{confirm} s)"
            if self.detail != msg:
                self._set_state("PAUSED_UNSAFE", msg)
            return
        left_min = (self._dawn() - now).total_seconds() / 60
        if left_min < RESUME_MIN_REMAINING_MIN:
            msg = (f"Safe again but only {max(0, int(left_min))} min of dark "
                   "left: not re-dispatching; dawn shutdown will run")
            if self.detail != msg:
                self._set_state("PAUSED_UNSAFE", msg)
            return
        if await self._dispatch_and_start(companion=False, fail_state=None):
            self._unsafe_stopped = False
            self._unsafe_since = self._safe_since = None
            self._set_state("RUNNING", "Safe again: remainder re-dispatched "
                                       "after the armer safety stop")
            await notify(self.config,
                         f"RESUMED: safe for {confirm} s, re-dispatched the "
                         f"remainder ({left_min / 60:.1f} h of dark left).",
                         title="PhotonScript resumed")
        else:
            # Stay PAUSED so the dawn shutdown still runs; retry next tick.
            self._safe_since = None
            self._set_state("PAUSED_UNSAFE",
                            f"Re-dispatch after safety stop FAILED "
                            f"({self.detail}); retrying")

    # -- PS-91 / PS-92: guide-camera hot-pixel map + unguided fallback ----------

    HOTPIX_LEAD_MIN = 30   # map window: this long before pre-config

    async def _maybe_hotpix_map(self, now: datetime, why: str) -> None:
        """Refresh the guide-camera hot-pixel map while the roof is closed
        (safety reads unsafe), at most one try per night, in the background.
        guide_hotpix.maybe_capture skips a current map and a busy or absent
        PHD2 by itself. Never raises, never blocks the tick."""
        if not getattr(self.config, "guard_enabled", True):
            return
        night = self.plan.get("night_of") or f"{now:%Y-%m-%d}"
        if self._hotpix_tried == night:
            return
        self._hotpix_tried = night

        async def _run():
            try:
                from photonscript.telescope_agent.guide_hotpix import maybe_capture
                res = await maybe_capture(self.config, why)
                logger.info("hot-pixel map (%s): %s", why, res)
            except Exception as e:  # noqa: BLE001
                logger.warning("hot-pixel map (%s) failed: %s", why, e)
        asyncio.create_task(_run())

    async def fallback_unguided(self, reason: str) -> bool:
        """Switch the rest of tonight to unguided (PS-91 recovery failure /
        PS-92 self-test FAIL): guiding_override="unguided", stop the sequence
        and the guider, re-dispatch the remainder (companion untouched).
        Once per night, RUNNING only.

        NOT reachable with the default config: guard_on_fail and
        selftest_on_fail both default to "alert" until PS-85 caps unguided
        sub lengths (an uncapped 600 s unguided sub at 3248 mm trails)."""
        night = self.plan.get("night_of")
        if self.state != "RUNNING" or not night or self._fallback_night == night:
            return False
        if not self._use_guiding():
            # PS-66: already unguided, nothing to fall back from
            logger.info("unguided fallback (%s) skipped: night is already "
                        "unguided", reason)
            return False
        self._fallback_night = night
        self.guiding_override = "unguided"
        self._persist()
        steps = []
        for label, key in (("stop", "sequence_stop"), ("guider stop", "guider_stop")):
            ok = await self._nina(key) is not None
            steps.append(f"{label} {'ok' if ok else 'FAILED'}")
        ok = await self._dispatch_and_start(companion=False, fail_state=None)
        steps.append(f"re-dispatch {'ok' if ok else 'FAILED'}")
        report = "; ".join(steps)
        self._set_state("RUNNING", f"Unguided fallback ({reason}): {report}")
        logger.warning("unguided fallback (%s): %s", reason, report)
        await notify(self.config,
                     f"Guiding abandoned for tonight ({reason}): the remainder "
                     f"is re-dispatched UNGUIDED ({report}).",
                     title="PhotonScript unguided fallback", priority=1)
        return ok

    # -- PS-90: guide-star gain / binning, pre-dusk ----------------------------

    async def _maybe_predusk_tune(self) -> None:
        """While ARMED and before the dispatch: write the PS-90 tuner's
        recommended guide gain / binning into PHD2's stored profile through
        the PS-89 writer (phd2_tuning.apply_predusk: phd2_audit_autofix,
        PHD2 closed, backup, verified registry names), then re-audit. Retried
        each tick only while PHD2 is running; one outcome per night. Never
        raises."""
        night = self.plan.get("night_of")
        if (not night or getattr(self, "_tune_night", None) == night
                or not self._use_guiding()):
            return
        try:
            from photonscript.scheduler import phd2_tuning
            res = await asyncio.to_thread(phd2_tuning.apply_predusk, self.config,
                                          str(self.state or ""))
        except Exception as e:  # noqa: BLE001
            logger.warning("PS-90 pre-dusk tune failed: %s", e)
            self._tune_night = night
            return
        note = res.get("note")
        if "PHD2 is running" not in str(note or ""):
            self._tune_night = night
        if note != getattr(self, "_tune_note", None):
            self._tune_note = note
            logger.info("PS-90 pre-dusk tune: %s (written %s)", note, res.get("written"))
        if res.get("written"):
            from photonscript.scheduler import phd2_audit
            asyncio.create_task(phd2_audit.run_audit(self.config,
                                                     reason="PS-90 pre-dusk tune"))

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
            if (preconfig - timedelta(minutes=self.HOTPIX_LEAD_MIN) <= now < preconfig
                    and self._hotpix_tried != self.plan.get("night_of")):
                # PS-91: roof closed before dusk = dark guide frames
                if await self._is_safe() is False:
                    await self._maybe_hotpix_map(now, "pre-dusk, roof closed")
            if now < preconfig:
                await self._maybe_predusk_tune()
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
                due = self._shutdown_due_at()
                reason = "dawn"
                if now < due:
                    # Dawn flat window (PS-36): hold the shutdown so both rigs
                    # can shoot flats, unless the roof is closed (no flats
                    # possible: shut down now, as before).
                    safe = await self._is_safe()
                    if safe is not False:
                        hold = (f"Dawn flat window: shutdown held until "
                                f"{due:%H:%M}Z")
                        if self.detail != hold:
                            self._set_state("RUNNING", hold)
                        return
                    reason = "dawn: unsafe, no flats possible"
                self._set_state("COMPLETE", "Night over — running dawn shutdown")
                report = await self.dawn_shutdown(reason=reason)
                self._set_state("COMPLETE", f"Dawn shutdown: {report}")
                await self._notify_complete(
                    f"Night complete: dawn shutdown ran ({reason}; "
                    f"{report}). Cooler check in 5 min; morning report at 9.")
                return
            safe = await self._is_safe()
            self._record_safety(safe, now)
            await self._watch_safety_monitor(now, safe)
            if safe is False:
                # The sequence's own night loop should leave SAFE_LOOP, park
                # and hold via WaitUntilSafe. PS-77: the armer no longer takes
                # that on faith; after unsafe_stop_grace_s it checks NINA's
                # tree and stops the sequence itself if imaging carried on.
                self._unsafe_since = now
                self._safe_since = None
                self._unsafe_check_done = False
                self._unsafe_tree_misses = 0
                grace = int(getattr(self.config, "unsafe_stop_grace_s", 120))
                self._set_state("PAUSED_UNSAFE",
                                f"Unsafe at {now:%H:%M}Z — NINA night loop "
                                "should park and wait for safe")
                await notify(self.config,
                             "PAUSED: unsafe. NINA's night loop should stop "
                             "imaging, park and wait; the armer checks in "
                             f"{grace} s and stops the sequence itself if it "
                             "is still imaging. Auto-resumes when safe.",
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
                await self._notify_complete(
                    "Night ended while paused: dawn shutdown "
                    f"stopped the night loop and shut down: {report}")
                return
            safe = await self._is_safe()
            self._record_safety(safe, now)
            if safe is True:
                if self._unsafe_stopped:
                    await self._resume_after_safety_stop(now)
                    return
                remaining = (self._dawn() - now).total_seconds() / 3600
                self._unsafe_since = self._safe_since = None
                self._set_state("RUNNING", "Safe again — night loop resuming")
                await notify(self.config,
                             f"RESUMED: safe again, {remaining:.1f}h of dark "
                             "left. NINA night loop re-entering targets.",
                             title="PhotonScript resumed")
            else:
                self._safe_since = None  # the confirm window restarts
                if safe is False:
                    if self._unsafe_since is None:  # e.g. restored mid-pause
                        self._unsafe_since = now
                    await self._maybe_stop_stuck_imaging(now)
                    # PS-91: opportunistic map refresh while the roof is shut
                    await self._maybe_hotpix_map(now, "paused unsafe")


def armer_guided_now(config) -> bool:
    """PS-66: True when a night is armed (ARMED / RUNNING / PAUSED_UNSAFE)
    and it is guided. Asks the scheduler's armer when it runs in this
    process, else reads its persisted armer_state.json (any run mode), where
    an unset override means config.guided_default. False when nothing is
    armed or the state is unreadable."""
    import sys
    app_mod = sys.modules.get("photonscript.scheduler.app")
    armer = getattr(app_mod, "_armer", None) if app_mod else None
    if armer is not None:
        try:
            return armer.state in ACTIVE_STATES and bool(armer._use_guiding())
        except Exception:  # noqa: BLE001
            return False
    p = Path(getattr(config, "data_dir", ".")) / "armer_state.json"
    try:
        saved = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - no file / unreadable = not armed
        return False
    if saved.get("state") not in ACTIVE_STATES:
        return False
    mode = norm_guiding_mode(saved.get("guiding_override"))
    if mode is not None:
        return mode == "guided"
    return bool(getattr(config, "guided_default", True))


async def request_recalibration(config, reason: str) -> bool:
    """PS-93: ask the scheduler's armer (same process in `start --mode
    full`) for a mid-night recalibration re-dispatch (Armer.recalibrate).
    False when no armer runs here or it declines."""
    import sys
    app_mod = sys.modules.get("photonscript.scheduler.app")
    armer = getattr(app_mod, "_armer", None) if app_mod else None
    if armer is None:
        logger.warning("recalibration requested (%s) but no armer in this "
                       "process", reason)
        return False
    try:
        return await armer.recalibrate(reason)
    except Exception as e:  # noqa: BLE001
        logger.warning("recalibration re-dispatch failed: %s", e)
        return False


async def request_fallback_unguided(config, reason: str) -> bool:
    """PS-91 / PS-92: ask the scheduler's armer (same process in `start
    --mode full`) to switch the rest of the night to unguided. Callers only
    reach this when guard_on_fail / selftest_on_fail is "unguided"; both
    default to "alert" until PS-85. False when no armer runs here."""
    import sys
    app_mod = sys.modules.get("photonscript.scheduler.app")
    armer = getattr(app_mod, "_armer", None) if app_mod else None
    if armer is None:
        logger.warning("unguided fallback requested (%s) but no armer in this "
                       "process", reason)
        return False
    try:
        return await armer.fallback_unguided(reason)
    except Exception as e:  # noqa: BLE001
        logger.warning("unguided fallback failed: %s", e)
        return False
