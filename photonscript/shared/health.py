"""Service health: event-loop lag, stall stacks, process context (PS-55).

Why this exists: on 2026-09-26 the scheduler API went from 6 s to 30 s+ per
request while the process was "running", and nothing in photonscript.log said
why. This module gives the next incident a direct answer:

- LoopMonitor: a tiny task that sleeps 0.5 s and records how late it woke up
  (event-loop lag). A watchdog THREAD checks the heartbeat; when the loop has
  not ticked for ``stall_s`` it writes the loop thread's current Python stack
  to ``<data_dir>/logs/stalls.log`` (and the service log), which names the
  code that is blocking the loop.
- process_context(): pid, user, Windows session id (0 = scheduled task with
  no desktop), priority class, power throttling (EcoQoS), elevation and the
  launcher tag set by deploy/run-photonscript.ps1.
- apply_process_qos(): best effort on Windows, opt the process out of power
  throttling and lift a below-normal priority/memory priority to normal.
- configure_astropy_iers(): never download IERS data from inside the night
  loop (astropy retries the download on EVERY transform when its cache is
  unwritable and the bundled table is stale).

Everything here is best effort: a failure is logged and ignored, never fatal.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# Filled in by the orchestrator at startup; read by GET /api/health.
_STATE: dict = {
    "mode": None,
    "started_at": None,      # ISO UTC
    "started_mono": None,    # time.monotonic() at start
    "process": {},           # process_context() snapshot at startup
    "qos": {},               # what apply_process_qos() did
    "iers": {},              # configure_astropy_iers() result
}
_MONITOR: Optional["LoopMonitor"] = None


def mark_started(mode: str) -> None:
    _STATE["mode"] = mode
    _STATE["started_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _STATE["started_mono"] = time.monotonic()


def state() -> dict:
    return _STATE


def monitor() -> Optional["LoopMonitor"]:
    return _MONITOR


# ---------------------------------------------------------------------------
# Event-loop lag + stall watchdog
# ---------------------------------------------------------------------------

class LoopMonitor:
    """Measure event-loop lag and catch stalls with the blocking stack.

    ``record()`` and ``check_stall()`` are pure given ``now`` so they are
    unit-tested without a real loop; ``run()`` and ``start_watchdog()`` wire
    them to asyncio and a daemon thread.
    """

    def __init__(self, interval_s: float = 0.5, window_s: float = 300.0,
                 warn_lag_s: float = 2.0, stall_s: float = 5.0,
                 repeat_s: float = 60.0, stall_log: Optional[Path] = None,
                 clock: Callable[[], float] = time.monotonic):
        self.interval_s = interval_s
        self.window_s = window_s
        self.warn_lag_s = warn_lag_s
        self.stall_s = stall_s
        self.repeat_s = repeat_s
        self.stall_log = stall_log
        self.clock = clock
        self.samples: deque = deque()          # (t, lag_s)
        self.last_lag_s = 0.0
        self.heartbeat = clock()
        self.loop_thread_id: Optional[int] = None
        self.stalls: deque = deque()           # t of each stall episode start
        self.last_stall_at: Optional[str] = None
        self._last_warn = -1e18
        self._stall_reported_at: Optional[float] = None
        self._stop = threading.Event()

    # --- pure bookkeeping -------------------------------------------------
    def record(self, now: float, lag_s: float) -> None:
        lag_s = max(0.0, lag_s)
        self.heartbeat = now
        self.last_lag_s = lag_s
        self.samples.append((now, lag_s))
        cutoff = now - self.window_s
        while self.samples and self.samples[0][0] < cutoff:
            self.samples.popleft()
        while self.stalls and self.stalls[0] < cutoff:
            self.stalls.popleft()
        self._stall_reported_at = None   # the loop ticked: episode over
        if lag_s >= self.warn_lag_s and now - self._last_warn >= self.repeat_s:
            self._last_warn = now
            logger.warning("Event loop lag %.1f s (the scheduler API, armer and "
                           "telescope agents all wait while the loop is busy)",
                           lag_s)

    def stats(self, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        lags = [lag for t, lag in self.samples if t >= now - self.window_s]
        return {
            "lag_ms": round(self.last_lag_s * 1000, 1),
            "max_lag_ms_5min": round(max(lags, default=0.0) * 1000, 1),
            "stalls_5min": sum(1 for t in self.stalls if t >= now - self.window_s),
            "last_stall_at": self.last_stall_at,
            "heartbeat_age_s": round(max(0.0, now - self.heartbeat), 1),
        }

    def check_stall(self, now: float,
                    frames: Optional[dict] = None) -> Optional[str]:
        """Called from the watchdog thread. Returns a report (and remembers
        the episode) when the loop has not ticked for ``stall_s``; None
        otherwise. Re-reports every ``repeat_s`` while the stall lasts."""
        age = now - self.heartbeat
        if age < self.stall_s:
            return None
        if self._stall_reported_at is not None and \
                now - self._stall_reported_at < self.repeat_s:
            return None
        first = self._stall_reported_at is None
        self._stall_reported_at = now
        if first:
            self.stalls.append(now)
            self.last_stall_at = datetime.now(timezone.utc).isoformat(
                timespec="seconds")
        stack = "(loop thread unknown)"
        if self.loop_thread_id is not None:
            frames = sys._current_frames() if frames is None else frames
            frame = frames.get(self.loop_thread_id)
            if frame is not None:
                stack = "".join(traceback.format_stack(frame))
        return (f"Event loop STALLED for {age:.1f} s "
                f"({'new' if first else 'still'}), loop thread stack:\n{stack}")

    # --- wiring -------------------------------------------------------------
    async def run(self) -> None:
        self.loop_thread_id = threading.get_ident()
        self.heartbeat = self.clock()
        while True:
            t0 = self.clock()
            await asyncio.sleep(self.interval_s)
            t1 = self.clock()
            self.record(t1, t1 - t0 - self.interval_s)

    def _write_stall(self, report: str) -> None:
        # Plain file append first: if the loop is blocked inside a logging
        # handler the logging lock is held and logger.warning would hang too.
        if self.stall_log is not None:
            try:
                self.stall_log.parent.mkdir(parents=True, exist_ok=True)
                with open(self.stall_log, "a", encoding="utf-8") as fh:
                    fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {report}\n")
            except OSError:
                pass
        logger.warning("%s", report)

    def start_watchdog(self, poll_s: float = 1.0) -> threading.Thread:
        def _loop():
            while not self._stop.wait(poll_s):
                try:
                    report = self.check_stall(self.clock())
                    if report:
                        self._write_stall(report)
                except Exception:  # noqa: BLE001 - never kill the watchdog
                    pass
        th = threading.Thread(target=_loop, name="loop-stall-watchdog",
                              daemon=True)
        th.start()
        return th

    def stop(self) -> None:
        self._stop.set()


def start_monitor(data_dir: Optional[Path]) -> tuple[LoopMonitor, asyncio.Task]:
    """Create the process-wide monitor, start its task and watchdog thread.
    Must be called from inside the running loop."""
    global _MONITOR
    log = Path(data_dir) / "logs" / "stalls.log" if data_dir else None
    mon = LoopMonitor(stall_log=log)
    task = asyncio.create_task(mon.run(), name="loop-monitor")
    mon.start_watchdog()
    _MONITOR = mon
    return mon, task


# ---------------------------------------------------------------------------
# Process context (who/where/how is this process running?)
# ---------------------------------------------------------------------------

_PRIORITY_NAMES = {0x40: "idle", 0x4000: "below_normal", 0x20: "normal",
                   0x8000: "above_normal", 0x80: "high", 0x100: "realtime"}
_NORMAL_PRIORITY_CLASS = 0x20
_ProcessMemoryPriority = 0
_ProcessPowerThrottling = 4
_PT_EXECUTION_SPEED = 0x1
_PT_IGNORE_TIMER_RESOLUTION = 0x4
_MEMORY_PRIORITY_NORMAL = 5


def _win():
    """(kernel32, ctypes) on Windows, else None."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        return k32, ctypes
    except Exception:  # noqa: BLE001
        return None


