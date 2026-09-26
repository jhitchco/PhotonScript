"""PhotonScript Orchestrator — starts and coordinates all agents.

The orchestrator can run in different modes:
- full: All agents (for single-machine setups or development)
- scheduler: Just the scheduler web UI (run anywhere)
- telescope: Telescope agent only (run on the Windows telescope PC)
- librarian: Librarian + image processor (run on local machine)
"""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import signal
import sys
from pathlib import Path
from typing import Optional

import uvicorn

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.messagebus import get_message_bus
from photonscript.shared import process_control

logger = logging.getLogger(__name__)

_LOG_FORMAT = "%(asctime)s [%(name)-20s] %(levelname)-7s %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"


def setup_logging(level: str = "INFO", data_dir: Optional[Path] = None):
    """Configure console logging (unchanged) and, when ``data_dir`` is given,
    ALSO a rotating file at ``<data_dir>/logs/photonscript.log``.

    Pure addition: callers that pass no ``data_dir`` keep the exact prior
    console-only behavior. The file handler mirrors the console format/level.
    """
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format=_LOG_FORMAT,
        datefmt=_LOG_DATEFMT,
    )
    # Quiet down noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

    if data_dir is not None:
        _add_rotating_file_handler(Path(data_dir), level)


def _add_rotating_file_handler(data_dir: Path, level: str) -> Optional[Path]:
    """Attach a ~10 MB x 5-backup rotating file handler to the root logger.

    Idempotent: if a file handler is already pointed at this log path (e.g.
    setup_logging was called twice), it is not added again.
    """
    log_dir = data_dir / "logs"
    log_path = log_dir / "photonscript.log"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:  # noqa: BLE001 — never let logging setup crash startup
        logging.getLogger(__name__).warning(
            "Could not create log dir %s (%s) — console logging only", log_dir, e)
        return None

    root = logging.getLogger()
    want = {target for target in (str(log_path), str(log_path.resolve()))}
    for h in root.handlers:
        if isinstance(h, logging.handlers.RotatingFileHandler) and \
                getattr(h, "baseFilename", None) in want:
            return log_path  # already installed

    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setLevel(getattr(logging, level.upper(), logging.INFO))
    fh.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATEFMT))
    root.addHandler(fh)
    logging.getLogger(__name__).info("File logging to %s", log_path)
    return log_path


def _ensure_tls_cert(config: PhotonScriptConfig) -> Optional[tuple[str, str]]:
    """Export/refresh the Tailscale cert for the HTTPS listener; return
    (certfile, keyfile) or None to stay HTTP-only. `tailscale cert` is
    idempotent (re-fetches only near expiry). Requires the service account to be
    the tailscaled operator (see MAINTENANCE / `tailscale set --operator`)."""
    import subprocess
    host = (config.scheduler_tls_hostname or "").strip()
    if not config.scheduler_tls_enabled or not host:
        return None
    cert_dir = Path(config.scheduler_tls_cert_dir or (config.data_dir / "certs"))
    cert_dir.mkdir(parents=True, exist_ok=True)
    certfile = cert_dir / f"{host}.crt"
    keyfile = cert_dir / f"{host}.key"
    try:
        subprocess.run(
            [config.tailscale_exe, "cert",
             "--cert-file", str(certfile), "--key-file", str(keyfile), host],
            check=True, capture_output=True, text=True, timeout=120)
        logger.info("TLS cert ready for %s (%s)", host, certfile)
    except Exception as e:  # noqa: BLE001
        if certfile.exists() and keyfile.exists():
            logger.warning("tailscale cert refresh failed (%s) — using existing "
                           "files; renew before ~90d expiry", e)
        else:
            logger.error("tailscale cert failed, no cert on disk (%s) — HTTPS "
                         "disabled, HTTP :%d still up", e, config.scheduler_port)
            return None
    return str(certfile), str(keyfile)


