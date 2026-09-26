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
import sys
from pathlib import Path
from typing import Optional

import uvicorn

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.messagebus import get_message_bus

logger = logging.getLogger(__name__)


def setup_logging(level: str = "INFO"):
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(name)-20s] %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Quiet down noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


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


async def run_scheduler(config: PhotonScriptConfig):
    """Run only the scheduler web UI (HTTP + optional remote HTTPS)."""
    await asyncio.gather(*[s.serve() for s in _web_servers(config)])


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
    await asyncio.gather(*[a.start() for a in _telescope_agents(config)])


async def run_librarian(config: PhotonScriptConfig):
    """Run the librarian and image processor together."""
    from photonscript.librarian.agent import Librarian
    from photonscript.image_processor.agent import ImageProcessor

    librarian = Librarian(config)
    processor = ImageProcessor(config)

    await asyncio.gather(
        librarian.start(),
        processor.start(),
    )


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

    await asyncio.gather(
        *[s.serve() for s in web],
        *[t.start() for t in telescopes],
        librarian.start(),
        processor.start(),
    )


def start(mode: str = "full", config: Optional[PhotonScriptConfig] = None):
    """Entry point to start PhotonScript in the specified mode."""
    if config is None:
        config = PhotonScriptConfig()

    setup_logging(config.log_level)
    config.data_dir.mkdir(parents=True, exist_ok=True)

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