def _power_throttling_struct(ctypes):
    class PROCESS_POWER_THROTTLING_STATE(ctypes.Structure):
        _fields_ = [("Version", ctypes.c_ulong), ("ControlMask", ctypes.c_ulong),
                    ("StateMask", ctypes.c_ulong)]
    return PROCESS_POWER_THROTTLING_STATE


def _win_details() -> dict:
    w = _win()
    if w is None:
        return {}
    k32, ctypes = w
    out: dict = {}
    try:
        sid = ctypes.c_ulong()
        if k32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):
            out["session_id"] = int(sid.value)
    except Exception:  # noqa: BLE001
        pass
    try:
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        h = k32.GetCurrentProcess()
        cls = k32.GetPriorityClass(ctypes.c_void_p(h))
        out["priority_class"] = _PRIORITY_NAMES.get(cls, hex(cls))
        st = _power_throttling_struct(ctypes)(1, 0, 0)
        if k32.GetProcessInformation(ctypes.c_void_p(h), _ProcessPowerThrottling,
                                     ctypes.byref(st), ctypes.sizeof(st)):
            speed_ctl = bool(st.ControlMask & _PT_EXECUTION_SPEED)
            speed_on = bool(st.StateMask & _PT_EXECUTION_SPEED)
            out["power_throttling"] = ("on" if speed_on else
                                       "off" if speed_ctl else "system-managed")
        mem = ctypes.c_ulong()
        if k32.GetProcessInformation(ctypes.c_void_p(h), _ProcessMemoryPriority,
                                     ctypes.byref(mem), ctypes.sizeof(mem)):
            out["memory_priority"] = int(mem.value)
    except Exception:  # noqa: BLE001
        pass
    try:
        out["elevated"] = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        pass
    return out


