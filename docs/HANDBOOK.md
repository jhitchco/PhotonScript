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
Refused with 409 while RUNNING, PAUSED_UNSAFE or PAUSED_OPERATOR (PS-64), while a grading job writes,
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
- Keep the companion's guards: CoolCamera at or below 0 C, a
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
met or enough new data arrived (PS-31). PS-142: each goal shows a campaign
status chip (Acquiring / Ready to process / Processing / Processed (vN) /
Published (vN)) and the target page a review panel (verdict, notes, asks with
Approve / Decline; a plan change shows its diff and goes through PATCH
/api/projects2 only after a confirm); a new ledger version pings Pushover;
`photonscript ledger-import` brings in the pre-PS-33 history. OSC_INTEGRATION.md
section 0b.

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
  anything; fail and first on the Guiding tab when two run). Runs at arm (no push) and on Refresh; Guiding tab section
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
  Sky off is fine with a catalog solve; polar limit 3'.
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
