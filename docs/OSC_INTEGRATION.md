# OSC (one-shot-color) integration — pipeline + calibration

Covers the 600 mm piggyback rig (AP26CC, IMX571 RGGB, 1.29"/px). Companion to the
mono RC16 SHO pipeline. Light epoch: **120 s, gain 100, offset 256, 0 °C**.

## 0. One command from the Library (PS-22)

```
photonscript integrate --target "Andromeda Galaxy" --rig piggyback [--since 2026-09-01] [--until D] [--out DIR]
```
Run on the desktop from the repo venv. What it does (code in
`photonscript/integration/`, PJSR templates `deploy/integrate_stack.js` and
`deploy/finish_osc.js`, the same finish `run-finish-osc.ps1` runs, section 1b):
1. **Select**: the approved subs of the target from the Library mirror
   (`desktop_library_dir`, READ-ONLY). A Library target folder only holds
   subs that passed QA and were reviewed, so the folder is the verdict.
   Folders under every alias count ("Andromeda Galaxy" + "M 31"). The
   Piggy-600 takes Bayer frames from `OSC`; `--rig rc16` takes the mono
   filters (one master per filter, `--filters Ha,OIII` to limit).
2. **Star QA** on the raw subs (`star_qa.py`, the M31_OSC4 v4b rules): low
   star count / bright sky / soft vs the 4 neighbours each side in the same
   exposure group, trailed (ecc > 0.70), doubled stars, not registered, and
   the **second star set**: stars off the reference AND not shared by other
   subs (a split-pointing exposure). `qa/star_qa.csv`. `--qa report` stacks
   everything and only reports; `--qa off` skips it.
3. **Calibration match** (`calib.py`): bias / darks on camera, gain, offset,
   binning, temperature and readout mode (PS-128); a missing dark length
   uses the most plentiful dark length with optimizeDarks (scaling);
   flats per filter only when cooled like the lights (uncooled flats are
   reported and skipped), `--no-flats` to skip.
4. **Stage** by COPY into a NEW run folder
   (`<staging-root>\<target>_<rig>_<yyyymmdd-hhmm>`, default staging root
   `D:\Astrophotography\Staging`), `manifest.json` / `manifest.csv`,
   `reference.txt`.
5. **PixInsight**: `integrate_run.js` (per exposure group calibration,
   CosmeticCorrection, Debayer, StarAlignment with distortion correction,
   LocalNormalization, PSF Signal Weight, Winsorized) then `finish_run.js`
   (finish_osc.js at the `run-finish-osc.ps1` defaults for every master of
   the run: SPCC with Gaia DR3/SP, deconvolution + noise reduction, StarNet2
   star reduction, `<name>_final_steps.json` per master; mono masters skip
   the color steps; GraXpert is found via `PS_GRAXPERT` or the usual paths).
   Only when no PixInsight is running; one at a time; logs polled with short
   reads (never `tail -F`). `--no-pixinsight` stops after writing the
   scripts and prints the launch line.
6. **AstroBin draft**: `astrobin\<target>_<date>_astrobin_acquisition.csv`
   and `..._astrobin_packet.md` (nothing is uploaded).

Timing per stage: `out\timing.csv` (Python stages) and `out\timing_pi.csv`
(PixInsight stages). Small test run: `--limit 3 --max-cal 15`.

## 0b. Ledger and the integrate watcher (PS-33, PS-31)

**Ledger (PS-33).** Every `photonscript integrate` run ends by writing
`<run>\ledger.json` (schema `photonscript.ledger/0.2`,
`photonscript/shared/ledger.py`): target, rig, version (v1, v2, ... per goal
and rig), every Library sub the run considered (file, night, filter,
exposure, used or not), integrated hours and nights, calibration used
(`calibrated | partial | uncalibrated`), star QA summary, PixInsight
integration and finish results with the finish steps files, output paths,
the AstroBin packet and CSV paths (never uploaded), stage timing, and for a
watcher run the trigger. The `review` block (verdict, notes, asks) is the
human half: no script writes it and a rewrite keeps it.

The run then POSTs the ledger to the scheduler (`POST /api/integrations`,
`integration_report_url`, default the scope's tailnet address). When the
scope is unreachable the file stays `reported: false` in its run folder and
the next run, the next watcher cycle or `photonscript integrate-report`
posts it (`--no-report` skips posting). The scheduler keeps one file per
version under `<data_dir>\ledgers\<goal id>\` and shows the latest on the
goal card, the Targets cards and the target page: "Integrated 9.4 h on
2026-10-05 (v2), packet ready" with the paths, plus "new data since:" the
approved hours not in that ledger's sub list. Asks change status only
(`POST /api/integrations/asks/<id>`); goals are edited by hand.

**Watcher (PS-31).** `photonscript integrate-watch --once` (desktop):
1. posts queued ledgers;
2. does nothing while PixInsight is running;
3. reads `GET /api/integrations/candidates` (per active goal and rig: goal
   progress, approved hours, last ledger, new data since it, calibration
   missing, calibration owed) and the thresholds set on the System page
   (Integration group): `integrate_watch_rigs` (piggyback),
   `integrate_watch_new_data_h` (1.0), `integrate_watch_first_h` (0 = first
   run only when the goal is met), `integrate_watch_min_interval_h` (12),
   `integrate_watch_require_calibration` (off: integrate anyway and record
   what was missing). Command line flags override them;
4. runs at most one `photonscript integrate` (QA report mode) into a new
   staging folder and posts its ledger.

`--dry-run` prints the decisions only. A lock file in the staging root keeps
two watchers apart; a run that fails before writing its ledger still counts
for the minimum interval. Nothing installs a task: to automate it, create a
Windows Scheduled Task yourself that runs
`C:\dev\PhotonScript\.venv\Scripts\photonscript.exe integrate-watch --once`
every 30 minutes while logged on (PixInsight needs the desktop session).

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
  companion's dawn set. OSC flats need no filter wheel (`_osc_sky_flat`), but NINA's SkyFlat
  still needs a SwitchFilter child with `Filter: null` (PS-132: without it NINA #2's
  validation threw and Start did nothing; see HANDBOOK, failure modes).

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
