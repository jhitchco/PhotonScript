# TPoint autocal (design, operator-started, supervised)

Status: DESIGN plus offline building blocks. `photonscript tpoint-autocal
--dry-run` prints the plan, the preflight and every TheSky script and sends
nothing. `--go` is NOT implemented (it raises with a message). Code:
`photonscript/telescope_agent/tpoint_autocal.py`, tests
`tests/test_scheduler/test_tpoint_autocal.py`.

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

## The procedure (`photonscript tpoint-autocal --points N --mode loop|window`)

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
