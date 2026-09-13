"""PHD2 Server API client — monitors guiding performance.

PHD2 exposes a JSON-RPC over TCP socket (default port 4400).
This client connects and monitors guiding metrics in real-time.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional, Callable, Awaitable

from photonscript.shared.models import GuidingMetrics, GuidingState

logger = logging.getLogger(__name__)


class PHD2Client:
    """Async client for PHD2's event-driven server API.

    PHD2 sends JSON events over a TCP socket. We connect, listen for
    events, and maintain a snapshot of current guiding performance.
    """

    def __init__(self, host: str = "localhost", port: int = 4400):
        from collections import deque
        self._px_scale = 1.0          # arcsec/px from get_pixel_scale RPC
        self._ra_hist = deque(maxlen=120)   # ~last 4-6 min of guide steps
        self._dec_hist = deque(maxlen=120)
        self.host = host
        self.port = port
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._metrics = GuidingMetrics()
        self._rpc_id = 0
        self._listeners: list[Callable[[GuidingMetrics], Awaitable[None]]] = []
        self._running = False

    @property
    def metrics(self) -> GuidingMetrics:
        return self._metrics

    def on_update(self, callback: Callable[[GuidingMetrics], Awaitable[None]]):
        self._listeners.append(callback)

    async def connect(self) -> bool:
        try:
            self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
            self._connected = True
            logger.info("Connected to PHD2 at %s:%d", self.host, self.port)
            return True
        except Exception as e:
            logger.warning("Cannot connect to PHD2 at %s:%d: %s", self.host, self.port, e)
            self._connected = False
            return False

    async def _send_rpc(self, method: str, params: list | None = None) -> dict:
        if not self._connected or not self._writer:
            return {}
        self._rpc_id += 1
        msg = {"method": method, "id": self._rpc_id}
        if params:
            msg["params"] = params
        data = json.dumps(msg) + "\r\n"
        self._writer.write(data.encode())
        await self._writer.drain()
        return msg

    async def refresh_pixel_scale(self):
        """PHD2 GuideStep distances are in guide-camera PIXELS; convert with
        the profile's pixel scale so RMS numbers are honest arcseconds."""
        try:
            r = await self._send_rpc("get_pixel_scale")
            scale = float(r.get("result") or 0)
            if scale > 0:
                self._px_scale = scale
        except Exception:  # noqa: BLE001
            pass

    async def get_app_state(self) -> str:
        """Query PHD2 application state (Stopped, Guiding, etc.)."""
        await self._send_rpc("get_app_state")
        return self._metrics.state.value

    async def start_guiding(self, settle_pixels: float = 1.5, settle_time: int = 10, settle_timeout: int = 60):
        """Start guiding with settle parameters."""
        await self._send_rpc("guide", [
            {"pixels": settle_pixels, "time": settle_time, "timeout": settle_timeout},
            False,  # recalibrate
        ])

    async def stop_guiding(self):
        await self._send_rpc("stop_capture")

    async def dither(self, amount: float = 5.0, settle_pixels: float = 1.5, settle_time: int = 10):
        """Dither the guide star."""
        await self._send_rpc("dither", [
            amount,
            False,  # raOnly
            {"pixels": settle_pixels, "time": settle_time, "timeout": 60},
        ])

    async def run_event_loop(self):
        """Listen for PHD2 events and update metrics continuously."""
        self._running = True
        while self._running and self._connected:
            try:
                line = await asyncio.wait_for(self._reader.readline(), timeout=5.0)
                if not line:
                    logger.warning("PHD2 connection lost")
                    self._connected = False
                    break

                event = json.loads(line.decode().strip())
                await self._handle_event(event)

            except asyncio.TimeoutError:
                continue
            except json.JSONDecodeError:
                continue
            except Exception as e:
                logger.error("PHD2 event loop error: %s", e)
                await asyncio.sleep(1)

    async def _handle_event(self, event: dict):
        """Process a PHD2 server event."""
        event_type = event.get("Event", event.get("jsonrpc", ""))

        if event_type == "GuideStep":
            # rolling true RMS in arcsec over the recent history window
            ra = event.get("RADistanceRaw", 0.0) * self._px_scale
            dec = event.get("DECDistanceRaw", 0.0) * self._px_scale
            self._ra_hist.append(ra)
            self._dec_hist.append(dec)

            def _rms(h):
                return (sum(x * x for x in h) / len(h)) ** 0.5 if h else 0.0

            self._metrics.rms_ra_arcsec = _rms(self._ra_hist)
            self._metrics.rms_dec_arcsec = _rms(self._dec_hist)
            self._metrics.rms_total_arcsec = (
                self._metrics.rms_ra_arcsec ** 2
                + self._metrics.rms_dec_arcsec ** 2) ** 0.5
            self._metrics.peak_ra_arcsec = max(abs(x) for x in self._ra_hist)
            self._metrics.peak_dec_arcsec = max(abs(x) for x in self._dec_hist)
            self._metrics.snr = event.get("SNR", 0)
            self._metrics.star_mass = event.get("StarMass", 0)
            self._metrics.state = GuidingState.GUIDING

        elif event_type == "Settling":
            self._metrics.state = GuidingState.SETTLING

        elif event_type == "SettleDone":
            self._metrics.state = GuidingState.GUIDING

        elif event_type == "StarLost":
            self._metrics.state = GuidingState.LOST_STAR

        elif event_type == "GuidingStopped":
            self._metrics.state = GuidingState.STOPPED

        elif event_type == "Calibrating":
            self._metrics.state = GuidingState.CALIBRATING

        elif event_type == "StartGuiding":
            self._ra_hist.clear()
            self._dec_hist.clear()
            self._metrics.state = GuidingState.GUIDING

        elif event_type == "AppState":
            state_map = {
                "Stopped": GuidingState.STOPPED,
                "Guiding": GuidingState.GUIDING,
                "Calibrating": GuidingState.CALIBRATING,
                "LostLock": GuidingState.LOST_STAR,
            }
            app_state = event.get("State", "Stopped")
            self._metrics.state = state_map.get(app_state, GuidingState.STOPPED)

        # Notify listeners
        for listener in self._listeners:
            try:
                await listener(self._metrics)
            except Exception:
                logger.exception("PHD2 listener error")

    async def disconnect(self):
        self._running = False
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._connected = False
