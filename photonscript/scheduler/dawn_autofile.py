"""PS-157: at dawn the night files itself into the Library, no manual review.

2026-10-07: nothing from 2026-10-05 and 2026-10-06 reached the desktop.
(1) The Piggy-600 subs stayed target "?" (NINA #2 owns no mount, so its
frames carry no RA/DEC; POST /api/runs/{date}/identify made ONE plate-solve
cluster of the whole night, 03:52-11:28, and matched nothing), and (2) every
sub sat in review, and build_library links approved subs only. Both were
fixed by hand (assign_target by the RC16 target windows, then approve).

file_night() does that at dawn (runs.post_night_warm, the shutdown and the
watched-night paths both call it):

1. Attribution. attribute_night() names what it can (header RA/DEC, then the
   RC16 timeline below inside identify, PS-137, the PS-51 correlation). Piggy
   subs the timeline found ambiguous (no RC16 target over most of the sub,
   not parked, not slewing) go to the PS-137 per-sub solve.
2. The pointing pass and the PS-13 slew gate run before anything is
   approved, so a straddler or an off-target sub is rejected first.
3. Auto-approve (auto_approve_at_dawn, default on): every QA-passing sub
   still waiting for review, both rigs, unless a person sent it back to
   review, it is a test / calibration sub (PS-152 is_test_record), or its
   target is still "?". Marked review_source "auto" (a later automatic
   reject still applies) plus approved_by "dawn".
4. build_library(date) so Syncthing gets the night, goal progress synced.
   PS-178: then the calibration sweep (runs.link_recent_calibration) files
   every calibration folder of the last CAL_SWEEP_DAYS on both rigs, so a
   calibration-only folder (daytime capture, roof-closed night, NINA
   restarted after midnight) reaches the Library and the desktop too.
   PS-182: and the readout fill (calibration_qa.fill_missing_readouts, up
   to calibration_readout_fill_at_dawn records): QA records of darks / bias
   with no readout recorded get their READOUTM from a header read, so the
   July LCG bias stops counting as HCG. Then the learned unguided limits
   refresh with the night in (scheduler/unguided_limits.py).
5. One record per night, runs/<date>_autofile.json, and the line
   "Library: N subs filed for sync (targets...)" for the morning push, the
   runs page and GET /api/runs/{date}/autofile.

RC16 target timeline (rc16_timeline / attribute_timeline_records). The RC16
subs of the night (canonical target, start from the file name / DATE-OBS,
plus exp_s) are merged into per-target segments (same name, gap at most
MERGE_GAP_MIN). Each segment is widened over the mount-log tracking span it
sits in (the mount stays on M31 after the RC16's last sub until the next
slew or park), else by PIGGYBACK_CORRELATE_TOL_MIN, never past a neighbour.
A Piggy "?" sub's exposure [start, start + exp] then gets:

  parked    the mount log says parked / not tracking over half of it: stays
            "?" (never enters a goal; PS-158 stops these at capture)
  target    a segment covers at least TIMELINE_MAJORITY of it: its name,
            test names included (PS-152: a test name never credits a goal)
  run       2026-10-24: no single segment holds the majority but the span
            of one calibration run (TPoint mapping: one short segment per
            point, the slews between them are the run too, first point to
            last) does: the run's name
            ("TPoint mapping"), marked test + calibration_run, so it never
            becomes a target, a goal or the '?' card (night 2026-10-09: 26
            Piggy subs rode along the PS-171 run and stayed '?')
  slewing   it overlaps a slew and no target holds the majority: stays "?"
  ambiguous none of the above: "?", the dawn pass tries the PS-137 solve

A sub assigned by hand (target_src "manual") or already named is never
touched. Evidence goes to rec["timeline"]; a renamed sub keeps target_raw
and gets target_src "rc16-timeline". Nothing here writes a FITS file or
touches the desktop mirror.

Dawn filing fix (2026-10-11). Night 2026-10-09 (armed with the TPoint
recipe, then left DISARMED) was never filed: file_night only started from
the armer's dawn shutdown / watched-night end (runs.post_night_warm), which
files night_of(now) only, so a night the armer no longer owned at dawn (or
whose filing thread a restart killed) stayed in review for good. And its
Piggy subs would have stayed "?" anyway: NINA #1 lost the mount at 03:08Z
and the mount log took the disconnected poll (RA 0, Dec 0, no alt / az) as
an "idle" mount, so every later sub was held as parked and never solved.

  * sweep / run_sweep (app startup, every SWEEP_TICK_S): every armed night
    (runs/<night>_plan.json, written at dispatch) of the last
    dawn_filing_catchup_days with a subs log and no filing record is filed
    once, SWEEP_AFTER_DAWN_MIN after its dawn, unless the armer still owns
    that night (its own dawn shutdown files it). The record is the "once".
  * mount_offline: a disconnected poll ends the mount state; the time after
    it is unknown (ambiguous, so the PS-137 per-sub solve runs).
  * plan_fallback_records: a Piggy sub still "?" after the timeline and
    the solve takes the plan target whose window (plan snapshot) covers
    most of it, only when the mount log shows it tracking over most of the
    sub AND the mount position is within PLAN_CONFIRM_DEG of that target.
    No evidence, no name: a "?" sub is never approved.
  * preview_night: the dry run of the CLI `photonscript dawn-file`.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.shared.fileops import replace_retry

logger = logging.getLogger(__name__)

PIGGY = "piggyback"
RC16 = "rc16"
MERGE_GAP_MIN = 30.0       # same RC16 target, gap at most this: one segment
TIMELINE_MAJORITY = 0.5    # a segment must cover this much of a Piggy sub
PARKED_MAJORITY = 0.5      # parked / not tracking over this much: held
SRC = "rc16-timeline"
APPROVED_BY = "dawn"
WAIT_BACKFILL_MAX_S = 3 * 3600.0   # dawn filing waits for a grading pass
WAIT_POLL_S = 30.0
HOLD_STATES = ("parked", "slewing")
PLAN_SRC = "plan-window"
PLAN_CONFIRM_DEG = 1.0     # mount position to plan target, at most this
SWEEP_TICK_S = 600
SWEEP_AFTER_DAWN_MIN = 90.0   # after the armer's own shutdown (dawn + ~75)
# armer states whose own dawn shutdown files their night
LIVE_STATES = ("ARMED", "RUNNING", "PAUSED_UNSAFE", "PAUSED_OPERATOR",
               "WATCHING")

_RUNNING: dict[str, bool] = {}


def enabled(config) -> bool:
    return bool(getattr(config, "auto_approve_at_dawn", True))


# ------------------------------------------------------------------ timeline

def _span(rec: dict, config) -> tuple[datetime, datetime] | None:
    from photonscript.scheduler.phd2_analysis import sub_start_utc
    st = sub_start_utc(rec, config)
    if st is None:
        return None
    try:
        exp = max(0.0, float(rec.get("exp_s") or 0.0))
    except (TypeError, ValueError):
        exp = 0.0
    return st, st + timedelta(seconds=exp)


def _overlap(a0, a1, b0, b1) -> float:
    return max(0.0, (min(a1, b1) - max(a0, b0)).total_seconds())


def _is_piggy(rec: dict) -> bool:
    return (rec.get("rig") or RC16) not in (RC16, "")


def rc16_timeline(config, subs: list[dict],
                  lines: list[dict] | None = None) -> list[dict]:
    """The RC16 target segments of a night: [{target, start, end, subs,
    first, last, test, src}] time-ordered (datetimes naive UTC). first/last
    are the RC16 subs' own span; start/end are widened (module doc)."""
    from photonscript.scheduler.runs import PIGGYBACK_CORRELATE_TOL_MIN
    from photonscript.shared import mount_log
    from photonscript.shared.target_names import (canonical_target,
                                                  is_test_target, target_key)
    spans = []
    for s in subs:
        if _is_piggy(s):
            continue
        name = canonical_target(s.get("target"))
        sp = _span(s, config) if name else None
        if sp:
            spans.append((sp[0], sp[1], name))
    spans.sort(key=lambda x: x[0])
    gap = timedelta(minutes=MERGE_GAP_MIN)
    segs: list[dict] = []
    for st, en, name in spans:
        last = segs[-1] if segs else None
        if (last and target_key(last["target"]) == target_key(name)
                and st - last["last"] <= gap):
            last["last"] = max(last["last"], en)
            last["subs"] += 1
            continue
        segs.append({"target": name, "first": st, "last": en, "subs": 1,
                     "test": is_test_target(name)})
    track = [g for g in mount_log.segments(lines or [])
             if g["state"] == "tracking"]
    tol = timedelta(minutes=PIGGYBACK_CORRELATE_TOL_MIN)
    for i, g in enumerate(segs):
        prev_end = segs[i - 1]["last"] if i else None
        next_start = segs[i + 1]["first"] if i + 1 < len(segs) else None
        lo, hi, src = g["first"] - tol, g["last"] + tol, "rc16-subs"
        t_hi = next((t for t in track if t["start"] <= g["last"] <= t["end"]),
                    None)
        t_lo = next((t for t in track if t["start"] <= g["first"] <= t["end"]),
                    None)
        if t_hi is not None:
            hi, src = max(g["last"], t_hi["end"]), "mount-log"
        if t_lo is not None:
            lo, src = min(g["first"], t_lo["start"]), "mount-log"
        # never past a neighbour: split the gap in the middle
        if next_start is not None and hi > next_start:
            hi = max(g["last"], g["last"] + (next_start - g["last"]) / 2)
        if prev_end is not None and lo < prev_end:
            lo = min(g["first"], prev_end + (g["first"] - prev_end) / 2)
        g.update(start=lo, end=hi, src=src)
    return segs


