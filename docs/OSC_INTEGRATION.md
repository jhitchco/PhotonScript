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
   PS-177: every (filter, exposure, binning, readout) group is judged on
   its own: its own reference, its own registration, coherence vote and
   neighbour medians, so a 30 s sub is never measured against a 300 s
   reference. The console and the `qa.groups` block of the manifest list
   keep / reject per group; the CSV has `group` and `group_reference`.
   Mono frames are 3x3 median filtered before star detection (raw RC16
   subs otherwise give sep the hot pixels: 2026-10-08 M31, 54 of 63 false
   "second star set" rejects; 77 of 79 kept after the fix).
3. **Calibration match** (`calib.py`): bias / darks on camera, gain, offset,
   binning, temperature and readout mode (PS-128); a missing dark length
   uses the most plentiful dark length with optimizeDarks (scaling);
   flats per filter only when cooled like the lights (uncooled flats are
   reported and skipped), `--no-flats` to skip. PS-178: when no bias
   matches, the bias sessions that miss the epoch are listed with the
   reason; a light length with no dark gets "nearest alternative" lines
   (same-length darks below `--min-darks`, same-length darks off the epoch
   and why, other lengths that could be scaled once a matching bias
   exists, else what to capture) and its subs are REFUSED (left out)
   unless `--allow-uncalibrated`.
   **Stacks (PS-177).** Mono (RC16) never mixes exposure lengths in one
   ImageIntegration: per filter the group with the most integration time
   is `master_<F>` (what `blend` and the finish expect), every other
   group of at least `--min-group-frames` (3) subs is its own HDR master
   `master_<F>_<exp>` (e.g. `master_L_30s`, combine later with HDRComposition
   if wanted), smaller groups are left out with a message. An OSC stack
   still holds every exposure group (one debayered master); it is marked
   `mixed_exposures` and when PSF Signal Weight fails the PJSR falls back
   to exposure-time weights, never equal weights.
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

**Auto-integrate (PS-161).** `photonscript autointegrate --once` is the
unattended desktop job built on the watcher (same lock, same decide(): never
re-integrates without new approved data, nothing while PixInsight is open):
1. a goal that wants a run waits until Syncthing has settled its Library
   folders: no `~syncthing~*.tmp` / `.syncthing.*.tmp` file, and the rig's
   light count + total size unchanged for `autointegrate_settle_min` (15)
   minutes (state in `<staging>\.autointegrate-state.json`; Syncthing keeps
   the source modification times, so file times cannot tell);
2. runs `photonscript integrate` (OSC natural color plus, with
   `autointegrate_hoo`, the HOO-mapped `<name>_hoo.{xisf,jpg}`: Ha = R,
   OIII = mean of G and B scaled to the Ha background; RC16 one master per
   filter) and posts its ledger;
3. two-rig goals: `photonscript blend` once both rigs have masters and no
   blend folder used exactly these inputs yet (`autointegrate_blend`);
4. writes `<run>\review.jpg` (the finals side by side, at most 2048 px) and
   sends it with Pushover (`autointegrate_notify`; the desktop .env needs the
   Pushover keys).
The Library mirror is `integration_library_dir`, else the first that exists
of `D:\ninashare\Library` (the mirror since 2026-10; `C:\Users\sleep\ninashare`
becomes a junction to it), `desktop_library_dir`, `~\ninashare\Library`;
`photonscript integrate` uses the same default. Install the task yourself:
`powershell -ExecutionPolicy Bypass -File deploy\install-autointegrate-task.ps1
-DryRun` first, then without -DryRun (every 30 min, one instance, output in
`logs\autointegrate.log`; -Uninstall removes it). `run-finish-osc.ps1 -Hoo on`
and `photonscript integrate --hoo` give the HOO image by hand.

**Campaign status and review (PS-142).** Every goal card (dashboard and
Targets page) carries a status chip per rig, the target page a "Campaign
review" panel (`GET /api/integrations/status`, `scheduler/integrations.py`
`goal_status`):
- **Acquiring**: no reason to integrate yet (goal not met, or under
  `integrate_watch_new_data_h` new since the last ledger).
