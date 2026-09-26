# PhotonScript — Maintenance & Operations

Operational tasks ship **in the repo** and reach the scope PC through the normal
deploy (`.\deploy\deploy.ps1 "msg"`) — never hand-copied.

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
GET /api/nina/log?lines=800&grep=Autofocus        # NINA (nina_logs_dir)
GET /api/phd2/log?lines=800&grep=GuideStep|star lost   # PHD2 GuideLog (phd2_logs_dir)
GET /api/phd2/log?kind=debug                       # PHD2 DebugLog
GET /api/ascom/log?name=Safety                     # ASCOM trace log
```

`phd2_logs_dir` defaults to `%USERPROFILE%\Documents\PHD2`; set
`PS_PHD2_LOGS_DIR` (System & Config → PHD2) if PHD2 writes its logs elsewhere.

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
- **Mount won't connect / TheSky COM3 "Error 201":** the port is locked. Kill any
  zombie TheSkyX, re-enumerate the USB in Device Manager, and connect in order
  **TheSky → NINA → PHD2** (or a clean reboot). Never let two apps own COM3.
- **Something pins the cooler at setpoint all day / warm+cooler fighting after a
  restart:** a restored armer state is re-issuing commands. Clear
  `C:\Users\jeremy\.photonscript\armer_state.json` and restart the service — do
  NOT click Disarm from RUNNING (it parks the mount as part of make-safe).