def mount_offline(r: dict) -> bool:
    """A poll of a mount NINA #1 no longer has: RA 0, Dec 0 and no alt / az
    (2026-10-10 03:08Z, logged as an "unpark" to an idle mount). It says
    nothing about where the mount is. The logger now writes an explicit
    "offline" marker instead (shared.mount_log.is_offline reads both)."""
    from photonscript.shared.mount_log import is_offline
    return is_offline(r)


def _mount_states(lines: list[dict] | None,
                  end: datetime | None = None) -> list[dict]:
    """The mount log's state spans. The logger writes a line on a change
    (and a heartbeat only while tracking), so the night's last state holds
    to `end` (the last sub) unless it is tracking (stale after STALE_S).
    A disconnected poll (mount_offline) ends the state before it; the time
    after it is unknown until the next real line."""
    from photonscript.shared import mount_log
    out: list[dict] = []
    run: list[dict] = []
    for r in lines or []:
        if mount_offline(r):
            if run:
                out += mount_log.segments(run, end=r["dt"])
            run = []
        else:
            run.append(r)
    if run:
        out += mount_log.segments(run, end=end)
    return out


def judge_sub(rec: dict, config, segs: list[dict],
              states: list[dict]) -> dict:
    """The timeline verdict for one Piggy sub: {state, name, frac, src}."""
    sp = _span(rec, config)
    if sp is None:
        return {"state": "ambiguous", "name": None, "frac": None,
                "src": None, "why": "no capture time"}
    st, en = sp
    dur = max(1.0, (en - st).total_seconds())
    covered = sum(_overlap(st, en, m["start"], m["end"]) for m in states)
    not_tracking = sum(_overlap(st, en, m["start"], m["end"]) for m in states
                       if m["state"] in ("parked", "idle"))
    slewing = sum(_overlap(st, en, m["start"], m["end"]) for m in states
                  if m["state"] == "slewing")
    if states and covered and not_tracking / dur >= PARKED_MAJORITY:
        return {"state": "parked", "name": None,
                "frac": round(not_tracking / dur, 2), "src": "mount-log"}
    best, best_ov = None, 0.0
    for g in segs:
        ov = _overlap(st, en, g["start"], g["end"])
        if ov > best_ov:
            best, best_ov = g, ov
    from photonscript.shared.target_names import calibration_run_name
    if best is not None and best_ov / dur >= TIMELINE_MAJORITY:
        run = calibration_run_name(best["target"])
        return {"state": "target", "name": run or best["target"],
                "frac": round(best_ov / dur, 2), "src": best["src"],
                "test": bool(best["test"] or run),
                **({"calibration_run": True} if run else {})}
    # 2026-10-24: a calibration run's span, first point to last
    spans: dict[str, list] = {}
    for g in segs:
        run = calibration_run_name(g["target"])
        if run:
            sp = spans.setdefault(run, [g["start"], g["end"], g["src"]])
            sp[0], sp[1] = min(sp[0], g["start"]), max(sp[1], g["end"])
    if spans:
        run, ov, src = max(((r, _overlap(st, en, a, b), s)
                            for r, (a, b, s) in spans.items()),
                           key=lambda x: x[1])
        if ov / dur >= TIMELINE_MAJORITY:
            return {"state": "target", "name": run, "frac": round(ov / dur, 2),
                    "src": src, "test": True, "calibration_run": True}
    if slewing > 0:
        return {"state": "slewing", "name": None,
                "frac": round(slewing / dur, 2), "src": "mount-log"}
    return {"state": "ambiguous", "name": None,
            "frac": round(best_ov / dur, 2) if best else 0.0,
            "src": best["src"] if best else None}


