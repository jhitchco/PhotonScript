"""Unit tests for the start/stop process controller and file logging.

These cover the pure, sandbox-friendly pieces: PID-file write/read/remove,
stale-vs-live detection, the STOP sentinel, the coordinated shutdown watcher
tripping on the sentinel, and setup_logging adding a rotating file handler.
The full asyncio dual-server lifecycle is not unit-tested here (hard to stand
up uvicorn in the sandbox); the watcher is exercised directly instead.
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os

import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared import process_control as pc
from photonscript import orchestrator


def _cfg(tmp_path):
    return PhotonScriptConfig(data_dir=tmp_path)


# --- PID file -------------------------------------------------------------

def test_pid_file_write_read_remove(tmp_path):
    cfg = _cfg(tmp_path)
    assert pc.read_pid_file(cfg) is None  # nothing yet

    path = pc.write_pid_file(cfg)
    assert path == tmp_path / "photonscript.pid"
    assert path.exists()
    assert pc.read_pid_file(cfg) == os.getpid()

    pc.remove_pid_file(cfg)
    assert not path.exists()
    # removing again is a no-op, not an error
    pc.remove_pid_file(cfg)


def test_write_pid_file_creates_data_dir(tmp_path):
    cfg = _cfg(tmp_path / "nested" / "dir")
    path = pc.write_pid_file(cfg, pid=4321)
    assert path.exists()
    assert pc.read_pid_file(cfg) == 4321


def test_running_pid_live_vs_stale(tmp_path):
    cfg = _cfg(tmp_path)
    # A live PID (our own) is reported as running.
    pc.write_pid_file(cfg, pid=os.getpid())
    assert pc.running_pid(cfg) == os.getpid()

    # A PID that is not alive -> stale, running_pid returns None (safe to
    # overwrite). Pick an unused, implausible PID.
    dead = _find_dead_pid()
    pc.write_pid_file(cfg, pid=dead)
    assert pc.read_pid_file(cfg) == dead
    assert pc.running_pid(cfg) is None


def test_read_pid_file_garbage_is_none(tmp_path):
    cfg = _cfg(tmp_path)
    pc.pid_file_path(cfg).write_text("not-a-pid", encoding="utf-8")
    assert pc.read_pid_file(cfg) is None
    assert pc.running_pid(cfg) is None


def test_is_pid_alive():
    assert pc.is_pid_alive(os.getpid()) is True
    assert pc.is_pid_alive(0) is False
    assert pc.is_pid_alive(-1) is False
    assert pc.is_pid_alive(_find_dead_pid()) is False


# --- STOP sentinel --------------------------------------------------------

def test_stop_sentinel_create_and_clear(tmp_path):
    cfg = _cfg(tmp_path)
    sentinel = pc.stop_sentinel_path(cfg)
    assert not sentinel.exists()

    created = pc.create_stop_sentinel(cfg)
    assert created == sentinel
    assert sentinel.exists()

    pc.clear_stop_sentinel(cfg)
    assert not sentinel.exists()
    pc.clear_stop_sentinel(cfg)  # idempotent


# --- Coordinated shutdown watcher ----------------------------------------

class _FakeServer:
    def __init__(self):
        self.should_exit = False


@pytest.mark.asyncio
async def test_shutdown_watcher_trips_on_sentinel(tmp_path):
    """Dropping the STOP sentinel should flip every server's should_exit,
    cancel the agent tasks, and remove the sentinel."""
    cfg = _cfg(tmp_path)
    stop_event = asyncio.Event()
    servers = [_FakeServer(), _FakeServer()]

    async def _long_agent():
        await asyncio.sleep(3600)

    agent_tasks = [asyncio.create_task(_long_agent()) for _ in range(2)]

    watcher = asyncio.create_task(
        orchestrator._shutdown_watcher(cfg, stop_event, servers, agent_tasks))

    # Simulate `photonscript stop`.
    await asyncio.sleep(0.05)
    pc.create_stop_sentinel(cfg)

    await asyncio.wait_for(watcher, timeout=5)

    assert all(s.should_exit for s in servers)
    assert all(t.cancelled() or t.done() for t in agent_tasks)
    assert not pc.stop_sentinel_path(cfg).exists()  # consumed
    # drain the cancelled agent tasks
    for t in agent_tasks:
        with pytest.raises(asyncio.CancelledError):
            await t


@pytest.mark.asyncio
async def test_shutdown_watcher_trips_on_event(tmp_path):
    """Setting the stop_event directly (as the signal handler does) also
    winds everything down, without any sentinel on disk."""
    cfg = _cfg(tmp_path)
    stop_event = asyncio.Event()
    servers = [_FakeServer()]
    agent_tasks: list[asyncio.Task] = []

    watcher = asyncio.create_task(
        orchestrator._shutdown_watcher(cfg, stop_event, servers, agent_tasks))
    await asyncio.sleep(0.05)
    stop_event.set()
    await asyncio.wait_for(watcher, timeout=5)

    assert servers[0].should_exit is True


# --- File logging ---------------------------------------------------------

def test_setup_logging_adds_rotating_file_handler(tmp_path):
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        orchestrator.setup_logging("INFO", data_dir=tmp_path)
        log_path = tmp_path / "logs" / "photonscript.log"
        assert log_path.exists()

        file_handlers = [h for h in root.handlers
                         if isinstance(h, logging.handlers.RotatingFileHandler)
                         and getattr(h, "baseFilename", None)
                         in {str(log_path), str(log_path.resolve())}]
        assert len(file_handlers) == 1
        fh = file_handlers[0]
        assert fh.maxBytes == 10 * 1024 * 1024
        assert fh.backupCount == 5

        # A log record actually lands in the file.
        logging.getLogger("photonscript.test").warning("hello-file-log")
        for h in file_handlers:
            h.flush()
        assert "hello-file-log" in log_path.read_text(encoding="utf-8")

        # Idempotent: a second call with the same data_dir must not add a
        # duplicate file handler.
        orchestrator.setup_logging("INFO", data_dir=tmp_path)
        again = [h for h in root.handlers
                 if isinstance(h, logging.handlers.RotatingFileHandler)
                 and getattr(h, "baseFilename", None)
                 in {str(log_path), str(log_path.resolve())}]
        assert len(again) == 1
    finally:
        # Detach any handlers we added so we don't leak into other tests.
        for h in list(root.handlers):
            if h not in before and isinstance(h, logging.handlers.RotatingFileHandler):
                root.removeHandler(h)
                h.close()


def test_setup_logging_no_data_dir_is_console_only(tmp_path):
    """The pure-addition contract: without data_dir, no file handler appears."""
    root = logging.getLogger()
    before_files = [h for h in root.handlers
                    if isinstance(h, logging.handlers.RotatingFileHandler)]
    orchestrator.setup_logging("INFO")
    after_files = [h for h in root.handlers
                   if isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(after_files) == len(before_files)


def _find_dead_pid() -> int:
    """Return a PID that is (almost certainly) not currently alive."""
    import psutil
    for candidate in range(999999, 100000, -1):
        if not psutil.pid_exists(candidate):
            return candidate
    return 999999