def _web_servers(config: PhotonScriptConfig) -> list[uvicorn.Server]:
    """HTTP listener (loopback + tailnet, unchanged) + optional HTTPS listener
    for remote browsers (replaces `tailscale serve`)."""
    servers = [uvicorn.Server(uvicorn.Config(
        "photonscript.scheduler.app:app",
        host=config.scheduler_host, port=config.scheduler_port,
        log_level="warning"))]
    tls = _ensure_tls_cert(config)
    if tls:
        certfile, keyfile = tls
        s = uvicorn.Server(uvicorn.Config(
            "photonscript.scheduler.app:app",
            host=config.scheduler_host, port=config.scheduler_tls_port,
            log_level="warning", ssl_certfile=certfile, ssl_keyfile=keyfile))
        s.install_signal_handlers = lambda: None  # only the first server owns signals
        servers.append(s)
        logger.info("Scheduler HTTPS on :%d (remote); HTTP on :%d (internal)",
                    config.scheduler_tls_port, config.scheduler_port)
    return servers


def _install_stop_signals(loop: asyncio.AbstractEventLoop,
                          stop_event: asyncio.Event) -> None:
    """Route SIGINT / SIGBREAK (Win) / SIGTERM to a single stop_event.

    Uses ``signal.signal`` (not ``loop.add_signal_handler``, which is
    unsupported on Windows) and schedules the set on the loop thread-safely.
    Only effective in the main thread; a non-main thread silently no-ops.
    """
    def _handler(signum, frame):  # runs in the main thread, sync context
        loop.call_soon_threadsafe(stop_event.set)

    for signame in ("SIGINT", "SIGBREAK", "SIGTERM"):
        sig = getattr(signal, signame, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError, RuntimeError):
            # e.g. not the main thread — leave default handling in place
            pass


async def _shutdown_watcher(config: PhotonScriptConfig,
                            stop_event: asyncio.Event,
                            servers: list[uvicorn.Server],
                            agent_tasks: list[asyncio.Task]) -> None:
    """Wait for an operator stop (signal OR the STOP sentinel file), then wind
    everything down: flip ``should_exit`` on every uvicorn server and cancel
    the agent tasks so the outer gather unwinds within ~a second.

    Polls ``<data_dir>/STOP`` every ~1s so ``photonscript stop`` can trigger a
    graceful shutdown cross-process on Windows.
    """
    sentinel = process_control.stop_sentinel_path(config)
    while not stop_event.is_set():
        try:
            if sentinel.exists():
                logger.info("STOP sentinel detected (%s) — shutting down", sentinel)
                stop_event.set()
                break
        except OSError:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

    logger.info("Coordinated shutdown: stopping %d server(s), cancelling %d "
                "agent task(s)", len(servers), len(agent_tasks))
    for s in servers:
        s.should_exit = True
    for t in agent_tasks:
        t.cancel()
    # The sentinel has done its job; remove it so a later start isn't
    # immediately told to stop again.
    process_control.clear_stop_sentinel(config)


async def _run_with_shutdown(config: PhotonScriptConfig,
                             servers: list[uvicorn.Server],
                             agent_coros: list) -> None:
    """Run uvicorn server(s) + agent coroutines under one coordinated stop.

    One owner of shutdown: uvicorn's own signal handlers are disabled on ALL
    servers and a single stop_event (fed by OS signals + the STOP sentinel)
    drives both the servers and the agents down together. This fixes the
    dual-server Ctrl-C hang where only the first uvicorn server owned signals
    and the TLS server (plus the agents) were left running.
    """
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    _install_stop_signals(loop, stop_event)

    for s in servers:
        s.install_signal_handlers = lambda: None  # single owner of shutdown

    server_tasks = [asyncio.create_task(s.serve(), name=f"uvicorn-{i}")
                    for i, s in enumerate(servers)]
    agent_tasks = [asyncio.create_task(c, name=f"agent-{i}")
                   for i, c in enumerate(agent_coros)]
    watcher = asyncio.create_task(
        _shutdown_watcher(config, stop_event, servers, agent_tasks),
        name="shutdown-watcher")

    all_tasks = server_tasks + agent_tasks + [watcher]
    try:
        # If any server or agent exits on its own (e.g. a crash), trip the stop
        # so the rest wind down too instead of hanging in gather.
        done, pending = await asyncio.wait(
            server_tasks + agent_tasks, return_when=asyncio.FIRST_COMPLETED)
        if not stop_event.is_set():
            stop_event.set()
        # Let the watcher flip should_exit / cancel, then drain everything.
        await asyncio.gather(*all_tasks, return_exceptions=True)
    finally:
        for t in all_tasks:
            if not t.done():
                t.cancel()
        # surface a genuine (non-cancellation) error from a completed task
        for t in server_tasks + agent_tasks:
            if t.done() and not t.cancelled():
                exc = t.exception()
                if exc is not None:
                    raise exc


