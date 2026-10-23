# TPoint autocal (operator-started, supervised)

Status (2026-10-20): LOOP MODE RAN ON HARDWARE (slews, camera hand-off,
exposures OK; AutoMap failed). The default add is now the TPoint import
file (next section), untested on hardware.
`photonscript tpoint-autocal --go --mode loop` (the default mode) runs the
loop below with TheSky's own camera. Window mode `--go` is still not
implemented. `--dry-run` prints the preflight, the chosen points and every
TheSky script and sends nothing. Code:
`photonscript/telescope_agent/tpoint_autocal.py` (pure: preflight, backup
plan, script builders, dry-run plan) and
`photonscript/telescope_agent/tpoint_autocal_live.py` (point selection and
the live loop, every outside call through a small adapter). Tests:
`tests/test_scheduler/test_tpoint_autocal.py`,
`tests/test_scheduler/test_tpoint_autocal_loop.py`,
`tests/test_scheduler/test_tpoint_autocal_import.py` (fakes only, plus the
format checks against the real in.dat fixtures).

## 2026-10-20: AutoMap failed, points now go to a TPoint import file

First hardware run (`photonscript tpoint-autocal --go --unattended --side
west --points 3`): preflight, the camera hand-off (NINA #1 -> TheSky ASCOM
Camera -> back), `SlewToAzAlt` and `TakeImage` all worked (frames
`00000983.tpoint autocal 1.fit`, `00000984.tpoint autocal 2.fit` in
TheSky's Camera AutoSave). `sky6RASCOMTheSky.AutoMap()` failed both times:
ImageLinkResults succeeded 0, "Process aborted. Error _ 212.", AutoMap's
own "requires the TheSky5 Level IV or TheSky6/TheSkyX Professional Edition
and it must be running. Error _ 224." (TheSky Professional 64, build
14139); TPoint stayed at 561 samples. (2026-10-09, PS-187:
`TheSkyXAction` AddPointingSample did not add points either.)

So the default add no longer depends on AutoMap (`--automap` keeps the old
path, off by default). Per point, after the exposure:

1. **Mount** (read only, right after `TakeImage` starts):
   `tpoint_autocal.mount_snapshot_script()`: `GetRaDec` (JNow, as TheSky
   reports it), `GetAzAlt`, `sky6Utils.ComputeLocalSiderealTime`, the JD.
   Pier side: from the hour angle (LST - mount RA: HA < 0 = east-looking,
   written through the pole, as in every real file); a `PIERSIDE` FITS card
   is recorded in the CSV when TheSky writes one.
2. **Solve** (`--solver`): `imagelink` (default) runs
   `tpoint_autocal.imagelink_solve_script(path, scale)`, its own script:
   `ImageLink.pathToFITS` / `scale` / `execute()` (thesky_client's
   `imagelink_pre`, the only properties a script may assign), then
   `ImageLinkResults` succeeded, errorCode, errorText, imageCenterRAJ2000 /
   DecJ2000, imageScale, imagePositionAngle, imageStarCount, solutionRMS.
   Its error goes to the CSV (`imagelink_err`, `imagelink_code`). `astap`
   solves with ASTAP on the scope PC (`astap_exe`, through
   `solve_store.solve`: never rewrites the FITS, `-fov` from the frame
   height, the mount position as the hint, 10 deg radius). `fallback`
   tries ImageLink, then ASTAP.