def process_context() -> dict:
    """Cheap facts about this process. Non-Windows fields are omitted."""
    try:
        import getpass
        user = getpass.getuser()
    except Exception:  # noqa: BLE001
        user = os.environ.get("USERNAME") or os.environ.get("USER") or "?"
    ctx = {
        "pid": os.getpid(),
        "user": user,
        "launcher": os.environ.get("PS_LAUNCHER", "") or "unknown",
        "cwd": os.getcwd(),
        "python": sys.version.split()[0],
    }
    if os.name != "nt":
        try:
            ctx["elevated"] = os.geteuid() == 0
        except Exception:  # noqa: BLE001
            pass
    ctx.update(_win_details())
    return ctx


def apply_process_qos() -> dict:
    """Windows only, best effort: opt out of power throttling (EcoQoS) and
    raise a below-normal CPU or memory priority to normal. A process started
    by Task Scheduler in session 0 has no window, which is exactly what the
    power-throttling heuristics treat as unimportant background work."""
    w = _win()
    if w is None:
        return {"applied": False, "reason": "not Windows"}
    k32, ctypes = w
    done: dict = {"applied": True}
    try:
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        h = ctypes.c_void_p(k32.GetCurrentProcess())
        mask = _PT_EXECUTION_SPEED | _PT_IGNORE_TIMER_RESOLUTION
        st = _power_throttling_struct(ctypes)(1, mask, 0)
        done["power_throttling_opt_out"] = bool(k32.SetProcessInformation(
            h, _ProcessPowerThrottling, ctypes.byref(st), ctypes.sizeof(st)))
        cls = k32.GetPriorityClass(h)
        if cls in (0x40, 0x4000):   # idle / below normal
            done["priority_raised_from"] = _PRIORITY_NAMES[cls]
            k32.SetPriorityClass(h, _NORMAL_PRIORITY_CLASS)
        mem = ctypes.c_ulong()
        if k32.GetProcessInformation(h, _ProcessMemoryPriority, ctypes.byref(mem),
                                     ctypes.sizeof(mem)) and \
                mem.value < _MEMORY_PRIORITY_NORMAL:
            done["memory_priority_raised_from"] = int(mem.value)
            want = ctypes.c_ulong(_MEMORY_PRIORITY_NORMAL)
            k32.SetProcessInformation(h, _ProcessMemoryPriority,
                                      ctypes.byref(want), ctypes.sizeof(want))
    except Exception as e:  # noqa: BLE001
        done["error"] = str(e)
    return done


# ---------------------------------------------------------------------------
# astropy IERS: never download inside the service
# ---------------------------------------------------------------------------

def configure_astropy_iers(offline: bool = True) -> dict:
    """With ``offline`` (default): use the IERS-A table bundled in the
    installed ``astropy-iers-data`` package, never download, and never treat
    it as too old to use.

    Why: astropy's IERS_Auto re-downloads finals2000A.all (~3.5 MB, 10 s
    timeout per mirror) on every UT1 lookup once the bundled predictions are
    over 30 days old, and when the download cannot be cached (seen
    2026-09-26: "[WinError 5] Access is denied: C:\\Users\\jeremy\\.cache\\
    astropy\\download\\url\\...\\contents") it retries on the next transform
    and then raises. PhotonScript only needs arcsecond-level Alt/Az for
    planning; a stale UT1-UTC prediction costs well under a second of time
    (a few arcsec), so the bundled table is always good enough. Refresh it by
    upgrading ``astropy-iers-data`` in the venv, never from the night loop.
    """
    out: dict = {"offline": bool(offline)}
    try:
        from astropy.utils import iers
        if offline:
            iers.conf.auto_download = False
            iers.conf.auto_max_age = None
        out["auto_download"] = bool(iers.conf.auto_download)
        try:
            import astropy_iers_data
            out["iers_data_version"] = getattr(astropy_iers_data, "__version__", "?")
        except Exception:  # noqa: BLE001
            pass
        try:
            from astropy.time import Time
            pm = iers.IERS_Auto.open().meta.get("predictive_mjd")
            if pm is not None:
                out["predictions_from"] = Time(pm, format="mjd").to_value(
                    "iso", subfmt="date")
        except Exception:  # noqa: BLE001
            pass
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)
    return out


def snapshot(now_mono: Optional[float] = None) -> dict:
    """Health fields owned by this module (GET /api/health adds the rest)."""
    now_mono = time.monotonic() if now_mono is None else now_mono
    started = _STATE.get("started_mono")
    mon = _MONITOR
    return {
        "mode": _STATE.get("mode"),
        "started_at": _STATE.get("started_at"),
        "uptime_s": round(now_mono - started, 1) if started else None,
        "pid": os.getpid(),
        "loop": mon.stats() if mon else None,
        "process": _STATE.get("process") or process_context(),
        "qos": _STATE.get("qos") or {},
        "iers": _STATE.get("iers") or {},
    }
