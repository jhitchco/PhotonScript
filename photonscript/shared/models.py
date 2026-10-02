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
    """Exposure plan for a single filter on a target.

    HDR (high-dynamic-range) support: a filter may carry an optional SHORTER
    companion sub set alongside its main (long) subs — short subs keep bright
    cores from clipping (e.g. a planetary nebula's central star) while the long
    subs pull the faint shell. `exposure_seconds`/`count` describe the LONG set;
    `hdr_short_seconds`/`hdr_short_count` describe the short companion set. Both
    HDR fields are optional and default to "no HDR", so existing projects.json
    round-trips unchanged."""
    filter_type: FilterType
    exposure_seconds: float = 300.0
    count: int = 20
    gain: int = 100
    offset: int = 50
    binning: int = 1
    acquired: int = 0  # how many already captured
    hdr_short_seconds: Optional[float] = None  # shorter companion sub length (s)
    hdr_short_count: int = 0  # how many short subs; 0 = no HDR companion set
    hdr_short_acquired: int = 0  # accepted short subs so far (long set uses `acquired`)

    def short_remaining(self) -> int:
        if not self.hdr_short_count or not self.hdr_short_seconds:
            return 0
        return max(0, self.hdr_short_count - self.hdr_short_acquired)

    def is_short_exposure(self, seconds) -> bool:
        """True when a sub of `seconds` belongs to this plan's HDR short set
        (closer to the short length than to the long one)."""
        if not self.hdr_short_seconds or not self.hdr_short_count or not seconds:
            return False
        s = float(seconds)
        return abs(s - self.hdr_short_seconds) < abs(s - self.exposure_seconds)


class ImagingProject(BaseModel):
    """A full imaging project for one target with multiple filters."""
    id: Optional[str] = None
    target: CelestialTarget
    exposure_plans: list[ExposurePlan] = Field(default_factory=list)
    priority: int = 50  # 0-100, higher = more important
    budget_hours: float = 8.0  # total imaging time to dedicate; drives filter allocation
    filter_mix: Optional[dict] = None  # custom {filter: percent} split; None = type default
    hdr: Optional[dict] = None  # {filter_value: short_exposure_seconds} — request an
    # HDR short companion sub set on those filters (see ExposurePlan HDR fields).
    # None/{} = no HDR. This is the GENERIC "special plan per target" hook: a
    # target opts into HDR purely as data, no per-target code.
    exposure_overrides: Optional[dict] = None  # {filter_value: long_exposure_seconds}
    # — override the config default long-sub length for specific filters (else
    # config.nb_exposure_s / bb_exposure_s). None = use the global defaults.
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    total_integration_hours: float = 0.0
    completion_pct: float = 0.0
    active: bool = True

    def compute_completion(self) -> float:
        total = sum(p.count + (p.hdr_short_count if p.hdr_short_seconds else 0)
                    for p in self.exposure_plans)
        acquired = sum(p.acquired + (min(p.hdr_short_acquired, p.hdr_short_count)
                                     if p.hdr_short_seconds else 0)
                       for p in self.exposure_plans)
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
    clipped_pct: Optional[float] = None    # % pixels at/near full well
    sat_star_pct: Optional[float] = None   # % detected stars with saturated cores
    swamp_factor: Optional[float] = None   # background variance / read-noise variance
    exposure_flag: Optional[str] = None    # under / ok / sat-stars / clipped
    passed_qa: bool = True
    rejection_reason: str = ""
    # PS-80 star sidecar (shared.star_table.build); kept out of bus payloads
    star_table: Optional[dict] = Field(default=None, exclude=True)


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
    """PHD2 guiding performance snapshot.

    PS-70: PHD2 measures guide errors in guide-camera PIXELS. The *_px fields
    are always those pixels; the *_arcsec fields are pixels x
    pixel_scale_arcsec and are None when the scale is unknown (units="px").
    scale_source says where the scale came from: "phd2" (its profile) or
    "config" (guide_camera_pixel_um x binning / guide focal length)."""
    state: GuidingState = GuidingState.STOPPED
    rms_ra_arcsec: Optional[float] = 0.0
    rms_dec_arcsec: Optional[float] = 0.0
    rms_total_arcsec: Optional[float] = 0.0
    peak_ra_arcsec: Optional[float] = 0.0
    peak_dec_arcsec: Optional[float] = 0.0
    rms_ra_px: float = 0.0
    rms_dec_px: float = 0.0
    rms_total_px: float = 0.0
    samples: int = 0                      # guide steps in the RMS window
    units: str = "arcsec"                 # "arcsec" or "px" (scale unknown)
    pixel_scale_arcsec: Optional[float] = None
    scale_source: Optional[str] = None
    guide_binning: Optional[int] = None
    scale_warning: Optional[str] = None
    snr: float = 0.0
    star_mass: float = 0.0
    guide_camera_exposure: float = 2.0    # seconds; PS-90 sets it from get_exposure
    hfd_px: Optional[float] = None        # PS-90: last GuideStep HFD (guide px)
    error_code: int = 0                   # last GuideStep ErrorCode (1 = saturated)
    saturated: bool = False               # PHD2 flagged the star clipped


class TelescopeState(BaseModel):
    """Current state snapshot from the telescope agent."""
    session_state: SessionState = SessionState.IDLE
    current_target: Optional[str] = None
    current_filter: Optional[FilterType] = None
    current_exposure_progress: float = 0.0  # 0-1
    mount_ra: Optional[float] = None
    mount_dec: Optional[float] = None
    mount_tracking: bool = False
    mount_at_park: Optional[bool] = None       # PS-91 guard (D3)
    mount_slewing: Optional[bool] = None
    mount_side_of_pier: Optional[str] = None   # "East" | "West" (PS-92 flip watch)
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
    # PS-76: a focus-offset calibration target. Instead of imaging, it runs a
    # bracketed series of autofocus runs (L, R, G, B, L, Ha, OIII, SII, L by
    # default) so NINA's AF reports measure every filter's best focus against
    # L at nearly the same temperature. See generate_focus_calibration_json().
    focus_calibration: bool = False
    focus_calibration_rounds: int = 1
    focus_calibration_filters: list[str] = Field(default_factory=list)
    # PS-84: an unguided tracking test target (TPoint + ProTrack check).
    # Instead of the imaging plan it stops guiding, focuses on L, centers,
    # then shoots an exposure ladder (each length `tracking_test_repeats`
    # times) in every filter, re-centering between filters. See
    # generate_tracking_test_json() and scheduler/tracking_test.py.
    tracking_test: bool = False
    tracking_test_filters: list[str] = Field(default_factory=list)
    tracking_test_exposures: list[float] = Field(default_factory=list)
    tracking_test_repeats: int = 2


class NinaSequenceFile(BaseModel):
    """Top-level NINA advanced sequencer file representation."""
    name: str
    targets: list[NinaSequenceTarget] = Field(default_factory=list)
    wait_for_altitude: float = 30.0  # minimum altitude degrees
    wait_until_local: Optional[str] = None  # "HH:MM:SS" — WaitForTime gate before imaging
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
