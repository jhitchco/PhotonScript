"""NINA Advanced API client — communicates with NINA on the Windows telescope PC.

NINA exposes a REST API (via the Advanced API plugin) that allows external
programs to query equipment state, start/stop sequences, and get image data.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


class NinaClient:
    """Async client for NINA's Advanced API (v2).

    Default endpoint: http://localhost:1888/api
    The NINA Advanced API plugin must be installed and enabled.
    """

    def __init__(self, base_url: str = "http://localhost:1888/api"):
        self.base_url = base_url.rstrip("/")
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=10.0)
        return self._client

    async def _get(self, path: str) -> dict:
        client = await self._get_client()
        resp = await client.get(path)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, json_data: dict | None = None) -> dict:
        client = await self._get_client()
        resp = await client.post(path, json=json_data or {})
        resp.raise_for_status()
        return resp.json()

    # --- Camera control (cooling watchdog) ---

    async def connect_camera(self) -> dict:
        return await self._get("/equipment/camera/connect")

    async def disconnect_camera(self) -> dict:
        return await self._get("/equipment/camera/disconnect")

    async def cool_camera(self, temperature: float, minutes: float = 10.0) -> dict:
        return await self._get(
            f"/equipment/camera/cool?temperature={temperature}&minutes={minutes}")

    # --- Equipment Status ---

    async def get_camera_info(self) -> dict:
        return await self._get("/equipment/camera")

    async def get_mount_info(self) -> dict:
        return await self._get("/equipment/mount")

    async def get_focuser_info(self) -> dict:
        return await self._get("/equipment/focuser")

    async def get_filter_wheel_info(self) -> dict:
        return await self._get("/equipment/filterwheel")

    async def get_rotator_info(self) -> dict:
        return await self._get("/equipment/rotator")

    async def get_guider_info(self) -> dict:
        return await self._get("/equipment/guider")

    # --- Sequence Control ---

    async def get_sequence_status(self) -> dict:
        """Get the current sequence execution status."""
        return await self._get("/sequence")

    async def start_sequence(self) -> dict:
        return await self._post("/sequence/start")

    async def stop_sequence(self) -> dict:
        return await self._post("/sequence/stop")

    async def load_sequence(self, file_path: str) -> dict:
        """Load a sequence file (XML) into NINA."""
        return await self._post("/sequence/load", {"FilePath": file_path})

    # --- Imaging ---

    async def get_image_history(self, count: int = 10) -> list[dict]:
        """Get recent image capture history."""
        result = await self._get(f"/imaging/history?count={count}")
        return result.get("images", [])

    async def get_last_image_stats(self) -> dict:
        """Get statistics from the last captured image."""
        return await self._get("/imaging/last")

    # --- Profile ---

    async def get_profile(self) -> dict:
        return await self._get("/profile")

    # --- Application ---

    async def get_application_status(self) -> dict:
        return await self._get("/application")

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
