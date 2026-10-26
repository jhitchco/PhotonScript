# Pushover v2: the night storyline (PS-212, 2026-10-24)

The phone tells the night as a short story (the plan, the milestones, the
problems a human can fix) instead of one push per event. Before v2 the week
of 2026-10-16 to 10-23 sent 314 pushes (30 to 56 on an imaging night, plus
NINA's own unthrottled "Send to Pushover" items on top). Replayed through v2
the same week makes 6 to 16 a night, median 11, counting every story push v2
adds (see "Replay" below).

## The three kinds

Every `notify()` (shared/pushover.py) carries `kind=`:

| kind  | what | pushed |
|-------|------|--------|
| `act` | needs Jeremy: a human could really help | before `pushover_act_cutoff_local` (00:00), priority >= 1, with "If you act: ... If not: ..." (ACT_CONSEQUENCES, per title) |
| `act` + `safety=True` | unsafe while not parked, camera / cooler dead after recovery, disk / sync failure, arm / dispatch / deploy blocked, PhotonScript down, a test push | always, any hour, never folded |
| `story` | the night's milestones | priority 0, once per fact per night |
| `info` | everything else (per sub, waits, retries, recoveries that worked) | never: the storyline and the audit log only |

No kind means `info` (opt in to noise, not out). From the cutoff until
`pushover_act_resume_local` (08:00) a non-safety act is held: it stays in the
storyline (`reason: act-after-cutoff`) and the end-of-night report lists it
under "Held overnight for you".

Once per fact: a story with `fact=` once per night per fact, else per title
and message with digits masked; an act once per title per night (the next
"Settings wrong" or "NANNY" of the night goes to the storyline). Safety acts
skip this rule (the old 300 s identical-text dedup still applies).

Arm-time acts wait for the plan: from the arm until the plan heads-up goes
out (or astro dark) a non-safety act is held (`reason: for-plan`) and the
heads-up lists it under "Needs you before pre-config HH:MM"; those facts are
then marked told for the night. The arm's checks (settings ledger, ProTrack,
TPoint preflight, PHD2 audit, apps not running) make one push, not five.

The old limiter still runs after the kind policy: daytime quieting (PS-50),
the 20 per hour burst cap, the monthly cap.

## The storyline

`<data_dir>/night_story/<night>.jsonl` (night = local evening date, noon to
noon): one line per decision, pushed or not:
`{"t", "kind", "title", "text", "sent", "reason", "priority", "safety"?,
"fact"?, "thumb"?}`. `GET /api/night/story?night=&kinds=act,story&sent_only=`
returns it (`entries`, `pushed`, `by_kind`); the dashboard's **Tonight**
panel shows the pushed entries (and held acts) newest first, thumbnails
inline, "show logged-only entries" for the rest. `GET
/api/pushover/weekly?days=7` counts pushes per night and kind (pre-v2 audit
rows count as `legacy`), the top titles and the held reasons, to tune the
noise with numbers.

## The story pushes (scheduler/night_story.py)

