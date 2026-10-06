"""Core data models shared across all PhotonScript agents."""

from __future__ import annotations

import enum
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, model_validator


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
    OSC = "OSC"  # PS-30: one-shot color (Piggy-600), no filter wheel
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
    # PS-66: accepted long-set integration in seconds. `acquired` follows it
    # (floor(acquired_s / exposure_seconds)), so two 300 s subs make one 600 s
    # sub and a 400 s sub on a 120 s plan is 3 (PS-118: every sub, both rigs). Old projects.json files have no acquired_s: it is
    # seeded from acquired x exposure_seconds on load.
    acquired_s: float = 0.0
    hdr_short_seconds: Optional[float] = None  # shorter companion sub length (s)
    hdr_short_count: int = 0  # how many short subs; 0 = no HDR companion set
    hdr_short_acquired: int = 0  # accepted short subs so far (long set uses `acquired`)
    rig: str = "rc16"  # PS-30: which rig shoots this plan ("rc16" | "piggyback")

    @model_validator(mode="after")
    def _seed_acquired_s(self):
        # never below what `acquired` already says (old files, or a plan
        # rebuilt by allocate_exposures with only the count carried over)
        floor_s = self.acquired * self.exposure_seconds
        if self.acquired_s < floor_s:
            self.acquired_s = float(floor_s)
        return self

    def credit_seconds(self, seconds=None) -> float:
        """Seconds one accepted long-set sub is worth: its own length, on
        either rig and whether or not the night was guided (PS-118: the goal
        is seconds of integration, so a 400 s sub on a 120 s plan is 400 s,
        not one 120 s sub). A record without a usable length (old history,
        a missing exp_s) counts as one full sub of this plan's length."""
        try:
            s = float(seconds) if seconds else 0.0
        except (TypeError, ValueError):
            s = 0.0
        return s if s > 0 else float(self.exposure_seconds)

    def credit_long(self, seconds=None) -> None:
        """PS-66/PS-118: credit one accepted long-set sub (see credit_seconds)."""
        self.acquired_s += self.credit_seconds(seconds)
        self.acquired = max(self.acquired, self.subs_from_seconds(self.acquired_s))

    def long_seconds_done(self) -> float:
        """PS-118: accepted long-set seconds (never below acquired x length,
        the same floor the validator keeps). The number goal bars show."""
        return max(float(self.acquired_s or 0.0),
                   float(self.acquired * self.exposure_seconds))

    def subs_from_seconds(self, seconds: float) -> int:
        """Whole long subs' worth of `seconds` (1e-6 slack for float sums)."""
        if not self.exposure_seconds or self.exposure_seconds <= 0:
            return 0
        return int(seconds / self.exposure_seconds + 1e-6)

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
        # PS-66: a capped unguided long sub (e.g. 300 s on a 600 s plan with
        # 120 s shorts) can sit nearer the short length; anything 1.5x the
        # short length or more is a long sub.
        if s >= 1.5 * float(self.hdr_short_seconds):
            return False
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
    # PS-30 campaign planner v2. driving_rig: which rig the mount centers for
    # (stored and shown; the centering offset itself is PS-26). min_alt_deg:
    # per-target altitude floor, None = config.campaign_min_alt_deg.
    # require_calibration: a goal is only `complete` once matching flats,
    # darks and bias exist for every rig it uses.
    driving_rig: str = "rc16"
    min_alt_deg: Optional[float] = None
    require_calibration: bool = True
    # PS-26: where the driving Piggy-600 frame is centered (e.g. between M31
    # and M110); None = the target. Only used when driving_rig is piggyback.
    frame_center_ra_hours: Optional[float] = None
    frame_center_dec_degrees: Optional[float] = None
    # PS-111: a mosaic panel. One mosaic goal = the panels sharing mosaic
    # "id"; each panel is its own project (own coordinates, plans, seconds
    # crediting). Keys: id, name, panel (1-based capture order), row, col,
    # of (panel count), companion (project id of the goal whose Piggy-600
    # plan the passenger subs credit, or None), layout (the mosaic
    # definition, scheduler/mosaic.py). None = an ordinary target.
    mosaic: Optional[dict] = None
    # PS-117 (b) light budget (advisory; seconds stay the goal unit).
    # feature_signal_e_s: target signal above sky at the faintest feature to
    # show, e-/s per 2x2 pixel of feature_rig (None = the driving rig),
    # measured by `photonscript exposure-report`. goal_snr: SNR wanted there
    # per 2x2 pixel (None = config light_budget_goal_snr, 20).
    goal_snr: Optional[float] = None
    feature_signal_e_s: Optional[float] = None
    feature_rig: Optional[str] = None
    feature_note: str = ""
    # PS-48: per-target QA gate overrides, per rig: {"rc16": {"hfr_max": 6.0,
    # "fwhm_max": 2.5, "ecc_max": 0.5}, "piggyback": {...}}. None = the rig's
    # gates (PS-114). Read by shared.qa_rules.thresholds for every grader.
    qa_overrides: Optional[dict] = None

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
    # PS-117 (b): sky e-/s per pixel (G for OSC), per channel, RN penalty %
    sky_adu: Optional[dict] = None
    sky_e_s: Optional[float] = None
    sky_e_s_ch: Optional[dict] = None
    rn_penalty_pct: Optional[float] = None
    # PS-94: the same measure on a 2x2-binned copy (RC16 only; 0.47"/px)
    ecc_bin: Optional[float] = None        # sqrt(1-(b/a)^2), like eccentricity
    hfr_bin_px: Optional[float] = None     # binned HFR in native px (x2)
    stars_bin: Optional[int] = None
    # PS-146: what the judged ecc / FWHM came from (star_measure
    # SHAPE_RECORD_KEYS: ecc_all, ecc_bright, ecc_src, fwhm_src, ...)
    shape: Optional[dict] = None
    # PS-108: full-resolution pixel counts (shared.pixel_stats.frame_stats)
    sat_px: Optional[int] = None           # pixels >= sat_adu
    sat_px_pct: Optional[float] = None
    zero_px: Optional[int] = None          # pixels at 0 (black clip)
    zero_px_pct: Optional[float] = None
    max_adu: Optional[float] = None
    sat_adu: Optional[float] = None        # saturation level used
    bg_median: Optional[float] = None      # median of every 4th px
    bg_mad: Optional[float] = None         # its median absolute deviation
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
    rig: str = "rc16"   # PS-67: which agent sent it (piggyback owns no mount)
    session_state: SessionState = SessionState.IDLE
    current_target: Optional[str] = None
    current_filter: Optional[FilterType] = None
    current_exposure_progress: float = 0.0  # 0-1
    mount_ra: Optional[float] = None    # HOURS, as ninaAPI reports (not x15)
    mount_dec: Optional[float] = None   # degrees
    mount_alt: Optional[float] = None   # PS-121: degrees (None = not reported)
    mount_az: Optional[float] = None    # PS-121: degrees
    mount_connected: Optional[bool] = None  # PS-121: NINA #1 mount Connected
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
    start_guiding: bool = False  # unguided (Paramount MX, TPoint + ProTrack) unless set
    # PS-85: on a guided target, filter blocks that run unguided tonight (no
    # real guide star through that filter): each block then carries its own
    # StopGuiding / StartGuiding (nina_sequence_json._build_target_container)
    unguided_filters: list[str] = Field(default_factory=list)
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
    # PS-148: a through-focus optics test target (astigmatism / collimation
    # check): AF on L, then short subs at best focus and at each focuser
    # offset in every filter, back to best focus at the end. See
    # generate_optics_test_json() and scheduler/optics_test.py.
    optics_test: bool = False
    optics_test_filters: list[str] = Field(default_factory=list)
    optics_test_offsets: list[int] = Field(default_factory=list)
    optics_test_exposure_s: float = 45.0
    optics_test_nb_exposure_s: float = 120.0
    optics_test_repeats: int = 2
    # PS-26: the rig the mount centers for (the project's driving_rig), its
    # frame-center option and tonight's transit (UTC, from the planner). A
    # "piggyback" target centers the RC16 so the target lands mid Piggy-600
    # frame (scheduler/piggy_offset.py, config piggy_center_mode).
    driving_rig: str = "rc16"
    frame_center_ra_hours: Optional[float] = None
    frame_center_dec_degrees: Optional[float] = None
    transit_utc: Optional[datetime] = None
    # PS-111: a mosaic panel. repeat_while_up False = shoot tonight's owed
    # subs once, then hand the mount to the next panel (the imaging loop gets
    # LoopCondition(1)); the last panel of a mosaic tonight keeps the usual
    # repeat-while-safe-and-up loop. mosaic_note is shown as an annotation.
    repeat_while_up: bool = True
    mosaic_id: Optional[str] = None
    mosaic_note: str = ""


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
