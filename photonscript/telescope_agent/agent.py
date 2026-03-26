"""Telescope Agent — runs on the Windows telescope PC.

Monitors NINA and PHD2, validates captured images, and reports status
back to the scheduler via the message bus.

This agent is designed to run on the Windows computer that controls
the telescope mount, camera, and guiding equipment.
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

    async def _nina_poll_loop(self):
        """Poll NINA for equipment state every few seconds."""
        while self._running:
            try:
                # Get camera info
                camera = await self.nina.get_camera_info()
                self.state.camera_temp_c = camera.get("Temperature")
                self.state.camera_cooling_on = camera.get("CoolerOn", False)

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

    async def _file_watch_loop(self):
        """Watch the image output directory for new FITS/TIFF files.

        Uses polling on Windows since watchdog may need additional setup.
        """
        seen_files: set[str] = set()

        # Initialize with existing files
        if self._watch_dir.exists():
            for f in self._watch_dir.iterdir():
                if f.suffix.lower() in (".fits", ".fit", ".tif", ".tiff", ".xisf"):
                    seen_files.add(str(f))

        while self._running:
            try:
                if not self._watch_dir.exists():
                    await asyncio.sleep(10)
                    continue

                for f in self._watch_dir.iterdir():
                    if f.suffix.lower() not in (".fits", ".fit", ".tif", ".tiff", ".xisf"):
                        continue
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
        # Example: M31_Ha_300s_Gain139_001.fits
        parts = file_path.stem.split("_")
        target_name = parts[0] if len(parts) > 0 else "Unknown"
        filter_str = parts[1] if len(parts) > 1 else "L"
        exposure_str = parts[2] if len(parts) > 2 else "300s"

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
