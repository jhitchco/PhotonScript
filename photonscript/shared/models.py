"""Core data models shared across all PhotonScript agents."""

from __future__ import annotations

import enum
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class FilterType(str, enum.Enum):
    LUMINANCE = "L"
    RED = "R"
    GREEN = "G"
    BLUE = "B"
    HA = "Ha"
    OIII = "OIII"
    SII = "SII"
    DARK = "Dark"
    FLAT = "Flat"
    BIAS = "Bias"


class TargetTier(str, enum.Enum):
    """Good / Better / Best ranking for target selection."""
    GOOD = "good"
    BETTER = "better"
    BEST = "best"


class ImageStatus(str, enum.Enum):
    CAPTURED = "captured"
    VALIDATED = "validated"
    REJECTED = "rejected"
    TRANSFERRED = "transferred"
    PROCESSED = "processed"
    STACKED = "stacked"


class AgentRole(str, enum.Enum):
    SCHEDULER = "scheduler"
    TELESCOPE = "telescope"
    LIBRARIAN = "librarian"
    PROCESSOR = "processor"


class SessionState(str, enum.Enum):
    IDLE = "idle"
    PLANNING = "planning"
    SEQUENCING = "sequencing"
    IMAGING = "imaging"
    PAUSED = "paused"
    PARKING = "parking"
    ERROR = "error"


class GuidingState(str, enum.Enum):
    STOPPED = "stopped"
    CALIBRATING = "calibrating"
    GUIDING = "guiding"
    SETTLING = "settling"
    LOST_STAR = "lost_star"
    ERROR = "error"


# ---------------------------------------------------------------------------
# Location & Observatory
# ---------------------------------------------------------------------------

class ObservatoryLocation(BaseModel):
    """Geographic location of the observatory."""
    name: str = "New Mexico Remote"
    latitude: float = 32.9  # degrees N — southern NM
    longitude: float = -105.5  # degrees W
    elevation: float = 2200.0  # meters
    timezone: str = "America/Denver"
    bortle_class: int = 2


# ---------------------------------------------------------------------------
# Target & Planning
# ---------------------------------------------------------------------------

class CelestialTarget(BaseModel):
    """A deep-sky target to image."""
    id: Optional[str] = None
    name: str
    catalog_id: str = ""  # e.g. "NGC 6992", "M 31"
    ra_hours: float  # right ascension in decimal hours
    dec_degrees: float  # declination in decimal degrees
    constellation: str = ""
    object_type: str = ""  # galaxy, nebula, cluster, etc.
    magnitude: Optional[float] = None
    angular_size_arcmin: Optional[float] = None
    tier: TargetTier = TargetTier.GOOD
    notes: str = ""
    astrobin_url: Optional[str] = None
    astrobin_image_count: int = 0
    recommended_total_hours: float = 10.0


class ExposurePlan(BaseModel):
    """Exposure plan for a single filter on a target."""
    filter_type: FilterType
    exposure_seconds: float = 300.0
    count: int = 20
    gain: int = 100
    offset: int = 50
    binning: int = 1
    acquired: int = 0  # how many already captured


class ImagingProject(BaseModel):
    """A full imaging project for one target with multiple filters."""
    id: Optional[str] = None
    target: CelestialTarget
    exposure_plans: list[ExposurePlan] = Field(default_factory=list)
    priority: int = 50  # 0-100, higher = more important
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    total_integration_hours: float = 0.0
    completion_pct: float = 0.0
    active: bool = True

    def compute_completion(self) -> float:
        total = sum(p.count for p in self.exposure_plans)
        acquired = sum(p.acquired for p in self.exposure_plans)
        if total == 0:
            return 0.0
        self.completion_pct = round(acquired / total * 100, 1)
        return self.completion_pct


# ---------------------------------------------------------------------------
# Image Metadata
# ---------------------------------------------------------------------------

class ImageQualityMetrics(BaseModel):
    """Quality metrics extracted from a captured sub-frame."""
    fwhm_arcsec: Optional[float] = None
    hfr_pixels: Optional[float] = None
    star_count: int = 0
    eccentricity: Optional[float] = None
    background_adu: Optional[float] = None
    noise_adu: Optional[float] = None
    snr: Optional[float] = None
    tracking_rms_arcsec: Optional[float] = None
    corner_spread: Optional[float] = None  # corner FWHM spread vs median (collimation/tilt watch)
    passed_qa: bool = True
    rejection_reason: str = ""


