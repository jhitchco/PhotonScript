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
- Image scale: 0.239"/px · FOV 0.414 x 0.277 deg
- Oversampled vs 2-3" seeing -> capture 1x1, software-bin 2x in integration
  (QA: ecc is measured at both 0.24"/px and the binned 0.48"/px since PS-94;
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

## Exposure & calibration standards
- NB 600s, BB 180s, gain 200, offset 256, 0 C ("NEW epoch", 2026-07-05+)
- OLD epoch (pre-2026-07-05 lights, e.g. Crescent 07-03): 300s, gain 200, offset 50
- Epoch = EXPTIME+GAIN+OFFSET+SET-TEMP; darks must match temperature,
  offset drift survivable via the 1000 DN calibration pedestal
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

## Desktop processing
- PixInsight: C:\Program Files\PixInsight\bin\PixInsight.exe
- Staging: C:\Users\sleep\Astrophotography\Staging\<Target>
- Library mirror (receive-only): C:\Users\sleep\ninashare\Library

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
