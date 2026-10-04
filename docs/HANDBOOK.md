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
- Tailscale: `100.94.189.77` - dashboard at `https://teles-feb25.lobster-bleak.ts.net`
  (Tailscale `serve` -> 127.0.0.1:8100; raw `http://100.94.189.77:8100` still works on the tailnet)
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
- Software Bisque PARAMOUNT MX (with encoders), driven via the TheSky64 10.5
  ASCOM driver (NINA shows the mount as `ASCOM.SoftwareBisque`). Earlier docs
  wrongly said "CEM70G" - that was a documentation guess, not what the driver
  reports; corrected 2026-09-04, see HARDWARE.md. TPoint + ProTrack available.
  Historically UNGUIDED; PHD2 present. Guiding now supported:
  `PS_GUIDED_DEFAULT=true` inserts StartGuiding after centering,
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
git add <files>; git commit -m "commit message"
.\deploy\deploy.ps1
```
ships only COMMITTED work (PS-56): refuses (and lists) a dirty tree, since
other sessions leave unfinished work in this checkout; `-IncludeWorkingTree
"msg"` commits everything after showing `git status` and asking. Then it
pulls --rebase, runs the test gate on exactly what ships, shows
`origin/main..HEAD`, pushes, POSTs `/api/update` on the scope, which stops
gracefully (exit 42), and waits for `GET /api/health` to report the pushed SHA.
Refused with 409 while RUNNING or PAUSED_UNSAFE, while a grading job writes,
and while ARMED unless `-AllowArmed` (the armed night is restored).
PS-58: the scope wrapper runs `photonscript self-update` with the OLD code:
fetch, check the new commit out into a staging worktree, import every module
from it (and the fast tests if `PS_UPDATE_SMOKE_TESTS=true`), and only then
fast-forward. The supervisor then waits `PS_UPDATE_VERIFY_S` (90 s) for
`/api/health` to report the new SHA; if it does not, the wrapper runs
`git reset --hard <previous SHA>`, sends "PhotonScript rolled back" and skips
that SHA until a newer one is pushed. deploy.ps1 prints "ROLLED BACK" or
"REFUSED" from the health `update` field. State: `<data_dir>\update_state.json`.
Check a pending update on the scope without switching: `photonscript self-update --dry-run`.
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
3. **Live watcher** grades each sub (sep HFR/ecc/FWHM on the NATIVE
   0.24"/px frame; RC16 subs are also measured on a 2x2-binned copy,
   `ecc_bin` at 0.48"/px, PS-94), skips calibration frames (path part or
   IMAGETYP != LIGHT), applies tracking-RMS rejection only while PHD2 reports
   guiding/settling. The backfill grader measures the 2x2-binned frame (plus
   native ecc with the live pipeline). Every ecc is sqrt(1-(b/a)^2); records
   say so in `ecc_def` (older backfill records held 1-b/a and are converted
   on read and on rescore). `qa_ecc_scale` (native, default, or binned)
   picks which scale gates; the other is shown as info only.
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
- 2026-09-26 (RC16 imaged 38 min into a closed roof, PS-77): the generated
  NINA JSON had no `$id`/`Parent` references, and NINA sets Parent ONLY from
  them, so every item loaded with Parent == null. CanContinue then never
  reached the Safety/Altitude/dawn conditions above a SmartExposure and the
  5 s safety watchdog could not interrupt. That is also why NINA never wrote
  OBJECT (PS-51) and why "a parent TimeCondition does not pull a running
  instruction out" (below). Fix: `link_parents()` on every generated tree,
  Safety + loop-end TimeCondition on every light SmartExposure (lint rules
  `parent-links`, `light-loop-safety`, `light-loop-end`), and the armer
  stops NINA itself if SAFE_LOOP is still running 2 min into an unsafe read.
- 2026-09-26 (no dawn flats on either rig, PS-36): the roof closed for clouds
  at 11:39Z (AARO roof state `status.astronomyacres.com/WeatherData/
  dragonfly_state.json`: `closeReason`) and the NINA #2 companion sat in its
  light loop's unbounded WaitUntilSafe until NINA #2 restarted; a parent
  TimeCondition does not pull a running instruction out (the RC16 also started
  subs after its nautical-10 loop end). Latent and worse: the armer's dawn
  shutdown fired at astro dawn + 30, but both rigs' flats start at nautical
  dawn + 5, which at AARO is 28-37 min after astro dawn, so the shutdown parked
  and warmed everything before any flat, every clear dawn since 2026-09-15.
  Fixed: shutdown held until nautical dawn + 5 + `dawn_flats_window_min`
  (capped at sunrise; unsafe = shut down now), bounded waits in the companion,
  and the shutdown now stops NINA #2 as well. `/api/arm` shows
  `shutdown_due_utc`. Note the "last sub 12:17:45Z" first blamed on the
  piggyback was an RC16 OIII sub.
- 2026-09-26 (safety watchdog was polling a 404): `NinaClient.get_safety_info`
  hit `/equipment/safetymonitor`, which ninaAPI v2 (2.2.15.2 on the scope)
  does not serve; the real endpoint is `/equipment/safetymonitor/info` (payload
  wrapped in `{"Response": ...}`). Every poll failed, so the watchdog treated a
  HEALTHY monitor as blind: it cycled disconnect/connect every ~20 s (the NINA
  "Safety Monitor connected" toasts), sent false DISCONNECTED pushes, and the
  "slow AlpacaDynamic3 reads Connected:False" notes were this bug. Fixed; the
  watchdog also now (a) reconnects by NAME (`connect?to=<Id>`: the pinned
  `safety_monitor_device_id` / `piggyback_safety_monitor_device_id`, else the
  Id last seen connected) and (b) idles while the sun is up and nothing is
  armed. Drivers as of today: NINA #1 (:1888) = ASCOM.AlpacaDynamic3
  (chooser name "OBS2 OSC Scope"), NINA #2 (:1889) = ASCOM.AlpacaDynamic4
  ("OBS2 Safety 2026"); AlpacaDynamic2 is the Alpaca SIMULATOR, never use it.
  OTHER NinaClient reads (camera/mount/focuser/sequence) use the same
  pre-v2 paths and still 404, so the agent's camera poll, cooling and dew
  watchdogs are inert until that is fixed (tracked separately).
- 2026-09-26 (both scopes imaging + warm-vs-arm): three durable fixes.
  (a) **OSC finally shoots lights on NINA #2.** Root cause it never did before:
  NINA #1 and #2 shared ONE AlpacaDynamic safety-monitor driver, and the
  driver's TraceLogger holds an exclusive file lock — the second instance to
  read it threw "trace log file used by another process", so NINA #2's
  `safetymonitor/info` failed, `has_safety` came back false, and the piggyback
  companion dispatched flats/darks ONLY, never lights. Fix: a SECOND Alpaca
  safety driver (AlpacaDynamic2) dedicated to NINA #2 (or disable driver trace
  logging). has_safety is auto-detected at arm, so once the driver reads clean
  the OSC images unattended.
  (b) **Gradual camera warm disabled** (`config.gradual_warm_minutes`, default
  0 = instant). A multi-minute WarmCamera ramp on cooler-off fought the next
  arm/precool — it kept pushing the sensor temp back UP while the arm wanted to
  cool now. Cutting the TEC (minutes=0) is already a soft, passive warm; the
  "gentler on the sensor" argument for the ramp doesn't hold. Threaded through
  every warm path: arm cooler-off, dawn shutdown, disarm make-safe, End area.
  Set >0 only to bring the old ramp back.
  (c) **Donut/AF night** (dead focus temperature model): `focus_seeds.
  harvest_night()` was never called and `_seed_position` got `ambient_c=None`
  with no config, so AF had no temperature seed and failed to bracket. Revived:
  harvest wired into the backfill `_work()`, and `_seed_position` now passes
  config + a live focuser temp (60 s cached GET `/equipment/focuser`).
- 2026-09-25 (M31_OSC2 double galaxy): half the Piggy-600 subs straddled RC16
  mount moves between two pointings ~51' apart, and the OSC script re-stacked
  stale intermediates (63 subs -> 125 frames). Fixed with osc_cull.py (split /
  duplicate cull before PixInsight) and per-run clearing + funnel assertions in
  integrate_osc.js. Details: OSC_INTEGRATION.md 1a. Piggyback slew gating
  (DUAL_RIG Phase 4) is now the real fix.
- 2026-09-04 (month of trailed subs): every light since 2026-07-28 drifts
  ~1-2 arcsec/min at a constant position angle - polar alignment error
  (~5-8 arcmin azimuth), NOT sidereal rate (encoders hold RA; drift is
  dec-dominated). Fix at the mount: NINA Three Point Polar Alignment.
  QA never caught it because the extractor was grading ~36k hot pixels
  (HFR 0.42, ecc 0.008) instead of stars, and the live watcher parsed
  NINA's <date>_<time>__<F>_<exp>s filename with split('_'), logging
  everything as L/300s/target=<date>. All three fixed: 3x3 median before
  sep + real-star selection + sqrt-form ecc, header-first metadata,
  star-flood/HFR gates in the live validator.
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
- The sandbox/cloud CANNOT reach Tailscale. The ONLY path to the dashboard/API
  is the Claude-in-Chrome extension, which IS Jeremy's home desktop's Chrome
  (browser "pc windows at home") - the machine that has Tailscale. Always drive
  the dashboard through Claude-in-Chrome (navigate + javascript_tool fetch);
  never expect the sandbox to reach `100.94.189.77`.
- DASHBOARD URL (2026-09-26, final): `https://teles-feb25.lobster-bleak.ts.net/`
  via `tailscale serve --bg http://127.0.0.1:8100` (set as `teles-feb25\sleep`).
  The in-app uvicorn TLS listener (:8443, `provision-tls.ps1`) was REMOVED: the
  service runs as `jeremy`, and tailscaled refuses `tailscale cert` from a
  non-owner account, so it never came up. Direct `http://...:8100` still works.
