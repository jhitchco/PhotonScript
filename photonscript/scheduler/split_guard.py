"""PS-27 part 2: the Piggy-600 never wastes a whole sub on a mount move.

The Piggy-600 (NINA #2) rides the RC16's mount and has no say over it. The
RC16 keeps full control of the timing: nothing here ever writes to NINA #1
or holds the RC16 sequence. Two pieces, both on NINA #2's side:

1. Settle gate (piggyback_settle_gate, default on). Each OSC light in the
   companion's OSC_LIGHT_LOOP is preceded by a NINA ExternalScript:

       NINA #2 ExternalScript -> deploy\\settle-gate.cmd -> photonscript
         settle-gate -> POST /api/piggyback/settle-gate (held while it waits)

   run_settle_gate() reads NINA #1's mount info (read-only GET, every
   piggyback_settle_poll_s) into the shared motion tracker
   (shared.mount_motion, also fed by the RC16 agent's 5 s poll and PHD2
   settle events) and returns once the mount is not slewing, has not moved
   for piggyback_settle_still_s, and PHD2 is not settling; at most
   piggyback_settle_timeout_s. It never skips a sub: every verdict exits 0
   (TIMEOUT, NINA #1 unreadable, service down, script missing all mean
   "shoot now"). Holds are logged to runs/<night>_events.jsonl.

   PS-158 tracking hold (piggyback_tracking_gate, default on): the same
   gate also holds while NINA #1 reports the shared mount parked (AtPark)
   or not tracking (TrackingEnabled false), re-checking every poll, at most
   piggyback_tracking_hold_s (then it shoots, like every other verdict);
   once the mount tracks again the settle checks get their own
   piggyback_settle_timeout_s, so one call holds at most the sum.
   2026-10-06: 22 Piggy-600 lights were shot on a parked, non-tracking
   mount (the parked mount is "still", so the settle checks passed). Only
   an explicit AtPark true / TrackingEnabled false holds: a driver that
   reports neither, or a mount NINA #1 shows disconnected, fails open.

2. Abort on move (piggyback_abort_on_move, default OFF). on_rc16_mount()
   runs after each RC16 agent mount poll. When the poll sees a slew, a pier
   change or a jump over piggyback_abort_move_arcmin, and NINA #2 is taking
   a light inside OSC_LIGHT_LOOP (not an AF frame, dark or flat) with its
   camera exposing, it calls NINA #2's ninaAPI v2
   GET /equipment/camera/abort-exposure (route verified in the ninaAPI
   source, V2 Camera controller). The loop then reaches the settle gate and
   starts a fresh sub. Default off: what NINA #2's sequencer does with a
   TakeExposure aborted from outside (error and move on, or wait out a
   download timeout) is not verified on the rig yet. Dithers never abort
   (an RC16 dither is about 1 px on the Piggy-600). Each abort is logged.

night_split_summary() is the per-night split-pointing rate for the run
page (PS-27 item 7): PS-13 straddlers / (judged Piggy-600 lights + aborted
subs), with the aborts and the gate holds that kept a sub off a move.
morning_split_note() puts that rate into the armer's "Night complete"
Pushover; over piggyback_split_alert_pct the push goes out at priority 1.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

GATE_SCRIPT_TOKEN = "settle-gate"   # file-name token the lint rule looks for
UNREADABLE_POLLS = 3     # NINA #1 mount unreadable this many polls: pass
ABORT_DEBOUNCE_S = 60.0  # at most one abort per move episode
ABORT_KINDS = ("slew-start", "slewing", "pier", "move")
SAVE_HELDS = ("slewing", "moved", "settling")   # a hold that kept a sub
                                                 # off a move or a settle
TRACKING_HELDS = ("parked", "not tracking")     # PS-158 holds
PASS_LINE = 0.05         # PS-27 pass criterion: split rejects under 5%
EVENT_SRC = "photonscript"

LAST: dict = {}          # last gate result, for GET /api/piggyback/split-guard
_ABORT: dict = {"at": None}


def gate_enabled(cfg) -> bool:
    return bool(getattr(cfg, "piggyback_settle_gate", True))


def tracking_gate_enabled(cfg) -> bool:
    return bool(getattr(cfg, "piggyback_tracking_gate", True))


def tracking_hold_s(cfg) -> float:
    """PS-158: the longest one gate call holds for a parked / non-tracking
    mount (never below the settle timeout)."""
    return max(_f(cfg, "piggyback_settle_timeout_s", 90.0),
               _f(cfg, "piggyback_tracking_hold_s", 900.0))


def gate_bound_s(cfg) -> float:
    """The longest one gate call can hold (the CLI waits this + a margin):
    the settle timeout, plus the tracking hold before it when that is on."""
    base = max(0.0, _f(cfg, "piggyback_settle_timeout_s", 90.0))
    return base + (tracking_hold_s(cfg) if tracking_gate_enabled(cfg) else 0.0)


def mount_not_tracking(raw) -> str | None:
    """PS-158: "parked" / "not tracking" from a ninaAPI mount info dict, or
    None. Only explicit flags count: a missing key or a mount NINA #1 shows
    disconnected is unknown (the gate fails open)."""
    if not isinstance(raw, dict) or raw.get("Connected") is False:
        return None
    if raw.get("AtPark") is True:
        return "parked"
    trk = raw.get("TrackingEnabled", raw.get("Tracking"))
    if trk is False:
        return "not tracking"
    return None


def abort_enabled(cfg) -> bool:
    return bool(getattr(cfg, "piggyback_abort_on_move", False))


def gate_script(cfg) -> str | None:
    """The script path when the OSC light loop should carry the gate: on AND
    the file exists on this machine (the armer generates on the scope PC).
    Missing script: no gate (the lint rule warns), never a stuck loop."""
    from pathlib import Path
    if not gate_enabled(cfg):
        return None
    path = str(getattr(cfg, "piggyback_settle_script", "") or "").strip()
    if not path:
        return None
    try:
        return path if Path(path).is_file() else None
    except OSError:
        return None


def _f(cfg, key, default) -> float:
    try:
        v = getattr(cfg, key, default)
        return float(default if v is None else v)
    except (TypeError, ValueError):
        return float(default)


def _move_arcmin(cfg) -> float:
    return max(0.05, _f(cfg, "piggyback_abort_move_arcmin", 0.5))


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def log_event(cfg, kind: str, value, when: datetime | None = None,
              **extra) -> dict | None:
    """Append one line to runs/<night>_events.jsonl (never raises)."""
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
        when = when or _utcnow()
        line = {"t": iso_z(when), "rig": "piggyback", "src": EVENT_SRC,
                "kind": kind, "value": value, **extra}
        append_jsonl(events_path(cfg, night_of(cfg, when)), line)
        return line
    except Exception as e:  # noqa: BLE001
        logger.warning("split guard event log failed: %s", e)
        return None


# ------------------------------------------------------------------ ninaAPI

def _unwrap(body):
    if isinstance(body, dict) and "Response" in body:
        return body["Response"]
    return body


async def _http_get(url: str, timeout: float = 5.0):
    import httpx
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url)
        r.raise_for_status()
        return _unwrap(r.json())


def _rc16_base(cfg) -> str:
    return str(getattr(cfg, "nina_base_url", "") or "").rstrip("/")


def _piggy_base(cfg) -> str:
    from photonscript.shared.rigs import PIGGYBACK, rig_config
    return str(rig_config(cfg, PIGGYBACK).nina_base_url or "").rstrip("/")


async def read_rc16_mount(cfg):
    """NINA #1 mount info (read-only), or None."""
    try:
        info = await _http_get(_rc16_base(cfg) + "/equipment/mount/info", 4.0)
        return info if isinstance(info, dict) else None
    except Exception as e:  # noqa: BLE001
        logger.debug("settle gate: NINA #1 mount read failed: %s", e)
        return None


