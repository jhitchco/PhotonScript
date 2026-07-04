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
        logger.info("New image detected: %s", file_path.name)

        # Parse filename for metadata (NINA naming convention)
        # Example: M31_Ha_300s_Gain200_001.fits
        parts = file_path.stem.split("_")
        target_name = parts[0] if len(parts) > 0 else "Unknown"
        filter_str = parts[1] if len(parts) > 1 else "L"
        exposure_str = parts[2] if len(parts) > 2 else "300s"

        # Filenames carry the NINA profile filter name (e.g. 'H'); translate
        filter_str = self.config.reverse_filter_map().get(filter_str, filter_str)
        try:
            filter_type = FilterType(filter_str)
        except ValueError:
            filter_type = FilterType.LUMINANCE

        try:
            exposure_seconds = float(exposure_str.rstrip("s"))
        except ValueError:
            exposure_seconds = 300.0

        # Validate image quality
        quality = validate_image(str(file_path), self.config)

        # Add tracking RMS from current guiding
        quality.tracking_rms_arcsec = self.state.guiding.rms_total_arcsec
        if quality.tracking_rms_arcsec > self.config.quality_tracking_rms_max:
            quality.passed_qa = False
            if quality.rejection_reason:
                quality.rejection_reason += "; "
            quality.rejection_reason += (
                f"Tracking RMS {quality.tracking_rms_arcsec:.2f}\" > "
                f"{self.config.quality_tracking_rms_max}\""
            )

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
