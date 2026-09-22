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
    library_dir: str = ""  # accepted-lights library (Syncthing this); "" = <data_dir>/Library
    desktop_library_dir: str = r"C:\Users\sleep\ninashare\Library"  # the
    # Syncthing mirror on the DESKTOP - used only to build copy-able paths in
    # the UI (browsers cannot open File Explorer directly)
    dawn_flats_enabled: bool = True  # sky flats after imaging, before shutdown
    syncthing_url: str = "http://localhost:8384"  # Syncthing REST on the scope PC
    syncthing_api_key: str = ""
    syncthing_folder_id: str = ""   # folder id of the Library share
    syncthing_device_id: str = ""   # the DESKTOP's device id
    astap_exe: str = "C:\\Program Files\\astap\\astap.exe"  # plate-solve fallback for identify
    dark_target_count: int = 30  # dark-library quota per exposure length (current epoch)
    dark_exposures: str = "600,180"  # exposures (s) the dark library should hold, at the setpoint temp
    library_cal_days: int = 120  # only calibration newer than this enters the library
    review_gate: bool = True  # subs need human approval before entering the library/transfer
    stamp_fits_object: bool = True  # write the resolved target name into a
                                    # blank FITS OBJECT header at capture (and
                                    # when identify attributes a sub) so
                                    # downstream tools + the runs page never see
                                    # target '?'. Never overwrites an existing
                                    # OBJECT.
    analysis_dropbox_subdir: str = "_analysis"  # subfolder of the library
                                    # Syncthing share used to hand individual
                                    # FITS to the desktop for off-scope analysis
    unsafe_darks_enabled: bool = True  # shoot darks while parked during unsafe pauses
    moon_aware_planning: bool = True  # nightly plan protects broadband on dark
                                      # (moonless) nights and defers it on bright
                                      # nights; see scheduler/moon.py tags
    bias_refresh_days: int = 60  # only capture the roof-closed 50-bias block if the
                                 # newest bias on disk is older than this (bias barely
                                 # ages: staleness is 180d). Was firing every unsafe
                                 # night and over-padding the library; 60 = ~every other
                                 # month. Set 30 for monthly, 0 to capture every night.
    flat_count: int = 15  # sky flats per filter at dawn
    nina_logs_dir: str = "C:\\Users\\jeremy\\AppData\\Local\\NINA\\Logs"
    ascom_logs_dir: str = "C:\\Users\\jeremy\\Documents\\ASCOM"  # ASCOM trace-log
    # base (TraceLogger writes dated subfolders here); enable Trace in the driver
    # setup to capture the safety-monitor client's HTTP/exception detail
    pixel_scale_arcsec: float = 0.24  # RC16 3248mm + ASI2600 native
    quality_fwhm_max: float = 4.0  # arcsec
    quality_fwhm_soft: bool = False  # False = FWHM is a hard reject gate (RC16).
                                     # True = advisory only (no reject); the OSC
                                     # piggyback sets this via rig_config so its
                                     # galaxy-inflated FWHM never bounces a tight-HFR
                                     # sub. HFR + ecc remain the hard gates.
    quality_hfr_abs_max: float = 10.0  # px. RC16 at 0.24"/px: 10px ~= 2.4" HFR,
                                       # consistent with the 4" FWHM gate. Was an
                                       # implicit 8px (getattr default) that rejected
                                       # soft-but-stackable subs whose stars NINA's own
                                       # HFR read ~1-1.5px lower (2026-09-17). Piggyback
                                       # overrides this to piggyback_hfr_abs_max in rigs.py.
    quality_eccentricity_max: float = 0.70  # was 0.60; raised 2026-09-17 to keep
                                             # mildly-trailed but stackable subs
                                             # (RC16 guided 600-900s). Loosens the
                                             # anti-trailing gate — watch for drift.
    quality_tracking_rms_max: float = 1.5  # arcsec (0.24"/px scale)
    quality_corner_spread_max: float = 0.35  # corner FWHM spread vs median (collimation watch)

    # --- Imaging defaults (AARO) ---
    default_gain: int = 200
    default_offset: int = 256  # bias floor ~= offset in ADU16; 50 was clipping
                                # the noise floor (bkg 51, sigma 15 -> left tail at 0)
    camera_setpoint_c: float = 0.0
    cooling_tolerance_c: float = 1.0
    camera_read_noise_adu: float = 4.1  # measured 2026-07-07: 4.07 ADU16 from the
                                        # library bias (3x50 frames, gain 200 LCG;
                                        # single-frame and pair-difference agree).
                                        # Floor for the exposure swamp score.
    auto_dusk_flats: bool = True  # auto-dispatch dusk sky flats for STALE
                                  # filters before auto-arm (the "checkmark")
    evening_forecast_enabled: bool = True  # push a night-viewing forecast a few
                                  # hours before sunset (tonight's rating, usable
                                  # dark hours, the astro-dark gate window, best
                                  # sky windows, moon). Runs from the auto-arm
                                  # loop independent of whether auto-arm is on.
    evening_forecast_lead_hours: float = 3.0  # how long before sunset to send it
    guided_default: bool = True  # PHD2 guiding on by default (2026-07-07): unguided
                                 # 300s at 3248mm lost 30-60% of frames to trailing.
                                 # Guiding enables 600s subs. Set PS_GUIDED_DEFAULT=false
                                 # to fall back to the Paramount MX encoders (+TPoint/
                                 # ProTrack) unguided.
    nb_exposure_s: float = 600.0  # narrowband subs: first-night data showed 300s
                                  # deeply read-noise-limited at f/8 + 3nm + SQM 23.9
    bb_exposure_s: float = 180.0  # broadband subs

    # --- Supervisor escalation ---
    pushover_user_key: str = ""
    pushover_api_token: str = ""
    consecutive_reject_limit: int = 3  # rejects in a row before severe alert
    auto_abort_on_severe: bool = False  # enable only after trusting the nanny
    heartbeat_minutes: int = 30
    # --- Pushover rate limiting (2026-09-12) ---
    pushover_ratelimit_enabled: bool = True   # False = old unthrottled behaviour
    pushover_dedup_window_s: int = 300        # drop identical (title,message) within this
    pushover_max_per_hour: int = 20           # rolling 1-hour burst cap (priority>=2 exempt)
    pushover_monthly_cap: int = 9000          # hard stop/month (headroom under Pushover's 10k)
    # Safety-flap debounce baked into the generated NINA sequence: after the sky
    # reads safe again it must STAY safe this long before the sequence unparks,
    # resumes and narrates. Kills the safe/unsafe Pushover storm + mount thrash.
    safety_confirm_seconds: int = 120
    connect_all_on_arm: bool = True  # on arm and on restart, actively connect
                                     # every device (esp. the safety monitor) so
                                     # a dead/slow device surfaces early. Connect
                                     # only — nothing moves; imaging still gated.
    # If the NINA safety monitor is DISCONNECTED (not merely unsafe) and cannot
    # be auto-reconnected while a sequence is RUNNING, stop the sequence. Off by
    # default: the watchdog escalates via Pushover and keeps retrying, but never
    # aborts a night on its own until you opt in. (2026-09-11: a disconnected
    # monitor let the rig image a closed roof for an hour of donuts.)
    safety_disconnect_aborts: bool = False
    # --- Piggyback rig: 2nd NINA instance (600mm + OGMA AP26CC, one-shot color) ---
    piggyback_enabled: bool = False  # turn on the 2nd-rig hooks (connect,
                                     # status, test-capture). Off until NINA #2
                                     # is up and confirmed.
    piggyback_name: str = "Piggy-600"
    piggyback_nina_base_url: str = "http://localhost:1889/v2/api"  # NINA #2 API
    piggyback_image_watch_dir: str = ""  # where NINA #2 writes FITS (set once known)
    piggyback_pixel_scale_arcsec: float = 1.29  # 600mm + IMX571 3.76um
    piggyback_default_gain: int = 100   # OGMA HCG-ish for OSC broadband
    piggyback_default_offset: int = 256
    piggyback_exposure_s: float = 120.0  # OSC default (DUAL_RIG.md §4.5)
    piggyback_focus_seed: int = 11045  # STATIC cold-start position for the OSC's
                                      # OWN focuser, used as the fallback before the
                                      # auto-harvester (piggyback_focus.py) has any
                                      # history. Seeded before the first AF so AF
                                      # starts near focus instead of failing to build
                                      # an HFR curve from a wild position (the
                                      # 2026-09-20 defocus night: FWHM 16.8"->6.5"
                                      # crept in over hours, 271/283 rejected). This
                                      # is a DIFFERENT EAF than the RC16's, so its
                                      # focus_seeds table cannot be reused. 11045 is
                                      # the good-focus position at the CAMERA's 0C
                                      # operating setpoint (Jeremy, 2026-09-21) — the
                                      # condition the OSC always images at
                                      # (piggyback_setpoint_c=0), NOT the daytime 21C
                                      # ambient. 0 = disabled until the harvester
                                      # learns one.
    piggyback_focus_harvest_max_hfr: float = 3.0  # only OSC subs at/below this real
                                      # HFR (px) feed the focus-seed store, so a soft
                                      # night never poisons the learned position.
    piggyback_focpos_min: int = 0     # OSC EAF travel clamp for harvested/seeded
    piggyback_focpos_max: int = 0     # positions. 0/0 = no clamp (set once the OSC
                                      # focuser's sane range is known).
    piggyback_image_lights: bool = True  # on arm, also shoot OSC lights while the
                                         # roof is open (needs the shared safety
                                         # monitor on NINA #2 to gate it). Off =
                                         # calibration companion only (old behavior).
    piggyback_hfr_abs_max: float = 4.5  # focused star ~2px at 1.29"/px (8px gate is wrong here)
    piggyback_fwhm_max: float = 6.0   # arcsec. The RC16's 4.0" gate is wrong for a
                                      # 1.29"/px wide-field OSC (a focused star is ~2.6"
                                      # FWHM; average seeing lands 4-6"). Applying 4.0"
                                      # rejected the entire piggyback set on 2026-09-19
                                      # ("FWHM 6.5\" > 4.0\""). Tune against real OSC subs.
    piggyback_ecc_max: float = 0.75   # OSC wide-field tolerates a touch more elongation
                                      # than the RC16 close-up; overrides quality_eccentricity_max
    piggyback_fwhm_soft: bool = True  # OSC FWHM is advisory, not a hard reject: the
                                      # estimator is inflated by extended bright objects
                                      # (galaxies/nebulae), so a tight-HFR sub can read a
                                      # large FWHM and still be sharp. HFR + ecc are the
                                      # real gates for this rig; FWHM stays a score factor.
                                      # Sets quality_fwhm_soft on the piggyback rig view.
    piggyback_setpoint_c: float = 0.0   # AP26CC cooling setpoint (it's a cooled cam)
    piggyback_library_dir: str = ""     # piggyback library subtree ("" = <main lib>/piggyback)
    piggyback_dark_exposures: str = "120"  # OSC dark-library exposures (s), match the OSC subs
    piggyback_calibrate_on_arm: bool = True  # on arm, also dispatch a calibration
                                     # companion to NINA #2 so ONE arm covers both
                                     # scopes: OSC dawn flats always, plus
                                     # roof-closed darks/bias whenever NINA #2 can
                                     # see the shared safety monitor. Whether it
                                     # can is AUTO-DETECTED at arm (connect + read
                                     # the NINA #2 safety monitor) — no manual flag.
    arm_preconfig_lead_min: int = 30  # dispatch the sequence this many min before dusk
    cool_lead_minutes: int = 30  # the night sequence turns the cooler + dew heater ON
                                 # this many min before astro dark (and not before),
                                 # so the camera is at setpoint the moment it's safe
                                 # to image. This is the "on 30 min before imaging"
                                 # lead; the dashboard shows a countdown to it.
    cooler_off_until_precool: bool = True  # on arm, force the cooler + dew heater OFF
                                 # (every rig) so they stay off from arm until the
                                 # sequence turns them on at cool_lead. Fresh-arm only
                                 # — never on restart, so a mid-night restart can't
                                 # kill cooling.
    # --- Auto-arm (hands-off multi-night) ---
    auto_arm_enabled: bool = False  # re-arm every night automatically (v2). Off by
                                    # default: opt in once you trust a night's run.
                                    # arm() rebuilds the plan from the store each time,
                                    # so this loop IS the nightly replan.
    auto_arm_lead_hours: float = 3.0  # arm window opens this long before pre-config;
                                      # recent enough that the preflight it runs
                                      # reflects real equipment state.
    auto_arm_require_preflight: bool = False  # False = arm-and-notify even on a
                                              # failing preflight (AARO roof controller
                                              # closes on weather independently). True =
                                              # skip-and-notify until preflight go=true.
    noon_arm_enabled: bool = True  # noon auto re-arm (2026-09-15): when the armer is
                                   # idle at 12:00 local, arm tonight's plan right
                                   # then instead of waiting for the evening window.
                                   # Also cooler belt #2: arm() forces cooler + dew
                                   # OFF, so a missed dawn shutdown is corrected at
                                   # noon at the latest.
    noon_arm_guiding: str = "guided"  # guiding mode for noon auto-arms:
                                      # "guided" | "encoders" | "default" (config)
    # --- TheSky64 direct hook (EXPERIMENTAL, 2026-09-21) ---
    # PhotonScript normally reaches the Paramount through NINA's ASCOM pass-through
    # (TheSky's connector), which does NOT expose TPoint/ProTrack. TheSky also runs
    # a TCP "TheSky TCP Server" (default :3040) that executes JavaScript; the
    # thesky_client module talks to it for pointing status and a (best-effort)
    # ProTrack toggle. Nothing in the nightly flow uses this yet — opt-in only.
    thesky_enabled: bool = False
    thesky_tcp_host: str = "localhost"
    thesky_tcp_port: int = 3040
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