- DASHBOARD ACCESS (RESOLVED 2026-09-24): the canonical remote URL is the
  Tailscale HTTPS hostname `https://teles-feb25.lobster-bleak.ts.net/`
  (`serve` -> `127.0.0.1:8100` on the scope PC). Verified end-to-end: a live
  `.../api/runs` fetch returns JSON through Claude-in-Chrome from the home
  desktop. Because it's a normal https host (not a raw IP), a one-time "site"
  approval sticks, so the unattended 8:04 AM debrief no longer bounces to
  chrome://newtab. Prefer this hostname everywhere; the raw
  `http://100.94.189.77:8100` still works on the tailnet as a fallback.
  BACKGROUND (the old raw-IP blocker): `http://100.94.189.77:8100` is a RAW-IP
  http site, so Chrome demanded a per-action approval on navigation that an
  unattended run had nobody to click (nights logged Status=unreachable, e.g.
  2026-09-21). That is what the hostname fixes.
  HOW IT WAS SET UP (gotchas, in case it needs redoing):
    * `serve` MUST run on the scope PC itself (`teles-feb25`), not the home
      desktop - a node cannot reach its own `serve` hostname, so a desktop-hosted
      serve is unreachable by the desktop's own Claude-in-Chrome.
    * `serve` config MUST be set as the tailscaled owner (`teles-feb25\sleep`) -
      Administrator does NOT override the identity check (401 Unauthorized:
      "connection from teles-feb25\jeremy not allowed"). Use
      `runas /user:teles-feb25\sleep powershell`, then `tailscale serve --bg 8100`.
      Optional: `tailscale set --operator=jeremy` so jeremy can manage it later.
    * Mint the cert from a writable dir (cd `C:\Users\sleep` first; `system32`
      gives "Access is denied"): `tailscale cert teles-feb25.lobster-bleak.ts.net`.
    * Requires MagicDNS + HTTPS certs enabled in the tailnet admin console (they
      are). `tailscale set --auto-update` is on.
    * `deploy.ps1` intentionally still defaults `$Scope` to the raw IP - it runs
      ON the scope PC, where the same-node loopback rule makes the hostname
      unreachable. Leave deploy pointed at `http://100.94.189.77:8100`/localhost.
