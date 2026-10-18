# TPoint refit (after tpoint-autocal)

Status (2026-10-18): BUILT, UNTESTED ON HARDWARE. Not in the remote CLI
allowlist (Jeremy decides that separately). Code:
`photonscript/telescope_agent/tpoint_refit.py`, CLI `photonscript
tpoint-refit`, tests `tests/test_scheduler/test_tpoint_refit.py` (fakes
only).

After `photonscript tpoint-autocal --go` adds samples, the TPoint model
still has to be refit (Super Model), saved, judged, and "Apply pointing
corrections" / ProTrack turned on. `tpoint-refit` scripts the parts that
can be scripted from evidence and keeps the rest a console step.

## What is evidenced and what is not

| Need | Evidence | Status |
|---|---|---|
| A TPoint scripting object (TPoint.Fit, TPoint.SuperModel, ...) | 2026-10-08 probe, TheSky64 10.5 build 14139: `TPoint`, `sky6TPoint`, `TPointAddOn2` globals are undefined (HANDBOOK, tpoint_sample.py) | NONE |
| Run a TPoint Command Line string (FIT, SUPERMODEL style, OUTMOD, INDAT) from a script | No known sky6* method takes a TPoint command string; HANDBOOK "TPoint Command Line from a script: not available on build 14139" | NONE |
| TheSkyXAction | An object of numeric action ids (AddPointingSample = 197, AutoPointingCalibration = 105, TPointModule = 146, TPointAddOn2 = 147); `execute` is a function that runs toolbar commands ("a subset of commands listed under Preferences, Toolbars, Customize") | EXISTS. The TPoint ids seen open TPoint windows |
| A TheSkyXAction id for a fit / Super Model / save | None seen. The probe's member list (names matching model / point / ...) was never captured in the repo or the session notes | UNKNOWN: `--discover` lists them |
| Which argument `execute` takes (id, name, upper-case string) | Not known without calling it (tpoint_sample.ADD_CANDIDATES lead with the numeric id) | UNKNOWN |
| A Super Model fit writes TPoint's files | Real files read from the scope PC 2026-10-08 (`Super Model Outmod.dat`, `Indat`, `Outdat`, the test fixtures); the TPoint watcher treats a model file with more than the 6 basic terms as the Super Model fit | EVIDENCED. Whether TPoint writes them on fit or only on File > Save is not |
| Read the new sky RMS / samples in the fit / fitted terms | `tpoint_files.stats` (PS-171 / PS-184), parsers tested on the real files | CONFIRMED |
| Turn on Apply pointing corrections / ProTrack | No write API. The one earlier attempt (an undocumented sky6RASCOMTele string command, never called) was removed in PS-104 (80a0586). `thesky_client.tpoint_flags` reads CANDIDATE property names only, never confirmed on site | NOT SCRIPTABLE: stays manual |
| Does TPoint reread its files after a restore? | Nothing known | UNKNOWN: the console test answers it |

Conclusion: no evidenced mechanism runs the fit from a script. The command
is built so the backup, the read-back, the judging and the restore are
real, and the fit itself is either done by Jeremy at the console (`--fit
console`, works today) or tried once as a CANDIDATE TheSkyXAction member
(`--fit action`, supervised test only).

## Commands (scope PC)

| What | Command |
|---|---|
| Dry run: the model now, the backup, thresholds, scripts. Sends and copies nothing | `photonscript tpoint-refit` |
| READ ONLY: list TheSkyXAction members mentioning TPoint / model / fit / super / ProTrack / correction / pointing (executes nothing) | `photonscript tpoint-refit --discover` |
| Console refit: backup, Jeremy runs Super Model + saves, read back, judge, restore if bad | `photonscript tpoint-refit --go` |
| Supervised first test of a candidate action | `photonscript tpoint-refit --go --test --fit action --action <Name>` |
| After a test confirmed a name | set `tpoint_refit_action=<Name>`, then `photonscript tpoint-refit --go --fit action` |

Options: `--min-samples` (default `tpoint_min_points` 50), `--max-rms`
(default `tpoint_rms_max_arcsec` 30), `--max-worse-pct` (default
`tpoint_refit_max_worse_pct` 20), `--wait-s` (default
`tpoint_refit_wait_s` 180), `--url` (the scheduler for GET /api/arm),
`--verbose`, `--json`. Exit 0 only when a fit was kept.

## What `--go` does