- **Ready to process**: the watcher's own rule (goal met with no ledger, the
  `integrate_watch_first_h` hours, or enough new data); with
  `integrate_watch_require_calibration` on, missing calibration keeps it
  Acquiring ("waiting for calibration"), else the chip's tooltip lists it.
- **Processing**: integrate-watch posted a processing notice (`POST
  /api/integrations/processing`, state start / end around each run; best
  effort, ignored after 12 h). A stored ledger ends it.
- **Processed (vN)**: the latest ledger; **Published (vN)** when that ledger
  records an AstroBin URL (`publish.astrobin.url`), and the chip links to it.
  A newer unpublished version reads "Processed (v4); v3 published".

The review panel shows, per rig, the latest ledger, the newest verdict and
notes (with their version) and every ask of every version, open ones with
Approve / Decline (`GET /api/integrations/review?project_id=`). The goal
card shows the compact form (verdict, open asks). Approve of a plan-changing
ask (more_hours: Piggy-600 OSC goal + hours, or the RC16 budget and mix;
short_subs on an RC16 filter: an HDR short set; reframe: the driving rig)
first reads `GET /api/integrations/asks/<id>/proposal` (the PATCH body and
the exposure-plan diff, computed on a copy of the goal), shows the diff in
a confirm, and only on OK sends it through `PATCH /api/projects2/<id>`
(ProjectStore.update), then marks the ask `applied` (the patch is kept on
the ask). Other asks (need_calibration, rest, fix_blocker) just change
status. Nothing edits projects.json directly.

**Ready ping.** A NEW ledger version (not a re-post of the same run, not an
import) sends one Pushover, title "PhotonScript <campaign> integrated":
"M31 v3 integrated: 9.4 h, packet ready" (or "integration FAILED" /
"staged ... (not integrated)"). The dawn "Night complete" push adds an
"Integrations: ..." line for the ledgers posted in the last 24 h.

