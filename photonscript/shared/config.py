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
    observatory_name: str = "AARO Pier 3 (Rodeo, NM)"
    observatory_lat: float = 31.906944
    observatory_lon: float = -109.021367
    observatory_elev: float = 1250.0
    observatory_tz: str = "America/Denver"
    observatory_bortle: int = 2

    # --- Scheduler Web UI ---
    scheduler_host: str = "0.0.0.0"
    scheduler_port: int = 8100

    # --- Telescope Agent ---
    nina_base_url: str = "http://localhost:1888/v2/api"  # NINA Advanced API (ninaAPI plugin)
    phd2_host: str = "localhost"
    phd2_port: int = 4400
    image_watch_dir: str = "C:\\Users\\jeremy\\Documents\\N.I.N.A"  # NINA output dir
    nina_logs_dir: str = "C:\\Users\\jeremy\\AppData\\Local\\NINA\\Logs"
    pixel_scale_arcsec: float = 0.24  # RC16 3248mm + ASI2600 native
    quality_fwhm_max: float = 4.0  # arcsec
    quality_eccentricity_max: float = 0.6
    quality_tracking_rms_max: float = 1.5  # arcsec (0.24"/px scale)
    quality_corner_spread_max: float = 0.35  # corner FWHM spread vs median (collimation watch)

    # --- Imaging defaults (AARO) ---
    default_gain: int = 200
    default_offset: int = 50
    camera_setpoint_c: float = 0.0
    cooling_tolerance_c: float = 1.0
    guided_default: bool = False  # CEM70G absolute encoders: unguided is the default
    nb_exposure_s: float = 600.0  # narrowband subs: first-night data showed 300s
                                  # deeply read-noise-limited at f/8 + 3nm + SQM 23.9
    bb_exposure_s: float = 180.0  # broadband subs

    # --- Supervisor escalation ---
    pushover_user_key: str = ""
    pushover_api_token: str = ""
    consecutive_reject_limit: int = 3  # rejects in a row before severe alert
    auto_abort_on_severe: bool = False  # enable only after trusting the nanny
    heartbeat_minutes: int = 30
    arm_preconfig_lead_min: int = 30  # start cooling this many min before astro dark
    # Filter names as they appear in the NINA profile, mapped from our classes.
    # AARO wheel names its filters with single letters.
    nina_filter_names: str = "Ha:H,OIII:O,SII:S,L:L,R:R,G:G,B:B"
    utc_offset_hours: float = -6.0    # local display offset (MDT)

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

    def filter_name_map(self) -> dict:
        """Our filter class -> NINA profile filter name."""
        out = {}
        for pair in self.nina_filter_names.split(","):
            if ":" in pair:
                cls, name = pair.split(":", 1)
                out[cls.strip()] = name.strip()
        return out

    def reverse_filter_map(self) -> dict:
        """NINA profile filter name -> our filter class."""
        return {v: k for k, v in self.filter_name_map().items()}

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