# ------------------------------------------------------------------ gate

async def run_settle_gate(cfg, label: str = "", *, read=None,
                          sleep=asyncio.sleep, clock=time.time,
                          tracker=None, disconnected=None) -> dict:
    """Hold until the shared mount is still and PHD2 is not settling, at most
    piggyback_settle_timeout_s; PS-158: and while it is parked or not
    tracking, at most piggyback_tracking_hold_s. Returns {"verdict": PASS |
    TIMEOUT | UNKNOWN | OFF | ABORTED, "waited_s", "held": [...], "reason"}.
    No verdict stops a sub (the CLI always exits 0). read(cfg) -> ninaAPI
    mount dict or None, tracker and disconnected() are injectable for
    tests."""
    from photonscript.shared.mount_log import sample_from_nina
    from photonscript.shared.mount_motion import TRACKER
    tracker = tracker or TRACKER
    read = read or read_rc16_mount
    base = {"rig": "piggyback", "label": label}
    if not gate_enabled(cfg):
        return {**base, "verdict": "OFF", "waited_s": 0.0, "held": [],
                "reason": "piggyback_settle_gate is off"}
    timeout_s = max(0.0, _f(cfg, "piggyback_settle_timeout_s", 90.0))
    still_need = max(0.0, _f(cfg, "piggyback_settle_still_s", 6.0))
    poll_s = max(0.5, _f(cfg, "piggyback_settle_poll_s", 2.0))
    move = _move_arcmin(cfg)
    track_gate = tracking_gate_enabled(cfg)
    hold_s = tracking_hold_s(cfg)
    t0 = clock()
    settle_from = t0
    unreadable = 0
    held: list[str] = []

    def done(verdict, reason, now):
        res = {**base, "verdict": verdict, "waited_s": round(now - t0, 1),
               "held": list(held), "reason": reason}
        LAST.clear()
        LAST.update(res, at=now)
        if res["waited_s"] >= 1 or verdict not in ("PASS", "OFF"):
            log_event(cfg, "settle_gate", verdict, waited_s=res["waited_s"],
                      held=list(held), reason=reason)
        return res

    while True:
        if disconnected is not None:
            try:
                if await disconnected():
                    return {**base, "verdict": "ABORTED",
                            "waited_s": round(clock() - t0, 1),
                            "held": list(held),
                            "reason": "NINA cancelled the gate"}
            except Exception:  # noqa: BLE001
                pass
        try:
            raw = await read(cfg)
        except Exception as e:  # noqa: BLE001
            logger.debug("settle gate read failed: %s", e)
            raw = None
        now = clock()
        sample = sample_from_nina(raw) if raw else None
        off = mount_not_tracking(raw) if track_gate and sample else None
        if sample is None:
            unreadable += 1
            if unreadable >= UNREADABLE_POLLS:
                return done("UNKNOWN", "NINA #1 mount unreadable; shooting "
                                       "anyway (fails open)", now)
        else:
            unreadable = 0
            tracker.observe(sample, now, move)
            why = []
            if sample.get("slewing"):
                why.append("slewing")
            else:
                still = tracker.still_s(now)
                if still is None or still < still_need:
                    recent = (tracker.last_move_t is not None
                              and now - tracker.last_move_t < still_need)
                    why.append("moved" if recent else "watching")
            if tracker.guider_settling(now):
                why.append("settling")
            if off:                      # PS-158: parked / not tracking
                why.append(off)
            if not why:
                return done("PASS", "mount still" if not held else
                            "mount still again", now)
            for w in why:
                if w not in held:
                    held.append(w)
        waited = now - t0
        if off:
            # PS-158: held while parked / not tracking, at most hold_s; the
            # settle checks then get their own timeout from the unpark
            settle_from = now
            left = hold_s - waited
        else:
            left = timeout_s - (now - settle_from)
        if left <= 0:
            what = (f"mount {off}" if off else "still moving or settling")
            return done("TIMEOUT", f"{what} after {waited:.0f} s; shooting "
                                   "anyway", now)
        await sleep(min(poll_s, max(0.5, left)))