**Import the history (PS-142, one time).** `photonscript ledger-import
<path> [--variant v4b] [--campaign M31] [--version N] [--out FILE] [--json]
[--dry-run | --apply] [--url URL]` reads a hand-written ledger (0.1 or 0.2:
a `ledger.json` or the folder holding it) or, for a hand-run staging folder
without one, synthesizes a 0.2 ledger from one processing variant:
`out\<variant>\weights.csv` (the integrated subs), `manifest.csv` (what was
staged; calibration counts), `<tag>_*selection.csv` (star QA),
`out\pipeline_<tag>.log` / `finish_<tag>.log` (ok, minutes, funnel) and
`astrobin\` (packet, CSV, crop JPG; an AstroBin link in the packet is kept
as `publish.astrobin.revision_of`, not as published). It never writes into
the source folder (not even `reported`); `--out` must point elsewhere. The
default is a dry run that prints the summary; `--apply` POSTs it to
`/api/integrations` (idempotent per run; imported ledgers carry
`machine.imported` and send no ping). A hand-written 0.1 file keeps its own
field names, so the import fills the 0.2 reads where missing (hours from
subs_by_filter, integration / finish ok from the recorded master and
outputs, the packet path) and lists them in `machine.imported.filled`; its
asks get stable ids so a repeat import keeps the decisions.

For Jeremy, once the scope runs PS-142 (desktop, repo venv):
```
photonscript ledger-import C:\Users\sleep\Astrophotography\Staging\M31_OSC3\ledger.json
photonscript ledger-import D:\Astrophotography\Staging\M31_OSC4 --variant v4b
# both look right? post them, oldest first:
photonscript ledger-import C:\Users\sleep\Astrophotography\Staging\M31_OSC3\ledger.json --apply
photonscript ledger-import D:\Astrophotography\Staging\M31_OSC4 --variant v4b --apply
```
M31 then reads "Processed (v4); v3 published" with v3's four asks open on
the target page.

## 0c. Two-rig blend: the RC16 core in the Piggy-600 image (PS-153)

For a two-rig goal (M31, PS-134) both rigs get their own `photonscript
integrate` run (`--rig piggyback`, `--rig rc16`). Then:
```
photonscript blend --target M31 --dry-run     # which masters, script check
photonscript blend --target M31               # weight 0.7, both products
```
Inputs: the newest piggyback run and the newest rc16 run of the target under
the staging root. Stage `linear` (default) takes the finish's
`*_linear.xisf` (gradient removed, color calibrated; a finished run beats a
newer unfinished one) and falls back to the raw `master_*.xisf`; `--stage
final` takes the stretched `*_final.xisf`. `--osc PATH` / `--rc16 P1,P2`
override. RC16 luminance = the L master, else the mean of the given masters.

deploy/blend_rc16_osc.js (rendered to `blend_run.js`): plate solve both
(ImageSolver; an existing solution is kept) -> Resample the RC16 down to
1.29"/px -> StarAlignment onto the OSC (fallback: affine through both
astrometric solutions) -> footprint mask (inset `--inset` 0.02 and feather
`--feather` 0.08 of the footprint's short side; `--lum-mask` adds a
brightness ramp) -> linear fit of the RC16 to the OSC CIE Y inside the
footprint -> CIE Lab: L = L_osc (1 - k) + L*(RC16) k with k = weight x mask,
a and b from the OSC. Product 2 (`--no-core` skips it): the OSC cropped
around the footprint, Resampled up to 0.236"/px, StarAlignment onto the RC16
(same WCS fallback), L from the RC16, color from the OSC.

Outputs in `<staging>\Blend\<target>_blend_<time>\out\final\`:
`<name>_blend_linear.xisf`, `<name>_blend.{xisf,tif,jpg}`, `<name>_osc_ab.jpg`
(the OSC alone at the same stretch, for A/B), `<name>_blend_mask.xisf`,
`<name>_core_linear.xisf`, `<name>_core.{xisf,tif,jpg}`,
`<name>_blend_steps.json`; `out\blend.log`, `out\timing.csv`,
`out\timing_pi.csv`, `manifest.json`, `ledger.json` (kind `blend`, rig
piggyback). Blend ledgers live one level below the staging root, so they are
not integration versions, do not trip integrate-watch and are not posted.
The stretch is the finish's linked stretch only (no noise reduction,
deconvolution or star reduction on the blend yet).

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
  nautical dawn + `piggyback_flat_dawn_offset_min` (PS-163, default 20; was a fixed 5,
  where the OSC SkyFlat found the sky too dim at its 30 s max on 2026-10-07) plus 90 s
  (the RC16's dawn slew), capped at nautical dawn + 5 + `dawn_flats_window_min` - 10.
  With the safety monitor they wait for safe only until nautical dawn +
  max(`piggyback_flat_wait_min` (25), the start offset) and are skipped if
  the roof is still closed; the light loop's wait for safe is bounded the same way, so a
  pre-dawn roof close can no longer wedge the companion. The armer's dawn shutdown holds
  until nautical dawn + 5 + `dawn_flats_window_min` (40, capped at sunrise) and then also
  stops NINA #2's sequence. Refocus in the light loop (PS-68): temperature
  (`piggyback_af_temp_change_c` 1.5 C), HFR rise (`piggyback_af_hfr_increase_pct` 10%)
  and every `piggyback_af_interval_min` (60; covers the RC16 meridian flip NINA #2 can't
  see, and a bad AF the HFR trigger won't catch).
  OSC lights (PS-175) run only in astronomical dark: the first light waits for astro
  dusk (`WAIT_ASTRO_DUSK_FOR_OSC_LIGHTS`, at or after the RC16's first target) and the
  light loop and its bounded waits end at astro dawn. The dawn flats above keep their
  nautical-dawn timing.
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
