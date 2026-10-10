# AARO Rig - Hardware & Site Reference

Known facts first, open questions at the bottom. Fill the TODOs in as they
get answered; the planner and QA thresholds should eventually read from here.

## Site
- Astronomy Acres Remote Observatories (AARO), Pier 3, Rodeo NM
- Sky: SQM ~23.9 (from first-night narrowband analysis)
- All-sky cam: https://allsky.astronomyacres.com · status: https://status.astronomyacres.com
- Scope PC on Tailscale: 100.94.189.77 (dashboard at https://teles-feb25.lobster-bleak.ts.net -> :8100; raw IP works on-tailnet)

## Optical train
- OTA: RC16 (406 mm) at 3248 mm f/8 (native, no reducer in use)
- Camera: OGMA AP26MC - IMX571 mono APS-C, 6224 x 4168, 3.76 um
- Image scale: 0.236"/px, measured by plate solves (TPoint run at 1x1 on
  2026-10-04, PS-97, PS-120; config pixel_scale_arcsec). FOV 0.408 x 0.273 deg
- Oversampled vs 2-3" seeing -> capture 1x1, software-bin 2x in integration
  (QA: ecc is measured at both 0.236"/px and the binned 0.47"/px since PS-94;
  the gate stays at native scale until `photonscript ecc-scale-report` on
  real nights says binning changes it. Switch with PS_QA_ECC_SCALE=binned
  and PS_QUALITY_ECCENTRICITY_MAX_BINNED.)
- Filters (NINA single-letter names -> canonical): L R G B + 3nm S(II) H(a) O(III)
- Camera setpoint 0 C, tolerance 1 C

## Mount & guiding
- Software Bisque PARAMOUNT MX with encoders (confirmed 2026-09-04; earlier
  docs wrongly said CEM70G). Driven via TheSky64 10.5 ASCOM driver.
  => TPoint model + ProTrack available: measures polar error precisely and
  corrects tracking rates in software - fully remote, no hands on the mount.
  Site (from driver): 31d54'25" N, -109d01'16", 1300 m.
- Historically unguided

#### TPoint record (add a line per model; PS-104)
Enter the same numbers in the Guiding tab's TPoint record form so the
TheSky / TPoint audit can judge them (model age, points, RMS, polar error).
| Date | Bin | Image scale ("/px) | Points | RMS (") | Polar error az / alt (') | Notes |
|------|-----|--------------------|--------|---------|--------------------------|-------|
| TODO | 4x4 | 0.942 | TODO | TODO | TODO | the model in HANDBOOK section 8; if it predates 2026-09-12 (dual-rig load change) the audit flags a rebuild once the date is entered |
| 2026-10-04 | ? | ? | 250 | 15.77 | MA -2.6 / ME -1.0 (total about 2.8) | TPoint advised no polar adjustment; ProTrack on |

#### Field rotation (PS-97)
Guiding tab, TheSky / TPoint section "Field rotation", `GET /api/rotation/report?nights=14`
or `photonscript rotation-report [--split 2026-10-04] [--ma M --me E] [--json]`.
Measured rate per rig and target block (star-sidecar registration, else stored
ASTAP angles) against what MA / ME from the TPoint record predict:
rate = 15.041 deg/h x eps[rad] x cos(H - H0) / cos(Dec), at most
15.041 x eps / cos(Dec). 2.8' gives at most 0.012 deg/h at Dec 0, 0.016 at
Dec 38, 0.031 at Dec 67. PS-96's 09-25 rates (0.05 to 0.10 deg/h) would need
about 9 to 10' at both Decs. Corner cost: 0.1 deg/h is about 1.1 px at an RC16
corner over a 600 s sub, about 39 px over 6 h (registration crops it).
- Unguided reality at 3248 mm: 300s subs lose 30-60% of frames to trailing
  (SII 9/22 through registration, 2026-07-03)
- PHD2 installed on scope PC; PS_GUIDED_DEFAULT=true as of 2026-07-07
  (StartGuiding after center, dither every 5, StopGuiding at unsafe/end)
- GUIDING COMMITTED TO THE OAG (GP678C on the RC16) as of 2026-09-12 — this
  frees the AP26CC (formerly the piggyback guide cam) to become the 600mm
  IMAGING camera. See the dual-rig plan below and docs/DUAL_RIG.md.
- PS-190: a ZWO ASI174MM Mini (IMX174 mono, 1936 x 1216, 5.86 um) replaces
  the GP678C (IMX678 color, 2.0 um) on the OAG (ordered 2026-10-10, AARO
  installs). PHD2 profile "Primary RC Profile (Guider)", id 2. New scale
  206.265 x 5.86 / 3248 = 0.372"/px at bin 1 (GP678C bin 2: 0.254"/px).
  config/phd2/desired_oag_rc16.toml knows both cameras ([[camera]]): the
  audit judges the connected one's rows (by the camera name PHD2 or the
  guide log reports), so both pass until the swap.