# ------------------------------------------------------------------ abort

def _is_running(node) -> bool:
    return str((node or {}).get("Status", "")).upper() == "RUNNING"


def light_loop_exposing(tree) -> bool:
    """True when NINA #2's /sequence/json shows OSC_LIGHT_LOOP running with
    an exposure item running inside it and none of its triggers (the PS-68
    refocus) running: a light is being taken, not an AF frame."""
    from photonscript.scheduler.calibration import OSC_LIGHT_LOOP_NAME
    names = (OSC_LIGHT_LOOP_NAME, OSC_LIGHT_LOOP_NAME + "_Container")
    found = False

    def walk(node):
        nonlocal found
        if found or not isinstance(node, dict):
            return
        if node.get("Name") in names and _is_running(node):
            trig = [t for t in node.get("Triggers") or [] if isinstance(t, dict)]
            if any(_is_running(t) for t in trig):
                return
            for it in node.get("Items") or []:
                if (isinstance(it, dict) and _is_running(it)
                        and "exposure" in str(it.get("Name", "")).lower()):
                    found = True
                    return
            return
        for child in node.get("Items") or []:
            walk(child)

    for top in (tree if isinstance(tree, list) else [tree]):
        walk(top)
    return found


async def maybe_abort(cfg, kind: str, *, get=None, clock=time.time) -> dict:
    """Abort NINA #2's current light if it is one (see light_loop_exposing)
    and the camera is exposing. Talks ONLY to NINA #2. Never raises."""
    get = get or _http_get
    now = clock()
    out = {"aborted": False, "kind": kind}
    if not abort_enabled(cfg):
        return {**out, "reason": "piggyback_abort_on_move is off"}
    from photonscript.shared.rigs import PIGGYBACK, rig_ids
    if PIGGYBACK not in rig_ids(cfg):
        return {**out, "reason": "piggyback not enabled"}
    if _ABORT["at"] is not None and now - _ABORT["at"] < ABORT_DEBOUNCE_S:
        return {**out, "reason": "debounced (aborted this move already)"}
    b2 = _piggy_base(cfg)
    try:
        tree = await get(b2 + "/sequence/json")
        if not light_loop_exposing(tree):
            return {**out, "reason": "NINA #2 is not taking an OSC light"}
        cam = await get(b2 + "/equipment/camera/info")
        if not (isinstance(cam, dict) and cam.get("IsExposing")):
            return {**out, "reason": "NINA #2 camera not exposing"}
        resp = await get(b2 + "/equipment/camera/abort-exposure")
    except Exception as e:  # noqa: BLE001
        logger.warning("split guard: abort on %s failed: %s", kind, e)
        return {**out, "reason": f"NINA #2 unreachable: {e}"}
    _ABORT["at"] = now
    log_event(cfg, "split_abort", kind, response=str(resp)[:80])
    logger.info("split guard: RC16 mount %s, aborted the Piggy-600 light "
                "(%s)", kind, resp)
    return {**out, "aborted": True, "reason": str(resp)[:80]}


