"""Telescope Agent — runs on the Windows telescope PC.

Monitors NINA and PHD2, validates captured images, and reports status
back to the scheduler via the message bus. Includes the nanny escalation
ladder: Pushover warn -> (optional) abort-to-safe. NINA's own Safety
Monitor remains the hard weather backstop.
"""

from __future__ import annotations

import asyncio
import logging
import os
import platform
from datetime import datetime, timedelta
from pathlib import Path, PureWindowsPath
from typing import Optional
from uuid import uuid4

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (
    AgentMessage, AgentRole, CapturedImage, FilterType, GuidingMetrics,
    ImageStatus, SessionState, TelescopeState,
)
from photonscript.shared.messagebus import get_message_bus
from photonscript.shared.pushover import notify
from photonscript.telescope_agent.nina_client import NinaClient
from photonscript.telescope_agent.phd2_client import PHD2Client, guide_rms_text
from photonscript.telescope_agent.image_validator import validate_image

logger = logging.getLogger(__name__)

# On Windows, use watchdog for file system monitoring
IS_WINDOWS = platform.system() == "Windows"


class TelescopeAgent:
    """Main telescope monitoring agent.

    Responsibilities:
    - Connect to NINA and PHD2 on the local Windows machine
    - Watch the image output directory for new captures
    - Validate each captured image for quality (FWHM, tracking, eccentricity)
    - Escalate systemic problems (consecutive rejects, cooling, collimation)
    - Report state and image events to the scheduler via message bus
    """

    def __init__(self, config: PhotonScriptConfig, rig: str = "rc16"):
        self.config = config
        self.rig = rig  # "rc16" (main) or "piggyback" (2nd NINA, OSC)
        self.nina = NinaClient(config.nina_base_url)
        self.phd2 = PHD2Client(config.phd2_host, config.phd2_port, config=config)
        self._rms_logged_at: float | None = None  # PS-70 RMS log rate limit
        self.bus = get_message_bus()
        self.state = TelescopeState()
        self._running = False
        self._watch_dir = Path(config.image_watch_dir)
        # Nanny / escalation state
        self._consecutive_rejects = 0
        self._alerted: set[str] = set()  # de-duped alert keys
        # Cooling watchdog state
        self._cool_bad_since: float | None = None
        self._cool_fix_attempts = 0
        # Dew-heater watchdog state
        self._dew_last_set: float = 0.0
        self._dew_api_broken = False
        # Safety-monitor watchdog state
        self._safety_bad_since: float | None = None
        self._safety_bad_reads = 0  # consecutive Connected:False polls (debounce)
        self._safety_fix_attempts = 0
        self._safety_last_attempt: float = 0.0
        self._safety_last_escalate: float = 0.0
        self._safety_aborted = False
        self._safety_device_id = ""   # chooser Id last seen connected (learned)
        self._safety_idle = False     # watchdog parked (daytime, not armed)

    async def start(self):
        """Start the telescope agent and begin monitoring."""
        self._running = True
        logger.info("Telescope Agent starting on %s", platform.node())
        logger.info("Image watch directory: %s", self._watch_dir)

        # Register PHD2 update callback
        self.phd2.on_update(self._on_guiding_update)

        # Launch monitoring tasks
        tasks = [
            asyncio.create_task(self._nina_poll_loop()),
            asyncio.create_task(self._safety_loop()),
            asyncio.create_task(self._phd2_monitor()),
            asyncio.create_task(self._file_watch_loop()),
            asyncio.create_task(self._state_broadcast_loop()),
            asyncio.create_task(self._heartbeat_loop()),
        ]

        # Listen for commands from scheduler
        self.bus.subscribe("command", self._on_command)

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("Telescope Agent shutting down")
        finally:
            await self.nina.close()
            await self.phd2.disconnect()

    async def stop(self):
        self._running = False

    # ------------------------------------------------------------------
    # Nanny escalation
    # ------------------------------------------------------------------

    async def _escalate(self, key: str, message: str, severe: bool = False):
        """Escalation ladder: Pushover warn -> (optional) abort-to-safe.

        Alerts are de-duplicated by key. NINA's own Safety Monitor remains
        the hard weather backstop — this layer is quality control on top.
        """
        if key in self._alerted:
            return
        self._alerted.add(key)
        logger.warning("NANNY ALERT [%s] %s", key, message)
        await notify(self.config, message, title="PhotonScript NANNY",
                     priority=1 if severe else 0)
        if severe and self.config.auto_abort_on_severe:
            logger.warning("auto_abort_on_severe enabled — stopping sequence")
            await self.nina.stop_sequence()
            await notify(self.config, "Sequence stopped by nanny.", priority=1)

    async def _next_milestone(self) -> str:
        """Hours until the armer does its next thing (best-effort)."""
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(
                    f"http://localhost:{self.config.scheduler_port}/api/arm")
                d = r.json()
        except Exception:  # noqa: BLE001
            return ""
        now = datetime.utcnow()

        def hrs(iso):
            if not iso:
                return None
            delta = (datetime.fromisoformat(iso.rstrip("Z")) - now
                     ).total_seconds() / 3600
            return round(delta, 1) if delta > 0 else None

        state = d.get("state", "DISARMED")
        if state == "ARMED":
            h = hrs(d.get("preconfig_utc"))
            return f" Next: pre-config in {h}h." if h else ""
        if state == "RUNNING":
            hd = hrs(d.get("dusk_utc"))
            if hd:
                return f" Next: imaging starts in {hd}h."
            h = hrs(d.get("dawn_utc"))
            return f" {h}h of dark remaining until shutdown." if h else ""
        if state == "PAUSED_UNSAFE":
            h = hrs(d.get("dawn_utc"))
            return (f" Paused (unsafe); {h}h of dark left — resumes if safe, "
                    "makes safe at dawn." if h else "")
        return ""

    async def _heartbeat_loop(self):
        """Low-priority 'still alive' ping with time-to-next-milestone."""
        while self._running:
            await asyncio.sleep(self.config.heartbeat_minutes * 60)
            milestone = await self._next_milestone()
            await notify(
                self.config,
                f"Nanny alive. {self.state.images_captured_tonight} subs tonight, "
                f"state={self.state.session_state.value}.{milestone}",
                title="PhotonScript heartbeat",
                priority=-1,
            )

    # ------------------------------------------------------------------
    # Monitoring loops
    # ------------------------------------------------------------------

    COOL_FAIL_GRACE_S = 600   # cooler must show progress within 10 min
    COOL_ALERT_GRACE_S = 1200  # off-setpoint this long before the "cooling" alert
    COOL_FIX_MAX = 3          # reconnect attempts per night

    async def _dew_heater_watchdog(self, camera: dict):
        """Cooler ON => window dew heater ON, always (2026-09-08 request).

        The heater draws a couple of watts; a fogged window in monsoon
        humidity costs a night. The driver does not reliably report heater
        state, so while the cooler is on we (re)assert the heater at most
        every 15 minutes. If the NINA API cannot switch it, escalate once
        via Pushover and stand down.
        """
        import time
        if self._dew_api_broken or not camera.get("CoolerOn"):
            return
        if self._cooling_hold(camera):  # PS-79: never after the dawn shutdown
            return
        if camera.get("HasDewHeater") is False:
            return  # this camera has no heater to switch (don't false-alarm)
        if camera.get("DewHeaterOn") is True:
            self._dew_last_set = time.monotonic()
            return
        if time.monotonic() - self._dew_last_set < 900:
            return
        try:
            await self.nina.set_dew_heater(True)
            self._dew_last_set = time.monotonic()
            logger.info("Dew-heater watchdog: heater ON asserted "
                        "(cooler is running)")
        except Exception as e:  # noqa: BLE001
            self._dew_api_broken = True
            logger.warning("Dew-heater watchdog: NINA API cannot switch the "
                           "heater (%s) — manual toggle needed", e)
            await self._escalate(
                "dew-heater",
                "Cooler is ON but the dew heater cannot be switched via the "
                "NINA API — turn it ON manually (Equipment > Camera).")

    SAFETY_POLL_S = 15         # dedicated safety loop cadence (independent poll)
    SAFETY_GRACE_S = 120       # tolerate a brief drop before acting
    SAFETY_FAST_ATTEMPTS = 5   # quick reconnects at poll pace, then steady retry
    SAFETY_RETRY_S = 60        # after the fast burst: keep RE-connecting every 60s
    SAFETY_SLOW_RETRY_S = 3600 # re-ESCALATE (Pushover) every 60 min while down
                               # (config safety_disconnect_repeat_min overrides)
    SAFETY_ABORT_AFTER_S = 300 # persistent-disconnect abort threshold (opt-in)

    async def _safety_loop(self):
        """Run the safety-monitor watchdog on its OWN cadence, isolated from the
        camera/mount poll. A slow or failing NINA call elsewhere must never keep
        the reconnect from firing — the safety monitor is the one device that,
        left disconnected, lets the rig image a closed roof."""
        while self._running:
            try:
                await self._safety_monitor_watchdog()
            except Exception as e:  # noqa: BLE001 - never let this loop die
                logger.warning("safety loop iteration errored: %s", e)
            await asyncio.sleep(self.SAFETY_POLL_S)

    # Armer states in which a night is in progress (mirrors
    # photonscript.scheduler.armer.ACTIVE_STATES; a test keeps them in sync).
    _ARMER_ACTIVE_STATES = ("ARMED", "RUNNING", "PAUSED_UNSAFE")

    def _armer_state(self) -> str | None:
        """The armer's persisted state, or None when there is no readable
        state file."""
        import json
        p = Path(getattr(self.config, "data_dir", ".")) / "armer_state.json"
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("state")
        except Exception:  # noqa: BLE001
            return None

    def _cooling_hold(self, camera: dict | None = None) -> str | None:
        """PS-79: why the cooler / dew watchdogs must leave the camera alone,
        or None.

        After the dawn shutdown (armer COMPLETE) or a disarm the rig is shut
        down until the next arm: NINA's WarmCamera keeps the TEC on for up to
        15 min while it warms, and the watchdogs used to read that as "cooler
        on" and turn the dew heater back on, alert "cooler on at the wrong
        setpoint" and even reconnect and re-cool the camera. No armer state
        file at all keeps the old behavior (nothing says the rig is shut
        down). The camera's own setpoint is deliberately NOT used to spot a
        warm: a wrong setpoint while armed is exactly what the cooling alert
        must still report."""
        st = self._armer_state()
        if st is not None and st not in self._ARMER_ACTIVE_STATES:
            return f"armer {st} (shut down until the next arm)"
        return None

    def _log_hold(self, why: str | None) -> None:
        if why and why != getattr(self, "_hold_logged", None):
            logger.info("Cooler/dew watchdogs standing down [%s]: %s",
                        getattr(self, "rig", "rc16"), why)
        self._hold_logged = why

    def _armer_active(self) -> bool:
        """True when the scheduler's armer has a night in progress. Read from
        its persisted state file so this works in any run mode."""
        import json
        p = Path(getattr(self.config, "data_dir", ".")) / "armer_state.json"
        try:
            state = json.loads(p.read_text(encoding="utf-8")).get("state")
        except Exception:  # noqa: BLE001 - no file / unreadable = not armed
            return False
        return state in self._ARMER_ACTIVE_STATES

    def _safety_watch_needed(self) -> bool:
        """The reconnect watchdog only matters when the rig could be imaging:
        a night is armed, or the sun is down. In daylight with nothing armed
        it stays out of the way (no reconnect cycling while you swap devices
        in NINA, no DISCONNECTED pushes about a monitor nobody is using)."""
        if self._armer_active():
            return True
        from datetime import timezone
        from photonscript.shared.pushover import sun_altitude_deg
        try:
            lat = float(self.config.observatory_lat)
            lon = float(self.config.observatory_lon)
        except (AttributeError, TypeError, ValueError):
            return True  # unknown site: stay on (fail safe)
        alt = sun_altitude_deg(lat, lon, datetime.now(timezone.utc))
        return alt <= float(getattr(self.config, "safety_watchdog_sun_alt_deg", -3.0))

    def _safety_reset(self) -> None:
        self._safety_bad_since = None
        self._safety_bad_reads = 0
        self._safety_fix_attempts = 0
        self._safety_last_attempt = 0.0
        self._safety_last_escalate = 0.0
        self._safety_aborted = False
        self._alerted.discard("safety-disconnected")

    def _safety_target_id(self) -> str:
        """Which monitor a reconnect should name: the configured pin for this
        rig, else the device last seen connected, else '' (NINA's selection)."""
        return ((getattr(self.config, "safety_monitor_device_id", "") or "").strip()
                or getattr(self, "_safety_device_id", "") or "")

    async def _safety_connect(self) -> None:
        target = self._safety_target_id()
        if not target:
            await self.nina.connect_safety()
            return
        try:
            await self.nina.connect_safety(target)
        except Exception as e:  # noqa: BLE001 - pinned Id gone? fall back
            logger.warning("Safety-monitor watchdog: connect to %s failed (%s) — "
                           "falling back to NINA's selected device", target, e)
            await self.nina.connect_safety()

    async def _safety_monitor_watchdog(self):
        """Keep the NINA safety monitor CONNECTED all night — a disconnected
        monitor is as dangerous as bad weather, because the sequence goes
        blind to the sky.

        2026-09-09/11/13 lessons: the ASCOM Alpaca monitor ("AARO Safety Obs 2")
        is slow (poll cycles routinely run 2-10 s against a 2 s interval) and
        NINA eventually drops it on a Connected-property error (seen 2026-09-13
        01:33). The watchdog: reconnects fast at first, then keeps RE-connecting
        every 60 s for the rest of the night (never backs off to 30-min gaps, so
        it recovers within ~1 min of the device coming back); on a stubborn drop
        it cycles disconnect->connect to clear a wedged ASCOM handle; escalates
        SEVERE and re-alerts every 30 min while still down; and — if
        safety_disconnect_aborts is set — stops a RUNNING sequence blind too long.
        """
        import time
        from photonscript.shared.models import SessionState
        if not self._safety_watch_needed():
            if not getattr(self, "_safety_idle", False):
                logger.info("Safety-monitor watchdog idle [%s]: sun up and "
                            "nothing armed", getattr(self, "rig", "rc16"))
                self._safety_idle = True
            self._safety_reset()
            return
        if getattr(self, "_safety_idle", False):
            logger.info("Safety-monitor watchdog active [%s]",
                        getattr(self, "rig", "rc16"))
            self._safety_idle = False
        try:
            info = await self.nina.get_safety_info()
        except Exception as _e:  # noqa: BLE001 - NINA itself unreachable
            # NINA down = we are BLIND to safety — at least as bad as a
            # disconnected monitor. Don't return mute: log (once, on transition)
            # and fall into the same escalation path so sustained blindness
            # alerts. The grace period still absorbs brief NINA blips.
            if self._safety_bad_since is None:
                logger.warning("Safety-monitor watchdog: NINA unreachable (%s) "
                               "— treating as safety-blind", _e)
            info = {"Connected": False}

        if info.get("Connected"):
            dev = info.get("DeviceId") or ""
            if dev and dev != getattr(self, "_safety_device_id", ""):
                logger.info("Safety-monitor watchdog [%s]: tracking %s",
                            getattr(self, "rig", "rc16"), dev)
                self._safety_device_id = dev
            if self._safety_bad_since is not None:
                logger.info("Safety-monitor watchdog: monitor connected again")
            self._safety_bad_since = None
            self._safety_bad_reads = 0
            self._safety_fix_attempts = 0
            self._safety_last_attempt = 0.0
            self._safety_last_escalate = 0.0
            self._safety_aborted = False
            self._alerted.discard("safety-disconnected")
            return

        # Debounce: require Connected:False on 2 consecutive SAFETY_POLL_S polls
        # before latching _safety_bad_since, so one racy/slow AlpacaDynamic3 read
        # can't start the down-clock or arm remediation. A single blip returns
        # early; SAFETY_GRACE_S still absorbs longer drops before any action.
        self._safety_bad_reads = getattr(self, "_safety_bad_reads", 0) + 1
        if self._safety_bad_reads < 2:
            return  # first bad read — treat as transient, wait for confirmation
        now = time.monotonic()
        if self._safety_bad_since is None:
            self._safety_bad_since = now
            return
        down_s = now - self._safety_bad_since
        if down_s < self.SAFETY_GRACE_S:
            return  # brief blip — don't act yet

        # --- escalate (severe), and repeat every 30 min while still down ----
        repeat_s = 60 * float(getattr(self.config, "safety_disconnect_repeat_min",
                                      self.SAFETY_SLOW_RETRY_S / 60))
        if now - self._safety_last_escalate >= repeat_s:
            self._safety_last_escalate = now
            self._alerted.discard("safety-disconnected")  # allow re-fire
            await self._escalate(
                "safety-disconnected",
                f"Safety monitor DISCONNECTED for {int(down_s // 60)} min — the "
                "sequence is blind to weather and may image a closed roof. "
                "Auto-reconnect is running; if it persists, reconnect it in "
                "NINA (Equipment > Safety Monitor).", severe=True)

        # --- reconnect: fast burst, then keep trying every 60s (never give up) -
        interval = 0 if self._safety_fix_attempts < self.SAFETY_FAST_ATTEMPTS \
            else self.SAFETY_RETRY_S
        if now - self._safety_last_attempt >= interval:
            self._safety_last_attempt = now
            self._safety_fix_attempts += 1
            try:
                # After the first plain attempt, cycle disconnect->connect: a
                # wedged ASCOM handle (the Connected-property error NINA hit)
                # often needs a clean drop before it will re-attach.
                if self._safety_fix_attempts > 1:
                    try:
                        await self.nina.disconnect_safety()
                        await asyncio.sleep(1)
                    except Exception:  # noqa: BLE001 - best effort
                        pass
                await self._safety_connect()
                # The slow AlpacaDynamic3 driver often still reads
                # Connected:False for a few seconds right after a SUCCESSFUL
                # connect. Don't trust a single immediate read — poll over a
                # short settle window before declaring the reconnect failed,
                # otherwise a good reconnect never clears _safety_bad_since and
                # the watchdog re-cycles the monitor every 60 s.
                ok = False
                for _ in range(4):  # ~ up to 6 s for the slow Alpaca driver
                    await asyncio.sleep(1.5)
                    try:
                        if (await self.nina.get_safety_info()).get("Connected"):
                            ok = True
                            break
                    except Exception:  # noqa: BLE001
                        pass
                if ok:
                    logger.info("Safety-monitor watchdog: reconnected on "
                                "attempt %d", self._safety_fix_attempts)
                    await self._escalate(
                        "safety-reconnected",
                        "Safety monitor was disconnected and has been "
                        "auto-reconnected — the sequence can see weather again.")
                    self._safety_bad_since = None
                    self._safety_bad_reads = 0
                    self._safety_fix_attempts = 0
                    self._safety_last_escalate = 0.0
                    self._safety_aborted = False
                    self._alerted.discard("safety-disconnected")
                    return
            except Exception as e:  # noqa: BLE001
                logger.warning("Safety-monitor watchdog: reconnect attempt %d "
                               "failed: %s", self._safety_fix_attempts, e)

        # --- last resort: stop a sequence that has been blind too long ------
        if (getattr(self.config, "safety_disconnect_aborts", False)
                and not self._safety_aborted
                and self.state.session_state == SessionState.IMAGING
                and down_s >= self.SAFETY_ABORT_AFTER_S):
            self._safety_aborted = True
            logger.warning("safety_disconnect_aborts: stopping sequence — "
                           "safety monitor blind for %ds while imaging",
                           int(down_s))
            try:
                await self.nina.stop_sequence()
            except Exception as e:  # noqa: BLE001
                logger.error("safety-abort stop_sequence failed: %s", e)
            await self._escalate(
                "safety-abort",
                "Sequence STOPPED: the safety monitor was disconnected while "
                "imaging and could not be recovered — imaging blind is worse "
                "than losing the night. Reconnect it in NINA and re-arm.",
                severe=True)

    async def _cooling_watchdog(self, camera: dict):
        """Active remediation for 'CoolerOn but 0% power, sensor at ambient'.

        Observed 2026-07-03..05: the OGMA driver reported CoolerOn=true with
        CoolerPower=0% and the sensor never left ambient — three sessions
        imaged at +30..40C. When that signature persists past the grace
        period, disconnect/reconnect the camera and re-issue the cool
        command (cycles the driver's cooler state). A sub in flight is
        sacrificed knowingly: at ambient temperature it was garbage anyway.
        """
        import time
        if self._cooling_hold(camera):  # PS-79: a warm is not a dead cooler
            self._cool_bad_since = None
            return
        cooler_on = camera.get("CoolerOn", False)
        power = camera.get("CoolerPower")
        temp = camera.get("Temperature")
        sp = self.config.camera_setpoint_c
        tol = self.config.cooling_tolerance_c
        failed = (cooler_on and temp is not None
                  and temp > sp + max(3 * tol, 3.0)
                  and (power is None or power <= 1.0))
        now = time.monotonic()
        if not failed:
            self._cool_bad_since = None
            if temp is not None and temp <= sp + tol:
                self._cool_fix_attempts = 0  # reached setpoint — reset budget
            return
        if self._cool_bad_since is None:
            self._cool_bad_since = now
            return
        if now - self._cool_bad_since < self.COOL_FAIL_GRACE_S:
            return
        if self._cool_fix_attempts >= self.COOL_FIX_MAX:
            await self._escalate(
                "cooling-dead",
                f"Cooler STILL not cooling after {self.COOL_FIX_MAX} camera "
                f"reconnects — sensor {temp:.1f}C, setpoint {sp:.1f}C. "
                "Manual intervention (12V / PDU outlet?) needed.",
                severe=True)
            return
        self._cool_fix_attempts += 1
        self._cool_bad_since = now  # restart grace clock for this attempt
        n = self._cool_fix_attempts
        logger.warning("Cooling watchdog: reconnect attempt %d/%d "
                       "(sensor %.1fC, power %s%%)", n, self.COOL_FIX_MAX,
                       temp, power)
        # Alert ONLY on the first attempt — the intermediate retries used to fire
        # a Pushover each ("attempt 2/4, 3/4…"), a burst per stuck-cooler episode.
        # The final give-up still escalates (above), so nothing important is lost.
        if n == 1:
            await notify(
                self.config,
                f"Cooler on but {0 if power is None else power:.0f}% power at "
                f"{temp:.1f}C (setpoint {sp:.1f}C) — reconnecting the camera and "
                f"re-cooling (up to {self.COOL_FIX_MAX} tries).",
                title="PhotonScript cooling watchdog", priority=1)
        try:
            await self.nina.disconnect_camera()
            await asyncio.sleep(10)
            await self.nina.connect_camera()
            await asyncio.sleep(5)
            # Instant re-cool (no 10-min ramp — a ramp fought arm/precool). Honors
            # cool_ramp_minutes so it matches the rest of the cooling path.
            await self.nina.cool_camera(
                sp, minutes=float(getattr(self.config, "cool_ramp_minutes", 0.0)))
            logger.info("Cooling watchdog: cool command re-issued (%.1fC)", sp)
        except Exception as e:  # noqa: BLE001
            logger.error("Cooling watchdog attempt %d errored: %s", n, e)
            if n == 1:   # first error only; the give-up escalation covers the rest
                await notify(self.config,
                             f"Cooling watchdog reconnect errored: {e}",
                             title="PhotonScript cooling watchdog", priority=1)

    async def _nina_poll_loop(self):
        """Poll NINA for equipment state every few seconds."""
        while self._running:
            try:
                # Get camera info
                camera = await self.nina.get_camera_info()
                self.state.camera_temp_c = camera.get("Temperature")
                self.state.camera_cooling_on = camera.get("CoolerOn", False)

                # Cooling watch: cooler on but sensor off-setpoint (the 0°C
                # incident). Must PERSIST past COOL_ALERT_GRACE_S: a normal
                # cool-down takes minutes and used to be indistinguishable.
                # (This loop only started receiving real data on 2026-09-26,
                # when the client moved to the ninaAPI v2 /info endpoints.)
                import time as _t
                hold = self._cooling_hold(camera)  # PS-79
                self._log_hold(hold)
                off = (not hold and self.state.camera_cooling_on
                       and self.state.camera_temp_c is not None
                       and abs(self.state.camera_temp_c - self.config.camera_setpoint_c)
                       > self.config.cooling_tolerance_c)
                if not off:
                    self._cool_off_since = None
                elif getattr(self, "_cool_off_since", None) is None:
                    self._cool_off_since = _t.monotonic()
                elif _t.monotonic() - self._cool_off_since >= self.COOL_ALERT_GRACE_S:
                    await self._escalate(
                        "cooling",
                        f"Sensor at {self.state.camera_temp_c:.1f}C with cooler on for "
                        f"{self.COOL_ALERT_GRACE_S // 60} min — setpoint is "
                        f"{self.config.camera_setpoint_c:.1f}C",
                    )
                await self._cooling_watchdog(camera)
                await self._dew_heater_watchdog(camera)
                # NB: the safety-monitor watchdog runs in its OWN loop
                # (_safety_loop), NOT here — so a slow/failing camera or mount
                # poll can never skip the safety reconnect (2026-09-13 lesson:
                # the monitor dropped at 01:33 and nothing reattempted it).

                # Get mount info
                mount = await self.nina.get_mount_info()
                self.state.mount_ra = mount.get("RightAscension")
                self.state.mount_dec = mount.get("Declination")
                self.state.mount_tracking = mount.get("Tracking", False)

                # Get focuser info
                try:
                    focuser = await self.nina.get_focuser_info()
                    self.state.focuser_position = focuser.get("Position")
                except Exception:
                    pass

                # Get sequence status
                seq = await self.nina.get_sequence_status()
                status = seq.get("State", "IDLE").upper()
                state_map = {
                    "IDLE": SessionState.IDLE,
                    "RUNNING": SessionState.IMAGING,
                    "PAUSED": SessionState.PAUSED,
                }
                self.state.session_state = state_map.get(status, SessionState.IDLE)

                # Current target from sequence
                current = seq.get("CurrentTarget")
                if current:
                    self.state.current_target = current.get("Name")

                self.state.updated_at = datetime.utcnow()

            except Exception as e:
                logger.debug("NINA poll error (may not be running): %s", e)

            await asyncio.sleep(5)

    async def _phd2_monitor(self):
        """Connect to PHD2 and monitor guiding events."""
        while self._running:
            connected = await self.phd2.connect()
            if connected:
                await self.phd2.refresh_pixel_scale()
            if connected:
                await self.phd2.run_event_loop()
            # Reconnect after delay
            await asyncio.sleep(10)

    # PS-70: judge guide RMS only on a real window of guide steps (30 steps is
    # about a minute at 2 s exposures), and log the breach at most every 5 min.
    RMS_MIN_SAMPLES = 30
    RMS_LOG_EVERY_S = 300

    async def _on_guiding_update(self, metrics: GuidingMetrics):
        """Called when PHD2 reports updated guiding metrics.

        The threshold (quality_tracking_rms_max) is TOTAL RMS in ARCSEC. It is
        only compared while PHD2 is actually guiding, over a full window, and
        only when the guide pixel scale is known: a pixel number is never
        compared with an arcsec threshold (the pre-PS-70 bug: "Guide RMS 98\""
        was 98 guide pixels). Both rigs' agents watch the same PHD2, so only
        the RC16 agent (the guide camera is on its OAG) logs and pushes."""
        self.state.guiding = metrics
        if getattr(self, "rig", "rc16") != "rc16":
            return
        if str(getattr(metrics.state, "value", metrics.state)).lower() != "guiding":
            return
        if metrics.samples < self.RMS_MIN_SAMPLES:
            return
        if metrics.units != "arcsec" or metrics.rms_total_arcsec is None:
            return
        limit = float(self.config.quality_tracking_rms_max)
        if metrics.rms_total_arcsec <= limit:
            return
        import time as _t
        now = _t.monotonic()
        if self._rms_logged_at is None or now - self._rms_logged_at >= self.RMS_LOG_EVERY_S:
            self._rms_logged_at = now
            logger.warning("Guiding RMS %s exceeds threshold %.2f\"",
                           guide_rms_text(metrics), limit)
        await self._escalate(
            f"rms-{datetime.utcnow():%Y%m%d%H}",  # re-alert at most hourly
            f"Guide RMS {guide_rms_text(metrics)} over threshold {limit:.2f}\"",
        )

    async def _file_watch_loop(self):
        """Watch the image output directory for new FITS/TIFF files.

        Uses polling on Windows since watchdog may need additional setup.
        """
        seen_files: set[str] = set()
        exts = (".fits", ".fit", ".tif", ".tiff", ".xisf")

        def _scan():
            # NINA nests output in date/target/LIGHT subfolders — walk the tree
            return [f for ext in exts
                    for f in self._watch_dir.rglob(f"*{ext}")]

        # Initialize with existing files
        if self._watch_dir.exists():
            for f in _scan():
                seen_files.add(str(f))

        while self._running:
            try:
                if not self._watch_dir.exists():
                    await asyncio.sleep(10)
                    continue

                for f in _scan():
                    fpath = str(f)
                    if fpath in seen_files:
                        continue

                    # Wait for file to finish writing
                    await asyncio.sleep(2)
                    size1 = f.stat().st_size
                    await asyncio.sleep(1)
                    size2 = f.stat().st_size
                    if size1 != size2:
                        continue  # Still being written

                    seen_files.add(fpath)
                    await self._process_new_image(f)

            except Exception as e:
                logger.error("File watch error: %s", e)

            await asyncio.sleep(3)

    @staticmethod
    def _canonical_capture_name(name) -> str:
        """PS-78: the target a live name refers to, '' when it names none.
        NINA's running container ("Heart Nebula imaging (repeats while safe
        and up)_Container") maps to "Heart Nebula"; a structural loop such as
        the Piggy-600's OSC_LIGHT_LOOP_Container maps to '' so the header
        match / plan rule / dawn correlation name the sub instead."""
        try:
            from photonscript.shared.target_names import canonical_target
            return canonical_target(name) or ""
        except Exception as e:  # noqa: BLE001
            logger.debug("target canonicalize skipped: %s", e)
            s = str(name or "").strip()
            return "" if s == "?" else s

    async def _process_new_image(self, file_path: Path):
        """Process a newly captured image — validate quality and report."""
        # Calibration frames (darks/flats/bias) are inventoried by the
        # calibration panel, not graded as lights: skip by folder or header
        _CAL_DIRS = {"DARK", "DARKS", "FLAT", "FLATS", "BIAS", "BIASES",
                     "SNAPSHOT"}
        if any(p.upper() in _CAL_DIRS for p in file_path.parts):
            return
        hdr = {}
        try:
            from astropy.io import fits as _fits
            hdr = dict(_fits.getheader(file_path))
            imagetyp = str(hdr.get("IMAGETYP", "LIGHT")).strip().upper()
            if imagetyp and "LIGHT" not in imagetyp:
                logger.debug("Skipping %s frame: %s", imagetyp,
                             file_path.name)
                return
        except Exception:  # noqa: BLE001 — unreadable yet; let grading retry
            pass
        logger.info("New image detected: %s", file_path.name)

        # Metadata: FITS header first (authoritative), filename fallback.
        # NINA's current pattern is <date>_<time>__<F>_<exp>.00s_<idx>;
        # the old split('_') read the TIME token as the filter and the empty
        # token as the exposure, so every live record since 2026-07-28 was
        # logged as L / 300s / target=<date>  (2026-09-04 lesson).
        import re as _re
        stem = file_path.stem
        filter_str = str(hdr.get("FILTER") or "").strip()
        if not filter_str:
            m = _re.search(r"__([A-Za-z][A-Za-z0-9]*)_", stem)
            filter_str = m.group(1) if m else "L"
        # NINA profile filter name (e.g. 'H') -> canonical (Ha)
        filter_str = self.config.reverse_filter_map().get(filter_str, filter_str)
        try:
            filter_type = FilterType(filter_str)
        except ValueError:
            filter_type = FilterType.LUMINANCE

        try:
            exposure_seconds = float(hdr.get("EXPTIME") or 0)
        except (TypeError, ValueError):
            exposure_seconds = 0.0
        if not exposure_seconds:
            m = _re.search(r"_(\d+(?:\.\d+)?)s", stem)
            exposure_seconds = float(m.group(1)) if m else 300.0

        target_name = str(hdr.get("OBJECT") or "").strip()
        object_in_header = bool(target_name)
        target_name = self._canonical_capture_name(target_name)  # PS-78
        if not target_name:
            tok = stem.split("_")[0]
            if not _re.match(r"^\d{4}-\d{2}-\d{2}$", tok):
                target_name = tok
        if not target_name:
            # NINA knows the active target even when it doesn't stamp OBJECT —
            # the poll loop keeps it in state.current_target (the 2026-09-11
            # donut night logged 11 subs as '?' with a live target the whole
            # time). Trust it as the primary fallback.
            live = self._canonical_capture_name(  # PS-78: not the container
                getattr(self.state, "current_target", ""))
            if live and live != "?":
                target_name = live
        if not target_name:
            # PS-51: the mount's RA/DEC is in every RC16 header; match it to a
            # campaign target now, so the sub is named from the first moment
            # (a 2-target night used to log every sub as '?'). Piggy-600
            # frames carry no coordinates and fall through to the plan rule /
            # the dawn time-correlation pass.
            try:
                from photonscript.scheduler.identify import target_from_header
                hit = target_from_header(self.config, hdr)
                if hit:
                    target_name = hit
            except Exception as e:  # noqa: BLE001
                logger.debug("header target match skipped: %s", e)
        if not target_name:
            # unambiguous if the night's plan has exactly one target
            try:
                from photonscript.scheduler.runs import _plan_target_names
                date_tok = next((p for p in file_path.parts
                                 if _re.match(r"^\d{4}-\d{2}-\d{2}$", p)), None)
                if date_tok:
                    names = _plan_target_names(self.config, date_tok)
                    if len(names) == 1:
                        target_name = names[0]
            except Exception:  # noqa: BLE001
                pass
        if not target_name:
            target_name = "?"

        # Stamp the resolved name back into the FITS OBJECT header when NINA
        # left it blank, so downstream tools (PixInsight, identify, the runs
        # page) carry the target instead of '?'. Never overwrites an existing
        # OBJECT; entirely best-effort.
        if (target_name != "?" and not object_in_header
                and getattr(self.config, "stamp_fits_object", True)):
            try:
                from photonscript.shared.fits_object import stamp_object
                stamp_object(file_path, target_name)
            except Exception as e:  # noqa: BLE001
                logger.debug("OBJECT stamp skipped for %s: %s",
                             file_path.name, e)

        # PS-21: measure, then grade ONCE with the full context through
        # shared.qa_rules.evaluate, the same rules the backfill grader uses
        # (frame metrics + sensor temp vs the configured setpoint + guiding
        # snapshot + PS-71 roof-closed / parked signatures + night medians).
        quality = validate_image(str(file_path), self.config, rig=self.rig)
        _g = self.state.guiding
        # PS-70: only an arcsec RMS is judged; None when the guide pixel
        # scale is unknown, so a pixel number never meets the arcsec gate.
        quality.tracking_rms_arcsec = (_g.rms_total_arcsec
                                       if getattr(_g, "units", "arcsec") == "arcsec"
                                       else None)
        # GuidingState is a str Enum: str() gives 'GuidingState.GUIDING', so
        # the old str(...).lower() in ("guiding", ...) test never matched and
        # the live RMS gate never ran. Compare the enum value.
        _gs = getattr(_g, "state", "")
        guide_state = str(getattr(_gs, "value", _gs) or "").lower()
        # The piggyback is a one-shot-color rig: record its filter as OSC
        # regardless of NINA's (empty/L) filter token.
        rec_filter = "OSC" if self.rig != "rc16" else filter_type.value
        night = rel_in_night = None
        try:
            watch = Path(self.config.image_watch_dir)
            rel = file_path.relative_to(watch)
            night = rel.parts[0] if rel.parts and \
                rel.parts[0][:2] == "20" else datetime.utcnow().strftime("%Y-%m-%d")
            rel_in_night = str(Path(*rel.parts[1:])) if len(rel.parts) > 1 \
                else file_path.name
        except ValueError:
            pass
        from photonscript.shared import qa_rules
        from photonscript.telescope_agent.image_validator import image_metrics
        start = wins = None
        try:  # PS-71 inputs: exposure start + UNSAFE safety-monitor windows
            from photonscript.shared.qa_signatures import exposure_start
            from photonscript.shared.safety_history import unsafe_windows
            start = exposure_start(hdr.get("DATE-OBS"),
                                   datetime.utcnow(), exposure_seconds)
            if start is not None:
                wins, _src = unsafe_windows(
                    self.config, start,
                    start + timedelta(seconds=exposure_seconds or 0))
        except Exception as e:  # noqa: BLE001 - never lose a sub over this
            logger.warning("safety history skipped for %s: %s",
                           file_path.name, e)
        night_ctx = None
        if night:
            try:  # night medians so far for this rig + target + filter
                from photonscript.scheduler.runs import _load_subs
                key = (self.rig, target_name, rec_filter)
                night_ctx = qa_rules.night_context(
                    [r for r in _load_subs(self.config, night)
                     if qa_rules.group_key(r) == key]).get(key)
            except Exception as e:  # noqa: BLE001
                logger.debug("night context skipped: %s", e)
        metrics = image_metrics(quality)
        metrics.update(exp_s=exposure_seconds,
                       ccd_temp=self.state.camera_temp_c,
                       set_temp=hdr.get("SET-TEMP"),
                       guide_rms=quality.tracking_rms_arcsec,
                       guide_state=guide_state)
        card = qa_rules.evaluate(metrics, qa_rules.context(
            self.config, self.rig, target_name, rec_filter, night=night_ctx,
            unsafe_windows=wins, start_utc=start))
        quality.passed_qa = card.passed
        quality.rejection_reason = card.reason
        qa_flag = card.qa_flag

        # Create image record
        image = CapturedImage(
            id=str(uuid4()),
            project_id="",  # Will be matched by scheduler
            filename=file_path.name,
            file_path=str(file_path),
            file_size_bytes=file_path.stat().st_size,
            target_name=target_name,
            filter_type=filter_type,
            exposure_seconds=exposure_seconds,
            camera_temp_c=self.state.camera_temp_c,
            status=ImageStatus.VALIDATED if quality.passed_qa else ImageStatus.REJECTED,
            quality=quality,
        )

        self.state.images_captured_tonight += 1
        self.state.last_image = image
        self.state.current_filter = filter_type

        # Persist per-sub record for the Imaging Runs page (night = local
        # date folder NINA used, i.e. the parent date directory if present)
        try:
            from photonscript.scheduler.runs import append_sub_record
            if night is None:
                raise ValueError(f"{file_path} is not under image_watch_dir")
            rec = {
                "rig": self.rig,
                "file": rel_in_night, "abs_path": str(file_path),
                "time": datetime.utcnow().isoformat() + "Z",
                "target": target_name, "filter": rec_filter,
                "exp_s": exposure_seconds,
                "ccd_temp": self.state.camera_temp_c,
                "hfr": quality.hfr_pixels, "fwhm_arcsec": quality.fwhm_arcsec,
                "stars": quality.star_count, "ecc": quality.eccentricity,
                # PS-94: 2x2-binned measure (RC16), both ecc in sqrt form
                "ecc_bin": quality.ecc_bin, "hfr_bin": quality.hfr_bin_px,
                "ecc_def": "sqrt(1-(b/a)^2)",
                "background": quality.background_adu,
                # PS-21: measured inputs live grading used to drop
                "noise": quality.noise_adu,
                "setpoint_c": card.thresholds.get("setpoint_c"),
                "set_temp": hdr.get("SET-TEMP"),
                "guide_rms": (round(quality.tracking_rms_arcsec, 3)
                              if quality.tracking_rms_arcsec is not None
                              else None),
                "guide_state": guide_state or None,
                "corner_spread": quality.corner_spread,
                "clipped_pct": quality.clipped_pct,
                "sat_stars_pct": quality.sat_star_pct,
                "swamp": quality.swamp_factor,
                "exposure": quality.exposure_flag,
            }
            # passed_qa, reason, qa_flag, scorecard, auto_verdict,
            # auto_reason, drivers (+ reviewed / review_source when all green)
            rec.update(card.record_fields())
            append_sub_record(self.config, night, rec)
            try:  # PS-80: the stars behind the medians, for the overlay
                from photonscript.shared.star_table import write as _w_stars
                _w_stars(self.config, night, rel_in_night, quality.star_table,
                         rig=self.rig)
            except Exception as se:  # noqa: BLE001
                logger.debug("star sidecar skipped: %s", se)
            # Pre-warm the runs-grid thumbnail (w=264) so the Runs page never
            # blocks generating it on first view. The RC16 gets this in the
            # backfill grade; the piggyback is graded live here, so warm it now.
            # Best-effort and nested so a thumbnail miss never drops the record.
            try:
                from photonscript.scheduler.runs import (
                    thumbnail, PREWARM_THUMB_WIDTH)
                thumbnail(self.config, night, rel_in_night,
                          width=PREWARM_THUMB_WIDTH, annotate=False,
                          fill_prewarm=True)
            except Exception as te:  # noqa: BLE001
                logger.debug("thumb pre-warm skipped: %s", te)
        except Exception as e:  # noqa: BLE001
            # A failed sub-record write means this exposure silently never
            # reaches the runs page, library, or transfer funnel — a real
            # imaging loss, not a cosmetic miss. Surface it loudly (was DEBUG).
            self._sub_write_failures = getattr(self, "_sub_write_failures", 0) + 1
            logger.error("Sub record append FAILED (%s) — sub %s NOT recorded; "
                         "it will be missing from the runs page and library",
                         e, locals().get("rel_in_night", "?"))

        # Nanny: consecutive rejects mean something systemic (clouds, dew,
        # focus loss, tracking) — a single bad sub is just a bad sub.
        if quality.passed_qa:
            self._consecutive_rejects = 0
        else:
            self._consecutive_rejects += 1
            if self._consecutive_rejects >= self.config.consecutive_reject_limit:
                await self._escalate(
                    f"rejects-{datetime.utcnow():%Y%m%d%H}",
                    f"{self._consecutive_rejects} consecutive rejected subs "
                    f"(last: {quality.rejection_reason})",
                    severe=True,
                )

        # Collimation/tilt watch (RC16): corner FWHM spread trending high
        if (quality.corner_spread is not None
                and quality.corner_spread > self.config.quality_corner_spread_max):
            await self._escalate(
                f"corners-{datetime.utcnow():%Y%m%d}",  # at most daily
                f"Corner FWHM spread {quality.corner_spread:.2f} exceeds "
                f"{self.config.quality_corner_spread_max:.2f} — check collimation/tilt",
            )

        # Report to scheduler
        await self.bus.publish(AgentMessage(
            sender=AgentRole.TELESCOPE,
            recipient=AgentRole.SCHEDULER,
            msg_type="image_captured",
            payload=image.model_dump(mode="json"),
        ))

        # Report quality
        qa_status = "PASS" if quality.passed_qa else f"REJECT ({quality.rejection_reason})"
        logger.info(
            "Image %s: FWHM=%.1f\" HFR=%.1fpx Stars=%d Ecc=%.2f — %s",
            file_path.name, quality.fwhm_arcsec or 0, quality.hfr_pixels or 0,
            quality.star_count, quality.eccentricity or 0, qa_status,
        )

        await self.bus.publish(AgentMessage(
            sender=AgentRole.TELESCOPE,
            recipient=AgentRole.LIBRARIAN,
            msg_type="image_quality_report",
            payload={
                "image_id": image.id,
                "file_path": str(file_path),
                "passed_qa": quality.passed_qa,
                "quality": quality.model_dump(mode="json"),
            },
        ))

    async def _state_broadcast_loop(self):
        """Periodically broadcast telescope state to the scheduler."""
        while self._running:
            await self.bus.publish(AgentMessage(
                sender=AgentRole.TELESCOPE,
                recipient=AgentRole.SCHEDULER,
                msg_type="telescope_state_update",
                payload=self.state.model_dump(mode="json"),
            ))
            await asyncio.sleep(10)

    async def _on_command(self, msg: AgentMessage):
        """Handle commands from the scheduler."""
        if msg.recipient != AgentRole.TELESCOPE:
            return

        action = msg.payload.get("action")
        logger.info("Received command: %s", action)

        if action == "start_sequence":
            file_path = msg.payload.get("sequence_file")
            if file_path:
                await self.nina.load_sequence(file_path)
                await self.nina.start_sequence()

        elif action == "stop_sequence":
            await self.nina.stop_sequence()

        elif action == "start_guiding":
            await self.phd2.start_guiding()

        elif action == "stop_guiding":
            await self.phd2.stop_guiding()

        elif action == "dither":
            await self.phd2.dither()
