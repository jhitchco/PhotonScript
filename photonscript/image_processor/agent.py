"""Image Processor Agent — stacking, processing, and progress tracking.

Takes transferred images, groups them by target and filter, runs stacking
via Siril (or optionally PixInsight), and feeds progress back to the scheduler.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (
    AgentMessage, AgentRole, FilterType, ImageStatus,
)
from photonscript.shared.messagebus import get_message_bus

logger = logging.getLogger(__name__)


class ImageProcessor:
    """Processes transferred astrophotography sub-frames.

    Responsibilities:
    - Receive transfer_complete events from the librarian
    - Group images by target and filter
    - Run automated stacking via Siril CLI
    - Optionally integrate with PixInsight for advanced processing
    - Feed progress and preview images back to the scheduler
    """

    def __init__(self, config: PhotonScriptConfig):
        self.config = config
        self.bus = get_message_bus()
        self._running = False
        self._image_groups: dict[str, dict[str, list[str]]] = {}  # target -> filter -> [paths]
        self._output_dir = Path(config.stacking_output_dir)
        self._processing_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()

    async def start(self):
        """Start the image processor agent."""
        self._running = True
        self._output_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Image Processor started — output: %s", self._output_dir)

        self.bus.subscribe("transfer_complete", self._on_transfer_complete)

        # Run processing loop
        await self._processing_loop()

    async def stop(self):
        self._running = False

    async def _on_transfer_complete(self, msg: AgentMessage):
        """Handle a transferred image — add to group for stacking."""
        local_path = msg.payload.get("local_path")
        target_name = msg.payload.get("target_name", "Unknown")

        if not local_path:
            return

        # Determine filter from directory structure (organized by librarian)
        path = Path(local_path)
        filter_name = path.parent.name  # e.g., "Ha", "L", "R"

        if target_name not in self._image_groups:
            self._image_groups[target_name] = {}
        if filter_name not in self._image_groups[target_name]:
            self._image_groups[target_name][filter_name] = []

        self._image_groups[target_name][filter_name].append(local_path)

        count = len(self._image_groups[target_name][filter_name])
        logger.info(
            "Added to group: %s/%s — now %d subs",
            target_name, filter_name, count,
        )

        # Report progress to scheduler
        await self._report_progress(target_name)

        # Auto-stack when we have enough subs (minimum 5 for meaningful stack)
        if count >= 5 and count % 5 == 0:
            await self._processing_queue.put((target_name, filter_name))

    async def _processing_loop(self):
        """Process stacking requests from the queue."""
        while self._running:
            try:
                target, filter_name = await asyncio.wait_for(
                    self._processing_queue.get(), timeout=60
                )
                await self._stack_filter(target, filter_name)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error("Processing loop error: %s", e)
                await asyncio.sleep(5)

    async def _stack_filter(self, target_name: str, filter_name: str):
        """Stack all subs for a target/filter combination using Siril."""
        images = self._image_groups.get(target_name, {}).get(filter_name, [])
        if len(images) < 3:
            return

        logger.info(
            "Stacking %d subs for %s/%s",
            len(images), target_name, filter_name,
        )

        output_dir = self._output_dir / target_name / filter_name
        output_dir.mkdir(parents=True, exist_ok=True)

        # Generate Siril processing script
        script = self._generate_siril_script(images, output_dir, target_name, filter_name)
        script_path = output_dir / "stack.ssf"
        script_path.write_text(script)

        # Run Siril
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [self.config.siril_path, "-s", str(script_path)],
                capture_output=True,
                text=True,
                timeout=3600,  # 1 hour max
            )

            if result.returncode == 0:
                logger.info("Stack complete: %s/%s", target_name, filter_name)
                stacked_file = output_dir / f"{target_name}_{filter_name}_stacked.fit"

                await self.bus.publish(AgentMessage(
                    sender=AgentRole.PROCESSOR,
                    recipient=AgentRole.SCHEDULER,
                    msg_type="stack_complete",
                    payload={
                        "target": target_name,
                        "filter": filter_name,
                        "stacked_file": str(stacked_file),
                        "sub_count": len(images),
                    },
                ))
            else:
                logger.error(
                    "Siril stacking failed for %s/%s: %s",
                    target_name, filter_name, result.stderr[:500],
                )

        except FileNotFoundError:
            logger.warning(
                "Siril not found at '%s' — skipping stack. "
                "Install Siril or set PS_SIRIL_PATH.",
                self.config.siril_path,
            )
        except subprocess.TimeoutExpired:
            logger.error("Siril stacking timed out for %s/%s", target_name, filter_name)

    def _generate_siril_script(
        self,
        image_paths: list[str],
        output_dir: Path,
        target_name: str,
        filter_name: str,
    ) -> str:
        """Generate a Siril processing script for stacking.

        Siril script format (.ssf) automates:
        1. Convert to Siril internal format
        2. Register (align) frames
        3. Stack with sigma-clipping rejection
        4. Save result
        """
        # Create a file list for Siril
        filelist_path = output_dir / "filelist.txt"
        with open(filelist_path, "w") as f:
            for img_path in image_paths:
                f.write(f"{img_path}\n")

        stacked_name = f"{target_name}_{filter_name}_stacked"

        script = f"""# PhotonScript auto-generated Siril stacking script
