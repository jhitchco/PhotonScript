# PhotonScript — Maintenance & Operations

Operational tasks ship **in the repo** and reach the scope PC through the normal
deploy (commit, then `.\deploy\deploy.ps1`) — never hand-copied.

## Running the service (start, stop, boot) - PS-44

On the scope PC PhotonScript runs as the `PhotonScript` scheduled task:
`deploy\run-photonscript.ps1` (`photonscript self-update`, PS-58) -> `photonscript supervise` ->
`photonscript start --mode full`. By default (PS-55) the task starts it 30 s
after `jeremy` logs on, in his desktop session with a hidden window
(`-LogonType Interactive`, the environment the console wrapper runs in); with
Windows auto-logon that is also "at boot". `-LogonType S4U` starts it at boot
with nobody logged on (session 0), which went slow on 2026-09-26 and is kept
only for a supervised test. The supervisor restarts it after a crash: 5 s, 10 s, 20 s ... up to 5 min, reset after 30 min up. Five crashes in
15 min and it gives up and sends "PhotonScript is down". Every crash sends
"PhotonScript crashed". Exit 42 (update) goes back to the wrapper, which runs
the staged, smoke-checked update and starts a fresh supervisor. After an
update the supervisor waits up to 90 s for /api/health to report the new
SHA; otherwise it exits 43 and the wrapper resets to the previous SHA and
sends "PhotonScript rolled back" (PS-58). The wrapper script is read once at
start, so a change to it takes effect only after the wrapper restarts:
`photonscript stop` (the wrapper ends with the supervisor), then
`Start-ScheduledTask PhotonScript` (or a reboot).

| Do this | Command (scope PC) |
|---|---|
| Status | `photonscript status` (pid, uptime, supervisor, version; API up / SLOW / refused / no answer, loop lag) |
| Watch logs | `photonscript monitor` (or from the desktop: `--url https://teles-feb25.lobster-bleak.ts.net`) |
| Stop, stay down | `photonscript stop` (`--force` to hard-kill; both leave a HOLD so the supervisor does not restart it) |
| Restart | `photonscript restart` |
| Start | `Start-ScheduledTask PhotonScript` |
| Task state | `Get-ScheduledTask PhotonScript \| Get-ScheduledTaskInfo` |
| Install / remove | elevated: `deploy\install-autostart.ps1 [-StartNow] [-LogonType Interactive\|S4U\|Password]` / `-Uninstall` |
| Verify autostart | `photonscript autostart-check` after an install or reboot (read-only; exit 1 on FAIL); `--hours 16` the morning after; `--watch-restart [--kill]` times a crash recovery. Full procedure: `docs/AUTOSTART_TEST_PLAN.md` (PS-34a) |

Logs: `<data_dir>\logs\photonscript.log` (service), `supervisor.log`
(starts, exits, restarts), `wrapper.log` (updates, rollbacks), `update_state.json` (last update: pending /
good / rejected / rolled_back, previous and last-good SHA). data_dir is
`C:\Users\jeremy\.photonscript` unless `PS_DATA_DIR` is set.

Gotchas: the task has no console, so use `photonscript monitor` instead of
watching a window. Running the wrapper by hand while the task is up just exits
("another supervisor is already running"). NINA and PHD2 are not started by
PhotonScript and still need jeremy logged on after a reboot (auto-logon).
Signing out of jeremy's session stops an Interactive task; disconnect Remote
Desktop instead of signing out. If an S4U task will not start with a logon
error, re-run the installer with `-LogonType Password`.

### Slow or stalled service (PS-55)

- `GET /api/health` (PS-57) answers from memory only: version, commit (full
  SHA), started_at, uptime_s, pid, mode, loop lag (`lag_ms`,
  `max_lag_ms_5min`, `stalls_5min`), process context, armer state. Its
  response time is the event loop's own latency. `photonscript status` uses
  it with a 30 s timeout and says `up`, `SLOW` (over 2 s), `connection
  refused` (not listening) or `no answer within 30 s` (running but stalled).
