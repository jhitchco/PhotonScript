"""NINA Advanced API client — communicates with NINA on the Windows telescope PC.

NINA exposes a REST API (via the Advanced API plugin) that allows external
programs to query equipment state, start/stop sequences, and get image data.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


def _unwrap(body):
    """ninaAPI v2 wraps payloads as {"Response": ..., "Success": ...}."""
    if isinstance(body, dict) and "Response" in body:
        r = body["Response"]
        return r if isinstance(r, (dict, list)) else {}
    return body


def _sequence_status(tree: list) -> dict:
    """Walk a /sequence/json tree: RUNNING if any item is running; the current
    target is the innermost running container under Targets_Container."""
    running = False
    target = None

    def walk(node, under_targets):
        nonlocal running, target
        if not isinstance(node, dict):
            return
        is_running = str(node.get("Status", "")).upper() == "RUNNING"
        if is_running:
            running = True
            if under_targets and node.get("Items") is not None \
                    and node.get("Name") != "Targets_Container":
                target = node.get("Name") or target
        name = node.get("Name", "")
        for child in node.get("Items") or []:
            walk(child, under_targets or name == "Targets_Container")

    for top in tree:
        walk(top, False)
    return {"State": "RUNNING" if running else "IDLE",
            "CurrentTarget": {"Name": target} if target else None}


class NinaClient:
    """Async client for NINA's Advanced API (v2).

    Default endpoint: http://localhost:1888/v2/api (ninaAPI 2.x). Every read
    unwraps the {"Response": ...} envelope. Paths verified against ninaAPI
    2.2.15.2 on the scope PC (2026-09-26).
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

    async def set_dew_heater(self, power: bool) -> dict:
        """Advanced API dew-heater control (window heater on the OGMA)."""
        return await self._get(
            f"/equipment/camera/dew-heater?power={'true' if power else 'false'}")

    async def get_camera_info(self) -> dict:
        return _unwrap(await self._get("/equipment/camera/info"))

    async def get_mount_info(self) -> dict:
        info = _unwrap(await self._get("/equipment/mount/info"))
        if isinstance(info, dict) and "Tracking" not in info:
            info["Tracking"] = info.get("TrackingEnabled", False)  # v2 name
        return info

    async def get_focuser_info(self) -> dict:
        return _unwrap(await self._get("/equipment/focuser/info"))

    async def get_filter_wheel_info(self) -> dict:
        return _unwrap(await self._get("/equipment/filterwheel/info"))

    async def get_rotator_info(self) -> dict:
        return _unwrap(await self._get("/equipment/rotator/info"))

    async def get_guider_info(self) -> dict:
        return _unwrap(await self._get("/equipment/guider/info"))

    async def get_safety_info(self) -> dict:
        """Safety-monitor state (Connected, IsSafe, DeviceId, ...).

        ninaAPI v2 serves this at /equipment/safetymonitor/info and wraps it in
        {"Response": {...}}. Until 2026-09-26 this called the bare
        /equipment/safetymonitor, which 404s: every watchdog poll failed, the
        monitor always looked disconnected, and the watchdog cycled a HEALTHY
        monitor (disconnect/connect every ~20 s) and sent false DISCONNECTED
        alerts all night."""
        return _unwrap(await self._get("/equipment/safetymonitor/info"))

    async def list_safety_devices(self) -> list[dict]:
        """Every safety monitor NINA's chooser offers (Id, DisplayName, ...)."""
        r = _unwrap(await self._get("/equipment/safetymonitor/list-devices"))
        return r if isinstance(r, list) else []

    async def connect_safety(self, device_id: str | None = None) -> dict:
        """Connect the safety monitor. With ``device_id`` (a NINA chooser Id such
        as ASCOM.AlpacaDynamic3.SafetyMonitor) NINA selects THAT device first,
        so a reconnect can never land on a different monitor; without it NINA
        connects whatever its chooser has selected."""
        if device_id:
            client = await self._get_client()
            resp = await client.get("/equipment/safetymonitor/connect",
                                    params={"to": device_id})
            resp.raise_for_status()
            body = resp.json()
            if isinstance(body, dict) and body.get("Success") is False:
                raise RuntimeError(body.get("Error") or "connect failed")
            return body
        return await self._get("/equipment/safetymonitor/connect")

    async def disconnect_safety(self) -> dict:
        return await self._get("/equipment/safetymonitor/disconnect")

    # --- Sequence Control ---

    async def get_sequence_status(self) -> dict:
        """{"State": "RUNNING"|"IDLE", "CurrentTarget": {"Name"}|None}.

        ninaAPI v2 has no status endpoint; derive it from /sequence/json, the
        container tree with a Status per item (CREATED/RUNNING/FINISHED...).
        409 "Sequencer not initialized" = nothing loaded = IDLE."""
        client = await self._get_client()
        resp = await client.get("/sequence/json")
        if resp.status_code == 409:
            return {"State": "IDLE", "CurrentTarget": None}
        resp.raise_for_status()
        tree = _unwrap(resp.json())
        return _sequence_status(tree if isinstance(tree, list) else [])

    async def start_sequence(self) -> dict:
        return await self._get("/sequence/start")

    async def stop_sequence(self) -> dict:
        return await self._get("/sequence/stop")

    async def load_sequence(self, file_path: str) -> dict:
        """Load an Advanced Sequencer JSON file into NINA (POST body = the
        sequence JSON, as the armer does)."""
        import json as _json
        from pathlib import Path as _P
        body = _json.loads(_P(file_path).read_text(encoding="utf-8"))
        return await self._post("/sequence/load", body)

    # --- Imaging ---

    async def get_image_history(self, count: int = 10) -> list[dict]:
        """Recent captures, newest last (ninaAPI /image-history?all=true)."""
        r = _unwrap(await self._get("/image-history?all=true"))
        return r[-count:] if isinstance(r, list) else []

    # --- Profile ---

    async def get_profile(self) -> dict:
        return _unwrap(await self._get("/profile/show"))

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
