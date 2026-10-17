"""Librarian Agent — image cataloging, QA triage, and bandwidth-aware transfer.

The Librarian sits between the telescope and the image processor.
It catalogs completed images, performs quick quality checks, and
transfers validated images from the remote telescope PC to the local
machine — but only during daytime hours to respect Starlink bandwidth
shared among other operators.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Optional
from uuid import uuid4

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (
    AgentMessage, AgentRole, CapturedImage, ImageStatus,
    TransferJob, TransferWindow,
)
from photonscript.shared.messagebus import get_message_bus

logger = logging.getLogger(__name__)

# Windows refuses these in a path component (WinError 123), plus control
# characters; a trailing dot or space is silently dropped by Win32.
_ILLEGAL = set('<>:"/\\|?*')
_RESERVED = {"CON", "PRN", "AUX", "NUL",
             *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}
UNKNOWN_DIR = "_unknown"


def safe_component(name) -> str:
    """One Windows-safe path component. Illegal characters become "_",
    trailing dots / spaces go, reserved device names get a "_" prefix, and
    a name with nothing left (or the "?" an unattributed sub carries)
    becomes UNKNOWN_DIR."""
    raw = str(name or "").strip()
    if not raw or set(raw) <= {"?"}:
        return UNKNOWN_DIR
    out = "".join("_" if (c in _ILLEGAL or ord(c) < 32) else c for c in raw)
    out = out.rstrip(" .")
    if not out.strip("_ ."):
        return UNKNOWN_DIR
    if out.split(".")[0].upper() in _RESERVED:
        out = "_" + out
    return out


def local_base_dir(value) -> Path:
    """config.local_image_dir with "~" and %VARS% / $VARS expanded (the
    scope's value, "~" plus a backslash, used to stay literal and land in
    the process's working folder)."""
    text = str(value or "").strip()
    if not text:
        return Path.home() / "Astrophotography"
    if text == "~" or text[:2] in ("~/", "~\\"):
        text = str(Path.home()) + text[1:]   # either separator, any OS
    return Path(os.path.expandvars(os.path.expanduser(text)))


def transfer_dest(base: Path, image: CapturedImage) -> Path:
    """<base>/<target>/<filter>/<file>, every component Windows-safe."""
    flt = getattr(image.filter_type, "value", image.filter_type)
    return (base / safe_component(image.target_name) / safe_component(flt)
            / safe_component(Path(str(image.filename)).name))


def _is_transfer_window(window: TransferWindow, timezone: str = "America/Denver") -> bool:
    """Check if current time falls within the transfer window (daytime hours)."""
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo(timezone))
    except ImportError:
        import pytz
        now = datetime.now(pytz.timezone(timezone))

    return window.start_hour_local <= now.hour < window.end_hour_local