async def on_rc16_mount(cfg, mount: dict, *, now: float | None = None,
                        tracker=None, get=None) -> dict | None:
    """Called by the RC16 agent after each mount poll: feed the tracker and,
    on a move while NINA #2 shoots a light, abort that light. Never raises."""
    try:
        from photonscript.shared.mount_log import sample_from_nina
        from photonscript.shared.mount_motion import TRACKER
        tracker = tracker or TRACKER
        now = time.time() if now is None else now
        kind = tracker.observe(sample_from_nina(mount), now, _move_arcmin(cfg))
        if kind in ABORT_KINDS and abort_enabled(cfg):
            return await maybe_abort(cfg, kind, get=get, clock=lambda: now)
        return None
    except Exception as e:  # noqa: BLE001
        logger.debug("split guard skipped: %s", e)
        return None


# ------------------------------------------------------------------ report

def night_split_summary(cfg, date: str, subs: list[dict] | None = None) -> dict:
    """Per-night split pointing for the Piggy-600 (PS-27 item 7).

    judged     Piggy-600 lights the PS-13 check judged (slew_overlap_s set)
    straddled  of those, exposed through an RC16 move
    aborted    lights aborted on a move (never saved, so not in judged)
    attempted  judged + aborted
    rate       straddled / attempted, None with no data. An aborted sub is
               not a split reject (its time went to a fresh sub), so a
               working abort lowers the rate; aborts are counted apart
    saves      settle-gate holds that kept a sub off a move or a settle
    holds      every settle-gate hold; timeouts: holds that ran out
    tracking_holds  PS-158: holds for a parked / non-tracking mount"""
    from photonscript.shared.night_events import events_path
    from photonscript.shared.phd2_store import read_jsonl
    from photonscript.scheduler.slew_gate import gated_rigs
    if subs is None:
        from photonscript.scheduler.runs import _load_subs
        subs = _load_subs(cfg, date)
    rigs = set(gated_rigs(cfg)) or {"piggyback"}
    judged = straddled = lights = 0
    for r in subs or []:
        if (r.get("rig") or "rc16") not in rigs:   # subs.jsonl: lights only
            continue
        lights += 1
        ov = r.get("slew_overlap_s")
        if ov is None:
            continue
        try:
            ov = float(ov)
        except (TypeError, ValueError):
            continue
        judged += 1
        straddled += int(ov > 0)
    aborted = holds = saves = timeouts = tracking = 0
    for e in read_jsonl(events_path(cfg, date)):
        if e.get("src") != EVENT_SRC:
            continue
        if e.get("kind") == "split_abort":
            aborted += 1
        elif e.get("kind") == "settle_gate":
            holds += 1
            timeouts += int(e.get("value") == "TIMEOUT")
            saves += int(any(h in SAVE_HELDS for h in e.get("held") or []))
            tracking += int(any(h in TRACKING_HELDS
                                for h in e.get("held") or []))
    attempted = judged + aborted
    rate = round(straddled / attempted, 3) if attempted else None
    return {"date": date, "lights": lights, "judged": judged,
            "straddled": straddled, "aborted": aborted, "attempted": attempted,
            "rate": rate, "pass_line": PASS_LINE,
            "passed": None if rate is None else rate < PASS_LINE,
            "saves": saves, "holds": holds, "timeouts": timeouts,
            "tracking_holds": tracking,   # PS-158: parked / not tracking
            "gate": gate_enabled(cfg), "abort": abort_enabled(cfg)}