| push | when | text |
|------|------|------|
| Plan heads-up (`PhotonScript tonight`) | `pushover_plan_lead_min` (90) before pre-config, at once for an arm inside that lead; once per plan (a re-arm with the same targets / windows / mode sends nothing); not after astro dark | imaging window, pre-config time, "Change the plan before", "Needs you before pre-config" (held acts), RC16 guided / unguided with each target's window and sub lengths, the Piggy-600 lengths, moon, TPoint model and preflight, ProTrack (unguided), cooler gate off, calibration owed, the evening forecast line |
| Imaging now (`PhotonScript imaging`) | once, the first armer tick after astro dark with the roof open | first target and sets, guided / unguided, the Piggy line, targets until dawn, planned hours |
| First light (`PhotonScript first light`) | the first kept (passed QA, not a test) light per rig | target, filter, length, HFR, ecc, stars; the sub's thumbnail attached (720 px), its URL in the storyline |
| Target switch (`PhotonScript target`) | the RC16 (the mount; the Piggy rides it) starts a new target | new target and sub, the previous target's kept / rejected / hours |
| Roof (`PhotonScript roof`) | debounced: a closure is info; the reopen after `pushover_roof_debounce_min` (10) or more is one story with its length and the dark left | "Roof reopened 23:22 after 1 h 12 min closed (since 22:10); imaging resumes, 5.1 h of dark left." |
| Flats (`PhotonScript flats`) | flats v2 (dusk_flats.maybe_report_windows): once per dusk and per dawn window, 12 min after the window's end (fact `flats:<night>:<phase>`). Exactly one push carries each count: the dusk count rides in "imaging now" when that push is still to come, the dawn count in the end-of-night report while it is held; otherwise it is its own story | the frames each rig actually saved per filter, or "none saved (reason)" |
| End of night (`PhotonScript night report`) | as soon as imaging is over: the dawn shutdown (after the dawn flat window; when a flats v2 dawn window is still to be counted the report waits for that count, about 10 min, at most 30), or the roof closed with less dark left than a resume needs (40 min); once per night | per rig and target subs shot / kept / rejected with the top reject reason, hours added and goal progress; median HFR / ecc and the sub doctor's verdict per rig; the roof timeline; dusk and dawn flats; darks saved; held acts; calibration owed; the split note; the best kept sub (or first light) attached |
| Dawn filing follow-up (`PhotonScript library`) | only when the kept count moved by 5+ subs and 10%+ against the end-of-night report (or no report went out); a failed filing step is a safety act | the filing line |
| Others | skipped night / skipped flats (once per reason), unguided fallback ran, goal complete (campaign), TPoint run finished, desktop integration done | as before |

The old dawn "PhotonScript complete" push and the "ARMED" / "running" /
"PAUSED" / "RESUMED" pushes are info now (the end-of-night report, the plan
heads-up, "imaging now" and the roof story say the same thing once).

## Watchers stay quiet with a known reason

`night_story.quiet_reason(config)` returns why "nothing happened in N min"
is expected, or None: a TPoint autocal run writing its CSV or import files
(the last 3 h), a watched sideload, a calibration capture, the roof closed
(safety history), the night disarmed by hand. The TPoint watcher's "run
started but no samples" (the 2026-10-23 false alarm: an empty TPoint file
opened by hand while tpoint-autocal wrote import files, not in.dat) and the
NINA watchdog's silent / stuck / parked alarms ask it first and record the
alarm with `reason: quiet-known-reason` instead of pushing. The frozen
sensor watch is info (34 pushes in the week, warning only).

## NINA's own Send to Pushover items