- The service log starts every run with a `Process:` line: pid, user, Windows
  session (0 = scheduled task without a desktop), priority class, power
  throttling, elevated, launcher (`console` or `task-<LogonType>`, set by
  `run-photonscript.ps1 -Launcher`). Compare a slow run with a healthy one.
- A watchdog thread writes `<data_dir>\logs\stalls.log` whenever the event
  loop has not ticked for 5 s, with the Python stack of the code that is
  blocking it; loop lag over 2 s is logged as `Event loop lag N s`.
- At startup the process opts out of Windows power throttling (EcoQoS) and
  lifts a below-normal CPU or memory priority to normal
  (`PS_PROCESS_QOS_GUARD=false` turns this off).
- astropy IERS data is pinned offline (`PS_IERS_OFFLINE=true`, default): the
  table bundled in `astropy-iers-data` is used and never downloaded from the
  night loop. Refresh it in daytime with
  `C:\astro\venv\Scripts\python.exe -m pip install -U astropy-iers-data`.
  The astropy cache is `C:\Users\jeremy\.cache\astropy` (not `.astropy`).
- Never start the service from an elevated (Administrator) shell: files it
  creates (astropy cache, logs) end up owned by Administrators and the normal
  account can no longer write them.

## Pruning old captures (free the capture drive)

Deletes captured FITS from nights before a cutoff. Uses the **live config
paths** (`image_watch_dir`, piggyback watch dir, `data_dir/thumbs`). Grade
records (`*_subs.jsonl` / `*_plan.json`) and contact sheets are **always kept** —
they are the durable per-sub learnings.

```powershell
photonscript prune-nights                    # dry run (default) — prints table, deletes nothing
photonscript prune-nights --execute          # delete, with a confirmation prompt
photonscript prune-nights --before 2026-08-01 --execute --yes   # custom cutoff, no prompt
photonscript prune-nights --execute --quarantine D:\pruned      # move instead of delete
```

Default cutoff is `2026-09-01` (keeps September on).

## Archiving runs from the sidenav

Old nights can be hidden from the Runs sidenav (leaving the current season)
without deleting anything:

- **Runs page → "⤓ archive all before 2026-09-01"** collapses old nights.
- Per-night ⤓ / ⤒ toggles archive / restore one night.
- "show N archived" reveals them again.
- API: `POST /api/runs/{date}/archive {archived: bool}`,
  `POST /api/runs/archive-before {date}`. State in
  `data_dir/archived_nights.json`.

## Per-run contact sheets (archival "screenshot" of a night)

```
GET /api/runs/{date}/contact-sheet.png[?cols=6&w=200]
```

Server-side montage of every sub for the night (green = accepted, red =
rejected, HFR/ecc captions), rendered where the FITS live and reusing the
thumbnail cache. Cached at `data_dir/contact_sheets/<date>.png`. Generate these
for nights you're about to prune so the visual record survives.

## Remote log tails (2 AM triage)

```
GET /api/nina/log?lines=800&grep=Autofocus        # NINA #1 / RC16 (nina_logs_dir)
GET /api/nina/log?rig=piggyback&grep=Autofocus     # NINA #2 / OSC (piggyback_nina_logs_dir)
GET /api/phd2/log?lines=800&grep=GuideStep|star lost   # PHD2 GuideLog (phd2_logs_dir)
GET /api/phd2/log?kind=debug                       # PHD2 DebugLog
GET /api/nina/log?rig=rc16&date=2026-09-26         # PS-73: every log of that night (12:00-12:00 local), survives a NINA restart
GET /api/nina/logs?rig=piggyback                   # PS-73: NINA log files with rig + start time (for file=)
GET /api/phd2/log?date=2026-09-26&grep=star lost   # PS-73: a night's PHD2 log(s); file=<name> for one
GET /api/phd2/logs                                 # PS-73: where PHD2 logs were searched / found, file list
GET /api/phd2/summary?date=2026-09-26              # PS-73: RMS RA/Dec (arcsec + px), star lost, calibrations (Dec, pier side)
GET /api/ascom/log?name=Safety                     # ASCOM trace log
GET /api/notifications?since_hours=24&title=cooler # Pushover audit: tally by type
```

