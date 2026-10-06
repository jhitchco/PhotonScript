# OSC (one-shot-color) integration — pipeline + calibration

Covers the 600 mm piggyback rig (AP26CC, IMX571 RGGB, 1.29"/px). Companion to the
mono RC16 SHO pipeline. Light epoch: **120 s, gain 100, offset 256, 0 °C**.

## 1. PixInsight pipeline (clean stars)
Files in `deploy/` (+ one Python helper):
- `photonscript/image_processor/osc_cull.py`: **runs first** (called by
  `run-integration-osc.ps1`). Rejects **split-pointing subs** (the mount moved
  mid-exposure, so the sub holds the field twice) and **duplicate subs** (same
  `DATE-OBS`, identical bytes, e.g. `_1` copies); flags sky-background outliers
  (twilight/moon) without moving them unless `-RejectBright`. Rejects are
  **moved** to `<stage>\REJECTED\<reason>\`, never deleted. Writes
  `cull_report.csv` and `reference.txt` (sharpest kept sub). Needs Python + numpy.
- `integrate_osc.js`: PJSR: **clears its own intermediates** (cal/cc/debayer/
  reg/ln) -> (bias/dark/flat if staged) -> ImageCalibration -> CosmeticCorrection
  (CFA, only with a master dark) -> Debayer RGGB (VNG) -> **StarAlignment with
  `distortionCorrection = true`** (reference = `reference.txt`, else mid-stack)
  -> **LocalNormalization** (falls back to additive+scaling on any failure) ->
  PSF-Signal-weighted `ImageIntegration` (WinsorizedSigmaClip) ->
  `masterOSC.xisf` + `masterOSC_review.jpg` (**unlinked** per-channel stretch).
  Every stage asserts it did not output more frames than it was given.
  (No SubframeSelector: its scripted dll access-violates on this PI build.)
- `prepare-integration-osc.ps1`: stage OSC lights + `INSTRUME=AP26CC`, epoch-matched
  calibration into `Staging/<name>/{LIGHTS/OSC,DARKS,BIAS,FLATS/OSC}`.
- `run-integration-osc.ps1`: cull, fill the staging path into the script, launch
  PixInsight. `-CullDryRun` = report only and stop; `-NoCull` = skip the cull.

Run on the desktop:
```
.\deploy\prepare-integration-osc.ps1 -Name "M31_OSC"
.\deploy\run-integration-osc.ps1     -Name "M31_OSC" -CullDryRun   # optional preview
.\deploy\run-integration-osc.ps1     -Name "M31_OSC"
```
It runs **uncalibrated** if no OSC masters are staged (still debayers, distortion-registers,
and integrates): so you get clean stars now, and full calibration once the frames below exist.

### 1a. Lesson: M31_OSC2 (2026-09-21 subs, integrated 2026-09-25)
The master showed **two M31 cores** (one with a black hole punched in it). Two
separate faults:
1. **Split-pointing subs.** The RC16 moved the mount between two pointings ~51'
   apart every few minutes while Piggy-600 kept exposing. 31/62 subs straddled a
   move (ground truth: M31 flux measured at both pointings in every sub). Stars
   of the minority copy are sigma-rejected, but its galaxy light survives. The
   cull detects these from the sub's own autocorrelation (extra peak at the slew
   vector, over the session's static star-field baseline): 0 misclassified.
2. **Stale intermediates.** The script listed whole output folders, so `_cc_d`
   files from an earlier (cosmetic-corrected, no dark) run were stacked alongside
   the new `_d` files: 63 subs went in as 125, and CosmeticCorrection's hole in
   the M31 core came along. Now cleared per run + funnel assertions.
Follow-up (2026-09-26, all 92 library subs as M31_OSC3): some subs sat
almost entirely on the OTHER pointing (M31 centered). Judged against the
majority field's sidelobe baseline they read as "split". The cull now first
groups subs by which field they saw (autocorrelation similarity), judges each
group against its own baseline, integrates only group 0 (the majority
framing) and moves clean subs from other framings to
`REJECTED\other_pointing_<n>\` so they can be stacked separately.
Also found: `0282.fits` and `0282_1.fits` identical (duplicate), and 0282 is a
twilight sub after a 44-min gap (+20% background).

## 1b. Finishing (master -> final image)
`deploy/run-finish-osc.ps1 -Name "M31_OSC2"` runs `finish_osc.js` on
`out\master\masterOSC.xisf`: crop 1.5% registration edges -> gradient removal
(GradientCorrection, else ABE degree 1; `-Gradient none` to skip) -> plate solve
with PixInsight's ImageSolver, seeded from `-Target`/`-RaDeg -DecDeg` (guessed
from the name, e.g. M31) and spiralling out to ~1.2 deg because Piggy-600 frames
rarely center on the target -> color -> SCNR green (`-Scnr`, 0.6) ->
deconvolution -> noise reduction (`-NoRC` skips the RC Astro tools) -> linked
stretch (`-BgTarget` 0.12, `-ShadowSigma` 2.0) + saturation (`-SatMid` 0.64) ->
core HDR blend (`-HdrLayers` 7) -> optional framing crop (`-Frame l,t,r,b`).
Output in `out\final\` (or a new `-OutDir`): `<Name>_linear.xisf`
(color-calibrated, for manual work), `<Name>_final.xisf`, `_final.tif` (16-bit),
`_final.jpg` and `<Name>_final_steps.json` (every step with tool, status and
settings; the short tag such as `SPCC` or `BN+CC` is also in the PSFINISH FITS
keyword). Log: `out\finish.log` (or `<OutDir>\finish.log`). Every optional step
logs and skips on failure. `-Master <xisf> -OutDir <new folder>` finishes any
master into a fresh folder (refused if the folder has files); `-Wait` runs
PixInsight unattended and checks for EXIT OK.

Color (PS-46, `-Color auto|spcc|basic`): SPCC runs when the image solved and
the Gaia DR3/SP database is selected in PixInsight (probed with Gaia
`get-info`). Curves come from PixInsight's `library\filters.xspd` and
`white-references.xspd` by name: QE `Sony IMX411/455/461/533/571`, RGB `Sony
Color Sensor R/G/B-UVIRcut`, white `Average Spiral Galaxy` (`-SpccQE`,
`-SpccRed`, `-SpccGreen`, `-SpccBlue`, `-SpccWhite`). Otherwise the log reads
`color: <reason> -> BN + ColorCalibration fallback`. One-time setup: download
Gaia DR3/SP from the PixInsight software distribution (needs the PixInsight
account), then Process > Gaia > wrench icon > select the DR3/SP files.

Deconvolution and noise reduction (PS-41) run on the LINEAR image after color
calibration, deconvolution first: deconvolution inverts a linear blur, which
no longer holds after the stretch, and should sharpen real detail rather than
noise-reduction smoothing; noise is still uniform before the stretch
amplifies the faint background. Tool order: deconvolution = BlurXTerminator,
else GraXpert `deconv-obj` (`-DeconvStrength`, 0.5) then `deconv-stellar` at
half strength, else skipped (no built-in: classic Deconvolution needs a
measured PSF). Noise reduction (`-Denoise 0..1`, 0.5; 0 = off) =
NoiseXTerminator, else GraXpert `denoising`, else MultiscaleLinearTransform
(4 layers, thresholds 3/2/1/0.5, inverted linear mask). GraXpert is found via
`-GraXpert <exe>`, `$env:PS_GRAXPERT`, or `C:\Program Files\GraXpert\`
(`GraXpert-win64.exe` / `GraXpert.exe`); `-NoGraXpert` turns it off,
`-GraXpertAiVersion`, `-GraXpertGpu true|false`, `-GraXpertTimeoutMin` (30)
pass through. Its output replaces the image only when the size matches and the
background level is plausible; otherwise the next tool runs.

Star reduction (PS-40, `-StarReduction on|off`, `-StarStrength` 0.7) needs the
StarNet2 PixInsight module and is skipped (logged) without it. After noise
reduction, a copy of the linear image gets a reversible midtones-only
pre-stretch (StarNet2 is trained on stretched data), StarNet2 removes the
stars, the inverse transform takes the starless image back to linear, and
stars = linear - starless. The starless image gets the normal stretch (its own
statistics, so slightly harder), saturation and the core HDR blend, and is
saved as `<Name>_starless.xisf`; the stars get the same midtones with a floor
at 2x the linear noise, then are screened back:
`~(~starless * ~(stars * k))`. Any failure falls back to the normal stretch.

## 2. Calibration capture — already built into the sequencer
No new code needed; the machinery matches the light epoch via `rig_config(PIGGYBACK)`
(gain 100 / offset 256 / 0 °C / `dark_exposures="120"`). Three ways to get it:

- **Automatically on arm** — `_dispatch_piggyback_companion` (armer.py) sends NINA #2 a
  companion that shoots OSC **dawn flats always**, and roof-closed **darks + a bias top-up
  when NINA #2 can see the shared safety monitor**. Requires `PS_PIGGYBACK_ENABLED` and
  `PS_PIGGYBACK_CALIBRATE_ON_ARM`. If NINA #2 doesn't have the safety monitor in its
  profile, it's **dawn-flats-only** — add the shared safety monitor to the NINA #2 profile
  to unlock roof-closed darks/bias.
  Dawn flats (PS-36): `piggyback_flat_count` (default 25) at gain 100 / offset 256, at
  nautical dawn +5 plus 90 s (the RC16's dawn slew). With the safety monitor they wait
  for safe only until nautical dawn + `piggyback_flat_wait_min` (25) and are skipped if
  the roof is still closed; the light loop's wait for safe is bounded the same way, so a
  pre-dawn roof close can no longer wedge the companion. The armer's dawn shutdown holds
  until nautical dawn + 5 + `dawn_flats_window_min` (40, capped at sunrise) and then also
  stops NINA #2's sequence. Refocus in the light loop (PS-68): temperature
  (`piggyback_af_temp_change_c` 1.5 C), HFR rise (`piggyback_af_hfr_increase_pct` 10%)
  and every `piggyback_af_interval_min` (60; covers the RC16 meridian flip NINA #2 can't
  see, and a bad AF the HFR trigger won't catch).
- **On demand (any closed-roof night)** — dispatch a matched OSC dark/bias set to NINA #2:
  ```
  POST /api/calibration/capture   {"rig":"piggyback"}          # 120 s x quota + 50 bias
  POST /api/calibration/capture   {"rig":"piggyback","darks":[[120,30]],"bias":50}
  ```
- **Flats on demand** — `POST /api/calibration/flats {"rig":"piggyback"}` (dusk) / the
  companion's dawn set. OSC flats need no filter wheel (`_osc_sky_flat`).

What you need banked (per the light epoch):
- **Darks:** 120 s, gain 100, offset 256, 0 °C — ~20–30.
- **Flats:** through the 600 mm with the AP26CC, RGGB, ~50 % ADU — ~20–30.
- **Bias** (or flat-darks): gain 100, offset 256, 0 °C — ~50.

`GET /api/calibration/health?rig=piggyback` reports what's banked and staleness.

## 3. One thing to verify (why no OSC calibration matched yet)
The M31 lights currently sit in `Library/_/OSC/` (target folder `_`), but the piggyback
calibration matching (`count_matching_darks`, `calibration_health`) scans the **piggyback
library subtree** (`rig_config` sets `library_dir` -> `.../Library/piggyback`). If the
librarian is filing OSC frames under `_/OSC` instead of the piggyback subtree, calibration
won't be found even once it's shot. Check `PS_PIGGYBACK_LIBRARY_DIR` / the librarian's
per-rig routing so OSC lights **and** their darks/flats land in the same subtree. (Left as a
check, not changed — it touches live librarian routing.)

## 4. WBPP recipe (no-scripting fallback)
Scripts → Batch Processing → **WeightedBatchPreprocessing**. Same result as
`integrate_osc.js` through the GUI.

0. Run `run-integration-osc.ps1 -CullDryRun` (or `osc_cull.py` directly) first:
   WBPP cannot see split-pointing subs.
1. **Add Lights** → `C:\Users\sleep\Astrophotography\Staging\M31_OSC\LIGHTS\OSC`
   (92 frames). Leave darks/flats/bias empty for now (none match yet — WBPP just
   runs uncalibrated).
2. **CFA / OSC:** tick **"CFA images"** (top of the Lights tab). Debayer pattern =
   **Auto** (reads `BAYERPAT=RGGB`); method VNG or Bilinear.
3. **Weighting (keep soft subs, weight down):** Image Weighting = **PSF Signal
   Weight** (default). It keeps every sub and down-weights the low-SNR/soft ones —
   do NOT set an approval/rejection that discards frames.
4. **Registration → enable "Distortion correction"** (Star Alignment distortion
   model). This is the key setting — it removes the ~0.16°/night field rotation
   that left star tails in the quick-look.
5. **Local Normalization:** ON (helps the uncalibrated gradients).
6. **Integration → Pixel Rejection:** Winsorized Sigma Clipping (WBPP auto-picks it
   for ~90 frames). Reference frame: let WBPP auto-select the best-weighted.
7. Set the output directory → **Run**. Master lands in `<output>/master/`.

Uncalibrated, so finish with **DynamicBackgroundExtraction / GradientCorrection**
(vignetting + light pollution) then stretch. Once OSC darks/flats/bias are banked
(§2), drop them into the same WBPP and it auto-calibrates before debayer.