- "SERVE PROXY IS FLAKY" WAS THE APP, NOT THE PROXY (2026-09-26 diagnosis):
  measured from the desktop, `/` took ~13.5 s DIRECT on :8100 and ~14 s via
  serve; `/api/runs` ~50 s. serve adds ~0.3-0.5 s. Causes: the dashboard ranked
  every seasonal target with thousands of scalar astropy transforms INSIDE an
  async handler (freezing the one event loop that also runs the telescope
  agents), and `/api/runs` rglob'd every night + re-parsed every subs log +
  walked Syncthing on every poll. Fixed in the perf pass (vectorized
  astronomy + caches + worker threads). If a 502 shows up again, time the same
  URL on :8100 before blaming serve. `deploy.ps1` still posts to :8100 direct.
- SERVICE (scope PC, PS-44): runs from the `PhotonScript` scheduled task
  (`deploy\install-autostart.ps1`; default `-LogonType Interactive` = at
  jeremy's logon, since S4U/session 0 went slow on 2026-09-26, PS-55),
  wrapper -> `photonscript supervise`,
  which restarts it after a crash with backoff and alerts via Pushover.
  `photonscript stop` / `restart` / `status` / `monitor`; start with
  `Start-ScheduledTask PhotonScript`. Paths are `C:\astro`, NOT the desktop's
  `C:\dev`. Details: docs/MAINTENANCE.md "Running the service".
- Scheduled tasks: `photonscript-morning-debrief` (daily 8:04 AM), `PhotonScript` (scope PC, at startup).
- Constraints: no GitHub pushes, no credentials, no AstroBin scraping for
  data tables, no writes into ninashare, scope deploys refused mid-night.

## 8. Current state (2026-09-26)

- DUAL-RIG IMAGING IS LIVE: both scopes shoot lights on one arm. RC16 (NINA #1,
  mono AP26MC, OAG->PHD2) and the piggyback 600 (NINA #2, OSC AP26CC, rides the
  mount unguided) both at setpoint 0C. The OSC unblock was the second Alpaca
  safety driver (see 2026-09-26 Night-ops lesson). Keep the OSC setpoint at 0C
  so it matches `piggyback_dark_exposures=120` darks.
- GUIDING: PHD2 calibrated near Dec 0 / meridian via Calibration Assistant with
  "Auto restore calibration" ON and NINA Force Calibration OFF; ~0.36" RMS
  achieved. TPoint model built (4x4 bin, wait for dark, image scale 0.942"/px)
  with ProTrack enabled.
- PHD2 CALIBRATION MANAGER (PS-93): PhotonScript decides when PHD2 calibrates,
  where, and whether the result is good. Every completed calibration is graded
  from `get_calibration_data` plus NINA's mount: PASS = axes within 5 deg of
  perpendicular, RA/Dec rate ratio within 30% of cos(Dec), 8+ steps per axis;
  FAIL = failed / aborted, a star that did not move, ortho over 5 deg, ratio off
  (judged within 60 deg of Dec 0), parity flipped against the last good one on
  that pier; WARN = far from Dec 0 / the meridian, absolute rate 30% off the
  guide speed, few steps. With `phd2_cal_mode=auto` a guided night gets a NINA
  `PHD2_CALIBRATION` slot (L filter, slew + center on a field at Dec -5..+15,
  0.25..1 h from the meridian on the first target's side, StartGuiding with
  ForceCalibration, StopGuiding, `phd2_cal_hold_s` hold) only when there is no
  calibration on record, the last one FAILED, it is older than
  `phd2_cal_max_age_days`, the PHD2 profile / binning / scale changed, or one
  was asked for (Guiding tab or `POST /api/phd2/calibrate`). The slot sits after
  the twilight AF and the PS-92 twilight self-test, before the imaging gate (on
  a late arm or re-dispatch: right after the unpark); with it no target forces
  a calibration. A FAIL inside the slot is retried once over PHD2 during the
  hold; a second FAIL keeps guiding on it and alerts once
  (`phd2_cal_fail_action=keep`). A profile / binning change or an uncalibrated
  PHD2 mid-night triggers one re-dispatch with the slot (1 h of dark left).
  After a meridian flip the first 3 min of guiding are checked for a Dec
  runaway; a runaway alerts with the "Reverse Dec output after meridian flip"
  fix (`phd2_flip_action=alert`), a clean pass marks the pier side verified.
  Record: `<data_dir>/phd2/calibration.json` (+ `calibrations.jsonl`),
  `GET /api/phd2/calibration`, the Guiding tab section and the runs page line.
  On an empty store the newest guide-log calibration is seeded and graded.
- PHD2 SETTINGS AUDIT (PS-89): `config/phd2/desired_oag_rc16.toml` holds the
  desired PHD2 / NINA / mount-driver state with a "why" per setting. Every
  guided arm audits it in the background (one push only on a FAIL) and the
  RC16 agent re-audits after a PHD2 configuration change. Guiding tab section
  "PHD2 Settings Audit", `GET /api/phd2/audit`, runs page line, preflight
  line. Apply: API keys (exposure, RA min-move, Dec guide mode) while PHD2 is
  Stopped or Looping; profile (registry) writes only behind
  `phd2_audit_autofix` (off) with PHD2 closed and a backup; ASCOM, TheSky and
  NINA rows report only. Registry names are unverified until a `reg export`
  from the scope PC (MAINTENANCE.md). `pe_owner=protrack`: PHD2 PPEC off.
- GUIDE-STAR AUTO-TUNE (PS-90): the RC16 agent measures the guide star after
  each settle and filter change (peak % of full scale, clipped, SNR, HFD).
  `phd2_tune_mode=observe` (default) records only; `exposure` steps PHD2's
  exposure toward a 60 to 80% peak within 1 to 4 s (dark-library exposures
  only). Gain advice for the next night (also the audit's gain row) is
  written pre-dusk only behind `phd2_audit_autofix`; binning stays at 2 and
  the report says from the measured HFD whether bin 3 would hit 2 to 5 px.
  `GET /api/phd2/tuning`, Guiding tab "Guide Star Tuner", runs page line.
- SHIPPED this session (see AUDIT-2026-09.md): revived focus-seed temperature
  model; cross-night polar-drift/optical-tilt/focus-drift trend alarm
  (trends.py, `/api/trends`); guided-but-not-guiding watchdog; meridian guard
  on plan open; OSC darks/bias fire unconditionally (dusk-capped) when no safety
  monitor; per-run contact sheets + sidenav archive; disk-space on the run view;
  `/api/runs/{date}` hang fix; deploy test/lint gate; `gradual_warm_minutes`
  (instant warm). Catalog: added NGC 6543 (Cat's Eye) + NGC 7008, widened
  seasonal windows.
- Calibration in library: 32x300s darks @0C/offset256, 50 bias, full flat set
  (verified: darks median ~257-330 ADU = offset floor; flats ~50% full well).
  OSC (piggyback) calibration now auto-captured on arm.
- Mosaic planner at `/mosaic`: panel grid over a DSS2 hips2fits cutout, one goal
  per panel.
- Guiding tab at `/guiding` (PS-103): the working surface for PHD2. Live state
  (`GET /api/phd2/live`: app state, RMS, SNR, HFD, exposure, binning, scale,
  lock position, phd2_ops owner), then the PS-89 audit (Refresh, per-row dry
  run then Apply), PS-92 self-test, PS-93 calibration, PS-91 guard and
  hot-pixel map, PS-90 tuner and tonight's PS-88 guide-log summary. Panel JS
  is static/js/phd2_panels.js; the System page keeps one status line each.
- THESKY / TPOINT AUDIT (PS-104): report only. `config/thesky/desired_rc16.toml`
  holds the desired TheSky state (site and clock, mount, TheSky's camera
  add-on, Automated Image Link settings, catalogs, TPoint and ProTrack, NINA
  first-slew error) with a why and a fix per row. Sources: TheSky's TCP
  scripting (port 3040) with read-only scripts only
  (`telescope_agent/thesky_client.READ_ONLY_JS`; a test greps them for
  connect / park / slew / sync / jog / tracking / take image / setting
  writes), the ASTAP Image Link check on the newest RC16 L frame
  (solve_store), the NINA Center log (`scheduler/nina_center_log.py`,
  `<data_dir>/pointing/<night>_center.jsonl`) and the manual TPoint record
  (`<data_dir>/thesky/manual.json`, entered on the Guiding tab after each
  TPoint session: TPoint numbers, ProTrack, run binning and catalogs are not
  scriptable). Runs at arm (no push) and on Refresh; Guiding tab section
  "TheSky / TPoint", `GET /api/thesky/audit`, `/api/thesky/imagelink-check`,
  `/api/thesky/pointing`, `POST /api/thesky/manual`, CLI `photonscript
  thesky-audit`, one runs-page line. The rebuild flag says REBUILD when the
  model is over `tpoint_max_age_days`, the equipment changed after it
  (camera angle, scale, the 2026-09-12 dual-rig change) or the 14-night
  first-slew median exceeds `pointing_first_slew_fail_arcmin`. TheSky's own
  Image Link runs on a temporary copy only from the button and only while the
  armer is idle. PS-104 also removed `thesky_client.set_protrack`
  (undocumented DoCommandStr) and made `get_mount_status` read without
  `Connect()` (which may unpark). Reading the audit and the on-site check:
  MAINTENANCE.md.
- OPEN THREADS: `gradual_warm_minutes` change is staged in the desktop repo but
  NOT yet deployed (deploy after a dawn shutdown, never mid-run - deploy 409s
  while armed). Confirm the OSC dark library has matching 120s @0C darks.

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