Every Pushover **decision** (sent AND suppressed) is now appended to
`<data_dir>/notifications.jsonl` — before, a sent alert was logged nowhere, so
there was no audit trail. `/api/notifications` reads it back with a per-title
tally (total / sent / suppressed) over a window, so "how many of each did I get,
and how many did the rate-limiter throttle" is answerable. The file is
append-only, auto-trimmed to the last ~5000 lines past ~4 MB.

`phd2_logs_dir` defaults to `%USERPROFILE%\Documents\PHD2`; set
`PS_PHD2_LOGS_DIR` (System & Config → PHD2) if PHD2 writes its logs elsewhere.
Set `piggyback_nina_logs_dir` (NINA #2's log folder) to tail the OSC's log.

## Pushover volume (verbosity + de-duplication)

Two Pushover streams reach the phone: the **sequence narration** (NINA's
GroundStation plugin, driven by the sequence PhotonScript generates) and
PhotonScript's **own watchdog alerts** (`notify()`).

- **Narration is the bulk of the volume.** `pushover_verbosity` controls it:
  `verbose` = every step incl the per-block "starting/done" pair (2×/filter/
  target); **`normal` (default)** drops the per-block pair but keeps per-target
  step lines + night milestones; `quiet` drops the per-target step lines too.
  Changing it takes effect on the next dispatched sequence.
- **Watchdog alerts** (`notify()`) are each once-per-episode with a reset:
  guiding, cooler-OFF, safety-monitor-unreadable, transfer-stall, AF-quality,
  trends, dawn-shutdown, dispatch/arm errors. Not affected by
  `pushover_verbosity`.
- **Cooler de-dup (2026-09-26):** the armer's cooler nanny and the
  telescope-agent cooling watchdog used to overlap. Now the **nanny alerts only
  when the cooler is flat OFF** (and silently re-asserts the setpoint otherwise),
  while the **agent watchdog owns "cooler on but 0% power / not cooling"** — so a
  single fault raises one alert, not two. The agent watchdog also used to
  Pushover on **every** reconnect attempt (2/4, 3/4… — a burst per stuck-cooler
  episode); it now alerts on the **first attempt only**, and the final give-up
  still escalates. Both now cool instantly (`cool_ramp_minutes`), no 10-min ramp.
  Tolerance is the single `cooling_tolerance_c`.

## Run-time alerts (Pushover)

Beyond the guiding + cooler watchdogs above, a run now also fires once on:
- **Safety monitor unreadable** — the monitor reads None (disconnected/erroring)
  for ~3 min during a run, so roof gating is blind (the OSC Alpaca sim that "came
  off"). Resets when it reads cleanly. `safety_monitor_watchdog=false` to mute.
  NINA's own SafetyMonitorCondition still gates imaging regardless.
- **Transfer stalled** — the desktop transfer batch's pending count hasn't
  drained in `sync_stall_min` (30) min while non-empty (a wedged Syncthing /
  librarian loop). Fires from `/api/sync` once per stall episode; `sync_stall_min=0`
  disables. Also surfaced as `batch.stalled` / `stalled_min` in the sync payload.

## Guiding is the default

`guided_default = True`. On the dashboard, **Arm (Guiding)** is the primary
button; **Arm (Encoders)** now shows a confirm guard (unguided long subs at
3248 mm trail).

**Not-guiding watchdog (escalation ladder).** Past `guiding_watchdog_grace_min`
(default 20) after dusk, if PHD2 isn't locked-and-guiding the armer escalates:
(1) one warning Pushover — a hard idle trips at once, a stuck
calibrating/looping state only after it persists (so a normal dither settle
doesn't false-fire); (2) if still unlocked, **one automatic guider restart**
(stop+start, no forced cal so Auto-restore reuses a good calibration) —
disable with `guiding_auto_recover=false`; (3) if still unlocked, a priority
escalation asking for hands-on help. Recovering to a locked state resets the
episode, so it re-arms for a later failure the same night. This catches both
the armed-guided/PHD2-idle case (2026-09-24) and a stuck "star did not move
enough" calibration loop (2026-09-26).

**First-target calibration.** The night's first guided target forces a fresh
PHD2 calibration (`StartGuiding.ForceCalibration`); every later target relies on
PHD2 Auto-restore. Set `guiding_force_first_calibration=false` to never force
(always trust a restored cal) — avoids a failed first-cal loop but risks guiding
on a stale/absent calibration.

**PHD2 calibration manager (PS-93).** With `phd2_cal_mode=auto` (default)
the night's sequence gets a `PHD2_CALIBRATION` slot only when it is needed (no
calibration on record, the last one FAILED, older than `phd2_cal_max_age_days`
(30), PHD2 profile / binning / scale changed, or a request from the Guiding tab
"Calibrate at the next dispatch" / `POST /api/phd2/calibrate?mode=next`).
`always` adds it every guided night (about 4 min of twilight), `never` keeps the
PS-72 behavior above. The slot calibrates on a field near Dec +5 by the
meridian, then holds `phd2_cal_hold_s` (240) while the RC16 agent grades it and
retries a FAIL once. `GET /api/phd2/calibration` shows the graded record, the
plan, the after-flip checks and the recommended PHD2 Calibration Step.
`mode=now` while a night runs re-dispatches once with the slot (1 h of dark
left); with nothing running it sends a standalone calibration sequence to NINA
(roof open and dark only). `GET /api/phd2/calibration-sequence` returns that
sequence for loading by hand.

**PHD2 settings audit (PS-89).** `config/phd2/desired_oag_rc16.toml` is the
desired PHD2 state for the RC16 OAG, one "why" per row. Every guided arm
audits it in the background after connecting the equipment (never blocks the
arm; one Pushover only when at least one row FAILs, once per night), and the
RC16 agent re-audits 60 s after a PHD2 configuration change (a push only for
a FAIL not pushed tonight, and only while a night is armed). Sources: PHD2's
JSON-RPC API (a short second connection), PHD2's stored profile in the
registry, the newest guide-log header (8-bit inferred from the PS-88
saturated-star rule), the newest Guiding Assistant, the PS-93 calibration
record, NINA's guider settings and mount guide rate, the TheSky TCP port and
the PHD2 dark library (`%LOCALAPPDATA%\phd2\darks_defects`). Each row: pass /
warn / fail / unknown / info, current vs desired, why, fix.
```
GET  /api/phd2/audit                  the last audit (arm, PHD2 change or refresh)
GET  /api/phd2/audit?refresh=1        audit now
GET  /api/phd2/audit?refresh=1&raw=1  + every observed value and every registry value read
POST /api/phd2/audit/apply {"ids": ["exposure_ms"], "dry_run": false}
```
Apply (also the Guiding tab buttons): API rows (guide exposure, picked from
PHD2's own exposure list; RA min-move from the Guiding Assistant; Dec guide
mode) only while PHD2 is Stopped or Looping and no other PhotonScript actor
holds PHD2. Profile rows (bit depth, Max ADU, search region, mass tolerance,
min HFD, ...) only with `phd2_audit_autofix=true`, the armer DISARMED or
COMPLETE, phd2.exe closed, a verified registry name and a fresh backup in
`<data_dir>\phd2_profile_backups\<ts>_<id>.reg` (restore: close PHD2, `reg
import <file>`). Mount driver, TheSky and NINA rows are report only. Every
change is logged to `<data_dir>\phd2_audit\changes.jsonl`. The registry value
names in `scheduler/phd2_profile_store.KEYS` were checked for reading against
the scope PC's `reg export` of 2026-10-04 (PHD2 2.6.14, profile 2; PS-119);
writes stay refused until a key is added to `WRITABLE`. Re-run the export
after a PHD2 update and compare. `pe_owner`
(`protrack`) says who corrects periodic error: with ProTrack, PHD2's RA
algorithm must not be Predictive PEC.

**Guide-star auto-tune (PS-90).** The RC16 agent measures the guide star
after every settle (and on a filter change: the OAG is assumed to sit behind
the wheel) from PHD2's `get_star_image` crop plus the GuideStep SNR, HFD and
ErrorCode: peak and star amplitude as a share of `phd2_guide_full_scale_adu`
(65535), clipped (a pixel at 98%, or ErrorCode 1), HFD, an 8-bit flag and the
one-pixel share (PS-91). Target: peak 60 to 80%, never clipped, SNR over 20,
HFD 2 to 5 px, exposure 1 to 4 s. It never touches PHD2 unless PHD2 is
Guiding and settled, nothing else holds it (guard recovery, self-test,
calibration retry, audit apply, hot-pixel map), the PS-93 calibration slot
is not running and no guard non-star episode is open.
```
phd2_tune_mode = observe     measure and record only (default; never set_exposure)
phd2_tune_mode = exposure    live exposure-only tuning (one change per settle at most)
phd2_tune_mode = off         not started
GET /api/phd2/tuning?date=   per filter, changes, last measurement, advice, bin 3 check
```
In `exposure` mode it steps down at once on a clipped star, otherwise only
after 3 readings outside 55 to 85%, and picks the longest listed PHD2
exposure not over the linear prediction, inside `phd2_tune_exp_ms` and only
one the PHD2 dark library holds (no library = no change). Changes go under
`phd2_ops.hold("tuner")` and are logged to `<data_dir>\phd2\tune\<night>.jsonl`
and the PS-89 `changes.jsonl`. Memory: `<data_dir>\phd2\tune.json`, one entry
per profile / binning / gain / target / filter; a filter change applies the
remembered (or L-ratio predicted) exposure first. Gain and binning are not
API settable: the Guiding tab "Guide Star Tuner" section shows next night's gain advice
(also the PS-89 audit's gain row) and whether bin 3 would hit the HFD band.
The armer writes the gain pre-dusk (ARMED, before the dispatch) through the
PS-89 profile writer only with `phd2_audit_autofix=true`, PHD2 closed, a
backup and a verified registry name, then re-audits. PHD2's gain is its own
0 to 100 setting (mapping to sensor gain unverified).

By hand on the scope PC before `exposure` mode: PHD2 16-bit, saturation by
Max ADU 65535, auto exposure off (so `set_exposure` sticks), and a dark
library covering every exposure from 1 to 4 s at the chosen gain and binning
(plus the defect map).

### Reading the TheSky / TPoint audit (PS-104)

Guiding tab, section "TheSky / TPoint" (`GET /api/thesky/audit?refresh=1`,
`photonscript thesky-audit [--json] [--imagelink]`). Report only: it never
writes TheSky, never moves / unparks / parks / syncs the mount and never
takes an image. Each row: status, current value (source), desired, why, fix,
and a confidence (High / Med / Low: how sure we are the read works on the
site's TheSky 10.5 build).
- Unknown with "TheSky TCP ... not reachable": TheSky's TCP server is off
  (Tools > TCP Server). Unknown with "manual": enter the TPoint record.
- Rebuild the model?: REBUILD with its reasons (age over
  `tpoint_max_age_days`, equipment changed after the model, camera angle
  moved over 1 deg mod 180, scale moved over 1%, first-slew median over
  `pointing_first_slew_fail_arcmin` on either side); "watch" at 75% of the
  age limit or a first-slew median over the warn limit.
- Image Link check: "ASTAP check now" solves the newest RC16 L frame
  (broadband fallback) and compares the native scale x run binning with
  TheSky's Automated Image Link scale (0.236 x 2 = 0.472; 0.942 is the old
  4x4 setting). "TheSky Image Link on a temp copy" runs TheSky's own solver on
  a copy in `<data_dir>/thesky_audit/tmp/` (deleted after), only while the
  armer is DISARMED or COMPLETE: success there and a failing "Take And Image
  Link Photo" means TheSky cannot take the picture (camera held by NINA #1,
  narrowband filter, AutoSave), not that it cannot solve.
- First-slew error: NINA's first solve of each Center run vs its target, by
  side of the meridian (from the hour angle unless the log names the pier),
  Dec band and HA band, 14 nights, counting only runs after the TPoint model
  (PS-120: the earlier of the record's entry time and the end of the model
  night; none yet reads "no slews since the model (n=0)", unknown). PS-67's mount vs solve median shows beside
  it once that record exists.
- PS-138: TPoint model on (Apply pointing corrections), points, RMS and
  ProTrack (Activate ProTrack + Enable tracking adjustments) are read live
  from TheSky (`thesky_client.READ_PAIRS["tpoint_flags"]`, candidate property
  names, confidence Low until the on-site check confirms them). When only the
  manual record answers, the row is info "manual (date)", never a pass:
  verify by eye. ProTrack is greyed while TheSky's mount is not connected or
  not tracking: that alone reads "OFF (greyed)" (warn by day, fail while a
  night is armed), shows as "ProTrack OFF" on the Guiding tab when it fails,
  and an UNGUIDED arm pushes one warning unless ProTrack reads on.
- First slew vs model index terms (PS-138): when the newest night's
  first-slew median is over `pointing_first_slew_fail_arcmin` and matches
  hypot(IH, ID) within x2 (and in direction, |north| / |east| vs |ID| / |IH|
  within 25 deg), the mount's index / home moved since the model: TPoint
  Recalibrate (IH / ID only), not a rebuild (the rebuild row then says
  "watch", not REBUILD). IH / ID come from TheSky if readable, else the
  TPoint record (TPoint Model tab values, arcsec).
- After each TPoint session: enter date, points, RMS, polar error, IH / ID, ProTrack,
  run binning and catalogs in the TPoint record form (and a line in
  HARDWARE.md "TPoint record"). Older than `thesky_manual_max_age_days` it
  reads unknown.

### TheSky on-site check (2 minutes, read only)

Once, on the scope PC, with no session running:
1. Open `/api/thesky/onsite-script` (Guiding tab "On-site check script"),
   copy it into TheSky's Tools > Run Java Script window and Run. Every line
   is a read; a "?ERR" names a property this build does not have. Paste the
   output into PS-104 (PS-138: note which `tpoint_flags.*` candidate
   answers with the real ProTrack / Apply pointing corrections state, and
   which read ?ERR) so the property names can be confirmed (or fixed in
   `thesky_client.READ_PAIRS`).
2. All Sky flags (Choice B2, optional): note the "Use All Sky Image Link"
   checkbox in the Automated Pointing Calibration Run setup, then run
   `var Out; sky6RASCOMTele.DoCommand(13, ''); Out = sky6RASCOMTele.DoCommandOutput;`
   and check the checkbox did not change. Only if it stayed put, set
   `PS_THESKY_AUDIT_ALLSKY_READ=true`.
3. A folder listing of TheSky's TPoint and Database folders (for a later
   file reader of the TPoint numbers and catalogs).
4. Verify the NINA Center-log parser on a real night log: `GET
   /api/thesky/pointing?nights=14&refresh=1` should show runs, and the first
   separations should match what NINA's Center log lines say. The parser was
   written from NINA's documented "Centering Solver - ... Separation ..."
   message (no real log on the desktop); a format difference shows as zero
   runs.

## Calibration — what an arm captures automatically

Matching is by **camera (INSTRUME) + full epoch** (`EXPTIME|GAIN|OFFSET|SET-TEMP`
for darks, camera for flats/bias), so RC16 (AP26MC) and the OSC (AP26CC) never
cross-use calibration, and only frames older than `library_cal_days` (120) or
shot at different settings are ignored. Capture at each rig's imaging settings
(the capture endpoints already do) and integration picks them up.

On arm:
- **RC16 darks** — shot roof-closed pre-dusk for `dark_exposures` up to
  `dark_target_count`, age-aware (a stale set counts as 0 and refills).
- **RC16 dawn flats** — for the filters used that night **plus any stale flat
  filter** (`auto_stale_flats`, default on) — so broadband flats stay fresh even
  across narrowband-only nights. Disable with `PS_AUTO_STALE_FLATS=false`.
- **OSC (piggyback) companion** — dawn flats always; darks/bias **always run**.
  When NINA #2 can see the shared safety monitor they're roof-gated
  (`LoopWhileUnsafe`); when it can't (the probe retries once first), they run
  **unconditionally, time-capped at astro dusk** so they fall in the roof-closed
  pre-dark window — a loud Pushover flags this mode, and frames that catch a
  just-opened roof are rejected by QA. Best fix: add the safety monitor to the
  NINA #2 profile to roof-gate them properly.

Manual capture any time (roof closed): `POST /api/calibration/capture
{"rig":"rc16"|"piggyback"}`; stale flats at dusk: `POST /api/calibration/flats
{"rig":"rc16","stale":true}`.

## Capture-drive free space

`GET /api/sync` includes `disk` (free/total GB, % used) for the capture drive;
the Runs page sync line shows it (green/amber/red at 80 % / 92 % used).

## Camera temperature (no ramps + a nanny)

Both the warm and the cool are **instant** by default, and a nanny enforces the
setpoint during the imaging window — no more "the cooler lost its mind" fights.

- `gradual_warm_minutes` (default **0 = instant**) — WarmCamera ramp on every
  cooler-off (arm cooler-off, dawn shutdown, disarm make-safe, sequence End,
  and the OSC companion End). 0 just cuts the TEC and lets the sensor drift.
- `cool_ramp_minutes` (default **0 = instant**) — CoolCamera ramp on precool.
  0 drives straight to the setpoint. A ramp is what let the cooler fight the
  arm/precool (kept pushing the temp back up). NINA's **Warming/Cooling →
  Min. Duration** on each camera should read 0 to match.
- `cooler_nanny` (default **on**), `cooler_tolerance_c` (default 3): on every
  safe RUNNING tick, from `cool_lead_minutes` before dark until dawn, each rig's
  cooler must be ON and within tolerance of setpoint. If a rig is off or warm
  (the 2026-09-26 stuck-at-20°C night that noised up the RC16 subs), the nanny
  drives it to setpoint with an **instant** cool and Pushovers once per rig
  until it recovers. The rule the user wanted: *safe → the camera is cold.*
  Set `cooler_nanny=false` to disable.

## Autofocus (narrowband star-starvation)

Autofocusing through a 3nm narrowband filter starves the star field — "Stars
detected: 1", no HFR curve, donuts (2026-09-26, Cat's Eye in Ha). Fixes, both
needed for full coverage:

- **PhotonScript's own AFs** (twilight startup + each target's start-of-target
  AF) now focus on `autofocus_filter` (default **L**) instead of the imaging
  filter. Set it "" to revert to focusing in the imaging filter.