1. **Preflight** (unknown = no go): GET `/api/arm` reads DISARMED (a refit
   changes pointing, never mid-night); TPoint files found to back up; the
   model before read from TPoint's files. `--fit action`: the name passes
   the fence (a plain identifier; never anything that adds / clears
   samples, starts a calibration, slews, syncs, parks, homes, aborts or
   touches the camera, so `AddPointingSample`, `AutoPointingCalibration`
   and `TPointAddOn2` are refused) and is either pinned in
   `tpoint_refit_action` or run with `--test`.
2. **Backup**: every TPoint file to `C:\astro\tpoint_backup\<UTC stamp>\`
   (the tpoint-autocal `backup_plan`). A failed copy stops before anything
   else.
3. **Fit**:
   - console: Jeremy runs Super Model in TPoint, waits, File > Save, types
     yes.
   - action: `TheSkyXAction.execute(TheSkyXAction.<Name>)`, only when the
     member is a number (else nothing is called). `--test` asks before
     sending and afterwards asks whether TPoint ran a fit.
4. **Read back**: polls TPoint's files (every 5 s, up to `--wait-s`) for a
   new model (file, time, samples, RMS or terms changed).
5. **Judge** (`judge()`): keep only with samples in the fit >=
   min samples, samples per fitted term >= 3 (`tpoint_refit_min_samples_per_term`),
   sky RMS <= the limit, and sky RMS at most +20% over the model before.
   Anything unread is a reject. No new model = `no_change` (nothing
   restored).
6. **Reject**: each changed file is copied to `<backup>\rejected\` and the
   backup copied back; files the fit created are moved to
   `<backup>\rejected\`. Nothing is deleted. TheSky still holds the
   rejected model IN MEMORY (ProTrack uses that one): reopen the restored
   model in TPoint (or undo the fit) and do not save before that.
7. **Kept**: the stats are saved (stats_history), a record goes to
   `<data_dir>\tpoint\refit_<stamp>.json`, and the console says the
   manual next step.

Never arms, never dispatches, never moves the mount, never syncs, never
turns pointing corrections or ProTrack on or off.

## What stays manual (every time, today)

- Turning on Apply pointing corrections and ProTrack (Activate ProTrack +
  Enable tracking adjustments) in TPoint / TheSky.
- With `--fit console`: the Super Model fit and the save.
- After a reject: reopening the restored model in TPoint.

So Jeremy cannot finish a refit remotely yet. The remote path needs (a) a
TheSkyXAction member that runs the fit, confirmed below, and (b) a way to
turn corrections / ProTrack on, which nothing evidences.

## First test at the console (once, about 15 minutes, no imaging night)

Armer DISARMED, both NINAs idle, right after a tpoint-autocal run (or any
time with a saved model). Jeremy at the scope PC.

1. `photonscript tpoint-refit` and `photonscript tpoint-stats`: note the
   model (samples, sky RMS, terms, file and time).
2. `photonscript tpoint-refit --discover`: paste the output into the
   ticket. It answers whether ANY TheSkyXAction member mentions a fit /
   super model / ProTrack / correction. Only a name that reads like a fit
   is worth step 5.
3. `photonscript tpoint-refit --go` (console form): when it asks, run
   Super Model in TPoint. Before saving, check
   `C:\Users\<user>\Documents\Software Bisque\TheSky Professional Edition 64\TPoint`
   (or `photonscript tpoint-stats --files`): did `Super Model Outmod.dat`
   change on the fit alone, or only after File > Save? Save, type yes,
   read the verdict.
4. To check the restore once: run step 3 again with `--max-rms 1` (forces a
   reject). Then in TPoint: does File > Open on the restored
   `Super Model Outmod.dat` / Indat bring the old model back (RMS and
   samples as in step 1)? Does TPoint, when closed and reopened, load the
   restored files? Note what TPoint shows. Put the model back the way you
   want it before leaving.
5. Only if step 2 listed a fit-like name: `photonscript tpoint-refit --go
   --test --fit action --action <Name>` and watch TPoint. Report: did a
   fit run, a window open, a dialog appear, or nothing? Did the files
   change without a save?
6. In TheSky's Tools > Run Java Script, the on-site check script
   (MAINTENANCE.md, `/api/thesky/onsite-script`) with ProTrack and Apply
   pointing corrections OFF and then ON: which `tpoint_flags` candidates
   change. That is read only and decides whether a later ticket can even
   READ the state; nothing evidences a WRITE.

Answers that unlock the remote path: step 5 ran a fit (pin the name in
`tpoint_refit_action`) and step 3 showed the files change without a
manual save (or the action also saves). Corrections / ProTrack stay
manual until a write is found.
