"""A fake PHD2 event server for offline tests (PS-91 / PS-92, reused by the
calibration manager, settings audit (PS-89: exposure durations, algorithm
params, Dec guide mode, search region, profiles) and auto-tune tickets).

It speaks PHD2's real wire protocol (JSON-RPC requests and replies plus the
event stream on one TCP socket, CRLF-terminated JSON lines), so the real
telescope_agent.phd2_client.PHD2Client is exercised end to end:

    fake = FakePHD2(tmp_path, field=SimField.default())
    port = await fake.start()
    client = PHD2Client("127.0.0.1", port, config=cfg)
    ...
    await fake.close()

The simulated sky is a SimField: stars that move with the mount (pointing
offset) and hot pixels fixed to the sensor. guide_pulse moves the field by
rate x ms (scaled per axis by a response factor: 0 = a dead mount, 1.41 =
the 09-26 guide-rate mismatch) along an RA axis at `ra_axis_deg` in the image;
pulses are refused while guiding, as PHD2 does. save_image writes the current
frame as FITS. find_star picks the brightest object (star OR hot pixel) in the
ROI, which is exactly how PHD2's auto-select locks onto a hot pixel.
"""
from __future__ import annotations

import asyncio
import base64
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np


@dataclass
class SimField:
    width: int = 160
    height: int = 120
    stars: list = field(default_factory=list)   # (x, y, peak ADU, sigma px)
    hot: list = field(default_factory=list)     # (x, y, ADU), sensor fixed
    bias: float = 500.0
    noise: float = 8.0
    offset: list = field(default_factory=lambda: [0.0, 0.0])  # mount, px
    seed: int = 7

    @classmethod
    def default(cls, n_stars: int = 8, hot: bool = True, **kw) -> "SimField":
        rng = np.random.default_rng(3)
        stars = []
        for _ in range(n_stars):
            stars.append((float(rng.uniform(30, kw.get("width", 160) - 30)),
                          float(rng.uniform(25, kw.get("height", 120) - 25)),
                          float(rng.uniform(3000, 12000)), 1.6))
        hp = [(20.0, 15.0, 30000.0), (130.0, 100.0, 25000.0)] if hot else []
        return cls(stars=stars, hot=hp, **kw)

    def render(self, frame_no: int = 0) -> np.ndarray:
        rng = np.random.default_rng(self.seed + frame_no)
        img = rng.normal(self.bias, self.noise, (self.height, self.width))
        yy, xx = np.mgrid[0:self.height, 0:self.width]
        ox, oy = self.offset
        for (x, y, peak, sig) in self.stars:
            cx, cy = x + ox, y + oy
            if -10 < cx < self.width + 10 and -10 < cy < self.height + 10:
                img += peak * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2)
                                     / (2 * sig * sig))
        for (x, y, v) in self.hot:
            img[int(y), int(x)] = v
        return np.clip(img, 0, 65535).astype(np.uint16)

    def objects(self) -> list[tuple]:
        """(x, y, brightness, kind) of every star and hot pixel, now."""
        ox, oy = self.offset
        out = [(x + ox, y + oy, peak, "star") for (x, y, peak, _s) in self.stars]
        out += [(float(x), float(y), v, "hot") for (x, y, v) in self.hot]
        return out