- **NINA's triggered AFs** (AF-After-Filter-Change / HFR / temperature triggers)
  are NINA's, not PhotonScript's — they obey NINA's **global Autofocus Filter**
  option. Set that to **L** and fill in **per-filter focus offsets** so those AFs
  also run on broadband. Without this, mid-block AFs still fire in narrowband.
- Note: the per-filter `_autoFocusExposureTime` PhotonScript stamps into each
  SwitchFilter's FilterInfo (`_NB_AF_EXPOSURE_S`, Ha/OIII 30 s, SII 45 s) is
  **ignored by NINA for AF** — NINA reads AF exposure from the profile's filter
  settings, which is why a live AF still ran at 8 s. Set the AF exposure per
  filter in the NINA profile too.

**AF-quality alert.** Point `nina_autofocus_reports_dir` at NINA's AutoFocus
report folder (e.g. `%LOCALAPPDATA%/NINA/AutoFocus`) and the nightly backfill
grades each AF run; a run with fit R² below `af_min_r2` (default 0.7) or <3
measure points fires one Pushover naming the filter. Empty dir = disabled.

## OSC (AP26CC) calibration facts — verified 2026-09-26

- **AP26CC saves raw Bayer CFA** (`NAXIS=2`, `BAYERPAT=RGGB`), even when the OGMA
  driver's format toggle read "RGB" — that toggle only affects the live preview,
  not the saved FITS. So no OSC data was ever debayered‑in‑camera; existing OSC
  lights are fine. Keep the driver on **RAW** anyway (belt‑and‑suspenders).
