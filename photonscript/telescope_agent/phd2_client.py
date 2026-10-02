"""PHD2 Server API client — monitors guiding performance.

PHD2 exposes a JSON-RPC over TCP socket (default port 4400).
This client connects and monitors guiding metrics in real-time.

PS-91: it also keeps a ring buffer (about 30 min) of full guide frames in the
shared.guide_motion frame shape, tracks the lock position and lock epoch,
counts looping frames, forwards every raw event to on_event() listeners, and
wraps the PHD2 RPCs PhotonScript commands (loop, find_star, save_image,
guide_pulse, ...) through call(), so an error reply raises PHD2RPCError
instead of vanishing. Only one PhotonScript actor should command PHD2 at a
time: hold telescope_agent.phd2_ops.hold(owner) around a command sequence.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Optional, Callable, Awaitable

from photonscript.shared.models import GuidingMetrics, GuidingState

logger = logging.getLogger(__name__)

# arcsec per pixel = 206.265 * pixel size (um) / focal length (mm)
_ARCSEC_K = 206.265
# PHD2's get_pixel_scale answers this when the profile has no focal length or
# pixel size (it then reports distances in pixels); never trust it as a scale.
_UNKNOWN_SCALE = 1.0
# Relative disagreement between PHD2's profile scale and the config optics that
# is worth a warning (e.g. a 600 mm profile left selected on the OAG).
_SCALE_MISMATCH = 0.25
_CONNECT_WARN_EVERY_S = 1800.0
# PS-91 ring buffer of guide frames: keep this many seconds (and at most
# FRAME_BUFFER_MAX frames, about 30 min at 0.5 s exposures)
FRAME_BUFFER_S = 1800.0
FRAME_BUFFER_MAX = 3600
# PHD2 event direction words -> the guide-log letters guide_motion expects
_DIR = {"east": "E", "west": "W", "north": "N", "south": "S",
        "e": "E", "w": "W", "n": "N", "s": "S"}
# AppState names PHD2 reports (get_app_state / AppState event)
APP_STATES = ("Stopped", "Selected", "Calibrating", "Guiding", "LostLock",
              "Paused", "Looping")


def _fnum(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def frame_from_event(event: dict, *, t: float, settling: bool, epoch: int,
                     output: bool = True) -> dict:
    """One GuideStep (guided) or StarLost (dropped) event as a guide frame in
    the shared.guide_motion / phd2_logs._frame shape, plus HFD and the PHD2
    frame number. Distances are raw guide-camera pixels; t is unix seconds."""
    lost = event.get("Event") == "StarLost"
    code = int(_fnum(event.get("ErrorCode"), 0))
    snr, mass = event.get("SNR"), event.get("StarMass")
    return {"n": int(_fnum(event.get("Frame"), 0)), "t": float(t),
            "ra": None if lost else _fnum(event.get("RADistanceRaw")),
            "dec": None if lost else _fnum(event.get("DECDistanceRaw")),
            "ra_ms": 0.0 if lost else _fnum(event.get("RADuration")),
            "ra_dir": _DIR.get(str(event.get("RADirection") or "").lower(), ""),
            "dec_ms": 0.0 if lost else _fnum(event.get("DECDuration")),
            "dec_dir": _DIR.get(str(event.get("DECDirection") or "").lower(), ""),
            "hfd": None if lost else (_fnum(event.get("HFD"), 0.0) or None),
            "snr": _fnum(snr) if snr is not None else None,
            "mass": _fnum(mass) if mass is not None else None,
            "code": code, "drop": lost,
            "reason": (str(event.get("Status") or "star lost") if lost else None),
            "settling": bool(settling), "epoch": int(epoch), "output": bool(output)}


def guide_rms_text(m) -> str:
    """Honest one-line guide RMS for logs, Pushover and the CLI: arcsec when
    the guide pixel scale is known (with the pixel figure and scale behind
    it), otherwise guide-camera pixels, labelled as such."""
    if getattr(m, "units", "arcsec") == "arcsec" and m.rms_total_arcsec is not None:
        txt = (f"{m.rms_total_arcsec:.2f}\" (RA {m.rms_ra_arcsec:.2f}\", "
               f"Dec {m.rms_dec_arcsec:.2f}\"")
        if m.pixel_scale_arcsec:
            txt += (f"; {m.rms_total_px:.1f} guide px at "
                    f"{m.pixel_scale_arcsec:.3f}\"/px from {m.scale_source}")
        return txt + ")"
    return (f"{m.rms_total_px:.2f} guide px (RA {m.rms_ra_px:.2f}, Dec "
            f"{m.rms_dec_px:.2f} px; guide pixel scale unknown, not arcsec)")


class PHD2RPCError(RuntimeError):
    """PHD2 answered a JSON-RPC request with an error object."""


def guide_focal_length_mm(config) -> float:
    """Guide-path focal length (mm). The OAG sees through the RC16, so unless
    guide_focal_length_mm is set it is derived from the imaging plate scale:
    206.265 * imaging pixel (um) / pixel_scale_arcsec (0.24"/px at 3.76 um is
    about 3230 mm)."""
    fl = float(getattr(config, "guide_focal_length_mm", 0) or 0)
    if fl > 0:
        return fl
    scale = float(getattr(config, "pixel_scale_arcsec", 0) or 0)
    ipx = float(getattr(config, "imaging_camera_pixel_um", 0) or 0)
    if scale > 0 and ipx > 0:
        return _ARCSEC_K * ipx / scale
    return 0.0


def guide_scale_from_config(config, binning: int | None = 1) -> float | None:
    """Guide-camera arcsec/px from config, or None when it cannot be known.

    phd2_pixel_scale_arcsec (if set) wins as-is; otherwise
    206.265 * guide_camera_pixel_um * binning / guide focal length."""
    if config is None:
        return None
    explicit = float(getattr(config, "phd2_pixel_scale_arcsec", 0) or 0)
    if explicit > 0:
        return explicit
    px = float(getattr(config, "guide_camera_pixel_um", 0) or 0)
    fl = guide_focal_length_mm(config)
    if px <= 0 or fl <= 0:
        return None
    return _ARCSEC_K * px * max(1, int(binning or 1)) / fl


class PHD2Client:
    """Async client for PHD2's event-driven server API.

    PHD2 sends JSON events over a TCP socket. We connect, listen for
    events, and maintain a snapshot of current guiding performance.
    """

    def __init__(self, host: str = "localhost", port: int = 4400, config=None):
        # PS-70: guide-camera arcsec/px. None = unknown, and then RMS stays
        # in guide-camera PIXELS and is labelled that way (units="px").
        self._px_scale: float | None = None
        self._scale_source: str | None = None  # "phd2" | "config" | None
        self._binning: int | None = None
        self._scale_mismatch: str | None = None
        self._config = config
        self._ra_hist = deque(maxlen=120)   # RAW pixels, last ~4-6 min of steps
        self._dec_hist = deque(maxlen=120)
        self._pending: dict[int, asyncio.Future] = {}
        self._loop_active = False     # run_event_loop() is reading the socket
        self._connect_warned_at: float | None = None
        self.host = host
        self.port = port
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._connected = False
        self._metrics = GuidingMetrics()
        self._rpc_id = 0
        self._listeners: list[Callable[[GuidingMetrics], Awaitable[None]]] = []
        self._running = False
        # PS-91: full guide frames, lock tracking, raw event listeners
        self._frames: deque = deque(maxlen=FRAME_BUFFER_MAX)
        self._event_listeners: list[Callable[[dict], Awaitable[None]]] = []
        self._settling = False
        self.lock_epoch = 0
        self.lock_position: tuple[float, float] | None = None
        self.star_selected: tuple[float, float] | None = None
        self.app_state: str = "Unknown"   # PHD2's own AppState name
        self.frame_count = 0              # looping + guide frames seen
        self.last_event_at: float | None = None

    @property
    def metrics(self) -> GuidingMetrics:
        return self._metrics

    def on_update(self, callback: Callable[[GuidingMetrics], Awaitable[None]]):
        self._listeners.append(callback)

    def on_event(self, callback: Callable[[dict], Awaitable[None]]):
        """PS-91: every raw PHD2 event, after the client has processed it.
        Listeners run inside the socket reader: never await call() from one
        (schedule a task instead)."""
        self._event_listeners.append(callback)

    @property
    def pixel_scale(self) -> float | None:
        return self._px_scale

    @property
    def binning(self) -> int | None:
        return self._binning

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def settling(self) -> bool:
        return self._settling

    def recent_frames(self, seconds: float | None = None,
                      now: float | None = None) -> list[dict]:
        """Guide frames from the ring buffer, oldest first; the last
        `seconds` only when given."""
        frames = list(self._frames)
        if seconds is None:
            return frames
        now = time.time() if now is None else now
        return [f for f in frames if f["t"] >= now - seconds]

    def clear_frames(self) -> None:
        self._frames.clear()

    async def wait_frames(self, n: int = 1, timeout: float = 30.0,
                          poll_s: float = 0.05) -> bool:
        """Wait until n more looping/guide frames have arrived (needs
        run_event_loop running). False on timeout."""
        target = self.frame_count + max(1, int(n))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while self.frame_count < target:
            if loop.time() >= deadline or not self._connected:
                return False
            await asyncio.sleep(poll_s)
        return True

    async def connect(self) -> bool:
        try:
            self._reader, self._writer = await asyncio.open_connection(self.host, self.port)
            self._connected = True
            logger.info("Connected to PHD2 at %s:%d", self.host, self.port)
            self._connect_warned_at = None
            return True
        except Exception as e:
            # PS-70: PHD2 not running is the normal daytime state; one WARNING
            # per outage (then every 30 min), not one per 14 s retry.
            now = time.monotonic()
            if (self._connect_warned_at is None
                    or now - self._connect_warned_at >= _CONNECT_WARN_EVERY_S):
                self._connect_warned_at = now
                logger.warning("Cannot connect to PHD2 at %s:%d: %s (retrying "
                               "quietly)", self.host, self.port, e)
            else:
                logger.debug("Cannot connect to PHD2 at %s:%d: %s",
                             self.host, self.port, e)
            self._connected = False
            return False

    async def call(self, method: str, params: list | None = None,
                   timeout: float = 5.0):
        """Send a JSON-RPC request and return PHD2's ``result``.

        PHD2 answers on the same socket as its event stream, so the reply is
        matched by id. While run_event_loop() owns the socket the reply is
        handed over through a future; before that (right after connect) this
        reads the stream itself, dispatching any events it passes. Raises
        PHD2RPCError on an error reply, asyncio.TimeoutError on no reply."""
        if not self._connected or not self._writer:
            raise ConnectionError("PHD2 not connected")
        loop = asyncio.get_running_loop()
        self._rpc_id += 1
        rid = self._rpc_id
        fut = loop.create_future()
        self._pending[rid] = fut
        msg = {"method": method, "id": rid}
        if params:
            msg["params"] = params
        try:
            self._writer.write((json.dumps(msg) + "\r\n").encode())
            await self._writer.drain()
            if self._loop_active:
                return await asyncio.wait_for(fut, timeout)
            deadline = loop.time() + timeout
            while not fut.done():
                left = deadline - loop.time()
                if left <= 0:
                    raise TimeoutError(f"no reply to {method}")
                line = await asyncio.wait_for(self._reader.readline(), left)
                if not line:
                    self._connected = False
                    raise ConnectionError("PHD2 connection lost")
                try:
                    obj = json.loads(line.decode().strip())
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                await self._dispatch(obj)
            return fut.result()
        finally:
            self._pending.pop(rid, None)

    async def _dispatch(self, obj: dict):
        """Route one line from PHD2: an RPC reply resolves its waiting call,
        anything else is an event."""
        if (isinstance(obj, dict) and "Event" not in obj and "id" in obj
                and ("result" in obj or "error" in obj)):
            fut = self._pending.get(obj.get("id"))
            if fut is not None and not fut.done():
                if obj.get("error") is not None:
                    err = obj["error"]
                    fut.set_exception(PHD2RPCError(
                        err.get("message") if isinstance(err, dict) else str(err)))
                else:
                    fut.set_result(obj.get("result"))
            return
        if isinstance(obj, dict):
            await self._handle_event(obj)

    async def refresh_pixel_scale(self):
        """PHD2 GuideStep distances are in guide-camera PIXELS. Learn the
        arcsec/px so RMS numbers are honest arcseconds (PS-70).

        1. PHD2's own get_pixel_scale (the profile's focal length, pixel size
           and binning). PHD2 answers null (or 1.0) when the profile lacks a
           focal length or pixel size: that is "unknown", not a scale.
        2. Otherwise config: guide_camera_pixel_um x PHD2's camera binning /
           guide focal length (see guide_scale_from_config).
        3. Otherwise unknown: RMS is reported in pixels and labelled "px".
        """
        phd2_scale = None
        try:
            r = await self.call("get_pixel_scale")
            v = float(r) if r is not None else 0.0
            if v > 0 and abs(v - _UNKNOWN_SCALE) > 1e-9:
                phd2_scale = v
        except Exception as e:  # noqa: BLE001
            logger.debug("PHD2 get_pixel_scale failed: %s", e)
        binning = None
        try:
            b = await self.call("get_camera_binning")
            binning = int(b) if b else None
        except Exception as e:  # noqa: BLE001
            logger.debug("PHD2 get_camera_binning failed: %s", e)
        cfg_scale = guide_scale_from_config(self._config, binning or 1)
        self._binning = binning
        self._scale_mismatch = None
        if phd2_scale:
            self._set_scale(phd2_scale, "phd2")
            if cfg_scale and abs(phd2_scale - cfg_scale) / cfg_scale > _SCALE_MISMATCH:
                self._scale_mismatch = (
                    f"PHD2 profile says {phd2_scale:.3f}\"/px but the configured "
                    f"optics give {cfg_scale:.3f}\"/px (bin {binning or 1}); check "
                    "the PHD2 profile focal length / guide camera")
                logger.warning("PHD2 pixel scale mismatch: %s", self._scale_mismatch)
        elif cfg_scale:
            self._set_scale(cfg_scale, "config")
            logger.info("PHD2 did not report a pixel scale; using %.3f\"/px "
                        "from config (bin %s)", cfg_scale, binning or 1)
        else:
            self._set_scale(None, None)
            logger.warning("PHD2 pixel scale unknown (no PHD2 focal length and "
                           "no guide_camera_pixel_um / focal length in config): "
                           "guide RMS is reported in guide-camera pixels")

    def _set_scale(self, scale: float | None, source: str | None):
        self._px_scale = scale
        self._scale_source = source
        self._recompute()

    async def get_app_state(self) -> str:
        """PHD2's application state name (Stopped, Selected, Calibrating,
        Guiding, LostLock, Paused, Looping), asked live. Before PS-91 this
        sent the request and returned the cached metrics state without
        reading the reply. Falls back to the last AppState seen (or
        "Unknown") when PHD2 does not answer."""
        try:
            r = await self.call("get_app_state")
            if r:
                self.app_state = str(r)
        except Exception as e:  # noqa: BLE001
            logger.debug("PHD2 get_app_state failed: %s", e)
        return self.app_state

    # -- PS-91 RPC wrappers (all through call(): errors raise PHD2RPCError) --

    async def loop(self):
        """Start looping exposures (no guiding)."""
        return await self.call("loop")

    async def stop_capture(self):
        """Stop looping and guiding."""
        return await self.call("stop_capture")

    async def find_star(self, roi: list | tuple | None = None):
        """Auto-select a star, inside roi = [x, y, width, height] when given.
        Returns PHD2's lock position [x, y]."""
        return await self.call("find_star", [list(roi)] if roi else None,
                               timeout=15.0)

    async def set_lock_position(self, x: float, y: float, exact: bool = True):
        return await self.call("set_lock_position", [float(x), float(y),
                                                     bool(exact)])

    async def get_lock_position(self):
        return await self.call("get_lock_position")

    async def get_star_image(self, size: int | None = None) -> dict:
        """The guide-star crop: {frame, width, height, star_pos, pixels}
        (pixels = base64 of 16-bit little-endian values)."""
        return await self.call("get_star_image", [int(size)] if size else None,
                               timeout=10.0)

    async def save_image(self) -> str:
        """Save PHD2's current frame as FITS; returns the file name (in
        PHD2's temp folder: the caller deletes it when done)."""
        r = await self.call("save_image", timeout=15.0)
        return (r or {}).get("filename", "") if isinstance(r, dict) else str(r or "")

    async def get_calibration_data(self, which: str = "Mount") -> dict:
        """{calibrated, xAngle, xRate (px/s), xParity, yAngle, yRate,
        yParity, declination} of the stored calibration."""
        return await self.call("get_calibration_data", [which]) or {}

    async def get_exposure(self) -> int | None:
        """Guide exposure in ms."""
        r = await self.call("get_exposure")
        return int(r) if r is not None else None

    async def get_camera_binning(self) -> int | None:
        r = await self.call("get_camera_binning")
        return int(r) if r else None

    async def get_current_equipment(self) -> dict:
        return await self.call("get_current_equipment") or {}

    async def guide_pulse(self, amount_ms: int, direction: str,
                          which: str = "Mount"):
        """One manual guide pulse (direction N/S/E/W or the full word)."""
        d = _DIR.get(str(direction).strip().lower())
        if d is None:
            raise ValueError(f"bad guide direction {direction!r}")
        return await self.call("guide_pulse", [int(amount_ms), d, which])

    async def start_guiding(self, settle_pixels: float = 1.5, settle_time: int = 10,
                            settle_timeout: int = 60, recalibrate: bool = False):
        """Start guiding with settle parameters.

        recalibrate=True forces PHD2 to drop stored calibration and recalibrate
        before guiding — the fix when settles keep timing out because the
        calibration no longer matches the sky (e.g. after switching to the OAG
        guide path or a fresh polar/TPoint change). Note: the nightly sequence
        drives PHD2 through NINA's StartGuiding (see nina_sequence_json.py), so
        its settle criteria come from the NINA guider profile, not this client;
        this path is the agent's direct control. PS-91: through call(), so a
        refused start raises PHD2RPCError instead of vanishing.
        """
        return await self.call("guide", [
            {"pixels": settle_pixels, "time": settle_time, "timeout": settle_timeout},
            recalibrate,
        ])

    async def stop_guiding(self):
        return await self.stop_capture()

    async def dither(self, amount: float = 5.0, settle_pixels: float = 1.5, settle_time: int = 10):
        """Dither the guide star."""
        return await self.call("dither", [
            amount,
            False,  # raOnly
            {"pixels": settle_pixels, "time": settle_time, "timeout": 60},
        ])

    async def run_event_loop(self):
        """Listen for PHD2 events (and RPC replies) and update metrics."""
        self._running = True
        self._loop_active = True
        try:
            while self._running and self._connected:
                try:
                    line = await asyncio.wait_for(self._reader.readline(), timeout=5.0)
                    if not line:
                        logger.warning("PHD2 connection lost")
                        self._connected = False
                        break

                    event = json.loads(line.decode().strip())
                    await self._dispatch(event)

                except asyncio.TimeoutError:
                    continue
                except json.JSONDecodeError:
                    continue
                except Exception as e:
                    logger.error("PHD2 event loop error: %s", e)
                    await asyncio.sleep(1)
        finally:
            self._loop_active = False
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("PHD2 event loop ended"))

    def _recompute(self):
        """Rolling RMS over the recent history, in pixels always and in arcsec
        when the scale is known (PS-70)."""
        m = self._metrics

        def _rms(h):
            return (sum(x * x for x in h) / len(h)) ** 0.5 if h else 0.0

        ra_px, dec_px = _rms(self._ra_hist), _rms(self._dec_hist)
        tot_px = (ra_px ** 2 + dec_px ** 2) ** 0.5
        m.rms_ra_px, m.rms_dec_px, m.rms_total_px = ra_px, dec_px, tot_px
        m.samples = len(self._ra_hist)
        m.pixel_scale_arcsec = self._px_scale
        m.scale_source = self._scale_source
        m.guide_binning = self._binning
        m.scale_warning = self._scale_mismatch
        pk_ra = max((abs(x) for x in self._ra_hist), default=0.0)
        pk_dec = max((abs(x) for x in self._dec_hist), default=0.0)
        if self._px_scale:
            k = self._px_scale
            m.units = "arcsec"
            m.rms_ra_arcsec, m.rms_dec_arcsec = ra_px * k, dec_px * k
            m.rms_total_arcsec = tot_px * k
            m.peak_ra_arcsec, m.peak_dec_arcsec = pk_ra * k, pk_dec * k
        else:
            m.units = "px"
            m.rms_ra_arcsec = m.rms_dec_arcsec = m.rms_total_arcsec = None
            m.peak_ra_arcsec = m.peak_dec_arcsec = None

    def _track(self, event_type: str, event: dict) -> None:
        """PS-91: ring buffer, lock position / epoch, app state, frame count."""
        now = _fnum(event.get("Timestamp"), 0.0) or time.time()
        self.last_event_at = now
        if event_type in ("GuideStep", "StarLost"):
            self.frame_count += 1
            self._frames.append(frame_from_event(
                event, t=now, settling=self._settling, epoch=self.lock_epoch))
            while self._frames and self._frames[0]["t"] < now - FRAME_BUFFER_S:
                self._frames.popleft()
            if event_type == "GuideStep":
                self.app_state = "Guiding"
            elif self.app_state == "Guiding":
                self.app_state = "LostLock"
        elif event_type == "LoopingExposures":
            self.frame_count += 1
            if self.app_state not in ("Guiding", "Calibrating"):
                self.app_state = "Looping"
        elif event_type == "LoopingExposuresStopped":
            self.app_state = "Stopped"
        elif event_type in ("LockPositionSet", "StarSelected"):
            xy = (_fnum(event.get("X")), _fnum(event.get("Y")))
            if event_type == "LockPositionSet":
                self.lock_position = xy
            else:
                self.star_selected = xy
            self.lock_epoch += 1
        elif event_type == "LockPositionLost":
            self.lock_position = None
        elif event_type in ("GuidingDithered", "StartGuiding"):
            self.lock_epoch += 1
            if event_type == "StartGuiding":
                self.app_state = "Guiding"
        elif event_type in ("SettleBegin", "Settling"):
            self._settling = True
        elif event_type == "SettleDone":
            self._settling = False
        elif event_type == "StartCalibration":
            self.app_state = "Calibrating"
        elif event_type == "GuidingStopped":
            self.app_state = "Stopped"
            self._settling = False
        elif event_type == "Paused":
            self.app_state = "Paused"
        elif event_type == "AppState":
            self.app_state = str(event.get("State") or self.app_state)

    async def _handle_event(self, event: dict):
        """Process a PHD2 server event."""
        event_type = event.get("Event", event.get("jsonrpc", ""))
        try:
            self._track(event_type, event)
        except Exception:  # noqa: BLE001 - never lose metrics over the buffer
            logger.exception("PHD2 event tracking error")

        if event_type == "GuideStep":
            # PHD2 reports RADistanceRaw / DECDistanceRaw in guide-camera
            # PIXELS; keep pixels in the history and scale in _recompute()
            self._ra_hist.append(float(event.get("RADistanceRaw") or 0.0))
            self._dec_hist.append(float(event.get("DECDistanceRaw") or 0.0))
            self._recompute()
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
            self._recompute()
            self._metrics.state = GuidingState.GUIDING

        elif event_type == "ConfigurationChange":
            # profile / camera / binning may have changed: re-learn the scale
            # (as a task: call() needs this loop free to read the reply)
            if self._loop_active:
                asyncio.get_running_loop().create_task(self._safe_refresh())

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
        for listener in self._event_listeners:
            try:
                await listener(event)
            except Exception:
                logger.exception("PHD2 event listener error")

    async def _safe_refresh(self):
        try:
            await self.refresh_pixel_scale()
        except Exception as e:  # noqa: BLE001
            logger.debug("PHD2 pixel-scale refresh failed: %s", e)

    async def disconnect(self):
        self._running = False
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._connected = False