ALERT_HINT = "check RC16 dither/center cadence"
ABORT_HINT = "consider PS_PIGGYBACK_ABORT_ON_MOVE"


def split_alert_pct(cfg) -> float:
    try:
        return float(getattr(cfg, "piggyback_split_alert_pct", 5.0))
    except (TypeError, ValueError):
        return 5.0


def split_push_line(cfg, s: dict) -> tuple[str | None, bool]:
    """The morning-push line for one night_split_summary(), and whether it
    is over piggyback_split_alert_pct. (None, False) when the Piggy-600 took
    no lights (and aborted none), e.g. a night without the companion."""
    lights, attempted = int(s.get("lights") or 0), int(s.get("attempted") or 0)
    if not lights and not attempted:
        return None, False
    if not attempted:
        return f"Piggy split n/a ({lights} lights, none judged)", False
    straddled = int(s.get("straddled") or 0)
    pct = 100.0 * straddled / attempted
    parts = [f"{straddled} of {attempted}"]
    if s.get("aborted"):
        parts.append(f"{int(s['aborted'])} aborted")
    if s.get("gate") or s.get("saves"):
        parts.append(f"{int(s.get('saves') or 0)} saved by gate")
    line = f"Piggy split {pct:.1f}% ({'; '.join(parts)})"
    limit = split_alert_pct(cfg)
    if pct <= limit:
        return line, False
    hint = ALERT_HINT if s.get("abort") else f"{ALERT_HINT}; {ABORT_HINT}"
    return f"{line}. Over {limit:g}%: {hint}.", True


def morning_split_note(cfg, date: str | None) -> tuple[str | None, bool]:
    """split_push_line() for a night, for the dawn "Night complete" push.
    Never raises: (None, False) on any error or without a night."""
    if not date:
        return None, False
    try:
        return split_push_line(cfg, night_split_summary(cfg, date))
    except Exception as e:  # noqa: BLE001 - never block the morning push
        logger.debug("split push line skipped for %s: %s", date, e)
        return None, False