def attribute_timeline_records(config, date: str, subs: list[dict],
                               lines: list[dict] | None = None,
                               apply: bool = True) -> dict:
    """Name the night's Piggy "?" subs from the RC16 timeline, IN PLACE.
    lines: the night's mount log (None = load it). Returns {piggy_unknown,
    named, by_target {name: n}, held {parked, slewing, ambiguous}, windows}."""
    from photonscript.shared import mount_log
    from photonscript.shared.target_names import UNATTRIBUTED, canonical_target
    out = {"date": date, "piggy_unknown": 0, "named": 0, "by_target": {},
           "held": {"parked": 0, "slewing": 0, "ambiguous": 0},
           "windows": []}
    todo = [s for s in subs if _is_piggy(s)
            and s.get("target_src") != "manual"
            and canonical_target(s.get("target")) is None]
    out["piggy_unknown"] = len(todo)
    if not todo:
        return out
    if lines is None:
        try:
            lines = mount_log.load(config, date)
        except Exception as e:  # noqa: BLE001
            logger.debug("timeline: no mount log for %s: %s", date, e)
            lines = []
    segs = rc16_timeline(config, subs, lines)
    out["windows"] = [{"target": g["target"], "test": g["test"],
                       "window": f"{g['start']:%H:%M}-{g['end']:%H:%M}",
                       "rc16": f"{g['first']:%H:%M}-{g['last']:%H:%M}",
                       "rc16_subs": g["subs"], "src": g["src"]} for g in segs]
    ends = [sp[1] for sp in (_span(s, config) for s in subs) if sp]
    states = _mount_states(lines, max(ends) if ends else None)
    for s in todo:
        j = judge_sub(s, config, segs, states)
        if j["state"] == "target":
            out["named"] += 1
            out["by_target"][j["name"]] = out["by_target"].get(j["name"], 0) + 1
            if apply:
                if not s.get("target_raw"):
                    cur = s.get("target")
                    s["target_raw"] = cur if cur not in (None, "") else UNATTRIBUTED
                s["target"] = j["name"]
                s["target_src"] = SRC
                if j.get("test"):
                    s["test"] = True   # PS-152: out of medians and goals
                if j.get("calibration_run"):
                    s["calibration_run"] = True   # never a target (2026-10-24)
        else:
            out["held"][j["state"]] += 1
        if apply and s.get("timeline") != j:
            s["timeline"] = j
    if out["named"] or any(out["held"].values()):
        logger.info("RC16 timeline %s: %d of %d Piggy '?' subs named %s, held "
                    "%s", date, out["named"], out["piggy_unknown"],
                    out["by_target"], out["held"])
    return out


