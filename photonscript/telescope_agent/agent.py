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
from datetime import datetime
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
from photonscript.telescope_agent.phd2_client import PHD2Client
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

    def __init__(self, config: PhotonScriptConfig):
        self.config = config
        self.nina = NinaClient(config.nina_base_url)
        self.phd2 = PHD2Client(config.phd2_host, config.phd2_port)
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
        self._safety_fix_attempts = 0
        self._safety_last_attempt: float = 0.0
        self._safety_last_escalate: float = 0.0
        self._safety_aborted = False

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

    SAFETY_GRACE_S = 120       # tolerate a brief drop before acting
    SAFETY_FAST_ATTEMPTS = 5   # quick reconnects at poll pace, then back off
    SAFETY_SLOW_RETRY_S = 1800 # after that: retry + re-escalate every 30 min
    SAFETY_ABORT_AFTER_S = 300 # persistent-disconnect abort threshold (opt-in)

    async def _safety_monitor_watchdog(self):
        """Keep the NINA safety monitor CONNECTED all night — a disconnected
        monitor is as dangerous as bad weather, because the sequence goes
        blind to the sky.

        2026-09-09/11 lesson: the ASCOM Alpaca monitor sat disconnected the
        whole night. WaitUntilSafe never released, SafetyMonitorCondition let
        the rig image a closed roof, and ~1 h of donuts resulted — silently.
        The old watchdog gave up after 5 tries and alerted only once at low
        priority. Now it reconnects fast at first, then keeps retrying every
        30 min for the rest of the night, escalates as SEVERE and re-alerts
        every 30 min while still down, and — if safety_disconnect_aborts is
        set — stops a RUNNING sequence that has been blind too long.
        """
        import time
        from photonscript.shared.models import SessionState
        try:
            info = await self.nina.get_safety_info()
        except Exception:  # noqa: BLE001 - NINA itself unreachable
            return

        if info.get("Connected"):
            if self._safety_bad_since is not None:
                logger.info("Safety-monitor watchdog: monitor connected again")
            self._safety_bad_since = None
            self._safety_fix_attempts = 0
            self._safety_last_attempt = 0.0
            self._safety_last_escalate = 0.0
            self._safety_aborted = False
            self._alerted.discard("safety-disconnected")
            return

        now = time.monotonic()
        if self._safety_bad_since is None:
            self._safety_bad_since = now
            return
        down_s = now - self._safety_bad_since
        if down_s < self.SAFETY_GRACE_S:
            return  # brief blip — don't act yet

        # --- escalate (severe), and repeat every 30 min while still down ----
        if now - self._safety_last_escalate >= self.SAFETY_SLOW_RETRY_S:
            self._safety_last_escalate = now
            self._alerted.discard("safety-disconnected")  # allow re-fire
            await self._escalate(
                "safety-disconnected",
                f"Safety monitor DISCONNECTED for {int(down_s // 60)} min — the "
                "sequence is blind to weather and may image a closed roof. "
                "Auto-reconnect is running; if it persists, reconnect it in "
                "NINA (Equipment > Safety Monitor).", severe=True)

        # --- reconnect: fast for the first few tries, then every 30 min -----
        interval = 0 if self._safety_fix_attempts < self.SAFETY_FAST_ATTEMPTS \
            else self.SAFETY_SLOW_RETRY_S
        if now - self._safety_last_attempt >= interval:
            self._safety_last_attempt = now
            self._safety_fix_attempts += 1
            try:
                await self.nina.connect_safety()
                info = await self.nina.get_safety_info()
                if info.get("Connected"):
                    logger.info("Safety-monitor watchdog: reconnected on "
                                "attempt %d", self._safety_fix_attempts)
                    await self._escalate(
                        "safety-reconnected",
                        "Safety monitor was disconnected and has been "
                        "auto-reconnected — the sequence can see weather again.")
                    self._safety_bad_since = None
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
        await notify(self.config,
                     f"Cooler on but {0 if power is None else power:.0f}% power "
                     f"at {temp:.1f}C (setpoint {sp:.1f}C) — reconnecting "
                     f"camera, attempt {n}/{self.COOL_FIX_MAX}",
                     title="PhotonScript cooling watchdog", priority=1)
        try:
            await self.nina.disconnect_camera()
            await asyncio.sleep(10)
            await self.nina.connect_camera()
            await asyncio.sleep(5)
            await self.nina.cool_camera(sp, minutes=10.0)
            logger.info("Cooling watchdog: cool command re-issued (%.1fC)", sp)
        except Exception as e:  # noqa: BLE001
            logger.error("Cooling watchdog attempt %d errored: %s", n, e)
            await notify(self.config,
                         f"Cooling watchdog reconnect attempt {n} errored: {e}",
                         title="PhotonScript cooling watchdog", priority=1)

    async def _nina_poll_loop(self):
        """Poll NINA for equipment state every few seconds."""
        while self._running:
            try:
                # Get camera info
                camera = await self.nina.get_camera_info()
                self.state.camera_temp_c = camera.get("Temperature")
                self.state.camera_cooling_on = camera.get("CoolerOn", False)

                # Cooling watch: cooler on but sensor off-setpoint (the 0°C incident)
                if (self.state.camera_cooling_on
                        and self.state.camera_temp_c is not None
                        and abs(self.state.camera_temp_c - self.config.camera_setpoint_c)
                        > self.config.cooling_tolerance_c):
                    await self._escalate(
                        "cooling",
                        f"Sensor at {self.state.camera_temp_c:.1f}C with cooler on — "
                        f"setpoint is {self.config.camera_setpoint_c:.1f}C",
                    )
                await self._cooling_watchdog(camera)
                await self._dew_heater_watchdog(camera)
                await self._safety_monitor_watchdog()

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

    async def _on_guiding_update(self, metrics: GuidingMetrics):
        """Called when PHD2 reports updated guiding metrics."""
        self.state.guiding = metrics

        # Check for tracking issues
        if metrics.rms_total_arcsec > self.config.quality_tracking_rms_max:
            logger.warning(
                "Guiding RMS %.2f\" exceeds threshold %.2f\"",
                metrics.rms_total_arcsec,
                self.config.quality_tracking_rms_max,
            )
            await self._escalate(
                f"rms-{datetime.utcnow():%Y%m%d%H}",  # re-alert at most hourly
                f"Guide RMS {metrics.rms_total_arcsec:.2f}\" over threshold "
                f"{self.config.quality_tracking_rms_max:.2f}\"",
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
        if not target_name:
            tok = stem.split("_")[0]
            if not _re.match(r"^\d{4}-\d{2}-\d{2}$", tok):
                target_name = tok
        if not target_name:
            # NINA knows the active target even when it doesn't stamp OBJECT —
            # the poll loop keeps it in state.current_target (the 2026-09-11
            # donut night logged 11 subs as '?' with a live target the whole
            # time). Trust it as the primary fallback.
            live = str(getattr(self.state, "current_target", "") or "").strip()
            if live and live != "?":
                target_name = live
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

        # Validate image quality
        quality = validate_image(str(file_path), self.config)

        # Add tracking RMS from current guiding — but an unguided rig has
        # PHD2 idling with junk RMS; only judge it when actively guiding
        quality.tracking_rms_arcsec = self.state.guiding.rms_total_arcsec
        _guiding_active = str(getattr(self.state.guiding, "state", "")
                              ).lower() in ("guiding", "settling")
        if _guiding_active and \
                quality.tracking_rms_arcsec > self.config.quality_tracking_rms_max:
            quality.passed_qa = False
            if quality.rejection_reason:
                quality.rejection_reason += "; "
            quality.rejection_reason += (
                f"Tracking RMS {quality.tracking_rms_arcsec:.2f}\" > "
                f"{self.config.quality_tracking_rms_max}\""
            )

        # Sensor far above setpoint at capture = cooler-failure sub. Dark
        # current at +30..40C swamps the signal and the dark library can't
        # match it — reject outright (2026-07-03..05 lesson).
        _t = self.state.camera_temp_c
        if _t is not None and _t > self.config.camera_setpoint_c + 5.0:
            quality.passed_qa = False
            if quality.rejection_reason:
                quality.rejection_reason += "; "
            quality.rejection_reason += (
                f"sensor {_t:.1f}C vs setpoint "
                f"{self.config.camera_setpoint_c:.0f}C (cooler failure)")

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
            watch = Path(self.config.image_watch_dir)
            rel = file_path.relative_to(watch)
            night = rel.parts[0] if rel.parts and                 rel.parts[0][:2] == "20" else datetime.utcnow().strftime("%Y-%m-%d")
            rel_in_night = str(Path(*rel.parts[1:])) if len(rel.parts) > 1                 else file_path.name
            append_sub_record(self.config, night, {
                "file": rel_in_night, "abs_path": str(file_path),
                "time": datetime.utcnow().isoformat() + "Z",
                "target": target_name, "filter": filter_type.value,
                "exp_s": exposure_seconds,
                "ccd_temp": self.state.camera_temp_c,
                "hfr": quality.hfr_pixels, "fwhm_arcsec": quality.fwhm_arcsec,
                "stars": quality.star_count, "ecc": quality.eccentricity,
                "background": quality.background_adu,
                "passed_qa": quality.passed_qa,
                "reason": quality.rejection_reason,
            })
        except Exception as e:  # noqa: BLE001
            logger.debug("Sub record append failed: %s", e)

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
