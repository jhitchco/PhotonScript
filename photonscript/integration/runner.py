"""PS-22: run one PixInsight script, unattended, one instance at a time.

Rules from the M31_OSC4 runs:
  * never start PixInsight while another instance is running (it shares
    settings and swap; a second instance either refuses or corrupts state);
  * launch with -n --automation-mode --run=<script> --force-exit so the
    process exits when the script ends and the next stage can start;
  * follow the script's log by polling: open, read what is new, close. A
    reader that keeps the log open (tail -F) broke a run on 2026-10-05.
Success = the process exited and the log contains "EXIT OK".

Hang watchdog (2026-10-14: the M31 v6 finish hung 3.5 h in a VizieR
download after "No database files have been selected"; PixInsight showed
Not Responding and the queue stayed blocked until it was closed by hand):
the log must keep growing, or the PixInsight tree must keep using CPU (the
v6 integration logged nothing for 145 min while it debayered 23 h of subs,
at full CPU; the hung VizieR download sat idle). No new log text and less
than pixinsight_stall_cpu_pct (default 10) percent of one core for
pixinsight_solve_stall_min (default 20) while the innermost open STAGE is
a plate solve / astrometry / SPCC / VizieR / Gaia stage, or
pixinsight_stall_min (default 60) in any other stage, is a hang. Then: a Pushover alert, and with
pixinsight_hang_action close (the default) the PixInsight process tree
THIS call started (its Popen pid and that pid's children, nothing else) is
terminated and the step fails with reason "hung: no log progress N min in
<stage>" (result["hung"]); finish_retry then retries without the plate
solve. pixinsight_hang_action alert only alerts and keeps waiting (up to
the overall timeout, which never kills). A PixInsight this runner did not
start is never touched.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path

DEFAULT_EXE = r"C:\Program Files\PixInsight\bin\PixInsight.exe"

STALL_MIN = 60.0          # pixinsight_stall_min: no log growth this long = hung
SOLVE_STALL_MIN = 20.0    # pixinsight_solve_stall_min: same inside a solve / SPCC stage
STALL_CPU_PCT = 10.0      # pixinsight_stall_cpu_pct: CPU use (percent of one core) that counts as progress
HANG_ACTIONS = ("close", "alert")
SOLVE_STAGE = re.compile(r"solve|astrometr|spcc|vizier|gaia|imagesolver", re.I)
_STAGE_START = re.compile(r"STAGE START\s+(.*?)\s*$")
_STAGE_END = re.compile(r"STAGE END\s+(.*?)(?:\s+\([^()]*\))?\s*$")


class PixInsightBusy(RuntimeError):
    pass


def pixinsight_running() -> list[int]:
    """PIDs of running PixInsight processes."""
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a dependency
        return []
    out = []
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info.get("name") or "").lower().startswith("pixinsight"):
                out.append(p.pid)
        except Exception:  # noqa: BLE001 - process vanished
            continue
    return out


def read_new(path: Path, pos: int) -> tuple[str, int]:
    """Text appended to `path` since byte `pos`; the file is opened and
    closed inside this call (never held open). A shorter file (rewritten
    from the start, as the PJSR log writer does) is read from 0."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            end = f.tell()
            if end < pos:
                pos = 0
            f.seek(pos)
            data = f.read()
    except OSError:
        return "", pos
    return data.decode("ascii", errors="replace"), pos + len(data)


