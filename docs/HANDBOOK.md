# PhotonScript Handbook

Everything needed to cold-start a working session on this system: what it is,
where it runs, how code moves, how images move, and how integration works.
Last updated 2026-07-07.

## 1. What PhotonScript is

**Mission: maximize the use of every dark hour of shutter time, and turn it
into award-winning, high-quality astrophotography.** Every design decision
bends toward one of those two poles: the honest funnel (dark hours -> shutter
hours -> accepted hours) measures the first; the QA gates, calibration
discipline, and integration pipeline serve the second.

PhotonScript is Jeremy's automation layer around NINA for the AARO remote
observatory. It plans nights, generates NINA Advanced Sequencer JSON, arms the
scope, grades every sub as it lands, tracks per-target goals and a 14-night
campaign, builds a QA'd image library that syncs home, and reports each
morning. GitHub: github.com/jhitchco/PhotonScript.

## 2. The two machines

### Scope PC (at AARO, Pier 3, Rodeo NM)
- Tailscale: `100.94.189.77` - dashboard at `http://100.94.189.77:8100`
- Repo: `C:\astro\PhotonScript`; runs via `run-photonscript.ps1` wrapper
  (restarts on exit code 42 = self-update)
- NINA + PHD2 live here. Image files land here first.
- Syncthing folder: `C:\Users\jeremy\NINAShare` (folder id
  `ninashare-ddpnx-urgun`); `PS_LIBRARY_DIR=C:\Users\jeremy\NINAShare\Library`

### Desktop (home, Windows: `C:\Users\sleep`)
- Repo working copy: `C:\Users\sleep\Claude\PhotonScript` (Claude delivers here)
- Syncthing receive-only mirror: `C:\Users\sleep\ninashare` (device LJASGRM-...)
  -> `ninashare\Library\` holds accepted lights + calibration masters-to-be
- PixInsight: `C:\Program Files\PixInsight\bin\PixInsight.exe`
- Integration staging: `C:\Users\sleep\Astrophotography\Staging\<Target>\`

### Hardware / site facts
- RC16 (406mm) at 3248mm f/8; scale 0.239"/px; FOV 0.414 x 0.277 deg
- OGMA AP26MC (IMX571 mono APS-C, 6224x4168, 3.76um)
- CEM70G mount - historically UNGUIDED; PHD2 present. Guiding now
  supported: `PS_GUIDED_DEFAULT=true` inserts StartGuiding after centering,
  DitherAfterExposures every 5 frames, StopGuiding at end/unsafe.
- Filters: L,R,G,B + 3nm S,H,O (NINA names are single letters; PhotonScript
  canonical names are Ha/OIII/SII - `filter_name_map()` translates)
- Camera setpoint 0C. Exposure defaults: NB 600s, BB 180s, gain 200, offset 256.

### Calibration epochs (critical)
An epoch = EXPTIME + GAIN + OFFSET + SET-TEMP from FITS headers.
- OLD lights (e.g. Crescent 2026-07-03): 300s, gain 200, **offset 50**, 0C
- NEW standard (2026-07-05 onward): 600s (+300s darks), gain 200, **offset 256**, 0C
Darks must match temperature; offset mismatch is survivable only because the
pipeline adds a 1000 DN output pedestal at calibration.

## 3. Code deploy loop

Desktop, from `C:\Users\sleep\Claude\PhotonScript`:
```powershell
.\deploy\deploy.ps1 "commit message"
```
commits, pulls --rebase, pushes to GitHub, then POSTs `/api/update` on the
scope, which pulls and restarts (exit 42). Refused with 409 while ARMED or
PAUSED_UNSAFE - by design; disarm first or wait for morning.
- Claude NEVER pushes to GitHub; Jeremy runs deploy.ps1.
- Scope commit hashes can differ from desktop after rebases - verify by the
  version stamp on the dashboard header, not by hash equality.

## 4. Nightly automation flow

1. **Arm** (dashboard button or `POST /api/arm {"armed": true}` - the body
   must be exactly that; `{}` disarms). Generates tonight's sequence from the
   campaign plan and dispatches to NINA.
2. Sequence: wait for dusk -> dusk sky flats if requested (least->most
   transmission: Ha,OIII,SII,R,G,B,L as sky darkens) -> per-target
   slew/center/AF -> (StartGuiding) -> SmartExposure loops (moon-timed BB
   windows) -> mid-night darks if roof closes (quota-driven, dawn-bounded,
   lowest priority) -> bias one-shot if still unsafe -> dawn sky flats
   (SafetyMonitorCondition-wrapped so a closed roof is skipped; order
   BB first -> NB last as sky brightens).
3. **Live watcher** grades each sub (sep HFR/ecc on binned frames), skips
   calibration frames (path part or IMAGETYP != LIGHT), applies tracking-RMS
   rejection only while PHD2 reports guiding/settling.
4. Morning: runs page `/runs/YYYY-MM-DD` (permalinks work) shows plan vs
   actual, the honest funnel (dark hours -> shutter hours -> accepted hours;
   sky utilization = accepted/dark), 4-state sub review
   (review/accepted-syncing/transferred/rejected; X key cycles states,
   thumbnail corner buttons give one-click verdicts). **Approve night** queues
   accepted subs into the Library -> Syncthing carries them to the desktop.
5. Daily 8:04 AM scheduled Claude task fetches `/api/runs`, `/api/sync`,
   `/api/calibration/health` via Chrome and writes a debrief.

## 5. File transfer: Syncthing

How images get from the scope to the desktop - the bridge between capture
and integration.

- **Scope side (send):** `C:\Users\jeremy\NINAShare`, folder id
  `ninashare-ddpnx-urgun`. PhotonScript's librarian HARDLINKS accepted subs
  and calibration frames into `NINAShare\Library\...` (hardlinks cost no
  disk and originals stay in the NINA capture tree). Syncthing watches the
  folder and ships whatever appears.
- **Desktop side (receive-only):** `C:\Users\sleep\ninashare`, device
  `LJASGRM-...`, GUI at `https://127.0.0.1:8384`. Receive-only means desktop
  edits/deletes get reverted at next sync - NEVER write into it. Stage out of
  it with hardlinks (prepare-integration does this).
