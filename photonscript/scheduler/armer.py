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
  PAUSED_OPERATOR PS-64: paused from the dashboard. NINA #1's sequence was
                 stopped after its current sub (night_pause.py); tracking,
                 cooler and PHD2 keep running, nothing parks. Resume =
                 the mid-night re-dispatch of the remainder. In
                 ACTIVE_STATES: a night in progress for every gate.
  WATCHING       PS-136: a sideloaded night (PS-123) the armer did not
                 dispatch is running in NINA #1. The armer tracks it like an
                 armed night but NEVER loads, starts or re-dispatches:
                 unsafe pause / resume alerts, guiding watchdog on guided
                 targets, update refusal, events, and at the dawn shutdown
                 time a read-only check (park + coolers) and a summary
                 (watch_dawn_action). Ends at the end of the sequence or at
                 dawn (-> COMPLETE). Not in ACTIVE_STATES on purpose: the
                 auto-arm guard, sideload and calibration gates already treat
                 it as busy, and armer_guided_now (PS-93 recalibration
                 re-dispatch) must not see it.

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
# PS-64: paused by the operator (Pause button); a night in progress.
PAUSE_STATE = "PAUSED_OPERATOR"
ACTIVE_STATES = ("ARMED", "RUNNING", "PAUSED_UNSAFE", PAUSE_STATE)
# PS-136: watching a sideloaded night. LIVE_STATES = the tick loop runs.
WATCH_STATE = "WATCHING"
LIVE_STATES = ACTIVE_STATES + (WATCH_STATE,)
WATCH_FROM_STATES = ("DISARMED", "COMPLETE", "ERROR")  # watch may start from
WATCH_END_IDLE_TICKS = 3   # NINA #1 idle this many ticks running = sequence over
WATCH_DETECT_SECONDS = 60  # auto-adopt poll while the armer is idle

# PS-66: the unguided mode is "unguided" (Paramount MX, TPoint + ProTrack).
# "encoders" was its old name and stays accepted everywhere a mode comes in
# (arm, POST /api/arm, a persisted armer_state.json, auto-arm).
GUIDING_MODE_ALIASES = {"guided": "guided", "unguided": "unguided",
                        "encoders": "unguided"}


def _iso(dt: datetime) -> str:
    """Naive UTC -> '2026-10-07T04:00:00Z' (PS-143 records)."""
    return dt.replace(microsecond=0).isoformat() + "Z"


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
    "mount_info": ["/equipment/mount/info"],
    "camera_info": ["/equipment/camera/info"],   # PS-64 pause wait
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

# PS-144 dusk focus-offset calibration (focus_calibration_tonight): rich,
# uncrowded fields in order of preference; the first one FOCUS_CAL_ALT_LO to
# _HI deg up at dusk is used, else the highest. NGC 7789 first (autumn).
FOCUS_CAL_FIELDS = (("NGC 7789", 23.957, 56.72), ("M52", 23.405, 61.59),
                    ("NGC 663", 1.77, 61.23), ("M37", 5.872, 32.55),
                    ("M35", 6.151, 24.33), ("M67", 8.856, 11.81),
                    ("M11", 18.851, -6.27))
