"""Supervisor for the long-running PhotonScript service (PS-44).

``photonscript supervise`` runs ``photonscript start`` as a child process and
keeps it up:

- A crash (any exit that is not an operator stop, a restart request or an
  update) is restarted with exponential backoff: 5 s, 10 s, 20 s ... capped at
  5 min. The backoff resets once a child has stayed up for 30 min.
- A crash loop (5 crashes inside 15 min) gives up, sends one alert and exits,
  so a bad deploy cannot hammer NINA or Pushover all night.
- Exit code 42 (the dashboard "Pull latest & restart" / ``POST /api/update``)
  is passed up to ``deploy/run-photonscript.ps1``, which pulls and relaunches
  the supervisor so the NEW supervisor code runs too.
- ``photonscript stop`` drops a HOLD marker before signalling the service, so
  the supervisor stays down after an operator stop, graceful or ``--force``.
- ``photonscript restart`` drops a RESTART marker: the supervisor restarts the
  child at once, without an alert and without counting it as a crash.
- PS-58: when ``update_state.json`` says an update is ``pending`` and the
  wrapper can roll back (``PS_WRAPPER_ROLLBACK=1``), the first start is
  verified: GET /api/health must report the new SHA within
  ``update_verify_s``. If it does, the SHA is recorded as good; if the child
  dies or never answers, the supervisor exits 43 and the wrapper resets the
  checkout to the previous SHA (see shared/updater.py).

Files, all under ``config.data_dir``:

- ``HOLD``            operator asked for the service to stay down
- ``RESTART``         operator asked for an immediate restart
- ``supervisor.pid``  the running supervisor (one per machine)
- ``logs/supervisor.log``  supervisor events (separate from photonscript.log,
  because two processes rotating one file on Windows fails)

The decision logic (:func:`decide`, :class:`RestartPolicy`) is pure and unit
tested; :func:`run` takes injectable ``sleep``/``clock``/``notify`` hooks.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from photonscript.shared import process_control
from photonscript.shared import updater

logger = logging.getLogger("photonscript.supervisor")

HOLD_NAME = "HOLD"
RESTART_NAME = "RESTART"
SUPERVISOR_PID_NAME = "supervisor.pid"

# Exit codes shared with cli.py and deploy/run-photonscript.ps1.
EXIT_OK = 0
EXIT_ALREADY_RUNNING = 3
EXIT_UPDATE = 42
EXIT_ROLLBACK = updater.EXIT_ROLLBACK   # 43: wrapper resets to the previous SHA

# decide() results
STOP = "stop"
UPDATE = "update"
ALREADY = "already"
RESTART = "restart"
CRASH = "crash"


# --- marker files ------------------------------------------------------------

def _path(config, name: str) -> Path:
    return Path(config.data_dir) / name


def _touch(config, name: str, text: str) -> Path:
    p = _path(config, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _clear(config, name: str) -> None:
    try:
        _path(config, name).unlink()
    except (FileNotFoundError, OSError):
        pass


def hold_path(config) -> Path:
    return _path(config, HOLD_NAME)


def create_hold(config) -> Path:
    return _touch(config, HOLD_NAME, "stay down")


def clear_hold(config) -> None:
    _clear(config, HOLD_NAME)


def hold_active(config) -> bool:
    return hold_path(config).exists()


def restart_path(config) -> Path:
    return _path(config, RESTART_NAME)


def create_restart(config) -> Path:
    return _touch(config, RESTART_NAME, "restart")


def consume_restart(config) -> bool:
    """True (and removes the marker) if a restart was requested."""
    p = restart_path(config)
    if not p.exists():
        return False
    _clear(config, RESTART_NAME)
    return True


def supervisor_pid_path(config) -> Path:
    return _path(config, SUPERVISOR_PID_NAME)


def running_supervisor_pid(config) -> Optional[int]:
    """PID of a live supervisor recorded in supervisor.pid, else None."""
    try:
        pid = int(supervisor_pid_path(config).read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return None
    if pid != os.getpid() and process_control.is_pid_alive(pid):
        return pid
    return None


# --- policy ------------------------------------------------------------------

def decide(rc: int, *, hold: bool, restart: bool, interrupted: bool) -> str:
    """What to do after the child exits. Order matters:

    Ctrl-C at the supervisor, or a HOLD from ``photonscript stop``, always
    wins (the operator wants it down). Then an update (42), a restart request,
    another instance already running (3). Everything else, including a bare
    exit 0 with no HOLD, is treated as a crash and restarted.
    """
    if interrupted or hold:
        return STOP
    if rc == EXIT_UPDATE:
        return UPDATE
    if restart:
        return RESTART
    if rc == EXIT_ALREADY_RUNNING:
        return ALREADY
    return CRASH


@dataclass
class RestartPolicy:
    base_delay_s: float = 5.0
    max_delay_s: float = 300.0
    healthy_reset_s: float = 1800.0   # a child up this long resets the backoff
    loop_window_s: float = 900.0      # crash-loop window
    loop_max: int = 5                 # crashes inside the window before giving up
    consecutive: int = 0
    crashes: list = field(default_factory=list)

    def record_crash(self, now: float, ran_for_s: float) -> Optional[float]:
        """Record a crash at ``now`` after the child ran ``ran_for_s``.

        Returns the delay before the next start, or None to give up."""
        if ran_for_s >= self.healthy_reset_s:
            self.consecutive = 0
        self.crashes = [t for t in self.crashes if now - t < self.loop_window_s]
        self.crashes.append(now)
        if len(self.crashes) >= self.loop_max:
            return None
        delay = min(self.base_delay_s * (2 ** self.consecutive), self.max_delay_s)
        self.consecutive += 1
        return delay


# --- logging / alerts ----------------------------------------------------------

def setup_supervisor_logging(data_dir: Path) -> Path:
    """Console plus <data_dir>/logs/supervisor.log (1 MB x 3). Idempotent."""
    log_dir = Path(data_dir) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / "supervisor.log"
    fmt = logging.Formatter("%(asctime)s [supervisor] %(levelname)-7s %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    have = {getattr(h, "baseFilename", None) for h in logger.handlers}
    if str(path) not in have:
        fh = logging.handlers.RotatingFileHandler(
            path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    if not any(isinstance(h, logging.StreamHandler)
               and not isinstance(h, logging.FileHandler) for h in logger.handlers):
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
    return path


def pushover_notifier(config) -> Callable[[str, str, int], None]:
    """Best-effort Pushover sender for the supervisor (never raises)."""
    def _notify(title: str, message: str, priority: int = 0) -> None:
        try:
            import asyncio
            from photonscript.shared import pushover
            asyncio.run(pushover.notify(config, message, title=title,
                                        priority=priority))
        except Exception as e:  # noqa: BLE001 - an alert must never kill the loop
            logger.warning("Pushover send failed: %s", e)
    return _notify


def _fmt_dur(s: float) -> str:
    s = int(s)
    if s < 120:
        return f"{s} s"
    if s < 7200:
        return f"{s // 60} min"
    return f"{s / 3600:.1f} h"


def _kill_child(config, proc) -> None:
    """Hard-kill the child tree (Windows: the venv launcher and the real
    python under it) plus the PID recorded in the PID file, if still alive."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, timeout=15)
    else:
        process_control.force_kill(proc.pid)
    svc = process_control.read_pid_file(config)
    if svc and svc != proc.pid and process_control.is_pid_alive(svc):
        process_control.force_kill(svc)