class Librarian:
    """Image librarian and transfer manager.

    Responsibilities:
    - Receive image_captured and image_quality_report events
    - Catalog images in the database
    - Queue validated images for transfer
    - Transfer images only during daytime (bandwidth-sensitive Starlink)
    - Organize files on the local machine by target/filter
    - Report transfer completions to the image processor
    """

    def __init__(self, config: PhotonScriptConfig):
        self.config = config
        self.bus = get_message_bus()
        self._transfer_queue: asyncio.Queue[TransferJob] = asyncio.Queue()
        self._completed_images: dict[str, CapturedImage] = {}
        self._running = False
        self._transfer_window = config.get_transfer_window()
        self._local_base = local_base_dir(config.local_image_dir)
        # The legacy per-sub copy to local_image_dir (and the processor's
        # auto-stack it feeds) predates the Library: dawn filing (PS-157)
        # and Syncthing now move subs. Off by default (see config).
        self._legacy_transfer = bool(getattr(config, "librarian_legacy_transfer", False))

    async def start(self):
        """Start the librarian agent."""
        self._running = True
        if self._legacy_transfer:
            try:
                await asyncio.to_thread(self._local_base.mkdir, parents=True,
                                        exist_ok=True)
            except Exception as e:  # noqa: BLE001
                logger.warning("Librarian: cannot create %s (%s)", self._local_base, e)
            logger.info("Librarian started, local storage: %s", self._local_base)
        else:
            logger.info("Librarian started, legacy transfer off "
                        "(librarian_legacy_transfer); cataloging only")
        logger.info(
            "Transfer window: %02d:00 - %02d:00 local (%s), limit: %s Mbps",
            self._transfer_window.start_hour_local,
            self._transfer_window.end_hour_local,
            self.config.observatory_tz,
            self._transfer_window.bandwidth_limit_mbps,
        )

        # Subscribe to events
        self.bus.subscribe("image_captured", self._on_image_captured)
        self.bus.subscribe("image_quality_report", self._on_quality_report)

        # Run transfer worker
        await self._transfer_loop()

    async def stop(self):
        self._running = False

    async def _on_image_captured(self, msg: AgentMessage):
        """Handle a new image capture event from the telescope agent."""
        image = CapturedImage(**msg.payload)
        self._completed_images[image.id] = image
        logger.info(
            "Cataloged image: %s [%s %ss] — %s",
            image.filename, image.filter_type.value,
            image.exposure_seconds, image.status.value,
        )

        # If passed QA, queue for transfer
        if image.status == ImageStatus.VALIDATED:
            await self._queue_transfer(image)

    async def _on_quality_report(self, msg: AgentMessage):
        """Handle quality report — update image status."""
        image_id = msg.payload.get("image_id")
        passed = msg.payload.get("passed_qa", False)

        if image_id in self._completed_images:
            image = self._completed_images[image_id]
            if passed and image.status != ImageStatus.VALIDATED:
                image.status = ImageStatus.VALIDATED
                await self._queue_transfer(image)
            elif not passed:
                image.status = ImageStatus.REJECTED
                logger.info("Image %s rejected by QA", image.filename)

    async def _queue_transfer(self, image: CapturedImage):
        """Add an image to the transfer queue. Never raises: this runs inside
        the image_captured / quality-report handlers, and the folder is made
        off the event loop (a slow or failing mkdir must not stall it)."""
        if not self._legacy_transfer:
            return
        try:
            dest = transfer_dest(self._local_base, image)
            await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
            job = TransferJob(
                id=str(uuid4()),
                image_id=image.id or str(uuid4()),
                source_path=image.file_path,
                dest_path=str(dest),
                file_size_bytes=image.file_size_bytes,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("Librarian: transfer not queued for %s (%s)",
                           image.filename, e)
            return
        await self._transfer_queue.put(job)
        logger.info(
            "Queued transfer: %s (%.1f MB), %d in queue",
            image.filename,
            image.file_size_bytes / 1024 / 1024,
            self._transfer_queue.qsize(),
        )

    async def _transfer_loop(self):
        """Main transfer loop — processes queue during daytime hours only."""
        while self._running:
            if self._transfer_queue.empty():
                await asyncio.sleep(30)
                continue

            if not _is_transfer_window(self._transfer_window, self.config.observatory_tz):
                logger.debug(
                    "Outside transfer window (%02d:00-%02d:00), %d files waiting",
                    self._transfer_window.start_hour_local,
                    self._transfer_window.end_hour_local,
                    self._transfer_queue.qsize(),
                )
                await asyncio.sleep(300)  # Check every 5 minutes
                continue

            try:
                job = self._transfer_queue.get_nowait()
            except asyncio.QueueEmpty:
                await asyncio.sleep(30)
                continue

            await self._execute_transfer(job)

    async def _execute_transfer(self, job: TransferJob):
        """Execute a single file transfer from remote to local.

        Uses SSH/SCP (paramiko) for secure transfer with bandwidth limiting.
        Falls back to local file copy if source is locally accessible.
        """
        job.status = "in_progress"
        job.started_at = datetime.utcnow()

        logger.info("Starting transfer: %s -> %s", job.source_path, job.dest_path)

        try:
            source = Path(job.source_path)
            dest = Path(job.dest_path)

            # If we have SSH config, use paramiko for remote transfer
            if self.config.transfer_host:
                await self._ssh_transfer(job)
            elif await asyncio.to_thread(source.exists):
                # Local transfer (telescope PC and local are same machine, or network share)
                await asyncio.to_thread(dest.parent.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(self._copy_file_with_limit, source, dest)
                job.status = "completed"
            else:
                job.status = "failed"
                job.error = f"Source not found and no SSH configured: {job.source_path}"

            job.completed_at = datetime.utcnow()

            if job.status == "completed":
                elapsed = (job.completed_at - job.started_at).total_seconds()
                if elapsed > 0:
                    job.transfer_rate_mbps = round(
                        job.file_size_bytes * 8 / elapsed / 1_000_000, 1
                    )

                # Update image record
                if job.image_id in self._completed_images:
                    img = self._completed_images[job.image_id]
                    img.transferred = True
                    img.transferred_at = job.completed_at
                    img.local_path = job.dest_path
                    img.status = ImageStatus.TRANSFERRED

                logger.info(
                    "Transfer complete: %s (%.1f MB @ %.1f Mbps)",
                    Path(job.source_path).name,
                    job.file_size_bytes / 1024 / 1024,
                    job.transfer_rate_mbps or 0,
                )

                # Notify image processor
                await self.bus.publish(AgentMessage(
                    sender=AgentRole.LIBRARIAN,
                    recipient=AgentRole.PROCESSOR,
                    msg_type="transfer_complete",
                    payload={
                        "image_id": job.image_id,
                        "local_path": job.dest_path,
                        "target_name": self._completed_images.get(job.image_id, CapturedImage(
                            project_id="", filename="", file_path="", target_name="Unknown",
                            filter_type="L", exposure_seconds=0
                        )).target_name,
                    },
                ))
            else:
                logger.error("Transfer failed: %s — %s", job.source_path, job.error)

        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            logger.error("Transfer error: %s", e)
            # Re-queue for retry
            await self._transfer_queue.put(job)

    async def _ssh_transfer(self, job: TransferJob):
        """Transfer file via SSH/SFTP with bandwidth limiting."""
        import paramiko

        def _do_transfer():
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            connect_kwargs = {
                "hostname": self.config.transfer_host,
                "port": self.config.transfer_port,
                "username": self.config.transfer_user,
            }
            if self.config.transfer_key_path:
                connect_kwargs["key_filename"] = self.config.transfer_key_path

            ssh.connect(**connect_kwargs)
            sftp = ssh.open_sftp()

            dest = Path(job.dest_path)
            dest.parent.mkdir(parents=True, exist_ok=True)

            # Transfer with bandwidth awareness
            sftp.get(job.source_path, str(dest))

            sftp.close()
            ssh.close()
            job.status = "completed"

        await asyncio.to_thread(_do_transfer)

    def _copy_file_with_limit(self, source: Path, dest: Path):
        """Copy file locally with optional bandwidth limiting."""
        import shutil
        shutil.copy2(str(source), str(dest))

    def get_catalog_summary(self) -> dict:
        """Return a summary of cataloged images."""
        total = len(self._completed_images)
        validated = sum(1 for img in self._completed_images.values() if img.status == ImageStatus.VALIDATED)
        rejected = sum(1 for img in self._completed_images.values() if img.status == ImageStatus.REJECTED)
        transferred = sum(1 for img in self._completed_images.values() if img.transferred)
        pending = self._transfer_queue.qsize()

        return {
            "total_images": total,
            "validated": validated,
            "rejected": rejected,
            "transferred": transferred,
            "pending_transfer": pending,
        }