FOCUS_CAL_FILTERS = ("L", "Ha", "L", "OIII", "L", "SII", "L")
FOCUS_CAL_ALT_LO, FOCUS_CAL_ALT_HI = 40.0, 70.0


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
        self._cooler_fight_alerted: dict[str, bool] = {}   # PS-154 per-rig mismatch latch
        self._guider_hold_logged = False  # PS-154 "restart held: mount parked" audit
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
        self.guide_fallback_rec: dict | None = None  # PS-156: tonight's decision
        # PS-85: tonight's per-block guiding decisions (mode auto) that the
        # dispatch applies: {"night", "blocks": {"<target>|<filter>": {...}},
        # "redispatches"}
        self.block_decisions: dict = {}
        self._block_alerts: list[tuple] = []   # (target, text) after a dispatch
        self._recal_night: str | None = None  # PS-93: mid-night recalibration done
        self._cal_force: str | None = None     # PS-93: reason forcing the slot
        self._audit_task: asyncio.Task | None = None  # PS-89: audit at arm
        self._thesky_task: asyncio.Task | None = None  # PS-104: TheSky audit
        self._tune_night: str | None = None    # PS-90: pre-dusk tune done
        self._tune_note: str | None = None
        self.guider_name: str | None = None  # PS-66: NINA's guider at arm
        self.watch: dict | None = None        # PS-136: the watched sideload
        self._watch_declined: str | None = None  # PS-136: sideload "t" stopped by hand
        self.last_validation: dict | None = None  # PS-132 check in dispatch_raw
        self._nina2_task: asyncio.Task | None = None  # PS-139 NINA #2 mount check
        self.pause_info: dict | None = None        # PS-64: the operator pause
        self._pause_task: asyncio.Task | None = None
        self.last_restart: dict | None = None      # PS-143: last restart result
        self.last_dispatch_targets: list[str] = []  # PS-143: names in the push
        # PS-152: tonight's dusk focus calibration (PS-144) once dispatched:
        # {"night", "container", "status": "dispatched" | "done", "field"}
        self.focus_cal: dict | None = None
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
                "guide_fallback": getattr(self, "guide_fallback_rec", None),
                "block_decisions": getattr(self, "block_decisions", None) or {},
                "recal_night": getattr(self, "_recal_night", None),
                "guider_name": getattr(self, "guider_name", None),
                "watch": getattr(self, "watch", None),
                "watch_declined": getattr(self, "_watch_declined", None),
                "pause": getattr(self, "pause_info", None),
                "focus_cal": getattr(self, "focus_cal", None),   # PS-152
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
        self._watch_declined = saved.get("watch_declined")
        if saved.get("state") not in LIVE_STATES:
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
        self.guide_fallback_rec = saved.get("guide_fallback")   # PS-156
        bd = saved.get("block_decisions") or {}
        self.block_decisions = (bd if bd.get("night") == self.plan.get("night_of")
                                else {})
        self._recal_night = saved.get("recal_night")
        self.guider_name = saved.get("guider_name")
        self.watch = saved.get("watch")  # PS-136: a watched night reattaches too
        self.pause_info = saved.get("pause")  # PS-64
        self.focus_cal = saved.get("focus_cal")  # PS-152
        self._task = asyncio.create_task(self._run())
        if (self.state == PAUSE_STATE
                and (self.pause_info or {}).get("phase") == "stopping"):
            # restarted while waiting for the sub to end: finish the stop
            self._pause_task = asyncio.create_task(self._finish_pause())
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
                "watch": (getattr(self, "watch", None)
                          if self.state == WATCH_STATE else None),
                "nina2_mount": self._nina2_mount_status(),
                "pause": (getattr(self, "pause_info", None)
                          if self.state == PAUSE_STATE else None),   # PS-64
                "restart": self._restart_status(),                  # PS-143
                "noon_arm": self._noon_arm_status()}

    def _nina2_mount_status(self) -> dict | None:
        """PS-139: tonight's NINA #2 mount-instruction finding (chip)."""
        try:
            from photonscript.scheduler.nina2_mount_check import tonight
            return tonight(self.config)
        except Exception:  # noqa: BLE001 (cosmetic, never fatal)
            return None

    def _start_nina2_mount_check(self, reason: str) -> None:
        """PS-139: read-only check of NINA #2's loaded sequence for mount
        instructions, in the background (never blocks the arm / watch)."""
        try:
            from photonscript.scheduler.nina2_mount_check import check, enabled
            if enabled(self.config):
                self._nina2_task = asyncio.create_task(check(self.config, reason))
        except Exception as e:  # noqa: BLE001
            logger.warning("NINA #2 mount check not started: %s", e)

    def _shutdown_due_iso(self) -> str | None:
        """Planned dawn-shutdown time for the dashboard (None when unarmed)."""
        if self.state not in LIVE_STATES or not self.plan.get("dawn_utc"):
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
        if self.state == WATCH_STATE:
            # PS-136: arming would stop the watched sideload at pre-config
            # and load PhotonScript's own night over it. Stop watching first.
            return {**self.status(), "refused": (
                "armer is WATCHING a sideloaded night: stop watching first "
                "(arming would replace the sequence NINA is running)")}
        self.guiding_override = norm_guiding_mode(guiding)
        self._guiding_alerted = False  # fresh night — re-arm the guiding watchdog
        self._guiding_gate = GuidingAlertGate(self.config)  # fresh flap history
        self.shutdown = None  # a fresh arm starts a new night — clear the chip
        self._cancel_pause_wait()   # PS-64
        self.pause_info = None
        self.plan = build_night_plan(self.config)
        if (getattr(self, "block_decisions", None) or {}).get("night") != self.plan.get("night_of"):
            self.block_decisions = {}   # PS-85: decisions belong to one night
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
        # PS-139: does NINA #2 hold slew / center / park instructions while
        # the RC16 is about to image? Read only, in the background.
        self._start_nina2_mount_check("arm")
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
        """PS-104 TheSky / TPoint audit at arm (read only; the audit itself
        never pushes). PS-138: on an unguided arm, one ProTrack warning when
        the audit does not read it on. Never raises."""
        try:
            from photonscript.scheduler import thesky_audit
            a = await asyncio.wait_for(
                thesky_audit.at_arm(self.config, armer_state=str(self.state or "")), 180)
            logger.info("TheSky audit at arm: %s", (a or {}).get("counts"))
            if a and not self._use_guiding():
                await self._warn_protrack_unguided(a)
        except Exception as e:  # noqa: BLE001
            logger.warning("TheSky audit at arm failed: %s", e)

    async def _warn_protrack_unguided(self, audit: dict) -> None:
        """PS-138: an unguided night depends on ProTrack. One push when the
        arm-time TheSky audit reads it off (unticked, or greyed because
        TheSky's mount is not connected / tracking) or cannot confirm it.
        A warning only: the arm goes ahead. Never raises."""
        try:
            from photonscript.scheduler.thesky_audit import protrack_status
            p = protrack_status(audit)
            if p["state"] == "on":
                return
            head = ("ProTrack is OFF" if p["state"] == "off"
                    else "ProTrack could not be confirmed on")
            await notify(self.config,
                         f"Armed UNGUIDED but {head} in TheSky "
                         f"({p.get('current') or 'not readable'}): unguided subs "
                         f"trail without it. {p['fix']}",
                         title="PhotonScript ProTrack")
        except Exception as e:  # noqa: BLE001
            logger.warning("ProTrack check at arm failed: %s", e)

    async def disarm(self) -> dict:
        prev = self.state
        if prev == WATCH_STATE:
            # PS-136: stop watching only. The sideloaded sequence is NINA's
            # and keeps running; no make-safe. Never auto-adopt it again.
            self._watch_declined = str(((self.watch or {}).get("sideload")
                                        or {}).get("t") or "") or None
            self._watch_event("stop", "stopped watching by hand")
        self._cancel_pause_wait()   # PS-64
        self.pause_info = None
        self._set_state("DISARMED", "")
        if self._task and not self._task.done():
            self._task.cancel()
        if prev == WATCH_STATE:
            await notify(self.config, "Stopped watching the sideloaded night. "
                         "NINA keeps running it; no dawn check, guiding "
                         "watchdog or update refusal from the armer now.",
                         title="PhotonScript watching")
        if prev in ("RUNNING", "PAUSED_UNSAFE", PAUSE_STATE):
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
        try:  # PS-142: ledgers the desktop integrator posted in the last 24 h
            from photonscript.scheduler.integrations import morning_note
            integ_line = morning_note(self.config)
            if integ_line:
                msg = f"{msg}\n{integ_line}"
        except Exception as e:  # noqa: BLE001 - the dawn push always goes out
            logger.debug("integration morning note unavailable: %s", e)
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
            lead = ("Watching a guided sideloaded target" if self.state == WATCH_STATE
                    else "Armed GUIDED")   # PS-136
            msg = (f"{lead} but PHD2 is {what}, {mins} min into "
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
            if ok is None:
                # PS-154: held, the mount is parked / not tracking;
                # _restart_guiding re-armed the restart for a later tick.
                # Audited once per episode, never pushed (the NINA watch
                # "parked" / "stuck" alarm is the page for that).
                if not self._guider_hold_logged:
                    self._guider_hold_logged = True
                    record(self.config, "Auto-recovery held: not starting "
                           f"PHD2 guiding ({self.detail}).",
                           title="PhotonScript guiding", priority=0,
                           reason="guiding-mount-not-tracking")
            else:
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

        # 4) PS-156: unlocked this long = PHD2 failed; the unguided fallback
        # decides (mode alert / auto, once per night)
        from photonscript.scheduler import guide_fallback as gf
        lost_min = (now - self._not_locked_since).total_seconds() / 60
        if gf.mode(self.config) != "off" and lost_min >= gf.after_min(self.config):
            await self.guide_fallback(
                f"PHD2 not locked and guiding for {lost_min:.0f} min "
                f"(state {state or 'unknown'})", source="watchdog")

    async def _restart_guiding(self) -> bool | None:
        """Best-effort stop→start of PHD2 guiding to break a stuck/idle loop.
        Start does NOT force calibration, so PHD2 Auto-restore reuses a good
        calibration when one exists. Never raises.
        PS-154: not while the mount is parked or not tracking (2026-10-06:
        PHD2 lost the star on a parked RC16, RMS 38"). Then it logs, returns
        None and re-arms the one restart so a later tick retries."""
        why = await self._mount_not_tracking()
        if why:
            logger.warning("guider restart skipped: the mount is %s; retrying "
                           "on a later tick", why)
            self.detail = f"guider_start held: mount {why}"
            self._guiding_recovered = False
            return None
        self._guider_hold_logged = False
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
                # PS-154: a sequence CoolCamera to another temperature is
                # running. Re-asserting would hold the driver at our setpoint
                # while NINA waits for its own, forever (2026-10-06: item at
                # -10 C, nanny at 0 C every 30 s, no lights all night). Page
                # once and leave that rig alone until the item ends.
                seq_t = await self._running_cool_target(rc.nina_base_url)
                if seq_t is not None and abs(seq_t - setp) > tol:
                    if not self._cooler_fight_alerted.get(rig):
                        self._cooler_fight_alerted[rig] = True
                        await notify(
                            self.config,
                            f"Cooler mismatch on {rig}: NINA's sequence is "
                            f"running Cool Camera to {seq_t:g} C but the "
                            f"configured setpoint is {setp:g} C (sensor "
                            f"{float(temp):.1f} C). PhotonScript is NOT "
                            "re-asserting while that item runs; it waits until "
                            "the sensor is within 1 C of its target and may "
                            "never finish. Fix the sequence or skip the item.",
                            title="PhotonScript cooler", priority=1)
                    continue
                self._cooler_fight_alerted[rig] = False
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

    async def _running_cool_target(self, base_url: str) -> float | None:
        """PS-154: the Temperature of a CoolCamera that NINA's sequence is
        running right now, else None (idle, another item, unreadable).
        Read only, never raises."""
        try:
            from photonscript.scheduler.sideload import read_sequence_state
            from photonscript.scheduler.where_panel import running_chain
            tree, _err = await asyncio.wait_for(read_sequence_state(base_url), 10)
            chain = running_chain(tree) if tree is not None else []
        except Exception:  # noqa: BLE001
            return None
        for node in reversed(chain):
            name = "".join(str(node.get("Name") or "").lower().split())
            if ("CoolCamera" in str(node.get("$type") or "")
                    or name == "coolcamera"):
                try:
                    return float(node.get("Temperature"))
                except (TypeError, ValueError):
                    return None
        return None

    async def _mount_not_tracking(self) -> str | None:
        """PS-154: why guiding cannot start now ("parked" / "not tracking"),
        or None when the mount tracks or cannot be read (fail open)."""
        data = await self._nina("mount_info")
        if not isinstance(data, dict):
            return None
        m = data.get("Response", data)
        if not isinstance(m, dict):
            return None
        if m.get("AtPark") is True:
            return "parked"
        trk = m.get("TrackingEnabled", m.get("Tracking"))
        if trk is False:
            return "not tracking"
        return None

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

    async def _crosscheck_unsafe(self, now: datetime) -> str | None:
        """PS-1 cross-check, observe only. Called once when NINA #1 first
        reads unsafe in an episode: read NINA #2's safety monitor (the same
        AARO device through its own driver) once. NINA #2 safe while NINA #1
        reads unsafe = "suspect" (NINA #1's driver or connection, not
        weather); NINA #2 unsafe = "agree"; unreadable = "unverified".
        safety_crosscheck: off | log (one events line) | alert (also one push
        a night on a suspect). Never changes the pause, park or resume.
        Returns the verdict, or None when off."""
        mode = str(getattr(self.config, "safety_crosscheck", "log")
                   or "off").strip().lower()
        if mode not in ("log", "alert"):
            return None
        other = None
        try:
            from photonscript.shared.rigs import PIGGYBACK, rig_config
            base = (rig_config(self.config, PIGGYBACK).nina_base_url
                    or "").rstrip("/")
            if base:
                async with httpx.AsyncClient(timeout=5) as client:
                    r = await client.get(base + NINA_PATHS["safety"][0])
                    r.raise_for_status()
                    data = r.json()
                payload = data.get("Response", data) if isinstance(data, dict) else {}
                if isinstance(payload, dict) and payload.get("Connected"):
                    other = bool(payload.get("IsSafe", False))
        except Exception as e:  # noqa: BLE001 - NINA #2 down = unverified
            logger.debug("safety cross-check: NINA #2 unreadable: %s", e)
        verdict = ("suspect" if other is True else
                   "agree" if other is False else "unverified")
        try:
            from photonscript.shared.night_events import events_path
            from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
            append_jsonl(events_path(self.config, night_of(self.config, now)),
                         {"t": iso_z(now), "rig": "rc16", "src": "safety",
                          "kind": "crosscheck", "value": verdict,
                          "piggyback_safe": other})
        except Exception as e:  # noqa: BLE001
            logger.debug("safety cross-check log: %s", e)
        if verdict == "suspect":
            logger.warning("safety cross-check: NINA #1 unsafe but NINA #2 "
                           "safe at %s", now.isoformat())
            if mode == "alert":
                from photonscript.shared.phd2_store import night_of
                night = night_of(self.config, now)
                if getattr(self, "_crosscheck_alerted", None) != night:
                    self._crosscheck_alerted = night
                    await notify(
                        self.config,
                        f"Safety cross-check at {now:%H:%M}Z: NINA #1 reads "
                        "UNSAFE but NINA #2 reads SAFE on the same AARO "
                        "monitor. Likely NINA #1's driver or connection, not "
                        "weather. Observe only: nothing was changed. Check "
                        "/api/safety/night.",
                        title="PhotonScript safety")
        return verdict

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
        self.last_dispatch_targets = [str(getattr(t, "name", "?")) for t in targets or []]
        if not targets:
            self.detail = "No targets with remaining subs visible tonight"
            return False
        use_guiding = self._use_guiding()
        for t in targets:
            t.start_guiding = use_guiding
        # PS-85: per-block guiding (tonight's decisions plus history); may
        # turn blocks, or a whole target, unguided
        if use_guiding:
            self._apply_block_decisions(targets)
        use_guiding = any(t.start_guiding for t in targets)
        # PS-66: unguided targets get the sub-length cap (same integration).
        # Here, after guiding is resolved, so fallback_unguided's re-dispatch
        # is capped too.
        cap_unguided(targets, getattr(self.config, "unguided_max_exposure_s", 300))
        if self._fallback_night and self._fallback_night == self.plan.get("night_of"):
            # PS-156: the fallback's remainder, per filter at the proven length
            from photonscript.scheduler.guide_fallback import cap_targets
            cap_targets(self.config, targets)
        # PS-144: a one-shot focus-offset calibration first, inside the same
        # night sequence (focus_calibration_tonight; reset after the start)
        self._focus_cal_field = None
        seq_targets = list(targets)
        if bool(getattr(self.config, "focus_calibration_tonight", False)):
            done = self._focus_cal_tonight()
            if done:
                # PS-152: one calibration per night. A re-dispatch (unsafe
                # pause, resume, recalibration, restart) never repeats it,
                # even if the flag is back on (e.g. the .env reset failed and
                # the service restarted)
                logger.info("Focus calibration already %s tonight (%s): not "
                            "repeated", done.get("status"), done.get("container"))
            else:
                cal = self._focus_calibration_target(now)
                if cal is not None:
                    seq_targets.insert(0, cal)
        seq = build_sequence_for_night(
            f"PhotonScript_{self.plan['night_of'].replace('-', '')}", seq_targets)

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

    # -- PS-144 dusk focus-offset calibration ---------------------------------

    def _focus_calibration_target(self, now: datetime):
        """The PS-76 focus_calibration target to put first in tonight's
        sequence: AF in FOCUS_CAL_FILTERS, one round, on a rich field
        FOCUS_CAL_ALT_LO to _HI deg up at dusk (first listed field that fits,
        else the highest). Records the field in self._focus_cal_field.
        Never raises: a planning error means no calibration tonight."""
        try:
            from photonscript.scheduler.nina_sequence_json import FOCUS_CAL_PREFIX
            from photonscript.scheduler.tracking_test import altitude
            from photonscript.shared.models import NinaSequenceTarget
            dusk = datetime.fromisoformat(self.plan["dusk_utc"].rstrip("Z"))
            when = max(now, dusk)
            lat = float(getattr(self.config, "observatory_lat", 31.9))
            lon = float(getattr(self.config, "observatory_lon", -109.0))
            best = None
            for name, ra, dec in FOCUS_CAL_FIELDS:
                alt = altitude(ra, dec, when, lat, lon)
                if FOCUS_CAL_ALT_LO <= alt <= FOCUS_CAL_ALT_HI:
                    best = (name, ra, dec, alt)
                    break
                if best is None or alt > best[3]:
                    best = (name, ra, dec, alt)
            name, ra, dec, alt = best
            self._focus_cal_field = {"name": name, "ra_hours": ra,
                                     "dec_degrees": dec,
                                     "alt_deg": round(alt, 1),
                                     "for_utc": when.isoformat() + "Z"}
            logger.info("Focus calibration first tonight: %s (%.0f deg up at "
                        "%s)", name, alt, when)
            return NinaSequenceTarget(
                name=f"{FOCUS_CAL_PREFIX}{name}", ra_hours=ra,
                dec_degrees=dec, focus_calibration=True,
                focus_calibration_rounds=1,
                focus_calibration_filters=list(FOCUS_CAL_FILTERS))
        except Exception as e:  # noqa: BLE001 - never fail a dispatch over it
            logger.warning("focus calibration skipped: %s", e)
            self._focus_cal_field = None
            return None

    async def _consume_focus_calibration(self) -> None:
        """After a successful dispatch that carried the calibration: turn
        focus_calibration_tonight off (live and in .env, the System page's
        path), log an event and push a note. Never raises."""
        field = getattr(self, "_focus_cal_field", None)
        if not field:
            return
        self._focus_cal_field = None
        self.config.focus_calibration_tonight = False
        from photonscript.scheduler.nina_sequence_json import FOCUS_CAL_PREFIX
        # PS-152: remember it for the night (persisted), so no re-dispatch
        # carries it again; marked "done" once its container finished
        self.focus_cal = {"night": (self.plan or {}).get("night_of"),
                          "container": f"{FOCUS_CAL_PREFIX}{field['name']}",
                          "status": "dispatched", "field": field}
        self._persist()
        saved = True
        try:
            from photonscript.shared import envfile
            envfile.update_env(envfile.env_path(),
                               {"PS_FOCUS_CALIBRATION_TONIGHT": "false"})
        except Exception as e:  # noqa: BLE001
            saved = False
            logger.warning("focus_calibration_tonight not reset in .env: %s", e)
        msg = (f"Focus-offset calibration dispatched first tonight on "
               f"{field['name']} ({field['alt_deg']:.0f} deg up at dusk; AF in "
               f"{','.join(FOCUS_CAL_FILTERS)}, about 25-30 min), then the "
               f"targets. focus_calibration_tonight is now off"
               + ("" if saved else " (live only: the .env write failed)") + ".")
        try:
            from photonscript.shared.night_events import events_path
            from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
            now = datetime.utcnow()
            append_jsonl(events_path(self.config, night_of(self.config, now)),
                         {"t": iso_z(now), "rig": "rc16", "src": "photonscript",
                          "kind": "focus_calibration", "value": "dispatched",
                          "detail": msg, "field": field})
        except Exception as e:  # noqa: BLE001
            logger.warning("focus calibration event not logged: %s", e)
        logger.info(msg)
        try:
            await notify(self.config, msg, title="PhotonScript focus calibration")
        except Exception as e:  # noqa: BLE001
            logger.warning("focus calibration note not sent: %s", e)

    def _focus_cal_tonight(self) -> dict | None:
        """PS-152: tonight's focus-calibration record (dispatched or done),
        or None when none was dispatched for this plan's night."""
        fc = getattr(self, "focus_cal", None)
        if (isinstance(fc, dict) and fc.get("night")
                and fc.get("night") == (self.plan or {}).get("night_of")):
            return fc
        return None

    def _note_focus_cal_progress(self, tree) -> bool:
        """PS-152: mark tonight's dispatched focus calibration "done" when
        NINA's sequence state shows its container FINISHED: log a
        focus_calibration "done" event and persist. True when it did."""
        fc = self._focus_cal_tonight()
        if not fc or fc.get("status") != "dispatched" or tree is None:
            return False
        want = _norm_item(fc.get("container"))
        if _nina_item_status(tree, want) != "FINISHED":
            return False
        now = datetime.utcnow()
        fc["status"] = "done"
        fc["done_utc"] = now.isoformat(timespec="seconds") + "Z"
        self._persist()
        try:
            from photonscript.shared.night_events import events_path
            from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
            append_jsonl(events_path(self.config, night_of(self.config, now)),
                         {"t": iso_z(now), "rig": "rc16", "src": "photonscript",
                          "kind": "focus_calibration", "value": "done",
                          "detail": f"{fc['container']} finished",
                          "field": fc.get("field")})
        except Exception as e:  # noqa: BLE001
            logger.warning("focus calibration done event not logged: %s", e)
        logger.info("Focus calibration finished tonight: %s", fc["container"])
        return True

    async def _check_focus_cal_before_redispatch(self) -> None:
        """PS-152: before a re-dispatch stops NINA, read its sequence state
        once so a finished calibration is recorded as done. Best-effort
        (the re-dispatch leaves the calibration out either way)."""
        fc = self._focus_cal_tonight()
        if not fc or fc.get("status") != "dispatched":
            return
        try:
            tree, err = await asyncio.wait_for(self._watch_read_state(), 15)
            if not err:
                self._note_focus_cal_progress(tree)
        except Exception as e:  # noqa: BLE001
            logger.debug("focus calibration state check skipped: %s", e)

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
        night is active, or watched (PS-136). Between the load and the start
        it reads NINA's own validation (PS-132 nina_validation): problems are
        pushed; nina_load_validation=refuse also skips the start when a
        validator threw."""
        if self.state in ("RUNNING", "PAUSED_UNSAFE", WATCH_STATE, PAUSE_STATE):
            self.detail = f"armer is {self.state}: not interrupting"
            return False
        # PS-113: never stop a calibration capture job's sequence on NINA #1
        from photonscript.scheduler.calibration_capture import busy as _cal_busy
        if _cal_busy("rc16"):
            self.detail = "a calibration capture job is running on the RC16"
            return False
        await self._nina("sequence_stop")
        t0 = datetime.now()   # local, like NINA's log lines
        loaded = await self._nina("sequence_load", method="POST",
                                  json_body=seq)
        if loaded is not None:
            refused = await self._validate_load(t0, label)
            if refused:
                self.detail = refused
                logger.warning("dispatch_raw %s: %s", label, refused)
                return False
        started = await self._nina("sequence_start", skipValidation="true")
        ok = loaded is not None and started is not None
        if ok:
            self.last_raw = {"label": label,
                             "at": datetime.utcnow().isoformat() + "Z"}
        logger.info("dispatch_raw %s: %s", label, "started" if ok
                    else f"FAILED ({self.detail})")
        return ok

    async def _validate_load(self, since: datetime, label: str) -> str | None:
        """PS-132 load validation for the RC16 dispatch_raw (same module and
        config as nina_dispatch: nina_load_validation alert | refuse | off).
        Pushes once on a problem. Returns the refusal text when refuse mode
        and a validator threw, else None. Never raises."""
        try:
            from photonscript.scheduler import nina_validation as nv
            from photonscript.shared.rigs import RC16
            vmode = nv.mode(self.config)
            if vmode == "off":
                return None
            res = await nv.check_loaded(self.config.nina_base_url, self.config,
                                        RC16, since=since)
            self.last_validation = res
            if res["ok"]:
                return None
            await nv.alert(self.config, RC16, res, f"armer dispatch: {label}")
            if vmode == "refuse" and res.get("errors"):
                return ("loaded but NOT started (NINA validation: "
                        + str(res.get("detail")) + ")")
        except Exception as e:  # noqa: BLE001 - never fail a dispatch over it
            logger.warning("load validation (%s) failed: %s", label, e)
        return None

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
        # PS-152: note a finished dusk calibration before the stop clears it
        await self._check_focus_cal_before_redispatch()
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
        await self._consume_focus_calibration()   # PS-144: one-shot reset
        if companion:
            await self._dispatch_piggyback_companion()
        await self._send_block_alerts()   # PS-85: history decisions, once per target
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
            # PS-132: the companion's own lint gates the dispatch (no mount
            # moves, cold setpoint, guarded light loops, and flats NINA #2
            # can validate without a filter wheel). A sequence NINA cannot
            # validate does nothing anyway; say so instead of starting it.
            from photonscript.scheduler.sideload import lint_companion
            from photonscript.scheduler.sequence_lint import format_result
            lint_res = lint_companion(json.loads(seq_text))
            if not lint_res.ok:
                errs = "; ".join(f"[{f.rule}] {f.detail}"
                                 for f in lint_res.findings
                                 if f.level == "ERROR")
                logger.warning("Piggyback companion NOT dispatched, lint "
                               "failed: %s", format_result(lint_res))
                await notify(cfg, "Piggyback companion NOT started: its lint "
                             f"failed ({errs[:400]}). RC16 night is "
                             "unaffected.", title="PhotonScript piggyback",
                             priority=1)
                return
            res = await nina_dispatch(pcfg.nina_base_url, json.loads(seq_text),
                                      config=cfg, rig=PIGGYBACK)
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

    # -- PS-136: watch a sideloaded night ---------------------------------------
    #
    # 2026-10-05: both NINAs ran hand-sideloaded sequences (PS-123) with the
    # armer DISARMED, so nothing checked the dawn shutdown, guiding, unsafe
    # pauses or refused a deploy. WATCHING tracks such a night without ever
    # loading, starting, stopping or re-dispatching a sequence (the only
    # commands it can send: the guiding watchdog's one PHD2 restart, as on an
    # armed night, and watch_dawn_action="shutdown" if chosen).

    def _watch_event(self, value: str, detail: str = "",
                     now: datetime | None = None, **extra) -> None:
        """One kind "watch" line in runs/<night>_events.jsonl. Never raises."""
        try:
            from photonscript.shared.night_events import events_path
            from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
            now = now or datetime.utcnow()
            append_jsonl(events_path(self.config, night_of(self.config, now)),
                         {"t": iso_z(now), "rig": "rc16", "src": "photonscript",
                          "kind": "watch", "value": value, "detail": detail,
                          **extra})
        except Exception as e:  # noqa: BLE001
            logger.warning("watch event (%s) not logged: %s", value, e)

    async def _watch_read_state(self):
        """(tree, error) of NINA #1's sequence state (sideload helper)."""
        from photonscript.scheduler.sideload import read_sequence_state
        return await read_sequence_state(self.config.nina_base_url)

    async def start_watch(self, trigger: str = "button", sideload: dict | None = None,
                    now: datetime | None = None) -> dict:
        """Start WATCHING tonight's sideloaded night. trigger "auto" (the
        detector saw NINA #1 run tonight's RC16 sideload) or "button".
        Refused unless the armer is DISARMED / COMPLETE / ERROR, or once
        tonight's dawn shutdown time has passed. Sends nothing to NINA.
        Returns {"ok", "detail", **status}."""
        now = now or datetime.utcnow()
        if self.state not in WATCH_FROM_STATES:
            return {**self.status(), "ok": False,
                    "detail": f"armer is {self.state}: watch only from "
                              "DISARMED, COMPLETE or ERROR"}
        plan = watch_plan(self.config, now)
        if "error" in plan:
            return {**self.status(), "ok": False, "detail": plan["error"]}
        try:
            due = self._shutdown_due_at(plan)
        except Exception:  # noqa: BLE001
            due = datetime.fromisoformat(plan["dawn_utc"].rstrip("Z")) + timedelta(minutes=30)
        if now >= due:
            return {**self.status(), "ok": False,
                    "detail": f"tonight's dawn shutdown time ({due:%H:%M}Z) "
                              "has passed: nothing to watch"}
        if sideload is None:
            from photonscript.scheduler.auto_armer import sideload_tonight
            sideload = sideload_tonight(self.config, now, rig="rc16")
        sl = dict(sideload or {})
        gt = guided_targets_from_file(sl.get("file"))
        self.plan = plan
        self.sequence_path = Path(sl["file"]) if sl.get("file") else None
        self.guiding_override = (None if gt is None
                                 else ("guided" if gt else "unguided"))
        self._guiding_alerted = False
        self._not_locked_ticks = 0
        self._not_locked_since = None
        self._guiding_recovered = self._guiding_escalated = False
        self._guiding_gate = GuidingAlertGate(self.config)
        self._unsafe_since = self._safe_since = None
        self._unsafe_check_done = False
        self.shutdown = None
        self.last_raw = None
        name = sl.get("value") or "a hand-loaded sequence"
        self.watch = {"since": now.replace(microsecond=0).isoformat() + "Z",
                      "trigger": trigger, "sequence": name,
                      "sideload": ({k: sl.get(k) for k in ("t", "value", "file", "recipe")}
                                   if sl else None),
                      "guided_targets": gt, "paused": False, "pauses": 0,
                      "idle_ticks": 0, "running": [],
                      "shutdown_due_utc": due.isoformat() + "Z"}
        detail = (f"Watching sideloaded night: {name} ({trigger}); dawn check "
                  f"at {due:%H:%M}Z. No dispatch.")
        self._set_state(WATCH_STATE, detail)
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
        self._watch_event("start", detail, now, trigger=trigger, sequence=name,
                          guided_targets=gt)
        self._start_nina2_mount_check("watch")   # PS-139
        guide_txt = ("guiding watchdog on " + ", ".join(gt) if gt else
                     "no guided targets: guiding watchdog off" if gt is not None
                     else "guiding per config (sequence file not read)")
        await notify(self.config,
                     f"WATCHING the sideloaded night ({name}, {trigger}). "
                     f"PhotonScript sends no sequence; it alerts on unsafe "
                     f"pauses and guiding ({guide_txt}), refuses updates, and "
                     f"checks park + coolers at {due:%H:%M}Z.",
                     title="PhotonScript watching")
        return {**self.status(), "ok": True, "detail": detail}

    async def maybe_adopt(self, now: datetime | None = None,
                          reader=None) -> dict | None:
        """The auto path: armer idle, tonight has a successful RC16 sideload
        (not one stopped by hand), and NINA #1 runs something. Returns the
        start_watch() result, or None when nothing to adopt. Never raises."""
        try:
            if (not getattr(self.config, "watch_sideload_auto", True)
                    or self.state not in WATCH_FROM_STATES):
                return None
            from photonscript.scheduler.auto_armer import sideload_tonight
            from photonscript.scheduler.sideload import nina_running
            now = now or datetime.utcnow()
            sl = sideload_tonight(self.config, now, rig="rc16")
            if not sl or (self._watch_declined
                          and str(sl.get("t")) == self._watch_declined):
                return None
            if reader is None:
                tree, err = await self._watch_read_state()
            else:
                tree, err = await reader(self.config.nina_base_url)
            if err or not nina_running(tree):
                return None
            res = await self.start_watch("auto", sideload=sl, now=now)
            return res if res.get("ok") else None
        except Exception as e:  # noqa: BLE001
            logger.warning("watch auto-adopt check failed: %s", e)
            return None

    def _watch_guiding_active(self, running: list[str]) -> bool:
        """Run the guiding watchdog this tick? Only while a target that has
        a StartGuiding in the sideloaded file is RUNNING (so the unguided
        tracking test and unsafe waits never trip it). Without the file:
        config guided_default, and never during a tracking test, an optics
        test or a focus calibration (PS-152: is_test_target)."""
        names = {_norm_item(n) for n in running}
        gt = (self.watch or {}).get("guided_targets")
        if gt is None:
            from photonscript.shared.target_names import is_test_target
            return (bool(getattr(self.config, "guided_default", True))
                    and bool(names)
                    and not any(is_test_target(n) for n in names))
        return any(_norm_item(t) in names for t in gt)

    async def _watch_tick(self, now: datetime) -> None:
        w = self.watch if isinstance(self.watch, dict) else {}
        self.watch = w
        if now >= self._shutdown_due_at():
            await self._watch_end("dawn", now)
            return
        tree, err = await self._watch_read_state()
        if err is None:
            from photonscript.scheduler.sideload import nina_running
            running = nina_running(tree)
            w["running"] = running[-3:]
            if running:
                w["idle_ticks"] = 0
            else:
                w["idle_ticks"] = int(w.get("idle_ticks") or 0) + 1
                if w["idle_ticks"] >= WATCH_END_IDLE_TICKS:
                    await self._watch_end("sequence ended", now)
                    return
        else:
            running = []   # unreadable: never ends the watch, never alarms
        safe = await self._is_safe()
        self._record_safety(safe, now)
        await self._watch_safety_monitor(now, safe)
        if safe is False:
            if not w.get("paused"):
                w["paused"] = True
                w["pauses"] = int(w.get("pauses") or 0) + 1
                self._unsafe_since = now
                self._unsafe_check_done = False
                await self._crosscheck_unsafe(now)   # PS-1, observe only
                msg = (f"Watching: unsafe at {now:%H:%M}Z. The sideloaded "
                       "sequence should stop imaging, park and wait for safe "
                       "by itself; PhotonScript will not stop or restart it.")
                self._set_state(WATCH_STATE, msg)
                self._watch_event("pause", msg, now)
                await notify(self.config, "PAUSED (watching): unsafe. The "
                             "sideloaded sequence's own safety branch should "
                             "park and wait; the armer alerts if it keeps "
                             "imaging.", title="PhotonScript watching",
                             priority=1)
            else:
                await self._watch_stuck_imaging(now, tree)
            return
        if safe is True and w.get("paused"):
            mins = (int((now - self._unsafe_since).total_seconds() // 60)
                    if self._unsafe_since else 0)
            w["paused"] = False
            self._unsafe_since = None
            msg = f"Watching: safe again after {mins} min; sequence resuming"
            self._set_state(WATCH_STATE, msg)
            self._watch_event("resume", msg, now)
            left = (self._dawn() - now).total_seconds() / 3600
            await notify(self.config, f"RESUMED (watching): safe again, "
                         f"{max(0.0, left):.1f} h of dark left. The sideloaded "
                         "sequence re-enters its targets by itself.",
                         title="PhotonScript watching")
        # PS-64: an operator pause of a watched night mutes the guiding
        # watchdog only (alert-only: no NINA command is ever sent)
        if (running and self._watch_guiding_active(running)
                and not w.get("operator_paused")):
            await self._maybe_warn_not_guiding(now)
        self._persist()

    async def _watch_stuck_imaging(self, now: datetime, tree) -> None:
        """PS-77's check, alert only: unsafe for unsafe_stop_grace_s and
        SAFE_LOOP still RUNNING. The armer cannot re-dispatch a sideloaded
        night, so it never stops it; one priority push per episode."""
        if self._unsafe_check_done or self._unsafe_since is None or tree is None:
            return
        grace = int(getattr(self.config, "unsafe_stop_grace_s", 120))
        if (now - self._unsafe_since).total_seconds() < grace:
            return
        self._unsafe_check_done = True
        if not self._safe_loop_running(tree):
            return
        mins = int((now - self._unsafe_since).total_seconds() // 60)
        msg = (f"Unsafe {mins} min but the sideloaded sequence is still "
               "imaging (SAFE_LOOP running). Watch mode does not stop it: "
               "check NINA, or use Stop & Make Safe.")
        self._watch_event("stuck_imaging", msg, now)
        await notify(self.config, msg, title="PhotonScript watching", priority=1)

    async def _watch_end(self, reason: str, now: datetime) -> None:
        """End of the watched night: the sequence ended, or the dawn
        shutdown time (PS-36 timing) came. Summary push, events, the
        lifecycle chip, then a read-only park + cooler check after the warm
        window. watch_dawn_action="shutdown" runs dawn_shutdown at dawn
        instead (stop, guider stop, warm, park), whose verify follows."""
        w = self.watch if isinstance(self.watch, dict) else {}
        still = []
        if reason == "dawn":
            tree, err = await self._watch_read_state()
            if err is None:
                from photonscript.scheduler.sideload import nina_running
                still = nina_running(tree)
        w["ended"] = {"at": now.replace(microsecond=0).isoformat() + "Z",
                      "reason": reason, "still_running": still[:5]}
        action = str(getattr(self.config, "watch_dawn_action", "verify")
                     or "verify").strip().lower()
        since = str(w.get("since") or "")[11:16]
        stats = (f"watched since {since}Z ({w.get('trigger')}), "
                 f"{int(w.get('pauses') or 0)} unsafe pause(s)")
        if reason == "dawn" and action == "shutdown":
            self._set_state("COMPLETE", "Watched night over: running dawn shutdown")
            report = await self.dawn_shutdown(reason="watched night: dawn")
            self._set_state("COMPLETE", f"Watched night, dawn shutdown: {report}")
            self._watch_event("end", report, now, reason=reason, action=action)
            await self._notify_complete(
                f"Watched night complete ({reason}; {stats}). Dawn shutdown "
                f"ran: {report}. Cooler check follows.")
            return
        steps = ["watch only: no commands sent"]
        if still:
            steps.append("NINA #1 still running: " + ", ".join(still[:3]))
        self.shutdown = {"at": now.replace(microsecond=0).isoformat() + "Z",
                         "reason": f"watched night: {reason}", "steps": steps,
                         "verify": None, "watched": True}
        delay = self._shutdown_verify_delay_s()
        self._set_state("COMPLETE", f"Watched night over ({reason}): "
                        f"checking park + coolers in {delay // 60} min")
        self._watch_event("end", "; ".join(steps), now, reason=reason,
                          action="verify")
        asyncio.create_task(self._verify_watched(delay_s=delay))
        try:
            from photonscript.scheduler.runs import post_night_warm
            post_night_warm(self.config)
        except Exception as e:  # noqa: BLE001
            logger.warning("post-night grade/thumbnail warm failed: %s", e)
        if still:
            await notify(self.config,
                         f"Watched night at its dawn shutdown time ({stats}) "
                         "but NINA #1 is STILL running "
                         f"({', '.join(still[:3])}). Watch mode sends no "
                         "commands: check NINA, or use Stop & Make Safe.",
                         title="PhotonScript watching", priority=1)
        else:
            await self._notify_complete(
                f"Watched night complete ({reason}; {stats}). Park "
                f"and cooler check in {delay // 60} min.")

    async def _verify_watched(self, delay_s: int = 300) -> dict:
        """Read-only end-of-night check for a watched night: every rig's
        cooler OFF and the mount parked. Sends no command (no warm retry,
        unlike _verify_shutdown); one priority push when anything is wrong.
        Never raises."""
        from photonscript.shared.rigs import rig_ids, rig_config, nina_camera_info
        await asyncio.sleep(delay_s)
        rigs: dict = {}
        still_on: list[str] = []
        for rig in rig_ids(self.config):
            try:
                info = await nina_camera_info(rig_config(self.config, rig).nina_base_url)
            except Exception:  # noqa: BLE001
                info = None
            if not info:
                rigs[rig] = "unreachable"
                continue
            on = bool(info.get("CoolerOn", False))
            rigs[rig] = {"cooler_on": on, "temp_c": info.get("Temperature"),
                         "dew_on": info.get("DewHeaterOn")}
            if on:
                still_on.append(rig)
        parked = None
        data = await self._nina("mount_info")
        if isinstance(data, dict):
            m = data.get("Response", data)
            if isinstance(m, dict) and m.get("Connected", True):
                parked = bool(m.get("AtPark", False))
        ok = not still_on and parked is not False
        res = {"at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z",
               "ok": ok, "rigs": rigs, "parked": parked}
        if self.shutdown is not None:
            self.shutdown["verify"] = res
            self._persist()
        self._watch_event("verify", "ok" if ok else "problem", parked=parked,
                          cooler_on=still_on)
        if not ok:
            bad = []
            if still_on:
                bad.append("cooler STILL ON on " + ", ".join(still_on))
            if parked is False:
                bad.append("mount NOT parked")
            await notify(self.config,
                         "Watched night check: " + " and ".join(bad) + ". "
                         "Watch mode sends no commands: use Stop & Make Safe "
                         "or check NINA.",
                         title="PhotonScript shutdown warning", priority=1)
        return res

    # -- PS-64: operator Pause / Resume -----------------------------------------
    #
    # 2026-09-26: a PHD2 Calibration Assistant slew left the mount at Dec 0
    # while the sequence kept shooting "Cat's Eye" subs; the only ways to
    # stop were Remote Desktop or Stop & Make Safe (warm + park). Pause stops
    # NINA #1 after its current sub and keeps tracking, cooler and PHD2;
    # Resume re-dispatches the remainder (the PS-77 / PS-93 path), so goals
    # and planning stay right. A watched sideload (PS-136) is never
    # commanded: its pause only mutes the guiding watchdog.

    def _pause_event(self, value: str, detail: str = "",
                     now: datetime | None = None, **extra) -> None:
        """One kind "operator_pause" line in runs/<night>_events.jsonl."""
        try:
            from photonscript.shared.night_events import events_path
            from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
            now = now or datetime.utcnow()
            append_jsonl(events_path(self.config, night_of(self.config, now)),
                         {"t": iso_z(now), "rig": "rc16", "src": "photonscript",
                          "kind": "operator_pause", "value": value,
                          "detail": detail, **extra})
        except Exception as e:  # noqa: BLE001
            logger.warning("pause event (%s) not logged: %s", value, e)

    def _cancel_pause_wait(self) -> bool:
        """Cancel a pending stop-after-exposure wait. True if one was."""
        t = getattr(self, "_pause_task", None)
        self._pause_task = None
        if t is not None and not t.done():
            t.cancel()
            return True
        return False

    def _piggy_enabled(self) -> bool:
        try:
            from photonscript.shared.rigs import PIGGYBACK, rig_ids
            return PIGGYBACK in rig_ids(self.config)
        except Exception:  # noqa: BLE001
            return False

    def _reset_guiding_watchdog(self) -> None:
        self._guiding_alerted = False
        self._not_locked_ticks = 0
        self._not_locked_since = None
        self._guiding_recovered = self._guiding_escalated = False

    async def pause(self, piggy: str = "keep", when: str = "after_exposure",
                    now: datetime | None = None) -> dict:
        """Operator pause. RUNNING: PAUSED_OPERATOR now, NINA #1's sequence
        is stopped in the background after its current sub (when="now":
        at once); piggy="pause" stops the Piggy-600 the same way (default
        "keep": it keeps imaging). WATCHING: alert-only, the guiding
        watchdog is muted and nothing is sent to NINA. Returns {"ok",
        "detail", **status}."""
        now = now or datetime.utcnow()
        piggy = "pause" if str(piggy or "").strip().lower() == "pause" else "keep"
        when = "now" if str(when or "").strip().lower() == "now" else "after_exposure"
        if self.state == WATCH_STATE:
            w = self.watch if isinstance(self.watch, dict) else {}
            self.watch = w
            if w.get("operator_paused"):
                return {**self.status(), "ok": False,
                        "detail": "the watched night is already paused"}
            w["operator_paused"] = {"since": now.replace(microsecond=0).isoformat() + "Z"}
            msg = ("Watching, paused by the operator: guiding alerts muted. "
                   "Nothing was sent to NINA: pause the sideloaded sequence "
                   "in NINA itself.")
            self._set_state(WATCH_STATE, msg)
            self._pause_event("pause", msg, now, mode="watch")
            await notify(self.config, "PAUSED (watching): guiding alerts muted "
                         "by the operator. The sideloaded sequence is NINA's: "
                         "PhotonScript sent no command.",
                         title="PhotonScript paused")
            return {**self.status(), "ok": True, "detail": msg}
        if self.state != "RUNNING":
            return {**self.status(), "ok": False,
                    "detail": f"armer is {self.state}: pause only while "
                              "RUNNING (or WATCHING)"}
        if piggy == "pause" and not self._piggy_enabled():
            piggy = "keep"
        self.pause_info = {"since": now.replace(microsecond=0).isoformat() + "Z",
                      "phase": "stopping", "when": when, "piggy": piggy,
                      "rigs": {}, "parked": False}
        what = ("now" if when == "now" else "after the current sub")
        msg = (f"Pausing: NINA #1 stops {what}"
               + ("; Piggy-600 too" if piggy == "pause" else
                  "; Piggy-600 keeps imaging")
               + ". Cooler, tracking and guiding stay on.")
        self._set_state(PAUSE_STATE, msg)
        self._pause_event("request", msg, now, when=when, piggy=piggy)
        self._pause_task = asyncio.create_task(self._finish_pause())
        return {**self.status(), "ok": True, "detail": msg}

    async def _finish_pause(self) -> None:
        """The background half of pause(): wait for each rig's sub to end,
        stop its sequence, report. A failed RC16 stop goes back to RUNNING
        (NINA may still be imaging) with a priority push. Never raises
        (except CancelledError from resume)."""
        from photonscript.scheduler import night_pause as np_
        p = self.pause_info if isinstance(self.pause_info, dict) else {}
        when = p.get("when", "after_exposure")

        async def rc16_cam():
            data = await self._nina("camera_info")
            pl = data.get("Response", data) if isinstance(data, dict) else None
            return pl if isinstance(pl, dict) else None

        async def rc16_stop():
            return await self._nina("sequence_stop") is not None

        jobs = {"rc16": np_.stop_after_exposure(rc16_cam, rc16_stop, when=when)}
        if p.get("piggy") == "pause":
            try:
                from photonscript.shared.rigs import PIGGYBACK, rig_config
                base = rig_config(self.config, PIGGYBACK).nina_base_url
                jobs["piggyback"] = np_.stop_after_exposure(
                    lambda: np_.camera_info(base), lambda: np_.sequence_stop(base),
                    when=when)
            except Exception as e:  # noqa: BLE001
                logger.warning("pause: Piggy-600 not stopped: %s", e)
        results = await asyncio.gather(*jobs.values(), return_exceptions=True)
        rigs = {}
        for rig, res in zip(jobs, results):
            rigs[rig] = (res if isinstance(res, dict)
                         else {"ok": False, "how": f"error: {res}", "waited_s": 0})
        now = datetime.utcnow()
        p["rigs"] = rigs
        rc = rigs.get("rc16") or {}
        if self.state != PAUSE_STATE:
            return   # disarmed / dawn while waiting
        if not rc.get("ok"):
            self.pause_info = None
            what = "Restart" if p.get("restart") else "Pause"
            msg = (f"{what} FAILED: NINA #1 sequence stop failed ({self.detail}); "
                   "still RUNNING, NINA may still be imaging")
            self._set_state("RUNNING", msg)
            self._pause_event("failed", msg, now, rigs=rigs)
            if p.get("restart"):
                self._restart_event("failed", msg, now, step="stop")
                self.last_restart = {"at": _iso(now), "ok": False, "detail": msg}
            await notify(self.config, msg + ". Use Stop & Make Safe or NINA.",
                         title=f"PhotonScript {what.lower()} FAILED", priority=1)
            return
        p["phase"] = "paused"
        p["stopped_at"] = now.replace(microsecond=0).isoformat() + "Z"
        if p.get("restart"):
            # PS-143: a Restart tonight from now: re-plan and re-dispatch
            self.pause_info = p
            self._restart_event("stopped", f"NINA #1 {np_.describe(rc)}", now,
                                rigs=rigs)
            await self._restart_dispatch(now, p)
            return
        pb = rigs.get("piggyback")
        pb_txt = ("Piggy-600 keeps imaging" if pb is None
                  else "Piggy-600 " + np_.describe(pb))
        msg = (f"Paused by the operator: RC16 {np_.describe(rc)}; {pb_txt}. "
               "Cooler, tracking and guiding kept; Resume re-dispatches the "
               "remainder.")
        self.pause_info = p
        self._set_state(PAUSE_STATE, msg)
        self._pause_event("stopped", msg, now, rigs=rigs)
        left = (self._dawn() - now).total_seconds() / 3600
        await notify(self.config, f"PAUSED by the operator: RC16 "
                     f"{np_.describe(rc)}; {pb_txt}. Cooler, tracking and PHD2 "
                     f"stay on, nothing parked. {max(0.0, left):.1f} h of dark "
                     "left; Resume on the dashboard re-dispatches the remainder.",
                     title="PhotonScript paused",
                     priority=(1 if pb is not None and not pb.get("ok") else 0))

    async def resume(self, now: datetime | None = None) -> dict:
        """Undo pause(). Before the stop happened: cancel the wait, RUNNING
        again (nothing was stopped). After it: the mid-night re-dispatch of
        the remainder (as after a PS-77 safety stop), plus the Piggy-600
        companion when this pause stopped it. Refused with under
        RESUME_MIN_REMAINING_MIN of dark left (the dawn shutdown still runs
        from PAUSED_OPERATOR). Returns {"ok", "detail", **status}."""
        now = now or datetime.utcnow()
        if self.state == WATCH_STATE:
            w = self.watch if isinstance(self.watch, dict) else {}
            if not w.get("operator_paused"):
                return {**self.status(), "ok": False,
                        "detail": "the watched night is not paused"}
            w.pop("operator_paused", None)
            self._reset_guiding_watchdog()
            msg = "Watching: resumed by the operator, guiding alerts back on"
            self._set_state(WATCH_STATE, msg)
            self._pause_event("resume", msg, now, mode="watch")
            await notify(self.config, "RESUMED (watching): guiding alerts back "
                         "on.", title="PhotonScript resumed")
            return {**self.status(), "ok": True, "detail": msg}
        if self.state != PAUSE_STATE:
            return {**self.status(), "ok": False,
                    "detail": f"armer is {self.state}, not paused"}
        p = self.pause_info if isinstance(self.pause_info, dict) else {}
        if p.get("phase") == "stopping":
            self._cancel_pause_wait()
            self.pause_info = None
            msg = "Resumed before the stop: the sequence kept running"
            self._set_state("RUNNING", msg)
            self._pause_event("resume", msg, now, redispatched=False)
            await notify(self.config, "RESUMED: the pause was cancelled before "
                         "NINA was stopped; nothing changed.",
                         title="PhotonScript resumed")
            return {**self.status(), "ok": True, "detail": msg,
                    "redispatched": False}
        left_min = (self._dawn() - now).total_seconds() / 60
        if left_min < RESUME_MIN_REMAINING_MIN:
            return {**self.status(), "ok": False,
                    "detail": f"only {max(0, int(left_min))} min of dark left: "
                              "not re-dispatching (the dawn shutdown will run)"}
        self._reset_guiding_watchdog()
        ok = await self._dispatch_and_start(companion=False, fail_state=None)
        if not ok:
            msg = f"Resume FAILED: re-dispatch failed ({self.detail}); still paused"
            self._set_state(PAUSE_STATE, msg)
            self._pause_event("resume_failed", msg, now)
            return {**self.status(), "ok": False, "detail": msg}
        pb = (p.get("rigs") or {}).get("piggyback")
        pb_txt = "Piggy-600 untouched"
        if p.get("piggy") == "pause" and isinstance(pb, dict) and pb.get("ok"):
            await self._dispatch_piggyback_companion()
            pb_txt = "Piggy-600 companion re-dispatched"
        self.pause_info = None
        self._unsafe_since = self._safe_since = None
        msg = f"Resumed by the operator: remainder re-dispatched; {pb_txt}"
        self._set_state("RUNNING", msg)
        self._pause_event("resume", msg, now, redispatched=True, piggy=pb_txt)
        await notify(self.config, f"RESUMED by the operator: the remainder is "
                     f"re-dispatched ({left_min / 60:.1f} h of dark left); "
                     f"{pb_txt}.", title="PhotonScript resumed")
        return {**self.status(), "ok": True, "detail": msg, "redispatched": True}

    async def _paused_tick(self, now: datetime) -> None:
        """PAUSED_OPERATOR tick. Dawn: the normal dawn shutdown (as from
        PAUSED_UNSAFE). Safety is recorded and watched; unsafe for
        unsafe_stop_grace_s once stopped = park (PS-77's rule: nothing else
        would park before the roof closes), one push. Safe: the cooler nanny
        keeps the setpoint. The guiding watchdog does not run."""
        if now >= self._dawn() + timedelta(minutes=30):
            self._cancel_pause_wait()
            self._set_state("COMPLETE", "Dawn while paused by the operator: "
                                        "running dawn shutdown")
            report = await self.dawn_shutdown(reason="dawn while operator-paused")
            self.pause_info = None
            self._set_state("COMPLETE", f"Dawn shutdown: {report}")
            await self._notify_complete(
                f"Night ended while paused by the operator: dawn shutdown ran: "
                f"{report}")
            return
        safe = await self._is_safe()
        self._record_safety(safe, now)
        await self._watch_safety_monitor(now, safe)
        p = self.pause_info if isinstance(self.pause_info, dict) else {}
        if safe is False:
            if self._unsafe_since is None:
                self._unsafe_since = now
            grace = int(getattr(self.config, "unsafe_stop_grace_s", 120))
            if (p.get("phase") == "paused" and not p.get("parked")
                    and (now - self._unsafe_since).total_seconds() >= grace):
                ok = await self._nina("mount_park") is not None
                p["parked"] = bool(ok)
                self.pause_info = p
                msg = (f"Paused and unsafe: mount park {'ok' if ok else 'FAILED'} "
                       "(cooler kept on; Resume re-dispatches)")
                self._set_state(PAUSE_STATE, msg)
                self._pause_event("park_unsafe", msg, now, ok=ok)
                await notify(self.config, f"Unsafe while paused: {msg}.",
                             title="PhotonScript paused", priority=1)
            return
        self._unsafe_since = None
        if safe is True and p.get("phase") == "paused":
            await self._reconcile_cooler(now)

    # -- PS-143: Restart tonight from now ---------------------------------------
    # Goals changed mid-night (a target added, a priority or filter mix
    # edited, a mosaic created): re-plan the remainder of tonight from the
    # current goals and re-dispatch it through the PS-64 resume path
    # (_dispatch_and_start(companion=False)). From RUNNING it first stops NINA
    # #1 after the current sub exactly as Pause does (PAUSED_OPERATOR with a
    # "restart" mark, so a PhotonScript restart mid-wait still finishes it).
    # Never warms, parks or turns a cooler off: tracking, cooler and PHD2
    # keep running across the stop; the new sequence's start area connects
    # and cools (already cool) and its targets slew, center, AF and start
    # guiding as after any resume. The Piggy-600 keeps imaging (a pause that
    # had stopped it gets its companion back, as Resume does).

    def _restart_event(self, value: str, detail: str = "",
                       now: datetime | None = None, **extra) -> None:
        """One kind "restart" line in runs/<night>_events.jsonl."""
        try:
            from photonscript.shared.night_events import events_path
            from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
            now = now or datetime.utcnow()
            append_jsonl(events_path(self.config, night_of(self.config, now)),
                         {"t": iso_z(now), "rig": "rc16", "src": "photonscript",
                          "kind": "restart", "value": value, "detail": detail,
                          **extra})
        except Exception as e:  # noqa: BLE001
            logger.warning("restart event (%s) not logged: %s", value, e)

    def _restart_status(self) -> dict | None:
        """The pending restart (waiting for the sub to end), else the last
        restart result, for the dashboard."""
        p = getattr(self, "pause_info", None)
        if self.state == PAUSE_STATE and isinstance(p, dict) and p.get("restart"):
            return {**p["restart"], "pending": True}
        return getattr(self, "last_restart", None)

    @staticmethod
    def _calibration_busy() -> str | None:
        """The rig a calibration capture job runs on (PS-113), else None."""
        try:
            from photonscript.scheduler.calibration_capture import busy
            for rig in ("rc16", "piggyback"):
                if busy(rig):
                    return rig
        except Exception:  # noqa: BLE001
            return None
        return None

    async def restart(self, when: str = "after_exposure",
                      now: datetime | None = None) -> dict:
        """Restart tonight from now. RUNNING: stop NINA #1 after the current
        sub (when="now": at once), then re-plan and re-dispatch.
        PAUSED_OPERATOR: re-plan and re-dispatch now (or as soon as the
        pause's stop is done). Refused while WATCHING (the sideload is
        NINA's), while a calibration capture job runs, in any other state,
        and with under RESUME_MIN_REMAINING_MIN of dark left. Returns
        {"ok", "detail", **status}."""
        now = now or datetime.utcnow()
        when = "now" if str(when or "").strip().lower() == "now" else "after_exposure"

        def refuse(detail: str) -> dict:
            self._restart_event("refused", detail, now, state=self.state)
            return {**self.status(), "ok": False, "detail": detail}

        if self.state == WATCH_STATE:
            return refuse(
                "a sideloaded night is NINA's: PhotonScript never re-dispatches "
                "it. Build the rest of tonight with the sideload preview "
                "(GET /api/sequence/sideload/preview) and sideload that instead.")
        cal = self._calibration_busy()
        if cal:
            return refuse(f"a calibration capture job is running on the {cal}: "
                          "restart after it finishes (or cancel it)")
        if self.state not in ("RUNNING", PAUSE_STATE):
            hint = {"ARMED": " (not dispatched yet: pre-config plans from the "
                             "current goals anyway)",
                    "PAUSED_UNSAFE": " (unsafe: the armer resumes or "
                                     "re-dispatches by itself once safe)"}
            return refuse(f"armer is {self.state}: Restart tonight from now "
                          "needs a running or operator-paused night"
                          + hint.get(self.state, ""))
        left_min = (self._dawn() - now).total_seconds() / 60
        if left_min < RESUME_MIN_REMAINING_MIN:
            return refuse(f"only {max(0, int(left_min))} min of dark left: not "
                          "re-dispatching (the dawn shutdown will run)")
        req = {"requested": _iso(now), "when": when, "from": self.state}
        if self.state == "RUNNING":
            self.pause_info = {"since": _iso(now), "phase": "stopping",
                               "when": when, "piggy": "keep", "rigs": {},
                               "parked": False, "restart": req}
            what = "now" if when == "now" else "after the current sub"
            msg = (f"Restarting tonight from now: NINA #1 stops {what}, then "
                   "the rest of tonight is re-planned from the current goals "
                   "and re-dispatched. Cooler, tracking and guiding stay on; "
                   "nothing parks or warms; the Piggy-600 keeps imaging.")
            self._set_state(PAUSE_STATE, msg)
            self._restart_event("request", msg, now, when=when, from_state="RUNNING")
            self._pause_task = asyncio.create_task(self._finish_pause())
            return {**self.status(), "ok": True, "detail": msg, "pending": True}
        p = self.pause_info if isinstance(self.pause_info, dict) else {}
        p["restart"] = req
        self.pause_info = p
        if p.get("phase") == "stopping":
            msg = ("Restart requested: the pause's stop is still waiting for "
                   "the sub to end; the rest of tonight is re-planned and "
                   "re-dispatched as soon as NINA #1 has stopped.")
            self._set_state(PAUSE_STATE, msg)
            self._restart_event("request", msg, now, when=when,
                                from_state=PAUSE_STATE, phase="stopping")
            return {**self.status(), "ok": True, "detail": msg, "pending": True}
        self._restart_event("request", "from an operator pause (NINA #1 "
                            "already stopped)", now, from_state=PAUSE_STATE)
        res = await self._restart_dispatch(now, p)
        return {**self.status(), **res}

    async def _restart_dispatch(self, now: datetime, p: dict) -> dict:
        """NINA #1 is stopped: re-plan and re-dispatch the remainder. A
        failure stays PAUSED_OPERATOR (stopped, nothing parked): Resume or
        Restart again. Returns {"ok", "detail", "redispatched"}."""
        p.pop("restart", None)
        p["phase"] = "paused"
        left_min = (self._dawn() - now).total_seconds() / 60
        if left_min < RESUME_MIN_REMAINING_MIN:
            self.pause_info = p
            msg = (f"Restart not dispatched: only {max(0, int(left_min))} min of "
                   "dark left; staying paused (the dawn shutdown will run)")
            self._set_state(PAUSE_STATE, msg)
            self._restart_event("failed", msg, now, step="dark")
            self.last_restart = {"at": _iso(now), "ok": False, "detail": msg}
            await notify(self.config, msg + ".", title="PhotonScript restart")
            return {"ok": False, "detail": msg, "redispatched": False}
        self._reset_guiding_watchdog()
        ok = await self._dispatch_and_start(companion=False, fail_state=None)
        if not ok:
            # _dispatch_and_start already pushed the failure (priority)
            self.pause_info = p
            msg = (f"Restart FAILED: re-dispatch failed ({self.detail}); paused "
                   "with NINA #1 stopped (nothing parked or warmed). Resume or "
                   "Restart tonight from now again.")
            self._set_state(PAUSE_STATE, msg)
            self._restart_event("failed", msg, now, step="dispatch")
            self.last_restart = {"at": _iso(now), "ok": False, "detail": msg}
            return {"ok": False, "detail": msg, "redispatched": False}
        pb = (p.get("rigs") or {}).get("piggyback")
        pb_txt = "Piggy-600 untouched"
        if p.get("piggy") == "pause" and isinstance(pb, dict) and pb.get("ok"):
            await self._dispatch_piggyback_companion()
            pb_txt = "Piggy-600 companion re-dispatched"
        self.pause_info = None
        self._unsafe_since = self._safe_since = None
        names = list(getattr(self, "last_dispatch_targets", None) or [])
        tg = ", ".join(names[:6]) + (f" (+{len(names) - 6} more)" if len(names) > 6 else "")
        msg = (f"Restarted tonight from now: re-planned from the current goals "
               f"and re-dispatched ({tg or 'plan'}); {pb_txt}")
        self._set_state("RUNNING", msg)
        self._restart_event("dispatched", msg, now, targets=names, piggy=pb_txt)
        self.last_restart = {"at": _iso(now), "ok": True, "detail": msg,
                             "targets": names}
        await notify(self.config, f"RESTARTED tonight from now: the remainder is "
                     f"re-planned from the current goals ({tg or 'plan'}) and "
                     f"re-dispatched, {left_min / 60:.1f} h of dark left. Cooler "
                     f"and tracking kept; nothing parked or warmed; {pb_txt}.",
                     title="PhotonScript restarted")
        return {"ok": True, "detail": msg, "redispatched": True}


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

    def _planned_filters(self) -> list[str]:
        """PS-156: the filters in tonight's plan snapshot (runs/<night>_plan.json),
        else every filter."""
        try:
            from photonscript.scheduler.runs import runs_dir
            p = runs_dir(self.config) / f"{self.plan.get('night_of')}_plan.json"
            snap = json.loads(p.read_text(encoding="utf-8"))
            out = []
            for t in snap.get("targets") or []:
                for e in t.get("exposures") or []:
                    if e.get("filter") and e["filter"] not in out:
                        out.append(e["filter"])
            if out:
                return out
        except Exception:  # noqa: BLE001 - no snapshot yet
            pass
        return ["L", "R", "G", "B", "Ha", "OIII", "SII"]

    async def guide_fallback(self, reason: str, source: str = "") -> dict | None:
        """PS-156: PHD2 failed on a guided night (guard D7 / D8 or the
        watchdog). Once per night: record a run event (kind guide_fallback)
        and, by guide_fallback_mode, push what auto would do (alert) or run
        fallback_unguided (auto: the remainder unguided, subs capped per
        filter at the tracking-test length; it pushes itself). Stays
        unguided until dawn. Returns the record, None when skipped."""
        from photonscript.scheduler import guide_fallback as gf
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl, iso_z
        mode = gf.mode(self.config)
        night = self.plan.get("night_of")
        if mode == "off" or not night or self.state not in ("RUNNING", WATCH_STATE):
            return None
        if (self.guide_fallback_rec or {}).get("night") == night:
            return None                       # decided already tonight
        if not self._use_guiding() and self.state != WATCH_STATE:
            return None                       # nothing to fall back from
        lens = gf.lengths(self.config, self._planned_filters())
        plan = gf.plan_text(lens)
        why_not = None
        if mode == "alert":
            why_not = "guide_fallback_mode=alert: nothing switched"
        elif self.state == WATCH_STATE:
            why_not = "watching a sideloaded night: the armer cannot re-dispatch it"
        rec = {"night": night, "t": iso_z(datetime.utcnow()), "reason": reason,
               "source": source, "mode": mode, "acted": why_not is None,
               "lengths": {f: s for f, (s, _) in lens.items()}, "note": why_not}
        self.guide_fallback_rec = rec
        self._persist()
        if why_not is None:
            ok = await self.fallback_unguided(f"{reason}; subs capped: {plan}")
            rec["ok"] = ok
            if not ok:
                rec["note"] = f"fallback_unguided declined or failed ({self.detail})"
            self._persist()
        try:
            append_jsonl(events_path(self.config, night), {
                "t": rec["t"], "rig": "rc16", "src": "photonscript",
                "kind": "guide_fallback",
                "value": "unguided" if rec["acted"] else mode,
                "detail": reason, "note": rec.get("note"), "lengths": rec["lengths"]})
        except Exception as e:  # noqa: BLE001
            logger.warning("PS-156 run event not written: %s", e)
        if why_not is not None:
            await notify(self.config,
                         f"PHD2 failed tonight ({reason}). The unguided fallback "
                         f"would run the rest of the night unguided at {plan}; "
                         f"{why_not}. Set guide_fallback_mode=auto (System page) "
                         "to let it, or re-arm unguided by hand.",
                         title="PhotonScript unguided fallback", priority=0)
        logger.warning("PS-156 guide fallback (%s, %s): %s", mode, source, rec)
        return rec

    # -- PS-85: per-block guiding ------------------------------------------------

    def _live_block_decisions(self) -> dict:
        bd = getattr(self, "block_decisions", None) or {}
        if bd.get("night") != self.plan.get("night_of"):
            return {}
        out = {}
        for k, v in (bd.get("blocks") or {}).items():
            tgt, _, flt = k.rpartition("|")
            out[(tgt, flt)] = v
        return out

    def _apply_block_decisions(self, targets) -> None:
        """PS-85, in _dispatch: mode auto applies tonight's live decisions
        and history (blocks, or a whole target, become unguided at the
        proven sub length); mode observe only records what history would
        have done (once per block per night). Never fails a dispatch."""
        from photonscript.scheduler import guide_blocks as gb
        mode = gb.block_mode(self.config)
        if mode == "off":
            return
        night = self.plan.get("night_of") or ""
        try:
            live = self._live_block_decisions() if mode == "auto" else {}
            dec = gb.dispatch_decisions(self.config, night, targets, live=live)
            if not dec:
                return
            seen = gb.tonight_decisions(self.config, night)
            for (tgt, flt), d in dec.items():
                if d.get("source") != "history" or (tgt, flt) in seen:
                    continue
                s_, src = gb.fallback_exposure_s(self.config, flt)
                rec = gb.append(self.config, night, {
                    "event": "decision", "target": tgt, "filter": flt,
                    "decision": "unguided", "source": "history", "mode": mode,
                    "acted": mode == "auto", "reason": d.get("reason"),
                    "exposure_s": s_, "exposure_source": src})
                verb = ("planned unguided" if mode == "auto"
                        else "would be planned unguided (observe: still guided)")
                if getattr(self, "_block_alerts", None) is None:
                    self._block_alerts = []
                self._block_alerts.append((tgt, (
                    f"{tgt} {flt} {verb} at {s_:g} s: {rec['reason']}."
                    if s_ else f"{tgt} {flt} {verb}: {rec['reason']}.")))
            if mode == "auto":
                gb.apply_to_targets(self.config, targets, dec)
        except Exception as e:  # noqa: BLE001
            logger.warning("PS-85 per-block guiding skipped: %s", e)

    async def _send_block_alerts(self) -> None:
        from photonscript.scheduler import guide_blocks as gb
        pending = list(getattr(self, "_block_alerts", None) or [])
        self._block_alerts = []
        night = self.plan.get("night_of") or ""
        for tgt, text in pending:
            try:
                await gb.alert_once(self.config, night, tgt, text)
            except Exception as e:  # noqa: BLE001
                logger.warning("PS-85 block alert failed: %s", e)

    async def block_unguided(self, target: str, filt: str, reason: str,
                             source: str = "live", evidence: dict | None = None) -> bool:
        """PS-85: no real guide star for this target's `filt` block. Records
        the decision and pushes once per target per night. Mode auto, a
        RUNNING guided night and under guide_block_max_redispatch: stop the
        sequence and the guider and re-dispatch the remainder with this block
        unguided at the proven sub length (companion untouched). Returns True
        only when it re-dispatched."""
        from photonscript.scheduler import guide_blocks as gb
        mode = gb.block_mode(self.config)
        night = self.plan.get("night_of") or ""
        if mode == "off" or not target or not filt:
            return False
        key = f"{target}|{filt}"
        bd = getattr(self, "block_decisions", None) or {}
        if bd.get("night") != night:
            bd = {"night": night, "blocks": {}, "redispatches": 0}
        if key in bd["blocks"]:
            return False                      # already unguided tonight
        planned = gb.planned_blocks(self.config, night)
        if planned is not None and (target, filt) not in planned:
            # e.g. the AF filter (L) between narrowband blocks: not a block
            logger.info("PS-85: %s %s is not a guided block tonight (%s)",
                        target, filt, reason)
            return False
        exp_s, src = gb.fallback_exposure_s(self.config, filt)
        rec = {"event": "decision", "target": target, "filter": filt,
               "decision": "unguided", "source": source, "mode": mode,
               "acted": False, "reason": reason, "exposure_s": exp_s,
               "exposure_source": src, "evidence": evidence or {}}
        length = f" at {exp_s:g} s ({src})" if exp_s else ""
        why_not = None
        if mode != "auto":
            why_not = "guide_block_mode=observe: nothing switched"
        elif self.state != "RUNNING":
            why_not = f"armer {self.state}, not RUNNING"
        elif not self._use_guiding():
            why_not = "night is unguided"
        elif bd.get("redispatches", 0) >= int(
                getattr(self.config, "guide_block_max_redispatch", 3) or 0):
            why_not = "re-dispatch limit reached tonight"
        if why_not:
            rec["note"] = why_not
            gb.append(self.config, night, rec)
            await gb.alert_once(self.config, night, target,
                                f"{target} {filt}: no real guide star ({reason}). "
                                f"Would run it unguided{length}; {why_not}.")
            return False
        bd["blocks"][key] = {"source": source, "reason": reason,
                             "t_utc": datetime.utcnow().isoformat() + "Z"}
        bd["redispatches"] = int(bd.get("redispatches", 0)) + 1
        self.block_decisions = bd
        self._persist()
        steps = []
        for label, nkey in (("stop", "sequence_stop"), ("guider stop", "guider_stop")):
            ok = await self._nina(nkey) is not None
            steps.append(f"{label} {'ok' if ok else 'FAILED'}")
        ok = await self._dispatch_and_start(companion=False, fail_state=None)
        steps.append(f"re-dispatch {'ok' if ok else 'FAILED'}")
        report = "; ".join(steps)
        rec.update(acted=True, note=report)
        gb.append(self.config, night, rec)
        self._set_state("RUNNING", f"{target} {filt} unguided ({reason}): {report}")
        logger.warning("PS-85 block %s %s unguided (%s): %s", target, filt, reason, report)
        await gb.alert_once(self.config, night, target,
                            f"{target} {filt}: no real guide star ({reason}). The "
                            f"rest is re-dispatched with {filt} unguided{length}, "
                            f"TPoint + ProTrack ({report}).")
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
            while self.state in LIVE_STATES:
                await self._tick()
                await asyncio.sleep(TICK_SECONDS)
        except asyncio.CancelledError:
            pass

    async def _tick(self):
        now = datetime.utcnow()

        if self.state == WATCH_STATE:
            await self._watch_tick(now)   # PS-136: observe, never dispatch
            return

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
                await self._crosscheck_unsafe(now)   # PS-1, observe only
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

        elif self.state == PAUSE_STATE:
            await self._paused_tick(now)   # PS-64

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


# -- PS-136 helpers -------------------------------------------------------------

def _nina_item_status(state, norm_name: str) -> str | None:
    """PS-152: the Status (upper case) of the first item in a ninaAPI
    sequence tree whose _norm_item name is norm_name, else None."""
    from photonscript.scheduler.sideload import _state_payload
    found: list[str] = []

    def walk(n):
        if found:
            return
        if isinstance(n, list):
            for x in n:
                walk(x)
            return
        if not isinstance(n, dict):
            return
        if _norm_item(n.get("Name")) == norm_name:
            found.append(str(n.get("Status", "")).upper())
            return
        for key in ("Items", "Conditions", "Triggers"):
            v = n.get(key)
            walk(v.get("$values") if isinstance(v, dict) else v)

    walk(_state_payload(state))
    return found[0] if found else None


def _norm_item(name) -> str:
    """A NINA item name for matching: ninaAPI's '_Container' suffix off,
    whitespace collapsed, lower case."""
    s = str(name or "")
    if s.endswith("_Container"):
        s = s[: -len("_Container")]
    return " ".join(s.split()).lower()


def guided_targets(seq) -> list[str]:
    """Names of the DeepSkyObjectContainers in a NINA sequence that carry a
    StartGuiding instruction somewhere inside (document order)."""
    out: list[str] = []

    def has_guiding(n) -> bool:
        if isinstance(n, dict):
            if "StartGuiding" in str(n.get("$type") or ""):
                return True
            return any(has_guiding(v) for v in n.values())
        if isinstance(n, list):
            return any(has_guiding(x) for x in n)
        return False

    def walk(n):
        if isinstance(n, dict):
            if "DeepSkyObjectContainer" in str(n.get("$type") or ""):
                if has_guiding(n) and str(n.get("Name")) not in out:
                    out.append(str(n.get("Name")))
                return
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for x in n:
                walk(x)

    walk(seq)
    return out


def guided_targets_from_file(path) -> list[str] | None:
    """guided_targets() of a saved sequence file (the PS-123 sideload copy);
    None when there is no file or it cannot be read."""
    if not path:
        return None
    try:
        return guided_targets(json.loads(Path(path).read_text(encoding="utf-8")))
    except Exception as e:  # noqa: BLE001
        logger.warning("watch: sequence file %s not read: %s", path, e)
        return None


def watch_plan(config, now: datetime | None = None) -> dict:
    """The watched night's times only (no targets): the noon-to-noon night
    `now` belongs to, so a watch started after midnight still gets the dawn
    ahead. Same keys the dawn-shutdown timing reads (PS-36)."""
    from photonscript.shared.astronomy import get_twilight_times
    from photonscript.shared.phd2_store import night_of
    now = now or datetime.utcnow()
    night = night_of(config, now)
    base = datetime.strptime(night, "%Y-%m-%d")
    obs = config.get_observatory()
    tw = get_twilight_times(obs, base)
    dusk, dawn = tw.get("astro_dark_start"), tw.get("astro_dark_end")
    if not dusk or not dawn:
        return {"error": "Could not compute darkness window"}
    naut = rise = None
    try:
        from photonscript.scheduler.night_plan import compute_night_times
        tw_all = compute_night_times(obs, base)
        naut, rise = tw_all.get("naut_dawn"), tw_all.get("sunrise")
    except Exception as e:  # noqa: BLE001
        logger.warning("watch plan: twilight lookup failed: %s", e)
    z = lambda dt: dt.isoformat() + "Z" if dt else None  # noqa: E731
    return {"night_of": night, "preconfig_utc": None, "dusk_utc": z(dusk),
            "dawn_utc": z(dawn), "naut_dawn_utc": z(naut), "sunrise_utc": z(rise),
            "dark_hours": round((dawn - dusk).total_seconds() / 3600, 1),
            "targets": [], "watch": True}


async def run_watch_detector(get_config, get_armer,
                             tick_seconds: int = WATCH_DETECT_SECONDS) -> None:
    """PS-136 background loop: while the armer is idle, adopt tonight's RC16
    sideload once NINA #1 runs it (Armer.maybe_adopt; config
    watch_sideload_auto). Read-only until it adopts. Never raises."""
    while True:
        try:
            if getattr(get_config(), "watch_sideload_auto", True):
                await get_armer().maybe_adopt()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("watch detector tick failed: %s", e)
        await asyncio.sleep(tick_seconds)


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


async def request_block_unguided(config, target: str, filt: str, reason: str,
                                 source: str = "live",
                                 evidence: dict | None = None) -> bool:
    """PS-85: ask the scheduler's armer (same process in `start --mode
    full`) to run this target's `filt` block unguided (Armer.block_unguided).
    Without an armer here the decision is still recorded and pushed once per
    target per night, and False is returned."""
    import sys
    app_mod = sys.modules.get("photonscript.scheduler.app")
    armer = getattr(app_mod, "_armer", None) if app_mod else None
    if armer is not None:
        try:
            return await armer.block_unguided(target, filt, reason, source=source,
                                              evidence=evidence)
        except Exception as e:  # noqa: BLE001
            logger.warning("PS-85 block decision failed: %s", e)
            return False
    from photonscript.scheduler import guide_blocks as gb
    from photonscript.shared import phd2_store
    if gb.block_mode(config) == "off":
        return False
    night = phd2_store.night_of(config)
    s_, src = gb.fallback_exposure_s(config, filt)
    gb.append(config, night, {"event": "decision", "target": target, "filter": filt,
                              "decision": "unguided", "source": source,
                              "mode": gb.block_mode(config), "acted": False,
                              "reason": reason, "exposure_s": s_,
                              "exposure_source": src, "evidence": evidence or {},
                              "note": "no armer in this process"})
    await gb.alert_once(config, night, target,
                        f"{target} {filt}: no real guide star ({reason}). Would run "
                        f"it unguided; no armer in this process.")
    return False


async def request_guide_fallback(config, reason: str, source: str = "") -> dict | None:
    """PS-156: hand a PHD2 failure (guard D7 / D8) to the scheduler's armer
    (same process in `start --mode full`). None when no armer runs here or
    it skipped (mode off, not a guided night, decided already tonight)."""
    import sys
    app_mod = sys.modules.get("photonscript.scheduler.app")
    armer = getattr(app_mod, "_armer", None) if app_mod else None
    if armer is None:
        logger.warning("guide fallback requested (%s) but no armer in this "
                       "process", reason)
        return None
    try:
        return await armer.guide_fallback(reason, source=source)
    except Exception as e:  # noqa: BLE001
        logger.warning("guide fallback failed: %s", e)
        return None


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