- **OSC real capture params: gain 100, offset 256, LCG, SET‑TEMP 0°C, 120 s.**
  NINA #2 overrides the sequence plan's gain/offset (200/50) with the camera's
  own defaults, so lights land at **100 / 256**. OSC darks/bias/flats MUST be
  captured at 100 / 256 / 0°C (120 s darks) or they won't match — verify the
  companion captures at those values, not the plan's 200/50.
- **There is currently NO AP26CC calibration in the library** — every BIAS/DARK/
  FLAT is the mono AP26MC. The OSC has been integrating uncalibrated. Fix =
  capture a first OSC set (auto on the next arm); nothing to delete.
- `/api/calibration/health` is **camera‑blind** (lumps AP26MC + AP26CC), so it
  won't reveal the OSC gap — split it by INSTRUME (backlog).
- The mono AP26MC calibration is healthy (900 s darks, NB flats, bias) — keep it.
  Watch that lights match dark SET‑TEMP: a "stuck at 20°C" cooler night (e.g.
  2026‑09‑26 warm 900 s Ha) won't match the 0°C dark library.

## Field-recovery cheats (things that bit us live)

- **OSC not shooting lights (only flats/darks):** NINA #2's safety monitor is
  failing to read, so `has_safety` is false. Almost always the shared
  AlpacaDynamic driver's trace-log lock ("trace log file used by another
  process") — give NINA #2 its OWN Alpaca safety driver (AlpacaDynamic2) or turn
  off driver trace logging. Re-check at the next arm (has_safety is auto-detected).
