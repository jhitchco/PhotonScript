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
3248 mm trail). A once-per-night Pushover fires if a night is armed guided but
PHD2 is not actually guiding ~20 min after dark.

## Capture-drive free space

`GET /api/sync` includes `disk` (free/total GB, % used) for the capture drive;
the Runs page sync line shows it (green/amber/red at 80 % / 92 % used).