# Target: {target_name} | Filter: {filter_name} | Subs: {len(image_paths)}
# Generated: {datetime.utcnow().isoformat()}

requires 1.2.0

cd {output_dir}

# Convert input files
convert light -out=./process
cd ./process

# Register (align) all frames
register pp_light

# Stack with Winsorized sigma clipping (good for rejecting satellites/planes)
stack r_pp_light rej 3 3 -norm=addscale -output_norm -out=../{stacked_name}

# Apply auto-stretch for preview
cd ..
load {stacked_name}
autostretch
save {stacked_name}_preview

close
"""
        return script

    async def _report_progress(self, target_name: str):
        """Report acquisition progress back to the scheduler."""
        if target_name not in self._image_groups:
            return

        filter_counts = {
            filt: len(paths)
            for filt, paths in self._image_groups[target_name].items()
        }

        await self.bus.publish(AgentMessage(
            sender=AgentRole.PROCESSOR,
            recipient=AgentRole.SCHEDULER,
            msg_type="acquisition_progress",
            payload={
                "target": target_name,
                "filter_counts": filter_counts,
                "total_subs": sum(filter_counts.values()),
            },
        ))

    def get_summary(self) -> dict:
        """Return a summary of all processed targets."""
        summary = {}
        for target, filters in self._image_groups.items():
            summary[target] = {
                "filters": {f: len(paths) for f, paths in filters.items()},
                "total_subs": sum(len(paths) for paths in filters.values()),
            }
        return summary


class PixInsightProcessor:
    """Optional PixInsight integration for advanced processing.

    Uses PixInsight's command-line interface to run scripts for:
    - Weighted Batch Pre-Processing (WBPP)
    - Image integration
    - Dynamic background extraction
    - Deconvolution
    - Noise reduction
    """

    def __init__(self, pixinsight_path: str):
        self.pi_path = pixinsight_path

    async def run_wbpp(
        self,
        image_paths: list[str],
        output_dir: str,
        target_name: str,
    ) -> Optional[str]:
        """Run PixInsight WBPP (Weighted Batch PreProcessing)."""
        if not self.pi_path:
            logger.info("PixInsight path not configured — skipping WBPP")
            return None

        # Generate PixInsight JavaScript for WBPP
        script = self._generate_wbpp_script(image_paths, output_dir, target_name)
        script_path = Path(output_dir) / "wbpp_script.js"
        script_path.write_text(script)

        try:
            result = await asyncio.to_thread(
                subprocess.run,
                [self.pi_path, "--run", str(script_path)],
                capture_output=True,
                text=True,
                timeout=7200,  # 2 hours
            )
            if result.returncode == 0:
                return str(Path(output_dir) / f"{target_name}_integrated.xisf")
            else:
                logger.error("PixInsight WBPP failed: %s", result.stderr[:500])
                return None
        except Exception as e:
            logger.error("PixInsight error: %s", e)
            return None

    def _generate_wbpp_script(
        self,
        image_paths: list[str],
        output_dir: str,
        target_name: str,
    ) -> str:
        """Generate a PixInsight WBPP JavaScript automation script."""
        files_array = ", ".join(f'"{p}"' for p in image_paths)
        return f"""
// PhotonScript PixInsight WBPP Automation
// Target: {target_name}
// Generated: {datetime.utcnow().isoformat()}

#include <pjsr/DataType.jsh>

var P = new WeightedBatchPreprocessing;
P.outputDirectory = "{output_dir}";
P.lightFrames = [{files_array}];
P.calibrateOnly = false;
P.generateDrizzleData = true;
P.executeGlobal();
"""
