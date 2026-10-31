"""One PixInsight queue at a time on the desktop: a shared lock file.

runner.run_script already refuses to start PixInsight while one is open,
but a job runs PixInsight several times (integrate: integration, HDR
combine, finish, color), and between two of its launches PixInsight is
closed: on 2026-10-12 a second queue started in such a gap and two scripts
overlapped. The lock is held for the whole job instead.

Lock file: %LOCALAPPDATA%\\PhotonScript\\pixinsight.lock (env
PS_PIXINSIGHT_LOCK overrides the path), JSON:

    {"pid": 1234, "pid_created": 1760380000.5, "command": "integrate M 27 [rc16]",
     "started": "2026-10-13T21:04:05", "started_epoch": 1760403845.0, "host": "DESK"}

Honored by `photonscript integrate`, `blend`, `finish`, `integrate-watch`,
`autointegrate` (a cycle holds it while it integrates and blends) and the
hand-run launchers deploy\\run-integration.ps1, run-integration-osc.ps1,
run-finish-osc.ps1 and prepare-integration.ps1 (deploy\\pixinsight-lock.ps1;
they write the PixInsight pid, so the lock ends when that PixInsight is
closed). Stale (taken over): the pid is not running, the pid now belongs
to a newer process (pid_created differs), a launcher's "starting" lock
older than STARTING_STALE_S, or an unreadable file older than
UNREADABLE_STALE_S. The lock is re-entrant inside one process (a job's
run_script calls nest). A second job waits up to pixinsight_lock_wait_min
(config, default 60; 0 = do not wait) and then stops with a message that
names the holder.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import time
from datetime import datetime
from pathlib import Path

from photonscript.integration.runner import PixInsightBusy

ENV_PATH = "PS_PIXINSIGHT_LOCK"
DEFAULT_WAIT_MIN = 60.0
POLL_S = 15.0
STARTING_STALE_S = 120.0     # a launcher that never got to start PixInsight
UNREADABLE_STALE_S = 30.0    # half-written by a crashed writer
CREATED_TOL_S = 2.0

_held: dict[str, int] = {}   # lock path -> nesting depth (this process)


class PixInsightLocked(PixInsightBusy):
    """Another job holds the PixInsight queue lock."""

    def __init__(self, msg: str, holder: dict | None = None):
        super().__init__(msg)
        self.holder = holder or {}


def lock_path() -> Path:
    env = os.environ.get(ENV_PATH, "").strip()
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "PhotonScript" / "pixinsight.lock"


def _pid_alive(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except Exception:  # noqa: BLE001
        return False


def _pid_created(pid: int) -> float | None:
    try:
        import psutil
        return float(psutil.Process(int(pid)).create_time())
    except Exception:  # noqa: BLE001 - gone or not allowed
        return None


def read(path: Path | None = None) -> dict | None:
    """The lock's content; None when there is no lock; {"unreadable": True,
    "age_s": ..} when the file cannot be parsed."""
    p = Path(path) if path else lock_path()
    try:
        raw = p.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        return {"unreadable": True, "age_s": 0.0}
    try:
        info = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(info, dict):
            raise ValueError("not an object")
        return info
    except ValueError:
        try:
            age = time.time() - p.stat().st_mtime
        except OSError:
            age = 0.0
        return {"unreadable": True, "age_s": age}


def stale_reason(info: dict, *, alive=None, created=None, now: float | None = None) -> str:
    """Why this lock no longer holds ('' = it is held)."""
    alive = alive or _pid_alive
    created = created or _pid_created
    now = time.time() if now is None else now
    if info.get("unreadable"):
        return "unreadable lock file" if info.get("age_s", 0) > UNREADABLE_STALE_S else ""
    try:
        pid = int(info.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid <= 0:
        return "no pid in the lock"
    if not alive(pid):
        return f"pid {pid} is not running"
    want = info.get("pid_created")
    if want:
        got = created(pid)
        if got is not None and abs(float(got) - float(want)) > CREATED_TOL_S:
            return f"pid {pid} is now another process"
    if info.get("starting"):
        try:
            age = now - float(info.get("started_epoch") or 0)
        except (TypeError, ValueError):
            age = STARTING_STALE_S + 1
        if age > STARTING_STALE_S:
            return f"launcher pid {pid} never started PixInsight"
    return ""


def describe(info: dict | None) -> str:
    if not info:
        return "nobody"
    if info.get("unreadable"):
        return "an unreadable lock file"
    return (f"'{info.get('command') or '?'}' (pid {info.get('pid')}, since "
            f"{info.get('started') or '?'}" + (f" on {info['host']}" if info.get("host") else "") + ")")


def _payload(command: str) -> dict:
    now = time.time()
    pid = os.getpid()
    return {"pid": pid, "pid_created": _pid_created(pid), "command": command,
            "started": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"), "started_epoch": round(now, 3),
            "host": socket.gethostname()}


def _try_create(p: Path, info: dict) -> bool:
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(json.dumps(info, ensure_ascii=True))
    return True


class Lock:
    """A held lock (release() or use as a context manager)."""

    def __init__(self, path: Path, info: dict):
        self.path = Path(path)
        self.info = info
        self.released = False

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        key = str(self.path)
        depth = _held.get(key, 0) - 1
        if depth > 0:
            _held[key] = depth
            return
        _held.pop(key, None)
        cur = read(self.path)
        if cur and not cur.get("unreadable") and int(cur.get("pid") or 0) != os.getpid():
            return   # taken over (we were judged stale): not ours to delete
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def acquire(command: str, *, wait_s: float = 0.0, poll_s: float = POLL_S, path: Path | None = None,
            echo=print, sleep=time.sleep, clock=time.monotonic, alive=None,
            created=None) -> Lock:
    """Take the PixInsight queue lock for `command`, waiting up to wait_s
    for another holder. Raises PixInsightLocked when still held then."""
    p = Path(path) if path else lock_path()
    key = str(p)
    if _held.get(key):
        cur = read(p)
        if cur and not cur.get("unreadable") and int(cur.get("pid") or 0) == os.getpid():
            _held[key] += 1
            return Lock(p, cur)
        _held.pop(key, None)   # our lock file vanished or was taken over: take it again
    t0 = clock()
    said_at = None
    while True:
        info = _payload(command)
        if _try_create(p, info):
            _held[key] = 1
            return Lock(p, info)
        cur = read(p)
        if cur is None:
            continue                      # released between the two calls
        why = stale_reason(cur, alive=alive, created=created)
        if why:
            echo(f"  PixInsight queue lock: taking over a stale lock ({why}; was {describe(cur)})")
            try:
                p.unlink()
                continue
            except FileNotFoundError:
                continue
            except OSError as e:          # cannot remove it: wait like for a live holder
                cur = dict(cur, command=f"{cur.get('command') or '?'} (stale, not removable: {e})")
        waited = clock() - t0
        if waited >= wait_s:
            mins = waited / 60.0
            raise PixInsightLocked(
                f"PixInsight queue busy: {describe(cur)} holds {p}"
                + (f"; waited {mins:.0f} min" if wait_s > 0 else "")
                + ". One PixInsight job runs at a time: run this again when it ends"
                + (" (or raise pixinsight_lock_wait_min)" if wait_s > 0 else
                   " (pixinsight_lock_wait_min = 0: no wait)"), cur)
        if said_at is None or clock() - said_at >= 600:
            echo(f"  PixInsight queue busy: {describe(cur)}; waiting up to "
                 f"{(wait_s - waited) / 60.0:.0f} more min")
            said_at = clock()
        sleep(min(poll_s, max(0.5, wait_s - waited)))


def job(command: str, *, enabled: bool = True, wait_min: float = DEFAULT_WAIT_MIN, echo=print):
    """Context manager for one PixInsight job: the lock (waiting up to
    wait_min) when enabled, else nothing (dry runs, --no-pixinsight)."""
    if not enabled:
        return contextlib.nullcontext()
    return acquire(command, wait_s=max(0.0, float(wait_min or 0)) * 60.0, echo=echo)


def wait_min(cfg) -> float:
    """pixinsight_lock_wait_min from the config (default 60; negative = 0)."""
    try:
        v = float(getattr(cfg, "pixinsight_lock_wait_min", DEFAULT_WAIT_MIN))
    except (TypeError, ValueError):
        v = DEFAULT_WAIT_MIN
    return max(0.0, v)
