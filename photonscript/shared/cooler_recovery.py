"""Cooler-stall recovery: disconnect and reconnect the camera, then cool.

Jeremy's operating rule: "when it's not cooling after a bit, disconnect and
reconnect the camera". 2026-10-16 both cameras sat at 32 to 35 C with the
cooler effectively off after the evening Cool Camera step failed on NINA #1
and #2 (the PS-61 cooler gate skipped: "sensor 35.0 C vs setpoint 0 C after
20 min"), and a capture job deferred with "cooler stalled at 23.4 C (TEC 0 %)".
Earlier that week the Piggy cooler sat at 0 % power until the camera was
disconnected, reconnected and cooled again by hand.

Who uses it:
  - the telescope agent's cooling watchdog (one per rig), while the armer
    expects cooling: ARMED / RUNNING / PAUSED_* / WATCHING inside the cold
    window (cool_lead_minutes before dusk until dawn, from the armer plan);
  - the calibration capture job while it is "cooling" (armer idle, the
    agent's watchdog stands down then).
The PS-61 cooler gate benefits from both: a recovery during its wait buys
the gate cooler_stall_gate_extend_min more before it skips.

A stall (StallDetector), only while the sensor is above setpoint + tol and
after a full cooler_stall_window_min of samples:
  - cooler OFF for the whole window, or
  - cooler ON with TEC power below cooler_stall_power_pct for the whole
    window, or
  - the sensor fell less than cooler_stall_drop_c over the window (unless
    the TEC is flat out, >= cooler_stall_flat_out_pct: a reconnect does not
    help a cooler that is already working as hard as it can).

The recovery never runs while the camera is exposing (IsExposing or a busy
CameraState, read fresh just before acting; unknown = busy). It disconnects
ONLY the camera, waits cooler_stall_reconnect_wait_s, connects it, then
cools to the rig setpoint with the caller's usual duration. At most
cooler_stall_max_attempts per night per rig, at least
cooler_stall_spacing_min apart, counted in <data_dir>/cooler_recovery.json
so the agent and the capture job share one budget across processes. Every
step is logged and written as a night event (src "cooler", kind
"cooler_recovery"); each attempt ends in exactly one Pushover: recovered,
still stalled, or the reconnect errored.

Nothing here touches safety, park, roof or any other device.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

LEDGER_NAME = "cooler_recovery.json"
LEDGER_KEEP_NIGHTS = 7
TITLE = "PhotonScript cooler recovery"
EXPECT_STATES = ("ARMED", "RUNNING", "PAUSED_UNSAFE", "PAUSED_OPERATOR",
                 "WATCHING")
# NINA CameraStates that mean an exposure is in flight (or about to be)
BUSY_STATES = ("exposing", "reading", "download", "downloading", "waiting",
               "liveview")


def settings(config) -> dict:
    """The cooler_stall_* keys with their defaults (bad values fall back)."""
    def f(key, default):
        try:
            v = getattr(config, key, default)
            return float(default if v is None else v)
        except (TypeError, ValueError):
            return float(default)
    return {
        "enabled": bool(getattr(config, "cooler_stall_recovery", True)),
        "window_s": max(60.0, f("cooler_stall_window_min", 8.0) * 60),
        "drop_c": max(0.1, f("cooler_stall_drop_c", 2.0)),
        "power_pct": max(0.0, f("cooler_stall_power_pct", 3.0)),
        "flat_out_pct": f("cooler_stall_flat_out_pct", 90.0),
        "max_attempts": max(0, int(f("cooler_stall_max_attempts", 2))),
        "spacing_s": max(0.0, f("cooler_stall_spacing_min", 15.0) * 60),
        "wait_s": max(0.0, f("cooler_stall_reconnect_wait_s", 5.0)),
        "gate_extend_s": max(0.0, f("cooler_stall_gate_extend_min", 15.0) * 60),
        "tol": f("cooling_tolerance_c", 1.0),
    }


def _num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None   # NaN -> None


def camera_busy(cam: dict | None) -> bool:
    """True when an exposure is (or may be) in flight: IsExposing, a busy
    CameraState, or nothing readable (unknown counts as busy)."""
    if not isinstance(cam, dict):
        return True
    if cam.get("IsExposing") is True:
        return True
    st = str(cam.get("CameraState") or "").strip().lower()
    if st in BUSY_STATES:
        return True
    return "IsExposing" not in cam and not st


# ---------------------------------------------------------------- expectation

def cooling_expected(config, now_utc: datetime | None = None) -> tuple[bool, str]:
    """Does the armer expect the coolers on now? (expected, why). From
    <data_dir>/armer_state.json: a live state and, when the plan has them,
    inside [dusk - cool_lead_minutes, dawn). Without plan times only
    RUNNING / PAUSED_* / WATCHING count (ARMED waits for pre-cool with the
    cooler deliberately off, cooler_off_until_precool)."""
    p = Path(getattr(config, "data_dir", ".")) / "armer_state.json"
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False, "no armer state"
    st = d.get("state")
    if st not in EXPECT_STATES:
        return False, f"armer {st}"
    plan = d.get("plan") or {}
    now = now_utc or datetime.utcnow()
    try:
        dusk = datetime.fromisoformat(str(plan["dusk_utc"]).rstrip("Z"))
        dawn = datetime.fromisoformat(str(plan["dawn_utc"]).rstrip("Z"))
    except Exception:  # noqa: BLE001
        if st == "ARMED":
            return False, "armer ARMED, no plan times"
        return True, f"armer {st}"
    lead = float(getattr(config, "cool_lead_minutes", 30) or 0)
    if now < dusk - timedelta(minutes=lead):
        return False, f"armer {st}, before pre-cool"
    if now >= dawn:
        return False, f"armer {st}, after dawn"
    return True, f"armer {st}, cold window"


# ---------------------------------------------------------------- detection

class StallDetector:
    """Feeds (time, temp, cooler on, power) samples; flags a stall once a
    full window shows no cooling while the sensor is above setpoint + tol."""

    def __init__(self, s: dict):
        self.s = s
        self.trace: list[tuple[float, float, bool, float | None]] = []

    def reset(self) -> None:
        self.trace = []

    def feed(self, cam: dict | None, setpoint: float, now: float) -> str | None:
        cam = cam or {}
        temp = _num(cam.get("Temperature"))
        if temp is None:
            return None
        on = bool(cam.get("CoolerOn", False))
        power = _num(cam.get("CoolerPower"))
        self.trace.append((now, temp, on, power))
        w = self.s["window_s"]
        # keep the newest sample at least a window old (the anchor) + newer
        old = [i for i, x in enumerate(self.trace) if now - x[0] >= w]
        if old:
            self.trace = self.trace[old[-1]:]
        if temp <= setpoint + self.s["tol"]:
            return None
        if not old:
            return None             # not a full window yet
        anchor = self.trace[0]
        win = self.trace[1:] or self.trace
        mins = (now - anchor[0]) / 60
        if not any(x[2] for x in self.trace):
            return (f"cooler OFF at {temp:.1f} C (setpoint {setpoint:g} C) for "
                    f"{mins:.0f} min")
        if all(x[2] and x[3] is not None and x[3] < self.s["power_pct"]
               for x in win):
            return (f"cooler on but TEC {power or 0:.0f} % at {temp:.1f} C "
                    f"(setpoint {setpoint:g} C) for {mins:.0f} min")
        drop = anchor[1] - temp
        if drop < self.s["drop_c"]:
            if on and power is not None and power >= self.s["flat_out_pct"]:
                return None         # flat out: a reconnect will not help
            return (f"sensor {temp:.1f} C fell only {drop:.1f} C in {mins:.0f} "
                    f"min (setpoint {setpoint:g} C, cooler "
                    f"{'on' if on else 'OFF'}, TEC "
                    f"{'?' if power is None else f'{power:.0f}'} %)")
        return None


# ---------------------------------------------------------------- ledger

def _ledger_path(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / LEDGER_NAME


def _night(config, now: float) -> str:
    from photonscript.shared.phd2_store import night_of
    return night_of(config, datetime.utcfromtimestamp(now))


def _read_ledger(config) -> dict:
    try:
        d = json.loads(_ledger_path(config).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_ledger(config, d: dict) -> None:
    try:
        keep = sorted(d)[-LEDGER_KEEP_NIGHTS:]
        p = _ledger_path(config)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({k: d[k] for k in keep}, indent=1),
                     encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        logger.warning("cooler recovery ledger not written: %s", e)


def attempts(config, rig: str, now: float | None = None) -> list[dict]:
    now = time.time() if now is None else now
    return list((_read_ledger(config).get(_night(config, now)) or {}).get(rig) or [])


def may_attempt(config, rig: str, now: float, s: dict) -> tuple[bool, str]:
    done = attempts(config, rig, now)
    if len(done) >= s["max_attempts"]:
        return False, f"{len(done)} of {s['max_attempts']} reconnects used tonight"
    if done:
        ago = now - float(done[-1].get("epoch", 0))
        if ago < s["spacing_s"]:
            return False, (f"last reconnect {ago / 60:.0f} min ago (spacing "
                           f"{s['spacing_s'] / 60:g} min)")
    return True, ""


def _record(config, rig: str, now: float, entry: dict) -> int:
    d = _read_ledger(config)
    night = _night(config, now)
    rows = d.setdefault(night, {}).setdefault(rig, [])
    rows.append(entry)
    _write_ledger(config, d)
    return len(rows)


def _set_result(config, rig: str, epoch: float, result: str) -> None:
    d = _read_ledger(config)
    for row in (d.get(_night(config, epoch)) or {}).get(rig) or []:
        if row.get("epoch") == epoch:
            row["result"] = result
    _write_ledger(config, d)


def recovered_since(config, rig: str, since: float,
                    now: float | None = None) -> bool:
    """True when this rig had a reconnect attempt at or after `since` (the
    cooler gate extends its wait once for it)."""
    return any(float(r.get("epoch", 0)) >= since
               for r in attempts(config, rig, now))


# ---------------------------------------------------------------- NINA port

class NinaCameraPort:
    """The four ninaAPI v2 camera calls the recovery needs (GET, like the
    rest of PhotonScript). base_url ends in /v2/api."""

    def __init__(self, base_url: str, timeout: float = 30.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout

    async def _get(self, path: str, params: dict | None = None) -> dict:
        import httpx
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            r = await client.get(self.base + path, params=params)
            r.raise_for_status()
            data = r.json()
        if isinstance(data, dict) and data.get("Success") is False:
            raise RuntimeError(str(data.get("Error") or "ninaAPI Success false"))
        return data if isinstance(data, dict) else {}

    async def camera(self) -> dict | None:
        try:
            data = await self._get("/equipment/camera/info")
        except Exception as e:  # noqa: BLE001
            logger.warning("cooler recovery camera read (%s): %s", self.base, e)
            return None
        payload = data.get("Response", data)
        return payload if isinstance(payload, dict) else None

    async def disconnect(self) -> None:
        await self._get("/equipment/camera/disconnect")

    async def connect(self) -> None:
        await self._get("/equipment/camera/connect")

    async def cool(self, setpoint: float, minutes: float) -> None:
        await self._get("/equipment/camera/cool",
                        {"temperature": setpoint, "minutes": minutes})


# ---------------------------------------------------------------- the watch

def _event(config, rig: str, now: float, step: str, detail: str) -> None:
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl, iso_z
        dt = datetime.utcfromtimestamp(now)
        append_jsonl(events_path(config, _night(config, now)), {
            "t": iso_z(dt), "rig": rig, "src": "cooler",
            "kind": "cooler_recovery", "value": step, "detail": detail})
    except Exception as e:  # noqa: BLE001
        logger.warning("cooler recovery event not written: %s", e)


class CoolerStallWatch:
    """One rig's stall detection, reconnect and result verdict.

    tick(cam, expected) on every camera poll. recover(reason) runs a
    reconnect directly (the capture job, which has its own stall check).
    The port needs async camera(), disconnect(), connect() and
    cool(setpoint, minutes)."""

    def __init__(self, config, rig: str, port, *, setpoint: float,
                 cool_minutes: float = 0.0, source: str = "watchdog",
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
                 notify_fn=None):
        self.config = config
        self.rig = rig
        self.port = port
        self.setpoint = float(setpoint)
        self.cool_minutes = float(cool_minutes)
        self.source = source
        self.clock = clock
        self.sleep = sleep
        self.notify_fn = notify_fn
        self.s = settings(config)
        self.detector = StallDetector(self.s)
        self.pending: dict | None = None
        self._logged: str | None = None

    # -- helpers --------------------------------------------------------
    def _label(self) -> str:
        try:
            from photonscript.shared.rigs import rig_label
            return rig_label(self.config, self.rig)
        except Exception:  # noqa: BLE001
            return self.rig

    def _log_once(self, msg: str) -> None:
        if msg != self._logged:
            logger.info("cooler recovery [%s]: %s", self.rig, msg)
        self._logged = msg

    async def _notify(self, msg: str, priority: int = 0) -> None:
        fn = self.notify_fn
        if fn is None:
            from photonscript.shared.pushover import notify as fn
        try:
            await fn(self.config, msg, title=TITLE, priority=priority)
        except Exception as e:  # noqa: BLE001
            logger.warning("cooler recovery notify failed: %s", e)

    # -- main entry -----------------------------------------------------
    async def tick(self, cam: dict | None, expected: bool) -> dict | None:
        """Returns a dict describing what happened, or None."""
        if not self.s["enabled"]:
            return None
        now = self.clock()
        if self.pending is not None:
            # keep the detector's window filling, so a "still stalled"
            # verdict can be followed by the next attempt once spacing allows
            self.detector.feed(cam, self.setpoint, now)
            return await self._verify(cam, now, expected)
        if not expected:
            self.detector.reset()
            return None
        reason = self.detector.feed(cam, self.setpoint, now)
        if not reason:
            return None
        return await self.recover(reason, cam)

    async def recover(self, reason: str, cam: dict | None = None) -> dict:
        """One reconnect attempt now, if the budget and the camera allow."""
        now = self.clock()
        if not self.s["enabled"]:
            return {"action": "disabled"}
        ok, why = may_attempt(self.config, self.rig, now, self.s)
        if not ok:
            self._log_once(f"stalled ({reason}); not reconnecting: {why}")
            return {"action": "capped", "why": why, "reason": reason}
        fresh = await self.port.camera()
        pw = _num((fresh or {}).get("CoolerPower"))
        if (fresh or {}).get("CoolerOn") and pw is not None                 and pw >= self.s["flat_out_pct"]:
            self._log_once(f"stalled ({reason}); TEC flat out at {pw:.0f} %, a "
                           "reconnect will not help")
            return {"action": "flat_out", "why": f"TEC flat out ({pw:.0f} %)",
                    "reason": reason}
        if camera_busy(fresh):
            self._log_once(f"stalled ({reason}); camera exposing or unreadable, "
                           "waiting for it to be idle")
            return {"action": "deferred", "reason": reason}
        cam = fresh
        temp0 = _num(cam.get("Temperature"))
        n = _record(self.config, self.rig, now, {
            "epoch": now, "t": datetime.utcfromtimestamp(now).isoformat() + "Z",
            "source": self.source, "reason": reason, "temp_c": temp0,
            "power": _num(cam.get("CoolerPower")), "result": "pending"})
        mx = self.s["max_attempts"]
        logger.warning("cooler recovery [%s] attempt %d/%d (%s): %s",
                       self.rig, n, mx, self.source, reason)
        _event(self.config, self.rig, now, "stall", f"{n}/{mx} {reason}")
        self.detector.reset()
        self._logged = None
        try:
            logger.info("cooler recovery [%s]: disconnecting the camera", self.rig)
            _event(self.config, self.rig, self.clock(), "disconnect", "camera")
            await self.port.disconnect()
            await self.sleep(self.s["wait_s"])
            logger.info("cooler recovery [%s]: connecting the camera", self.rig)
            _event(self.config, self.rig, self.clock(), "connect", "camera")
            await self.port.connect()
            await self.sleep(min(2.0, self.s["wait_s"]))
            logger.info("cooler recovery [%s]: cooling to %g C (%g min)",
                        self.rig, self.setpoint, self.cool_minutes)
            _event(self.config, self.rig, self.clock(), "cool",
                   f"{self.setpoint:g} C over {self.cool_minutes:g} min")
            await self.port.cool(self.setpoint, self.cool_minutes)
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            logger.error("cooler recovery [%s] attempt %d errored: %s",
                         self.rig, n, msg)
            _event(self.config, self.rig, self.clock(), "error", msg)
            _set_result(self.config, self.rig, now, "error")
            await self._notify(
                f"{self._label()}: cooler stalled ({reason}); camera reconnect "
                f"{n}/{mx} errored: {msg}. Check the camera in NINA.", priority=1)
            return {"action": "error", "attempt": n, "detail": msg}
        self.pending = {"epoch": now, "attempt": n, "temp0": temp0,
                        "reason": reason}
        return {"action": "reconnected", "attempt": n, "reason": reason}

    async def conclude(self, cam: dict | None = None) -> dict | None:
        """Give a pending attempt its verdict now (the caller stops
        watching, e.g. a capture job that aborts)."""
        if self.pending is None:
            return None
        if cam is None:
            try:
                cam = await self.port.camera()
            except Exception:  # noqa: BLE001
                cam = None
        return await self._verify(cam, self.clock(), True, force=True)

    async def _verify(self, cam: dict | None, now: float,
                      expected: bool, force: bool = False) -> dict | None:
        p = self.pending
        cam = cam or {}
        temp = _num(cam.get("Temperature"))
        power = _num(cam.get("CoolerPower"))
        on = bool(cam.get("CoolerOn", False))
        sp, tol = self.setpoint, self.s["tol"]
        at_sp = temp is not None and temp <= sp + tol
        due = force or now - p["epoch"] >= self.s["window_s"]
        if not at_sp and not due:
            if not expected and now - p["epoch"] >= 60:
                # the night ended / disarmed while waiting: close quietly
                self.pending = None
                _set_result(self.config, self.rig, p["epoch"], "abandoned")
                _event(self.config, self.rig, now, "abandoned",
                       "cooling no longer expected")
            return None
        t0 = p.get("temp0")
        dropped = (temp is not None and t0 is not None
                   and t0 - temp >= self.s["drop_c"])
        ok = at_sp or (on and power is not None
                       and power >= self.s["power_pct"] and dropped)
        self.pending = None
        n, mx = p["attempt"], self.s["max_attempts"]
        t_txt = "?" if temp is None else f"{temp:.1f}"
        t0_txt = "?" if t0 is None else f"{t0:.1f}"
        p_txt = "?" if power is None else f"{power:.0f}"
        if ok:
            result = "recovered"
            msg = (f"{self._label()}: cooler recovered after camera reconnect "
                   f"{n}/{mx}: sensor {t0_txt} -> {t_txt} C, TEC {p_txt} % "
                   f"(setpoint {sp:g} C).")
            prio = 0
        else:
            result = "still_stalled"
            nxt = (f"next reconnect allowed in {self.s['spacing_s'] / 60:g} min "
                   "if it stays stalled" if n < mx else
                   "no more reconnects tonight; check the camera 12 V / USB")
            msg = (f"{self._label()}: cooler STILL stalled after camera "
                   f"reconnect {n}/{mx}: sensor {t0_txt} -> {t_txt} C, cooler "
                   f"{'on' if on else 'OFF'}, TEC {p_txt} % (setpoint {sp:g} "
                   f"C); {nxt}.")
            prio = 1
        logger.warning("cooler recovery [%s] attempt %d: %s", self.rig, n, result)
        _set_result(self.config, self.rig, p["epoch"], result)
        _event(self.config, self.rig, now, result,
               f"{n}/{mx} sensor {t0_txt} -> {t_txt} C, TEC {p_txt} %")
        await self._notify(msg, priority=prio)
        return {"action": result, "attempt": n}
