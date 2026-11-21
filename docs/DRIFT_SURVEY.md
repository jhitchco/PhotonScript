# Drift survey (unguided tracking drift around the sky)

Status (2026-10-28): built and unit tested with fakes; NOT yet run on
hardware. Code: `photonscript/telescope_agent/drift_survey.py`, the CLI
`photonscript drift-survey`. Tests: `tests/test_scheduler/test_drift_survey.py`.

## What

The RC16 (0.236"/px) drifts unguided at about 1"/min in one steady
direction (2026-10-28, NGC 2403: trail 2.8" at 300 s, 6.1" at 450 s, 10.2"
at 600 s; ecc 0.49 / 0.78 / 0.86). The TPoint model behind ProTrack was thin
(83 fitted points, sky RMS 22.8"). The drift survey measures the drift rate
at N sky positions in a few minutes each, without long subs, so a TPoint
rebuild can be judged before / after.

Per point:

1. Open-loop slew (TheSky `SlewToAzAlt`, the tpoint-autocal script), settle
   `--settle-s` (10 s). With `--track`, sidereal tracking is turned on
   (ProTrack stays as configured). The mount is read once (read only): if it
   reads NOT tracking the run stops with "re-run with --track".
2. A `--exposure-s` (10 s) frame with TheSky's camera every `--every-s` (60
   s) for `--window-s` (180 s): frames at +0, +60, +120, +180 s. Nothing is
   sent to the mount in between (no slew, no sync, no guide pulse).
3. Each frame is plate solved (`--solver fallback`: TheSky ImageLink, then
   ASTAP on the scope PC when ImageLink fails), J2000 centre.
4. A least-squares line through RA x cos(Dec) and Dec (arcsec) against the
   exposure midpoints (start + exposure / 2) gives the drift per axis in
   "/min, the total rate, its direction (position angle, north through east)
   and the residual RMS. All solves are J2000 (one frame), so precession does
   not matter over minutes.
5. From the rate: the smear of 300 / 600 / 900 / 1200 s subs (rate x t), the
   predicted eccentricity at `--fwhm-arcsec`, and the longest standard sub
   that keeps the smear at or under `--max-smear-arcsec`.

Reused from tpoint-autocal (docs/TPOINT_AUTOCAL.md): the preflight (armer
DISARMED, auto-arm off, both NINAs idle or `--stop-nina2`, no calibration
capture, roof safe, dark, TheSky answering, the park state), NINA #1's filter
wheel to `--filter` (L), the camera lent from NINA #1 to TheSky and ALWAYS
handed back (also after Ctrl+C or an error), bin 1 / 10 s, `--unpark`,
`--track`, the solvers, the point picker (equal-area grid, both sides,
meridian band / pole / moon skipped, alt >= 30, a point left out when its
whole window would take it below 30 deg or into the meridian band).

Never syncs, never parks, never touches TPoint (no backup, no import file,
no AutoMap, no model change), never changes ProTrack. Every script it sends
goes through a guard that refuses sync / park / ClosedLoopSlew / AutoMap /
TPoint / SetDocumentProperty / ProTrack.

## How to run

At the scope PC (a console Jeremy watches; Ctrl+C stops safely):

| What | Command |
|---|---|
| Plan, timing, scripts; sends nothing | `photonscript drift-survey --dry-run --status --label before` |
| Baseline, 10 points (~46 min) | `photonscript drift-survey --go --label before --unpark --stop-nina2` |
| Same, mount not tracking after the slew | add `--track` |
| Remote (job killed at 30 min: keep it short) | `photonscript remote --confirm-write --remote-timeout 1800 -- drift-survey --go --unattended --points 5 --label before --unpark --stop-nina2` |

Options: `--points` (10, 1..60), `--side east|west|both`, `--window-s`
(180), `--every-s` (60; must leave 15 s after the exposure for download and
solve), `--exposure-s` (10), `--binning` (1), `--filter` (L; none = leave the
wheel), `--settle-s` (10), `--max-smear-arcsec` (2.0), `--fwhm-arcsec` (3.5),
`--solver imagelink|astap|fallback` (fallback), `--label` (free text, stored
in the JSON and the file names), `--unpark`, `--track`, `--stop-nina2`,
`--unattended` (no console question), `--max-run-min` (local only: stop
starting points after N min; with `--unattended` the default is 28 and a
plan longer than that is refused, because a remote job is killed at 30 min
before the camera hand-back), `--slew-timeout`, `--verbose`, `--status`,
`--json`.

Timing: about slew (~45 s) + settle + window + exposure + solve (~25 s) a
point, plus 90 s for the pier flip: ~270 s a point with the defaults.

## How to read it

While running, one line per point:

    point 3 (east, alt 57 az 73): 1.04"/min (RA*cos(Dec) +0.85, Dec -0.60, PA 125 deg),
    resid 0.10" over 4 solves / 180 s; smear 300 s 5.2", 600 s 10.4", ...; longest sub 90 s

- The rate is the drift on the sky in arcsec per minute. RA*cos(Dec) > 0 is
  east, Dec > 0 is north; PA is the drift direction from north through east.
- The residual says how straight the drift is (solve noise is ~0.1-0.3").
  A residual much larger than that: wind, a slipping axis, or a bad solve.
- Longest sub: the longest standard length (60 ... 1800 s) whose smear stays
  under `--max-smear-arcsec`.

The summary: the TPoint model in force (date, points, sky RMS, from TPoint's
own files) so before / after runs can be compared; the median and worst
rate, the mean drift direction and how well the points agree (1 = all the
same way: a model / polar term; low = local errors); the smear of each sub
length at the worst and the median rate; the RECOMMENDED unguided sub (worst
point, i.e. good everywhere surveyed) and the typical one (median); a table
by side and by region (alt band 30-45 / 45-60 / 60-90 x side).

The smear limit: `--max-smear-arcsec` 2.0 by default. The eccentricity is
predicted for a Gaussian star of `--fwhm-arcsec` smeared by a line of the
smear's length (second moments s^2 and s^2 + L^2 / 12). At 3.5" FWHM that
gives 2.8" -> 0.48, 6.1" -> 0.76, 10.2" -> 0.89, against 0.49 / 0.78 / 0.86
measured on 2026-10-28; so the 2.0" default is ecc ~0.36, and a limit of 2.8"
is ecc ~0.5.

Files (`<data_dir>\runs\`), stem `<night>_drift_survey_<UTC HHMM>Z[_<label>]`:

- `<stem>.csv`: one row per frame (start, midpoint, solver, solved centre,
  offset from the first solve in arcsec).
- `<stem>_fits.csv`: one row per point (rates, PA, residual, smear and ecc
  per sub length, longest sub, region).
- `<stem>.json`: the run (label, model, parameters, per-point fits with alt /
  az / HA / Dec, the summary), rewritten after every point, so a later
  sky-map plot can read it.

## Tonight (2026-10-28): before, TPoint, after

0. TheSky refraction air (read only): `photonscript thesky-audit
   --discover-docprops` at the scope PC (or from the desktop:
   `photonscript remote -- thesky-audit --discover-docprops`) prints TheSky's
   `sky6StarChart.DocumentProperty(0..120)` next to NINA #1's weather and the
   site line TheSky wrote into TPoint's input file. Compare with TheSky's own
   dialog, pin the indices (`PS_THESKY_DOCPROP_PRESSURE` /
   `_TEMPERATURE` / `_REFRACTION`, System page, PHD2 group), then
   `photonscript thesky-audit`: the `site_pressure` row wants ~870 hPa (a
   sea level 1013 over-corrects refraction by ~16%, ~15" at 30 deg alt).
   Fix it in TheSky BEFORE the baseline if it is wrong, so before and after
   differ only by the model.
1. Baseline: `photonscript drift-survey --go --label before --unpark
   --stop-nina2` (add `--track` if it stops for "NOT tracking").
2. Dense all-new TPoint run: `photonscript tpoint-autocal --go ...`
   (docs/TPOINT_AUTOCAL.md), then at the console: TPoint, Module Settings >
   Import Pointing Data, refit (Super Model), check the RMS, save, Apply
   pointing corrections / ProTrack.
3. After: the same command with `--label after` (same `--points` / `--side`:
   the grid is in alt / az, so both runs sample the same alt / az
   positions). Compare the two `.json` summaries (median / worst rate,
   recommended sub, the model header).

## TheSky refraction temperature sync (2026-10-28)

TPoint's input files show TheSky refracting with 20 to 38 C (an indoor /
electronics value) while AARO nights run ~0 to 15 C: 25 C too warm
under-corrects refraction by ~8% (~7" at 30 deg alt), for ProTrack and the
pointing corrections. The pressure is right (~873 hPa, `site_pressure` PASS).

- While a night is armed (armer RUNNING or PAUSED_UNSAFE, from pre-config to
  the dawn shutdown), every `thesky_temp_sync_min` (default 45) minutes the
  armer reads the focuser probe (NINA #1 `equipment/focuser/info`
  Temperature, else NINA #2; NaN / disconnected / outside -30..45 C skipped)
  and writes it into TheSky with `sky6StarChart.SetDocumentProperty(<i>,
  T)` at `i = thesky_docprop_temperature`, then reads it back.
- Skipped when TheSky already reads within 2 C, when TheSky (IsSlewComplete)
  or NINA #1 (mount Slewing) says the mount slews or the state cannot be
  read, and when no focuser gives a plausible value.
- Unpinned (-1, the default) = nothing is written; one line per night says
  "temperature sync skipped: docprop unpinned". The index is never guessed,
  and a pin that is a known site property (latitude, longitude, time zone,
  elevation, ...), equals the pressure / refraction pin, or reads something
  that is not a temperature now is refused.
- Each write (and a refusal / failure) is a kind `thesky_temp_sync` line in
  `runs/<night>_events.jsonl` (before, after, sensor) and a log line.
- Kill switch: `PS_THESKY_TEMP_SYNC=false`. Code:
  `scheduler/thesky_temp_sync.py` (the write is kept out of
  `thesky_client.READ_ONLY_JS`, which stays read only).
- Audit: the `site_temperature` row compares TheSky's value with the same
  focuser reading (else NINA #1's station) and WARNs past 5 C, showing both.
  Unpinned, it shows the TPoint input file's value as info.

To turn it on: run the discovery (remote is fine), find the index whose
value matches TheSky's Location dialog temperature, set
`PS_THESKY_DOCPROP_TEMPERATURE=<i>` (System page, PHD2 group). The next
armed night syncs; `photonscript thesky-audit` shows the row.