class FakePHD2:
    def __init__(self, tmpdir, field: SimField | None = None, *,
                 ra_px_s: float = 20.0, dec_px_s: float = 20.0,
                 ra_axis_deg: float = 0.0, ra_response: float = 1.0,
                 dec_response: float = 1.0,
                 drift_px_frame: tuple = (0.0, 0.0),
                 exposure_s: float = 0.02, frames: bool = True,
                 binning: int = 2, pixel_scale: float = 0.255,
                 app_state: str = "Stopped", calibration: dict | None = None,
                 refuse: set | None = None, camera: str = "GP678C",
                 gain: int = 100, cal_outcomes: list | None = None,
                 profile: str = "Primary RC Profile (Guider)",
                 algo: dict | None = None, search_region: int = 15,
                 dec_guide_mode: str = "Auto",
                 durations: list | None = None):
        self.tmpdir = Path(tmpdir)
        self.field = field or SimField.default()
        self.ra_px_s, self.dec_px_s = ra_px_s, dec_px_s
        self.ra_axis_deg = ra_axis_deg
        self.response = {"ra": ra_response, "dec": dec_response}
        self.drift = drift_px_frame
        self.exposure_s = exposure_s
        self.frames_enabled = frames
        self.binning = binning
        self.pixel_scale = pixel_scale
        self.app_state = app_state
        self.calibration = calibration or {
            "calibrated": True, "xAngle": ra_axis_deg, "xRate": ra_px_s,
            "xParity": "+", "yAngle": ra_axis_deg + 90.0, "yRate": dec_px_s,
            "yParity": "+", "declination": 0.0}
        self.refuse = set(refuse or ())
        self.camera, self.gain = camera, gain
        # PS-93: guide(recalibrate=True) runs a simulated calibration; each
        # outcome is calibration data (a dict -> CalibrationComplete) or a
        # failure reason (a str -> CalibrationFailed). Empty = complete with
        # the current calibration.
        self.cal_outcomes = list(cal_outcomes or [])
        self.profile = profile
        # PS-89 settings audit: the API-readable / settable guiding settings.
        # guide_exposure_ms is what get/set_exposure report (exposure_s stays
        # the simulated frame cadence).
        self.algo = algo or {
            "ra": {"algorithmName": "Lowpass2", "Aggressiveness": 55.0,
                   "MinMove": 0.76},
            "dec": {"algorithmName": "Lowpass2", "Aggressiveness": 50.0,
                    "MinMove": 0.76}}
        self.search_region = search_region
        self.dec_guide_mode = dec_guide_mode
        self.durations = list(durations or [500, 1000, 1500, 2000, 2500, 3000,
                                            3500, 4000, 4500, 5000, 6000])
        self.guide_exposure_ms: int | None = None
        self.calibrations = 0
        self.lock: tuple | None = None
        self.settling = False
        self.frame_no = 0
        self.requests: list[dict] = []
        self.pulses: list[tuple] = []
        self.saved: list[str] = []
        self.writers: list = []
        self.server = None
        self._task = None

    # -- server -------------------------------------------------------------

    async def start(self) -> int:
        self.tmpdir.mkdir(parents=True, exist_ok=True)
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self._task = asyncio.create_task(self._frames())
        return self.server.sockets[0].getsockname()[1]

    async def close(self):
        if self._task:
            self._task.cancel()
        for w in self.writers:
            try:
                w.close()
            except Exception:  # noqa: BLE001
                pass
        if self.server:
            self.server.close()
            await self.server.wait_closed()

    async def _send(self, w, obj):
        try:
            w.write((json.dumps(obj) + "\r\n").encode())
            await w.drain()
        except Exception:  # noqa: BLE001 - client went away
            pass

    async def push(self, obj: dict):
        for w in list(self.writers):
            await self._send(w, obj)

    async def _handle(self, reader, writer):
        self.writers.append(writer)
        await self._send(writer, {"Event": "Version", "PHDVersion": "2.6.13",
                                  "MsgVersion": 1})
        await self._send(writer, {"Event": "AppState", "State": self.app_state})
        while True:
            try:
                line = await reader.readline()
            except Exception:  # noqa: BLE001
                break
            if not line:
                break
            req = json.loads(line)
            self.requests.append(req)
            try:
                result = await self._rpc(req["method"], req.get("params") or [])
                await self._send(writer, {"jsonrpc": "2.0", "id": req["id"],
                                          "result": result})
            except Exception as e:  # noqa: BLE001
                await self._send(writer, {"jsonrpc": "2.0", "id": req["id"],
                                          "error": {"code": 1, "message": str(e)}})
        if writer in self.writers:
            self.writers.remove(writer)
        try:
            writer.close()   # server.wait_closed() waits for every connection
        except Exception:  # noqa: BLE001
            pass

    @staticmethod
    def _axis(a) -> str:
        return "ra" if str(a).lower() in ("ra", "x") else "dec"

    def methods(self) -> list[str]:
        return [r["method"] for r in self.requests]

    # -- simulation ---------------------------------------------------------

    def _move(self, axis: str, sign: float, px: float):
        a = math.radians(self.ra_axis_deg)
        u = (math.cos(a), math.sin(a)) if axis == "ra" else (-math.sin(a), math.cos(a))
        self.field.offset[0] += sign * px * u[0]
        self.field.offset[1] += sign * px * u[1]

    def _locked_kind(self):
        if self.lock is None:
            return None
        best = min(self.field.objects(),
                   key=lambda o: math.hypot(o[0] - self.lock[0], o[1] - self.lock[1]))
        return best[3] if math.hypot(best[0] - self.lock[0],
                                     best[1] - self.lock[1]) < 3 else None

    async def _frames(self):
        while True:
            await asyncio.sleep(self.exposure_s)
            if not self.frames_enabled:
                continue
            self.field.offset[0] += self.drift[0]
            self.field.offset[1] += self.drift[1]
            if self.app_state == "Looping":
                self.frame_no += 1
                await self.push({"Event": "LoopingExposures", "Frame": self.frame_no})
            elif self.app_state == "Guiding":
                self.frame_no += 1
                kind = self._locked_kind()
                await self.push({
                    "Event": "GuideStep", "Frame": self.frame_no, "Time": 0.0,
                    "Mount": "Mount", "RADistanceRaw": 0.01, "DECDistanceRaw": -0.01,
                    "RADuration": 0, "RADirection": "West", "DECDuration": 0,
                    "DECDirection": "North", "StarMass": 5000, "SNR": 40.0,
                    "HFD": 0.6 if kind == "hot" else 3.8, "ErrorCode": 0})
                if self.settling and self.frame_no % 3 == 0:
                    self.settling = False
                    await self.push({"Event": "SettleDone", "Status": 0,
                                     "TotalFrames": 3, "DroppedFrames": 0})

    def _pick(self, roi=None):
        x0, y0, w, h = roi if roi else (8, 8, self.field.width - 16,
                                        self.field.height - 16)
        cands = [o for o in self.field.objects()
                 if x0 <= o[0] < x0 + w and y0 <= o[1] < y0 + h]
        if not cands:
            return None
        return max(cands, key=lambda o: o[2])

    def _crop(self, size=15):
        img = self.field.render(self.frame_no)
        cx, cy = int(round(self.lock[0])), int(round(self.lock[1]))
        r = size // 2
        x0, y0 = max(0, cx - r), max(0, cy - r)
        crop = img[y0:cy + r + 1, x0:cx + r + 1]
        return crop, (cx - x0, cy - y0)

    async def _calibrate(self):
        """PS-93: StartCalibration, Calibrating steps West then North, then
        CalibrationComplete (guiding follows) or CalibrationFailed."""
        self.calibrations += 1
        await asyncio.sleep(0.01)
        await self.push({"Event": "StartCalibration", "Mount": "Mount"})
        for d in ("West", "East", "North", "South"):
            for i in range(1, 13):
                await self.push({"Event": "Calibrating", "Mount": "Mount", "dir": d,
                                 "dist": 25.0 * i / 12, "dx": 0.0, "dy": 0.0,
                                 "pos": [0, 0], "step": i, "State": d})
        out = self.cal_outcomes.pop(0) if self.cal_outcomes else dict(self.calibration)
        if self.app_state != "Calibrating":
            return                                  # stopped meanwhile
        if isinstance(out, str):
            self.app_state = "Stopped"
            await self.push({"Event": "CalibrationFailed", "Reason": out})
            return
        self.calibration = dict(out, calibrated=True)
        await self.push({"Event": "CalibrationComplete", "Mount": "Mount"})
        self.app_state = "Guiding"
        self.settling = True
        await self.push({"Event": "StartGuiding"})

    async def _rpc(self, method: str, params):
        if method in self.refuse:
            raise RuntimeError(f"{method} refused")
        if method == "get_app_state":
            return self.app_state
        if method == "get_pixel_scale":
            return self.pixel_scale
        if method == "get_camera_binning":
            return self.binning
        if method == "get_exposure":
            if self.guide_exposure_ms is not None:
                return self.guide_exposure_ms
            return int(self.exposure_s * 1000)
        if method == "set_exposure":
            ms = int(params[0])
            if ms not in self.durations:
                raise RuntimeError(f"invalid exposure {ms}")
            self.guide_exposure_ms = ms
            return 0
        if method == "get_exposure_durations":
            return list(self.durations)
        if method == "get_search_region":
            return self.search_region
        if method == "get_algo_param_names":
            return list(self.algo[self._axis(params[0])])
        if method == "get_algo_param":
            ax = self.algo[self._axis(params[0])]
            if params[1] not in ax:
                raise RuntimeError(f"could not get param {params[1]}")
            return ax[params[1]]
        if method == "set_algo_param":
            ax = self.algo[self._axis(params[0])]
            if params[1] not in ax or params[1] == "algorithmName":
                raise RuntimeError(f"could not set param {params[1]}")
            ax[params[1]] = float(params[2])
            return 0
        if method == "get_dec_guide_mode":
            return self.dec_guide_mode
        if method == "set_dec_guide_mode":
            if params[0] not in ("Off", "Auto", "North", "South"):
                raise RuntimeError("invalid dec guide mode")
            self.dec_guide_mode = params[0]
            return 0
        if method == "get_profiles":
            return [{"id": 1, "name": self.profile, "selected": True}]
        if method == "get_variable_delay_settings":
            return {"Enabled": False, "ShortDelaySeconds": 0, "LongDelaySeconds": 0}
        if method == "get_connected":
            return True
        if method == "get_current_equipment":
            return {"camera": {"name": self.camera, "connected": True},
                    "mount": {"name": "ASCOM.SoftwareBisque", "connected": True}}
        if method == "get_calibration_data":
            return self.calibration
        if method == "get_profile":
            return {"id": 1, "name": self.profile}
        if method == "loop":
            if self.app_state in ("Guiding", "Calibrating"):
                raise RuntimeError("cannot loop while guiding")
            self.app_state = "Looping"
            return 0
        if method == "stop_capture":
            was = self.app_state
            self.app_state = "Stopped"
            self.settling = False
            if was == "Guiding":
                await self.push({"Event": "GuidingStopped"})
            await self.push({"Event": "LoopingExposuresStopped"})
            return 0
        if method == "find_star":
            roi = params[0] if params else None
            o = self._pick(roi)
            if o is None:
                raise RuntimeError("could not find a star")
            self.lock = (o[0], o[1])
            await self.push({"Event": "StarSelected", "X": o[0], "Y": o[1]})
            await self.push({"Event": "LockPositionSet", "X": o[0], "Y": o[1]})
            return [o[0], o[1]]
        if method == "set_lock_position":
            self.lock = (float(params[0]), float(params[1]))
            await self.push({"Event": "LockPositionSet", "X": self.lock[0],
                             "Y": self.lock[1]})
            return 0
        if method == "get_lock_position":
            return list(self.lock) if self.lock else None
        if method == "get_star_image":
            if self.lock is None:
                raise RuntimeError("no star selected")
            size = int(params[0]) if params else 15
            crop, pos = self._crop(max(15, size))
            return {"frame": self.frame_no, "width": int(crop.shape[1]),
                    "height": int(crop.shape[0]), "star_pos": list(pos),
                    "pixels": base64.b64encode(crop.astype("<u2").tobytes()).decode()}
        if method == "save_image":
            from astropy.io import fits
            img = self.field.render(self.frame_no)
            p = self.tmpdir / f"phd2_frame_{len(self.saved):04d}.fits"
            hdr = fits.Header()
            hdr["XBINNING"] = self.binning
            hdr["GAIN"] = self.gain
            hdr["EXPOSURE"] = self.exposure_s
            fits.PrimaryHDU(img, header=hdr).writeto(p, overwrite=True)
            self.saved.append(str(p))
            return {"filename": str(p)}
        if method == "guide_pulse":
            ms, d = int(params[0]), str(params[1]).upper()[:1]
            if self.app_state in ("Guiding", "Calibrating"):
                raise RuntimeError("cannot issue guide pulse while guiding")
            self.pulses.append((ms, d))
            axis = "ra" if d in ("W", "E") else "dec"
            rate = self.ra_px_s if axis == "ra" else self.dec_px_s
            sign = 1.0 if d in ("W", "N") else -1.0
            self._move(axis, sign, rate * self.response[axis] * ms / 1000.0)
            return 0
        if method == "guide":
            if self.app_state not in ("Stopped", "Looping"):
                raise RuntimeError(f"cannot start guiding while {self.app_state}")
            if self.lock is None:
                o = self._pick()
                if o is None:
                    raise RuntimeError("no star")
                self.lock = (o[0], o[1])
                await self.push({"Event": "StarSelected", "X": o[0], "Y": o[1]})
                await self.push({"Event": "LockPositionSet", "X": o[0], "Y": o[1]})
            recal = len(params) > 1 and bool(params[1])
            if recal or not self.calibration.get("calibrated"):
                self.app_state = "Calibrating"
                asyncio.get_running_loop().create_task(self._calibrate())
                return 0
            self.app_state = "Guiding"
            self.settling = True
            await self.push({"Event": "StartGuiding"})
            await self.push({"Event": "SettleBegin"})
            return 0
        if method == "dither":
            await self.push({"Event": "GuidingDithered", "dx": 1.0, "dy": 1.0})
            return 0
        raise RuntimeError(f"unknown method {method}")