def health_probe(url: str, expected: str, timeout: float = 5.0) -> bool:
    """True when GET ``url`` (/api/health) answers ok with commit ``expected``."""
    try:
        import httpx
        r = httpx.get(url, timeout=timeout)
        d = r.json()
        return (r.status_code == 200 and d.get("ok") is True
                and d.get("commit") == expected)
    except Exception:  # noqa: BLE001 - down, starting, or not JSON yet
        return False


def verify_child(proc, check: Callable[[], bool], verify_s: float, *,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 poll_s: float = 3.0) -> str:
    """Wait for a freshly updated child to answer healthy.
    Returns "healthy", "exited" (the child ended first) or "timeout"."""
    deadline = clock() + verify_s
    while True:
        if proc.poll() is not None:
            return "exited"
        if check():
            return "healthy"
        if clock() >= deadline:
            return "timeout"
        sleep(poll_s)


def child_command(mode: str) -> list:
    return [sys.executable, "-m", "photonscript.cli", "start", "--mode", mode]


# --- main loop -----------------------------------------------------------------

def run(config, cmd: Sequence[str], *,
        cwd: Optional[Path] = None,
        notify: Optional[Callable[[str, str, int], None]] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        policy: Optional[RestartPolicy] = None,
        host: str = "",
        health_url: Optional[str] = None,
        rollback: bool = False,
        verify_s: float = 90.0,
        probe: Callable[[str, str], bool] = health_probe) -> int:
    """Supervise ``cmd`` until an operator stop, an update (returns 42), a
    crash-loop give-up, or another instance already running. Returns the exit
    code for the wrapper."""
    notify = notify or (lambda title, msg, prio=0: None)
    policy = policy or RestartPolicy()
    host = host or os.environ.get("COMPUTERNAME", "") or "scope PC"

    other = running_supervisor_pid(config)
    if other is not None:
        logger.warning("Another supervisor is already running (pid %s); exiting.",
                       other)
        return EXIT_ALREADY_RUNNING

    clear_hold(config)            # starting the supervisor = operator wants it up
    _clear(config, RESTART_NAME)
    _touch(config, SUPERVISOR_PID_NAME, str(os.getpid()))
    logger.info("Supervisor started (pid %s): %s", os.getpid(), " ".join(cmd))
    starts = 0
    try:
        while True:
            starts += 1
            t0 = clock()
            interrupted = False
            pid_before = process_control.read_pid_file(config)
            proc = subprocess.Popen(list(cmd), cwd=str(cwd) if cwd else None)
            logger.info("Started PhotonScript (pid %s, start #%d)", proc.pid, starts)
            pend = (updater.pending_target(config)
                    if (rollback and health_url) else None)
            verdict = None
            try:
                if pend:
                    logger.info("Verifying the update to %s: waiting up to %s "
                                "for %s", pend[:7], _fmt_dur(verify_s), health_url)
                    verdict = verify_child(
                        proc, lambda: probe(health_url, pend), verify_s,
                        sleep=sleep, clock=clock)
                    if verdict == "healthy":
                        updater.mark_good(config, pend)
                        logger.info("Update %s is healthy after %s; recorded "
                                    "as last good.", pend[:7],
                                    _fmt_dur(clock() - t0))
                    elif verdict == "timeout":
                        why = (f"/api/health did not report {pend[:7]} within "
                               f"{_fmt_dur(verify_s)}")
                        logger.error("%s; stopping it for a rollback.", why)
                        _kill_child(config, proc)
                        proc.wait()
                        if process_control.read_pid_file(config) not in (
                                None, pid_before):
                            process_control.remove_pid_file(config)
                        updater.mark_failed(config, why)
                        return EXIT_ROLLBACK
                rc = proc.wait()
            except KeyboardInterrupt:
                # Ctrl-C reaches the child too; give it time to shut down.
                interrupted = True
                try:
                    rc = proc.wait(timeout=60)
                except (subprocess.TimeoutExpired, KeyboardInterrupt):
                    _kill_child(config, proc)
                    rc = proc.wait()
            ran = clock() - t0

            # os._exit(42) and hard kills skip the child's own cleanup; drop
            # the PID file it wrote so a reused PID can't make the next start
            # refuse. (Compare with the value before launch rather than
            # proc.pid: on Windows the venv python.exe is a launcher, so the
            # service's PID is a grandchild's. A child that exited 3 because
            # another instance runs never wrote the file, so it is kept.)
            pid_after = process_control.read_pid_file(config)
            if pid_after is not None and pid_after != pid_before:
                process_control.remove_pid_file(config)

            action = decide(rc, hold=hold_active(config),
                            restart=consume_restart(config),
                            interrupted=interrupted)
            logger.info("PhotonScript exited with code %s after %s -> %s",
                        rc, _fmt_dur(ran), action)

            if verdict == "exited" and action == CRASH:
                why = (f"new code exited with code {rc} after {_fmt_dur(ran)}, "
                       f"before /api/health reported {pend[:7]}")
                logger.error("%s; handing back to the wrapper to roll back.", why)
                updater.mark_failed(config, why)
                return EXIT_ROLLBACK

            if action == STOP:
                logger.info("Operator stop: staying down.")
                return EXIT_OK
            if action == UPDATE:
                logger.info("Update requested: handing back to the wrapper to pull.")
                return EXIT_UPDATE
            if action == ALREADY:
                logger.warning("Another PhotonScript is already running; "
                               "not supervising.")
                return EXIT_OK
            if action == RESTART:
                logger.info("Restart requested: starting again now.")
                continue

            delay = policy.record_crash(clock(), ran)
            if delay is None:
                msg = (f"PhotonScript on {host} crashed {policy.loop_max} times in "
                       f"{int(policy.loop_window_s // 60)} min (last exit {rc}). "
                       "Giving up; it is DOWN until someone starts it.")
                logger.error(msg)
                notify("PhotonScript is down", msg, 1)
                return EXIT_OK
            msg = (f"PhotonScript on {host} exited with code {rc} after "
                   f"{_fmt_dur(ran)}. Restarting in {_fmt_dur(delay)} "
                   f"(crash {len(policy.crashes)} of {policy.loop_max} allowed "
                   f"in {int(policy.loop_window_s // 60)} min).")
            logger.warning(msg)
            notify("PhotonScript crashed", msg, 1)

            # Wait in 1 s steps so a `photonscript stop` during the backoff
            # still keeps it down.
            waited = 0.0
            while waited < delay:
                step = min(1.0, delay - waited)
                sleep(step)
                waited += step
                if hold_active(config):
                    logger.info("Operator stop during backoff: staying down.")
                    return EXIT_OK
    except KeyboardInterrupt:
        logger.info("Supervisor interrupted: staying down.")
        return EXIT_OK
    finally:
        try:
            if supervisor_pid_path(config).read_text(encoding="utf-8").strip() \
                    == str(os.getpid()):
                supervisor_pid_path(config).unlink()
        except (FileNotFoundError, OSError, ValueError):
            pass