3. **Append** one line to the TPoint import file
   `C:\astro\tpoint_import\<UTC stamp>_<side>.dat` (`--import-dir`; refused
   inside TheSky's folders or ninashare). Stop after 3 points in a row not
   solved or not recorded.

Then, by hand: TheSky TPoint, Module Settings > Import Pointing Data, pick
the file, refit (Super Model), check the RMS, save, Apply corrections /
ProTrack; `photonscript tpoint-refit` can wrap the backup / judge.

**Import file format** (`photonscript/telescope_agent/tpoint_import.py`),
matched byte for byte to the real TheSky64 file
`tests/test_scheduler/fixtures/tpoint/2026-10-15/TPoint base run in.dat`
(a test regenerates that file and the 2026-10-14 one from their parsed
values; `tpoint_files.parse_data` and `tpoint_coverage.parse` read the
generated file back with the same count and values):

```
!TheSky Version 10.5.0 Build 14139 (64 bit)
Paramount MX
:NODA
:EQUAT
+31 54 25 2026 10 20 29.31 870.00 1300.00  0.50 0.5500 0.0065
20 07 14.7056 +15 04 48.851 20 06 07.2000 +14 58 12.000  22  6.000 
```

Comment line, caption and options copied from the newest real in.dat; the
site line is latitude d m s, the local date, then temperature, pressure,
height, humidity, wavelength, lapse rate copied from that file, except
that temperature, pressure and humidity are the observatory weather (NINA
#1's weather device, outside air temperature; NaN readings ignored) at
collection time when it reads: no station pressure (or a sea level one) =
870 mB (AARO ~1250 m). The source is in the run's output, the CSV column
site_source and the summary (2026-10-23: the copied 29.31 C was not the
night's air, TheSky's own dialog showed a 38.31 C dome sensor). One line
per sample: true RA (h m s.ssss), true Dec (d m s.sss), observed RA,
observed Dec, two spaces, sidereal time (h m.mmm, minutes right-aligned in
6), a trailing space. CRLF, no END line. True = the solved J2000 centre as
the apparent place of the date (erfa); observed = the mount's reading, on
the east-looking side in TPOINT's through-the-pole form (RA + 12 h, Dec
180 - Dec).

Caveat (HANDBOOK, "Hand import"): TheSky reports the mount AFTER any
TPoint pointing corrections. With "Apply pointing corrections" on, the
lines measure the current model's residuals, not raw pointing errors.
Check what TPoint shows after the import of the 3-point test before a long
run.

## Commands

Run them on the scope PC, in a console Jeremy watches (they ask
questions; Ctrl+C stops safely).

| What | Command |
|---|---|
| See the points, send nothing | `photonscript tpoint-autocal --side west --points 20 --dry-run --no-scripts` |
| Supervised 3-point first test | `photonscript tpoint-autocal --go --test` |
| West-side top-up | `photonscript tpoint-autocal --go --side west --points 20 --solver fallback` |
| Both sides | `photonscript tpoint-autocal --go --points 40` |
| Remote 3-point import test | `photonscript remote --confirm-write --remote-timeout 900 -- tpoint-autocal --go --unattended --side west --points 3 --solver fallback --verbose` |
| The old AutoMap add | add `--automap` |

Options: `--side east|west|both` (default both), `--points N` (1..500),
`--from-coverage` (points from a TPoint coverage module when the build has
one; today it falls back to the grid + `--side`, and says so),
`--exposure-s` (default: TheSky's Automated Image Link exposure, else
`tpoint_mapping_exposure_s`), `--binning` (default
`tpoint_mapping_binning` 2), `--slew-timeout` (180 s), `--verbose` (every
TheSky script call and reply), `--url` (the scheduler, for the GET
preflight), `--json` (the summary as JSON at the end), `--solver
imagelink|astap|fallback`, `--import-dir` (local only, not remote),
`--automap`. `--test` = `--points 3 --side west --verbose` (with
`--automap`: plus a pause after the first solved point to look at TPoint's
pointing data). Steps 5 to 8 below describe the `--automap` path; the
default import path is the section above.

### What `--go --mode loop` does

1. **Points.** `tpoint_mapping.plan_points` (equal-area grid above
   `tpoint_mapping_min_alt`, meridian band, under-pole and moon skipped),
   filtered to `--side` (and, for east / west, at least 0.5 h from the
   meridian so the mount cannot pick the other pier side), thinned evenly
   to N, walked serpentine (hour-angle strips, Dec up / down, east before
   west). Each point gets a slot time (now + i x ~40 s: 5 s exposure + 35 s
   overhead, plus 90 s for the pier flip between halves). The mount slews
   to a fixed alt / az and then tracks, so a point is dropped when its
   tracked position would set below min alt, or (east) drift into the
   meridian band, before its slot ends; slots after astronomical dawn are
   dropped. The list (with UTC times) is printed first.
2. **Preflight** (refuses on any fail or unknown): GET `/api/arm` DISARMED,
   GET `/api/apps/status` (both NINAs no RUNNING item, no calibration
   capture job, TheSky answering with the mount connected, sun <= -12),
   NINA #1 safety monitor IsSafe, `PS_AUTO_ARM_ENABLED` false, TPoint files
   found to back up.
3. **Backup.** Copies every TPoint file to
   `C:\astro\tpoint_backup\<UTC stamp>\`, reads the TPoint input-data
   row count (`in.dat` / `Indat.dat` files), then asks Jeremy to note
   TPoint's point count, save unsaved points, check TheSky's Camera Setup
   and PHD2, and type `yes`. Anything else: nothing is sent.
4. **Lend the camera.** NINA #1 sequence idle again, camera info recorded
   (cooler, setpoint), `/equipment/camera/disconnect`, wait for Connected
   false; TheSky reads its selected camera, `ccdsoftCamera.Connect()`.
5. **Setup.** TheSky camera exposure / bin / light / autosave / async;
   Automated Image Link scale (`pixel_scale_arcsec` x bin) and exposure.
6. **Per point.** Re-check: armer DISARMED, roof safe, NINA #1 idle (else
   stop). `SlewToAzAlt` async, poll `IsSlewComplete` (timeout = stop),
   settle 2 s, `TakeImage` async, poll `IsExposureComplete` (exposure +
   120 s), `AutoMap()`, read the TPoint input-data count again, one CSV
   row in `runs/<night>_tpoint_autocal.csv`, an ETA line.
7. **Confirm AutoMap adds.** After the first solved point: if the file
   count went up, AutoMap adds points (`--test` asks Jeremy to confirm
   anyway). If it did not (TPoint may only write the file on save) Jeremy
   looks at TPoint's pointing data: yes = go on (later points rely on
   AutoMap's solve), no = stop, "AutoMap did not add a sample".
8. **Stop rules.** 2 AutoMap failures in a row (not solved, or solved with
   no sample while the file count is live), slew or exposure timeout, a
   TheSky error, the armer leaving DISARMED, the roof unsafe, NINA #1
   running, Ctrl+C. Abort (`ccdsoftCamera.Abort()` + `sky6RASCOMTele.Abort()`)
   is sent only while a slew or exposure may be in flight.
9. **Hand-back (always).** TheSky `ccdsoftCamera.Disconnect()`, NINA #1
   `/equipment/camera/connect`, wait for Connected true, cooler re-applied
   at the old setpoint if it was on. Not connected: a loud console block
   and a Pushover, no retry loop.
10. **Summary.** Points attempted / solved / failed, samples confirmed in
    the file (or by eye), time, the in.dat count before / after, whether
    the camera is back, and the next step: refit the Super Model in TPoint
    by hand, save, Apply corrections / ProTrack, `photonscript
    tpoint-stats`. `photonscript tpoint-refit --go` wraps the refit (backup,
    read back, judge, restore a bad fit): docs/TPOINT_REFIT.md.

Never arms, never dispatches, never loads or starts a NINA sequence, never
syncs (no ClosedLoopSlew), never parks, never changes tracking.

### Known unknowns (the 3-point test answers them)

- Does `sky6RASCOMTheSky.AutoMap()` add a TPoint sample on build 14139
  (and without a dialog)? The whole loop depends on it.
- Does NINA #1 release the AP26MC so TheSky's Connect works (ASCOM vs
  native driver), and does NINA reconnect cleanly (cooler state)?
- Does TPoint write `in.dat` / `Indat.dat` as samples are added, or only on
  save? (If only on save, step 7's operator check carries the run.)
- `SlewToAzAlt` pier side for west points near 0.5 h HA; `IsSlewComplete`
  and `IsExposureComplete` read as 1 / 0 or true / false over TCP (both
  handled).
- The TCP call for AutoMap blocks for the Image Link solve: the client
  timeout is raised to 60 s for the run; a slower solve reads as a TheSky
  error and stops the run.
- No scheduler-wide "camera lent to TheSky" lock yet: preflight refuses
  with auto-arm on and warns on dusk flats; do not start a calibration
  capture by hand during a run.

## Why

The Paramount MX drifts unguided (PS-168); a dense TPoint model with
ProTrack is the fix. The scripted path (PS-171 mapping run: NINA shoots,
TheSky Image Links the FITS, `TheSkyXAction.execute(AddPointingSample)`)
does not add points (PS-187): TheSky's add acts on TheSky's OWN camera
Image Link. The 356-point model of 2026-10-10 came from TheSky's Automated
Pointing Calibration Run run by hand with TheSky's camera connected (about
414 planned points over several hours). Goal: make that repeatable with
less hand work, without ever touching an armed or imaging night.

## Hard rules (from the 2026-10 attempt that was blocked)

- Operator-started only: Jeremy runs the command (or a future dashboard
  button) and stays at the console. No timer, no auto-arm hook, no
  overnight hand-off.
- Never arms, never dispatches, never starts a NINA sequence.
- Refuses unless the armer is DISARMED, both NINAs are idle, no calibration
  capture job runs, the roof reads open, it is dark, TheSky answers with the
  mount connected and a TPoint backup is planned. Unknown = no go.
- The camera is lent, never taken: NINA #1 disconnects it first (one app
  can hold the AP26MC), TheSky gives it back at the end, and NINA #1 must
  read Connected again before the command returns.

## What is confirmed vs unknown

| Need | How | Status |
|---|---|---|
| TheSky camera connect / disconnect | `ccdsoftCamera.Connect()` / `Disconnect()` | Published API, used by many TheSkyX clients (POCS, pySkyX). Not yet run on build 14139 with the AP26MC. Needs TheSky's Camera Setup to select the AP26MC (ASCOM or native) once by hand. |
| NINA #1 releases the camera | ninaAPI `GET /equipment/camera/disconnect`, `/connect`, `/info` | Already in `nina_client.py` (`disconnect_camera` / `connect_camera`). Cooler behaviour on disconnect not checked. |
| Exposure / binning | `ccdsoftCamera.ExposureTime`, `BinX`, `BinY`, `Frame`, `ImageReduction`, `AutoSaveOn`, `Asynchronous`, `TakeImage()`, `IsExposureComplete`, `LastImageFileName` | Published API. |
| Image Link settings for TheSky's own runs | `AutomatedImageLinkSettings.imageScale`, `exposureTimeAILS` | Published; we already READ them (PS-104 `ails`). Binning is not among them (the camera's BinX is). |
| Slew to a sky point without sync | `sky6RASCOMTele.SlewToAzAlt(az, alt, name)`, `IsSlewComplete`, `Asynchronous` | Published. ClosedLoopSlew is NOT used (it syncs). |
| Add one TPoint sample from script | `sky6RASCOMTheSky.AutoMap()` | Published method name (in Bisque's reference and in public clients, e.g. alora's add_to_pointing_model.js runs TakeImage then AutoMap). Believed: Image Link the camera's last image and add it to TPoint. NOT confirmed on build 14139: this is the one thing the first test must prove. |
| Start the Automated Pointing Calibration Run | `TheSkyXAction.execute(...)` | TheSkyXAction runs toolbar commands ("a subset of commands listed under Preferences, Toolbars, Customize"). `AutoPointingCalibration` = 105 on build 14139 most likely OPENS the window. No scripted point count, Create Pointing Targets, Start, progress or stop is known. CANDIDATE. |
| Point count / sky grid / exposure of the GUI run | none known | UNKNOWN: set in the window by hand. |
| Progress of the GUI run | indirect: frames in `ccdsoftCamera.AutoSavePath`, mount position reads | Read-only, plausible, unverified. |
| Model fit / save / Apply corrections / ProTrack | none | No TPoint scripting object on build 14139 (2026-10-08 probe). Stays MANUAL in the TPoint window. |
| Model numbers before / after | `photonscript tpoint-stats` (TPoint's own files) | Confirmed (PS-171 / PS-184). |
| Stop | `ccdsoftCamera.Abort()`, `sky6RASCOMTele.Abort()` | Published. |

## Two modes

**loop (recommended once AutoMap is proven).** PhotonScript drives each
point itself with TheSky's camera: slew (async, poll), expose (async,
poll), `AutoMap()`, a CSV row. The point grid is the PS-171 one
(`tpoint_mapping.plan_points`: equal-area spiral above
`tpoint_mapping_min_alt`, meridian band and under-pole skipped, east then
west, serpentine). PhotonScript owns the point count, the progress, the
stop rules and the log. Everything depends on AutoMap adding a sample.

**window (fallback, works today in principle).** PhotonScript does the
preflight, backup, camera hand-off and settings, opens TheSky's Automated
Pointing Calibration window, and Jeremy clicks Create Pointing Targets (N)
and Start. PhotonScript only watches (AutoSave frame count, mount position)
and does the return hand-off and the before / after stats when Jeremy says
the run is over. This is the 2026-10-10 procedure with the plumbing done
for him.

## The procedure as designed (`--mode loop|window`; loop is built, see above)

1. **Preflight (PhotonScript, read only).** GET `/api/arm` (state must be
   DISARMED; any of ARMED, RUNNING, PAUSED_UNSAFE, PAUSED_OPERATOR,
   WATCHING, COMPLETE, ERROR refuses), GET `/api/apps/status` (NINA #1 and
   #2 sequence state: no RUNNING item and readable; no calibration capture
   job; TheSky answering with its mount connected; sun at or below -12
   deg), NINA #1 safety monitor IsSafe (roof open), `PS_AUTO_ARM_ENABLED`
   false (the evening auto-arm could arm and load NINA #1 mid-run),
   `PS_AUTO_DUSK_FLATS` (warning: start after the dusk flats window). Any
   fail or unknown stops here and prints why.
2. **Operator checks (console).** PHD2 not guiding; roof open on the
   webcam; TheSky Camera Setup selects the AP26MC; TPoint window open; note
   the TPoint point count.
3. **Backup.** Copy every TPoint file (`tpoint_files.list_files`) to
   `C:\astro\tpoint_backup\<UTC stamp>\` keeping the relative paths, then
   `tpoint-stats` (BEFORE numbers, saved to stats_history).
4. **Lend the camera.** NINA #1: GET `/sequence/state` again (still idle),
   `/equipment/camera/info` (remember Connected, cooler, setpoint),
   `/equipment/camera/disconnect`, `/info` (Connected false). TheSky: read
   the selected camera, `ccdsoftCamera.Connect()`, read Status.
5. **Setup.** TheSky camera: exposure (default `tpoint_mapping_exposure_s`
   5 s), bin (`tpoint_mapping_binning` 2), light frame, no reduction,
   autosave, asynchronous. Automated Image Link: scale =
   `pixel_scale_arcsec` x bin (0.472"/px), exposure.
6. **Run.**
   - loop: per point slew, poll, expose, poll, AutoMap, CSV row
     (`runs/<night>_tpoint_autocal.csv`: point, alt / az, solved, RMS,
     error). Every point also re-reads `/api/arm` and the safety monitor.
   - window: open the window, Jeremy starts it; PhotonScript prints
     progress (frames saved, mount alt / az) every 30 s.
7. **Progress report.** Console lines plus `tpoint_autocal` events in
   `runs/<night>_events.jsonl` (start, every 10 points, stop reason, end).
8. **Abort** (any time): Ctrl+C or `photonscript tpoint-autocal --abort`
   (future) sends `ccdsoftCamera.Abort()` and `sky6RASCOMTele.Abort()`, then
   continues with step 10 (the camera is always returned). Automatic stop:
   3 unsolved points in a row (clouds / focus), the roof unsafe, the armer
   leaving DISARMED, NINA #1 showing a RUNNING item, or a TheSky error.
9. **Model (MANUAL).** TPoint window: fit (Super Model), check the RMS,
   save, Apply pointing corrections / ProTrack as usual. No scripting
   object exists for any of this.
10. **Return the camera.** TheSky `ccdsoftCamera.Disconnect()`, NINA #1
    `/equipment/camera/connect`, `/info` must read Connected (and the cooler
    is re-applied if it was on). If NINA cannot reconnect: a loud console
    message and a Pushover, never a retry loop.
11. **After.** `tpoint-stats` (AFTER numbers), a before / after summary
    line (points, sky RMS, polar ME / MA). Never arms; arming tonight is a
    separate, deliberate click.

## What stays manual

TheSky Camera Setup (once), the TPoint fit / save / Apply corrections /
ProTrack, judging the RMS, the go decision itself, and in window mode the
point count and Start.

## Risks

- **Auto-arm / dusk flats / calibration capture grabbing the camera.** Any
  scheduler job that connects NINA #1's camera mid-run takes it from
  TheSky. Preflight refuses with auto-arm on and warns on dusk flats, but a
  scheduler-wide "camera lent to TheSky" lock that every connect path
  honours is needed before `--go` (proposed ticket).
- **Two apps on one camera.** If NINA does not release the driver (ASCOM
  in-proc vs native SDK), TheSky's Connect fails or both hang. First test
  covers it.
- **Cooler.** Disconnecting in NINA may stop cooling; short Image Link
  frames do not need it, but NINA #1 must re-apply it before any imaging.
- **AutoMap semantics.** It might add nothing, add a sample using the
  scripted ImageLink state, or open a dialog. Unknown until one supervised
  call.
- **TPoint correction state while sampling.** Whether TheSky records raw
  mount coordinates regardless of Apply pointing corrections is assumed
  (it does for its own runs), not verified for AutoMap.
- **Long run vs the night.** ~40 s per point: 400 points is about 4.5 h.
  A run that started "tonight" blocks imaging tonight; that is the price.
- **Mount safety.** Slews go to 30..85 deg altitude only, open loop, no
  sync, no park, no tracking change; TheSky's own limits still apply.

## First supervised test (one night, Jeremy at the console, ~30 min)

1. Afternoon: `photonscript tpoint-autocal --points 8 --dry-run --status`
   and read the plan and preflight; `photonscript tpoint-stats`; note the
   TPoint window's point count.
2. Dark, armer DISARMED, NINAs idle. By hand (not by PhotonScript): NINA #1
   Equipment > Camera > Disconnect. TheSky: Camera Setup selects the
   AP26MC, Connect. Does it connect? Take one 5 s bin 2 frame from
   TheSky's Camera window.
3. Tools > Run Java Script: paste the dry run's `automap` script (one
   point). Watch the TPoint window: did the point count go up by one? Note
   the Out text.
4. Paste the `open_autocal_window` script: does it open the Automated
   Pointing Calibration window (and nothing else)?
5. TheSky camera Disconnect; NINA #1 camera Connect; confirm cooling.
6. Report: camera hand-off OK / not, AutoMap added a point yes / no,
   window opened yes / no. Those three answers decide whether `--go` is
   built in loop mode, window mode, or not at all.

## Decisions for Jeremy

1. Loop mode (PhotonScript owns the points, needs AutoMap) or window mode
   (TheSky's own run, Jeremy clicks Start)? Recommendation: test both in
   the first session, build loop if AutoMap adds a point.
2. Point count for a routine run, and whether runs ADD to the 356-point
   model (recal) or build a new base.
3. Where the go button lives: CLI only, or a dashboard button too (still
   operator-pressed, with the preflight shown first).
4. Whether the "camera lent to TheSky" lock should also block the noon
   re-arm and the cooler belts, or only evening jobs.
5. Backup location: `C:\astro\tpoint_backup` (default here) or elsewhere.