- **PHD2 "star did not move enough" / won't calibrate at high Dec:** calibrate
  near **Dec 0 at the meridian** with the Calibration Assistant, enable **Auto
  restore calibration** (Brain → Guiding), and turn **OFF** NINA's Force
  Calibration. Don't recalibrate every target.
- **PHD2 calibration FAILED alert (PS-93):** read `GET /api/phd2/calibration`.
  "few steps" means the PHD2 Calibration Step is too long: set it to the
  recommended step (about 50 to 70 ms here, not 250; Brain > Guiding >
  Calibration step calculator). "star moved only N px" is the pulse path (run
  the PS-92 self-test) or a hot-pixel lock (PS-91 guard). Ortho over 5 deg with
  few steps is the step size again; ortho over 5 deg with 12 steps is backlash
  or flexure. Ask for a fresh one with "Calibrate at the next dispatch".
- **"Dec runs away after the meridian flip" alert (PS-93):** PHD2 Advanced >
  Mount > "Reverse Dec output after meridian flip" does not match the mount.
  Toggle it only after this alert (leave it alone until the flip check proves
  it), and keep the Bisque driver's "Can Get Pointing State" ticked so PHD2
  knows the pier side (the guide log must never show `Pier side = Unknown`).
- **Mount won't connect / TheSky COM3 "Error 201":** the port is locked. Kill any
  zombie TheSkyX, re-enumerate the USB in Device Manager, and connect in order
  **TheSky → NINA → PHD2** (or a clean reboot). Never let two apps own COM3.
- **Something pins the cooler at setpoint all day / warm+cooler fighting after a
  restart:** a restored armer state is re-issuing commands. Clear
  `C:\Users\jeremy\.photonscript\armer_state.json` and restart the service — do
  NOT click Disarm from RUNNING (it parks the mount as part of make-safe).