class CapturedImage(BaseModel):
    """Metadata for a single captured sub-frame."""
    id: Optional[str] = None
    project_id: str
    filename: str
    file_path: str
    file_size_bytes: int = 0
    target_name: str
    filter_type: FilterType
    exposure_seconds: float
    gain: int = 100
    offset: int = 50
    binning: int = 1
    captured_at: datetime = Field(default_factory=datetime.utcnow)
    camera_temp_c: Optional[float] = None
    status: ImageStatus = ImageStatus.CAPTURED
    quality: ImageQualityMetrics = Field(default_factory=ImageQualityMetrics)
    transferred: bool = False
    transferred_at: Optional[datetime] = None
    local_path: Optional[str] = None


# ---------------------------------------------------------------------------
# Guiding & Telescope State
# ---------------------------------------------------------------------------

class GuidingMetrics(BaseModel):
    """PHD2 guiding performance snapshot."""
    state: GuidingState = GuidingState.STOPPED
    rms_ra_arcsec: float = 0.0
    rms_dec_arcsec: float = 0.0
    rms_total_arcsec: float = 0.0
    peak_ra_arcsec: float = 0.0
    peak_dec_arcsec: float = 0.0
    snr: float = 0.0
    star_mass: float = 0.0
    guide_camera_exposure: float = 2.0


class TelescopeState(BaseModel):
    """Current state snapshot from the telescope agent."""
    session_state: SessionState = SessionState.IDLE
    current_target: Optional[str] = None
    current_filter: Optional[FilterType] = None
    current_exposure_progress: float = 0.0  # 0-1
    mount_ra: Optional[float] = None
    mount_dec: Optional[float] = None
    mount_tracking: bool = False
    guiding: GuidingMetrics = Field(default_factory=GuidingMetrics)
    camera_temp_c: Optional[float] = None
    camera_cooling_on: bool = False
    focuser_position: Optional[int] = None
    images_captured_tonight: int = 0
    last_image: Optional[CapturedImage] = None
    weather_safe: bool = True
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Transfer & Librarian
# ---------------------------------------------------------------------------

class TransferJob(BaseModel):
    """A file transfer job from remote to local."""
    id: Optional[str] = None
    image_id: str
    source_path: str
    dest_path: str
    file_size_bytes: int = 0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    transfer_rate_mbps: Optional[float] = None
    status: str = "pending"  # pending, in_progress, completed, failed
    error: str = ""


class TransferWindow(BaseModel):
    """Defines when transfers are allowed (daytime for bandwidth)."""
    start_hour_local: int = 8   # 8 AM local
    end_hour_local: int = 18    # 6 PM local
    max_concurrent: int = 1
    bandwidth_limit_mbps: Optional[float] = 50.0  # be kind on Starlink


# ---------------------------------------------------------------------------
# NINA Sequence
# ---------------------------------------------------------------------------

class NinaSequenceTarget(BaseModel):
    """Represents a target block within a NINA sequence file."""
    name: str
    ra_hours: float
    dec_degrees: float
    rotation: float = 0.0
    exposures: list[ExposurePlan] = Field(default_factory=list)
    slew_and_center: bool = True
    auto_focus_on_start: bool = True
    auto_focus_interval_minutes: int = 60
    meridian_flip: bool = True
    dither_every_n: int = 5
    start_guiding: bool = False  # CEM70G encoders: unguided default
    cool_camera: bool = True
    camera_temp_c: float = -10.0


class NinaSequenceFile(BaseModel):
    """Top-level NINA advanced sequencer file representation."""
    name: str
    targets: list[NinaSequenceTarget] = Field(default_factory=list)
    wait_for_altitude: float = 30.0  # minimum altitude degrees
    park_on_finish: bool = True
    warm_camera_on_finish: bool = True


# ---------------------------------------------------------------------------
# Agent Messages (inter-agent communication)
# ---------------------------------------------------------------------------

class AgentMessage(BaseModel):
    """Message passed between PhotonScript agents."""
    id: Optional[str] = None
    sender: AgentRole
    recipient: AgentRole
    msg_type: str  # e.g. "image_captured", "transfer_complete", "quality_report"
    payload: dict = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.utcnow)
