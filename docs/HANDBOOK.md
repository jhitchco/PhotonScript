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
- RC16 (406mm) at 3248mm f/8; scale 0.236"/px (plate-solved; config pixel_scale_arcsec); FOV 0.408 x 0.273 deg
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
Refused with 409 while RUNNING, PAUSED_UNSAFE or WATCHING, while a grading job writes,
while ARMED unless `-AllowArmed` (the armed night is restored), and while
PAUSED_OPERATOR (PS-64) unless `-AllowPaused` (2026-10-14): then only with the
pause settled ("paused", no Resume / Restart in flight), NINA #1 and #2 idle
and no capture job, and the night comes back PAUSED_OPERATOR with NO device
command (no connect-all, unguided guard, park, warm, cooler, load). Made for a
TPoint run or polar alignment during a pause; Resume afterwards re-plans on the
new code. Details: MAINTENANCE.md "Deploying while paused".
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
   **Cooler gate (PS-61).** Each RC16 light block is its own container
   "<target> filter block (cooler-gated)" that starts with a NINA
   ExternalScript running `deploy\cooler-gate.cmd` (the Piggy-600's OSC
   image pass starts with the same gate). It holds until the sensor is
   within `cooler_gate_tolerance_c` (1.0 C) of the temperature the sequence
   cooled to, at most `cooler_gate_timeout_min` (20). On timeout one
   priority Pushover "RC16: not imaging <target> <filter>, sensor at X C vs
   setpoint Y C after N min; check the cooler" (a reminder hourly, a
   "back at setpoint" notice on recovery), and in `cooler_gate_mode=skip`
   the script exits 1 so ErrorBehavior 1 skips that block only; the
   imaging loop retries it next pass. `warn` alerts and images anyway,
   `off` emits no gate. Fails open: sensor unreadable, service down or
   script missing never skip (a missing script is announced at dusk and is
   a lint warning). The dashboard camera row shows "waiting for cooler:
   X C -> Y C" / "not imaging: ..."; `GET /api/cooler/gate` has the live
   state and recent results (`<data_dir>/cooler_gate.jsonl`). Lint rule
   `cooler-gate`: every light loop must have the gate before it.
   **Cooler-stall recovery (2026-10-17, `shared/cooler_recovery.py`).**
   Jeremy's rule "when it's not cooling after a bit, disconnect and
   reconnect the camera", automated. While the armer expects cooling
   (ARMED / RUNNING / PAUSED_* / WATCHING inside dusk minus
   `cool_lead_minutes` to dawn) the telescope agent of each rig, and a
   calibration capture job while it is "cooling", watch for a stall over
   `cooler_stall_window_min` (8) with the sensor above setpoint + tolerance:
   cooler OFF, TEC under `cooler_stall_power_pct` (3 %), or a drop under
   `cooler_stall_drop_c` (2 C) unless the TEC is flat out
   (`cooler_stall_flat_out_pct`, 90 %). Then, never while the camera is
   exposing (IsExposing / CameraState, read fresh, unknown = wait), it
   disconnects THAT camera only, waits `cooler_stall_reconnect_wait_s` (5),
   connects it and cools to the rig setpoint. At most
   `cooler_stall_max_attempts` (2) per rig per night,
   `cooler_stall_spacing_min` (15) apart, shared by the agent and capture
   jobs through `<data_dir>/cooler_recovery.json`. Each step is a night
   event (src `cooler`, kind `cooler_recovery`); each attempt ends in one
   Pushover (recovered / still stalled / errored). A reconnect during a
   cooler gate wait extends that gate once by
   `cooler_stall_gate_extend_min` (15). `cooler_stall_recovery=false` turns
   it off. It replaced the old agent watchdog (CoolerOn at 0 %, 3 tries).
   **Settle gate (PS-27, Piggy-600 only).** In the companion's OSC light
   loop NINA #2 runs `deploy\settle-gate.cmd` (ExternalScript, ErrorBehavior
   0) once before the loop and after every OSC light, so each light starts
   only when the RC16 mount is not slewing, has been still for
   `PS_PIGGYBACK_SETTLE_STILL_S` (6 s) and PHD2 is not settling; at most
   `PS_PIGGYBACK_SETTLE_TIMEOUT_S` (90 s), then it shoots anyway. It reads
   NINA #1's mount info (GET only) and never writes to NINA #1: the RC16
   keeps the timing. Always exit 0, fails open (service down, NINA #1
   unreadable, script missing: annotation + lint warning). Optional
   `PS_PIGGYBACK_ABORT_ON_MOVE` (default off until night-tested): on a
   slew, flip or a jump over `PS_PIGGYBACK_ABORT_MOVE_ARCMIN` seen by the
   RC16 agent's mount poll while NINA #2 exposes an OSC light, PhotonScript
   calls NINA #2's `/equipment/camera/abort-exposure`; the loop then waits
   at the gate and starts a fresh sub. Holds and aborts go to
   `runs/<night>_events.jsonl` (src `photonscript`, kinds `settle_gate`,
   `split_abort`). The runs page "Piggy-600 split pointing" line (and
   `GET /api/runs/{date}/split`) gives straddled / (judged + aborted)
   against the 5% pass line; `GET /api/piggyback/split-guard` shows the
   live state. Lint rule `settle-gate`.