async def run_scheduler(config: PhotonScriptConfig):
    """Run only the scheduler web UI (HTTP + optional remote HTTPS)."""
    await _run_with_shutdown(config, _web_servers(config), [])


def _telescope_agents(config: PhotonScriptConfig) -> list:
    """One TelescopeAgent per enabled rig. The piggyback gets a config view
    pointed at NINA #2 (its base URL, watch dir, pixel scale, setpoint)."""
    from photonscript.telescope_agent.agent import TelescopeAgent
    from photonscript.shared.rigs import rig_ids, rig_config, PIGGYBACK
    agents = [TelescopeAgent(config, rig="rc16")]
    if PIGGYBACK in rig_ids(config):
        # Require the piggyback's OWN watch dir. If unset, rig_config would
        # inherit the RC16 folder and the 2nd agent would double-grade the main
        # rig's frames — so refuse to start until a distinct dir is configured.
        pb_dir = getattr(config, "piggyback_image_watch_dir", "") or ""
        main_dir = str(getattr(config, "image_watch_dir", "") or "")
        if pb_dir and Path(pb_dir) != Path(main_dir):
            agents.append(TelescopeAgent(rig_config(config, PIGGYBACK),
                                         rig=PIGGYBACK))
            logger.info("Piggyback rig enabled — 2nd telescope agent watching %s",
                        pb_dir)
        else:
            logger.warning("Piggyback enabled but PS_PIGGYBACK_IMAGE_WATCH_DIR "
                           "is unset or equals the RC16 dir — not starting its "
                           "agent (would double-grade the main rig)")
    return agents


async def run_telescope_agent(config: PhotonScriptConfig):
    """Run the telescope agent(s) — main rig plus piggyback if enabled."""
    await _run_with_shutdown(
        config, [], [a.start() for a in _telescope_agents(config)])


async def run_librarian(config: PhotonScriptConfig):
    """Run the librarian and image processor together."""
    from photonscript.librarian.agent import Librarian
    from photonscript.image_processor.agent import ImageProcessor

    librarian = Librarian(config)
    processor = ImageProcessor(config)

    await _run_with_shutdown(config, [], [librarian.start(), processor.start()])


async def run_full(config: PhotonScriptConfig):
    """Run all agents together (development / single-machine mode)."""
    from photonscript.librarian.agent import Librarian
    from photonscript.image_processor.agent import ImageProcessor

    telescopes = _telescope_agents(config)  # main + piggyback if enabled
    librarian = Librarian(config)
    processor = ImageProcessor(config)

    # Start scheduler as uvicorn server(s): HTTP (internal) + optional remote HTTPS
    web = _web_servers(config)

    logger.info("=" * 60)
    logger.info("  PhotonScript — Remote Telescope Orchestration")
    logger.info("=" * 60)
    logger.info("  Observatory:  %s", config.observatory_name)
    logger.info("  Location:     %.1f°N, %.1f°W, %dm", config.observatory_lat, abs(config.observatory_lon), config.observatory_elev)
    logger.info("  Dashboard:    http://%s:%d", config.scheduler_host, config.scheduler_port)
    logger.info("  Mode:         full (all agents)")
    from photonscript.shared.version import repo_version
    logger.info("  Version:      %s", repo_version())
    logger.info("=" * 60)

    agent_coros = [t.start() for t in telescopes]
    agent_coros += [librarian.start(), processor.start()]
    await _run_with_shutdown(config, web, agent_coros)


def start(mode: str = "full", config: Optional[PhotonScriptConfig] = None):
    """Entry point to start PhotonScript in the specified mode."""
    if config is None:
        config = PhotonScriptConfig()

    config.data_dir.mkdir(parents=True, exist_ok=True)
    setup_logging(config.log_level, config.data_dir)

    runners = {
        "full": run_full,
        "scheduler": run_scheduler,
        "telescope": run_telescope_agent,
        "librarian": run_librarian,
    }

    runner = runners.get(mode)
    if runner is None:
        logger.error("Unknown mode: %s (choose: %s)", mode, ", ".join(runners))
        sys.exit(1)

    try:
        asyncio.run(runner(config))
    except KeyboardInterrupt:
        logger.info("PhotonScript shutting down")