def launch_kwargs(window: str | None = None, priority: str | None = None) -> dict:
    """Popen options so an automation PixInsight stays out of the way of the
    desktop: its window starts minimized without taking focus
    (pixinsight_window minimized | normal) and at below-normal CPU priority
    (pixinsight_priority below_normal | normal). Windows only; {} elsewhere
    or when both are normal. PixInsight runs the script the same either way."""
    if os.name != "nt":
        return {}
    if window is None or priority is None:
        try:
            from photonscript.shared.config import PhotonScriptConfig
            cfg = PhotonScriptConfig()
            window = window or getattr(cfg, "pixinsight_window", "minimized")
            priority = priority or getattr(cfg, "pixinsight_priority", "below_normal")
        except Exception:  # noqa: BLE001
            window, priority = window or "minimized", priority or "below_normal"
    kw: dict = {}
    if str(window).lower() == "minimized":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7  # SW_SHOWMINNOACTIVE: minimized, focus stays put
        kw["startupinfo"] = si
    if str(priority).lower() == "below_normal":
        kw["creationflags"] = subprocess.BELOW_NORMAL_PRIORITY_CLASS
    return kw


def launch_args(exe: str, script: Path) -> list[str]:
    return [str(exe), "-n", "--automation-mode", f"--run={script}", "--force-exit"]


# ----------------------------------------------------------------- hang watchdog

class StageTracker:
    """The open STAGE START / STAGE END blocks of a PJSR log (nested: the
    finish's plate solve inside "<name> / finish")."""

    def __init__(self):
        self.open: list[str] = []

    def feed(self, line: str) -> None:
        m = _STAGE_END.search(line)
        if m:
            name = m.group(1).strip()
            for i in range(len(self.open) - 1, -1, -1):
                if self.open[i] == name:
                    del self.open[i:]
                    return
            if self.open:
                self.open.pop()
            return
        m = _STAGE_START.search(line)
        if m:
            self.open.append(m.group(1).strip())

    @property
    def current(self) -> str:
        return self.open[-1] if self.open else ""

    @property
    def solving(self) -> bool:
        return bool(self.open) and bool(SOLVE_STAGE.search(self.open[-1]))


def watchdog_settings(stall_min=None, solve_stall_min=None, hang_action=None, cpu_pct=None) -> dict:
    """pixinsight_stall_min / pixinsight_solve_stall_min / pixinsight_hang_action /
    pixinsight_stall_cpu_pct from the config for whatever was not passed
    (<= 0 minutes = that limit off; cpu_pct <= 0 = the log alone counts)."""
    if stall_min is None or solve_stall_min is None or hang_action is None or cpu_pct is None:
        try:
            from photonscript.shared.config import PhotonScriptConfig
            cfg = PhotonScriptConfig()
        except Exception:  # noqa: BLE001
            cfg = None
        if stall_min is None:
            stall_min = getattr(cfg, "pixinsight_stall_min", STALL_MIN)
        if solve_stall_min is None:
            solve_stall_min = getattr(cfg, "pixinsight_solve_stall_min", SOLVE_STALL_MIN)
        if hang_action is None:
            hang_action = getattr(cfg, "pixinsight_hang_action", "close")
        if cpu_pct is None:
            cpu_pct = getattr(cfg, "pixinsight_stall_cpu_pct", STALL_CPU_PCT)
    action = str(hang_action or "close").strip().lower()
    if action not in HANG_ACTIONS:
        action = "close"
    try:
        stall = float(stall_min)
    except (TypeError, ValueError):
        stall = STALL_MIN
    try:
        solve = float(solve_stall_min)
    except (TypeError, ValueError):
        solve = SOLVE_STALL_MIN
    try:
        cpu = float(cpu_pct)
    except (TypeError, ValueError):
        cpu = STALL_CPU_PCT
    return {"stall_min": stall, "solve_stall_min": solve, "hang_action": action, "cpu_pct": cpu}


def stall_limit_min(tracker: StageTracker, w: dict) -> float:
    """The no-progress limit (minutes, <= 0 = off) for the current stage."""
    return w["solve_stall_min"] if tracker.solving else w["stall_min"]


