"""Application configuration loaded from environment / config file."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings
from pydantic import Field

from photonscript.shared.models import ObservatoryLocation, TransferWindow


# The checkout this package runs from (photonscript/shared/config.py -> repo).
REPO_ROOT = Path(__file__).resolve().parents[2]


class PhotonScriptConfig(BaseSettings):
    """Master configuration for the entire PhotonScript system."""

    # The repo's .env is read wherever the command runs from (a CLI started in
    # C:\Users\jeremy used to see code defaults: ecc 0.70 instead of 0.6).
    # A .env in the current directory still wins (pydantic: later file wins).
    model_config = {"env_prefix": "PS_",
                    "env_file": (str(REPO_ROOT / ".env"), ".env"),
                    "extra": "ignore"}

    # --- General ---
    app_name: str = "PhotonScript"
    data_dir: Path = Path.home() / ".photonscript"
    db_path: Path = Path.home() / ".photonscript" / "photonscript.db"
    log_level: str = "INFO"
    # PS-55: never download astropy IERS data from inside the service (use the
    # table bundled in astropy-iers-data; refresh it with pip, in daytime)
    iers_offline: bool = True
    # PS-55: on Windows, opt out of power throttling (EcoQoS) and lift a
    # below-normal CPU/memory priority to normal at startup
    process_qos_guard: bool = True
    # PS-58: after an update the supervisor waits this long for /api/health
    # to report the new SHA before the wrapper rolls back to the previous one
    update_verify_s: int = 90
    # PS-58: also run the fast test subset in the staging checkout before
    # switching code (needs pytest in the scope venv; skipped if missing)
    update_smoke_tests: bool = False

    # --- Observatory ---
    observatory_name: str = "AARO Pier 3 (Rodeo, NM)"
    observatory_lat: float = 31.906944
    observatory_lon: float = -109.021367
    observatory_elev: float = 1300.0  # metres; AARO Pier 3 per the Paramount (TheSky) driver, confirmed by Jeremy 2026-10-04
    observatory_tz: str = "America/Denver"
    observatory_bortle: int = 2

    # --- Scheduler Web UI ---
    scheduler_host: str = "0.0.0.0"
    scheduler_port: int = 8100

    # --- Telescope Agent ---
    nina_base_url: str = "http://localhost:1888/v2/api"  # NINA Advanced API (ninaAPI plugin)
    phd2_host: str = "localhost"
    phd2_port: int = 4400
    # PS-70: guide-camera plate scale. PHD2 reports guide errors in PIXELS; the
    # telescope agent asks PHD2 (get_pixel_scale) first and falls back to these
    # when the PHD2 profile has no focal length / pixel size.
    phd2_pixel_scale_arcsec: float = 0.0  # explicit guide "/px override; 0 = compute
    guide_camera_pixel_um: float = 2.0    # OGMA GP678C on the OAG (2.0 um pixels)
    guide_focal_length_mm: float = 0.0    # 0 = derive from pixel_scale_arcsec and
                                          # imaging_camera_pixel_um (0.24"/px at
                                          # 3.76 um = about 3230 mm; the OAG
                                          # shares the RC16 focal length)
    imaging_camera_pixel_um: float = 3.76  # RC16 imaging camera (AP26MC / IMX571)
    image_watch_dir: str = "C:\\Users\\jeremy\\Documents\\N.I.N.A"  # NINA output dir
    library_dir: str = ""  # accepted-lights library (Syncthing this); "" = <data_dir>/Library
    desktop_library_dir: str = r"C:\Users\sleep\ninashare\Library"  # the
    # Syncthing mirror on the DESKTOP - used only to build copy-able paths in
    # the UI (browsers cannot open File Explorer directly)
    dawn_flats_enabled: bool = True  # sky flats after imaging, before shutdown
    dawn_flats_window_min: int = 40  # PS-36: the armer's dawn shutdown waits
    # until nautical dawn + 5 + this many minutes (capped at sunrise) whenever dawn
    # flats are expected, so it no longer parks/warms both rigs before the flat
    # window opens. Astro->nautical dawn is 28-37 min at AARO, so the old astro
    # dawn + 30 shutdown ALWAYS fired before nautical dawn + 5. Unsafe at that
    # point = shut down at once (no flats possible).
    meridian_guard_min: int = 20  # don't open the run on a target crossing the
    # meridian within this many minutes of dark-start (avoids an immediate flip
    # + recenter failure); it's reordered to image after the meridian instead
    campaign_min_alt_deg: float = 30.0  # PS-30: campaign planner altitude floor
    # (deg) for a target's usable 10-min slots; a project's min_alt_deg overrides.
    # Flat 30 deg at AARO Pier 3 (Jeremy, 2026-09-26: no roof/terrain profile).
    campaign_notify: bool = True  # PS-30 (absorbs PS-14): Pushover once when a
    # campaign goal turns `complete` or `lights_done_needs_calibration`
    auto_stale_flats: bool = True  # at dawn, also reshoot flats for filters whose
    # library set has gone stale (>45d) even if tonight didn't image them — keeps
    # broadband flats fresh across runs of narrowband-only nights
    # --- Desktop transfer (Syncthing) ---
    syncthing_url: str = "http://localhost:8384"  # Syncthing REST on the scope PC
    syncthing_api_key: str = ""
    syncthing_folder_id: str = ""   # folder id of the Library share
    syncthing_device_id: str = ""   # the DESKTOP's device id
    sync_stall_min: int = 30        # alarm when the transfer batch's pending count
                                    # hasn't dropped in this many minutes while
                                    # still non-empty (a wedged transfer loop that
                                    # only reports the backlog). 0 = disable.
    # --- Calibration & library ---
    astap_exe: str = "C:\\Program Files\\astap\\astap.exe"  # plate-solve fallback for identify
    dark_target_count: int = 30  # dark-library quota per exposure length (current epoch)
    dark_exposures: str = "600,180"  # exposures (s) the dark library should hold, at the setpoint temp
    library_archive_before: str = ""  # YYYY-MM-DD: nights before this stay out of the
                                      # Library (archived); the archive tool records it too
    library_archive_dir: str = ""     # archive root outside the share; "" = <share parent>/NINAArchive
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
    # PS-113: calibration capture + QA (scheduler/calibration_qa.py,
    # calibration_capture.py). Bad frames never enter the Library or a master.
    calibration_qa_mode: str = "quarantine"  # off (file every frame, no QA) |
                                 # report (QA + verdicts, file every frame) |
                                 # quarantine (failing frames go to
                                 # Library/.../Calibration/_quarantine/ with
                                 # the reasons; NINA's originals untouched)
    calibration_temp_tol_c: float = 1.0  # darks / bias: CCD-TEMP within SET-TEMP
                                 # +/- this; a capture job also waits for it
                                 # before the first exposure and stops on drift
    calibration_capture_budget_min: float = 240.0  # one capture job's time
                                 # budget (cooling included); the plan is
                                 # trimmed to fit and the job stops at it
    calibration_autofill: bool = False  # daytime auto-fill: when the sun is up
                                 # and the armer idle, start one gap-driven
                                 # darks + bias job per rig per day (ends 2 h
                                 # before sunset). Never at night. Off = only
                                 # the Capture now button / CLI start a job
    calibration_owed_lookback_days: int = 60  # PS-122: the Calibration owed
                                 # view reads lights of active goals from the
                                 # last this many nights (report only)
    # --- Log directories (remote 2 AM triage) ---
    nina_logs_dir: str = "C:\\Users\\jeremy\\AppData\\Local\\NINA\\Logs"
    piggyback_nina_logs_dir: str = ""  # NINA #2 (OSC) log dir. Empty = same dir as
                                    # nina_logs_dir (both NINAs log to one folder;
                                    # /api/nina/log picks each rig's file by the
                                    # Advanced API port its log says it listens on).
    phd2_logs_dir: str = "C:\\Users\\jeremy\\Documents\\PHD2"  # PHD2 GuideLog +
    # DebugLog dir (PHD2 default). Lets the dashboard tail guiding remotely —
    # RMS, star-lost, calibration — the same way nina_logs_dir does for NINA.
    ascom_logs_dir: str = "C:\\Users\\jeremy\\Documents\\ASCOM"  # ASCOM trace-log
    # base (TraceLogger writes dated subfolders here); enable Trace in the driver
    # setup to capture the safety-monitor client's HTTP/exception detail
    # --- Quality gates (per-sub grading) ---
    pixel_scale_arcsec: float = 0.24  # RC16 3248mm + ASI2600 native
    # PS-81: imaging sensor size (px) for the Targets page field-of-view boxes.
    # The AP26MC and AP26CC both write NAXIS1 x NAXIS2 = 6224 x 4168 (FITS
    # headers, 2026-09-26). Display only; nothing is gated on them.
    sensor_width_px: int = 6224
    sensor_height_px: int = 4168
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
    optics_corner_alert: bool = False  # PS-95: the daily "corner FWHM spread"
                                 # Pushover from the live grader. Off: it fired
                                 # most nights (0.45 to 0.99 on 09-26, trailing
                                 # inflates it); the persistent tilt/collimation
                                 # finding from the optics trend replaces it.
                                 # corner_spread is still stored on records.
    optics_min_stars_zone: int = 8  # PS-95 optics report: stars needed in a
                                 # 3x3 zone before it is measured
    optics_tilt_warn: float = 1.20  # PS-95: softest / sharpest corner FWHM
                                 # ratio that counts as tilt (with the sharp
                                 # spot off center)
    # PS-71 parked / roof-closed frame signatures (shared.qa_signatures):
    quality_fwhm_min_arcsec: float = 1.0  # physical floor: no RC16 star is
                                 # sharper than ~1" (seeing + 3248 mm optics;
                                 # good subs read 2-3.5"). Below it the
                                 # "stars" are hot pixels: the 2026-09-26
                                 # roof-closed subs read 0.57" / HFR 1.5 px.
                                 # HFR is judged as FWHM ~ 2 x HFR x scale.
    quality_bias_floor_margin_adu: float = 6.0  # background <= default_offset
                                 # + this = the sensor saw no sky (bias +
                                 # dark current only; 2026-09-26: 257 ADU)
    quality_bias_floor_min_exp_s: float = 600.0  # background at the floor
                                 # rejects ON ITS OWN only from this length:
                                 # short narrowband subs legitimately sit at
                                 # the floor (09-26 Ha 60 s: 257 ADU with
                                 # 156 real stars; 900 s NB runs 269+). Below
                                 # it the floor only rejects together with
                                 # sub-physical star sizes.
    quality_reject_unsafe_subs: bool = True  # reject a light whose exposure
                                 # overlaps a window the safety monitor read
                                 # UNSAFE (safety history, PS-71)
    # PS-21 unified QA rules (shared.qa_rules: one scorecard for live and
    # backfill grading). Limits above stay the single source; these add the
    # ones that used to be hard-coded plus the scorecard behavior.
    quality_star_min: int = 5      # fewer detected stars = cloud / no sky
    quality_star_max: int = 5000   # more = defocus donuts / false detections
    qa_warn_fraction: float = 0.10  # yellow "near the limit" band on the max
                                 # gates (ecc, HFR, FWHM, guide RMS); 0 = off
    qa_background_rel_max: float = 2.0  # warn above this x the night's median
                                 # background for the same rig/target/filter
    qa_hfr_outlier_factor: float = 1.4  # reject HFR above this x the night's
                                 # median for the same rig/target/filter
    qa_night_min_subs: int = 5    # subs needed before the night-median checks
    qa_tracking_jump_max: float = 0.25  # doubled-star fraction = mount jump
    # PS-67 "On target" check: the sub's position (plate solve, else the
    # mount position from the header / mount log) vs the named target.
    # Normal RC16 offsets are 2 to 5'. Approved 2026-09-27: reject when the
    # target is out of the frame, flag (needs a look) in between.
    pointing_off_target_flag_arcmin: float = 8.0     # RC16: warn above
    pointing_off_target_reject_arcmin: float = 15.0  # RC16: reject above
    piggyback_off_target_flag_arcmin: float = 30.0   # Piggy-600: warn above
    piggyback_off_target_reject_arcmin: float = 60.0  # Piggy-600: reject above
    qa_pointing_mode: str = "fail"  # off-target above the reject limit:
                                 # fail (reject, approved) | warn | info
    pointing_header_reject_deg: float = 5.0  # PS-107: an offset with no
                                 # plate solve (header, mount log) rejects
                                 # only above this (gross miss); between
                                 # the flag limit and this it warns
    # PS-13 "Clear of RC16 moves": a Piggy-600 sub exposing through an RC16
    # slew, meridian flip or park (mount log, else the RC16 frames)
    qa_slew_straddle_mode: str = "fail"  # fail (reject) | warn | info
    slew_gate_pad_s: float = 10.0  # padding each side of a mount-log move
    slew_gate_min_move_arcmin: float = 5.0  # RC16 frames (older nights): a
                                 # pointing change this big between frames
                                 # is a move
    # PS-67 pointing record: mount log, plate-solve sampling at dawn
    mount_log_enabled: bool = True  # RC16 agent appends runs/<night>_mount.jsonl
                                 # from the poll it already makes (read-only)
    mount_log_move_arcmin: float = 1.0  # log a line when the mount moved this far
    mount_log_heartbeat_s: int = 60  # and at least this often while tracking
    pointing_solve_policy: str = "sampled"  # dawn ASTAP pass: sampled (every
                                 # Nth + flagged + first after a slew) |
                                 # all | off
    pointing_solve_every: int = 10  # sampled: every Nth sub per rig
    pointing_solve_budget_min: float = 30.0  # stop the dawn solve pass after
                                 # this many minutes of ASTAP time
    qa_auto_approve: bool = True  # all-green subs are approved (reviewed)
                                 # automatically; the reviewer can override
    qa_auto_approve_rigs: str = "rc16"  # rigs whose all-green subs auto-
                                 # approve (comma list; empty = every rig).
                                 # Jeremy 2026-09-27: RC16 only for now; the
                                 # Piggy-600 stays manual.
    qa_target_overrides: str = ""  # PS-48 hook: JSON {"<target>" or
                                 # "<target>|<filter>" or "<rig>:<target>":
                                 # {"hfr_max": 6.0, ...}} tighter per target
    qa_guide_rms_mode: str = "info"  # guide RMS check: info (recorded, not
                                 # judged) | warn | fail. "info" until PS-70
                                 # puts the PHD2 RMS in real arcsec
    qa_guide_lock_mode: str = "warn"  # PS-91: a sub guided on a non-star lock
                                 # (guard episode overlapping it): warn | fail
    qa_star_sidecar_max: int = 500  # PS-80 star sidecar: brightest N stars
                                 # per sub for the review overlay; 0 = off
    # PS-94: eccentricity at the 2x2-binned scale (0.48"/px on the RC16,
    # the scale the _bin2 masters integrate at) next to the native 0.24"/px.
    # Report-only until the ecc-scale-report numbers are in.
    qa_ecc_binned: bool = True   # live grader also measures a 2x2-binned
                                 # copy of each RC16 sub (ecc_bin, hfr_bin)
    qa_ecc_scale: str = "native"  # which scale gates: native | binned (the
                                 # other is recorded as info only)
    quality_eccentricity_max_binned: float = 0.0  # gate at 0.48"/px when
                                 # qa_ecc_scale=binned; 0 = same as
                                 # quality_eccentricity_max
    # PS-108: 0 to 100 sub score (shared.qa_score, weights per rig in
    # config/qa/score_weights.toml). Approved 2026-10-04: approve at 80 and
    # up on both rigs, review 60 to 79, reject under 60; shipped in preview.
    qa_score_mode: str = "preview"  # preview (record + show what it would do,
                                 # change no verdict) | on (the score sets the
                                 # verdict and replaces qa_auto_approve /
                                 # qa_auto_approve_rigs). Human verdicts win.
    qa_score_approve: float = 80.0  # auto-approve at this score and above
    qa_score_reject: float = 60.0   # reject below this score
    qa_score_weights_file: str = ""  # empty = config/qa/score_weights.toml
    qa_saturation_adu: float = 65000.0  # a pixel at or above this counts as
                                 # saturated (FITS SATURATE wins when present);
                                 # same level as the PS-21 clipping gates

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
    # --- Guiding, autofocus & sequence narration ---
    guided_default: bool = True  # PHD2 guiding on by default (2026-07-07): unguided
                                 # 300s at 3248mm lost 30-60% of frames to trailing.
                                 # Guiding enables 600s subs. Set PS_GUIDED_DEFAULT=false
                                 # to run unguided on the Paramount MX (TPoint +
                                 # ProTrack) by default.
    unguided_dither: bool = False  # PS-66: keep dithering on unguided nights
                                 # through NINA's Direct Guider (mount pulses).
                                 # Off until NINA's guider is switched to Direct
                                 # Guider; the armer checks the connected guider
                                 # at arm and drops dithers (one note) otherwise.
    unguided_max_exposure_s: float = 300.0  # PS-66: RC16 sub-length cap on every
                                 # unguided target (armed unguided or the
                                 # unguided fallback): a 600 s set becomes 300 s
                                 # x twice the count, same integration. Projects
                                 # are credited in seconds (ExposurePlan.
                                 # acquired_s). Set from the PS-84 tracking-test
                                 # verdict. 0 = no cap.
    guiding_force_first_calibration: bool = False  # PS-72 (2026-09-27): default off;
                                 # a forced cal at a high-Dec first target wrecked
                                 # 2026-09-26. When True, the night's FIRST guided
                                 # target sets StartGuiding.ForceCalibration so
                                 # PHD2 gets one fresh cal to settle against;
                                 # every later target relies on Auto-restore.
                                 # Set false to NEVER force — always trust PHD2's
                                 # restored calibration (avoids a failed first-cal
                                 # loop, but risks guiding on a stale/absent cal).
    guiding_watchdog_grace_min: int = 20  # after dusk, give guiding this long to
                                 # start (slew->center->AF->calibrate->settle)
                                 # before the not-guiding watchdog can trip.
    pushover_verbosity: str = "normal"  # how chatty the sequence's Pushover
                                 # narration is. "verbose" = every step incl the
                                 # per-block "starting/done" pair (2×/filter/
                                 # target — the bulk of the noise); "normal"
                                 # (default) drops per-block but keeps per-target
                                 # steps + milestones; "quiet" drops the
                                 # per-target step lines too, leaving night
                                 # milestones (start, unsafe/safe, target done,
                                 # shutdown). PhotonScript's own watchdog alerts
                                 # (notify()) are unaffected by this.
    autofocus_filter: str = "L"  # filter PhotonScript switches to for the AFs it
                                 # EMITS (twilight startup AF + each target's
                                 # start-of-target AF) so autofocus runs on bright
                                 # broadband, never a 3nm narrowband filter that
                                 # starves the star field ("Stars detected: 1" ->
                                 # no HFR curve -> donuts). Empty = focus in the
                                 # imaging filter (old behavior). NOTE: NINA's own
                                 # AF-After-Filter-Change / HFR / temp TRIGGERS run
                                 # AF too and obey NINA's *global* Autofocus Filter
                                 # option — set that to L (+ per-filter offsets) so
                                 # the triggered AFs also focus on broadband.
    focus_filter_offsets: str = "Ha:-187,OIII:-187,SII:-187"  # EAF steps from
                                 # the autofocus_filter's best focus to each
                                 # imaging filter's (PS-65). Every filter block
                                 # autofocuses on autofocus_filter, then applies
                                 # this as a MoveFocuserRelative. -187 is the
                                 # 2026-07-03 paired measurement in
                                 # focus_seeds.json (L 6040 at 28.8C vs NB 5853 at
                                 # 28.3C). Unlisted filters (R/G/B) get 0. Set it
                                 # EMPTY if NINA's own profile filter offsets are
                                 # turned on, so the offset is not applied twice.
    nina_autofocus_reports_dir: str = ""  # path to NINA's AutoFocus report *.json
                                 # folder (e.g. %LOCALAPPDATA%/NINA/AutoFocus). Set
                                 # it to enable the post-night AF-quality alert
                                 # below; empty = alert disabled.
    af_min_r2: float = 0.7  # an AF run whose best fit R^2 is below this (a
                                 # too-few-stars / bad-curve run) trips the AF
                                 # quality alert in the nightly backfill.
    focus_model_seed: bool = True  # PS-76: seed each RC16 autofocus from the
                                 # learned focus model (focus_model.py) when it
                                 # is at least "med" confidence for that filter,
                                 # instead of the July focus_seeds table. The
                                 # model only learns from NINA AF reports, so
                                 # this does nothing until
                                 # nina_autofocus_reports_dir is set. AF always
                                 # still runs after the seed.
    focus_model_min_af_points: int = 5  # PS-76: an AF report with fewer measure
                                 # points than this never enters the focus model.
    # PS-76 follow-up: both NINAs write AF reports into one folder. Which rig
    # a report belongs to: empty = built-in rule (RC16 filter-wheel name and
    # RC16 EAF range 4000-7000; else Piggy-600). Otherwise ';'-separated
    # clauses, all must hold: field~a|b (substring), field=a|b (exact),
    # field:lo-hi (number). Fields: filter, position, temp, file, any (whole
    # report text) or a report key path. E.g. "any~AP26MC" once NINA's
    # reports are seen to name the camera.
    focus_model_rc16_match: str = ""
    focus_model_piggyback_match: str = ""
    focus_model_piggyback: bool = True  # also keep a (read-only) Piggy-600
                                 # focus model from its AF reports
    guiding_auto_recover: bool = True  # when the watchdog sees guiding stay down
                                 # (idle OR stuck calibrating/looping) well past
                                 # the grace, attempt ONE automatic PHD2 guider
                                 # restart (stop+start, no forced cal so
                                 # Auto-restore reuses a good calibration) to
                                 # break a stuck loop before escalating. Set false
                                 # to warn/escalate only and never touch guiding.
    # PS-91 non-star lock guard (telescope_agent.guide_guard): the RC16 agent
    # watches PHD2 live for a hot-pixel / non-star lock, PHD2 guiding a parked
    # or closed-roof scope, and max pulses that move nothing.
    guard_enabled: bool = True   # detect, log episodes, mark subs, alert once
                                 # per night (observe-only unless the next is on)
    guard_auto_recover: bool = False  # on a non-star lock, re-select a vetted
                                 # real star and resume guiding (D3: stop PHD2).
                                 # Off for the first guarded night (observe-only)
    guard_on_fail: str = "alert"  # recovery failed: "alert" (one push per night)
                                 # or "unguided" (armer.fallback_unguided: re-
                                 # dispatch the rest unguided). Keep "alert"
                                 # until PS-85 caps unguided sub lengths
    guide_min_star_hfd_px: float = 1.5  # D1: a guide "star" under this HFD
                                 # (PHD2 px at bin 2; scaled by 2 / binning) is
                                 # checked for a one-pixel profile
    # PS-92 pulse-path self-test (telescope_agent.pulse_selftest): does the
    # mount move on a guide pulse? Run from NINA ExternalScript slots (after
    # the twilight AF, and before each guided target's StartGuiding).
    phd2_selftest_enabled: bool = False  # insert the NINA slots + lint them.
                                 # Off until the first manual twilight run
                                 # (POST /api/phd2/selftest/run) looks right
    phd2_selftest_script: str = "C:\\astro\\PhotonScript\\deploy\\phd2-selftest.cmd"
    selftest_step_px: float = 10.0   # aim each pulse at about this many px
    selftest_steps: int = 3          # pulses per direction (W, E, N, S)
    selftest_ratio_min: float = 0.5  # observed/expected below this = FAIL
    selftest_ratio_max: float = 1.5  # above this = WARN (guide-rate mismatch)
    selftest_timeout_s: int = 240    # hard stop (INCONCLUSIVE)
    guide_rate_sidereal: float = 0.5  # fallback guide speed (x sidereal) when
                                 # neither NINA nor the PHD2 log reports one
    selftest_on_fail: str = "alert"  # FAIL: "alert" (one push per night) or
                                 # "unguided" (armer.fallback_unguided). Keep
                                 # "alert" until PS-85 caps unguided subs
    phd2_hotpix_max_age_days: float = 7.0  # rebuild the guide-camera hot-pixel
                                 # map after this (or a binning/exposure change)
    # PS-93 PHD2 calibration manager (scheduler.phd2_calibration +
    # telescope_agent.phd2_calmanager): calibrate near Dec +5 by the meridian
    # in a PHD2_CALIBRATION slot when one is needed, grade it, retry once.
    phd2_cal_mode: str = "auto"  # auto: a slot only when needs_calibration
                                 # says so (none on record, FAIL, too old,
                                 # profile/binning changed, manual request);
                                 # always: every guided night (~4 min of
                                 # twilight); never: no slot (the PS-72 behavior)
    phd2_cal_max_age_days: float = 30.0  # recalibrate after this many days
    phd2_cal_hold_s: int = 240   # hold after the slot: the agent grades and
                                 # retries once inside it (needs 150 s left)
    phd2_cal_fail_action: str = "keep"  # second failed grade: "keep" guiding
                                 # on the poor calibration and alert once, or
                                 # "unguided" (armer.fallback_unguided; keep
                                 # "keep" until PS-85 caps unguided subs)
    phd2_flip_action: str = "alert"  # Dec runs away after a meridian flip:
                                 # "alert" (one push per night with the Reverse
                                 # Dec fix) or "off" (record only). In-place
                                 # recalibration is not built (PS-93 approval)
    # PS-89 PHD2 settings audit (scheduler.phd2_audit): compare PHD2, its
    # stored profile, the guide log, NINA and the dark library with
    # config/phd2/desired_oag_rc16.toml at every guided arm.
    phd2_audit_enabled: bool = True   # audit at a guided arm (one push only
                                 # on a FAIL) and on PHD2 ConfigurationChange
    phd2_audit_autofix: bool = False  # allow registry profile writes (PHD2
                                 # closed, armer idle, backup first). Off until
                                 # one daytime round trip has been checked
    phd2_desired_file: str = ""  # desired-state TOML; "" = the repo's
                                 # config/phd2/desired_oag_rc16.toml
    phd2_dark_max_age_days: float = 30.0  # PHD2 dark library older = WARN
    phd2_darks_dir: str = ""     # PHD2 dark library folder; "" =
                                 # %LOCALAPPDATA%\phd2\darks_defects
    pe_owner: str = "protrack"   # who corrects periodic error: protrack
                                 # (TheSky; PHD2 PPEC must be off) | phd2_ppec
                                 # | none
    # PS-90 guide-star auto-tune (telescope_agent.guide_tuner +
    # scheduler.phd2_tuning): measure the guide star after each settle and
    # (mode exposure) step PHD2's exposure toward a bright, unclipped peak.
    phd2_tune_mode: str = "observe"  # off | observe (measure and record only,
                                 # never set_exposure) | exposure (live
                                 # exposure-only tuning). Gain / binning change
                                 # only pre-dusk via the PS-89 profile writer
                                 # (behind phd2_audit_autofix)
    phd2_tune_peak_lo: float = 0.60  # target band for the star's peak as a
    phd2_tune_peak_hi: float = 0.80  # fraction of full scale (aims at the middle)
    phd2_tune_snr_min: float = 20.0  # a guide star under this SNR is "faint"
    phd2_tune_hfd_px: str = "2,5"    # HFD target band (guide px at the binning)
    phd2_tune_exp_ms: str = "1000,4000"  # exposures the tuner may pick (ms)
    phd2_guide_full_scale_adu: int = 65535  # guide camera full scale (16-bit)
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
    pushover_quiet_daytime: bool = True        # sun up: no heartbeats, 1 per title per window
    pushover_daytime_title_window_h: float = 4.0
    pushover_daytime_sun_alt_deg: float = -3.0  # "daytime" = sun above this altitude
    pushover_emergency_retry_s: int = 300      # priority-2 pushes repeat this often until
                                              # acknowledged (Pushover requires retry+expire)
    pushover_emergency_expire_s: int = 1800    # ...and stop repeating after this (max 10800)
    # --- Guiding alert collapse (PS-66) ---
    guiding_alert_repeat_min: float = 30.0     # after a "guiding lost" push, hold further
                                              # lost/auto-recovery pushes this long
    guiding_recovered_push_min: float = 10.0   # push "recovered" only if the loss lasted
                                              # at least this long (else audit at -1)
    guiding_flap_count: int = 3                # this many losses inside the flap window
                                              # -> one "guiding flapping: N losses" push
    guiding_flap_window_min: float = 60.0
    safety_monitor_device_id: str = ""  # NINA #1 chooser Id to (re)connect, e.g.
                                        # ASCOM.AlpacaDynamic3.SafetyMonitor. "" = the
                                        # device last seen connected (auto-learned).
    piggyback_safety_monitor_device_id: str = ""  # same, for NINA #2 (the OSC)
    safety_watchdog_sun_alt_deg: float = -3.0  # the reconnect watchdog idles while the
                                               # sun is above this AND nothing is armed
    safety_disconnect_repeat_min: int = 60     # repeat the safety-DISCONNECTED push every N min
    # Safety-flap debounce baked into the generated NINA sequence: after the sky
    # reads safe again it must STAY safe this long before the sequence unparks,
    # resumes and narrates. Kills the safe/unsafe Pushover storm + mount thrash.
    safety_confirm_seconds: int = 120
    # PS-77 defense in depth: when the safety monitor has read UNSAFE for this
    # long and NINA's sequence tree still shows SAFE_LOOP running (the night
    # loop never left imaging), the armer stops the sequence, stops guiding
    # and parks, keeping the cooler at setpoint; when it has been safe for
    # safety_confirm_seconds it re-dispatches the remainder. The grace covers
    # NINA's own interrupt (5 s watchdog) plus the UNSAFE branch's park.
    unsafe_stop_enabled: bool = True
    unsafe_stop_grace_s: int = 120
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
    piggyback_sensor_width_px: int = 0   # PS-81 FOV box; 0 = sensor_width_px
    piggyback_sensor_height_px: int = 0  # PS-81 FOV box; 0 = sensor_height_px
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
    piggyback_fwhm_max: float = 15.0  # arcsec (PS-114; was 6.0). The RC16's 4.0" gate
                                      # rejected every piggyback sub on 2026-09-19 and the
                                      # 6.0" one cost every normal Piggy sub score points
                                      # ("FWHM 11.3\" > 6\"", -4.9). The live OSC measure
                                      # (sep second moments on 2x2 superpixels, 2.58"/px)
                                      # reads the 190 accepted Piggy-600 Library subs
                                      # (2026-09-21 to 10-04, re-measured on the desktop)
                                      # at median 7.5" (120 s) and 9.5 to 10.9" (M 31,
                                      # 400 s), 95th percentile 10.6", normal max 12.0".
                                      # 15" = 12" / 0.8: the score gives full marks up to
                                      # 80% of a gate, so normal 8 to 12" stars cost
                                      # nothing. It also matches the 4.5 px HFR gate
                                      # (FWHM reads ~1.2 x 2 x HFR x 1.29 = 13.8").
                                      # Every accepted sub above 14" had ecc 0.87+
                                      # (trailing). Advisory on this rig.
    piggyback_fwhm_min_arcsec: float = 2.0  # PS-71 physical floor at 1.29"/px
                                      # (focused OSC stars read 4.8-5.7")
    piggyback_ecc_max: float = 0.75   # OSC wide-field tolerates a touch more elongation
                                      # than the RC16 close-up; overrides quality_eccentricity_max
    piggyback_fwhm_soft: bool = True  # OSC FWHM is advisory, not a hard reject: the
                                      # estimator is inflated by extended bright objects
                                      # (galaxies/nebulae), so a tight-HFR sub can read a
                                      # large FWHM and still be sharp. HFR + ecc are the
                                      # real gates for this rig; FWHM stays a score factor.
                                      # Sets quality_fwhm_soft on the piggyback rig view.
    # PS-114: the rest of the Piggy-600's QA gates (shared.rigs.PIGGYBACK_GATES).
    # None (blank) = same as the RC16 key, so nothing changes until a value
    # is set; `photonscript qa-baselines --rig piggyback` proposes one.
    piggyback_star_min: Optional[int] = None    # RC16: quality_star_min
    piggyback_star_max: Optional[int] = None    # RC16: quality_star_max (the
                                      # OSC measure keeps at most 400 stars,
                                      # so this max cannot trip on this rig)
    piggyback_background_rel_max: Optional[float] = None  # qa_background_rel_max
    piggyback_hfr_outlier_factor: Optional[float] = None  # qa_hfr_outlier_factor
    piggyback_tracking_rms_max: Optional[float] = None    # quality_tracking_rms_max
    piggyback_tracking_jump_max: Optional[float] = None   # qa_tracking_jump_max
    piggyback_corner_spread_max: Optional[float] = None   # quality_corner_spread_max
    piggyback_bias_floor_margin_adu: Optional[float] = None  # quality_bias_floor_margin_adu
    qa_baseline_k: float = 3.0  # PS-114 qa-baselines: proposed gate = median
                                # + k x robust sigma (1.4826 x MAD) of the
                                # rig's accepted subs; 3 keeps ~99.7% of a
                                # normal night inside the gate. Report only.
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
    piggyback_flat_count: int = 25  # PS-36: OSC dawn sky flats per night (20-30
                                    # target), at the OSC gain/offset
    piggyback_flat_wait_min: int = 25  # PS-36: after nautical dawn + 5, wait at most
                                       # until nautical dawn + this for the roof to be
                                       # safe; later = skip the OSC flats (not wedge)
    piggyback_af_temp_change_c: float = 1.5  # PS-68: OSC refocus on this focuser
                                             # temperature change (C)
    piggyback_af_hfr_increase_pct: float = 10.0  # PS-68: OSC refocus when HFR rises
                                                 # this % over the post-AF baseline
    piggyback_af_interval_min: int = 60  # PS-68: OSC periodic refocus (min). NINA #2
                                         # can't see the RC16's meridian flip or a bad
                                         # AF (HFR trigger baselines on it), so this is
                                         # the in-sequence repair. 0 = off.
    piggyback_resume_grace_s: int = 300  # PS-25: after NINA #2 reads safe it holds
                                         # safety_confirm_seconds + this before its
                                         # AF and lights, so the OSC does not shoot
                                         # while NINA #1 unparks, slews, focuses and
                                         # centers. Ends early at nautical dawn.
    flexure_warn_arcsec_min: float = 0.5  # PS-96: flag a night when the Piggy-600
                                      # drifts this much faster than the RC16
                                      # ("/min; 0.5 = ~0.8 px per 120 s OSC sub).
                                      # Report only: no sub is rejected by it.
    flexure_solve_all: bool = False  # PS-96: plate-solve EVERY Piggy sub in the
                                     # daytime flexure pass (absolute track + PS-67
                                     # data, a few s of scope-PC CPU each). Off =
                                     # first/middle/last per block + one RC16 sub.
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
    cool_ramp_minutes: float = 0.0  # duration of the camera COOL ramp (mirror of
                                 # gradual_warm_minutes). 0 = drive straight to the
                                 # setpoint, no forced multi-minute ramp — the TEC
                                 # pulls down as fast as it can and the nanny below
                                 # verifies it got there. A ramp is what kept the
                                 # cooler "losing its mind" fighting arm/precool.
    cooler_nanny: bool = True    # active temperature reconciler: during the safe
                                 # imaging window (cool_lead before dark → dawn)
                                 # every rig's cooler must be ON and at setpoint. If
                                 # a rig is off or warm (the 2026-09-26 stuck-at-20°C
                                 # night that ruined the RC16 subs), drive it to
                                 # setpoint with an INSTANT cool and alert once. Set
                                 # false to disable the nanny entirely.
    cooler_stuck_minutes: int = 20  # nanny: alert when a rig is STILL warm this
                                 # long into the cold window even with the cooler
                                 # ON (wrong setpoint that won't take, weak TEC).
                                 # Re-asserting silently all night is how the
                                 # 2026-09-26 20°C night went unnoticed.
    sub_temp_over_setpoint_c: float = 5.0  # grading: reject a sub whose sensor
                                 # was more than this above the CONFIGURED rig
                                 # setpoint (never the header SET-TEMP, which is
                                 # whatever wrong setpoint the camera was given:
                                 # 2026-09-26 SET-TEMP=20 let 25°C subs pass)
    sub_temp_max_c: float = 10.0  # grading: absolute ceiling, reject any sub with
                                 # the sensor above this regardless of setpoint
    safety_monitor_watchdog: bool = True  # alert if the safety monitor reads
                                 # UNREADABLE (disconnected/erroring) for a while
                                 # during a run — roof gating is then blind (the
                                 # 2026-09-26 OSC Alpaca sim that "came off"). Off
                                 # = no alert. Imaging still rides NINA's own
                                 # SafetyMonitorCondition regardless.
    gradual_warm_minutes: float = 0.0  # duration of the camera warm ramp on cooler-off
                                 # (arm cooler-off, dawn shutdown, disarm make-safe, End
                                 # area). 0 = INSTANT: just release the setpoint / turn
                                 # the TEC off and let the sensor drift to ambient on its
                                 # own — no forced multi-minute ramp. A ramp fights an
                                 # arm/precool that wants to cool RIGHT NOW (it kept
                                 # pushing the temp back up), and the "gentler on the
                                 # sensor" argument doesn't hold up: cutting the TEC is
                                 # already a soft, passive warm. Set >0 only to bring the
                                 # old gradual ramp back.
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
    noon_arm_guided: bool = True  # auto/noon re-arm uses PHD2 guiding when set;
    # uncheck to have the hands-off re-arm run UNGUIDED (TPoint + ProTrack). Replaces the
    # old tri-state noon_arm_guiding string with a plain checkbox.
    noon_arm_guiding: str = "guided"  # (legacy) guiding mode for noon auto-arms:
                                      # "guided" | "unguided" (alias "encoders") |
                                      # "default" (config). Not read anywhere now.
    # --- TheSky64 direct hook (EXPERIMENTAL, 2026-09-21) ---
    # PhotonScript normally reaches the Paramount through NINA's ASCOM pass-through
    # (TheSky's connector), which does NOT expose TPoint/ProTrack. TheSky also runs
    # a TCP "TheSky TCP Server" (default :3040) that executes JavaScript; the
    # thesky_client module talks to it with read-only scripts (PS-104).
    # thesky_enabled stays the gate for anything that would write.
    thesky_enabled: bool = False
    thesky_tcp_host: str = "localhost"
    thesky_tcp_port: int = 3040
    # PS-104 TheSky / TPoint audit (scheduler.thesky_audit): report only, it
    # never writes TheSky, moves the mount or takes an image.
    thesky_audit_enabled: bool = True   # read-only audit (on demand, at arm,
                                 # night report); independent of thesky_enabled
    thesky_audit_imagelink_thesky: bool = False  # TheSky Image Link on a copied
                                 # frame from the CLI too (the Guiding tab
                                 # button runs it while the armer is idle)
    thesky_audit_allsky_read: bool = False  # read the All Sky flags through
                                 # DoCommand 12 / 13 (read form); on only after
                                 # the on-site script check (MAINTENANCE.md)
    tpoint_max_age_days: float = 90.0   # TPoint model older = rebuild
    tpoint_min_points: int = 50         # ProTrack's minimum per Bisque
    tpoint_rms_max_arcsec: float = 30.0
    tpoint_polar_max_arcmin: float = 2.0
    pointing_first_slew_warn_arcmin: float = 2.0  # NINA first-solve median
    pointing_first_slew_fail_arcmin: float = 5.0  # (14 nights, per side)
    thesky_manual_max_age_days: float = 30.0  # manual TPoint record older
                                 # reads unknown (re-enter after a session)
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

    def focus_offset_map(self) -> dict:
        """focus_filter_offsets -> {filter class: int steps}. Bad pairs are
        skipped, so a typo can never break sequence generation."""
        out = {}
        for pair in (self.focus_filter_offsets or "").split(","):
            if ":" in pair:
                cls, val = pair.split(":", 1)
                try:
                    out[cls.strip()] = round(float(val.strip()))
                except ValueError:
                    continue
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