def timeline_hold(rec: dict) -> bool:
    """True when the timeline judged this sub parked or slewing: the PS-51
    correlation and the cluster solve must not name it."""
    return (rec.get("timeline") or {}).get("state") in HOLD_STATES


# ------------------------------------------------------------------ plan window

def plan_snapshot(config, date: str) -> dict | None:
    """runs/<date>_plan.json (the armer writes it at dispatch), or None."""
    from photonscript.scheduler.runs import runs_dir
    try:
        snap = json.loads((runs_dir(config) / f"{date}_plan.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return snap if isinstance(snap, dict) else None


def _utc(v) -> datetime | None:
    from photonscript.shared.phd2_store import parse_z
    return parse_z(v) if v else None


def plan_windows(snap: dict | None) -> list[dict]:
    """[{target, start, end}] of the plan targets with a window (snapshots
    from before this fix carry none)."""
    out = []
    for t in (snap or {}).get("targets") or []:
        st, en = _utc(t.get("window_start_utc")), _utc(t.get("window_end_utc"))
        if t.get("name") and st and en and en > st:
            out.append({"target": t["name"], "start": st, "end": en})
    return out


def _target_coords(config, name: str) -> tuple[float, float] | None:
    """(ra, dec) deg of a goal / catalog target by name, else None."""
    from photonscript.scheduler.identify import _candidates
    from photonscript.shared.target_names import target_key
    k = target_key(name)
    for c in _candidates(config):
        if target_key(c[0]) == k:
            return float(c[1]), float(c[2])
    return None


def plan_fallback_records(config, date: str, subs: list[dict],
                          lines: list[dict] | None = None,
                          apply: bool = True) -> dict:
    """Name the Piggy "?" subs the timeline and the solve left ambiguous
    after the plan target window covering most of the sub, IN PLACE, only
    with the mount evidence of the module doc. Returns {tried, named,
    by_target, skipped {reason: n}}."""
    from photonscript.shared import mount_log
    from photonscript.shared.target_names import UNATTRIBUTED, canonical_target
    from photonscript.scheduler.identify import _sep_deg
    out = {"tried": 0, "named": 0, "by_target": {}, "skipped": {}}
    todo = [s for s in subs if _is_piggy(s)
            and s.get("target_src") != "manual"
            and canonical_target(s.get("target")) is None
            and not timeline_hold(s)]
    wins = plan_windows(plan_snapshot(config, date)) if todo else []
    if not todo or not wins:
        if todo:
            out["skipped"]["no plan windows"] = len(todo)
        return out
    if lines is None:
        try:
            lines = mount_log.load(config, date)
        except Exception:  # noqa: BLE001
            lines = []
    real = [r for r in lines if not mount_offline(r)]
    states = _mount_states(lines)
    coords: dict[str, tuple[float, float] | None] = {}

    def skip(why):
        out["skipped"][why] = out["skipped"].get(why, 0) + 1

    for s in todo:
        sp = _span(s, config)
        if sp is None:
            skip("no capture time")
            continue
        out["tried"] += 1
        st, en = sp
        dur = max(1.0, (en - st).total_seconds())
        best, best_ov = None, 0.0
        for w in wins:
            ov = _overlap(st, en, w["start"], w["end"])
            if ov > best_ov:
                best, best_ov = w, ov
        if best is None or best_ov / dur < TIMELINE_MAJORITY:
            skip("no plan window")
            continue
        trk = sum(_overlap(st, en, m["start"], m["end"]) for m in states
                  if m["state"] == "tracking")
        if trk / dur < TIMELINE_MAJORITY:
            skip("mount not seen tracking")
            continue
        pos = mount_log.position_at(real, st + (en - st) / 2)
        name = best["target"]
        if name not in coords:
            coords[name] = _target_coords(config, name)
        if pos is None or coords[name] is None:
            skip("no mount position" if pos is None else "no target coordinates")
            continue
        sep = _sep_deg(float(pos["ra"]), float(pos["dec"]), *coords[name])
        if sep > PLAN_CONFIRM_DEG:
            skip("mount elsewhere")
            continue
        out["named"] += 1
        out["by_target"][name] = out["by_target"].get(name, 0) + 1
        if apply:
            if not s.get("target_raw"):
                cur = s.get("target")
                s["target_raw"] = cur if cur not in (None, "") else UNATTRIBUTED
            s["target"] = name
            s["target_src"] = PLAN_SRC
            s["plan_window"] = {"target": name, "sep_deg": round(sep, 2),
                                "window": f"{best['start']:%H:%M}-"
                                          f"{best['end']:%H:%M}"}
    if out["named"]:
        logger.info("Plan window %s: %d Piggy '?' subs named %s", date,
                    out["named"], out["by_target"])
    return out


def _plan_fallback(config, date: str) -> dict:
    from photonscript.scheduler.runs import edit_subs
    with edit_subs(config, date) as subs:   # under the night lock
        return plan_fallback_records(config, date, subs)


# ------------------------------------------------------------------ approve

def approvable(rec: dict) -> bool:
    """A QA-passing sub still waiting for review that no person held back,
    not a test / calibration sub, with a real target, and not held back by
    the Library pointing check (scheduler.library_pointing: a person files
    those)."""
    from photonscript.scheduler.library_pointing import held
    from photonscript.scheduler.runs import _human_verdict
    from photonscript.shared.qa_rules import REJECTED, is_test_record
    from photonscript.shared.target_names import canonical_target
    if not rec.get("passed_qa") or rec.get("reviewed"):
        return False
    if rec.get("auto_verdict") == REJECTED or _human_verdict(rec) or held(rec):
        return False
    if is_test_record(rec) or timeline_hold(rec):
        return False
    return canonical_target(rec.get("target")) is not None


def approve_records(subs: list[dict], now: str | None = None) -> list[dict]:
    """Mark every approvable() sub approved (in place). Returns them."""
    now = now or datetime.utcnow().isoformat(timespec="seconds") + "Z"
    hits = []
    for s in subs:
        if approvable(s):
            s.update(reviewed=True, review_source="auto",
                     approved_by=APPROVED_BY, reviewed_at=now)
            hits.append(s)
    return hits


# ------------------------------------------------------------------ record

def record_path(config, date: str) -> Path:
    from photonscript.scheduler.runs import runs_dir
    return runs_dir(config) / f"{date}_autofile.json"


def load_record(config, date: str) -> dict | None:
    try:
        return json.loads(record_path(config, date).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def filed_counts(config, subs: list[dict]) -> dict[str, int]:
    """{target: n} of the night's subs the Library holds for sync: passing,
    approved (or no review gate), not test subs, with a real target."""
    from photonscript.shared.qa_rules import is_test_record
    from photonscript.shared.target_names import canonical_target
    gate = bool(getattr(config, "review_gate", True))
    out: dict[str, int] = {}
    for s in subs:
        if not s.get("passed_qa") or (gate and not s.get("reviewed")):
            continue
        name = canonical_target(s.get("target"))
        if name is None or is_test_record(s):
            continue
        out[name] = out.get(name, 0) + 1
    return out


def summary_line(filed: dict[str, int], approved: int | None = None,
                 held: int = 0) -> str:
    """'Library: N subs filed for sync (M 31 180, Heart Nebula 32)'."""
    n = sum(filed.values())
    tops = sorted(filed.items(), key=lambda kv: (-kv[1], kv[0]))
    line = f"Library: {n} subs filed for sync"
    if tops:
        line += " (" + ", ".join(f"{k} {v}" for k, v in tops[:6])
        line += (", ..." if len(tops) > 6 else "") + ")"
    extra = []
    if approved:
        extra.append(f"{approved} auto-approved at dawn")
    if held:
        extra.append(f"{held} Piggy subs left '?'")
    return line + ("; " + "; ".join(extra) if extra else "")


def morning_line(config, date: str | None) -> str | None:
    """The record's line for a push or a page, None without one."""
    rec = load_record(config, date) if date else None
    return (rec or {}).get("line")


# ------------------------------------------------------------------ the pass

def _solve_fallback(config, date: str, max_solves: int = 30) -> dict:
    """PS-137 per-sub solve for the Piggy subs the timeline left ambiguous
    (never the parked / slewing ones, never a manual target)."""
    from photonscript.scheduler.piggy_attribution import attribute_records
    from photonscript.scheduler.runs import edit_subs
    from photonscript.shared.target_names import canonical_target
    with edit_subs(config, date, hold_lock=False) as subs:
        amb = [s for s in subs if _is_piggy(s)
               and s.get("target_src") != "manual"
               and canonical_target(s.get("target")) is None
               and (s.get("timeline") or {}).get("state") == "ambiguous"]
        if not amb:
            return {"tried": 0, "named": 0}
        res = attribute_records(config, date, amb, apply=True, solve=True,
                                max_solves=max_solves)
    return {"tried": len(amb), "named": len(res.get("changed") or [])}


def file_night(config, date: str, push: bool = True,
               approve: bool | None = None) -> dict:
    """The dawn pass (module doc). Never raises; returns and stores the
    record. approve: None = auto_approve_at_dawn."""
    from photonscript.scheduler import runs
    approve = enabled(config) if approve is None else bool(approve)
    rec: dict = {"date": date,
                 "at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                 "auto_approve": approve, "errors": []}

    def step(name, fn):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 - one step never stops the rest
            logger.warning("Dawn filing %s: %s failed: %s", date, name, e)
            rec["errors"].append(f"{name}: {e}")
            return None

    rec["attribution"] = step("attribution",
                              lambda: runs.attribute_night(config, date))
    rec["solve_fallback"] = step("solve fallback",
                                 lambda: _solve_fallback(config, date))
    rec["plan_fallback"] = step("plan fallback",
                                lambda: _plan_fallback(config, date))

    def _pointing():
        from photonscript.scheduler.pointing_record import night_pass
        r = night_pass(config, date, solve=False)
        return {"verdicts_changed": r.get("verdicts_changed")}
    rec["pointing"] = step("pointing pass", _pointing)

    def _slew():
        from photonscript.scheduler.slew_gate import night_pass
        r = night_pass(config, date)
        return {k: r.get(k) for k in ("judged", "straddled", "newly_rejected")}
    rec["slew_gate"] = step("slew gate", _slew)

    def _timeline_summary():
        with runs.edit_subs(config, date, write=False) as subs:
            return attribute_timeline_records(config, date, subs, apply=False)
    tl = step("timeline summary", _timeline_summary) or {}
    rec["timeline"] = {"windows": tl.get("windows") or [],
                       "still_unknown": tl.get("piggy_unknown", 0),
                       "held": tl.get("held") or {}}

    def _approve():
        with runs.edit_subs(config, date) as subs:   # under the night lock
            hits = approve_records(subs)
        by: dict[str, int] = {}
        for h in hits:
            by[h.get("target")] = by.get(h.get("target"), 0) + 1
        return {"approved": len(hits), "by_target": by,
                "by_rig": {r: sum(1 for h in hits
                                  if (h.get("rig") or RC16) == r)
                           for r in sorted({h.get("rig") or RC16
                                            for h in hits})},
                "files": [h.get("file") for h in hits]}
    rec["approved"] = (step("auto-approve", _approve) if approve
                       else {"approved": 0, "skipped": "auto_approve_at_dawn off"})

    def _library():
        res = runs.build_library(config, date)
        try:
            from photonscript.scheduler import sync_batch
            sync_batch.mark_reset(config)
        except Exception as e:  # noqa: BLE001
            logger.debug("sync batch reset skipped: %s", e)
        runs.sync_goal_progress(config)
        return {k: res.get(k) for k in ("linked", "already_there", "retagged",
                                        "pending_review", "rejected_excluded",
                                        "missing_files")}
    rec["library"] = step("library", _library)

    def _cal_sweep():
        r = runs.link_recent_calibration(config)
        return {rg: {"nights": len(v.get("nights") or []), "linked": v.get("linked")}
                for rg, v in r.items()}
    rec["calibration_sweep"] = step("calibration sweep", _cal_sweep)

    def _readout_fill():   # PS-182: record READOUTM on QA records without one
        from photonscript.scheduler.calibration_qa import fill_missing_readouts
        from photonscript.shared.rigs import rig_ids
        n = int(getattr(config, "calibration_readout_fill_at_dawn", 400) or 0)
        return {rg: fill_missing_readouts(config, rg, n) for rg in rig_ids(config)}
    rec["readout_fill"] = step("readout fill", _readout_fill)

    def _unguided_limits():   # the learned unguided limits, with tonight in
        from photonscript.scheduler import unguided_limits as ul
        if ul.mode(config) == "off":
            return {"skipped": "unguided_auto_length off"}
        doc = ul.refresh(config, date)
        return {rg: {"used": t.get("n_used"),
                     "limit_s": (t.get("overall") or {}).get("limit_s")}
                for rg, t in (doc.get("rigs") or {}).items()}
    rec["unguided_limits"] = step("unguided limits", _unguided_limits)
    filed = step("count", lambda: filed_counts(config,
                                               runs._load_subs(config, date)))
    rec["filed"] = filed or {}
    rec["line"] = summary_line(
        rec["filed"], (rec.get("approved") or {}).get("approved") or 0,
        rec["timeline"]["still_unknown"])
    step("record", lambda: _write_record(config, date, rec))
    logger.info("Dawn filing %s: %s", date, rec["line"])
    if push and (sum(rec["filed"].values()) or rec["errors"]):
        step("push", lambda: _push(config, rec))
    return rec


def _write_record(config, date: str, rec: dict) -> None:
    p = record_path(config, date)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rec, indent=1, default=str), encoding="utf-8")
    replace_retry(tmp, p)


def _push(config, rec: dict) -> None:
    import asyncio

    from photonscript.shared.pushover import notify
    msg = f"{rec['date']}: {rec['line']}"
    if rec["errors"]:
        msg += "\nSteps failed: " + "; ".join(rec["errors"])[:300]
    asyncio.run(notify(config, msg, title="PhotonScript library"))


def _wait_backfill(config, date: str, sleep=time.sleep,
                   clock=time.monotonic) -> bool:
    """Wait (bounded by WAIT_BACKFILL_MAX_S) while the night's grading pass
    runs. True when it is done, False when the bound ran out."""
    from photonscript.scheduler.runs import backfill_status
    t0 = clock()
    while clock() - t0 < WAIT_BACKFILL_MAX_S:
        try:
            if not backfill_status(config, date).get("running"):
                return True
        except Exception:  # noqa: BLE001
            return True
        sleep(WAIT_POLL_S)
    return False


def start_dawn_filing(config, date: str) -> bool:
    """Run file_night in a background thread once the night's grading is
    done (bounded wait). One run per night at a time. False when one is
    already running."""
    if _RUNNING.get(date):
        return False
    _RUNNING[date] = True

    def _work():
        try:
            if not _wait_backfill(config, date):
                logger.warning("Dawn filing %s: grading still running after "
                               "%.0f h; filing what is graded", date,
                               WAIT_BACKFILL_MAX_S / 3600)
            file_night(config, date)
        except Exception as e:  # noqa: BLE001
            logger.warning("Dawn filing %s failed: %s", date, e)
        finally:
            _RUNNING.pop(date, None)

    threading.Thread(target=_work, daemon=True,
                     name=f"dawn-file-{date}").start()
    return True


# ------------------------------------------------------------------ dry run

def preview_night(config, date: str) -> dict:
    """What file_night would name and approve, on a copy (writes nothing,
    solves nothing: the subs left ambiguous are the ones the dawn pass
    would plate-solve; the header pass and the PS-51 correlation are not
    run, so it can count a few more "?" subs than the pass leaves). The
    dry run of `photonscript dawn-file`."""
    import copy

    from photonscript.scheduler import runs
    from photonscript.shared import mount_log
    from photonscript.shared.target_names import canonical_target
    subs = copy.deepcopy(runs._load_subs(config, date))
    try:
        lines = mount_log.load(config, date)
    except Exception:  # noqa: BLE001
        lines = []
    tl = attribute_timeline_records(config, date, subs, lines=lines)
    would_solve = sum(1 for s in subs if _is_piggy(s)
                      and s.get("target_src") != "manual"
                      and canonical_target(s.get("target")) is None
                      and (s.get("timeline") or {}).get("state") == "ambiguous")
    pf = plan_fallback_records(config, date, subs, lines=lines)
    hits = approve_records(subs)
    by: dict[str, int] = {}
    for h in hits:
        by[h.get("target")] = by.get(h.get("target"), 0) + 1
    unknown = sum(1 for s in subs if _is_piggy(s)
                  and canonical_target(s.get("target")) is None)
    return {"date": date, "dry_run": True,
            "record": bool(load_record(config, date)),
            "subs": len(subs),
            "timeline": {"named": tl.get("named", 0),
                         "by_target": tl.get("by_target") or {},
                         "held": tl.get("held") or {},
                         "windows": tl.get("windows") or []},
            "would_solve": would_solve,
            "plan_fallback": pf,
            "would_approve": len(hits), "by_target": by,
            "by_rig": {r: sum(1 for h in hits if (h.get("rig") or RC16) == r)
                       for r in sorted({h.get("rig") or RC16 for h in hits})},
            "piggy_still_unknown": unknown,
            "auto_approve_at_dawn": enabled(config)}


# ------------------------------------------------------------------ sweep

def due_nights(config, now: datetime | None = None, armer_state: str = "",
               armer_night: str | None = None) -> list[str]:
    """The armed nights to file now (module doc): a plan snapshot, a subs
    log, no filing record, none running, SWEEP_AFTER_DAWN_MIN past their
    dawn, within dawn_filing_catchup_days, not the armer's live night."""
    from photonscript.scheduler.runs import runs_dir
    from photonscript.shared.phd2_store import night_of
    days = int(getattr(config, "dawn_filing_catchup_days", 3) or 0)
    rd = runs_dir(config)
    if days <= 0 or not rd.exists():
        return []
    now = now or datetime.utcnow()
    today = night_of(config, now)
    first = (datetime.strptime(today, "%Y-%m-%d")
             - timedelta(days=days)).strftime("%Y-%m-%d")
    live = str(armer_state or "") in LIVE_STATES
    out = []
    for p in sorted(rd.glob("*_plan.json")):
        d = p.name[:10]
        if not (first <= d <= today) or _RUNNING.get(d):
            continue
        if live and armer_night == d:
            continue
        if not (rd / f"{d}_subs.jsonl").exists() or record_path(config, d).exists():
            continue
        dawn = _utc((plan_snapshot(config, d) or {}).get("dawn_utc"))
        if dawn is None:
            if d >= today:   # no dawn known: only nights already over
                continue
        elif now < dawn + timedelta(minutes=SWEEP_AFTER_DAWN_MIN):
            continue
        out.append(d)
    return out


def sweep(config, armer_state: str = "", armer_night: str | None = None,
          now: datetime | None = None, start=None) -> list[str]:
    """Start the dawn filing of every due night. Returns the nights
    started. Never raises."""
    start = start or start_dawn_filing
    try:
        nights = due_nights(config, now, armer_state, armer_night)
    except Exception as e:  # noqa: BLE001
        logger.warning("dawn filing sweep failed: %s", e)
        return []
    started = []
    for d in nights:
        try:
            if start(config, d):
                started.append(d)
        except Exception as e:  # noqa: BLE001
            logger.warning("dawn filing sweep: %s not started: %s", d, e)
    if started:
        logger.info("Dawn filing sweep: filing %s (armer %s)",
                    ", ".join(started), armer_state or "?")
    return started


async def run_sweep(get_config, get_armer, tick_seconds: int = SWEEP_TICK_S) -> None:
    """Background loop (app startup). Never raises."""
    import asyncio
    while True:
        try:
            armer = get_armer()
            plan = getattr(armer, "plan", None) or {}
            sweep(get_config(), str(getattr(armer, "state", "") or ""),
                  plan.get("night_of") if isinstance(plan, dict) else None)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("dawn filing sweep failed: %s", e)
        await asyncio.sleep(tick_seconds)