Generated sequences (nina_sequence_json, calibration, flat_cover) no longer
carry GroundStation "Send to Pushover" items: `_pushover()` returns an
Annotation `[<title>] <text>` (still visible in NINA's sequence view) unless
`nina_pushover_items` is on. Checked: every one of the 58 items narrates a
step PhotonScript already sees (NINA's running item in the run events, the
safety monitor, the subs log, dusk_flats_check), and none covers a case
where PhotonScript cannot speak: when PhotonScript is down the supervisor
pushes "PhotonScript is down / crashed" (safety act), and when the scope PC
is off the network NINA's push cannot leave either. The one warning among
them (PS-61 "cooler gate OFF tonight") moved into the plan heads-up.

## Settings

| key | default | |
|-----|---------|-|
| `pushover_kinds` | `act,story` | kinds pushed (info never; a safety act always) |
| `pushover_plan_lead_min` | 90 | plan heads-up lead before pre-config |
| `pushover_act_cutoff_local` | `00:00` | from here only safety acts |
| `pushover_act_resume_local` | `08:00` | acts push again from here |
| `pushover_roof_debounce_min` | 10 | shorter closures get no reopen push |
| `nina_pushover_items` | false | NINA's own push items in generated sequences |

The auto-arm's preflight no longer sends its test push (one per auto-arm);
the dashboard / CLI preflight still does.

## Call-site mapping

Every `notify()` call site and the kind it carries now. "act/safety" = a
safety act. Conditions are the code's own.

| module | push (title) | kind |
|--------|--------------|------|
| scheduler/armer.py | armed, running, disarmed, restored, restarted, restart refused, watching started / stopped / paused / resumed, paused / resumed (unsafe, operator), RESUMED after a safety stop | info |
| scheduler/armer.py | complete (the shutdown summary) | info; the end-of-night report is the story |
| scheduler/armer.py | apps not running (at arm, pre-config - 60), guider is Direct Guider on a guided arm, ProTrack not on (unguided arm), dispatch gate fallback, arm recipe fallback, Piggy companion not started (lint / NINA #2), pause / resume FAILED, PHD2 failed and the fallback is not allowed | act |
| scheduler/armer.py | guiding: first "not guiding" warning, "STILL not guiding" escalation | act |
| scheduler/armer.py | guiding: short loss, flap summary, auto-recovery restart | info |
| scheduler/armer.py | cooler mismatch, cool command FAILED, still warm after N min | act |
| scheduler/armer.py | cooler was OFF, turned on | info |
| scheduler/armer.py | safety monitor unreadable (watchdog, dropout past grace) | act/safety while dark, act after astro dawn |
| scheduler/armer.py | safety monitor readable again | info |
| scheduler/armer.py | safety cross-check (NINA #1 unsafe, NINA #2 safe) | act |
| scheduler/armer.py | Dispatch FAILED (both paths), cooler STILL ON after the dawn check, watched night still running at dawn / park or cooler check failed / still imaging while unsafe | act/safety |
| scheduler/armer.py | SAFETY STOP | info when the park was ok, else act/safety |
| scheduler/armer.py | unsafe while paused by the operator | info when the park was ok, else act/safety |
| scheduler/armer.py | TPoint preflight | act on a block, else info (the heads-up carries it) |
| scheduler/armer.py | TPoint coverage, focus calibration note, recipe note, PHD2 calibration invalid (re-dispatched with a slot), unguided dithers off, Piggy started, OSC darks unconditional | info |
| scheduler/armer.py | guiding abandoned, re-dispatched unguided | story |
| scheduler/auto_armer.py | evening forecast | info (its title rides in the plan heads-up) |
| scheduler/auto_armer.py | auto-arm skipped / auto flats skipped (skip_and_notify) | story, once per reason |
| scheduler/auto_armer.py | auto-arm skipped (preflight go=false), auto-armed with preflight failed | act |
| scheduler/app.py | manual make-safe | info |
| scheduler/app.py | dashboard test push | act/safety |
| scheduler/app.py | transfer STALLED | act/safety |
| scheduler/preflight.py | preflight test push | act/safety (the auto-arm sends none) |
| scheduler/routers/apps.py | observatory apps start incomplete | act |
| scheduler/routers/integrations.py | "<goal> integrated" (desktop) | story |
| integration/autointegrate.py | integration summary with the review JPG | story |
| integration/runner.py | PixInsight hang; startup crash on both attempts (PS-31) | act |
| integration/runner.py | startup crash retried with software rendering | info (run log only) |
| scheduler/campaign.py | goal complete / only lacks calibration | story |
| scheduler/calibration_capture.py | capture started, bias drift, capture complete / cancelled | info |
| scheduler/calibration_capture.py | capture aborted | act |
| scheduler/cooler_gate.py | not imaging, sensor off setpoint | act |
| scheduler/cooler_gate.py | sensor back, imaging resumed | info |
| scheduler/flat_cover.py | cover not open, image pass skipped | act |
| scheduler/flat_cover.py | cover open again | info |
| scheduler/dawn_autofile.py | library filed | story only when kept moved materially, else info; act/safety when a step failed |
| scheduler/dawn_mount_check.py | mount not parked after dawn | act/safety |
| scheduler/dusk_flats.py | auto dusk flats dispatched | info (the flats story gives the counts) |
| scheduler/dusk_flats.py | flats v2 window report "Dusk flats <night>: ..." / "Dawn flats <night>: ..." | story, once per window |
| scheduler/focus_model.py | focus offsets | info |
| scheduler/focuser_guard.py | focuser reset (counter dropped to about 0 / jumped, both rigs), restore did not finish | act (not safety: data, not hardware; held 00:00-08:00) |
| scheduler/focuser_guard.py | restore done, counter-only verdict, advise outcome | info |
| scheduler/focus_reports.py | autofocus reports bad | act |
| scheduler/frozen_sensor.py | frozen sensor | info |
| scheduler/guide_blocks.py | guiding per block | info |
| scheduler/night_pilot.py | needs a human, ALERT (PHD2 on an unguided night), FAILED to act, restart that did not happen, no subs after an action, "did NOT help" with a human step | act |
| scheduler/night_pilot.py | PS-228 roof closed mid-night with darks owed (once per closure) | act |
| scheduler/night_pilot.py | advise, acted, optics reading, "worked", "did NOT help" without a human step | info |
| scheduler/nina_offline.py | NINA down past the grace | act (act/safety for the RC16) |
| scheduler/nina_offline.py | NINA back | info |
| scheduler/nina_watchdog.py | not running / API down / silent / stuck / parked while armed | act (act/safety for the RC16 not running / API down); a known reason keeps silent / stuck / parked quiet |
| scheduler/nina_watchdog.py | outside an armed night, OK again | info |
| scheduler/nina_validation.py | load validation | act/safety when fatal, else info |
| scheduler/nina2_mount_check.py | NINA #2 holds mount items | act while in the loop and not replaced, else info |
| scheduler/off_target.py | OFF TARGET | act |
| scheduler/phd2_audit.py | PHD2 settings at arm / changed | act |
| scheduler/piggy_guard.py | Piggy lights on a mount that does not track | act |
| scheduler/protrack_recheck.py | ProTrack still off | act |
| scheduler/settings_ledger.py | settings wrong | act |
| scheduler/stuck_loop.py | stuck loop: restarted by the pilot | info |
| scheduler/stuck_loop.py | stuck loop: advise / alert | act |
| scheduler/tpoint_watch.py | TPoint run finished | story |
| scheduler/tpoint_watch.py | TPoint run started but no samples | act; quiet with a known reason |
| scheduler/trends.py | trend alert | info |
| scheduler/unguided_guard.py | guider connected and disconnected, guiding stopped by the guard | info |
| scheduler/unguided_guard.py | could not stop / disconnect, policy alert, sequence alert | act |
| shared/cooler_recovery.py | recovered, still stalled with a reconnect left | info |
| shared/cooler_recovery.py | still stalled after the last reconnect, reconnect errored at the last attempt | act/safety, with the hours a power cycle saves |
| shared/cooler_recovery.py | reconnect errored before the last attempt | act |
| shared/supervisor.py, shared/updater.py | PhotonScript is down / crashed, update refused, rolled back | act/safety |
| telescope_agent/agent.py | NANNY severe | act (consecutive rejects: info while the night pilot is on, its push says it with the fix) |
| telescope_agent/agent.py | NANNY non-severe, heartbeat | info |
| telescope_agent/agent.py | sequence stopped by the nanny | act/safety |
| telescope_agent/agent.py | focuser at 0, autofocus not finishing, PHD2 not correcting, mount refuses pulses, guide guard | act |
| telescope_agent/agent.py | first light, target switch (after each graded sub) | story (night_story.after_sub) |
| telescope_agent/guide_tuner.py | guide star faint | info |
| telescope_agent/phd2_calmanager.py | calibration outside the slot FAILED | act |
| telescope_agent/pulse_selftest.py | self-test failed, Dec reversed after the flip | act |
| telescope_agent/sub_doctor_live.py | sub doctor guider_conflict / focuser | act |
| telescope_agent/sub_doctor_live.py | other causes | info (the night pilot acts; the end-of-night report has the verdict) |
| (quiet carriers) | RC16 on an unguided moon-fill carrier: sub doctor (not guider_conflict), night pilot RC16 actions and "needs a human", reject streak | none (run events and records only); RC16 AF stall / AF report / sub doctor focuser info unless the Piggy-600 fails too (scheduler/carrier_quiet.py) |
| telescope_agent/tpoint_autocal_live.py | autocal run notes | story |
| cli.py | `photonscript notify` | `--kind` (act), `--safety/--no-safety` (on) |
| `record()` callers (held pushes) | | info |

## Sample texts, a typical night

Plan heads-up, 17:28 (pre-config 18:58):

    Tonight 2026-10-24: imaging 19:28-05:28 (10.1 h dark), pre-config 18:58. Change the plan before 18:58.
    Needs you before pre-config 18:58: ProTrack: Armed UNGUIDED but ProTrack is OFF in TheSky (OFF): unguided subs trail without it. If you act: tick Activate ProTrack in TheSky and unguided subs stay round. If not: unguided subs trail and are rejected.
    RC16 unguided (TPoint + ProTrack): NGC 604 19:28-22:28 (Ha/OIII 20x300s); M33 22:28-03:28 (L 30x180s, R/G/B 10x120s); Heart Nebula fill 03:28-05:28
    Piggy-600: OSC 450s riding the RC16
    Moon 95%: up at dusk, sets 04:10, broadband from 04:10
    TPoint: TPoint model 2026-10-23: 26 points, sky RMS 14.44", polar 0.79'
    Calibration owed: RC16 flats Ha, OIII (dawn); Piggy-600 darks 450 s x20
    Tonight: GREEN - 10.2/10.2h usable

Dusk flats, 19:05 (flats v2 window report): `Dusk flats 2026-10-24: RC16 L 15, B 15, ...; Piggy-600 OSC 25`

Imaging now, 19:28: `Imaging now (19:28): RC16 unguided on NGC 604 (Ha/OIII 20x300s). Piggy-600: OSC 450s riding the RC16. 2 target(s) until 05:28, ~8.2 h planned.`

First light, 20:14 (thumbnail attached): `First light RC16 (20:14): NGC 604 Ha 300s, HFR 2.31 px, ecc 0.42, 1240 stars.`
and 20:21: `First light Piggy-600 (20:21): NGC 604 OSC 450s, HFR 3.05 px, ecc 0.47, 3810 stars.`

An act before midnight, 21:40: `Piggy-600 cooler STILL stalled after camera reconnect 2/2 (sensor 23.9 C, setpoint 0 C, TEC 0 %); no more reconnects tonight. If you act: a camera power cycle saves ~7 h of Piggy-600 data. If not: the RC16 still images and the Piggy sits out.`

Target switch, 22:30: `RC16 now on M33 (L 180s, 22:30) after NGC 604: 31 kept / 4 rejected, 2.6 h.`

Roof, 01:52: `Roof reopened 01:52 after 1 h 12 min closed (since 00:40); imaging resumes, 3.6 h of dark left.`

End of night, 06:40 (best sub attached):

    Night 2026-10-24 over (dawn, 06:40): 92 kept of 104 subs, 7.4 h added.
    RC16: NGC 604 35 shot, 31 kept, 4 rej (top ecc 3), +2.6 h (goal 14.9/20 h); M33 41 shot, 38 kept, 3 rej (top hfr 2), +1.9 h (goal 6.1/12 h)
    RC16 stars: median HFR 2.28 px, ecc 0.41, sub doctor: healthy
    Piggy-600: NGC 604 23 shot, 23 kept, 0 rej, +2.9 h
    Piggy-600 stars: median HFR 3.02 px, ecc 0.46, sub doctor: healthy
    Roof: closed 00:40-01:52 (1 h 12 min)
    Dusk flats 2026-10-24: RC16 6 shot, 6 filed; Piggy-600 5 shot, 5 filed
    Dawn flats 2026-10-24: RC16 12 shot, 0 filed (12 waiting for the dawn sweep)
    Held overnight for you: 03:10Z Night pilot: needs a human: ...
    Calibration owed: Piggy-600 darks 450 s x20

## Replay (tests/test_scheduler/test_ps212_pushover_v2.py)

The scope's audit log of 2026-10-16 to 10-24 (GET /api/notifications,
427 records, 314 sent; tests/test_scheduler/fixtures/ps212) through the v2
policy at each record's own time, each record mapped to its call site's kind
(tests/test_scheduler/ps212_replay.py), plus the story pushes v2 adds (an
upper bound: every armed target reached, both rigs' first light):

| night | pushes before | v2 replayed + story = total |
|-------|---------------|-----------------------------|
| 10-16 (shakedown, two failed dispatches) | 39 | 10 + 6 = 16 |
| 10-17 | 56 | 6 + 6 = 12 |
| 10-18 | 30 | 3 + 6 = 9 |
| 10-20 | 52 | 4 + 7 = 11 |
| 10-21 | 42 | 3 + 4 = 7 |
| 10-22 | 36 | 2 + 4 = 6 |
| 10-23 (TPoint night, armed after dark) | 44 | 8 + 5 = 13 |

No fact is pushed twice in a night, every safety act is still sent, nothing
non-safety buzzes between 00:00 and 08:00, and the 10-23 TPoint watcher
false alarm stays quiet.
