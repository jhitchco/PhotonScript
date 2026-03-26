"""Application configuration loaded from environment / config file."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings
from pydantic import Field

from photonscript.shared.models import ObservatoryLocation, TransferWindow


class PhotonScriptConfig(BaseSettings):
    """Master configuration for the entire PhotonScript system."""

    model_config = {"env_prefix": "PS_", "env_file": ".env", "extra": "ignore"}

    # --- General ---
    app_name: str = "PhotonScript"
    data_dir: Path = Path.home() / ".photonscript"
    db_path: Path = Path.home() / ".photonscript" / "photonscript.db"
    log_level: str = "INFO"

    # --- Observatory ---
    observatory_name: str = "New Mexico Remote"
    observatory_lat: float = 32.9
    observatory_lon: float = -105.5
    observatory_elev: float = 2200.0
    observatory_tz: str = "America/Denver"
    observatory_bortle: int = 2

    # --- Scheduler Web UI ---
    scheduler_host: str = "0.0.0.0"
    scheduler_port: int = 8100

    # --- Telescope Agent ---
    nina_base_url: str = "http://localhost:1888/api"  # NINA Advanced API
    phd2_host: str = "localhost"
    phd2_port: int = 4400
    image_watch_dir: str = "C:\\Astrophotography\\Tonight"  # NINA output dir
    quality_fwhm_max: float = 4.0  # arcsec
    quality_eccentricity_max: float = 0.6
    quality_tracking_rms_max: float = 2.0  # arcsec

    # --- Librarian ---
    remote_image_dir: str = "C:\\Astrophotography"
    local_image_dir: str = str(Path.home() / "Astrophotography")
    transfer_host: str = ""  # SSH host for remote telescope computer
    transfer_port: int = 22
    transfer_user: str = ""
    transfer_key_path: str = ""
    transfer_start_hour: int = 8
    transfer_end_hour: int = 18
    transfer_bandwidth_limit_mbps: float = 50.0

    # --- Image Processor ---
    siril_path: str = "siril-cli"
    pixinsight_path: str = ""  # optional
    stacking_output_dir: str = str(Path.home() / "Astrophotography" / "Processed")

    # --- AstroBin ---
    astrobin_api_key: str = ""
    astrobin_api_secret: str = ""

    def get_observatory(self) -> ObservatoryLocation:
        return ObservatoryLocation(
            name=self.observatory_name,
            latitude=self.observatory_lat,
            longitude=self.observatory_lon,
            elevation=self.observatory_elev,
            timezone=self.observatory_tz,
            bortle_class=self.observatory_bortle,
        )

    def get_transfer_window(self) -> TransferWindow:
        return TransferWindow(
            start_hour_local=self.transfer_start_hour,
            end_hour_local=self.transfer_end_hour,
            bandwidth_limit_mbps=self.transfer_bandwidth_limit_mbps,
        )