- **Library layout:** `Library\<Target>\<Filter>\*.fits` for lights;
  `Library\Calibration\{DARK,FLAT,BIAS}\<session>\...` for cal frames.
- **PhotonScript integration:** the scope talks to Syncthing's REST API
  (key from its config). `GET /api/sync` on the dashboard = folder completion
  % + whether the Library subtree is fully synced; `GET /api/sync/queue`
  = what the desktop still needs (`/rest/db/remoteneed`, grouped by folder).
  This drives the 4-state sub lifecycle on the runs page:
  review -> accepted (syncing) -> transferred -> rejected.
- **Approve night** queues that night's accepted subs into the Library;
  **Reset library** (approved nights only) rebuilds it from scratch after
  bookkeeping changes.
- Typical throughput observed: 227 files / 11 GB overnight batch; watch
  progress on /api/sync or the Syncthing GUI on either end.

## 6. PixInsight integration pipeline (desktop)

Run from `C:\Users\sleep\Claude\PhotonScript`:
```powershell
# 1. stage (hardlinks from ninashare Library; -Loose relaxes dark matching
#    to exposure+temperature, offset may differ)
.\deploy\prepare-integration.ps1 -Target "Crescent Nebula" [-Loose] [-Copy]
# answer N to its launch prompt - it opens PixInsight WITHOUT the script

# 2. run (regenerates the PJSR script from deploy/integrate_sho.js and
#    launches PixInsight with it)
.\deploy\run-integration.ps1 -Target "Crescent Nebula"
```
Staging layout: `LIGHTS\<Filter>\`, `DARKS\`, `BIAS\`, `FLATS\<Filter>\`.
To restage from scratch: `Remove-Item -Recurse` the staging folder first.

Pipeline steps (deploy/integrate_sho.js, pure-ASCII PJSR):
masterBias -> masterDark -> per filter: masterFlat (bias-calibrated,
multiplicative/equalize-fluxes) -> ImageCalibration (optimizeDarks,
**outputPedestal 1000 DN**) -> CosmeticCorrection (auto hot/cold 3.0) ->
StarAlignment to a shared mid-stack reference (sensitivity raised for
star-poor narrowband) -> ImageIntegration -> masterLight_<F>.xisf +
masterLight_<F>_bin2.xisf (2x average downsample; 0.24"/px is oversampled).
Finally: masterSHO_review.jpg (R=SII,G=Ha,B=OIII) and masterRGB_review.jpg
(if R/G/B masters exist) - borders cropped 1.5%, channels autostretched to
~12% background. Per-frame accounting: every dropped frame is logged with
its stage ("DROPPED at registration [SII]: ...") plus a funnel line per
filter (staged -> calibrated -> cleaned -> registered).

Log: `Staging\<Target>\out\pipeline.log`, flushed per step; ends "EXIT OK"
or "ERROR: ...". Masters in `out\master\`.

### PJSR lessons (each cost a debugging session)
- No `/*` anywhere in `//` comments - PixInsight's preprocessor opens a block
  comment. Inside string globs is fine.
- Write the generated script BOM-less (`[IO.File]::WriteAllText`); PowerShell
  `Set-Content -Encoding UTF8` adds a BOM that breaks parsing. Keep pure ASCII.
- ImageIntegration default PSF weighting fails on starless frames
  ("Zero or insignificant PSF Signal Weight"); NoiseEvaluation gives ~1e-6
  weights that the 0.005 minWeight floor then excludes entirely. Use
  `weightMode = DontCare; minWeight = 0` - PhotonScript QA already culled.
- `searchDirectory` matches FILES only - probe filter subdirs by known names.
- A master dark with a higher offset than the lights clips the background to
  zero without an output pedestal (masters look like pure noise).
- Never mix dark temperatures; -Loose enforces temp match since 5b4c6c9.

### Night-ops lessons
- 2026-07-07 (zero-light night): weather held the roof shut past midnight;
  on safe re-entry the first target (Eagle) was exactly AT the meridian, the
  flip fired before the first exposure, and the flip's recenter plate solve
  (through the 3nm H filter) failed until manually cancelled at dawn - the
  stuck "Recentre - Solving..." dialog blocked the whole sequence for 4+ safe
  hours. Root cause: PlateSolve2 (Regions=5000)
  hung 6+ hours on a cloud frame; blind failover misconfigured. Fix: ASTAP as
  primary AND blind solver (fails fast -> safety condition recovers the night).
  Also: meridian-aware target ordering (backlog); /api/nina/log for triage.

## 7. Claude session context

- Mounts: `C:\Users\sleep\Claude` (deliver repo here via
  `cp -rf /tmp/PhotonScript/. .../mnt/Claude/PhotonScript/` - never rm -rf, it
  fails while a shell is cd'd there), `C:\Users\sleep\ninashare` (READ ONLY -
  receive-only Syncthing, never write), `C:\Users\sleep\Astrophotography`
  (staging + pipeline.log readable/writable directly; file deletion needs the
  permission grant).
- The sandbox CANNOT reach Tailscale. The ONLY path to the dashboard/API is
  the Claude-in-Chrome extension (browser "pc windows at home") using
  javascript_tool fetch against `http://100.94.189.77:8100`.
- Scheduled tasks: `photonscript-morning-debrief` (daily 8:04 AM).
- Constraints: no GitHub pushes, no credentials, no AstroBin scraping for
  data tables, no writes into ninashare, scope deploys refused mid-night.

## 8. Current state (2026-07-07)

- Crescent Nebula: 82 accepted OLD-epoch lights (Ha 40/OIII 20/SII 22 staged;
  27/18/9 survived registration unguided). Goal being raised to 25h,
  OIII-heavy + ~1h RGB for star color. Guiding enables 600s subs and should
  lift registration survival above 90%.
- Calibration in library: 32x300s darks @0C/offset256, 50 bias, full flat set
  (verified: darks median ~257-330 ADU = offset floor; flats ~50% full well).
- Mosaic planner shipped at `/mosaic`: panel grid over a DSS2 hips2fits
  cutout, one goal per panel.

## 9. Troubleshooting runbook (how to debrief a night)

Read the roof BEFORE judging the rig. The roof controller at
`https://status.astronomyacres.com/` is the source of truth for open time -
trust its Roof Log over the PhotonScript API's `safe_hours` (which is not the
same as roof-open hours). If the roof never opened or opened <~1 h, the night
was weather-limited: attribute low/zero lights to weather, do NOT invent a
hardware/AF failure. Only escalate to a rig problem when the roof was genuinely
open for a meaningful stretch but few/no good lights resulted.

### Triage endpoints (reach via Claude-in-Chrome, not the sandbox)
- `/api/runs` -> newest night (dates are LOCAL evening date). `/api/runs/DATE`
  -> score, funnel (dark/light/accepted hrs), per target+filter table, HFR.
- `/api/preflight` (POST) -> config, directories, equipment, disk. The "NINA
  image directory" check catches a missing capture folder. (Sends a Pushover
  test as a side-effect.)
- `/api/calibration/health` (staleness), `/api/sync` (transfer backlog).
- `/api/nina/log?lines=N&grep=term1|term2` -> the log endpoint greps the WHOLE
  file server-side FIRST, then returns the last min(N,5000) matching rows. So
  for a specific event type (plate solve, autofocus, a given error) grep sees
  the entire night; the 5000-line cap only bites when one signature spams
  (e.g. a validation crash loop). For the complete raw log use the full night
  bundle: `/api/runs/DATE/bundle` (zip). Note the log message body can wrap onto
  timestamp-less continuation lines - grep the OWNER (`file.cs|Method|line`) or
  read the stack frames to attribute an exception.

### Failure modes seen in the field
- **Missing NINA capture folder** (2026-07-26): every LIGHT `TakeExposure`
  fails validation with "The folder ... in Image File Settings ... was not
  found"; zero subs land. Fix: recreate the folder on the scope PC
  (`New-Item -ItemType Directory -Force "C:\Users\jeremy\Documents\N.I.N.A"`)
  or repoint NINA Options -> Imaging -> File Settings. Confirm with
  `/api/preflight` ("NINA image directory" -> pass). PS_IMAGE_WATCH_DIR only
  sets where PhotonScript *watches*, not where NINA *saves* - changing it does
  NOT fix this.
- **SmartExposure dither validation crash** (2026-07-26): NINA's SmartExposure
  ctor always expects a `DitherAfterExposures` trigger at `Triggers[0]`;
  `Validate()` -> `GetDitherAfterExposures()` indexes `Triggers[0]` with no
  empty-guard in the 3.2.0.9001 release, so a SmartExposure with no dither
  trigger throws `ArgumentOutOfRangeException` and the whole container fails
  validation (log spam: `SequenceContainer.cs|Validate|554`). PhotonScript
  omitted the trigger whenever guiding was off. Fix (generator): ALWAYS emit
  the trigger; `AfterExposures=0` disables dithering (Execute early-returns,
  Validate stays clean) - see `_smart_exposure` in nina_sequence_json.py. The
  develop branch of NINA added the `Triggers.Count > 0` guard, so a NINA update
  also fixes it.
- **Plate solve failing 100%** (2026-07-26): ASTAP "Plate solve failed" x82,
  zero successes, so `Center`/`Slew and center` never completes and no target
  is acquired. Cross-check the SOLVE conditions before blaming the sky: last
  night it solved through **L** filter (20s, bin2, gain300 - correct, plenty of
  stars), scale hint FocalLength 3248 / PixelSize 7.52 was right, and autofocus
  completed (5853->6104). 100% failure on well-exposed L frames on a clear,
  roof-open night points to ASTAP setup, not the frames: verify the ASTAP star
  database (D50/G05) is installed and its path is set in NINA, and that blind
  failover (ASTAP as blind solver) actually fires. This is the recurring
  plate-solver battle - grep `astap|platesolv|Center|filter to` and check for
  any "solved" line to confirm.

### Two-error rule
Don't stop at the first cause. A dead night can stack independent failures
(missing folder + dither crash + plate-solve failure all on 2026-07-26). After
fixing one, re-check the LIVE log tail: if the fixed error stops but a
different one keeps firing at a newer timestamp, there's more to do.