def tree_cpu_seconds(pid: int | None, *, psutil_mod=None) -> float | None:
    """CPU seconds (user + system) used so far by process `pid` and its
    children; None when it cannot be read."""
    if not pid:
        return None
    try:
        ps = psutil_mod
        if ps is None:
            import psutil as ps
        root = ps.Process(int(pid))
        procs = [root]
        try:
            procs += root.children(recursive=True)
        except Exception:  # noqa: BLE001
            pass
        total = 0.0
        for p in procs:
            try:
                t = p.cpu_times()
                total += float(t.user) + float(t.system)
            except Exception:  # noqa: BLE001 - a child that just exited
                continue
        return total
    except Exception:  # noqa: BLE001 - gone, or not allowed
        return None


def kill_tree(pid: int | None, *, psutil_mod=None, wait_s: float = 15.0) -> list[int]:
    """Terminate (then kill) process `pid` and its children: only the tree
    of the PixInsight this runner started (the caller passes its own Popen
    pid). Returns the pids it signalled. Never raises."""
    if not pid:
        return []
    try:
        ps = psutil_mod
        if ps is None:
            import psutil as ps
        root = ps.Process(int(pid))
    except Exception:  # noqa: BLE001 - already gone
        return []
    try:
        kids = root.children(recursive=True)
    except Exception:  # noqa: BLE001
        kids = []
    procs = kids + [root]
    done = []
    for p in procs:
        try:
            p.terminate()
            done.append(p.pid)
        except Exception:  # noqa: BLE001 - vanished
            continue
    try:
        _gone, alive = ps.wait_procs(procs, timeout=wait_s)
    except Exception:  # noqa: BLE001
        alive = procs
    for p in alive:
        try:
            p.kill()
        except Exception:  # noqa: BLE001
            continue
    return done


def hang_notifier(title: str = "PhotonScript PixInsight"):
    """notify(message) through the shared rate-limited Pushover client."""
    def notify(message: str) -> bool:
        try:
            import asyncio

            from photonscript.shared.config import PhotonScriptConfig
            from photonscript.shared.pushover import notify as po
            return bool(asyncio.run(po(PhotonScriptConfig(), message, title=title, priority=1, kind="act")))
        except Exception:  # noqa: BLE001 - an alert must never break the run
            return False
    return notify


def run_script(script: Path, log_path: Path, *, exe: str = DEFAULT_EXE, poll_s: float = 15.0,
               timeout_s: float = 6 * 3600, echo=print, popen=subprocess.Popen,
               running=pixinsight_running, sleep=time.sleep, ok_marker: str = "EXIT OK",
               stall_min: float | None = None, solve_stall_min: float | None = None,
               hang_action: str | None = None, stall_cpu_pct: float | None = None, notify=None,
               kill=None, cpu=None, clock=time.monotonic) -> dict:
    """Run `script` in a fresh PixInsight and wait for it. Returns
    {ok, exit_code, minutes, last_line, timed_out, hung, hang_stage,
    killed}. Raises PixInsightBusy if PixInsight is already running or
    another job holds the PixInsight queue lock (pilock.py; nothing is
    started then). The hang watchdog (module doc): stall_min /
    solve_stall_min / hang_action / stall_cpu_pct default to the config;
    notify(message) defaults to Pushover, kill(pid) to kill_tree,
    cpu(pid) (CPU seconds of the tree) to tree_cpu_seconds."""
    from photonscript.integration import pilock
    # the queue lock (pilock.py): a no-op nest inside a job that holds it
    # (integrate / blend / finish), else this one launch takes it
    with pilock.acquire(f"PixInsight {Path(script).name}", wait_s=0, echo=echo):
        return _run_script(script, log_path, exe=exe, poll_s=poll_s, timeout_s=timeout_s, echo=echo,
                           popen=popen, running=running, sleep=sleep, ok_marker=ok_marker,
                           watchdog=watchdog_settings(stall_min, solve_stall_min, hang_action,
                                                      stall_cpu_pct),
                           notify=notify, kill=kill, cpu=cpu, clock=clock)


