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
Also found: `0282.fits` and `0282_1.fits` identical (duplicate), and 0282 is a
twilight sub after a 44-min gap (+20% background).

## 2. Calibration capture — already built into the sequencer
No new code needed; the machinery matches the light epoch via `rig_config(PIGGYBACK)`
(gain 100 / offset 256 / 0 °C / `dark_exposures="120"`). Three ways to get it:

- **Automatically on arm** — `_dispatch_piggyback_companion` (armer.py) sends NINA #2 a
  companion that shoots OSC **dawn flats always**, and roof-closed **darks + a bias top-up
  when NINA #2 can see the shared safety monitor**. Requires `PS_PIGGYBACK_ENABLED` and
  `PS_PIGGYBACK_CALIBRATE_ON_ARM`. If NINA #2 doesn't have the safety monitor in its
  profile, it's **dawn-flats-only** — add the shared safety monitor to the NINA #2 profile
  to unlock roof-closed darks/bias.
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