#### ASI174MM Mini swap checklist (PS-190)
Do these in order on the first session after AARO installs the camera.
1. Hardware: OAG focus. Loop in PHD2 (2 s, gain 40) on a bright star with
   the RC16 at imaging focus, and move the OAG camera's helical / spacer
   until the guide stars are sharp (HFD a few px at 0.372"/px). The IMX174
   sits at a different back-focus than the GP678C (check the spacer).
2. PHD2 profile: edit "Primary RC Profile (Guider)" (or make a new one and
   delete the old): Connect Equipment > camera = ZWO ASI174MM Mini (native
   ZWO driver); Brain > Camera: pixel size 5.86 um, binning 1, 16-bit,
   saturation by Max ADU 65535, gain 33 to 50 (PHD2 percent, about native
   200 to 300) to start; exposure 2 s; Brain > Guiding: search region 25 px,
   minimum star HFD 2.0 px, star mass detection off, multi-star on;
   Brain > Global: focal length 3248 mm.
3. PhotonScript config (System page): guide_camera_pixel_um = 5.86 (the
   config fallback scale; the audit, the guide logs and drift already take
   5.86 from the camera name). Leave phd2_pixel_scale_arcsec at 0.
4. Dark library: Darks > Dark library, roof closed, 1 to 4 s at the new
   gain and bin 1 (the GP678C library does not match the new sensor).
5. Bad-pixel (defect) map: Darks > Bad-pixel map at the same gain and
   binning, then auto-load it (or the dark library) on connect.
6. Calibration: clear the old calibration (it was at bin 2, 0.254"/px) and
   calibrate near Dec 0 (System page: Calibrate at the next dispatch,
   PS-93). The calibration step changes with the scale: use the PHD2
   calculator or the PS-93 recommended step.
7. Guiding Assistant: run it 2 to 3 minutes on the new camera, then set the
   RA / Dec min-move it recommends (the audit's ra_min_move / dec_min_move
   rows follow it).
8. NINA (NINA #1) Options > Equipment > Guider: settle pixels are guide
   pixels, so at 0.372"/px 0.5 to 1.0" is 1.3 to 2.7 px (was 2 to 3.9 px at
   0.254"/px): set about 2 px; settle timeout 60 s or more; dither pixels
   about 2/3 of the GP678C value for the same sky move.
9. Audit: GET /api/phd2/audit?refresh=1 (or arm guided): the camera row
   says "ZWO ASI174MM Mini: its rows are applied" and the guide pixel
   scale row passes at about 0.372"/px. Note the first calibration and
   typical RMS under section 1 below.

## Exposure & calibration standards
- NB 600s, BB 180s, gain 200, offset 256, 0 C ("NEW epoch", 2026-07-05+)
- OLD epoch (pre-2026-07-05 lights, e.g. Crescent 07-03): 300s, gain 200, offset 50
- Epoch = EXPTIME+GAIN+OFFSET+SET-TEMP+readout mode (READOUTM, PS-128);
  darks must match temperature, offset drift survivable via the 1000 DN
  calibration pedestal
- Readout mode: RC16 lights HCG since 2026-09-26 (gain 0.25 e-/ADU, RN
  5.66 ADU; LCG is 0.79 e-/ADU, RN 4.27 ADU). The bias of 07-08 / 07-31 and
  the 600 s darks of 07-04 / 07-28 are LCG (READOUTM checked 2026-10-05), so
  HCG bias and HCG 600 s darks are owed. Set in the NINA profile (readout
  mode for sequence images), not in the sequence. Piggy-600 AP26CC: LCG.
- Dark quota: PS_DARK_EXPOSURES x PS_DARK_TARGET_COUNT at the setpoint
- Flats: dusk NB-first -> L-last; dawn BB-first -> NB-last; target 50% histogram

#### Camera constants (PS-117, measured on Library frames 2026-10-04)
Config keys in brackets. The graders take the read noise for the frame's rig
and readout mode (READOUTM: "Low Conversion Gain" = LCG, anything else = HCG)
for the swamp factor; the QA thresholds did not change.
| Rig / mode | Read noise (ADU) | Gain (e-/ADU) | Source |
|------------|------------------|---------------|--------|
| RC16 AP26MC gain 200, HCG (lights since 09-26) | 5.66 = 1.4 e- [camera_read_noise_adu] | 0.25 [camera_gain_e_adu] | 09-26 180 s HCG dark pairs (upper bound); sky noise vs RN on 14 lights |
| RC16 AP26MC gain 200, LCG | 4.27 [camera_read_noise_lcg_adu] | 0.79 [camera_gain_lcg_e_adu] | 07-31 bias; 07-04 flat pairs |
| Piggy-600 AP26CC gain 100, offset 256, LCG, 0 C | 3.27 = 2.4 e- [piggyback_read_noise_adu] | 0.74 [piggyback_gain_e_adu] | 16 bias pair differences; 5 flat pairs |

Light budget (PS-117 part b) also subtracts a bias level and dark current
from every sub's sky:
| Rig | Bias (ADU) | Dark at 0 C (e-/s per px) | Source |
|-----|------------|---------------------------|--------|
| RC16 AP26MC HCG | 256 [camera_bias_adu] | 0 = not measured [camera_dark_e_s] | offset 256 stands in; HCG bias and darks owed |
| Piggy-600 AP26CC | 256.5 [piggyback_bias_adu] | 0.023 [piggyback_dark_e_s] | 16-bias master; 120 s dark +3.8 ADU over bias |

Re-measure from the Library calibration frames with
`photonscript exposure-report --camera-cal` (read-only; prints the keys to
compare). Run 2026-10-06 on the desktop mirror: Piggy-600 RN 3.28 ADU, gain
0.72 e-/ADU, dark 0.024 e-/s; RC16 LCG RN 4.20 ADU, gain 0.79 e-/ADU (no
HCG bias in the Library yet). Change a key on the System page by hand when a
value moves (Choice E).

#### Light budget: sky, sub length and SNR per target (PS-117 part b)
- Every graded sub records `sky_e_s` (sky e-/s per pixel: median of the
  darkest 10% of 64 px block medians, bias and dark removed; the G channel
  on the Piggy-600, all three in `sky_e_s_ch`) and `rn_penalty_pct` (how
  much read noise adds to that pixel's per-sub noise). Older subs get them
  from the stored background with `qa-rescore --date D --apply`
  (approximate, `sky_src` "background").
- Targets page "Light budget" (`GET /api/target/light-budget?name=`): per
  rig + filter the sky spread, measured overhead per sub and acceptance per
  length, a per-length table (RN penalty, sky / RN^2, SNR per hour, hours
  to the SNR goal) and an advisory length; with a feature signal, the share
  of the light the SNR goal needs ((SNR now / goal)^2) next to the hours bar.
- Per target: `goal_snr` (default `light_budget_goal_snr` 20 per 2x2 pixel
  at the faintest feature) and `feature_signal_e_s` (e-/s per 2x2 pixel
  there, measured with `photonscript exposure-report --target T
  --feature-arcmin R`; set with PATCH /api/projects2/<id>). M31: 0.19 at
  the outer disk 60' from the core. Advisory only: the goal stays seconds.

## Piggy-600 Wanderer Cover V4-EC (PS-189)
- WandererAstro Wanderer Cover V4-EC 150 mm on the Piggy-600 (Askar FRA600):
  motorized dust cover with a built-in EL flat panel. ASCOM CoverCalibrator
  driver; in NINA #2 it is the "Flat Panel" device (ninaAPI
  /equipment/flatdevice/...). Ordered 2026-10-10, AARO installs.
- Everything sits behind `piggyback_cover_enabled` (default false: no
  sequence or job changes until it is on). On:
  - companion Start: connect the Flat Panel, panel off, cover CLOSED; the
    OSC darks / bias run with the cover closed (no roof gate; capped at
    astro dusk on a lights night)
  - each OSC image pass: OpenCover, then deploy\cover-gate.cmd checks the
    cover reads Open (ninaAPI flatdevice info) for at most
    `piggyback_cover_gate_timeout_s` (180); not open = the pass is skipped
    (no light through a closed cover) and one page per night. The gate does
    not fail open: with the service down the CLI reads NINA #2 itself.
  - each light-loop pass closes the cover while the roof reads unsafe; the
    next safe pass opens it again
  - panel flats at astro dawn (no sky, no roof; replaces the PS-163 dawn sky
    flats): cover closed, light on, `piggyback_panel_brightness`,
    `piggyback_flat_count` (25) flats; `piggyback_panel_flat_mode` auto =
    NINA AutoExposureFlat (exposure sought for a 50% histogram, like the Flat
    Wizard, at most `piggyback_panel_flat_max_s`), fixed =
    `piggyback_panel_flat_exposure_s`; light off
  - End area, dawn shutdown, PS-77 unsafe stop, paused unsafe park: panel
    off + cover closed (closed by day)
  - calibration capture on the Piggy-600: a closed cover (panel off) is
    light-tight, so daytime darks no longer need the roof closed; the job
    closes the cover itself, the PS-113 daytime probe and leak checks still
    run, and a cover that opens mid-job stops it
- Dashboard: Piggy-600 pane, "Cover" row (cover state, panel on / off,
  waiting / skipped gate note). `GET /api/cover/gate` for the gate log.
- One-time setup after the install (before turning it on):
  1. NINA #2 profile: Equipment > Flat Panel = the Wanderer Cover V4-EC
     ASCOM driver; connect; open / close / light on / off by hand once.
     Note MinBrightness / MaxBrightness (ninaAPI flatdevice info) and set
     `piggyback_panel_brightness` inside it.
  2. Optional: NINA Flat Wizard on the panel at the OSC gain 100 / offset
     256 to find a brightness that gives 1 to 5 s exposures.
  3. Confirm deploy\cover-gate.cmd is on the scope PC
     (`piggyback_cover_script`).
  4. If the Piggy-600 daytime capture state is "disabled" from a roof-era
     leak, reset it (calibration-qa --rig piggyback --reset-daytime) so the
     probe re-runs on the cover.
  5. Set `piggyback_cover_enabled` true on the System page.
- Cover record (add a line per change):
| Date | What | Notes |
|------|------|-------|
| TODO | installed | brightness range, open / close time |

## Desktop processing
- PixInsight: C:\Program Files\PixInsight\bin\PixInsight.exe
- Staging: C:\Users\sleep\Astrophotography\Staging\<Target>
- Library mirror (receive-only): D:\ninashare\Library (C:\Users\sleep\ninashare
  becomes a junction to it; the integrator's integration_library_dir, PS-161)

## TODO - unknowns to fill in (answers unblock real decisions)

### 1. Guide optics  [ANSWERED 2026-07-08 - commissioning in progress]
Three cameras visible to PHD2 (2.6.14, mount via ASCOM TheSky driver):
- GP678C: guide cam on the OFF-AXIS GUIDER (RC16, 3248mm) - mechanical
  state/focus unverified. THE right answer long-term: no differential
  flexure, sees mirror shifts, ~0.13"/px guide scale.
- AP26CC: color cam on a 600mm PIGGYBACK scope (existing PHD2 profile
  "Piggy Back - 600mm") - easy stars, but 5.4x focal mismatch to the RC16;
  differential flexure is the risk on 600s subs.
- AP26MC: main imaging camera (never select in PHD2).
DECISION 2026-09-12 (dual-rig): guide EXCLUSIVELY on the OAG GP678C; promote the
AP26CC to the 600mm piggyback IMAGING camera (OGMA, one-shot color, own
motorized focuser on a separate COM/USB port). Confirm PHD2 selects GP678C only
and never the AP26CC. Full design in docs/DUAL_RIG.md.
Plan: separate PHD2 profiles per guide path with correct focal lengths
(OAG-RC16-3248 / PiggyBack-600); build a PHD2 dark library for the guide cam
(hot pixels = fake guide stars - the idle "21 arcsec RMS" artifact); twilight
OAG test, piggyback fallback; record first calibration + typical RMS here:
- First guided-night calibration result / typical RMS: TODO

### 2. Safety monitor  [ANSWERED 2026-07-08 from NINA log]
- ASCOM Alpaca: "AARO Safety Obs 2" (ASCOM.AlpacaDynamic1.SafetyMonitor v1.0)
- Latency between "clouds/rain" and roof close: TODO

### 3. NINA install  [ANSWERED 2026-07-08 from NINA log]
- NINA 3.2.0.9001 · profile "RC16"
- Plugins seen: Ground Station (Pushover), Hocus Focus (auto-updates)
- Mount driver: ASCOM.SoftwareBisque (through TheSky) · FW: ASCOM.OGMAVision
- Guider: "PHD2_Single" at 127.0.0.1:4400 - connects cleanly
- Plate solve: L filter, 10s, bin 2x2 (PixelSize 7.52), gain 300, search 30deg,
  5 attempts x 0.5 min. ROOT CAUSE 2026-07-07: primary solver was PlateSolve2
  (Regions=5000) which HUNG >6h on a cloud frame after the roof closed
  mid-recenter; blind failover was misconfigured (ASPS dropdown pointing at
  astap.exe) so it never fired. FIX: primary + blind solver = ASTAP
  (C:\Program Files\astap\astap.exe) - fails fast, recenter aborts in ~5 min
  and the safety condition takes over. Verify ASTAP star DB (D50/G05) present.

### 4. PixInsight add-ons  [processing pipeline scope]
- StarXTerminator: yes/no · NoiseXTerminator: yes/no · BlurXTerminator: yes/no
  (2026-10-10, Jeremy: licensed. Desktop has doc/tools/BlurXTerminator and
  src/scripts/rc-astro/BatchStarXTerminator.js but no *XTerminator* module
  in bin yet; the finish detects the processes at run time, PS-186)
- Other licensed tools:

### 5. Horizon & slew limits  [planner usable-hours accuracy]
- Obstructions by azimuth (deg alt at N/NE/E/SE/S/SW/W/NW):
- Mount altitude/meridian limits configured in NINA:

### 6. Failure recovery  [2 AM runbook]
- Remote power cycling (smart PDU? which outlets?):
- If NINA hangs / Tailscale drops:
- AARO support contact + hours:

### 7. Optical quirks  [QA threshold tuning]
- Collimation history, known tilt/corner behavior: see the log below.
- Focuser model, backlash, per-filter focus offsets:

#### Reading the optics report (PS-95)
Runs page "Optics (tilt / collimation)" section, `GET /api/optics/report?date=`,
or `photonscript optics-report --date D [--json]`. Passive: it reads the PS-80
star sidecars, no sky time. Trend: `GET /api/optics/trend?nights=30`.
- Grid: 3x3 zones as the runs-page thumbnail shows them (row 0 = top of the
  image). Number = median FWHM-eq in arcsec (2 x HFR x scale); color = that
  zone's FWHM over the center's (green 1.0, red 1.3 and up); tick = the
  stars' stretch direction in the zone, longer = more aligned.
- Subs used: those passing the HFR and star checks; PS-84 tracking-test rungs
  of 120 s or less first, else the shortest exposure per filter. Longer subs
  are flagged, since trailing can hide or mimic optics.
- Verdicts: tracking (one stretch direction everywhere, center included, not
  radial); tilt (softest corner at least `optics_tilt_warn` 1.20 x the
  sharpest, sharp spot off center, direction stable across subs: check the
  tilt plate or spacer on the soft side); curvature (all corners evenly soft:
  expected on an RC16 with no flattener, not a fault); collimation (center
  stars elongated with no common direction: weak signal, confirm before
  touching the secondary); fine (corners within 15% of the center).
- Gives direction and ratio, not microns. Before a site visit, run the NINA
  Hocus Focus Aberration Inspector by hand to confirm the direction.
- The daily "corner FWHM spread" Pushover is off (`optics_corner_alert`);
  a tilt or collimation finding on most of the last 5 measured nights sends
  one trend alert instead (again if the tilt direction changes).

#### Collimation / tilt history (add a line per adjustment)
| Date | What was done | Optics report before -> after |
|------|---------------|-------------------------------|
| 2026-09-25/26 | none (baseline; PS-95 offline check, desktop re-grade of the 19 RC16 Library subs) | 900 s / 300 s subs: tracking wins the verdict; underneath, the lower-left / left side is the soft side in all 8 measurable subs (softest / sharpest corner 1.17 to 1.38). Confirm on short PS-84 rungs. |
