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


async def run_scheduler(config: PhotonScriptConfig):
    """Run only the scheduler web UI."""
    server = uvicorn.Server(uvicorn.Config(
        "photonscript.scheduler.app:app",
        host=config.scheduler_host,
        port=config.scheduler_port,
        log_level="warning",
    ))
    await server.serve()


async def run_telescope_agent(config: PhotonScriptConfig):
    """Run only the telescope agent (on Windows telescope PC)."""
    from photonscript.telescope_agent.agent import TelescopeAgent
    agent = TelescopeAgent(config)
    await agent.start()


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
    from photonscript.telescope_agent.agent import TelescopeAgent
    from photonscript.librarian.agent import Librarian
    from photonscript.image_processor.agent import ImageProcessor

    telescope = TelescopeAgent(config)
    librarian = Librarian(config)
    processor = ImageProcessor(config)

    # Start scheduler as uvicorn server
    server = uvicorn.Server(uvicorn.Config(
        "photonscript.scheduler.app:app",
        host=config.scheduler_host,
        port=config.scheduler_port,
        log_level="warning",
    ))

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
        server.serve(),
        telescope.start(),
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