def _run_script(script: Path, log_path: Path, *, exe: str, poll_s: float, timeout_s: float, echo,
                popen, running, sleep, ok_marker: str, watchdog: dict | None = None, notify=None,
                kill=None, cpu=None, clock=time.monotonic) -> dict:
    busy = running()
    if busy:
        raise PixInsightBusy(f"PixInsight is already running (pid {', '.join(map(str, busy))}); "
                             "close it and run again")
    if not Path(exe).exists():
        raise FileNotFoundError(f"PixInsight not found at {exe}")
    t0 = time.time()
    args = launch_args(exe, Path(script))
    # the real Popen gets the minimized / below-normal options; test fakes
    # take the argument list only
    proc = popen(args, **launch_kwargs()) if popen is subprocess.Popen else popen(args)
    # the only process the watchdog may ever close: the one started here
    own_pid = getattr(proc, "pid", None)
    w = watchdog or {"stall_min": 0.0, "solve_stall_min": 0.0, "hang_action": "alert", "cpu_pct": 0.0}
    cpu = cpu or tree_cpu_seconds
    cpu_pct = float(w.get("cpu_pct") or 0.0)
    cpu_prev = (cpu(own_pid), clock()) if cpu_pct > 0 else (None, 0.0)
    tracker = StageTracker()
    pos, last, timed_out = 0, "", False
    hung, hang_stage, alerted, killed = "", "", False, []
    seen_at = clock()
    while True:
        rc = proc.poll()
        txt, new_pos = read_new(Path(log_path), pos)
        if new_pos != pos or txt:
            seen_at = clock()
        pos = new_pos
        if cpu_pct > 0:
            # a busy PixInsight (debayer, star alignment: no log line for
            # hours) is working, not hung
            c_now, t_now = cpu(own_pid), clock()
            c0, t0c = cpu_prev
            if c_now is not None and c0 is not None and t_now > t0c \
                    and (c_now - c0) >= cpu_pct / 100.0 * (t_now - t0c):
                seen_at = t_now
            cpu_prev = (c_now, t_now)
        for line in txt.splitlines():
            if line.strip():
                last = line.strip()
                tracker.feed(last)
                if "STAGE" in line or "ERROR" in line or "DROPPED" in line or "EXIT" in line:
                    echo("  PI | " + last)
        if rc is not None:
            break
        if time.time() - t0 > timeout_s:
            timed_out = True
            echo(f"  PixInsight still running after {timeout_s / 3600:.1f} h; leaving it, not killing it")
            break
        limit = stall_limit_min(tracker, w)
        if limit > 0 and not alerted and (clock() - seen_at) / 60.0 >= limit:
            alerted = True
            hang_stage = tracker.current or "startup"
            reason = f"hung: no log progress {limit:g} min in {hang_stage}"
            close = w["hang_action"] == "close" and bool(own_pid)
            echo(f"  PixInsight {reason} (pid {own_pid}); "
                 + ("closing the PixInsight this runner started" if close
                    else "pixinsight_hang_action alert: leaving it running"))
            msg = (f"PixInsight {reason} ({Path(script).name}); "
                   + (f"closed it (pid {own_pid}), the step fails" if close
                      else "left running: close it by hand"))
            try:
                (notify or hang_notifier())(msg)
            except Exception:  # noqa: BLE001 - the alert must not stop the watchdog
                pass
            if close:
                hung = reason
                killed = (kill or kill_tree)(own_pid) or []
                for _ in range(20):           # let the Popen see the exit
                    if proc.poll() is not None:
                        break
                    sleep(min(poll_s, 1.0))
                break
        sleep(poll_s)
    full, _ = read_new(Path(log_path), 0)
    return {"ok": (ok_marker in full) and not timed_out and not hung, "exit_code": proc.poll(),
            "minutes": round((time.time() - t0) / 60.0, 2), "last_line": last,
            "timed_out": timed_out, "hung": hung, "hang_stage": hang_stage if hung else "",
            "killed": killed}