3. **Live watcher** grades each sub (sep HFR/ecc/FWHM on the NATIVE
   0.236"/px frame; RC16 subs are also measured on a 2x2-binned copy,
   `ecc_bin` at 0.47"/px, PS-94), skips calibration frames (path part or
   IMAGETYP != LIGHT), applies tracking-RMS rejection only while PHD2 reports
   guiding/settling. PS-83: the live and the backfill grader measure with
   ONE function, `shared.star_measure.measure_frame`, on the same native
   frame (HFR, FWHM, ecc, star count capped at the brightest 400,
   background, noise, swamp, saturation, `ecc_bin`), so the same sub gets
   the same numbers from either; records say `measure_v`. Backfill records
   from before PS-83 hold a binned HFR about 2x the live one (13 to 15 px
   vs 6.5 to 7 px on the 09-26 Crescent Ha subs), an uncapped star count
   and HFR x scale as FWHM (not judged). PS-130: `photonscript qa-rescore
   --remeasure --date D` (or `--all-before-ps83`; dry run unless `--apply`,
   API POST `/api/runs/{date}/remeasure`) re-measures just those records
   from their FITS with measure_frame and re-judges the night; human
   verdicts and review fields are never touched, missing FITS are skipped
   and counted, the old numbers stay under `pre_ps83`. Plain `qa-rescore`
   only re-judges stored numbers. A full re-grade (Re-grade night / Re-grade
   all, `photonscript regrade --date D | --since D`) re-measures every sub;
   PS-141: it replaces each record in place and keeps a person's verdict
   (and a target assigned by hand), `--discard-manual` (the tick box on the
   Runs page) is the old wipe. Every ecc is
   sqrt(1-(b/a)^2); records say so in `ecc_def` (older backfill records held
   1-b/a and are converted on read and on rescore). `qa_ecc_scale` (native,
   default, or binned) picks which scale gates; the other is shown as info
   only. **Narrowband grading (PS-146, measure_v ps83.2).** On 3 nm subs
   faint stars read 0.03 to 0.15 more elongated than the bright ones, so
   the judged ecc is the median of the brightest `qa_ecc_bright_n` (50)
   stars (or every star at peak SNR >= `qa_ecc_bright_snr` when set) when
   `qa_ecc_bright_rule` says so (auto: a narrowband filter in
   `qa_ecc_bright_filters`, an "under" exposure or fewer than
   `qa_ecc_bright_min_stars` 150 stars; always; off). `ecc_all` keeps the
   all-star median, `ecc_src` / `ecc_why` say which was judged, and the
   scorecard row reads "brightest 50 stars, narrowband (all stars 0.71)".
   The FWHM is 2.355 x sep's threshold moment, which reads faint defocused
   stars far too small (Ha 2026-10-05: 1.6 to 2.8" with HFR 10.5 px, about
   5"): when it is under `qa_fwhm_hfr_ratio` (1.2) x HFR the sub is flagged
   `fwhm_unreliable` and the judged FWHM is 2 x HFR (`qa_fwhm_method` auto;
   moment = the old value, hfr = always), `fwhm_moment_arcsec` keeps the
   moment, the row says "from 2 x HFR". Plain `qa-rescore` derives both for
   older PS-83 and live records from their stored numbers and star sidecar
   (`shape_from` "stored", by rank only), dry run first.
4. Morning: runs page `/runs/YYYY-MM-DD` (permalinks work) shows plan vs
   actual, the honest funnel (dark hours -> shutter hours -> accepted hours;
   sky utilization = accepted/dark), 4-state sub review
   (review/accepted-syncing/transferred/rejected; X key cycles states,
   thumbnail corner buttons give one-click verdicts). **Approve night** queues
   accepted subs into the Library -> Syncthing carries them to the desktop.
   **Review speed (PS-24).** A verdict repaints at once and never reloads
   the night (static/js/review.js): the tile, table row, counters
   ("reviewed N of M") and lightbox change immediately, the POST runs in the
   background and a refused one rolls back with a red toast. Keys: Enter
   opens the first sub to review, A accept / R reject (then the next sub to
   review), U undo, arrows, X, Esc (closing never refetches). Shift / ctrl
   click or "Select all shown" + Accept / Reject applies one verdict to many
   subs (`POST /api/runs/{date}/qa-batch`). The grid draws every sub (lazy
   tiles), the lightbox preview is a JPEG and the next two are prefetched.
   The Targets page tiles carry the same accept / reject buttons. Server:
   one lock per night shared with the live appender, atomic subs-log
   rewrites, only that sub's Library links change, goal progress follows
   about 2 s after the last verdict (`/review-summary` and the target page
   flush it first). PS-140: every subs-log writer goes through
   `runs.edit_subs` (re-score, attribution and identify, pointing and slew
   passes, PS-71 / PS-78 backfills, re-measure, assign target), so a verdict
   given while one runs is never overwritten by its stale copy: metadata
   passes hold the night lock from load to rewrite; passes with slow work
   (identify, Piggy attribution, pointing) merge only the fields they
   changed, and a verdict given meanwhile keeps every verdict field.
   PS-141: the pointing pass decides its Library moves from the merged
   log under the lock, so a sub accepted during the pass keeps its link.
   PS-147: so does Piggy attribution (`piggy-attribution --apply`): a sub
   whose target a person changed during the pass keeps its links. A sub
   accepted after a move put its link in Library/_rejected loses that
   stale copy (only the same file as its original or its live link; never
   in the desktop mirror); build_library cleans the same way. A sub's
   `file` is stored with "/" (shared.sub_file); every reader normalizes, so
   older logs with the OS separator still match.
   The subs show as a Grid or a Table (PS-116, remembered per browser): the
   table has a small thumbnail, score, verdict and the core stats per sub,
   sortable (lowest score first by default), and the rig / target / filter /
   off-target / timeline chips apply to both. The lightbox side panel
   (PS-115) shows a Target section on top (target, filter / exposure, the
   offset from the target with its source, alt, pier, HA, and the reference
   image with this rig's frame and the pointed position), one score line,
   then the metric bars sorted by points lost; unmeasured rows fold into one
   "not measured" line.
   **Sub score (PS-108).** Every sub also gets a 0 to 100 score from its
   PS-21 scorecard: each judged check graded by how far inside or outside
   its gate it sits, weighted per rig (`config/qa/score_weights.toml`, a
   "why" per weight), capped by hard fails (roof closed / parked 0,
   solve-confirmed off target 20, slew straddle 20, non-star guiding at
   FAIL / warm sensor / doubled stars / star count 40; any other failed gate
   79, or 59 when far past it). Approved 2026-10-04: 80 and up approve on
   both rigs, 60 to 79 review, under 60 reject (`qa_score_approve`,
   `qa_score_reject`). `qa_score_mode=preview` (default) records the score
   and shows what it WOULD do (lightbox side panel, runs-page line,
   `photonscript score-report --date D [--json]`) and changes no verdict:
   the PS-21 rule stays in force (all-green auto-approve on the RC16 only,
   any failed check rejects). Transition to `on` (System page): from then on
   new subs take their verdict from the score on both rigs and the all-green
   rule (`qa_auto_approve`, `qa_auto_approve_rigs`) is no longer used; a
   failed gate near its limit now waits for review instead of rejecting.
   Past nights keep their verdicts until `photonscript qa-rescore --date D
   --apply` (add `--allow-unreject` to let the score move rejected subs back
   to review). Human verdicts always win, in both modes. The lightbox also
   shows the full-resolution histogram (PS-5, cached under
   `<data_dir>/hist/`, warmed at dawn with the thumbnails; fetched only
   while its section is open and the sub stays on screen 0.3 s): L for mono,
   R / G / B Bayer sites for OSC with their medians, log / linear toggle,
   marks for the bias floor, median and saturation, and a red edge bar for
   black (0 ADU) or white clipping. Also the new pixel counts both graders record:
   saturated pixels (>= `qa_saturation_adu` 65000, FITS SATURATE wins),
   pixels at 0, max ADU, background median and MAD.
   **Gates per rig (PS-114).** Every QA gate has an RC16 key and a
   Piggy-600 override (`shared/rigs.py` PIGGYBACK_GATES, env
   `PS_PIGGYBACK_...`; a blank Piggy key uses the RC16 value). The System
   page "QA gates per rig" table shows both rigs side by side
   (`GET /api/qa/gates`). The Piggy-600 FWHM gate is 15" (its live OSC
   measure reads normal stars at 8 to 12"; the score gives full marks up to
   80% of a gate). `photonscript qa-baselines [--rig R] [--nights N] [--k K]
   [--json]` (or the System page button, `GET /api/qa/baselines`) gives each
   rig's median and MAD per filter of FWHM, HFR, ecc, stars and background
   over its accepted subs and proposes gates (median + k x 1.4826 x MAD,
   `qa_baseline_k` 3) next to the gates in force. Report only: change a gate
   by hand, then `qa-rescore --date D` (dry run) shows how many verdicts and
   scores move (`score_changed`, `score_up`, `score_down`).
   **Per-target gates (PS-48).** A goal can tighten (or loosen) Max HFR,
   Max FWHM and Max eccentricity on one rig, e.g. 6 px HFR on the RC16 for
   a small bright planetary where sharpness is the point. Edit them on the
   target page ("QA gates for this target", blank = the rig gate; API `GET /
   POST /api/targets/qa-overrides`); they are stored on the project
   (`qa_overrides` in projects.json, per rig) and `qa_rules.thresholds`
   applies them for both graders, live and rescore, matching the goal by
   name, catalog id or a PS-135 catalog alias. The ecc override also sets the binned ecc gate. The
   scorecard panel shows such a gate as "<= 6 px (target override)" and a
   reject reason ends "(target override)". New subs only: a graded night
   moves after `qa-rescore --date D` (dry run first).
   **Viewer tools (PS-80, PS-6, PS-17; static/js/viewer.js).** A row under
   the lightbox bar: **S** star overlay, the grader's own stars from the
   PS-80 sidecar (`GET /api/runs/{date}/stars`; a sub without one is
   measured once on view with the backfill measure and cached, labeled
   "measured on view"), circles about 2 x HFR, **C** colors by HFR or ecc
   (green under 90% of the rig's gate, yellow to the gate, red past it; ecc
   draws ellipses along the stretch), hover a star for HFR / ecc / x, y;
   follows the click / wheel zoom. **L** loupe: hover for a 256 px
   full-resolution crop under the cursor (`GET /api/runs/{date}/crop`,
   memmap window, the preview's stretch, OSC as the 2x2 superpixel), click
   pins it, 1:1 / 2:1, nearest star's HFR / ecc. **M** 3x3: corners, edge
   midpoints and center at 1:1 in one PNG (`/mosaic`, 128 / 256 px tiles,
   cached under `<data_dir>/thumbs/`), per tile the median HFR / ecc of the
   zone's sidecar stars (`/mosaic-info`; else the backfill corner_ecc).
   Crops never take the grading lock; at most two run at once.
5. Daily 8:04 AM scheduled Claude task fetches `/api/runs`, `/api/sync`,
   `/api/calibration/health` via Chrome and writes a debrief.

### Sideload a custom night (PS-123)

For a night the armer cannot plan (e.g. a tracking test first, then
tonight's targets), the dashboard's Target Goals card has a **Sideload** box:
pick a recipe, tick targets to exclude, **Preview** (lint + container tree
for each rig), then **Load into RC16 / Piggy-600** (confirm dialog). It LOADS
the sequence into that NINA and never starts it: press Start in NINA. The
armer is not involved and stays DISARMED.

- Recipe `tracking_test_then_tonight`: RC16 = tonight's sequence (same as
  the download: start area waits for dusk) with the PS-84 tracking test on an
  auto-picked field (`?at=<UTC>` picks for a later time) as the first item of
  the Targets area, before LOOP_ALL_NIGHT, and tonight's targets minus the
  excluded ones in TARGETS_CONTAINER; ids and Parent links rebuilt (PS-77). Piggy-600 = the companion with
  `has_safety=True` and lights (`piggyback_image_lights`), generated on the
  scope so its dark quotas see the real library.
- API: `GET /api/sequence/sideload/preview?recipe=&exclude=&exclude=&at=`;
  `POST /api/sequence/sideload?rig=rc16|piggyback&recipe=...` or a JSON body
  (the sequence, or `{"sequence": {...}}`). Refusals: lint errors 422 (the
  Piggy companion uses its own lint: no mount moves, cold setpoint, Parent
  links, guarded light loops; PS-139: a hand-built JSON body on rig
  piggyback is graded, a mount instruction inside a loop or a trigger is
  an error, one outside any loop a warning); armer ARMED / RUNNING / PAUSED_UNSAFE 409;
  that NINA's sequence state unreadable 502 or anything RUNNING 409 (`GET
  /sequence/state`, falls back to `/sequence/json`); Piggy recipe while NINA
  #2 does not report the safety monitor connected 409.
- PS-132: after the load PhotonScript reads NINA's own validation (state
  Issues + Validate errors in that NINA's log): a Validate error answers 422
  / ok false (NINA would refuse Start), Issues are reported; both push once.
  See "Load validation" under failure modes.
- Every load saves `sequences/Sideload_<rig>_<name>_<stamp>.json` and logs
  a `kind: "sideload"` line in `runs/<night>_events.jsonl` plus an entry in
  the notification audit (`/api/notifications`, sent=false).
- PS-127: the tracking test runs at most once per night. Inside the night
  loop its LoopCondition(1) was reset each time LOOP_ALL_NIGHT looped after
  an unsafe pause, so the ladder ran again (about 1 h); the Targets area runs
  once and is never reset. Unsafe during the ladder: the rest of the ladder
  is skipped for tonight (the partial ladder still reports), the night loop
  parks and resumes tonight's targets. Unsafe when the Targets area starts:
  the test is skipped; re-sideload another night. The standalone tracking-test
  download keeps its park-and-hold after the ladder.

### Arm with a recipe (PS-181)

A sideload only loads; someone must press Start. For an unattended night,
arm with the recipe instead: Tonight's Run, **with recipe** (none / TPoint
mapping (N) / tracking test / optics test; N = points, empty = the config's
`tpoint_mapping_points`), then an Arm button. `POST /api/arm {"armed": true,
"guiding": ..., "recipe": "<recipe id>", "recipe_opts": {"points": 10,
"add": "off"|"auto"}}`; options are TPoint only (points clamp to 3..300,
`add` overrides `tpoint_sample_add` for this run and reaches the script as
`--add`); anything else is a 400.

- At pre-config the armer builds tonight's normal sequence, lints it, then
  splices the recipe's test before LOOP_ALL_NIGHT with the same builder as
  the sideload preview (`routers/sideload.build_rc16_from`; at = astro dusk
  when dispatching before it) and lints that. It loads AND starts it like a
  normal night, plus the Piggy companion as usual.
- The recipe will not build or lint: the normal plan is dispatched instead
  and one priority page says why (`arm_recipe` event `fallback`).
- One night only: kept in `armer_state.json` (`recipe`), shown on
  `GET /api/arm` and the armer card, cleared on disarm, on any new arm and
  at the dawn shutdown.
- Re-dispatches (unsafe resume, Resume after Pause, Restart tonight from
  now, recalibration, unguided fallback) first read NINA's sequence state
  (PS-183; on 2026-10-09 a PHD2 recalibration re-dispatch during startup
  dropped a TPoint recipe that had never started). Recipe container
  FINISHED: `done`, left out. Container RUNNING, anything inside it RUNNING
  or FINISHED, or any `TPoint point` container FINISHED: `started`, left
  out (a partial test is never repeated). Container CREATED or absent
  (never started, or NINA restarted): back to `armed` and spliced into this
  re-dispatch again. NINA unreadable: left out, as before.
  Events: `kind: "arm_recipe"` `dispatched` / `re-included` / `started` /
  `done` / `fallback`.

### Through-focus optics test (PS-148)

Answers "astigmatism / collimation, or a mechanical stretch?" when stars
show one fixed elongation axis (2026-10-05: ~33 deg in every zone of the Ha
subs, magnified by ~310 steps of defocus). About 16 min with the defaults.

How to run it:
1. Dashboard, Target Goals card, **Sideload** box: recipe **Through-focus
   optics test** (`optics_through_focus`), tick targets to exclude, **Preview**
   (the RC16 row shows the test field, its altitude and length, and lint
   PASS with an `optics-test` warning that says what it is).
2. **Load into RC16** (and **Load into Piggy-600** for the companion), confirm.
   Nothing starts: press **Start in NINA #1** (and NINA #2). The armer stays
   DISARMED. Load refuses while armed or while that NINA runs anything.
3. After the sweep, tonight's targets follow (each with its own AF). Open the
   runs page for the night: the Optics section gets a "Through-focus optics
   test" block (also `GET /api/optics-test/report?date=` or
   `photonscript optics-test-report --date D`).

What the sequence does (RC16, spliced into the Targets area before the night
loop, runs once): StopGuiding, sidereal tracking, slew to a field 50 to 70
deg up near the meridian (the PS-84 picker; `?at=<UTC>` picks for later),
AF on L, center, then per filter: subs at best focus, then at each offset in
ascending order (the focuser only moves outward inside a sweep), then back
to best focus (the moves add up to zero; lint rule `optics-test` checks it).
No autofocus triggers and no guiding inside. Each step is a nested
DeepSkyObjectContainer so every sub's OBJECT is
`Optics test <field> <filter> <offset>` (e.g. `Optics test M 2 L -300`).
Those subs are defocused on purpose: the telescope agent leaves them out of
the consecutive-reject alert. Unsafe mid-sweep ends the test; the focuser
may be left off focus, but every later target starts with its own seed move
and AF. A standalone download (park and hold after the sweep) is
`GET /api/optics-test/sequence`; `GET /api/optics-test/target` shows the
field it would pick.

Test subs (PS-152): tracking-test, optics-test and focus-calibration subs
(`target_names.is_test_target`) are recorded with `"test": true` but never
count toward or reset the consecutive-reject alert, never feed the night
medians (`hfr_rel` / `bg_rel` skip for them) or the QA baselines, and stay
out of the night score (`report.test_hours` shows their time). The PS-144
dusk focus calibration runs once per night: the armer records it at the
dispatch (`armer_state.json` `focus_cal`), marks it done when NINA shows its
container FINISHED (event `focus_calibration` / `done`), and no re-dispatch
that night carries it again. `focus_seeds.harvest_night` skips filters with
a `focus_filter_offsets` entry (their FOCPOS is the L AF plus the offset).

Config (System page, Quality group): `optics_test_offsets` ("-300,-150,150,300"
EAF steps; 0 is always shot first), `optics_test_filters` ("L"; "L,Ha" adds a
narrowband pass after the Ha focus offset), `optics_test_exposure_s` (45),
`optics_test_nb_exposure_s` (120), `optics_test_repeats` (2 per step).

Reading the report (per filter; angles from +x in the runs-page thumbnail
view): per offset the median eccentricity and HFR of the 100 brightest
stars, the stretch axis and its coherence R, the soft corner, and the axis
per 3x3 zone.
- **astigmatism**: the axis flips about 90 deg between the farthest inside and
  outside steps. If the center zone flips too: on-axis astigmatism, i.e.
  collimation (secondary tilted / decentered) or a pinched mirror. If only
  the off-axis zones flip: field astigmatism, normal for an RC without a
  flattener.
- **constant-axis**: the same axis (within 25 deg) at every offset: tracking,
  wind, flexure or a mechanical stretch, not astigmatism. A stretch that
  shrinks with defocus is a fixed-length smear (tracking / wind); one that
  grows is a fixed beam deformation (pinched or loose part).
- **defocus-only**: round at best focus (bright-star ecc under 0.45) and no
  direction that survives defocus: the optics are fine.
- **Tilt** line: a soft side that swaps across focus is sensor tilt (camera
  tilt plate / spacer); a soft corner at best focus that does not swap is not
  a plain tilt.

### TPoint mapping run (PS-171, supervised)

After any new model: the Guiding tab's TPoint record shows "record stale"
when TPoint's files hold a newer model than the manual record (2026-10-12:
the record said 10-04 / 250 points while the files held 10-10 / 356);
"Use file values" fills model date, points, sky RMS and IH / ID from the
files and keeps the rest (ProTrack, binning, catalogs, polar, notes).
Operator triggered; it writes only PhotonScript's manual.json.

Builds a denser TPoint model without handing the camera to TheSky (its
Automated Pointing Calibration Run needs TheSky's camera add-on, NINA #1
owns the AP26MC). NINA slews to N alt/az points, shoots one short frame at
each, and an ExternalScript asks TheSky to Image Link the frame. Why: the
mount drifts ~0.4"/min in RA and ~0.3"/min in Dec unguided (PS-168);
TPoint + ProTrack on a dense model is the fix. About 1 h for 60 points.

What the sequence does (RC16, spliced into the Targets area before the
night loop, runs once, then tonight's targets): StopGuiding, cool, sidereal
tracking, Slew to Alt/Az to the first point, AF on L, then per point a
run-once container `TPoint point <i>/<n> alt <a> az <z> (<side>)`: Slew to
Alt/Az (NO center, NO sync), one `tpoint_mapping_exposure_s` (5 s) L frame
at bin `tpoint_mapping_binning` (2), then `deploy\tpoint-sample.cmd` (always
exits 0, so a failed solve never stops the loop). Points: an equal-area
spiral above `tpoint_mapping_min_alt` (30 deg, at most 85), minus points
within `tpoint_mapping_moon_deg` (15) of the moon over the run, within 15
min of HA of the meridian (pier side ambiguous) and beyond
`tpoint_mapping_max_ha_h` (6 h, under the pole); all east-of-meridian
points first, then west (one flip), in 1 h HA strips walked up / down in
Dec. No MeridianFlipTrigger and no Center in it (a flip re-centers and
NINA's Center syncs, which would erase what TPoint measures); lint rule
`tpoint-mapping` errors on either, and on an unguarded point. The frames
are test subs (PS-152: no QA alerts, no night medians) and the off-target
alert ignores the run.

What `photonscript tpoint-sample` does per point (scope PC): waits up to
30 s for the newest FITS under `image_watch_dir` (not the one sampled
last), then ONE TheSky script: Image Link at `pixel_scale_arcsec` x
XBINNING, read the solution (J2000 center, stars, RMS), the mount as TheSky
reports it (RA / Dec, alt / az), LST, JD and TPoint "apply corrections".
With `tpoint_sample_add=auto` and a probe that found an add method, the
same script then calls that one method after a successful solve (the only
TheSky write PhotonScript makes; `tpoint_sample.ADD_CANDIDATES`, test
`test_add_calls_live_only_in_the_sample_module`). Default `off`: Image
Link + CSV only. Every point appends a row to
`<data_dir>/runs/<night>_tpoint.csv` and a `tpoint_sample` event.

The probe (afternoon, READ ONLY, safe any time TheSky runs):
`C:\astro\venv\Scripts\photonscript.exe tpoint-sample --probe` (add
`--json` for everything). It prints which candidate add methods exist on
this TheSky build (typeof checks, nothing called), the TPoint / Image Link
object members that mention TPoint / point / sample / model (so the real
API names can be read off), and the model's point count, RMS, IH / ID and
ProTrack flags from the PS-138 candidate names. Each probe is saved
(`<data_dir>/tpoint/probe_latest.json` + a dated copy) and the next one
prints "was ..." next to every changed flag: run it before and after a
manual TPoint session to compare the model (and the PS-168 drift report
the nights either side). Every probe item is guarded on its own (a
throwing item reads `ERR:<message>` and is listed under "probe item(s)
failed"; the rest still read), and the items go in five scripts (basic
typeofs, member lists, execute's length / text, model-ish values, TPoint
flags); a script TheSky rejects as a whole is re-sent item by item. The
2026-10-08 probe that printed None everywhere is what this fixes.

Site evidence (2026-10-08, TheSky64 10.5 build 14139, read only): there is
no TPoint scripting object (`TPoint`, `sky6TPoint`, `TPointAddOn2` globals
are undefined). `TheSkyXAction` is an object whose members are numeric
action ids (`AddPointingSample` = 197, `TPointAddOn2` = 147, ...) and
`TheSkyXAction.execute` is a function, so a pointing sample is added with
`TheSkyXAction.execute(<action>)`. Which argument it takes is not known
without calling it, so the add candidates are, in order:
`TheSkyXAction.execute(TheSkyXAction.AddPointingSample);` (the numeric id),
`TheSkyXAction.execute('AddPointingSample');`,
`TheSkyXAction.execute('ADD_POINTING_SAMPLE');`. The probe prints
execute's own text and length (a Qt slot may show its signature), the
TheSkyXAction members, all members of ImageLink, sky6RASCOMTele,
sky6RASCOMTheSky and OpticalTubeAssembly, and their plain values whose
names mention model / point / RMS / ProTrack / correction. Model points
and RMS read "unavailable" unless one of those objects exposes them (the
`tpoint_flags` read, and so the PS-104 audit, looks there too); compare a
manual session in TheSky's TPoint window and with the PS-168 drift
report instead. Nothing is called by the probe.
The first found candidate is used unless `tpoint_sample_add_method` pins
one. `photonscript tpoint-sample --dry-run` prints the exact add line
("add call: ...") and whether it would run. First add: ONE supervised point
with `PS_TPOINT_SAMPLE_ADD=auto` (the action may open a TheSky dialog or
use TheSky's own Image Link state rather than the scripted one; watch the
TPoint window and `add_result` / `add_error` in the CSV), then pin the form
that worked, or go back to `off`.

PS-187 finding (2026-10-09 run, read on 2026-10-10 over read-only GETs):
the sideloaded 45-point run went with `--add auto` and was stopped after 9
points (NINA #1 log, 20:57 to 21:07 local). No TPoint file saved after it
holds any of those points: `TPoint base run in.dat` was not saved between
the run and 12:55Z, the recal run saved at 03:50Z (51 points, LST 21:10 to
21:48) and the 10-10 base run (356 points, LST 21:56 to 03:25, TheSky's own
Automated Pointing Calibration) have no observation at the mapping's LST
(20:12 to 20:22). Working hypothesis: TheSky's Add Pointing Sample action
(`TheSkyXAction.execute(AddPointingSample)`) acts on TheSky's own last
camera Image Link, not on a scripted `ImageLink` of a FITS file, so the
scripted path cannot add points. Per point `add_result` / `add_error` is in
the night's CSV: `GET /api/tpoint/samples?date=2026-10-09` (PS-187, read
only: rows plus a summary of solved / added / add errors) once deployed.
So: the mapping recipe now passes `--add off` on every point unless the run
asks for `auto` explicitly (per-arm / preview `add=auto`, a supervised
test only; `tpoint_sample_add` no longer switches it on for the recipe);
the CSV is the deliverable (hand import, below). The supported way to add
points to TPoint is TheSky's Automated Pointing Calibration Run with the
camera handed to TheSky, as done on 2026-10-09 / 10. Automating that
(operator-started, supervised, never armed): docs/TPOINT_AUTOCAL.md and
`photonscript tpoint-autocal --dry-run` (plan only; `--go` not built yet).

How to run it (supervised; first run on a clear night, about 1 h):
1. Afternoon: run the probe (above). Note the point count / RMS. The
   recipe samples with `--add off` (PS-187): import the CSV by hand
   (below), or add points with TheSky's Automated Pointing Calibration.
   Only for a supervised add test pass `add=auto` with the run.
2. Dashboard, Target Goals, **Sideload**: recipe **TPoint mapping run**
   (`tpoint_mapping_then_tonight`), **Preview**. The RC16 row shows the
   point count (east / west), the minutes, the skips and whether the sample
   script was found ("MISSING" = frames only), lint PASS with a
   `tpoint-mapping` warning. `?at=<UTC>` previews a later start (the moon
   skip uses that time).
3. **Load into RC16** (and the Piggy-600 companion if wanted), confirm,
   then **Start in NINA #1**. The armer stays DISARMED and WATCHES (PS-136).
4. Watch: NINA #1's Advanced Sequencer (each point: slew, 5 s frame,
   script), TheSky's TPoint window (points arriving with `auto`), and
   `runs/<night>_tpoint.csv` (one row per point: solved yes / no).
   Several "not solved" rows in a row = clouds or focus: stop.
5. Stop early: **Stop** in NINA #1. Then on the dashboard **Stop
   watching** and **Arm** tonight (the armer dispatches the rest of the
   night from now). Or re-sideload
   `tracking_test_then_tonight` / plain tonight from the Sideload box.
6. Afterwards: in TheSky's TPoint window run the model fit (Super Model)
   yourself, check the RMS, turn on "Apply pointing corrections" /
   ProTrack as usual, then run the probe again (before / after).

TPoint numbers without a scripting object (`photonscript tpoint-stats`,
`GET /api/thesky/tpoint`): TPoint keeps its pointing data and model in its
own files under TheSky's user folder (`thesky_user_dir`; default
`Documents\Software Bisque\TheSky Professional Edition 64`, plus the
TheSkyX / "64 2" reinstall folders; the desktop copy has a `TPoint`
subfolder there, empty). `scheduler/tpoint_files.py` lists every file
whose path mentions TPoint / pointing / ProTrack or sits under Imaging
System Profiles (Camera AutoSave, where the Automated Pointing Run frames
go, is skipped), reads the text ones (never writes) and parses:
- the TPOINT input data format (caption, `:` options, site line with the
  run date, one line per observation, END): the point count. TheSky64
  writes `TPoint\<run> in.dat` / `Super Model Indat.dat` (all points) and
  `Super Model Outdat.dat` (the points kept in the fit, `& <extra columns>`
  after each);
- TheSky64's model file (`TPoint\<run> outmod.dat`, `Super Model
  Outmod.dat`): caption, `T <n obs> <sky RMS> <refraction A> <refraction
  B>`, one `[&][=]<TERM> <value> [<sigma>]` line per term (`=` = fixed),
  END; also a TPOINT fit report (`Sky RMS =`, `Popn SD =`). The model file
  has no popn SD: it is derived as sky RMS x sqrt(n / (n - fitted terms))
  and shown as "(derived)";
- a `ProTrack... = on / off` line in any listed file.
PS-184: the BASE model is the newest model file that is not a
recalibration (Super Model files when they are newer than the base run's)
and the data file of the same run gives its points; a RECAL run ("recal"
in the run name or caption: IH / ID and a few terms refitted on a short
run, the rest held `=`) is reported separately with its date, points,
fitted terms and RMS. The active model (the one ProTrack uses) is the
recal when it was saved after the base and has data, else the base; a
recal older than the base (the base was refitted after it) is shown as
"BEFORE it, not active". The top-level numbers (points, RMS, the audit
and history) are always the base's.
`tpoint-stats` prints points (and how many are in the fit), sky RMS, popn
SD, model date, the main terms and every term (fixed ones marked `=`), the
recal line, the active model, the polar alignment from ME / MA (each from
the active model when it fitted it, else the base's) in arcmin against
`tpoint_polar_max_arcmin`, and ProTrack (unknown unless a file states it).
Polar alignment is measured from the REFRACTED pole, as TheSky's Polar
Alignment Report does: recommended ME = -R, R = A tan z + B tan^3 z from
the model's T line refraction constants at the pole altitude (z = 90 -
latitude; 2026-10-10: A 47.546", B -0.0564" give 76.1", exactly TheSky's
"Recommended ME -76.1"); without them R comes from the data file's site
temperature / pressure (standard atmosphere at the site height when the
file's pressure is implausible); a "Recommended ME" line in any listed
file wins. Under 10" from the target says "no adjustment". Sign
convention (TPOINT, matching that report): ME > 0 = polar axis below the
pole, raise it; MA > 0 = polar axis east of the pole, move it west. The
azimuth advice is in Paramount MX knob form (Bisque manual: tightening the
right azimuth knob moves the axis east, the left one west; loosen the
opposite knob first). Confirm with TheSky's Polar Alignment Report before
turning a knob. `--files` lists what it found; `GET /api/thesky/tpoint/files`
does the same and `GET /api/thesky/tpoint/file?rel=` shows the head of one
listed file (anything else 404s) so the parsers can be checked against
TheSky64's real files (the `thesky64_*` test fixtures are those files,
read this way from the scope PC on 2026-10-08). The PS-104 audit reads the same numbers as source
`tpoint-files` (rows model points, RMS, polar error, model age) on the
scope PC, the probe prints them with "was ..." against the previous
probe, and every change is a line in `<data_dir>/tpoint/stats_history.jsonl`
that the PS-168 drift trend uses to key nights by model (date, points)
when the audit has no record for that night.

TPoint run watcher (2026-10-14, `scheduler/tpoint_watch.py`, a scheduler
background task; reads TPoint's files only, never TheSky or the mount).
During a calibration run TheSky autosaves `TPoint\<run> in.dat` and
`<run> outmod.dat` (6 basic terms) after every point. The watcher follows
the newest raw-input data file (read only when its size or mtime changes):
- running: its point count grew. Pace = mean gap between points from the
  LST column of each observation (gaps over 10 min left out), ETA to
  `tpoint_run_target_points` when set (0 = pace only);
- stopped: no new point for `tpoint_watch_quiet_min` (15) and no fitted
  model: waiting for the fit;
- finished: a model file written at or after the last point with more than
  the 6 basic terms fitted (the Super Model fit), or stopped for
  `tpoint_watch_fit_wait_min` (45) more. Once per run: one Pushover
  ("PhotonScript TPoint run finished", `tpoint_watch_mode` alert; observe
  = no push) with points, pace, sky RMS, popn SD, terms fitted, points and
  MA / ME / polar error against the previous model, a `tpoint_watch` run
  event and a "TPoint: ..." morning report line (a TPoint night has no
  subs log, so the line also shows on the next morning card whose night is
  earlier). A run seen only after it ended is never pushed.
- cleared (2026-10-16): a new run cleared the data (`<run> in.dat` header
  only: 0 points, written within `tpoint_watch_quiet_min` when seen, or
  shrank to the header). No sample `tpoint_watch_quiet_min` (15) min after
  the clear while the sun is under -12 deg: once per run a run event
  `tpoint_watch` value `no_samples`, a morning "TPoint: ..." line and one
  Pushover ("PhotonScript TPoint run", alert mode) "TPoint run started but
  no samples in 15 min (roof closed? camera? Image Link?)". The status line
  says "TPoint run started HH:MM local (data cleared, header only), no
  samples yet".
Model history: `<data_dir>/tpoint/models.jsonl`, one line per fitted model
(date, local night, points, points in the fit, sky RMS, popn SD, terms
fitted, IH ID MA ME CH NP of the base model, an active recal, the polar
error), appended when the model in TPoint's files changes while no run is
in progress; an empty history is seeded with the model before the newest
run and the current one. `GET /api/thesky/tpoint/history`, `photonscript
tpoint-stats --history` (remote CLI: read). The Guiding tab's TheSky /
TPoint section opens with the watcher's status line (`GET
/api/thesky/tpoint/watch`): "TPoint run in progress: 89 points of 228, 45
s/point, ETA 23:33 local" or "TPoint model 2026-10-15: 228 points, sky RMS
..., polar ...' (MA ..., ME ...)".
Unguided limits epoch: a new BASE model whose MA or ME moved more than
`tpoint_epoch_polar_shift_arcsec` (30") from the previous base model (the
polar axis was moved, e.g. the 2026-10-14 SharpCap alignment: MA -160" to
-76", ME -80" to +8") writes `<data_dir>/tpoint/unguided_epoch.json`; the
learned unguided limits then take evidence from that model's local night
on (the later of it and the TPoint model in force; `unguided_limit_since`
still overrides both), the table is refreshed at once and the morning line
says "Unguided limits: evidence restarts <night>".

TPoint Command Line from a script: not available on build 14139. There is
no TPoint scripting object, `TheSkyXAction`'s TPoint members
(`TPointAddOn2` = 147, `TPointModule`) are action ids that open TPoint's
windows, and no `sky6*` method taking a TPoint command string is known;
the probe lists all members of sky6RASCOMTele, sky6RASCOMTheSky and
OpticalTubeAssembly so one would show up, but nothing is executed to find
out. The file route above is the supported one; anything the Command Line
tab shows (FIT, polar report) can also be saved from TPoint as a text file
into its folder, where `tpoint-stats` picks it up.
Refit after a calibration run (2026-10-18, `photonscript tpoint-refit`,
docs/TPOINT_REFIT.md): backs up TPoint's files, Jeremy runs Super Model at
the console (or a CANDIDATE TheSkyXAction member found with the read-only
`--discover`, supervised `--test` only), reads the new samples / sky RMS /
terms back from the files and restores the backup when the fit is worse
than the limits. Apply pointing corrections / ProTrack stay manual (no
write API on build 14139). Not in the remote CLI allowlist.

Hand import (no add method, or `off`): the CSV has, per point, the solved
J2000 center (`solved_ra_j2000_h`, `solved_dec_j2000_d`), the mount's RA /
Dec as TheSky reported it (`mount_ra_h`, `mount_dec_d`, JNow), its alt /
az, LST and JD. TPoint (TheSky TPoint window, File > Import, verify the
menu on the site build) takes "true" (catalog) and "observed" (mount)
positions per point. Caveat: TheSky reports the mount position AFTER any
TPoint corrections; when `apply_corrections` reads 1 in the CSV the rows
measure the current model's residuals (good for checking it), not raw
pointing errors, so for a fresh model turn "Apply pointing corrections"
off for the run or rely on the add method. Plan C: TheSky's own Automated
Pointing Calibration Run (disconnect the camera in NINA #1 first, connect
it in TheSky; give it back to NINA before the night).

Config: `tpoint_mapping_points` (60, 3 to 300), `tpoint_mapping_min_alt`
(30), `tpoint_mapping_exposure_s` (5), `tpoint_mapping_binning` (2),
`tpoint_mapping_moon_deg` (15), `tpoint_mapping_max_ha_h` (6),
`tpoint_sample_script` (`C:\astro\PhotonScript\deploy\tpoint-sample.cmd`;
missing = frames only), `tpoint_sample_add` (off | auto),
`tpoint_sample_add_method` ("" = the probe's first; or one candidate id),
`thesky_user_dir` (TheSky's user folder for the TPoint files; "" = the
default Documents path), `tpoint_watch_mode` (alert | observe | off),
`tpoint_run_target_points` (0), `tpoint_watch_poll_s` (60; 5x idle),
`tpoint_watch_quiet_min` (15), `tpoint_watch_fit_wait_min` (45),
`tpoint_epoch_polar_shift_arcsec` (30; 0 = never). Preflight:
`tpoint_preflight` (block | warn | off), `tpoint_preflight_rms_max_arcsec`
(40), `tpoint_polar_changed_on` (""), `tpoint_saved_models_dir`
(`C:\astro`). Coverage (next
section): `tpoint_coverage_thin` (4), `tpoint_coverage_target` (6),
`tpoint_coverage_min_alt` (30), `tpoint_coverage_pace_s` (35),
`tpoint_coverage_arm_warn` (alert | observe | off),
`unguided_limit_uncovered_s` (0 = the shortest ladder step).

### TPoint coverage check: where the model has samples (2026-10-15)

2026-10-14, M33 unguided: subs round from 22:00 to 01:00 MDT (RC16 ecc
0.49 to 0.55 at 120 s, Piggy-600 0.45 to 0.58 at 450 s), then from the
meridian flip (about 01:00) both rigs trailed (RC16 0.68 to 0.75, Piggy
0.77 to 0.89). The TPoint base run in force (99 samples, 22-term super
model, sky RMS 12.4") had every sample EAST of the meridian: after the
flip ProTrack ran on a model with no data on that pier side. A low RMS
says nothing about where the samples are; `scheduler/tpoint_coverage.py`
does.

Input: the base model's data file (`TPoint\<run> in.dat`, the one
`tpoint-stats` pairs with the model; read only). TheSky 10.5 row format:
star RA (h m s), star Dec (d m s), scope RA (h m s), scope Dec (d m s),
LST (h m.mmm); the site line gives the latitude. Per sample: HA = LST -
star RA, Dec, altitude / azimuth at the site, pier side.

Pier side convention (TPOINT through-the-pole notation): the mount
reports one pier side with the scope Dec beyond the pole (|Dec| > 90, RA
12 h away). On 2026-10-14, 94 of 99 rows are beyond the pole and all of
them are east of the meridian (HA -8.6 to -0.2 h), so beyond the pole =
the east-looking side (before the flip). The 5 rows under 90 sit within
0.14 h of the meridian (alt 66 to 89 deg): the mount was on the other
side there, as the Paramount may be near the meridian; they are counted
as "meridian zone" samples, not as west coverage. The convention is
re-derived from every file (samples 1 h or more from the meridian vote);
with none, the 2026-10-14 mapping is assumed.

Grid: HA band east far (< -3 h) / east (-3..0) on the east-looking side,
west (0..3) / west far (> 3 h) on the west-looking side (after the flip),
x Dec band < 0 / 0-30 / 30-60 / 60+. A cell is usable when at least 20 %
of it (HA up to `tpoint_mapping_max_ha_h`, Dec down to the horizon) is
above `tpoint_coverage_min_alt` (30 deg). Per usable cell: empty (0),
thin (under `tpoint_coverage_thin`, 4) or ok. Overall: east-only /
west-only (one side has nothing), full (every usable cell ok), partial.
Top-up: every usable cell to `tpoint_coverage_target` (6) samples, timed
at the run's measured pace (the TPoint watcher's, else the file's own LST
gaps, else `tpoint_coverage_pace_s`, 35 s), with a target HA / Dec / alt /
az per cell.

The current model (base run of 2026-10-14, read 2026-10-15):

    Dec       east far    east     west     west far
    60+          9         5        0         0
    30-60       19        12        0         0
    0-30        13        17        0         0
    <0           5        14        0         -  (under 30 deg)

EAST-ONLY: 99 samples (east far 46, east 53, west 0), 7 usable west cells
empty, "east Dec 60+" one short of 6. Top-up: 43 points (west 42, east 1),
about 34 min at the run's 48 s per point.

Where it shows:
- `photonscript tpoint-stats` (and `--json`, key `coverage`) and `GET
  /api/thesky/tpoint` (`coverage`, `coverage_text`): the grid, the
  convention, the top-up per cell. Saved as
  `<data_dir>/tpoint/coverage_latest.json`.
- TPoint watcher: each model history entry carries `coverage` (overall,
  points per side, empty / thin cells, top-up); the finish push and the
  morning "TPoint: ..." line add the coverage line; during a run the status
  line says "so far east N / west M of the meridian".
- Guiding page, TheSky / TPoint section: under the watcher line, the
  one-line summary and a small Dec x HA grid (green ok, amber thin, red
  empty, "-" under the altitude floor); during a run the grid is the run so
  far.
- Morning report: "TPoint coverage: east-only (...). Top-up: 43 points in 8
  cells, about 34 min" while any usable cell is empty or thin.
- Arm / every dispatch (`tpoint_coverage_arm_warn` alert | observe | off):
  tonight's planned target windows are sampled every 10 min; the side is
  taken from the HA (west of the meridian = after the flip). A target with
  15 min or more in empty / thin cells gives one warning a night (run event
  `tpoint_coverage`, plan snapshot `tpoint_coverage`, and in alert one
  Pushover "PhotonScript TPoint coverage"), e.g. "M33 spends 4 h 15 min of
  its 7 h 30 min window in TPoint cells with no samples (after the flip,
  west of the meridian): west Dec 30-60 0 pts, ...". A warning only: the
  arm goes ahead.
- Learned unguided limits: a sample in an empty TPoint cell is unknown sky.
  Only a limit measured in that same region counts; no Dec band or
  whole-sky limit is carried in; else `unguided_limit_uncovered_s` (0 = the
  shortest ladder step, negative = ignore coverage), and the plan's why
  says "unknown sky: TPoint has no samples in west Dec 30-60". The limits
  regions (20 deg Dec bands x five HA bands with a meridian band) map onto
  the TPoint cells per sample position; the table (`photonscript
  unguided-limits`, `GET /api/tracking/unguided-limits` row `tpoint`)
  marks a region "[TPoint empty]" (no sample in any overlapping cell) or
  "[TPoint partial]" (the meridian band straddles both sides).

### TPoint / ProTrack preflight at arm (2026-10-16)

2026-10-16: a TheSky "Full" calibration run was started and cleared the
pointing data: `TPoint base run in.dat` 137 bytes (header only),
`outmod.dat` 98 bytes ("T 0", IH / ID +0.0000), the active TPoint settings
an empty "TPoint Settings-2026oct-number6"; the good model (99 east-only
samples, super model, sky RMS about 12") only in
`C:\astro\TPoint Settings-2026oct-12RMS.tptx`. An unguided night on that
tracks with ProTrack on no model and trails. `tpoint_files.stats` still
reported 91 points then (the data file falls back to another run's), so
the stats now also carry the ACTIVE model's own run: `active_run_points`
(0 = header only), `active_t_obs`, `active_nonzero_terms`.

`scheduler/tpoint_preflight.py` (TPoint's files only, read only) runs at
every arm and every NINA #1 dispatch:
- block: the active model has no samples or no fitted model (its run data
  header only, or "T 0" / no non-zero term, or no model file);
- warn: coverage east-only / west-only, sky RMS over
  `tpoint_preflight_rms_max_arcsec` (40"), model older than
  `tpoint_polar_changed_on` or the manual TPoint record's
  `equipment_changed_on`, ProTrack off (the `ProTrackOn=` flag in
  ImagingSystem.ini), files not readable;
- ok otherwise.
`tpoint_preflight` = block (default) | warn | off. Only the no-model case
on an UNGUIDED night is enforced: it never is a judgement call (every
unguided sub trails), the warn cases are (an east-only model still tracks
before the flip). Enforced: the arm goes ahead and pushes the fix at once
(priority 1, "PhotonScript TPoint preflight"); every dispatch re-reads the
files and, while the model is still empty, refuses to load NINA #1 ("TPoint
preflight blocked the night", the usual "Dispatch FAILED" page; re-arm when
fixed). Not enforced (push and line only): a guided arm (info, no push),
the TPoint mapping recipe, `tpoint_preflight=warn`, and an arm with
`{"tpoint_override": true}`: the Arm card's **Arm anyway (TPoint loaded)**
button, for a model loaded by hand that TheSky has not written back to the
files. The fix names the newest .tptx saved BEFORE the clear in
`tpoint_saved_models_dir` (`C:\astro`, listing only) or the TPoint
folders, else "your latest saved model": "TPoint has no model: open
C:\astro\TPoint Settings-2026oct-12RMS.tptx (TPoint Module Settings > Open)
and turn ProTrack on, or run a calibration".
Where it shows: the Arm card line "TPoint preflight: ...", `GET /api/arm`
`tpoint_preflight` (level, action, enforced, line, fix, checks, ProTrack,
points, terms, RMS, model date, coverage, saved models), run events kind
`tpoint_preflight` (at arm, on a changed level, on every enforced block),
`<data_dir>/tpoint/preflight_latest.json` and the morning report
"TPoint preflight: ..." line (also on a night with no subs log).

Fix: a top-up calibration run that covers the west side (TheSky's
Automated Pointing Calibration, or the mapping recipe's west strips), then
refit; the watcher reports the new coverage when the run finishes.

### A safe hand-built NINA #2 sequence (PS-139)

The Piggy-600 rides the RC16 mount and NINA #1 owns that mount (PS-25).
NINA's stock deep-sky template is NOT safe for NINA #2: on 2026-10-03 its
Telescope Center sat inside the per-sub loop, so every sub ran a slew, a
plate solve, a sync TheSky refused, a ~58' offset slew and a second solve
(34.6 s median gap, ~150 s with an HFR autofocus). On a dual-rig night each
of those would pull the RC16 off its target. Prefer PhotonScript's
companion (the sideload Piggy recipe); when building one by hand:

- No Slew to Ra/Dec or Alt/Az, Slew and center, Center and rotate, Solve and
  sync, Set tracking, Park / Unpark, Find home or Connect (mount) anywhere in
  a loop, and no Meridian Flip or Center After Drift trigger at all
  (triggers fire between exposures). Delete them from the template's target
  container before adding exposures.
- Center once, before the loop, at most, and only when the RC16 is not
  imaging (it moves the RC16 too). While the RC16 images, NINA #2 only
  cools, focuses and exposes: the RC16's own centering aims both scopes.
- Keep the companion's guards: CoolCamera at the Piggy-600 setpoint and
  bounded by a TimeSpanCondition (PS-154), at or below 0 C, a
  SafetyMonitorCondition and a dawn TimeCondition on the light loop itself,
  the settle gate before each light (PS-27).
- Load it with the sideload (rig piggyback, JSON body) so the lint runs:
  rule `piggy-mount` refuses a mount instruction inside a loop or a trigger
  (422) and warns on one outside any loop. `rigs.nina_dispatch` (companion,
  Piggy dusk flats, calibration capture) refuses a looped one on NINA #2
  before any stop or load.
- A sequence loaded by hand in NINA #2 is checked read-only at arm and when
  the armer starts WATCHING (PS-136): `GET /sequence/state` on NINA #2, one
  Pushover a night ("PhotonScript NINA #2 mount"), a red / amber chip in
  Tonight's Run and a Guiding "What to change" item. At arm the push says
  when the companion replaces it at pre-config (`piggyback_calibrate_on_arm`).
  `PS_PIGGYBACK_MOUNT_CHECK` = alert (default) | off. The live tree has no
  `$type`, so it matches NINA's display names (best effort).

### Watch a sideloaded night (PS-136)

A sideloaded night is NINA's, but the armer can WATCH it: state `WATCHING`
(purple chip "Watching sideloaded night" in Tonight's Run). It enters by
itself when tonight's RC16 sideload (`kind: "sideload"`, rig rc16, ok) is
loaded and NINA #1's `/sequence/state` shows anything RUNNING (detector
every 60 s while the armer is DISARMED / COMPLETE / ERROR;
`PS_WATCH_SIDELOAD_AUTO`, default true), or by the dashboard's **Watch this
night** button (`POST /api/arm/watch`). The button turns into **Stop
watching** (`POST /api/arm/watch/stop`): NINA keeps running, and the
detector never adopts that sideload again. `GET /api/arm/watch` shows the
watch, tonight's RC16 sideload and what NINA #1 runs.

- Never loads, starts, stops or re-dispatches a sequence. Arm 409s while
  watching (it would replace the sideload at pre-config), `dispatch_raw`
  refuses, `POST /api/update` and the boot-time self-update refuse (PS-58),
  calibration capture / QA backfill / ecc-scale wait. Disarm = stop
  watching, no make-safe.
- Unsafe: one priority push "PAUSED (watching)", one "RESUMED" when safe
  again (kind `watch` events `pause` / `resume`). PS-77's check is alert
  only: unsafe for `unsafe_stop_grace_s` with SAFE_LOOP still running = one
  priority push, the sequence is not stopped (the armer could not resume it).
- Guiding watchdog (same ladder and alert gate as an armed night, including
  its one PHD2 restart) only while a target that has a StartGuiding in the
  saved sideload file is RUNNING: never during the unguided tracking test,
  an unguided target or an unsafe wait. No file: `guided_default`, never
  during a "Tracking test" container.
- Ends when NINA #1 has run nothing for 3 ticks (sequence over) or at the
  dawn shutdown time (PS-36 timing): summary push, lifecycle chip, then
  after the warm window a READ-ONLY check (every cooler off, mount AtPark)
  with a priority push on a problem. At dawn with NINA #1 still running:
  priority push, no command. `PS_WATCH_DAWN_ACTION=shutdown` runs the armed
  night's `dawn_shutdown` (stop, guider stop, warm + dew off, park) instead.
- Restart mid-watch: reattaches like an armed night (`armer_state.json`).
- Not covered while watching: the cooler nanny, PS-93 recalibration and the
  unguided fallback (both re-dispatch).
- `dispatch_raw` (RC16 dusk flats) now reads NINA's load validation between
  load and start (PS-132 `nina_validation`, `PS_NINA_LOAD_VALIDATION`
  alert / refuse / off, default alert).

### Pause / Resume and the "Where is it" panel (PS-64)

The top of the dashboard is a live **Where is it** panel (`GET
/api/night/where`, `scheduler/where_panel.py`, `static/js/night_panel.js`,
refreshed by the top strip's 10 s poll): target (and mosaic panel), filter,
sub n of N with seconds left and a progress bar (NINA #1's sequence tree
LoopCondition + the camera's ExposureEndTime), what NINA runs now and the
next items, guiding mode (PHD2 state and RMS, or unguided TPoint +
ProTrack), cooler per rig (with the PS-61 gate note), roof / safety, the
Piggy-600 (running item, sub, exposure, PS-27 settle gate / abort-on-move /
mount still or slewing) and the astro-dark / dawn / shutdown countdown.
Read only; a NINA that cannot be read shows "-".

- **Pause** (button on that panel, `POST /api/arm/pause {"piggy":
  "keep"|"pause", "when": "after_exposure"|"now"}`), only while RUNNING:
  state `PAUSED_OPERATOR` at once, and NINA #1's sequence is stopped
  (`/sequence/stop`) once the current sub has finished and downloaded
  (`scheduler/night_pause.py` polls the camera every 2 s; at once when
  nothing exposes, the camera is unreadable or a new sub started; never
  longer than the exposure end + 60 s, cap 16 min). Tracking, cooler and
  PHD2 keep running, nothing parks or warms; dithers stop with the
  sequence. The Piggy-600 keeps imaging unless "pause it too" was chosen
  (then NINA #2 is stopped the same way). One Pushover when the stop is
  done (priority on a failed stop: back to RUNNING), events kind
  `operator_pause` (request / stopped / failed / resume / resume_failed /
  park_unsafe) in `runs/<night>_events.jsonl`.
- While paused: no not-guiding alerts, the cooler nanny keeps the setpoint,
  safety is recorded; unsafe for `unsafe_stop_grace_s` parks the mount
  (PS-77's rule, one push); dawn runs the normal dawn shutdown. Deploys,
  self-update, sideload, calibration capture / QA backfill and dispatch_raw
  refuse as for RUNNING (PAUSED_OPERATOR is in ACTIVE_STATES); a restart
  reattaches (and finishes a pending stop).
- **Resume** (`POST /api/arm/resume`): before the stop happened it only
  cancels the wait. After it: the armer's mid-night re-dispatch of the
  remainder (`_dispatch_and_start(companion=False)`, as after a PS-77
  safety stop: the planner subtracts accepted subs; slew, center, AF and
  StartGuiding run again), plus the Piggy-600 companion if this pause
  stopped it. Refused with under 40 min of dark left. A failed re-dispatch
  stays paused.
- A watched sideloaded night (PS-136) pauses alert-only: the guiding
  watchdog is muted and nothing is sent to NINA (pause the sideload in NINA
  itself); unsafe and stuck-imaging alerts stay on.

### Sub doctor: why the subs are bad and the fix

2026-10-12: armed unguided, PHD2 kept guiding (LostLock / Guiding) on a
calibration graded FAIL (axes 78 deg from perpendicular) and every RC16 sub
trailed; the scorer rejected them and nothing said why. Now after each
graded light that failed (and until the rig is healthy again) the telescope
agent judges the rig's last `sub_doctor_window` (3) subs, at least
`sub_doctor_min_bad` (2) bad, each paired with the other rig's subs exposed at
the same time (`scheduler/sub_pairs.py`: both soft and round = seeing, one
soft = that rig's focus, both trailing = the mount, one trailing = that
rig's guider or flexure, both star counts down = clouds), and names one
cause with its evidence and fix (`scheduler/sub_doctor.py`): focuser
(FOCPOS 0, AF stall), guider_conflict (PHD2 guiding on an unguided night,
not guiding on a guided one, guiding on a FAIL calibration, RMS tiny or a
PS-91 non-star lock while subs trail), pointing, tracking (trail direction
from the star sidecar turned to the sky with the last plate solve: RA / Dec
drift / field rotation, the trail rate, the PS-169 drift-capped length),
clouds, seeing, focus (AF age, temperature since the last AF), other.
`sub_doctor_mode` alert (default) pushes once per cause per rig per night
(observe: chip and events only, off). Each diagnosis is a run event kind
`sub_doctor` (cause, confidence, evidence, suggested_action {id: stop_phd2 |
recalibrate_phd2 | reselect_guide_star | start_guiding | run_af |
check_focuser | shorten_subs:<s> | check_tracking | check_pointing |
wait_clouds | none}); a recovery writes cause "none". One actor for
remedies (2026-10-13): the doctor only diagnoses and the night pilot is the
only thing that acts on a suggested_action. The doctor's own opt-in
remedies and its `<data_dir>/sub_doctor/caps.json` sub cap (the armer's
dispatch hook) are retired; `sub_doctor_stop_phd2_unguided`,
`sub_doctor_request_af` and `sub_doctor_shorten_subs` stay readable but
have no effect (superseded by `night_pilot_stop_phd2`, `night_pilot_af`,
`night_pilot_shorten_subs`). The push says "Remedy <id>: the night pilot's
(<mode>)". Dashboard chip (`GET /api/sub-doctor`), Runs page "What went
wrong and the fix" (`GET /api/runs/{date}/sub-doctor`), the morning report
line, pairs for other agents (`GET /api/sub-doctor/pairs`). Past nights:
`photonscript sub-doctor --night D [--rig R] [--url U] [--nina-events]
[--timeline] [--json]` (read only).

### Learned unguided limits (sub length per sky region)

2026-10-13, unguided (TPoint + ProTrack): on M31 (Dec +41, east, alt about
46 deg) 300 s RC16 subs trailed (ecc 0.79 / 0.71) while 180 s stayed round
(ecc 0.43 to 0.55); near the meridian at Dec +12, 180 s was round. One cap
(`unguided_max_exposure_s`) for the whole sky is wrong somewhere, so
`scheduler/unguided_limits.py` learns the longest unguided sub that stays
round per rig and region from the graded subs:

- Evidence: lights of unguided nights (plan snapshot: target or PS-85 block
  unguided; tracking-test subs count; no plan entry: PHD2 stopped) since the
  TPoint model in force, or the TPoint watcher's epoch when a new base
  model moved the polar axis and that is later (section "TPoint run
  watcher"; `unguided_limit_since` overrides both), over
  `unguided_limit_lookback_days` (60). Left out and counted: PHD2 active
  during the sub or within 10 min of an active sub on either rig, a sub
  doctor `guider_conflict` diagnosis covering it (the 2026-10-12 Heart night
  drops out whole), FOCPOS 0, star count under half the night median,
  HFR over its gate while round (soft stars hide drift; FWHM over its gate
  is seeing and is kept), test subs (TPoint, optics, focus calibration).
- Region: Dec band (20 deg) x hour-angle band (east far, east, meridian,
  west, west far); pooled per Dec band and for the whole sky as fallbacks,
  except in sky the TPoint model has no samples in (section "TPoint
  coverage check"): there only the region's own measured limit counts,
  else the shortest step.
- Per region and sub length: subs and passes under the rig's ecc gate.
  Limit = the longest ladder length (`unguided_limit_steps`
  300,240,180,120, plus the rig's own length) with pass rate at least
  `unguided_limit_pass_rate` (0.8) over at least `unguided_limit_min_subs`
  (2) subs, or, untested, the drift model (elongation per second from every
  sub, e(L) = kL / sqrt(1 + (kL)^2)) predicting ecc at least
  `unguided_limit_model_margin` (0.05) under the gate. A shorter length that
  fails on measurement caps it. Confidence high (8+ subs at the limit),
  medium (4+), low; an uncertain region names the next longer step to probe.
- Planner (armer dispatch, after the PS-66 cap and the PS-156 fallback):
  each unguided RC16 set longer than the limit of the regions its target
  crosses tonight (window sampled every 30 min, shortest wins) is cut to the
  longest ladder length at or under it with matching darks, else darks
  scalable from a bias (night pilot `dark_match`), never longer than the
  goal; a goal's hand-set exposure override for that filter wins. The
  Piggy-600 companion's sub length gets its own table the same way.
  `unguided_auto_length` off | advise (default: logged, in the plan snapshot
  `unguided_limits` and the morning report, nothing changes) | apply.
- Probe (`unguided_limit_probe` off (default) | advise | apply): an
  uncertain region gets `unguided_limit_probe_subs` (2) subs at the next
  longer step ahead of the target's longest-owed set (its count reduced);
  graded like any sub, they raise the limit at the next refresh only if
  they pass.
- Calibration: each region's limit, its probe step and (for a drift-model
  limit) the step under it join the night dark quota
  (`calibration.night_dark_exposures`) and the Calibration owed view
  ("unguided limit learned"), at most `unguided_limit_darks_max` (3), so a
  roof-closed night shoots 180 s and 240 s darks before the lights need
  them.
- Refresh: the dawn filing step, the Guiding page Refresh, `GET
  /api/tracking/unguided-limits?rig=&refresh=true`, `photonscript
  unguided-limits [--rig R] [--refresh] [--url U] [--since D] [--json]`
  (`--url` computes from a running PhotonScript's runs, read-only). Store:
  `<data_dir>/unguided_limits.json`. Morning report: "Unguided limits:
  tonight M31 (Dec +41 east) 180 s 5/5, 300 s 0/2 round. Plan: ...".

### Target core saturation: is the M31 nucleus clipped? (2026-10-13)

sat_stars_pct and the frame-wide clip counts never see a galaxy nucleus or a
bright nebula / cluster core (a negligible fraction of 26 MP). Both graders
now measure the target's own core on each light (`shared/core_sat.py`, a
small memmapped cutout): the core is placed by a WCS in the header, else
this sub's stored plate solve (`solve_store`), else the header RA / DEC as
the frame center with a search disk as wide as the offset from the target,
else the frame center; inside the search the brightest block median marks
it. The disk is `qa_core_radius_arcmin` (1') for galaxies, 10% of the
catalog size (1x to 3x that) for other targets. Recorded per sub:
`core_sat_px` (pixels >= the saturation level: FITS SATURATE, else
`qa_saturation_adu`), `core_peak_adu`, `core_p999`, `core_src`, `core_xy`.
Scorecard row "core" (weight 0, never a reject): `qa_core_mode` note
(default: passes with "target core clipped: N px" above
`qa_core_sat_warn_px` 25), warn (holds the sub for a look), off.
HDR coverage (`scheduler/core_check.py`): per target and filter, when at
least half of the longest subs clip the core, the goal's HDR set
(`hdr_short_seconds` / `hdr_short_count`, else `core_hdr_min_short` 12) is
compared with the approved shorts in the Library; a gap goes into the
morning report ("Core clipped: ... add N x 30 s (enable the goal's HDR set,
hdr {"L": 30})"), the sub doctor's report and the target page. The
suggested short length scales the longest unclipped measured length so the
core p99.9 lands at 80% of saturation (nothing unclipped yet:
`core_hdr_short_s`, 30 s). Integration checks the master the finish uses
(master_<F>_hdr, else the longest master) for a flat-topped core:
"core clipped in master_L_hdr: 0 px" in the run log, `out\pipeline.log`
and ledger `machine.core_check`. CLI (read only):
`photonscript core-check --target M31 [--rig rc16] [--night D] [--url U]
[--measure <Library folder>] [--json]`; `--measure` reads FITS directly
(for nights graded before the core check).

### Heart narrowband blend: Ha first and blend readiness (2026-10-14)

The RC16 frames only the Heart core (24 x 16' of a 60' nebula), so its
data reaches the full image through the narrowband two-rig blend
(OSC_INTEGRATION.md 0d), whose main input is an RC16 Ha master. The planner
shares a night by the goal's remaining counts (the mix), so a fresh goal
gets Ha 30% of each pass (2026-10-13: Ha 32 / OIII 37 / SII 38 x 180 s).
`plan_nb_blend_ha_first` (default off) gives a two-rig narrowband goal (an
RC16 Ha plan and a Piggy-600 plan) the night to RC16 Ha first until it would
hold `blend_nb_min_ha_h` (2 h) accepted, then OIII / SII share the rest by
the mix; the PS-180 window rescale keeps that Ha reserve. Per goal,
`ha_first_h` on PATCH /api/projects2 overrides it (null = the config, 0 =
off, > 0 = on at that many hours). Readiness (`scheduler/blend_readiness.py`):
per two-rig NB goal the accepted RC16 Ha / OIII / SII subs and hours, the
newest Piggy-600 master (posted ledger), ready when there is a Piggy master
and Ha >= `blend_nb_min_ha_h`, OIII added at `blend_nb_min_oiii_h` (1.5 h),
the SHO core at `blend_nb_min_sii_h` (1.5 h, with OIII), and nights to each
threshold from tonight's planned hours per filter (planned, not accepted:
a night that rejects most RC16 subs takes longer). It is one morning card
line ("Heart Nebula NB blend: not ready: Ha 0.0/2 h ..."), a block under
Integration readiness on the target page, GET /api/blend-readiness
[?target=] and `photonscript blend-readiness [--target T] [--json] [--url
U]`. autointegrate skips a narrowband blend while the chosen RC16 run's
ledger shows less Ha than `blend_nb_min_ha_h` ("no blend: narrowband blend
waits for RC16 Ha 0.3 h < 2 h"); unknown depth and L blends are not gated,
`blend_nb_gate=false` turns it off, and a manual `photonscript blend` is
never gated.

### Piggy-600 sub length per target (2026-10-14)

Ticket "Piggy-600: shoot M31 at 120 s (nucleus clips at 300 s)". The core
check of the Library: Piggy M31 300 s subs clip the nucleus on 122 of 162
subs, 120 s on 1 of 98 (p99.9 86% of saturation). Before this, NINA #2's
companion shot ONE length all night (`piggyback_exposure_s`, the learned
unguided limit in apply), so a goal's OSC plan length never reached it.

- **Set it on the goal**: the `"OSC"` key of `exposure_overrides` (`PATCH
  /api/projects2/<id>`; the body replaces the whole dict, so send the RC16
  keys too). `ProjectStore.update` resizes the goal's OSC plan to that
  length (same goal hours, or `osc_hours` when given; accepted seconds
  carried, PS-118) and, when only the OSC key changed, leaves the RC16 plans
  alone. Removing the key leaves the plan length as it is.
- **Tonight** (`scheduler/piggy_length.py`, armer `_piggy_lengths` after
  the unguided limits): per RC16 target in plan order, the goal's OSC
  override, else (`piggy_core_length` apply) the core-check length, else
  `piggyback_exposure_s`, never above the applied unguided Piggy limit.
  One length all night = the companion's `piggyback_exposure_s`; several =
  one `OSC_LIGHT_LOOP` per run of equal lengths, each with the usual
  Safety + astro-dawn guards, AF triggers and settle gates, ending at the
  PS-180 handoff clock time of its last target (the last at astro dawn).
  The record is `plan["piggy_length"]` (plan snapshot `piggy_length`) and a
  log line "Piggy lengths: Andromeda Galaxy 120 s until 07:30Z, then Heart
  Nebula 450 s". No override and the default mode: nothing changes. The
  sideload preview's companion (no goals there) keeps `piggyback_exposure_s`.
- **Advice** (`core_check.osc_advice`, config `piggy_core_length` off |
  advise (default) | apply): a Piggy target whose longest length clips the
  core gets status `osc_length` while the length shot on it is longer than
  the longest measured unclipped length (at least 3 subs; else
  `suggest_short`): morning report, sub doctor and target page read "Piggy
  Andromeda Galaxy core clips at 300 s (core clipped on 122 of 162 subs
  ...): set OSC to 120 s (exposure_overrides {"OSC": 120}; now 450 s from
  piggyback_exposure_s) (120 s: 1 of 98 subs clip, core p99.9 86% of
  saturation)". apply: the planner shoots that length on that target's
  Piggy subs unless the goal has its own OSC override.
- **Integration**: an OSC stack of several lengths gets the core combine
  (OSC_INTEGRATION.md, "OSC core combine"): the short subs (<= 180 s)
  stack apart and replace the clipped nucleus of `master_OSC` into
  `master_OSC_hdr`.

### Deep night: one goal all night (2026-10-14)

Jeremy: "it's kinda nice to be able to image a target a night and create
those deep, vivid images (lots of texture)". The default planner ("split",
PS-180 windows) shares each night among the goals by priority, so each gets
a slice (10-14: M31, NGC 604, Heart). "Deep" gives the dark window to ONE
goal on both rigs and falls back to split for what is left.

- **Mode**: config `plan_night_mode` = `split` (default) | `deep` (System
  page, Imaging). Tonight only: the dashboard's Tonight's Run panel, "Night
  mode" select (default / split / deep, and with deep a goal or "best
  score"), or `POST /api/tonight/mode {"mode": "deep", "target": <project
  id, optional>}` (`"default"` removes it; `GET /api/tonight/mode` shows
  it). Stored with the night like the night pilot's ledger
  (`<data_dir>/night_mode/<night>.json`, keyed by the local evening date,
  so it never applies to another night) and marked cleared by the dawn
  shutdown (a shutdown step "night mode override (deep) cleared"). Setting
  it sends nothing to NINA: it is planned at the next dispatch (arm, Resume,
  or Restart tonight from now).
- **The pick** (`scheduler/deep_night.py`): the explicit target wins
  (any active goal up tonight, mosaic panels included); else the best
  score over tonight's visible goals, 0 to 100: hours above the altitude
  floor from now to dawn / dark hours (35), fill = owed hours on the busier
  rig (RC16 and Piggy run in parallel) / hours up, capped at 1 (25),
  priority / 100 (20), moon = share of the owed seconds tonight's moon
  allows (10: narrowband always; broadband and OSC by the one moon rule,
  `moon.broadband_deferred`, so a moon that sets within 1.5 h of dusk
  allows them, and only until moonrise when the moon rises later; 0 = not
  eligible), transit = 1 at the dark-window midpoint down to 0 at 6 h from
  it (10). Mosaic panels are not auto-picked.
- **The night**: the pick gets its time up (from its rise above the floor,
  or now, to its set or dawn), or until both rigs are done (need = the
  larger of RC16 owed and Piggy OSC owed, + 15%). The RC16 shoots its
  filters by the mix (the usual moon fit, one pass repeating to the window
  end); the Piggy-600 companion shoots OSC on the same pointing all night.
  Time before the pick rises and after it sets or is done is split among
  the other goals (PS-180 rule); "Andromeda Galaxy done 03:46Z, split
  after". A Piggy-driven goal with no RC16 plan can be picked: the mount
  centers for the Piggy frame and the RC16 shoots bonus subs of the
  target type's default plan (not counted toward the goal). If the moon
  rule drops the pick or it gets less than `plan_window_min_minutes`, the
  night is planned split and `deep_note` says why.
- **Shown**: `GET /api/tonight` has `mode`, `mode_source` (config |
  tonight), `deep_pick` (score, components with value / norm / points,
  window, fallback, split_targets), `deep_scores` (every goal, best first,
  with `why` when not eligible) and `deep_note`; `/api/nightplan` has
  `night_mode` and `deep_pick` and the timeline shows "Deep night: NGC 604
  (score 88.4: hours up 33.5, fill 24.2, priority 11.0, moon 10.0, transit
  9.7)". The armer records the dispatched mode in `plan["night_mode"]`
  and the morning report leads with "Deep night on NGC 604: 6.2 h RC16,
  8.4 h Piggy" (accepted lights on the pick).
- **10-14/15 at 02:10Z** (fixture `tests/test_scheduler/fixtures/deep_night`,
  from /api/projects2): NGC 604 88.4 (hours up 9.4: 33.5, fill RC16 6.0 h /
  Piggy 9.1 h: 24.2, priority 55: 11.0, moon 10.0, transit 0.2 h: 9.7),
  Heart 84.5 (32.9, RC16 8.0 / Piggy 3.1 h: 21.6, 12.0, 10.0, 8.0), M31
  69.1 (33.5, RC16 1.4 / Piggy 0 h: 3.7, 13.0, 10.0, 8.9).

### Off-target alert and "Restart tonight from now" (PS-143)

- **On target** cell of the Where is it panel (`off_target` in `GET
  /api/night/where`, `scheduler/off_target.py`): separation of the mount
  (NINA #1 mount info; the planned J2000 center is precessed to the date
  when the mount reports JNow, `off_target_mount_epoch` auto | jnow | j2000)
  and, when one is at most `off_target_solve_max_age_min` (15) old, the
  latest RC16 plate solve (`<data_dir>/solves/<night>/rc16.jsonl`, wins over
  the mount) from the planned center: the goal's coordinates, a PS-111
  mosaic panel's own center, or for a Piggy-driven target in
  `piggy_center_mode=on` the PS-26 shifted RC16 center for the pier side.
- Alert: over `off_target_arcmin` (10') for more than `off_target_subs` (2)
  consecutive subs or for `off_target_minutes` (5) while imaging (armer
  RUNNING or WATCHING, NINA #1's leaf an exposure, nothing on the running
  path a slew, center, AF, flat, dark, bias, calibration or tracking test,
  mount not slewing). Any non-imaging read restarts the streak. One
  priority Pushover per target per night, red chip while it holds, events
  kind `off_target` (alert / clear). A background loop checks every 30 s
  while a night images. Observe only: nothing is sent to NINA.
  `off_target_mode` alert (default) | panel (chip only) | off.
- **Restart tonight from now** (button, confirm; `POST /api/arm/restart
  {"when": "after_exposure"|"now"}`): re-plans the remainder of tonight from
  the current goals (added targets, edited priorities, a new mosaic) and
  re-dispatches it through Resume's path (`_dispatch_and_start(companion=
  False)`). RUNNING: NINA #1 stops after the current sub as for Pause
  (PAUSED_OPERATOR, detail "Restarting", survives a PhotonScript restart),
  then re-dispatches. PAUSED_OPERATOR: re-dispatches at once (or once the
  pause's stop is done). Never warms, parks or turns a cooler off; the
  Piggy-600 keeps imaging (a pause that stopped it gets its companion back).
  Refused (409) while WATCHING (use the sideload preview), while a
  calibration capture job runs, in ARMED / PAUSED_UNSAFE / other states and
  with under 40 min of dark. A failed re-dispatch stays paused. Events kind
  `restart` (request / stopped / dispatched / failed / refused), one push.
  Every re-dispatch (Restart, Resume, the PS-77 safe-again resume) waits
  after its sequence stop until NINA #1's `/sequence/state` reads not
  running (poll 3 s, stop re-sent every 30 s, at most 10 min; an unreadable
  state goes on after 3 polls), and a load/start that still answers
  "Sequence is already running" is stopped and retried 3 times (5 / 15 /
  30 s backoff) before it fails. 2026-10-13: the restart's load and start
  ran 1 s before NINA's SEQUENCE-FINISHED and the restart failed.
  Resume and Restart from PAUSED_OPERATOR run that re-dispatch in the
  background (like the pause's stop): the request answers at once
  ("waiting for NINA #1"), pause.phase reads `resuming` with
  pause.resume.step / restart.step as progress (the dashboard's state chip
  shows it; Resume and Restart are hidden and refused meanwhile). A failure
  goes back to phase `paused`; a PhotonScript restart mid re-dispatch
  restores as paused (Resume again).

### Night pilot: adjustments over the night (2026-10-13)

Why: 2026-10-12 a stray PHD2 session (guiding on a FAIL calibration) on an
UNGUIDED night trailed every RC16 sub for hours, 300 s trailed until a
human switched to 180 s, and a restart race needed a manual retry.
`scheduler/night_pilot.py` turns the sub doctor's diagnoses (run events
kind `sub_doctor`) into bounded, logged, verified adjustments. Background
loop every 60 s (`run_pilot`, app startup).

| cause (sub doctor) | action | switch | max / night | cooldown | verified on |
|---|---|---|---|---|---|
| guider_conflict, UNGUIDED night, PHD2 active | stop PHD2 guiding (`stop_capture`) | `night_pilot_stop_phd2` | 2 | 30 min | ecc |
| guider_conflict, GUIDED night, FAIL calibration | rest of tonight UNGUIDED (`fallback_unguided`, PS-156 caps) | `night_pilot_unguided_fallback` | 1 | - | ecc |
| tracking, RC16 unguided, shared mount or measured drift | RC16 subs a step shorter for tonight (`night_pilot_sub_steps` 300,240,180,120, floor `night_pilot_sub_floor_s` 120; the longest lower step with matched darks, i.e. in the night dark quota or in the library, else one whose darks scale from a bias at the same epoch, else the next step; the push says "darks match" / "scaled from the <date> bias" / "darks owed") as goal exposure overrides + Restart tonight from now after the current sub | `night_pilot_shorten_subs` | 2 | 15 min | ecc |
| focus, soft round stars, other rig sharp | AF: RC16 a direct ninaAPI autofocus between subs (`GET /equipment/focuser/auto-focus`, `night_pilot_af_direct` on: waits for NINA #1's camera to go idle after the running sub, only inside an imaging block and never in a flat / dark / bias / TPoint / tracking test / calibration / slew / AF item; verified on AUTOFOCUS-STARTING then AUTOFOCUS-FINISHED in the event history and a non-zero focuser position read back); refused, not finished, or the switch off: restart after the sub (target start runs AF); Piggy-600 NINA #2 stopped after its sub + companion re-dispatch | `night_pilot_af` | 2 | 30 min | HFR |
| pointing, plate solves failing (>= `night_pilot_solve_failures` 3) | goal inactive for tonight + restart | `night_pilot_skip_target` | 1 | - | subs on another target |
| clouds, seeing | nothing (run event only) | | | | |
| focuser; guided trailing; reselect star | one push for a human, nothing sent | | | | |

- `night_pilot_mode`: **act** (default, Jeremy's request) | advise (the
  same decisions, one "Night pilot (advise): would have ..." push per rig
  and action, no command) | off. A switch that is off makes that action
  advise only. Diagnoses under `night_pilot_min_confidence` (medium) are
  only noted.
- Verification: the next `night_pilot_verify_subs` (2) subs of that rig
  that started after the action took effect (a sub cap: only subs at the new
  length) against the diagnosis' numbers: better = ecc down 0.05, HFR down
  10%, or every sub passed QA. Not better: a shorten step is undone (the
  goal goes back to the length before it, or its default, with a restart
  after the current sub when the guards and 45 min of dark allow, else
  from the next dispatch), "trailing is not drift-per-second" is recorded
  (morning report "learned"), one priority push asks a human and there is
  no more shortening that night (2026-10-12 replay: 300, 180 and 120 s
  trailed alike and the night would have stayed at 120 s for nothing); a
  step that helped may be followed by one more on a later diagnosis. AF
  gets one more try; then one priority push for a human
  and no more tries for that cause tonight (a PHD2 stop that did not help
  pushes too, but a PHD2 that starts guiding again later is stopped again,
  at most twice a night). No subs within
  `night_pilot_verify_timeout_min` (60): a priority push.
- Restart race: a restart the armer reports failed is retried once after
  NINA #1 reads idle; the armer's re-dispatch also waits for NINA #1 to wind
  down (cherry-picked ps-bugs-1013 fix).
- Guardrails: acts only while the armer is RUNNING (never ARMED, PAUSED_*,
  WATCHING, COMPLETE); never during a calibration capture job, an arm recipe
  in progress, a NINA #1 TPoint / tracking test / PHD2 calibration / flat /
  dark / bias / focus calibration item, or PHD2 calibrating; no re-dispatch
  with under 45 min of dark; at most `night_pilot_max_actions` (6) per
  night and one per rig per `night_pilot_rig_cooldown_min` (15); never
  repeats a (retired, old-log) sub doctor remedy. Never arms, disarms, parks,
  unparks, touches the roof / safety or a PHD2 calibration.
- Every step is a run event (kind `night_pilot`: act, advise, held, human,
  annotate, dispatched, retry, verify, failed, learned, revert) and each
  action and its result one push: "Night pilot: <what> because
  <evidence>; next check <HH:MM> local". State per night in
  `<data_dir>/night_pilot/<night>.json`; `GET /api/night/pilot`; the
  dashboard's Tonight's Run card has a "Night pilot" line (last action and
  result); the morning report has its lines and "Night pilot learned: ..."
  suggestions (never applied).
- Tonight-only goal changes (sub caps, a skipped goal) are ledgered with
  the original values and restored by the dawn shutdown (a step "night
  pilot restored ..."), or by the next tick after a night that ended any
  other way (disarm, error, service restart). A goal edited by hand since
  the pilot changed it is left alone and said so.

### Unguided guard: nothing guides on an unguided night (2026-10-13)

Why: 2026-10-12 and 10-13 were armed UNGUIDED but NINA #1 had PHD2
connected as its guider (the armer's connect-all at arm and the sequence's
Start area both connected it; on 10-13 also by hand, calibration FAIL).
NINA's meridian flip resumes guiding whenever a guider is connected, even
one that was not guiding (10-13: MOUNT-BEFORE-FLIP 02:24, GUIDER-START
02:28:03, MOUNT-AFTER-FLIP 02:28:32), and each autofocus after it stopped
and restarted it (02:57, 03:40, 04:18). The MeridianFlipTrigger has no
setting for it, so the guard keeps the guider disconnected instead
(`scheduler/unguided_guard.py`).

- Sequences: an unguided night's Start area has an Annotation instead of
  ConnectEquipment(Guider) (kept on a guided night and for PS-66 Direct
  Guider dithers). Lint rule `unguided-guider` warns on an unguided run
  that connects the guider.
- Armer connect-all (arm, restart, the dashboard button) skips the guider
  on an armed unguided night ("skipped (unguided night)").
- Armer check at arm, at a restart, before every (re-)dispatch and every
  tick while RUNNING: NINA #1's guider (`/equipment/guider/info`) and
  PHD2's AppState (`get_app_state`). Direct Guider is left alone.
- `unguided_guider_policy` disconnect (default) | alert | off:
  disconnect = a connected PHD2 guider is disconnected in NINA
  (`/equipment/guider/disconnect`); guiding / calibrating / lost lock is
  stopped (`/equipment/guider/stop`, disconnect, then PHD2 `stop_capture`
  if PHD2 still guides). Guider commands only: never the mount, the camera
  or the sequence, never a start. At most one command per 2 min and 8 a
  night. alert = the push with the manual step (NINA #1: Equipment >
  Guider > Disconnect), nothing sent. off = no guard, connect-all connects
  the guider as before.
- One push per episode, naming the cause from NINA #1's event history
  (`attribute_starts`: a GUIDER-START within 15 min of MOUNT-BEFORE-FLIP or
  3 min before MOUNT-AFTER-FLIP = the flip, within 5 min after
  AUTOFOCUS-FINISHED = an autofocus, within 3 min after MOUNT-CENTER = a
  center), priority 2 when the stop failed. Run events kind
  `unguided_guard` (detect, stopped, disconnected, failed, alert, clear).
- The sub doctor's guider_conflict on an unguided night names it: "NINA
  started PHD2 after the meridian flip (08:28Z) in an unguided night ..."
  (the guard's detect event live, NINA #1's event history in a replay).
- Not covered: a WATCHING sideloaded night (NINA's own), guided nights
  (PS-85 per-block unguided blocks keep the guider).

### Dispatch gate: lint every night against the arm (PS-197, 2026-10-15)

Why: 2026-10-14 armed UNGUIDED, deep night on NGC 604. The NGC 604
container held a StartGuiding; the guard had disconnected the guider, so it
failed 3 attempts, SkipInstructionSetOnError skipped the container and the
night loop parked, unparked, slewed, centered and focused on NGC 604 for
20 min with no lights. The armer's lint was told the guiding mode the
TARGETS carried (`any(t.start_guiding)`), so a guided plan linted clean.

- One place sets the guiding flag: `target_planner.apply_guiding(targets,
  guided)`. `plan_night_sequence(..., guided=)` applies the arm's mode to
  every target it returns (split, deep, the split around a deep pick, a
  Piggy-driven hold, the fill); the armer passes `guided=_use_guiding()`
  and applies it again (unguided also clears PS-85 per-block flags).
- `scheduler/dispatch_gate.py` checks the generated JSON against the ARMED
  mode in `Armer._dispatch`, so before every NINA #1 load (arm, resume,
  restart, unguided fallback, deep): unguided = no StartGuiding and no
  ConnectEquipment(Guider) (Direct Guider dithers excepted); guided = a
  StartGuiding in every target the plan kept guided (per-block counts);
  always = at least one target container with exposure items. The
  sequence_lint ERRORs count too.
- ERROR: nothing is loaded. The same goals are planned again in split
  mode once (when the refused plan was not split already); clean = that
  night is loaded and a priority push says so ("Dispatch gate refused
  tonight's deep plan (...). Loading a split plan of the same goals
  instead"). Otherwise NINA #1 is left idle and "Dispatch FAILED: dispatch
  gate refused the night (...)" is pushed (priority 1).
- Run events kind `dispatch_gate` (ok, refused, fallback, failed);
  `GET /api/arm` has `dispatch_gate` (ok, action, line, errors) and the
  Arm card shows it as a small "Dispatch lint" line.
- Unguided guard: at arm, restore and right after each dispatch's load
  (phase `loaded`) it also reads NINA #1's loaded sequence (GET
  /sequence/state) for StartGuiding not yet finished. Found = run event
  `unguided_guard` / `loaded_start_guiding`; a priority push once per
  night when that sequence runs (or after our own load).

### Night pilot sub watch and closed-roof darks (2026-10-14)

Jeremy: "augment the night watch and pilot to watch the subs as they come in
to adjust if something doesn't work", and "we did have some closed roof
tonight, it would have been nice to have gotten some of those 450 second
subs in". The sub doctor speaks only after failed subs; the **sub watch**
(`scheduler/sub_watch.py`, `night_pilot_watch`, default on) reads every
graded light of both rigs on each pilot tick, pairs it with the other rig's
sub exposed at the same time, and hands what it shows to the pilot, which
runs it through the same guards, rate limits, verification, ledger and dawn
revert as a sub doctor diagnosis (run events kind `night_pilot`, field
`source: "watch"`).

| signal | when | action | mode key (default) |
|---|---|---|---|
| focus drift | HFR over the night's best for that target and filter (median of its 3 sharpest round subs) by `night_pilot_watch_hfr_rise` (20%) on each of the last `night_pilot_watch_subs` (3) round subs; the other rig's same-time subs did not rise (else "the sky, not focus"); no AF since the softening began that left the next sub as soft (else "the AF did not bring it back") | `refocus`: RC16 direct ninaAPI AF between subs (restart fallback), Piggy-600 companion re-dispatch; verified on HFR | `night_pilot_refocus_mode` (advise) |
| trailing past the learned limit | UNGUIDED night, PHD2 idle, 2 of the last 3 RC16 subs fail ecc, stars not down, sub longer than the learned unguided limit of the region it points at; the Piggy-600 trails too (shared mount) or is round as the RC16's trail rate predicts at 1.29"/px | `limit_subs`: the goal's RC16 subs cut to the limit (`unguided_limits.snap_length`, matched darks first) for tonight, restart after the current sub; verified on ecc, undone if it did not help | `night_pilot_limit_subs_mode` (advise) |
| only the RC16 trails | the Piggy-600 should trail at that rate but is round | a human (flexure, optics); nothing sent | |
| PHD2 on an unguided night | the newest sub of either rig exposed with PHD2 guiding / settling / lost star, or PHD2 active now | priority ALERT push at once (once per spell, any mode); `guider_stop`: the unguided guard hook, else PHD2 stop_capture | `night_pilot_guider_stop_mode` (advise) |
| clouds, roof | star counts down on both rigs; roof row failed | noted only | |

- Modes: each watch action is off | advise | act on its own key, default
  advise. `night_pilot_mode=advise` (the scope's setting) caps them all;
  setting `night_pilot_mode=act` alone does NOT make them act: set the
  action's own key to act when Jeremy says so.
- One actor per remedy: `stop_phd2` / `guider_stop`, `run_af` / `refocus`
  and `shorten_subs` / `limit_subs` share their max per night and cooldown,
  and a sub doctor diagnosis of a rig in the same tick speaks for the
  watch's signal on that rig.
- Advise timeline: every advised decision that passes the guards is
  recorded (rate limited as if it had run; the same cut on the same target
  is advised once), one push and one run event per rig and action per
  night. Morning report: "Night pilot would have: 21:48 cut RC16 subs 180
  -> 120 s (learned unguided limit, Dec +30..+50, east); 02:31 stopped PHD2
  guiding ... (advise: nothing was sent)".
- **Unguided guard hook** (`scheduler/pilot_hooks.py`,
  `guider_active_unguided(cfg, rig, evidence, now)`): the branch
  ps-unguided-guard fills it. Return `{"ok", "detail"}` when it stopped
  guiding, None to let the pilot fall back to PHD2 stop_capture. Called
  only when `night_pilot_guider_stop_mode=act`.
- NINA's AF history (GET `/event-history` on both NINAs) is read only when
  a focus signal needs it.
- Backtest (read only, nothing sent, scratch state):
  `photonscript pilot-backtest --night 2026-10-13 --url
  http://100.94.189.77:8100 [--no-limits] [--json]`
  (`scheduler/pilot_backtest.py`). The limits table is today's (hindsight).
  2026-10-13: 21:48 cut M31 RC16 subs 180 -> 120 s (Dec +30..+50 east),
  21:52 a human (one RC16 sub at ~2.8"/min the Piggy-600 did not show),
  22:33 Piggy-600 softer but the RC16 rose too (the sky, no refocus),
  23:31 cut Heart RC16 subs 180 -> 120 s (Dec +50..+70 east), 02:31 ALERT
  + stop PHD2 (NINA started guiding at the 02:28 flip), 03:00 ALERT +
  03:03 stop again (NINA restarted guiding after the 02:52 AF), 04:21
  closed roof with 99 min to dawn: Piggy-600 450 s x13 of 22 owed.

**Closed-roof darks** (`closed_roof_darks` on | off, default on;
`closed_roof_darks_min_left_min` 30). The RC16 has shot its dark quota in
the UNSAFE branch since PS-160 (`unsafe_darks_enabled`), but the Piggy-600
companion filled its quota only before the light loop, so a roof that
closed mid-night (2026-10-13 04:20, 1.6 h before dawn) left NINA #2 idle
with 22 x 450 s darks owed. Now every OSC_LIGHTS_UNTIL_DAWN pass starts with
`OSC_CLOSED_ROOF_DARKS` (`calibration._osc_closed_roof_darks`):
LoopWhileUnsafe (never with the roof open; ends after the dark in progress
once safe, then the usual wait-for-safe and resume hold hand back to the
lights), a TimeCondition at astro dawn minus the minimum, the PS-160 cooler
gate (darks only at the setpoint), then the owed blocks (the night quota
deficit, tonight's Piggy light lengths first, each block roof-gated and
ending one exposure before astro dawn). No mount, cover, filter or guider
instruction; refused by the PS-188 window `manual` without a cover; with
the Wanderer Cover the pass closes it first (close_if_unsafe). Counts are
the quota at dispatch; a second closed spell the same night may shoot a
length again (idle time only). Default on because it only uses closed-roof
idle time with the same gates as the trusted pre-light darks. The pilot
records each spell (run event `closed_roof`, one push, morning line "Night
pilot closed roof: roof closed at 04:20 with 99 min to dawn: the sequences
shoot owed darks until safe or dawn: Piggy-600 450 s x13 (owed 22); ...");
it never commands, opens or moves anything. The RC16 list is the one at
dispatch (lengths learned later, e.g. 120 / 240 s from a dawn limits
refresh, join at the next dispatch).

Night test needed: an armed act-mode night for each watch action (one at a
time), a mid-night roof closure with the new companion (NINA #2 darks start
and hand back on safe), and the hook once ps-unguided-guard lands.

### Safety dropout grace: an unreadable monitor is not a closed roof (2026-10-16)

The armer reads NINA #1's safety monitor every 30 s tick. Three answers:
SAFE, UNSAFE (Connected, IsSafe false) and UNREADABLE (ninaAPI error or
timeout, `Success: false`, or the monitor reported not Connected). Before
this change an unreadable read never paused anything (only the ~3 min
"UNREADABLE" push); a sustained one was left to NINA's own
SafetyMonitorCondition.

- A reported UNSAFE acts on the first tick, as before (PAUSED_UNSAFE, push,
  PS-77 stop / park after `unsafe_stop_grace_s`). Never graced, also when a
  dropout is in progress.
- An UNREADABLE read starts a dropout (log line "safety dropout START", run
  event kind `safety_dropout` value `start`). Inside
  `safety_dropout_grace_s` (60) and before 2 unreadable reads in a row it
  changes nothing: no pause, no park, no push.
- Past the grace: one priority push ("UNREADABLE for N s, past the 60 s
  dropout grace"), event `over_grace`. With `safety_dropout_action`
  `alert` (the default) that push is all: the read stays unknown and
  nothing pauses, stops or parks (the old behaviour). `unsafe` is an
  opt-in: the read then counts as UNSAFE: RUNNING pauses (the pause push
  says it is a dropout), PS-77 stops / parks if SAFE_LOOP is still running
  `unsafe_stop_grace_s` later (and re-dispatches once safe), an operator
  pause parks, a watched night pauses (alert only). The watchdog's own ~3 min push is not
  repeated; its "readable again" push closes the episode.
- The first readable read ends the dropout (event `end`, with its length,
  reads and whether it outlasted the grace). Unreadable reads more than
  5 min apart are separate dropouts (no carry-over from an earlier night).
- Not changed: the dawn flat hold and the pre-dusk hot-pixel map use the raw
  read; the hot-pixel map in a pause only runs on a reported unsafe; the
  safety history records the raw read (grading rejects only reported
  unsafe); the PS-1 cross-check runs only on a reported unsafe.
- Not covered (NINA side): NINA's own SafetyMonitorCondition treats a
  disconnected monitor as unsafe at once, so a NINA-side blip still makes
  the night loop leave SAFE_LOOP and park. PhotonScript cannot grace that.

Night test needed: an armed night with a deliberate short safety dropout
(disconnect the safety monitor in NINA #1 for under 60 s: no pause, "START" /
"END" lines in the scheduler log and the events file) and a long one (over
60 s with the roof safe: one push, the armer stays RUNNING with the default
`alert`; only if `unsafe` is opted in: PAUSED_UNSAFE, PS-77 check, resume
once it reads safe again). A real roof closure the same week to confirm the
immediate pause is unchanged.

### Frozen sensor watch: a reading that stopped moving (2026-10-16)

`scheduler/frozen_sensor.py`, in the PS-150 loop (60 s), dusk to dawn
(`nina_watch_sun_alt_deg`), per rig, GETs only. A reading identical within
1e-6 for `frozen_sensor_minutes` (30) is frozen:

- camera: Temperature and CoolerPower together, only while Connected with
  the cooler on (a regulated sensor holds its setpoint, but its TEC power
  moves; a warm sensor is not judged);
- focuser: the EAF Temperature (the focus model's ambient proxy, known to
  freeze at 21.29 C);
- weather: every finite field of NINA's weather device together
  (temperature, humidity, dew point, pressure, sky temperature / quality /
  brightness, cloud cover, wind). One field may stay put; the station may
  not.

The safety monitor is a boolean that legitimately reads the same all night,
so it is not judged here (its liveness is the dropout grace above and the
agent's reconnect watchdog). An unreadable reading resets that sensor's
clock. Output: `GET /api/nina/watch` key `frozen` (per rig and sensor the
value and minutes unchanged, plus the frozen list), amber lines in the NINA
watch chip on the dashboard, run events kind `frozen_sensor` (`frozen` /
`clear`) and one push per rig and sensor per night (priority 0).
`frozen_sensor_mode`: alert | panel (no push) | off. Warning only: nothing
acts on it.

Night test needed: one armed night with the dashboard chip open; confirm no
false flag on a regulated camera and the AARO station, and that a frozen EAF
thermometer (21.29 C) is flagged once.

### Stuck-loop detector: should be imaging, no lights (2026-10-15)

Why: 2026-10-14 22:21 to 22:41 MDT NINA #1 ran AF, center, park, unpark
three times with zero lights (a StartGuiding in the target container failed
on an unguided night, the container was skipped and the night loop started
it again). Nothing paged; Jeremy saw it in NINA.

`scheduler/stuck_loop.py`, called from the armer's tick while RUNNING and
the roof is not unsafe (`stuck_loop.armer_tick`, a background check at most
every `stuck_loop_poll_s` 60 s), only inside a planned target window between
astro dusk and dawn. GET only: NINA #1 / #2 `/event-history` and the subs
log. Per rig the clock starts at the latest of the window start, dusk, the
last light or IMAGE-SAVE (darks and flats count as busy), the roof turning
safe again, the armer coming back to RUNNING (a pause / unsafe spell / a
restart: no tick for 3+ min) and the detector's own restart. The
Piggy-600 is judged only while NINA #2's event history shows its sequence
running (a companion not dispatched tonight or ended is the NINA
watchdog's).

| signal | rule (defaults) |
|---|---|
| no_light | no light for max(`stuck_loop_gap_subs` 3 x the planned sub + `stuck_loop_gap_margin_min` 10, `stuck_loop_gap_min` 20) min (RC16 120 s: 20 min; Piggy-600 450 s: 32.5 min); held while an AF (< 15 min) or a meridian flip (< 20 min) runs |
| restarts | `stuck_loop_repeats` 2 AF + MOUNT-CENTER cycles with no light, confirmed by a 3rd cycle, a park after the last center, or no light a sub + 5 min after it |
| park_cycle | `stuck_loop_park_cycles` 2 park -> unpark with no light and no unsafe SAFETY-CHANGED between |

- Not a loop: NINA #1 running a calibration / dark / flat / bias / TPoint /
  tracking test item (the pilot's BUSY_WORDS) is a planned slot, no alert.
- One priority push per episode (an episode ends with a light, or with the
  pilot's restart) naming the steps from the event history: "RC16 looping:
  AF 22:21, 22:29; center x2; park/unpark x2; no light in 16 min. Target
  NGC 604. Likely: the target container keeps failing ... the guider was
  disconnected at 22:21: a StartGuiding on an unguided night fails".
- `night_pilot_stuck_loop_mode` off | **advise** (default) | act;
  `night_pilot_mode=advise` caps it, `off` turns it off. act = Restart
  tonight from now (after the current sub) for the RC16, NINA #2 stopped
  after its sub + companion re-dispatch for the Piggy-600, once per rig per
  night behind the pilot's live guard (RUNNING, no calibration job, arm
  recipe or busy NINA #1 item) and 45 min of dark. A second episode that
  night is an alert only ("already restarted it at 22:35").
- Run events kind `stuck_loop` (alert, advise, act, held, failed, repeat,
  clear), state `<data_dir>/stuck_loop/<night>.json`, morning report lines
  "Stuck loop 22:35: RC16 looping: ... (advise: the pilot would have
  restarted it, cleared 22:47)". `stuck_loop_mode=off` disables it.
- NINA quirk: event history MOUNT-CENTER rows carry a naive LOCAL time
  ("2026-10-14T22:25:09.4002"), every other row an offset; the detector
  reads a naive Time as observatory local time (`stuck_loop.event_time`).
- Backtest (`stuck_loop.backtest(doc, cfg)`, nothing sent; fixtures
  `tests/test_scheduler/fixtures/stuck_loop/2026-10-14.json` and
  `pilot_watch/2026-10-13.json`): 10-14 alerts once at 22:35 (restarts +
  park_cycle, six minutes before the hand restart at 22:41); 10-13 (AF
  every ~40 min, lights between) no alert on either rig.

Night test needed: an armed act-mode night with a sequence that loops.

### Re-entry gates: no AF / re-acquire when nothing changed (2026-10-15)

Why: 2026-10-14 NINA #1 autofocused at 22:21, 22:29, 22:35 and 22:41 and
every run landed on 5719 (L, HFR 7.13 / 6.19 / 6.64). Every entry into a
target container (the SAFE_LOOP coming back after a park, an armer restart,
Resume, a sub-length change) re-runs slew, the start AF (~3.3 min), center
and StartGuiding.

Mechanism (`scheduler/reentry.py`, the PS-61 cooler-gate pattern): NINA has
no "autofocus only if the last one is old" instruction; its AF triggers
ride on the light SmartExposures, so they cannot stand in for the start AF
that tightens the stars before the center. So each gated step sits in a
small container of its own whose first item is an ExternalScript,
`deploy\reentry-gate.cmd` -> `photonscript reentry-gate` -> `POST
/api/reentry/gate`, ErrorBehavior 1 (SkipInstructionSetOnError). The
service answers when NINA reaches the item, from NINA's own state, so the
same gate covers an armer re-dispatch and the in-sequence SAFE_LOOP
re-entry, and a decision cannot go stale between a dispatch and the
target. SKIP: the script exits 1 and NINA skips the rest of that small
container only. RUN, the service down, a timeout, any error: exit 0 and
the step runs as before.

| container | skipped when |
|---|---|
| `<target> start AF (re-entry gate)` = gate, seed move, RunAutofocus | the newest finished AF in NINA #1's `/event-history` (runs cut by an unsafe spell or a sequence stop are ignored; a later failed AF = RUN) ended under `reentry_af_skip_min` (45) min ago; its NINA AF report (ninaAPI `focuser/last-af`, else `nina_autofocus_reports_dir`) is a good fit; the focuser temperature moved at most `reentry_af_temp_c` (1.0 C) since (frozen sensor: a `focus_frozen_temps` value, or unchanged to 0.01 C for `reentry_frozen_h` 6 h by the focus table's track or the AF reports: time only); the offset from the AF's filter to the start filter is known (same filter, PS-76 measured or `focus_filter_offsets`). The service then moves the focuser to that AF's best focus + the offset and reads it back (3 steps); a failed move answers RUN. |
| `<target> slew (re-entry gate)` (mode on) | the mount is connected, tracking, not parked, not slewing and within `reentry_mount_arcmin` (10') of the target's NINA coordinates |
| `<target> center (re-entry gate)` (mode on) | the slew gate skipped AND the newest RC16 light (ended under `reentry_center_max_age_min` 60 min ago, no park / unpark / flip / center / mount disconnect since it started) plate-solves (stored solve, else ASTAP now) within `reentry_center_arcmin` (2') of the target. Pointing error unknown: the cheap re-center (slew skipped, center runs). |

- `reentry_optimize`: off (no gates, the old sequence) | advise (all gates,
  always RUN, decisions logged) | **af** (default: the AF gate only) | on
  (AF + slew + center). Default af because every AF rule is measured from
  NINA at that moment (event history, AF report, live focuser
  temperature), any doubt answers RUN, and NINA's own triggers stay as the
  net: every light SmartExposure keeps its HFR-increase and temperature AF
  triggers (smart policy: also the timed AF), which NINA measures from the
  same last AF the gate checked. The slew / center skip waits for a
  supervised night (on).
- Mode on, dispatch after dusk (restart, Resume): the Start area no longer
  stops tracking ("holding until nautical dusk"), which drifted the field
  15"/s until the target; the SetTracking stopped moves to the End area
  after the park. A dusk-gated arm is unchanged.
- Script missing on the machine (`reentry_gate_script`): no gates, a
  Start-area annotation says so. Lint rule `reentry-gate` (ERROR): the
  gate is the first item of a one-pass container without triggers that
  holds its step and nothing that shoots, ErrorBehavior 1, Attempts 1.
- Log: run events kind `reentry` (skip_af / run_af / advise_af, the same
  for slew and center; detail, AF age, dT, target position, saved_s),
  `GET /api/reentry` (tonight's decisions), morning report line
  "Re-entry: 3 skip(s), ~10 min saved (22:29 NGC 604 AF; ...); 1 ran".
- Replay of 10-14 (`tests/test_scheduler/test_reentry.py`: the stuck-loop
  fixture's event history, AF reports synthesized from it): RUN at 22:21
  (no AF since NINA started), SKIP at 22:29, 22:35 and 22:41 (last AF 3 to
  4 min old, dT 0, focuser to 5719): 8.2 min of AF NINA actually ran
  (3.3 + 3.2 + 1.7 cut short), ~10 min at full AF length. Mode on would
  only have skipped the slew at 22:41 (no light solved yet: re-center).

Night test: `reentry_optimize=advise` first (decisions only), Pause +
Resume mid-target and read `GET /api/reentry`; then af; then on.

### AF on HFR rise: per-rig trigger settings (2026-10-16)

Every RC16 light SmartExposure (every_block, smart and focus-drive alike)
and the Piggy-600 `OSC_LIGHT_LOOP` carry NINA's "Autofocus after HFR
increase" trigger (`NINA.Sequencer.Trigger.Autofocus.AutofocusAfterHFRIncreaseTrigger,
NINA.Sequencer`, fields `Amount` % and `SampleSize` subs; type string from
the known-good AARO sequence, a1f2886, and a NINA 3 saved sequence). The
values come from `af_policy.hfr_trigger`: `rc16_af_hfr_increase_pct` /
`piggyback_af_hfr_increase_pct` (10 %, min 5, max 100) and
`rc16_af_hfr_sample_size` / `piggyback_af_hfr_sample_size` (4 subs, min
3, max 50). The defaults are the values the sequences always used, so a
deploy changes nothing until a key is set. Before, every_block and
focus-drive hard-coded 10 % / 4 and only smart read the percent.

With the re-entry gates: NINA measures the rise over the lights since its
own last AF, so a fresh AF leaves it no history (no trigger until
SampleSize subs exist), and a gate SKIP is not an AF, so the trigger keeps
measuring from the AF the gate judged recent and good. The trigger sits
only on the SmartExposure (never in a gate container, never on the target
container, where it could fire before the start AF). Lint rule `af-hfr`
(ERROR, WARN when hand built; RC16 lint and the companion lint, so the
dispatch gate refuses it): every HFR trigger has Amount >= 5 and
SampleSize >= 3.

### Daily app lifecycle: NINA x2 + PHD2 closed after dawn, relaunched before noon (PS-170)

Why: PHD2 ran 2026-10-05 to 10-08 on one mount connection; after TheSky was
reworked its in-process SoftwareBisque driver went stale and refused every
guide pulse for three nights (PS-167). A fresh start each day gives every
night fresh driver instances.

- **Who acts:** `deploy\observatory-apps.ps1`, run by two scheduled tasks in
  jeremy's interactive session (`deploy\install-app-lifecycle-tasks.ps1`;
  logon type Interactive, so NINA and PHD2 open on his desktop, never in
  session 0). The service only reports and decides (`GET /api/apps/status`,
  `scheduler/app_lifecycle.py`); it never starts, closes or kills an app.
- **"PhotonScript Apps Stop"**, daily 06:30 and every 15 min until 11:00:
  `-Stop -Scheduled`. Acts once a day, when the service lists no blocker:
  armer not ARMED / RUNNING / WATCHING / PAUSED_*, no NINA #1 / #2 sequence
  and no calibration capture job running, sun above -6 deg, last night's dawn shutdown recorded with its
  cooler verify done and `app_lifecycle_stop_after_shutdown_min` (30) past
  it. A disarmed day has no shutdown to wait for. PHD2: `stop_capture`,
  `set_connected false` (releases the stale driver), `shutdown` over its
  event server; NINA #2 then NINA #1: close the main window. An app still
  open after 60 s is killed (the report says KILLED). TheSky is never
  touched (`-IncludeTheSky` by hand only). Refused when the service does
  not answer (the guards cannot be read). `-Force` waives only the
  shutdown timing and the once-a-day rule.
- **"PhotonScript Apps Start"**, daily 11:45 (`app_lifecycle_start_local`,
  before the 12:00 noon re-arm, whose PS-125 guard needs NINA #1 up):
  `-Start -Scheduled`. Only what does not answer is launched. TheSky must
  run with its mount connected (read-only `IsConnected` over :3040), else
  nothing is launched and one page goes out. NINA #1 / #2: `NINA.exe
  --profileid <GUID>`, the GUID resolved from the profile name
  (`app_nina1_profile` RC16, `app_nina2_profile` piggyback600, the NINA profile name of the Piggy-600) in
  `%LOCALAPPDATA%\NINA\Profiles\*.profile`; an unresolvable profile is
  never launched (it could open the wrong camera). PHD2: launched, profile
  `app_phd2_profile_id` (2) selected with `set_profile`, camera + mount
  connected with `set_connected true` (`-NoPhd2Connect` skips that). Each
  port (1888, 1889, 4400) must answer within 180 s. Then `POST
  /api/equipment/connect` and `POST /api/preflight?push=0` (no test push).
- **Reports:** every run that acts posts `POST /api/apps/report`
  (`<data_dir>/app_lifecycle.jsonl`); a failed run pages once per mode and
  day. Log: `<data_dir>\logs\observatory-apps.log`. System page panel
  "Observatory apps": ports, TheSky mount, "Close now: allowed / blocked
  (why)", last stop / start.
- **Off by default:** the tasks do nothing while `app_lifecycle_enabled` is
  off; a hand run always may (behind the same guards).
  `observatory-apps.ps1 -Status` prints ports, TheSky mount and blockers;
  `-DryRun` with `-Stop` / `-Start` shows what would happen.
- **Alerts (on even with the lifecycle off, `app_lifecycle_alert`):** the
  armer pages once at arm and once in the hour before pre-config when NINA
  #1, NINA #2 (piggyback on) or PHD2 (guided night) does not answer.
  Preflight row "Observatory apps running": FAIL only with the lifecycle on,
  on an armed day, 15 min past the launch time; WARN otherwise.
- **Interplay:** a `calibration_autofill` job (off today) that starts at
  sunrise holds the stop until it ends; one that would start while the apps
  are closed is refused for that day (one attempt a day). The PS-150 NINA
  watchdog only watches after sunset, so a closed NINA in the morning does
  not page.
- **One-time setup (scope PC, as jeremy, elevated PowerShell):**
  `powershell -ExecutionPolicy Bypass -File
  C:\astro\PhotonScript\deploy\install-app-lifecycle-tasks.ps1 -DryRun`, then
  without `-DryRun`. Watched first run by hand (normal PowerShell, morning
  after a night): `observatory-apps.ps1 -Status`, `-Stop -DryRun`, `-Stop`,
  `-Start -DryRun`, `-Start`; confirm both NINAs open on the right profiles
  (RC16 on :1888, Piggy-600 on :1889) and PHD2 on "Primary RC Profile
  (Guider)" with camera + mount connected. Then set `app_lifecycle_enabled`
  on the System page. Remove: `install-app-lifecycle-tasks.ps1 -Uninstall`.

### Settings ledger: what every program was set to, and when it changed

Why: 2026-10-12 failed on settings (PHD2 guided on a FAIL calibration,
AutoFind min SNR 50 vs a 28 SNR star, the pulse-guide alert silenced, NINA #1
plate solve at bin 2, TheSky at time zone +0 after a reinstall, a guide-scale
mismatch, the RC16 EAF counter reset), each found by hand afterwards.

- **What:** `scheduler/settings_ledger.py` snapshots NINA #1 and #2 (the
  active profile, `profile/show?active=true`, and every `equipment/*/info`),
  PHD2 (the PS-89 audit's get_* reads, calibration data, the registry profile,
  "don't show again" alerts, the PS-93 record, the dark library), TheSky (the
  PS-104 read-only scripts) and PhotonScript's own config (secrets redacted).
  READ ONLY: nothing is written to any program.
- **When:** at arm, at pre-config, after dispatch, on every armer state change
  and every `settings_ledger_interval_min` (60) while RUNNING / WATCHING. A
  snapshot is hashed and stored only when a setting changed; temperatures,
  focuser position, mount state and PHD2 state are kept as compact "live"
  readings in every index line. An app that does not answer keeps its last
  reading (marked "not read now"); PHD2 / TheSky never read yet fall back to
  the newest stored audit.
- **Where:** `<data_dir>/settings_ledger/<night>/` (index.jsonl, one
  `<id>.json` per change, status.json) and `latest.json`. Each new sub record
  carries `settings_id` (the snapshot in force); older subs are matched by
  capture time.
- **Rules:** `config/settings/desired_settings.toml` (edit it like the PS-89 /
  PS-104 desired files: id, app, path, desired form, when, severity, why,
  fix). Every snapshot is judged; the first FAIL of a rule each night sends
  one Pushover (`settings_ledger_push`). System page > Settings ledger shows
  the result, when each app was read and last changed, the exact fixes and a
  diff "what changed since the last good night".
- **One pager per setting:** a PHD2 rule that duplicates a PS-89 audit row
  names it in `audit_row` (`phd2_min_snr` -> `min_snr`, `phd2_guide_scale` ->
  `guide_scale`, `phd2_min_hfd`, `phd2_pulse_alert`, `phd2_darks_or_bpm`).
  While the ledger can page it (FAIL severity, `settings_ledger_enabled`,
  `settings_ledger_push`, phd2 in `settings_ledger_apps`) the ledger is the
  only pager: the PS-89 row keeps its status in the audit report, marked
  "pages from the settings ledger", and is left out of the PS-89 arm and
  re-audit pushes. Ledger off or not pushing: PS-89 pages it as before.
  (2026-10-13: the AutoFind min SNR could page from both in one night.)
- **CLI (read only):** `photonscript settings` (status),
  `--night 2026-10-12 --at 23:40`, `--sub <file>`, `--diff last_good latest`
  (also `YYYY-MM-DD`, `YYYY-MM-DD@HH:MM`, ids), `--full`, `--json`,
  `--url http://host:8100` from the desktop. `photonscript settings-snapshot`
  takes one now; `photonscript settings-backfill [--apply]` rebuilds past
  nights from the PS-89 / PS-104 audits, the sub records (gain, offset,
  readout, binning, setpoint, focuser position) and the PHD2 guide-log
  headers. Not reconstructable: NINA profiles before the ledger, PHD2
  registry-only settings (AutoFind SNR, the full profile), TheSky between
  audits, PhotonScript config history.
- **Off:** `settings_ledger_enabled` false; drop an app with
  `settings_ledger_apps`.

### Remote CLI: run photonscript commands on the scope PC (2026-10-14)

Why: Claude on the desktop reaches only the scope's HTTP API, so a one-off
command that must run on the scope PC (`frame-check --last 3 --type SNAPSHOT
--compare`, `settings-snapshot`, `unguided-limits --refresh`,
`guide-rms-backfill --apply`, `calibration-status`, `sub-doctor`) needed
Jeremy at the keyboard.

- **What:** `POST /api/cli/run` `{"args": ["frame-check", "--last", "3"],
  "timeout_s": 300}` runs `python -m photonscript <args>` on the scope PC
  (the service's own venv and checkout; an argv list, never a shell) and
  answers 202 with a job id. `GET /api/cli/jobs/{id}` gives status (running,
  done, failed, timeout, error), exit code, duration and the stdout / stderr
  tail (`?tail=N` bytes). `GET /api/cli/jobs` lists running jobs and the audit
  log, metadata only; System page > Remote commands shows it.
  Code: `scheduler/remote_cli.py`, `scheduler/routers/remote_cli.py`.
- **Allowlist, not a denylist:** `config/remote_cli.toml`. A command runs only
  when listed, with only its listed flags; each flag is also checked against
  the command's own typer options (unknown flag 400, a real flag not allowed
  remotely 403) and the whole argv is parsed by click before anything runs.
  `<command> --help` is allowed for every command. Never listed: anything
  that moves the mount / focuser / cover / roof, arms or disarms, starts or
  stops a sequence, restarts or updates the service, deploys, notifies,
  connects a device, runs PixInsight or moves / deletes files; also no
  `--url` / `--nina-url` and no flag that takes a file or folder path.
  Refused with 403 and a message. A test (`tests/test_scheduler/
  test_remote_cli.py`) fails when the TOML names a flag the CLI does not have.
- **Classes:** `read` = pure reports. `write` = local data maintenance
  (`settings-snapshot --no-push`, `unguided-limits --refresh`,
  `settings-backfill --apply`, `guide-rms-backfill --apply`): the request must
  carry `"confirm": "write"`, and it is refused (409) while the armer is
  RUNNING / WATCHING / PAUSED_* or a grading job writes, unless the command is
  `night_safe` (only settings-snapshot, which the armer itself takes
  mid-night). A remote settings-snapshot must carry `--no-push`.
- **Limits:** one job at a time per class (409 otherwise); hard timeout
  (default 300 s, max 1800 s) kills the child's whole process tree; the last
  256 KB of each stream is kept; jobs live in memory (the last 50), so the
  output of a job is gone after a restart (its audit line stays).
- **Audit:** `<data_dir>/remote_cli/audit.jsonl`, one line per run (time,
  client IP, the tailscale user when serve forwards it, args, class, status,
  exit, duration, bytes) and per refusal (the status and why). The token is
  never logged.
- **Access:** the API stays tailnet only. POST and the job detail also need
  the header `X-PhotonScript-Token` equal to `PS_REMOTE_CLI_TOKEN`; empty
  (the default) or under 16 characters = the feature is off (403). The token
  is not in the dashboard config editor (it can only be set in .env by hand)
  and the settings ledger redacts it.
- **Turn it on (scope PC, once):** make a random token on the desktop, e.g.
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`, add
  `PS_REMOTE_CLI_TOKEN=<token>` to `C:\astro\PhotonScript\.env` on the scope
  PC, then restart PhotonScript the normal way (the next deploy, or
  `photonscript restart`). Check: `GET /api/cli/jobs` shows `"enabled": true`.
  Turn it off: blank the line and restart.
- **Desktop client:** put the same `PS_REMOTE_CLI_TOKEN=<token>` in the
  desktop checkout's `.env` (`C:\Users\sleep\Claude\PhotonScript\.env`, never
  committed); `photonscript remote` reads it from there (or the environment)
  and sends it only in the header, never printed. Usage:
  `photonscript remote -- frame-check --last 3 --type SNAPSHOT --compare`,
  `photonscript remote --confirm-write -- unguided-limits --refresh`,
  `--url` (default the tailscale serve URL; `PS_REMOTE_CLI_URL`),
  `--no-wait` (print the job id), `--remote-timeout S`. It polls, prints the
  output and exits with the remote exit code (2 refused / unreachable, 124 on
  the remote hard timeout). Put the remote command after `--`.
- **One exception, observatory apps (2026-10-17):** `apps --start --only
  nina1|nina2|phd2` (class write via `write_flags`, `night_safe`, so it needs
  `--confirm-write`) runs `deploy\observatory-apps.ps1 -Start -Only <app>`:
  it launches that app ONLY when it is not running and never closes, kills or
  restarts anything. Refused (exit 2, nothing launched) when the app's port
  already answers, when a process of it is open but its port does not (a
  second instance), from session 0, for TheSky (never launched from here) or
  NINA #2 with the piggyback off. After the launch the script runs the same
  connect-only step as the arm (only disconnected devices; `--no-connect`
  skips it) and preflight. It waits up to `--timeout` (240 s) for the port
  (NINA `/v2/api/version`), returning before the remote hard timeout
  because a timed-out job's whole process tree is killed. The GUI shows on
  jeremy's desktop because the service runs as the Interactive
  "PhotonScript" task in his session (launcher task-Interactive), so the
  child script is in that session; under S4U (session 0) the command
  refuses. `apps` / `apps --status` is a read.
  `photonscript remote --confirm-write -- apps --start --only nina2`
- **Adding a command:** read its code first; if it changes no hardware, sends
  nothing and deletes nothing, add a `[commands.<name>]` block with `class`,
  `flags` (and `write_flags`, `require`, `night_safe` when needed), run the
  test, deploy.

### Target aliases: no placeholder names (2026-10-14)

Why: the Heart Nebula page read "also recorded as: ? . Heart Nebula imaging
(repeats while safe and up)_Container . OSC_LIGHTS_UNTIL_DAWN_Container .
OSC_LIGHT_LOOP_Container", M 31 "? . M 31". The subs were already credited
to the right goal (PS-78 canonicalizes on read); the list printed each sub's
provenance `target_raw`: '?' (Piggy subs written unattributed, named later
by the RC16 timeline / Piggy attribution / PS-51 correlation) and the
container names the agent used before PS-77 (kept by the PS-78 rename).

- **Rule** (`shared/target_names.py`): `is_placeholder_name` is true for
  blank, '?', anything ending in `_Container`, every generator container or
  loop name, and any UPPER_SNAKE name that is not a known target (an
  unlisted loop such as a future `OSC_NEW_LOOP`; catalog ids like NGC_7000
  excepted). `canonical_target` returns None for the UPPER_SNAKE case too,
  so such a name is never a target. `recorded_alias(raw, target)` gives the
  raw name only when it is a real other spelling ("M 31").
- **Read side**: sub_index rows carry `target_raw` = the alias or the
  target, and `target_recorded` = what the log holds; `raw_names` ("also
  recorded as") and `/api/target/history` use the clean value. The runs page
  hover uses `target_alias` from `/api/runs/{date}`. Logs are not rewritten:
  `target_raw` stays as provenance (writers rely on '?' meaning "was
  unattributed").
- **Report / backfill**: `photonscript target-aliases [--date D] [--json]`
  (remote CLI allowed) lists per target the hidden placeholder names and
  re-attributes any sub still stored under a container name (canonical
  target, or '?' for a loop) or still '?' where the RC16 target imaged at
  that time names it (PS-51; parked / slewing stay '?'). `--apply` rewrites
  the subs logs only, never a Library file or FITS; the next build_library
  refiles a re-attributed accepted sub as usual; then POST
  /api/projects2/recount. Dry run 2026-10-14 from the live API: 0 subs to
  re-attribute, 656 placeholder raws hidden (Heart 331, Cat's Eye 141,
  Andromeda 164, Crescent 10, NGC 604 8, 2 test targets), 427 subs stay
  unattributed (289 Piggy outside any RC16 window, 138 RC16 on 2025-12-14,
  2025-12-18, 2026-04-06).
- **Why the companion writes no name**: NINA #2's OSC_LIGHT_LOOP sits in no
  DeepSkyObjectContainer, so OBJECT and the $$TARGETNAME$$ file token are
  empty ("2026-10-14_04-08-10__0.00_450.00s_0058.fits"). Not changed: the
  loop is time-driven (segments end at RC16 handoff clock times), so a
  static target name would mislabel subs whenever the RC16 runs early or
  late, and a named sub skips the RC16 timeline / PS-51 correlation that
  places it today. Lint and split_guard also find the loop by its name.

### Add a target (PS-124)

The Target Goals **Add** box takes a catalog name, catalog id or alias
(case, spaces and punctuation ignored: "M76", "Little Dumbbell", "ngc604"),
or a name plus RA/Dec for anything else: `NGC 604 01:34:33 +30:47`,
`NGC 604 1.5758 30.783` (RA hours, or degrees when over 24; Dec degrees),
`01h34m33s +30d47m`, `Foo 01 34 33 +30 47 00` (spaced form needs the Dec
sign). No network: names resolve against `SEASONAL_TARGETS` in
`shared/astronomy.py` plus the user catalog.

- Coordinate adds land in `<data_dir>/user_catalog.json` (same name
  replaces) and from there in `get_seasonal_targets()`: the seasonal
  fallback, tonight's picker, campaign suggestions and the identify pass.
  Months default to four around the midnight transit.
- Defaults on create: `CATALOG_EXTRAS` (astronomy.py) gives a row its goal
  hours (else 8 h), narrowband mix and Piggy-600 OSC goal (NGC 604: 10 h
  Ha 50 / OIII 40 / SII 10 plus a 10 h OSC goal for M33). The response and
  the dashboard line show the rig hint (RC16 under 15', Piggy-600 over 60',
  both in between) and the sub lengths (`nb_exposure_s` 600 s on the 3 nm
  filters, PS-117; `bb_exposure_s` for LRGB). The hint does not change the
  plans.
- API: `POST /api/projects2/from_catalog {"name", "budget_hours"?}`,
  `POST /api/projects2/custom {"text"}` or `{"name", "ra_hours",
  "dec_degrees", "type"?, "size_arcmin"?, "budget_hours"?}`,
  `GET /api/catalog/user`, `GET /api/catalog/lookup?q=`.

### Target links: progression sheets on the target page (2026-10-15)

A goal can carry up to 10 external links (`ImagingProject.links`, each
`{label, url}`): the target page shows them as a "Links" row under "What it
is" (opens in a new tab), with an "edit links" add / remove box, and the
/targets card shows a small "N links" badge. Labels are 1-60 chars and
unique per goal; urls must be http(s). Nothing else reads them.

- API: `PATCH /api/projects2/{id} {"links": [{"label", "url"}, ...]}`
  replaces the whole list; `{"links": []}` clears; a bad entry is a 400
  and changes nothing. Links ride in `/api/projects2`,
  `/api/targets` and `/api/targets/detail`.
- Desktop CLI (not on the remote CLI allowlist): `photonscript
  target-links --target M31 [--add LABEL URL] [--remove LABEL] [--url U]
  [--json]` lists, or GETs the list and PATCHes it back.
- Current sheets (Claude artifacts): M31
  `https://claude.ai/artifact/P5vqZKUQW2eQxyRhhaNRcq`, Heart Nebula
  `https://claude.ai/artifact/JBqqooj2QLSh45Ys6HJQbe`, both labelled
  "Progression sheet":
  `photonscript target-links --target M31 --add "Progression sheet"
  https://claude.ai/artifact/P5vqZKUQW2eQxyRhhaNRcq --url
  http://100.94.189.77:8100`.

### Mosaic goals (PS-111)

A mosaic is ONE goal made of RC16 panel goals: each panel is its own project
(own coordinates, filter plan, PS-118 seconds crediting) carrying `mosaic`
{id, name, panel, row, col, of, companion, layout}. The dashboard shows one
card per mosaic (panel progress in capture order, the next panel, the
companion goal, a small preview of the panel outlines and the Piggy-600
frame); the Targets page groups the panels under the mosaic. Code:
`scheduler/mosaic.py`, API `routers/mosaic.py`, page `/mosaic`.

- **Layout.** Gnomonic tangent plane around the mosaic center; panels step
  by the RC16 frame (24.5' x 16.4' at 0.236"/px, PS-133) x (1 - overlap),
  default 15%. P1 is row 1 col 1 (top-left in a north-up, east-left view at
  PA 0), numbered row by row; that is the capture order. 2 x 2 at 15% =
  45.3' x 30.3'.
- **Rotation.** Neither rig has a rotator, so every panel is shot at the
  camera's fixed angle and the grid must use it. `rotation: "camera"`
  (default) = the circular median of the RC16 plate-solve PAs
  (`<data_dir>/solves/*/rc16.jsonl`, mod 180), else 0 (north up) with a
  warning. A requested angle that differs by more than 2 deg is kept but
  flagged (turn the camera by hand first). `major_axis_pa` is information:
  the camera angle that lays the long side along it is major + 90 (M31 PA
  35 needs the camera at 125 deg).
- **Order: finished in order** (not balanced): complete panels are usable
  on their own, each panel's data comes from fewer nights, and there is at
  most about one panel move a night. The night planner admits the first
  unfinished panels while the earlier ones (owed hours x 1.15) end before the
  mosaic's usable time (first panel's visible hours x 0.85); the rest are
  held (logged). All but the last admitted panel get LoopCondition(1) on
  their imaging loop (shoot the owed subs, then the next panel); the last
  keeps repeat-while-safe-and-up. An unsafe pause re-runs a panel's owed
  subs (the same reset as every target, PS-127); the next night re-plans
  from the credited seconds. The 14-night campaign gives a panel slots only
  once every earlier panel has no RC16 hours left.
- **Sequence.** One DeepSkyObjectContainer per panel, Slew + Center on the
  panel coordinates, an annotation naming the panel; panels of one mosaic
  stay together at the first panel's transit time.
- **Piggy-600.** It rides along: its frame moves with each panel (up to
  half the mosaic span from the mosaic center, 12.5' for the M31 2 x 2).
  `companion` names the goal its subs credit (M31's OSC plan): live and in
  the goal sync, a piggyback sub named after a panel credits the
  companion. Interplay: PS-26 (center the target in the 600 mm frame) only
  applies when the piggyback drives; on a mosaic night the RC16 drives, so
  the Piggy frame sits at the panel center plus the fixed boresight offset.
  PS-27 / PS-13: a panel move is an RC16 slew + center, so the Piggy sub
  exposing through it is split and rejected by the slew gate; in-order
  capture keeps that to about one sub per move. The Piggy subs are still
  filed under the panel's name in the runs table and the Library (proposed
  follow-up).
- **API.** `GET /api/mosaics`, `GET /api/mosaics/suggest?name=M31`,
  `GET /api/mosaics/preview?...`, `POST /api/mosaics` (definition +
  `companion` + `dry_run`), `PATCH /api/mosaics/{id}` (hours_per_panel,
  filter_mix, priority, active on every panel), `DELETE /api/mosaics/{id}`
  (panel goals only; subs and Library untouched). v1 `GET /api/mosaic/plan`
  and `POST /api/mosaic/create` remain for old links.
- **M31 (approved 2026-10-05).** 2 x 2 RC16 panels over the core and inner
  dust lanes, LRGB 50 / 17 / 17 / 17 at 300 s, 4 h per panel, companion
  "M 31" (the Piggy-600 shoots the whole galaxy). Create it with the
  suggestion (`/api/mosaics/suggest?name=M31`, then POST it with
  `dry_run: true` first).

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
- **Sync hygiene (PS-43, observe only):** `GET /api/sync/hygiene` = the whole
  backlog by top-level folder (an hourly background census pages
  remoteneed fully but keeps only counts; never while armed), each folder
  tagged astro or other, Syncthing folder errors (e.g. OneDrive cloud
  placeholders it cannot read) and a diagnosis. Shown on the dashboard sync
  strip and the System page. `GET /api/sync/ignore-suggestion[?format=text]`
  generates the ignore patterns for the non-astronomy folders (e.g.
  `/OneDrive`) for review; it never applies them. To apply: Syncthing UI on
  the scope PC, Folders > NINAShare > Edit > Ignore Patterns. Ignoring
  deletes nothing; the paths just stop being offered to the desktop.
- **Approve night** queues that night's accepted subs into the Library;
  **Reset library** (approved nights only) rebuilds it from scratch after
  bookkeeping changes.
- Typical throughput observed: 227 files / 11 GB overnight batch; watch
  progress on /api/sync or the Syncthing GUI on either end.
- **Calibration first (PS-182, Jeremy's step, PhotonScript never changes
  Syncthing).** One folder pulls one queue, so on 2026-10-08 the night's
  calibration sat behind 15 GB of lights at ~1 MB/s. Give
  `Library\Calibration` and `Library\piggyback\Calibration` folders of
  their own: Syncthing pulls folders side by side, so a few hundred MB of
  calibration no longer waits for the light backlog. Order matters (a
  receive-only parent would "revert" files a child folder writes):
  1. Desktop, Syncthing GUI `https://127.0.0.1:8384`: Folders > NINAShare >
     Edit > Ignore Patterns, add the two lines
     `/Library/Calibration` and `/Library/piggyback/Calibration`. Save.
  2. Scope PC, Syncthing GUI (`syncthing_url`): Folders > NINAShare
     (`ninashare-ddpnx-urgun`) > Edit > Ignore Patterns, add the same two
     lines (next to the PS-43 patterns). Save. Ignoring deletes nothing: the
     desktop keeps the copies it has.
  3. Scope PC: Add Folder, Label `Library Calibration RC16`, Folder ID
     `ninashare-cal-rc16`, Folder Path
     `C:\Users\jeremy\NINAShare\Library\Calibration`; Sharing: tick the
     desktop device; Advanced: Folder Type `Send Only`, Watch for Changes
     on. Save. Again for `Library Calibration Piggy`, ID
     `ninashare-cal-piggy`, path
     `C:\Users\jeremy\NINAShare\Library\piggyback\Calibration`.
     Optional in both: Ignore Patterns `/_quarantine` (QA-failed frames
     then stay on the scope; integrate never reads them).
  4. Desktop: accept both folder offers. Paths
     `D:\ninashare\Library\Calibration` and
     `D:\ninashare\Library\piggyback\Calibration` (the folders that are
     already there: Syncthing hashes them and only pulls what is missing),
     Folder Type `Receive Only`.
  5. Optional, desktop: NINAShare > Edit > Advanced > File Pull Order
     `Newest First`, so last night's lights come before the old backlog.
  6. Scope PC System page: `syncthing_calibration_folder_ids` =
     `ninashare-cal-rc16,ninashare-cal-piggy`, so the completeness view
     asks those folders what the desktop still needs (the Library share's
     remoteneed cache no longer lists calibration). Check with `photonscript
     calibration-status --url http://100.94.189.77:8100` on the desktop.
  Undo: remove the two folders on both PCs, then the two ignore lines.

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
masterLight_<F>_bin2.xisf (2x average downsample; 0.236"/px is oversampled).
Finally: masterSHO_review.jpg (R=SII,G=Ha,B=OIII) and masterRGB_review.jpg
(if R/G/B masters exist) - borders cropped 1.5%, channels autostretched to
~12% background. Per-frame accounting: every dropped frame is logged with
its stage ("DROPPED at registration [SII]: ...") plus a funnel line per
filter (staged -> calibrated -> cleaned -> registered).

Log: `Staging\<Target>\out\pipeline.log`, flushed per step; ends "EXIT OK"
or "ERROR: ...". Masters in `out\master\`.

One command for a whole target from the Library (PS-22, see
docs/OSC_INTEGRATION.md section 0):
`photonscript integrate --target "Andromeda Galaxy" --rig piggyback`.
Every run writes `ledger.json` and reports it to the scheduler (PS-33);
`photonscript integrate-watch --once` starts runs by itself when a goal is
met or enough new data arrived (PS-31); `photonscript autointegrate --once`
(PS-161, deploy\install-autointegrate-task.ps1) adds the Syncthing settle
wait, the two-rig blend, the OSC HOO image and a Pushover review JPG
(OSC_INTEGRATION.md "Auto-integrate"). Since 2026-10-15 it watches the RC16
too (first run at 1 h, `integrate_watch_rc16_*`), waits while the desktop is
in use (`autointegrate_idle_min` / `_max_cpu_pct`), retries a failed finish
alone (`autointegrate_finish_retries`, the 2nd without the plate solve),
re-blends when an input is newer (runs found under every root incl.
`C:/Astro/Staging`, `integration_extra_roots`), makes the vivid blend for
galaxies (`blend_vivid`) and writes a morning summary
(`Staging\Summaries\<date>.txt`, Pushover; `photonscript autointegrate-summary`). PS-142: each goal shows a campaign
status chip (Acquiring / Ready to process / Processing / Processed (vN) /
Published (vN)) and the target page a review panel (verdict, notes, asks with
Approve / Decline; a plan change shows its diff and goes through PATCH
/api/projects2 only after a confirm); a new ledger version pings Pushover;
`photonscript ledger-import` brings in the pre-PS-33 history. OSC_INTEGRATION.md
section 0b. Two-rig goals: `photonscript blend --target M31` puts the RC16 L
core into the Piggy-600 color image (full field + RC16-scale core crop) in a
new `Staging\Blend\` folder (PS-153, OSC_INTEGRATION.md section 0c).
With RC16 Ha (OIII, SII) masters (the Heart) the same command picks the
narrowband blend: Ha into red, OIII into green and blue, natural HaRGB +
gold, RC16-scale SHO / HOO core (`--mode l|nb|auto`, section 0d).
Mono runs HDR combine a filter's exposure-length masters (PS-177
`master_L` + `master_L_30s`) into `master_<F>_hdr` before the finish
(config `integrate_hdr_combine`, default on; the finish and blend prefer it).
Re-finish one master without integrating: `photonscript finish --master <xisf>
--name <NAME> [--palette gold] [--dry-run]` (NEW `Staging\Finish\` folder,
ledger kind finish; replaces `run-finish-osc.ps1` for re-finishes).
Every OSC finish also writes `<NAME>_vivid.{xisf,tif,jpg}` (config
`finish_vivid`, `--no-vivid` skips it): more saturation and a pink / warm /
blue lift through a mask that spares the background and the core, natural
stars (OSC_INTEGRATION.md section 1b). The configured gold palette only runs
for `finish_palette_kinds` targets (emission / nebula / remnant; galaxies and
clusters skip it, logged); `--palette gold` forces it.
Mono runs also get color (config `integrate_mono_color`, default on;
`--no-mono-color` skips it): after the finish, `color_run.js`
(`deploy/finish_mono_color.js`, the finish_osc.js functions reused) makes
`out\final\<target>_LRGB` (`_RGB` without L) from R G B (+ L, LRGBCombination),
`_SHO` (R = SII, G = Ha, B = OIII, SCNR green + gold / blue hue curve) and
`_HOO` (R = Ha, G = B = OIII; alone when SII is missing), each .xisf / .tif /
.jpg, `mono_color_steps.json`, `out\color.log`, a `color` block in the ledger.
The color image is then the run's final (ledger `final`, first in the Pushover
review JPG, the AstroBin packet's Final image, in that order LRGB, RGB, SHO,
HOO), the packet's processing text names ChannelCombination / LRGBCombination
and the SHO / HOO mapping, and the dashboard, target and review pages list the
color images of the latest ledger.
Color an existing mono run without restacking: `photonscript finish --mono-run
<run folder> --color auto|lrgb|rgb|sho|hoo [--dry-run]` (NEW `Staging\Finish\`
folder, ledger kind finish).

Pointing sanity (2026-10-13: the "NGC 2408" lights were really M27): integrate
checks each selected light's header RA / DEC against the target's catalog
position (more than `integrate_pointing_max_deg`, 1 deg = off target), its
altitude (CENTALT, else computed; below `integrate_min_alt_deg`, 20 deg) and
a mount that was not tracking (alt pinned while RA ran at the sidereal
rate). `integrate_pointing_check` (desktop `.env`, `--pointing`): warn
(default: run log, ledger `qa.pointing`, AstroBin known issues; everything is
stacked), refuse (those lights left out), off. Piggy-600 lights carry no RA /
DEC: only counted.

The same off-target and mount-stopped checks run on the scope PC when a light
is first filed into a Library target folder (`scheduler/library_pointing.py`:
dawn filing / `build_library`, and an Approve): `library_pointing_check` warn
(default) keeps it out of `<Target>/<Filter>/` and sends it back to review
with the reason (review_source `pointing`, `library_pointing` on the record;
dawn filing never re-approves it; a person's Approve of it files it and
records the override), refuse rejects it, off files as before
(`library_pointing_max_deg`, 1 deg). Only new links are checked; altitude is
not (a low sub is not a misfiled one).

One PixInsight job at a time (2026-10-12 two scripts overlapped in the gap
between one job's launches): a queue lock file
`%LOCALAPPDATA%\PhotonScript\pixinsight.lock` (pid, command, start time;
`photonscript/integration/pilock.py`) is held for a whole `integrate` /
`blend` / `finish` job and for an `integrate-watch` / `autointegrate` cycle,
and by the hand-run `run-integration.ps1`, `run-integration-osc.ps1`,
`run-finish-osc.ps1` and `prepare-integration.ps1` launchers
(`deploy\pixinsight-lock.ps1`; they record the PixInsight pid, so the lock
ends when that PixInsight closes). A second job waits up to
`pixinsight_lock_wait_min` (desktop `.env`, default 60; launchers
`-LockWaitMin`) and then stops naming the holder; the watcher and
autointegrate skip to their next cycle. A lock whose pid is gone (or reused)
is taken over; nothing needs deleting by hand.

Fast scratch (desktop `.env` `PS_INTEGRATION_SCRATCH_ROOT=C:/Astro/Scratch`;
blank = off): integrate / blend runs are built on the NVMe and, once a
master exists, the keepers (masters, finals, packet, ledger, manifest, logs,
scripts) move to the same run name under the staging root on D: and the
scratch folder (intermediates + staged frame copies, 250 to 350 GB for a
Piggy M31) is deleted. No master = the scratch folder is kept and the run
prints its size and the `Remove-Item` line to clear it. Too little free
space on C: = the run is built on D: as before, with a warning.
`photonscript scratch-status` lists what is left in scratch (and legacy
`--out` runs under C:/Astro/Staging) with size, age, master, archive twin and
the Remove-Item line; `--clean <name>` deletes one listed folder after a y/N.
`photonscript restage <archived run>` copies its staged frames back from the
Library (manifest frames[].source, sizes verified) into a new folder and
prints the PixInsight lines to rerun it.
Details: OSC_INTEGRATION.md "Fast scratch disk".

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
- 2026-10-07 (the first target held the RC16 all night, PS-180): the plan
  had M31 LRGB, NGC 604 and the Heart. Picked by priority, the Heart (60)
  took most of the budget, so M31 and NGC 604 were scaled to 1 or 2 subs
  per filter per pass; transit order then put M31 first, and its imaging
  loop "repeats while safe and up", so it shot all 281 subs and the other
  two never ran. Fixed: the planner gives every target a time window
  (`target_planner.allocate_windows`: in run order, a priority-weighted
  share of the time left among the targets that can still use it, capped
  by the goal hours still owed and the target's time above 30 deg, and by
  moonrise for a broadband-only target; no idle gap; a target that cannot
  get `plan_window_min_minutes` (30) is left out). Each target but the last
  hands the mount on at its window end: a TimeCondition at that local clock
  time on the imaging loop and the DSO container (a sub that would not fit
  is not started). Leftover time at the end of the night goes back to the
  highest-priority target still up: the last target's window grows, or a
  final "<target> fill (rest of the night)" container runs. Per-pass counts
  are fitted into the window (every filter is shot inside it). Windows show
  in `/api/nightplan` (`schedule[].window_start_utc`, `window_end_utc`,
  `handoff_utc`, `fill`) and on the dashboard plan. Kill switch:
  `plan_target_windows` (System page). A re-dispatch replans from now.
- 2026-10-07 (RC16 shutter open 5.4 h of 9.8 h, PS-176): M31 LRGB all
  night. 28 Run Autofocus items (118 min), one per filter block, because
  the planner's per-pass counts were L x3, R/G/B x1 and every block AFs
  (PS-65); 225 of 281 subs were the goal's 30 s HDR shorts (12 per R/G/B),
  re-shot on every pass because NINA resets a SmartExposure's
  LoopCondition when the imaging loop repeats; and each triggered AF left
  the next sub in L (NINA fires AF triggers after the SmartExposure's
  SwitchFilter: 15 L 30 s subs inside the R/G/B runs). Fixed: HDR shorts
  run once per visit ("<target> HDR shorts (once per visit)", before the
  loop); trigger runners switch back to the block's filter (lint rule
  `af-runner-filter`); `rc16_af_policy` smart (opt-in, default
  every_block) moves the focuser by the PS-76 model offset with no AF
  ("<target> filter block (focus by offset)", moves add up to zero, lint
  rule `focus-offset`), AF at target start, after `rc16_af_temp_change_c`,
  an `rc16_af_hfr_increase_pct` HFR rise, every `rc16_af_interval_min`,
  and at every block of a 3 nm filter whose offset has under 3 AFs; R/G/B
  default to `rc16_rgb_exposure_s` (120 s) on new goals. The night plan
  (`GET /api/nightplan` `efficiency`, dashboard "Est. Shutter Open")
  predicts the shutter share under both policies from the last nights'
  timelines (AF run, same-filter gap, filter change).
- 2026-10-06 (NINA #2 busy loop before nautical dawn, PS-149): after a pass
  NINA re-checks a loop's conditions with no next item (0 s) and, if they
  hold, resets and re-runs it. The companion's OSC_LIGHTS_UNTIL_DAWN
  [TimeCondition nautical dawn] held only conditioned containers, and in the
  last 30 s before dawn (a 30 s wait no longer fits) every one was a no-op,
  so it spun thousands of passes a second ("LoopWhileUnsafe /
  SafetyMonitorCondition finished" lines). RC16 sibling: SAFE_LOOP held until
  astro dawn while an all-narrowband LOOP_ALL_NIGHT ends at nautical dawn
  -10, so with its targets set it re-ran unpark / Pushover / park. The same
  night NINA #1 spun too ("TimeCondition finished", 04:19 to 05:56 local):
  M31 was LRGB only on a moon-down-at-dusk night, so every block sat in
  "<filter> until moonrise" and after the 04:17 moonrise its imaging loop
  held only no-ops; NGC 604 and the Heart never got a turn. Fixed: every
  repeating loop carries an unconditional WaitForTimeSpan (companion 30 s,
  SAFE_LOOP 60 s after holding to the loop end, an imaging loop with no AF
  60 s); an all-broadband moon-capped target ends at moonrise itself (DSO
  and imaging loop carry the moonrise TimeCondition), so the next target
  runs; the PS-144 dusk focus calibration runs its AF series once when
  targets follow (it repeated while safe and up, i.e. all night on a field
  that stays high); lint rule `loop-spin` (ERROR) flags any loop with no
  waiting item, in the armer and sideload gates; the PS-77 simulator now
  models NINA's reset decision, TimeCondition cut-off, wait estimates and
  clock-time conditions, and counts passes that consume no time.
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
- 2026-10-12 (moon rule, wrong night): from 18:02 local the planner logged
  "Skipping Andromeda Galaxy tonight: broadband-only and the moon defers
  broadband" on a BB night with a 7 % moon (17:57 still planned M31 then the
  Heart). moon_window_tonight() anchored to the UTC date, so after 18:00 MDT
  it described TOMORROW night (moon up at dusk), and night_moon() cached by a
  date label that the forecast spells as the local date, so that night got
  the 10-14/15 night's 20 % moon: up at dusk and >= 20 % defers broadband.
  Fixed: moon_window_tonight, the planner and the night plan anchor noon to
  noon local (`localtime.night_base_date`, same as `night_of`; after local
  midnight the planner had also planned the next night: "Dark window:
  2026-10-14 ..." at 00:04 local), night_moon caches by the dark window,
  and the skip line says why (illumination, altitude at dusk, rise / set).
  The rule itself is unchanged: broadband defers only with the moon up at
  dusk and at least 20 % lit.
- 2026-10-16 (moon windows): "Skipping Andromeda Galaxy tonight: ... moon
  37% lit, up at dusk (alt 24 deg), sets 22:54 local" for all three goals,
  then "Night plan: 0 targets". The plan is made at pre-config, before
  moonset, so the 1.5 h grace and the already-set rule never helped.
  Now a moon up at dusk that sets before dawn never defers the night
  (`moon.broadband_deferred`); `moon.broadband_from` is the moonset: the
  planner opens broadband-only windows there (the symmetric `up_from` of
  the PS-180 moonrise cap) and runs targets with narrowband first; the
  generator puts a WaitForTime at the local moonset before a broadband-only
  target slews, and wraps each broadband block of a mixed target in
  "<filter> after moonset" (WaitForTime first). The campaign and deep-night
  scores count only the post-moonset broadband share. Only a moon up all
  night (and at least 20 % lit) still defers broadband. No darks run
  during the wait (darks only run while unsafe).
- 2026-10-06 (cool stall, PS-154): a clear new-moon night lost >1 h. The
  Start-area Cool Camera item asked for -10 C (the NinaSequenceTarget
  camera_temp_c model default, carried in by the PS-144 dusk focus
  calibration target inserted first) while the config setpoint was 0 C and
  the armer's cooler nanny re-asserted 0 C every 30 s. NINA's CoolCamera
  (CameraVM.RegulateTemperature) waits until the sensor is within 1 C of its
  target with no timeout (its 2 min idle timeout counts only at <1 % or
  >99 % cooler power), so it never finished: no unpark, no tracking, no RC16
  lights, PHD2 "guiding" a parked mount, NINA #2 shooting a parked sky. Now
  every generated CoolCamera uses the rig's configured setpoint (a per-target
  value is logged and ignored) inside "COOL_CAMERA (bounded N min, then
  continue)", a run-once container whose TimeSpanCondition
  (`cooler_gate_timeout_min`, plus any ramp) interrupts it; lint rule
  `cooling` fails a CoolCamera off the setpoint by more than
  `cooler_gate_tolerance_c` or without that bound (WARN for a hand-built
  sideload). The NINA watch pages STUCK (one non-long instruction running
  `nina_watch_stuck_minutes`, 25) and NOT IMAGING (RC16 not tracked yet
  `nina_watch_parked_after_dusk_min`, 15, after dusk, roof safe); the cooler
  nanny pages instead of fighting a running sequence CoolCamera to another
  temperature; the guiding auto-recovery never starts PHD2 on a parked or
  idle mount.
- 2026-10-09 (mount left tracking, PS-185): a DISARMED night (a manual
  TPoint run) was never parked: at 06:04 local the mount was still
  unparked and tracking (az 354, alt 62) and TheSky then said "The mount
  cannot slew" until the Bisque control software was restarted. The
  armer's dawn shutdown only runs for an armed night. Now the dawn mount
  check (scheduler/dawn_mount_check.py, every 2 min, any armer state) from
  astro dawn + `dawn_mount_check_after_dawn_min` (20) to the noon roll-over:
  armer with a night (its own shutdown parks) or a NINA #1 sequence
  running = wait; else TheSky's read-only IsParked() / IsTracking (TCP
  3040, alt / az too), or NINA #1's mount info, unparked AND tracking =
  one priority page per night with the fix (TheSky: Telescope > Park; if
  "cannot slew", restart the Bisque control software) and a `dawn_mount`
  event (the latch across restarts). `dawn_mount_check_mode` alert
  (default) | off; it never parks.
- 2026-10-07 (false "park FAILED", PS-159): the 13:07Z dawn shutdown
  reported "guider stop FAILED ... park FAILED" because NINA #1's own End
  area had already parked (mount parked since the 09:24Z unsafe) and run
  DisconnectAllEquipment. The shutdown now reads state first: guider not
  connected / stopped = "guider stop ok (not connected)"; mount AtPark in
  NINA, or (NINA mount disconnected) TheSky's IsParked over TCP 3040
  read-only, or the last mount-log line = "park ok (already parked[,
  source])". A park call that fails is re-read before it counts. Real
  failures land in `shutdown.failed` and only those make the "Night
  complete" push priority 1. Make-safe trusts only NINA's AtPark (an abort
  still connects and parks). The generated End area already parks before
  DisconnectAllEquipment (a test pins the order).
- 2026-10-04 (blind night, PS-150): both NINAs were closed by 19:46 local
  (NINA #2 "requested by user" 19:31 after its safety driver threw "RPC
  server is unavailable"; NINA #1 closed and restarted twice, last closed
  19:45:52) and nothing started them until 06:10. The arm at 19:40 hit
  "ConnectError" and the nanny only pushed "Safety monitor DISCONNECTED"
  hourly. Not a hang, sleep or Windows update. Now the NINA watchdog
  (scheduler/nina_watchdog.py, GET /api/nina/watch, dashboard chip) says per
  rig NOT RUNNING / API DOWN / UP BUT SILENT, once per state per night, dusk
  to dawn. It never starts NINA: start it by hand on the scope PC.
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
- Scope-PC commands from the desktop: `photonscript remote -- <command>`
  (allowlisted reports and local data maintenance only; section 4, Remote CLI).
- Constraints: no GitHub pushes, no credentials, no AstroBin scraping for
  data tables, no writes into ninashare, scope deploys refused mid-night.

## 8. Current state (2026-09-26)

- DUAL-RIG IMAGING IS LIVE: both scopes shoot lights on one arm. RC16 (NINA #1,
  mono AP26MC, OAG->PHD2) and the piggyback 600 (NINA #2, OSC AP26CC, rides the
  mount unguided) both at setpoint 0C. The OSC unblock was the second Alpaca
  safety driver (see 2026-09-26 Night-ops lesson). Keep the OSC setpoint at 0C
  so it matches `piggyback_dark_exposures=120` darks.
- ONE PHD2 CONNECTION (2026-10-13): the RC16 telescope agent owns the PHD2
  client (connection, event loop, pixel scale, RMS). The Piggy-600 agent
  gets that client from the orchestrator through a read-only view
  (`phd2_client.ReadOnlyPHD2`): it reads the metrics and app state and
  registers listeners, but every control call (stop_capture, loop,
  guide_pulse, start_guiding, setters, connect) raises, and scheduler
  guiding commands are ignored by it. A Piggy-600 agent started alone
  opens its own connection, still read-only.
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
  hold (only once PHD2 has sat idle 10 s: NINA's PHD2 driver re-requests a
  calibration itself after its 300 s guide-start timeout, and that counts as
  the second attempt); a second FAIL keeps guiding on it and alerts once
  (`phd2_cal_fail_action=keep`), unless PHD2 is left with NO calibration (a
  failed one clears PHD2's): then the rest of the night falls back to
  unguided (subs capped, one push) and goes straight back to the target.
  A profile / binning change or an uncalibrated PHD2 mid-night triggers one
  re-dispatch with the slot (1 h of dark left), never a second slot for "no
  calibration" after tonight's slot failed. 2026-10-13: the slot's
  StartGuiding ate 20 min (3 x 300 s), the night stayed guided on no
  calibration, and the next PHD2 config change re-dispatched a second slot
  that cancelled M31's autofocus and slewed back to M15.
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
  NINA rows report only. Registry names were verified for reading against
  the scope PC's 2026-10-04 `reg export` (PS-119): the profile is read live
  (source order PHD2 API > PHD2 profile > guide log); nothing is writable
  yet (`phd2_profile_store.WRITABLE` is empty). `pe_owner=protrack`: PHD2
  PPEC off.
- GUIDE-STAR AUTO-TUNE (PS-90): the RC16 agent measures the guide star after
  each settle and filter change (peak % of full scale, clipped, SNR, HFD).
  `phd2_tune_mode=observe` (default) records only; `exposure` steps PHD2's
  exposure toward a 60 to 80% peak within 1 to 4 s (dark-library exposures
  only). Gain advice for the next night (also the audit's gain row) is
  written pre-dusk only behind `phd2_audit_autofix`; binning stays at 2 and
  the report says from the measured HFD whether bin 3 would hit 2 to 5 px.
  `GET /api/phd2/tuning`, Guiding tab "Guide Star Tuner", runs page line.
- PER-BLOCK GUIDING (PS-85): the OAG sees each filter, so a 3 nm block may
  have no real guide star (2026-10-05: PHD2 guided on noise, SNR 22 to 31).
  The RC16 agent checks PHD2's star at the start of each guided block (SNR,
  HFD, profile; read only) and guard D6 watches for SNR under the minimum
  with a non-star profile while guiding. A block without a real star runs
  unguided (TPoint + ProTrack) at the tracking-test length per filter (else
  L 60 s, NB 300 s), no dithers. `guide_block_mode=observe` (default)
  records and pushes once per target; `auto` re-dispatches. NB tuner band
  1 to 8 s. `GET /api/phd2/blocks`, Guiding tab "Per-block guiding".
- GUIDING THAT DOES NOT CORRECT (PS-155): 2026-10-06 PHD2 reported Guiding
  all night (RMS 17 to 42") with the star 5 to 160 px off the lock and every
  RA / Dec pulse 0 ms. Guide output was ON; PHD2's debug log shows the
  TheSky ASCOM driver failing IsSlewing and every PulseGuide ("pulseguide
  command failed", the PHD2 alert was silenced). Guard D7: `phd2_nocorr_frames`
  (20) guided frames in a row over `phd2_nocorr_px` (5) px off the lock with
  no pulse on either axis; D8: the star walking away from the lock position
  over `phd2_drift_window_min` (5) within one lock epoch. Both hold off for
  15 min while guide output is known off (the Guiding Assistant measures
  that way). One priority page per night naming the likely causes (driver
  refusing pulses, Max RA / Dec duration 0, guide output off, paused), run
  event `no_corrections`, guard episode kind `no_corrections` (counted on
  `GET /api/phd2/guard`; subs are not marked non-star). The morning analysis
  has finding `no_corrections` (and `guide_output_off` outside a GA run);
  the PS-89 audit has row `guide_output` (PHD2 API `get_guide_output_enabled`,
  else the guide log; Advanced Settings > Guiding > Shared Parameters).
- MOUNT DRIVER REFUSING PULSES (PS-167): 2026-10-05 22:55 to 10-07 every
  PHD2 pulse failed ("ScopeASCOM::IsSlewing failed: (ASCOM.SoftwareBisque.
  Telescope) Slewing", then "pulseguide command failed ... PulseGuide",
  [80020009]); SideOfPier and the coordinates failed too while the guide
  rate (a driver setting) still read, and NINA's own connection slewed and
  tracked normally. Cause: PHD2's copy of the in-process Bisque driver,
  connected 10-05 08:50 and never reconnected, lost TheSky when TheSky was
  reworked that day (two TheSky apps, PS-138); not a slew in progress. Fix:
  reconnect the mount in PHD2 (not a TheSky restart). Detection: the RC16
  agent tails PHD2's debug log (read only, every 30 s) and pages once per
  night after `phd2_pulse_refusal_min` (3) refusals with the exact error
  and the fix (run event `pulse_refused`); the morning analysis has finding
  `pulse_refused` from the night's debug logs (`pulse_refusal` block in
  `GET /api/phd2/analysis`); the PS-89 audit row `silenced_alerts` warns on
  any PHD2 alert ticked "don't show again" (registry `Confirm/<profile>`,
  e.g. `PulseGuideFailedAlertEnabled = 0`). Recovery is operator-run only:
  `photonscript guide-recover --dry-run` shows the readings (debug log,
  TheSky slew state over 3040, PHD2 state) and the steps (abort a slew TheSky
  reports while the mount stands still; PHD2 stop_capture, set_connected
  false / true, verify); without --dry-run it asks for "yes" and runs them.
- UNGUIDED FALLBACK (PS-156): when D7 / D8 fire, or PHD2 stays unlocked for
  `guide_fallback_after_min` (10) on a guided night, `guide_fallback_mode`
  decides once per night: `alert` (default) records run event
  `guide_fallback` and pushes what auto would do; `auto` runs the PS-91
  `fallback_unguided` (stop, guider stop, re-dispatch the remainder unguided:
  no StartGuiding, no dithers, Piggy-600 untouched) with every sub capped per
  filter at the tracking-test length (`guide_fallback_exposure_s` /
  `guide_fallback_test_date`, never over `unguided_max_exposure_s`). The night
  stays unguided until dawn; re-arm guided by hand to go back. A watched
  sideload is alert only.
- FOCUSER AT 0 (2026-10-08 to 10-11): the RC16 ZWO EAF dropped off USB
  about midday 10-08; NINA #1 kept "EAF(MainRC16)" Connected with Position
  0 (StepSize 600000, Temperature frozen at 21.29 C), autofocus started 55
  times and never finished, and every RC16 light had FOCPOS 0 at about 5"
  FWHM. Neither rig ever focuses at 0 (RC16 about 5600 to 6100, Piggy-600
  about 10900 to 11100). Now: a LIGHT whose FOCPOS is 0, or whose live
  focuser read was 0 at grading, fails scorecard row `focuser_zero`
  (reason code, `qa_focuser_zero_mode` fail | warn | info | off; calibration
  frames and a missing FOCPOS are never rejected); each rig's agent pushes
  once per night when its focuser reads 0 for a minute while imaging
  (`focuser_zero_alert`, run event `focuser_zero`); `photonscript integrate`
  leaves FOCPOS 0 lights out in every --qa mode (`focuser_zero N` in the
  group summary; `--keep-focuser-zero` overrides). Already captured subs:
  `photonscript qa-focuser-zero --since 2026-10-08 [--rig rc16]` lists them
  from the Library (read only) and this machine's subs logs; `--apply` ON
  THE SCOPE PC rejects them in the subs logs (row swap, human verdicts
  kept, Library links to `_rejected`).
- STALE GUIDE RMS (2026-10-12: every RC16 sub read 34.84", the last rolling
  sample, while PHD2 was stopped or LostLock): since 2026-10-13 a sub's
  `guide_rms` comes only from the guide frames inside its exposure and the
  record carries `guide_rms_frames`. Older records:
  `photonscript guide-rms-backfill --date D | --since D [--examples]` (dry
  run) and `--apply` ON THE SCOPE PC (where the PHD2 guide logs are)
  recompute each pre-fix sub's RMS from the night's guide log, or set none
  with `guide_rms_frames` 0 where PHD2 was not guiding (`guide_rms_src`
  phd2_log | not_guiding); the scorecard row follows, human verdicts kept.
- TEST FRAMES NEVER GRADED (2026-10-13: two 180 s NINA Imaging-tab
  snapshots, ProTrack off then on, are ImageType SNAPSHOT, not sequence
  lights, and pulling the raw FITS through ninaAPI timed out over
  Tailscale): `photonscript frame-check [--rig rc16|piggyback] [--last N |
  --index I ...] [--type SNAPSHOT] [--pa DEG] [--compare]` on the scope PC
  reads NINA's image history, finds the files under NINA's save path
  (`<FilePath>\<DATEMINUS12>\<IMAGETYPE>\`) and measures them like subs (HFR,
  FWHM", ecc, stars, background) plus the elongation axis and the trail
  (sqrt(12 (a^2 - b^2)) of the bright stars, arcsec and "/min); the axis is
  named RA / Dec when the camera PA is known (FITS WCS, the rig's newest
  plate solve, or --pa). `--compare` prints the verdict ("both round", "B
  trails along RA at 0.66"/min"). From the desktop: `--url
  http://100.94.189.77:8100` reads GET `/api/frame-check?rig=&last=` (small
  JSON); with no file and no --url it prints the command for the scope PC.
  ninaAPI has no star list or crop, only a stretched 8-bit preview.
- AUTOFOCUS NOT FINISHING (same incident: 55 AUTOFOCUS-STARTING, 54 points,
  no finish, no page for three nights): while a rig's NINA is imaging, its
  agent reads NINA's event history (`/event-history`, plus `last-af`) every
  2 min and pushes once per rig per night when `af_stall_runs` (3) AF runs
  in a row ended with fewer than `af_stall_min_points` (4) points or no
  completed fit (`af_stall_alert`, run event `af_stall`, a morning report
  line). AF runs under an unsafe roof (SAFETY-CHANGED) or ended by the
  sequence finishing do not count (scheduler/af_stall.py).
- NINA OFFLINE (2026-10-09 armed TPoint recipe night): NINA #1 dropped at
  about 02:42Z (PS-150 paged) and again at 03:07:58Z, and PS-150's once per rig and
  state per night latch kept the second outage silent; the Piggy-600 imaged
  an unknown field for 3.5 h. scheduler/nina_offline.py runs before PS-150
  in the same loop: while the armer is RUNNING / WATCHING or an arm recipe
  is dispatched (or was within 10 min when the outage began), a NINA API
  that does not answer for `nina_offline_grace_min` (3) gets one priority-1
  push per outage (what was running, the mount as last seen, "the Piggy is
  imaging an unknown field" when NINA #2 still exposes) and a recovery note;
  PS-150 keeps its chip and events for that rig but leaves the push to it
  (`nina_offline_alert`, run events `nina_offline`). Never restarts NINA.
  2026-10-12: PS-150's latch is now per outage: a rig ok again for
  `nina_watch_rearm_ok_min` (10) re-arms it, so a repeat outage on an
  unarmed night pages too (0 = the old once per night); a rig the offline
  watch owns is still only recorded, never double paged.
  Piggy guard (scheduler/piggy_guard.py, same loop, after PS-150): when
  NINA #2 takes lights while the RC16 mount log says parked / not tracking /
  offline / unknown for `piggy_guard_grace_min` (10), dusk to dawn, one push
  per night with what NINA #2 is shooting, run events `piggy_guard`
  (alert / clear) and a morning report line (`piggy_guard_mode` alert |
  panel | off). Independent of NINA #1 being up and of the armer. Never
  stops NINA #2.
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
- CALIBRATION CAPTURE + QA (PS-113): every BIAS / DARK / FLAT frame is QA'd
  before it may count or reach a master (sensor within SET-TEMP +/- 1 C,
  header, dark level vs the epoch's bias, center vs corner light leak, no
  stars, set outliers; flats: 20 to 80 % level, saturation, vignetting).
  With `calibration_qa_mode=quarantine` (default) a failing frame's Library
  link moves to `Calibration/_quarantine/<TYPE>/<date>/` with `reasons.json`;
  NINA's originals are never touched and the desktop scripts never read the
  quarantine. Readiness and the night dark quota count only QA-passed frames
  once a rig has a QA store (`<data_dir>/calibration_qa/`). Calibration page
  "Needs vs have" (needs from config, active goals, tonight's plan and the
  Library's lights), `photonscript calibration-plan`, `calibration-qa
  --backfill [--dry-run]`, and the guarded capture job `calibration-capture
  --rig R` (refused unless the armer is DISARMED / COMPLETE, the rig's NINA
  idle, the roof closed and PHD2 idle; cools to setpoint +/- 1 C first; stops
  on roof open, temperature drift, budget, arm, light in the frames; never
  touches the mount or the other rig's NINA). Daytime darks are compared
  with night ones; a leak disables daytime capture for that rig until
  `calibration-qa --rig R --reset-daytime`. `calibration_autofill` (daytime
  only) is off by default.
- CALIBRATION REACHES THE LIBRARY (PS-178): build_library(date) files only
  that night's own NINA folder, and build_library() only nights with a subs
  file, so a calibration-only folder (a daytime capture job, a roof-closed
  night, NINA restarted after midnight: the 2026-09-27 RC16 300 s darks,
  24 QA-passed) never reached the Library or the desktop. The dawn pass
  (PS-157 file_night) now ends with a calibration sweep
  (`runs.link_recent_calibration`: every calibration folder of the last 14
  days on both rigs, QA'd and quarantined as usual, idempotent) and
  build_library() without a date files those folders too. Read-only
  report: `GET /api/calibration/library-report?rig=` and `photonscript
  calibration-library [--rig R] [--url http://100.94.189.77:8100]
  [--desktop-library D:\ninashare\Library] [--frames N]` list every
  calibration frame as library / quarantine / not_filed with the reason,
  whether a dark / bias fits the lights' epoch (gain, offset, SET-TEMP vs
  setpoint, readout), and with --url on the desktop how many Library frames
  the mirror still lacks (Syncthing pending). On the desktop `photonscript
  integrate` lists the bias sessions that miss the epoch and, for a light
  length with no dark, the nearest alternatives; it refuses that length
  unless `--allow-uncalibrated`.
- CALIBRATION COMPLETENESS (PS-182): `GET /api/calibration/completeness?rig=`
  and `photonscript calibration-status [--rig R] [--url
  http://100.94.189.77:8100] [--mirror D:\ninashare\Library] [--nights N]`
  (exit 1 when anything is missing). One model per rig: every light set
  (target, filter, exposure, gain, offset, SET-TEMP, readout, binning) of
  the last `calibration_completeness_nights` (14) nights, every goal, plus
  tonight's plan; for each its bias, darks and flats through captured ->
  QA-passed (quota) -> in the Library on the scope -> on the desktop (from
  the scope: the Syncthing remoteneed answer; on the desktop with --url: the
  mirror itself), with the reason at the first stage that is short. Also the
  darks windows, the deferred dawn plan, the bias plan and a light-tightness
  check of the recent darks (the Piggy-600 has no wheel or dark slide: a
  warn says cap the lens or keep its darks roof-closed at night). The
  morning report card and the dawn push carry "Calibration: complete for
  <targets> / missing <...>". One rule for "usable"
  (`calibration_qa.usable_misses`): gain, offset, SET-TEMP within 1.5 C,
  readout; the owed view, the night quota, the bias age gate, the library
  report and this model all use it. 2026-10-08 finding: the owed view
  called the 2026-07-31 RC16 bias usable (gain 200, offset 256, SET-TEMP 0:
  on epoch except readout) because its QA record has no readout and was
  assumed HCG; its header says LCG (HARDWARE.md), so integrate on the
  desktop rejected it. The dawn sweep now header-reads READOUTM for QA
  records without one (`calibration_readout_fill_at_dawn`, 400 a dawn), and
  `camera_readout_since` (RC16: set 2026-09-26) stops assuming HCG for any
  older frame that still has none. Passed bias also has to match SET-TEMP
  now (the 09-30 bias at 20 C).
- FILE CALIBRATION NOW (PS-188): 2026-10-10 the completeness and windows
  endpoints answered HTTP 500: a focuser with no temperature probe reports
  NaN, the cooler history stored it, and Starlette refuses NaN in JSON. The
  history now drops non-finite values (old NaN rows read as None) and both
  endpoints answer with a per-rig "error" field instead of a 500. The
  calibration sweep used to run only in the dawn pass, so a disarmed night
  (10-09, manual TPoint) filed nothing. `POST /api/calibration/file`
  {"rig", "dry_run", "rejudge", "regrade", "days"} and `photonscript
  calibration-file --url http://100.94.189.77:8100 [--dry-run] [--rejudge]
  [--regrade] [--days N]` (exit 1 when anything is left) file both rigs'
  calibration folders of the last 14 days now and say what was filed,
  quarantined and left, and why (QA pending, quarantined because X, older
  verdict that passes today, folder outside the window with the --days to
  use); `GET /api/calibration/file` is the last report. --rejudge judges
  the whole QA store by today's rule and moves filed frames to match (the
  pre-PS-182 HCG darks back out of the quarantine). With
  `calibration_file_auto` (default on) the same filing runs at arm time,
  when a night ends without a dawn pass and after a capture job; while the
  armer images, new frames are not measured (left "QA pending" for dawn).
  The completeness requirements carry a concrete reason, an `action` and
  the not-filed frames grouped by reason. Piggy-600 darks:
  `piggyback_dark_capture_window` unsafe-night (default, today: the
  companion's OSC_DARKS_IF_UNSAFE behind the cooler gate) | dawn-roof-closed
  (also saved as the Piggy-600 dawn plan at each filing: lens cap on) |
  manual (no companion darks).
- DARKS THAT WORK (PS-182): (a) QA: the dark-over-bias allowance's dark
  current term scales with the readout's ADU per electron
  (`calibration_qa_dark_scale_by_readout`; RC16 HCG 0.79 / 0.25 = 3.16 x).
  The limit was measured on LCG darks, so good HCG 600 s darks (28 to 46 ADU
  over bias on 10-05 / 10-06) were quarantined as "level". The next QA pass
  re-judges them and the dawn sweep files the last 14 days' frames that now
  pass (not `calibration-qa --restore`: that undoes every quarantine, real
  light leaks included).
  (b) Cooler history (`cooler_history.py`, data_dir/cooler_history, one
  sample per rig every `cooler_history_sample_s`, from the telescope agent's
  NINA poll and every capture job read): sensor, setpoint, TEC power,
  focuser temperature as the ambient proxy. The model: power at the
  setpoint ~ k x (ambient - setpoint) (2026-10-08: 62 % at 33 C, k ~ 1.9
  %/C) plus the ambients where the TEC ran flat out above the setpoint;
  per local hour the typical ambient. (c) A daytime capture job is refused
  at once when the TEC already reads flat out above the setpoint or the
  model predicts more than `cooler_reach_max_power_pct` (90) at the live
  ambient, and a cooling job aborts after `calibration_cool_stall_min` (6)
  when the sensor stopped falling (< 0.5 C in 3 min) above the setpoint,
  instead of holding the rig (`calibration_cool_reach` refuse | warn |
  off). Either way the plan is kept as the rig's deferred dawn plan
  (data_dir/calibration_deferred.json; dawn = astro dawn + 30 min to astro
  dawn + `calibration_dawn_window_min`), which `calibration_dawn_capture`
  (default off) starts by itself; `GET /api/calibration/windows?rig=`
  lists now / pre-config cool-down / night (roof closed) / dawn with the
  prediction. (d) Bias: when a rig has no usable bias the night sequence
  shoots BIAS_AT_SETPOINT (RC16 start area, Piggy-600 companion first
  target item): the cooler gate, then 50 x 1 ms at the rig's gain / offset,
  any roof (`calibration_bias_when_missing`).
- BIAS ONLY + BIAS DRIFT (2026-10-16): on 10-15 (roof closed all night)
  the Piggy-600 450 s darks read 294 to 298 ADU and failed "level" against
  the 2026-09-12 bias (256: 36 to 42 over, 19 allowed); both jobs aborted
  after 2 frames. A job posted with `"exposures": [], "bias": 30` still
  shot the owed darks (empty exposures = owed) and shot them before the
  bias, so light (no shutter) vs a drifted bias was never decided. Now:
  (a) bias only: `POST /api/calibration/capture-job {"rig": "piggyback",
  "bias": 30, "darks": false}` (or `"bias_only": true`; `photonscript
  calibration-capture --rig piggyback --bias-only 30`, local CLI only, not
  on the remote allowlist) shoots only the bias, by day and whatever the
  roof reads (0 s), still refused while the armer is ARMED / RUNNING or
  NINA is busy. `"exposures": []` without `"darks": false` still means the
  owed darks. (b) Every job shoots the bias FIRST. (c) Dark QA judges each
  dark against the QA-clean bias session in force at its date (SET-TEMP
  matched; was: the newest bias, which would have failed every September
  dark "below bias" once a drifted bias landed), and the reason names it:
  "over bias 256 (bias 2026-09-12, median 256 of 30 frames)". (d) Drift:
  the job compares its new bias with the previous session (job
  `bias_drift`, `GET /api/calibration/bias-drift?rig=`) and pushes when it
  moved more than `calibration_bias_drift_adu` (8). A dark that fails the
  level check against its older bias but sits inside the allowance over a
  newer drifted bias gets a `bias_drift` warning ("bias drift, not light",
  `level_verdict`), passes and does not count toward the light abort;
  otherwise the verdict is "light". With the 10-15 numbers: a new bias at
  290 makes those darks bias drift, one at 257 leaves them light (then cap
  the lens or check the box for a leak). `calibration-file --rejudge` files
  darks the new verdict passes.
- LONG DARKS, THE DARK MODEL: the generic dark limit (10 ADU + 0.02 ADU/s at
  0 C, 19 ADU at 450 s) is replaced, once a camera has accepted darks, by a
  per-camera model (calibration_qa.dark_expectation, per gain / offset /
  readout / SET-TEMP): the expected excess over bias for a length is the
  newest 3 earlier nights of that length, else interpolated between known
  lengths (bias = 0 s), else extrapolated past the longest along the top
  segment; the limit adds `calibration_dark_model_allow_adu` (5) +
  `calibration_dark_model_allow_frac` (0.25) x expected. Dark sets are judged
  in date order; a set with 3+ fully passing frames anchors later nights
  (store `dark_model` rows). Light is judged inside the set: `trend` (level
  moves > 3 ADU through the set, DATE-OBS order, sun altitude quoted),
  `gradient` (a frame's corner-minus-center pattern off its set's by > 3
  ADU), `outlier`; a `leak` pattern every frame of the set shares (within
  1.5 ADU, up to 3 + 0.25 x expected) becomes a `zone_pattern` warning. Bias
  drift vs light is unchanged. `calibration_dark_model=false` restores the
  formula. Read-only check of one quarantine folder: `photonscript
  calibration-release-check --rig piggyback --dir <lib>/Calibration/
  _quarantine/DARK/<date>` (measures that folder plus up to 6 frames per
  other set, writes nothing; releasing stays an operator step on the scope).
  2026-10-16 run: the 450 s darks (+47, uniform, no trend, fixed zone
  pattern) still fail, against the accepted 2026-10-11 450 s set at +19
  (limit 28.8).
- CALIBRATION OWED (PS-122): `GET /api/calibration/owed?rig=`, the Calibration
  page "Calibration owed" cards and `photonscript calibration-owed` list, per
  rig, the frames still needed for the lights of active goals over the last
  `calibration_owed_lookback_days` (60) nights plus tonight's plan: darks per
  (exposure, gain, offset, set temp) with have / need, flats per filter (last
  set, count, age vs 45 d, lights since), bias, uncalibrated nights, and a
  config fix when a light length is missing from the night quota list (e.g.
  `PS_PIGGYBACK_DARK_EXPOSURES=120,300,400`). Darks are counted by
  `calibration.dark_quota`, the same function the RC16 unsafe darks and the
  Piggy-600 companion size their blocks with (QA-passed only once a rig has a
  QA store). Lights logged since PS-122 carry gain / offset / xbin / readout.
- CALIBRATION COVERAGE (PS-160): the owed view now drives the night. The
  on-epoch lengths the lights used that the config dark list lacks join the
  night quota (`calibration.night_dark_exposures`: RC16 unsafe darks and the
  Piggy-600 companion; `calibration_darks_follow_lights`, at most
  `calibration_darks_follow_lights_max` 2 per rig and night, most used
  first), so the Piggy-600's 300 s lights get 300 s darks without a config
  edit. Night darks sit behind the PS-61 cooler gate
  (`calibration_darks_gated`): in skip mode a sensor that never reached the
  setpoint (a PS-154 bounded cool that timed out) skips the darks instead of
  filing off-setpoint frames (RC16: a DARKS_AT_SETPOINT container inside the
  unsafe branch; Piggy-600: first item of OSC_DARKS). RC16 stale-flat
  reshoots (dawn `auto_stale_flats`, dusk flats) only for filters the lights
  used (`calibration_flats_as_used`; every stale filter while no lights are
  logged), missing first then oldest, at most
  `calibration_dawn_flat_extra_max` (3) on top of tonight's per dawn.
  `calibration_flats_reset` ("rc16:YYYY-MM-DD,piggyback:YYYY-MM-DD") marks
  flats older than an optics change (rotation, spacer / focus change,
  cleaning) owed. Each dark row shows the newest matching dark (age) and the
  median CCD-TEMP of the counted darks; each rig card has a "Plan" block;
  the dawn "Night complete" push carries one "Calibration owed:" line. The
  Piggy-600 still shoots one OSC flat set every safe dawn (companion).
- READOUT MODE (PS-128): darks and bias count only at the lights' camera
  readout mode (AP26MC HCG: 0.25 e-/ADU, RN 5.66 ADU; LCG: 0.79 e-/ADU,
  RN 4.27 ADU). `camera_readout_mode` (RC16, default HCG: every RC16 light
  since 2026-09-26) and `piggyback_readout_mode` (default LCG); blank = not
  matched (pre-PS-128). The mode comes from the FITS READOUTM keyword (NINA
  writes "High Conversion Gain" / "Low Conversion Gain"; READMODE / READOUT
  also read); a frame without one is assumed to be at the rig's mode and the
  owed view says how many were assumed. QA records store it (`readout`);
  `calibration-qa --backfill --dry-run` fills it on older records from a
  header-only read (no re-measure, no moves). The night quota (RC16 unsafe
  darks, Piggy-600 companion), the roof-closed bias age gate, the owed view,
  the gap report and readiness all count at that mode, so the July LCG bias
  and LCG 600 s darks no longer satisfy the HCG lights. NINA has no
  per-exposure readout field in the generated sequences: every LIGHT / DARK
  / BIAS is shot at the profile's "readout mode for sequence images"
  (Options > Equipment > Camera), so the RISK is that profile setting
  changing between the lights and their darks. The capture job refuses when
  ninaAPI reports a different mode, QA fails a capture-job frame whose
  READOUTM differs from the asked mode, and the lint rule `readout` errors
  when a (hand-edited) sequence's "Set readout mode" instructions put
  darks / bias at another mode than the lights.
- Mosaic planner at `/mosaic`: panel grid over a DSS2 hips2fits cutout; since
  PS-111 one mosaic goal made of in-order panel goals (section 4).
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
  TPoint session: run binning, catalogs and polar error are not scriptable;
  PS-138: model on, points, RMS, IH / ID and ProTrack are read live from
  TheSky first, the record is only an info fallback "manual (date)", never a
  pass). PS-138 also adds "First slew vs model index terms" (TPoint
  Recalibrate instead of a rebuild when the first slews miss by the model's
  IH / ID), "ProTrack OFF" on the Guiding tab and an unguided arm warning,
  and "One TheSky running" (`scheduler/thesky_procs.py`: the Bisque sky apps
  in this PC's process list, the TCP 3040 listener and the TheSky the Bisque
  ASCOM driver targets per the registry; read only, never closes or starts
  anything; fail and first on the Guiding tab when two run). The ProTrack
  row is not judged while TheSky's mount is parked (the arm-time audit runs
  by day); the nina watch loop re-judges just that row once a night when the
  RC16 mount first tracks, or at its first light (`scheduler/protrack_recheck.py`,
  read-only mount_flags + tpoint_flags), puts it in the night's audit
  (`rechecks`, the row replaced) and pushes once when ProTrack is OFF on an
  unguided night. Runs at arm (no push) and on Refresh; Guiding tab section
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
- GUIDING "WHAT TO CHANGE" (PS-119): the top of `/guiding` lists every fail,
  then every warn, from the cached PHD2 and TheSky audits, the PS-93
  calibration, the PS-92 self-test, the PS-91 guard / hot-pixel map and the
  PS-90 tuner (scheduler/guiding_attention.py, `GET /api/guiding/attention`,
  CLI `photonscript guiding-status [--json]`, one System page line). Each
  line: current (source and age) -> desired, the fix, where (PHD2 / NINA /
  TheSky / mount driver / PhotonScript), and Apply (dry run, then confirm)
  only where the audit can set it live. A guide-log reading older than the
  last PHD2 ConfigurationChange or 12 h is "stale: may already be fixed" in
  its own group; unknown rows are grouped by the one action that clears
  them. Passing rows fold behind "N passing" per section; the nav shows
  fail / warn counts. Never runs an audit itself. With it: the search region
  must cover 1 x NINA's dither (not 2 x), a Dec min-move row (FAIL over
  5 px), the stored calibration (profile scope/calibration) graded by PS-93,
  TheSky's site longitude is strict and a time zone / DST row. PS-120: the
  scripted longitude reads +109.021 for both E and W, so the row checks its
  magnitude and takes E / W from TheSky's own sidereal time (unknown, verify
  by eye, when that read is missing); DST index 17 = U.S. and Canada;
  first slews count only after the TPoint model; run binning 1 or 2; All
  Sky off is fine with a catalog solve; polar limit 3'. 2026-10-12: a
  zone that disagrees with the PC is a WARN (not FAIL) when TheSky's
  Julian date and LST are right (its UT comes from the computer clock), and
  a TPoint model file newer than the manual record moves the first-slew
  cutoff and the model age.
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
- `/api/safety/night?date=DATE` (PS-1, also the runs page "Safety monitor"
  section) -> both NINAs' safety-monitor timelines from their logs. A change
  both NINAs log within 60 s is AARO's own state (weather or roof logic), not
  a driver. "Unsafe" logged right before "Disconnected" is a connection
  loss, not weather. A change on one NINA while the other stays connected in
  the opposite state is a suspect. `/api/safety/summary?days=14` gives one row
  per night. The armer's live cross-check (`safety_crosscheck`: off | log |
  alert, default log) reads NINA #2 once at each unsafe onset and writes
  `src: safety, kind: crosscheck` to `runs/<night>_events.jsonl`. It never
  changes the pause or park.
- `/api/preflight` (POST) -> config, directories, equipment, disk. The "NINA
  image directory" check catches a missing capture folder. (Sends a Pushover
  test as a side-effect.)
- `/api/calibration/health` (staleness), `/api/sync` (transfer backlog).
- `/api/nina/log?lines=N&grep=term1|term2` -> PS-172: the log endpoints
  (nina, phd2, ascom) run in a worker thread and read each file BACKWARDS from
  its end, returning the last min(N,5000) matching rows. A grep looks back at
  most scan_mb (default 64 MB) per page; the header says when it stopped
  short ("scan_mb= to look further"). offset=K skips the newest K matching
  rows, so the header's "older: offset=..." pages back past 5000 lines.
  download=1 streams one whole log (file= or the newest) up to 300 MB; a
  bigger file gets HTTP 413 (page it instead). The night bundle
  `/api/runs/DATE/bundle` (zip) is still there for everything. Note the log
  message body can wrap onto
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
- **Piggy-600 SkyFlat validation crash** (2026-10-05, PS-132): NINA #2 logged
  `InvalidOperationException: Sequence contains no matching element` at
  `SkyFlat.GetSwitchFilterItem()` from `SkyFlat.Validate()` every 5 s while
  the companion was loaded; Start did nothing (twice). NINA's SkyFlat finds
  its filter with `Items.First(x is SwitchFilter)`, and the OSC flat had no
  SwitchFilter (the Piggy-600 has no wheel). Fix (generator): the OSC SkyFlat
  carries a SwitchFilter with `Filter: null`, which NINA validates clean
  without a wheel (SwitchFilter checks the wheel only when a filter is set)
  and which SkyFlat passes as "no filter" to its captures, so NINA's own
  auto-exposure (50 % histogram) still runs. Guards: the `flat-filter` lint
  rule (any flat instruction without its SwitchFilter, any rig) and
  `no-filter-wheel` (a filter selected on a rig without a wheel,
  `rigs.rig_has_filter_wheel`), in the night lint, the companion lint
  (sideload) and before the armer dispatches the companion (a lint error
  there = no dispatch + Pushover). And load validation (below).
- **Center inside the Piggy-600 loop** (2026-10-03, PS-139): a NINA #2 M31
  sequence built from NINA's stock deep-sky template centered before every
  sub (slew, solve, refused sync, ~58' offset slew, solve). See "A safe
  hand-built NINA #2 sequence" above; lint rule `piggy-mount` and the arm /
  watch check of NINA #2's loaded sequence catch it.
- **Load validation** (PS-132): NINA refuses a manual Start silently when a
  validator throws. After every ninaAPI `/sequence/load` (sideload, and
  `rigs.nina_dispatch`: Piggy companion, Piggy dusk flats, calibration
  capture) PhotonScript waits `nina_load_validation_settle_s` (8 s), reads
  the `Issues` lists in `GET /sequence/state` and the ERROR blocks naming a
  Validate in that rig's NINA log since the load (a validator that throws
  never sets Issues), and pushes one "PhotonScript NINA validation" alert.
  Sideload: a Validate error makes the response 422 / ok false ("NINA
  REJECTS it"), Issues alone are reported with ok true. Dispatch:
  `nina_load_validation=alert` (default) starts anyway (skipValidation, as
  before) and says so; `refuse` skips the Start on a Validate error; `off`
  skips the check. The RC16 armer's own dispatch is not covered.
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
