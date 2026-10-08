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
  slewing   it overlaps a slew and no target holds the majority: stays "?"
  ambiguous none of the above: "?", the dawn pass tries the PS-137 solve

A sub assigned by hand (target_src "manual") or already named is never
touched. Evidence goes to rec["timeline"]; a renamed sub keeps target_raw
and gets target_src "rc16-timeline". Nothing here writes a FITS file or
touches the desktop mirror.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

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


def _mount_states(lines: list[dict] | None,
                  end: datetime | None = None) -> list[dict]:
    """The mount log's state spans. The logger writes a line on a change
    (and a heartbeat only while tracking), so the night's last state holds
    to `end` (the last sub) unless it is tracking (stale after STALE_S)."""
    from photonscript.shared import mount_log
    return mount_log.segments(lines or [], end=end) if lines else []


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
    if best is not None and best_ov / dur >= TIMELINE_MAJORITY:
        return {"state": "target", "name": best["target"],
                "frac": round(best_ov / dur, 2), "src": best["src"],
                "test": bool(best["test"])}
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


# ------------------------------------------------------------------ approve

def approvable(rec: dict) -> bool:
    """A QA-passing sub still waiting for review that no person held back,
    not a test / calibration sub, with a real target."""
    from photonscript.scheduler.runs import _human_verdict
    from photonscript.shared.qa_rules import REJECTED, is_test_record
    from photonscript.shared.target_names import canonical_target
    if not rec.get("passed_qa") or rec.get("reviewed"):
        return False
    if rec.get("auto_verdict") == REJECTED or _human_verdict(rec):
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
    tmp.replace(p)


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
