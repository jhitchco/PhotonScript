"""Process control for the long-running PhotonScript service.

Small, pure, unit-testable helpers behind the ``photonscript start`` /
``photonscript stop`` CLI commands and the orchestrator's coordinated
shutdown. Everything is anchored under ``config.data_dir``:

- ``<data_dir>/photonscript.pid`` — the running foreground process's PID.
- ``<data_dir>/STOP``            — the stop sentinel. ``photonscript stop``
  drops this file; the orchestrator's shutdown watcher polls for it (~1 Hz),
  sets its stop event and deletes the file. This is how ``stop`` talks to a
  running instance on Windows without fragile cross-process signals.

None of this touches the exit-code-42 self-update path (see
``deploy/run-photonscript.ps1``): a clean operator stop makes ``asyncio.run``
return normally (exit 0), while the self-update still calls ``os._exit(42)``
so the wrapper's ``while`` loop restarts the process. The two are independent.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PID_FILENAME = "photonscript.pid"
STOP_SENTINEL_NAME = "STOP"


def pid_file_path(config) -> Path:
    """Absolute path to the PID file for this config's data_dir."""
    return Path(config.data_dir) / PID_FILENAME


def stop_sentinel_path(config) -> Path:
    """Absolute path to the stop sentinel for this config's data_dir."""
    return Path(config.data_dir) / STOP_SENTINEL_NAME


def is_pid_alive(pid: int) -> bool:
    """True if a process with ``pid`` is currently running.

    Uses psutil when available (cross-platform, reliable on Windows); falls
    back to ``os.kill(pid, 0)`` on POSIX. A malformed/zero pid is never alive.
    """
    if not pid or pid <= 0:
        return False
    try:
        import psutil  # a project dependency
        return psutil.pid_exists(pid)
    except Exception:  # noqa: BLE001 — psutil missing or errored; fall back
        pass
    if os.name == "nt":
        # No psutil and on Windows: cannot cheaply probe; assume not alive so a
        # stale file never blocks startup. (psutil is a hard dep, so this is a
        # last-resort branch.)
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return False
    return True


def read_pid_file(config) -> Optional[int]:
    """Return the PID recorded in the PID file, or None if absent/garbage."""
    path = pid_file_path(config)
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def write_pid_file(config, pid: Optional[int] = None) -> Path:
    """Write the current (or given) PID to the PID file, creating data_dir.

    A stale file is simply overwritten. Callers wanting to *refuse* startup on
    a live PID should check :func:`running_pid` first.
    """
    if pid is None:
        pid = os.getpid()
    path = pid_file_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(pid), encoding="utf-8")
    return path


def remove_pid_file(config) -> None:
    """Best-effort removal of the PID file (used on clean exit)."""
    try:
        pid_file_path(config).unlink()
    except (FileNotFoundError, OSError):
        pass


def running_pid(config) -> Optional[int]:
    """If a *live* instance is recorded in the PID file, return its PID.

    Returns None when there is no PID file, or the recorded PID is not alive
    (a stale/orphan file — the caller may safely overwrite it).
    """
    pid = read_pid_file(config)
    if pid is not None and is_pid_alive(pid):
        return pid
    return None


def create_stop_sentinel(config) -> Path:
    """Create the STOP sentinel so a running instance shuts down gracefully."""
    path = stop_sentinel_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("stop", encoding="utf-8")
    return path


def clear_stop_sentinel(config) -> None:
    """Best-effort removal of the STOP sentinel (consumed by the watcher)."""
    try:
        stop_sentinel_path(config).unlink()
    except (FileNotFoundError, OSError):
        pass


def force_kill(pid: int) -> tuple[bool, str]:
    """Hard-kill ``pid``. Windows: ``taskkill /PID <pid> /F``; POSIX: SIGKILL.

    Returns (ok, detail). Used only by ``photonscript stop --force`` as a
    fallback when the graceful sentinel path is not enough.
    """
    if not pid or pid <= 0:
        return False, f"invalid pid {pid!r}"
    if os.name == "nt":
        import subprocess
        try:
            proc = subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                capture_output=True, text=True, timeout=15)
        except Exception as e:  # noqa: BLE001
            return False, f"taskkill failed: {e}"
        detail = (proc.stdout or proc.stderr or "").strip()
        return proc.returncode == 0, detail or f"taskkill rc={proc.returncode}"
    # POSIX (sandbox / dev)
    import signal as _signal
    try:
        os.kill(pid, _signal.SIGKILL)
    except ProcessLookupError:
        return False, f"no such process {pid}"
    except OSError as e:
        return False, f"kill failed: {e}"
    return True, f"sent SIGKILL to {pid}"
