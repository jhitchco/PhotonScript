"""Stuck-loop detector: a rig that should be imaging but saves no lights.

2026-10-14 22:21 to 22:41 local: NINA #1 ran AUTOFOCUS-STARTING / FINISHED,
MOUNT-CENTER, MOUNT-PARKED, MOUNT-UNPARKED three times with zero lights
saved (a StartGuiding in the target container failed on an unguided night
and skipped the container, so the night loop restarted it). Nothing paged;
Jeremy noticed by watching NINA.

Polled from the armer's tick while it is RUNNING and the roof reads safe
(`armer_tick`, one hook call; at most every `stuck_loop_poll_s`), inside a
planned target window only (plan targets window_start_utc..window_end_utc,
between astro dusk and dawn). Reads (GET only) NINA #1 and NINA #2's
ninaAPI event history and the subs log. Per rig, the clock starts at the
latest of: the target window start, astro dusk, the last light (subs log)
or IMAGE-SAVE, the roof turning safe again (SAFETY-CHANGED), the armer
coming back to RUNNING (a pause, an unsafe spell, a restart: no tick for
`RESUME_GAP_S`) and the last restart this detector asked for.

Signals (any one starts an episode):
  no_light   no light for max(stuck_loop_gap_subs (3) x the planned sub
             length + stuck_loop_gap_margin_min (10), stuck_loop_gap_min
             (20)) minutes. Held while an autofocus (up to AF_MAX_MIN) or a
             meridian flip (up to FLIP_MAX_MIN) is in progress.
  restarts   the target container restarting: stuck_loop_repeats (2)
             autofocus + center cycles (AUTOFOCUS-STARTING, then
             MOUNT-CENTER before the next AF) with no light, confirmed by a
             third cycle, a park after the last center, or no light one
             sub + 5 min after the last center (a real target start images
             right after its center).
  park_cycle stuck_loop_park_cycles (2) MOUNT-PARKED -> MOUNT-UNPARKED with
             no light and no unsafe SAFETY-CHANGED in between.
Not stuck: unsafe / roof closed (the armer does not call while unsafe, and
an unsafe event resets the clock), an AF or flip within its normal
duration, before dusk / after dawn, a pause (the clock restarts on
resume), NINA #1 running a calibration / dark / flat / TPoint / tracking
test item (night_pilot.BUSY_WORDS), a slew between targets (the window
start resets the clock), any IMAGE-SAVE (darks and flats count as busy).

Action, once per episode (an episode ends when a light is saved, or with
the restart the pilot asked for):
  * a priority Pushover naming the repeating steps from the event history:
    "RC16 looping: AF 22:21, 22:29, 22:35; center x3; park/unpark x2; no
    light in 20 min ..." plus a likely cause and what the pilot does;
  * night_pilot_stuck_loop_mode off | advise (default) | act
    (night_pilot_mode advise caps it, off turns it off): act = Restart
    tonight from now (Armer.restart, after the current sub) for the RC16,
    the Piggy-600 companion stopped and re-dispatched (PilotIO.piggy_af) for
    NINA #2; once per rig per night. A later episode the same night is an
    alert only ("already restarted at ..."). Guards: the night pilot's
    live_guard (RUNNING, no calibration job / arm recipe / busy NINA #1
    item) and RESTART_DARK_MIN of dark left.
  * run events kind "stuck_loop" (alert, act, advise, held, failed, clear,
    repeat), state <data_dir>/stuck_loop/<night>.json, morning report lines
    (morning_lines).

stuck_loop_mode alert (default) | off turns the detector off.
Backtest: backtest(doc, cfg) replays a night document (pilot_backtest's
shape: plan, subs, nina_events) minute by minute, nothing sent.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.shared.fileops import replace_retry

logger = logging.getLogger(__name__)

KIND = "stuck_loop"
TITLE = "PhotonScript stuck loop"
MODES = ("off", "advise", "act")
AF_MAX_MIN = 15.0        # an autofocus longer than this is not "normal"
FLIP_MAX_MIN = 20.0      # a meridian flip (before -> after) at most this
CENTER_AFTER_AF_MIN = 15.0   # a MOUNT-CENTER this soon after an AF start = one cycle
CONFIRM_MARGIN_MIN = 5.0     # restarts: no light one sub + this after the center
RESUME_GAP_S = 180.0     # no armer tick this long = not RUNNING meanwhile
DUP_S = 5.0              # NINA doubles some events (MOUNT-PARKED x2)

AF_START = "AUTOFOCUS-STARTING"
CENTER = "MOUNT-CENTER"
PARKED = "MOUNT-PARKED"
UNPARKED = "MOUNT-UNPARKED"
SAFETY = "SAFETY-CHANGED"
IMAGE_SAVE = "IMAGE-SAVE"
FLIP_BEFORE = "MOUNT-BEFORE-FLIP"
FLIP_AFTER = "MOUNT-AFTER-FLIP"


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def _f(cfg, key: str, default: float) -> float:
    try:
        v = float(getattr(cfg, key, default))
    except (TypeError, ValueError):
        return float(default)
    return v if v == v else float(default)


def detect_on(cfg) -> bool:
    return str(getattr(cfg, "stuck_loop_mode", "alert") or "alert").strip().lower() != "off"


def action_mode(cfg) -> str:
    """night_pilot_stuck_loop_mode, capped by night_pilot_mode."""
    m = str(getattr(cfg, "night_pilot_stuck_loop_mode", "advise") or "advise").strip().lower()
    if m not in MODES:
        m = "advise"
    pm = str(getattr(cfg, "night_pilot_mode", "act") or "act").strip().lower()
    if pm == "off":
        return "off"
    if pm == "advise" and m == "act":
        return "advise"
    return m


def gap_limit_min(cfg, sub_s: float | None) -> float:
    """max(3 x planned sub + 10 min, 20 min)."""
    sub_min = (float(sub_s) if sub_s else 0.0) / 60.0
    return max(_f(cfg, "stuck_loop_gap_subs", 3.0) * sub_min
               + _f(cfg, "stuck_loop_gap_margin_min", 10.0),
               _f(cfg, "stuck_loop_gap_min", 20.0))


# --------------------------------------------------------------------------
# reading the inputs
# --------------------------------------------------------------------------

def _parse(s) -> datetime | None:
    from photonscript.scheduler.af_stall import parse_time
    return parse_time(s)


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def event_time(cfg, s) -> datetime | None:
    """An event row's Time -> naive UTC. NINA writes most rows with an
    offset ('...-06:00') but MOUNT-CENTER as naive LOCAL time
    ('2026-10-14T22:25:09.4002', seen 2026-10-14): a naive Time is the
    observatory's local time when cfg is given."""
    t = _parse(s)
    if t is None or cfg is None:
        return t
    txt = str(s).strip()
    tail = txt[10:]
    if txt.endswith("Z") or "+" in tail or "-" in tail:
        return t
    try:
        from photonscript.shared.localtime import utc_offset_hours
        return t - timedelta(hours=utc_offset_hours(cfg, t))
    except Exception:  # noqa: BLE001
        return t


def parse_events(hist, cfg=None) -> list[tuple[datetime, str, dict]]:
    """ninaAPI /event-history rows -> [(naive UTC, EVENT, row)], oldest
    first, NINA's doubled rows (same event within DUP_S) collapsed."""
    rows = []
    for e in hist or []:
        if not isinstance(e, dict):
            continue
        t = event_time(cfg, e.get("Time"))
        if t is not None:
            rows.append((t, str(e.get("Event") or "").upper(), e))
    rows.sort(key=lambda r: r[0])
    out: list[tuple[datetime, str, dict]] = []
    for r in rows:
        if out and out[-1][1] == r[1] and r[1] != IMAGE_SAVE \
                and (r[0] - out[-1][0]).total_seconds() <= DUP_S:
            continue
        out.append(r)
    return out


def light_times(subs: list[dict], rig: str) -> list[datetime]:
    """End times (naive UTC) of the rig's lights in the subs log."""
    from photonscript.scheduler.night_pilot import _light
    from photonscript.shared.sub_time import parse_utc, sub_end
    out = []
    for r in subs or []:
        if (r.get("rig") or "rc16") != rig or not _light(r):
            continue
        try:
            t = sub_end(r)
        except Exception:  # noqa: BLE001
            t = None
        t = t or parse_utc(r.get("end_utc") or r.get("time"))
        if t is not None:
            out.append(t)
    return sorted(out)


def current_window(plan: dict | None, now: datetime) -> dict | None:
    """The plan target whose window holds now: {name, start, end, sub_s}."""
    for t in (plan or {}).get("targets") or []:
        if not isinstance(t, dict):
            continue
        a, b = _parse(t.get("window_start_utc")), _parse(t.get("window_end_utc"))
        if a is None or b is None or not (a <= now < b):
            continue
        subs = []
        for e in t.get("exposures") or []:
            try:
                subs.append(float((e or {}).get("exp_s")))
            except (TypeError, ValueError):
                pass
        return {"name": t.get("name"), "start": a, "end": b,
                "sub_s": max(subs) if subs else None,
                "guided": t.get("guided")}
    return None


def piggy_sub_s(cfg, plan: dict | None) -> float | None:
    pl = ((plan or {}).get("piggy_length") or {}) if isinstance(plan, dict) else {}
    vals = []
    for s in [*(pl.get("segments") or []), *(pl.get("targets") or []),
              {"exp_s": pl.get("single_s")}, {"exp_s": pl.get("base_s")}]:
        try:
            v = float((s or {}).get("exp_s"))
        except (TypeError, ValueError):
            continue
        if v > 0:
            vals.append(v)
    if vals:
        return max(vals)
    try:
        return float(getattr(cfg, "piggyback_exposure_s", None) or 0) or None
    except (TypeError, ValueError):
        return None


def _rigs(cfg) -> tuple[str, ...]:
    return ("rc16", "piggyback") if getattr(cfg, "piggyback_enabled", False) else ("rc16",)


# --------------------------------------------------------------------------
# the judgment (pure)
# --------------------------------------------------------------------------

def assess(cfg, rig: str, events: list[tuple[datetime, str, dict]],
           lights: list[datetime], *, now: datetime, floor: datetime,
           sub_s: float | None) -> dict:
    """One rig, now. -> {"state": ok | hold | stuck, "signals": [...],
    "since", "last_light", "gap_min", "limit_min", "af", "centers",
    "parks", "why"}. events: parse_events() rows up to now."""
    ev = [r for r in events if r[0] <= now]
    since = floor
    # the roof: an unsafe spell restarts the clock; unsafe now = hold
    unsafe_now = False
    for t, name, row in ev:
        if name == SAFETY:
            safe = row.get("IsSafe")
            if safe is False:
                unsafe_now = True
            elif safe is True:
                if unsafe_now:
                    since = max(since, t)
                unsafe_now = False
    last_light = max([t for t in lights if t <= now]
                     + [t for t, n, _r in ev if n == IMAGE_SAVE], default=None)
    if last_light is not None:
        since = max(since, last_light)
    out = {"state": "ok", "signals": [], "since": since, "last_light": last_light,
           "gap_min": round(max(0.0, (now - since).total_seconds() / 60.0), 1),
           "limit_min": round(gap_limit_min(cfg, sub_s), 1), "sub_s": sub_s,
           "af": [], "centers": [], "parks": [], "why": None}
    if unsafe_now:
        out.update(state="hold", why="unsafe (SAFETY-CHANGED)")
        return out
    if rig != "rc16":
        # the Piggy-600 companion: judged only while its NINA #2 sequence
        # runs (not dispatched tonight, or ended: the NINA watchdog's)
        seq = [n for _t, n, _r in ev if n.startswith("SEQUENCE-")]
        if not seq or seq[-1] != "SEQUENCE-STARTING":
            out.update(state="hold", why="NINA #2 sequence not running")
            return out
    win = [r for r in ev if r[0] > since]
    # autofocus + center cycles, park -> unpark cycles
    cycles: list[dict] = []
    parks: list[tuple[datetime, datetime | None]] = []
    af_open = flip_open = None
    for t, name, _row in win:
        if name == AF_START:
            cycles.append({"af": t, "center": None})
            af_open = t
        elif name.startswith("AUTOFOCUS-") and name != "AUTOFOCUS-POINT-ADDED":
            af_open = None
        elif name == CENTER:
            if cycles and cycles[-1]["center"] is None and \
                    (t - cycles[-1]["af"]).total_seconds() / 60.0 <= CENTER_AFTER_AF_MIN:
                cycles[-1]["center"] = t
            out["centers"].append(t)
        elif name == PARKED:
            if not parks or parks[-1][1] is not None:
                parks.append((t, None))
        elif name == UNPARKED:
            if parks and parks[-1][1] is None:
                parks[-1] = (parks[-1][0], t)
        elif name == FLIP_BEFORE:
            flip_open = t
        elif name == FLIP_AFTER:
            flip_open = None
    full = [c for c in cycles if c["center"] is not None]
    done_parks = [p for p in parks if p[1] is not None]
    out["af"] = [c["af"] for c in cycles]
    out["parks"] = parks
    sub_min = (sub_s or 0.0) / 60.0
    sig = []
    if len(full) >= max(1, int(_f(cfg, "stuck_loop_repeats", 2))):
        last_c = full[-1]["center"]
        parked_after = any(p[0] >= last_c for p in parks)
        waited = (now - last_c).total_seconds() / 60.0 >= sub_min + CONFIRM_MARGIN_MIN
        if len(full) >= 3 or parked_after or waited:
            sig.append("restarts")
    if len(done_parks) >= max(1, int(_f(cfg, "stuck_loop_park_cycles", 2))):
        sig.append("park_cycle")
    hold = None
    if flip_open is not None and (now - flip_open).total_seconds() / 60.0 < FLIP_MAX_MIN:
        hold = f"meridian flip in progress since {flip_open:%H:%M}Z"
    over = out["gap_min"] >= out["limit_min"]
    if over:
        if af_open is not None and (now - af_open).total_seconds() / 60.0 < AF_MAX_MIN:
            hold = hold or f"autofocus in progress since {af_open:%H:%M}Z"
        else:
            sig.append("no_light")
    out["signals"] = sig
    if hold and (sig or over):
        out.update(state="hold", why=hold)
    elif sig:
        out["state"] = "stuck"
    return out


def _hm(cfg, t: datetime | None) -> str:
    try:
        from photonscript.scheduler.nina_watchdog import _local_hm
        return _local_hm(cfg, t)
    except Exception:  # noqa: BLE001
        return t.strftime("%H:%MZ") if t else "?"


def _label(cfg, rig: str) -> str:
    try:
        from photonscript.shared.rigs import rig_label
        return rig_label(cfg, rig)
    except Exception:  # noqa: BLE001
        return "RC16" if rig == "rc16" else "Piggy-600"


def steps_text(cfg, rig: str, v: dict, now: datetime) -> str:
    """"RC16 looping: AF 22:21, 22:29, 22:35; center x3; park/unpark x2;
    no light in 20 min"."""
    lab = _label(cfg, rig)
    parts = []
    if v["af"]:
        parts.append("AF " + ", ".join(_hm(cfg, t) for t in v["af"][-6:]))
    if v["centers"]:
        n = len(v["centers"])
        parts.append("center" + (f" x{n}" if n > 1 else ""))
    done = [p for p in v["parks"] if p[1] is not None]
    if done:
        parts.append("park/unpark" + (f" x{len(done)}" if len(done) > 1 else ""))
    gap = int(round(v["gap_min"]))
    if v.get("last_light") is not None and v["last_light"] >= v["since"]:
        parts.append(f"no light since {_hm(cfg, v['last_light'])} ({gap} min)")
    else:
        parts.append(f"no light in {gap} min")
    head = "looping" if ("restarts" in v["signals"] or "park_cycle" in v["signals"]) \
        else "not imaging"
    return f"{lab} {head}: " + "; ".join(parts)


def cause_text(cfg, rig: str, v: dict, events, guided: bool | None) -> str:
    """A likely cause from the event history (best effort)."""
    since = v["since"]
    guider = [(t, n) for t, n, _r in events
              if n in ("GUIDER-CONNECTED", "GUIDER-DISCONNECTED", "GUIDER-START")
              and t >= since - timedelta(minutes=5)]
    if "restarts" in v["signals"] or "park_cycle" in v["signals"]:
        base = ("the target container keeps failing and the night loop restarts "
                "it (an instruction that fails skips the container")
        if guided is False and guider:
            base += (f"; the guider was {guider[-1][1].split('-')[1].lower()} at "
                     f"{_hm(cfg, guider[-1][0])}: a StartGuiding on an unguided "
                     "night fails")
        return base + "). Check NINA's sequence for a failing instruction."
    return ("the sequence is not exposing (waiting on a condition, a failed "
            "instruction, or a stalled camera). Check NINA's sequence.")


# --------------------------------------------------------------------------
# state, events
# --------------------------------------------------------------------------

def state_path(cfg, night: str) -> Path:
    return Path(getattr(cfg, "data_dir", ".")) / "stuck_loop" / f"{night}.json"


def load_state(cfg, night: str) -> dict:
    try:
        st = json.loads(state_path(cfg, night).read_text(encoding="utf-8"))
        if isinstance(st, dict) and st.get("night") == night:
            return st
    except (OSError, ValueError):
        pass
    return {"night": night, "open": {}, "episodes": [], "restarts": {},
            "last_call": None, "running_since": None}


def save_state(cfg, st: dict) -> None:
    p = state_path(cfg, st["night"])
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
        replace_retry(tmp, p)
    except OSError as e:
        logger.warning("stuck loop state not saved: %s", e)


def _event(cfg, night: str, value: str, detail: str, now: datetime,
           rig: str = "rc16", **extra) -> None:
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl
        append_jsonl(events_path(cfg, night),
                     {"t": _iso(now), "rig": rig, "src": "photonscript",
                      "kind": KIND, "value": value, "detail": detail, **extra})
    except Exception as e:  # noqa: BLE001
        logger.warning("stuck loop event (%s) not logged: %s", value, e)


# --------------------------------------------------------------------------
# the live check
# --------------------------------------------------------------------------

class LoopIO:
    """The scope PC (GET only for the reads). Every method never raises."""

    async def history(self, cfg, rig: str):
        from photonscript.scheduler.nina_watchdog import rig_base
        from photonscript.scheduler.night_pause import nina_get
        base = rig_base(cfg, rig)
        if not base:
            return None
        try:
            return await asyncio.wait_for(nina_get(base, "/event-history",
                                                   timeout=15.0), 20)
        except Exception:  # noqa: BLE001
            return None

    def subs(self, cfg, night: str) -> list[dict]:
        try:
            from photonscript.scheduler.runs import _load_subs
            return _load_subs(cfg, night)
        except Exception:  # noqa: BLE001
            return []

    async def nina1_running(self, cfg):
        from photonscript.scheduler.night_pilot import PilotIO
        return await PilotIO().nina1_running(cfg)

    async def notify(self, cfg, msg: str, priority: int = 1) -> None:
        try:
            from photonscript.shared.pushover import notify
            await notify(cfg, msg, title=TITLE, priority=priority)
        except Exception as e:  # noqa: BLE001
            logger.warning("stuck loop push failed: %s", e)

    async def restart(self, armer, now: datetime) -> dict:
        try:
            return await armer.restart(when="after_exposure", now=now)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detail": str(e)}

    def piggy_restart(self, cfg, armer, done) -> None:
        """NINA #2: stop after its sub, re-dispatch the companion (the
        night pilot's Piggy AF path), in the background."""
        from photonscript.scheduler.night_pilot import PilotIO

        async def _run():
            try:
                ok, detail = await PilotIO().piggy_af(cfg, armer)
            except Exception as e:  # noqa: BLE001
                ok, detail = False, str(e)
            done(ok, detail)
        try:
            asyncio.get_running_loop().create_task(_run())
        except RuntimeError:
            done(False, "no event loop")


_LIVE: dict = {"last_poll": None, "task": None}


def armer_tick(armer, now: datetime | None = None, io: LoopIO | None = None):
    """The armer's hook (RUNNING, roof not unsafe). Rate limited to
    stuck_loop_poll_s; the check runs as a background task so the armer's
    tick never waits on the NINA reads (one at a time). Returns the task or
    None. Never raises."""
    try:
        cfg = armer.config
        if not detect_on(cfg) or str(getattr(armer, "state", "")) != "RUNNING":
            return None
        now = now or datetime.utcnow()
        poll = _f(cfg, "stuck_loop_poll_s", 60.0)
        last = _LIVE.get("last_poll")
        if last is not None and 0 <= (now - last).total_seconds() < poll:
            return None
        prev = _LIVE.get("task")
        if prev is not None and not prev.done():
            return None
        _LIVE["last_poll"] = now

        async def _run():
            try:
                return await check(cfg, armer, io or LoopIO(), now)
            except Exception as e:  # noqa: BLE001
                logger.warning("stuck loop check failed: %s", e)
                return None
        _LIVE["task"] = asyncio.get_running_loop().create_task(_run())
        return _LIVE["task"]
    except Exception as e:  # noqa: BLE001
        logger.warning("stuck loop hook failed: %s", e)
        return None


async def check(cfg, armer, io, now: datetime) -> dict:
    """One pass over the rigs. Returns {rig: verdict} (and why skipped)."""
    plan = getattr(armer, "plan", None) or {}
    night = plan.get("night_of")
    if not night:
        from photonscript.shared.phd2_store import night_of
        night = night_of(cfg, now)
    st = load_state(cfg, night)
    lc = _parse(st.get("last_call"))
    if lc is None or (now - lc).total_seconds() > RESUME_GAP_S + _f(cfg, "stuck_loop_poll_s", 60.0):
        st["running_since"] = _iso(now)     # back to RUNNING (or first call)
    st["last_call"] = _iso(now)
    res: dict = {"night": night}
    dusk, dawn = _parse(plan.get("dusk_utc")), _parse(plan.get("dawn_utc"))
    win = current_window(plan, now)
    if win is None or (dawn is not None and now >= dawn) or (dusk is not None and now < dusk):
        res["skip"] = "outside a planned target window"
        save_state(cfg, st)
        return res
    floor = max(x for x in (win["start"], dusk, _parse(st.get("running_since")))
                if x is not None)
    subs = io.subs(cfg, night)
    rigs = _rigs(cfg)
    hists = await asyncio.gather(*(io.history(cfg, r) for r in rigs))
    for rig, hist in zip(rigs, hists):
        if hist is None:
            res[rig] = {"state": "unknown", "why": "event history unreadable"}
            continue
        rfloor = floor
        rs = _parse((st.get("restarts") or {}).get(rig, {}).get("t")) \
            if isinstance((st.get("restarts") or {}).get(rig), dict) else None
        if rs is not None:
            rfloor = max(rfloor, rs)
        sub_s = win["sub_s"] if rig == "rc16" else piggy_sub_s(cfg, plan)
        events = parse_events(hist, cfg)
        v = assess(cfg, rig, events, light_times(subs, rig), now=now,
                   floor=rfloor, sub_s=sub_s)
        res[rig] = await _handle(cfg, armer, io, st, rig, v, events, win, now, night)
    save_state(cfg, st)
    return res


def _short(v: dict) -> dict:
    return {"state": v["state"], "signals": v["signals"], "gap_min": v["gap_min"],
            "limit_min": v["limit_min"], "why": v.get("why")}


async def _handle(cfg, armer, io, st, rig, v, events, win, now, night) -> dict:
    opened = (st.get("open") or {}).get(rig)
    if opened:
        alert_t = _parse(opened.get("t"))
        if v.get("last_light") is not None and alert_t is not None \
                and v["last_light"] > alert_t:
            st["open"].pop(rig, None)
            for e in st.get("episodes") or []:   # the stored copy
                if e.get("rig") == rig and e.get("t") == opened.get("t"):
                    e["cleared"] = _iso(now)
            _event(cfg, night, "clear", f"{_label(cfg, rig)}: a light was saved at "
                   f"{_hm(cfg, v['last_light'])}; the loop is over", now, rig=rig)
            return {**_short(v), "episode": "cleared"}
        return {**_short(v), "episode": "open"}
    if v["state"] == "hold":
        return {**_short(v), "episode": None}
    if v["state"] != "stuck":
        return _short(v)
    names, target = (await io.nina1_running(cfg)) if rig == "rc16" else (None, None)
    busy = _busy_item(names, target)
    if busy:   # a planned calibration / dark / TPoint slot, not a loop
        return {**_short(v), "state": "hold", "why": f"NINA #1 runs '{busy}'",
                "episode": None}
    # a new episode
    steps = steps_text(cfg, rig, v, now)
    cause = cause_text(cfg, rig, v, events, win.get("guided"))
    ep = {"rig": rig, "t": _iso(now), "signals": v["signals"], "steps": steps,
          "target": win.get("name"), "gap_min": v["gap_min"],
          "af": [_iso(t) for t in v["af"]], "action": None}
    m = action_mode(cfg)
    prev = (st.get("restarts") or {}).get(rig)
    what = "Restart tonight from now" if rig == "rc16" else \
        "stop NINA #2 after its sub and re-dispatch the companion"
    if m == "off":
        act_text = ""
    elif prev:
        ep["action"] = "repeat"
        act_text = (f" The night pilot already restarted it at "
                    f"{_hm(cfg, _parse(prev.get('t')))} tonight; alert only: a human "
                    "needs to fix the sequence.")
        _event(cfg, night, "repeat", steps, now, rig=rig)
    elif m == "advise":
        ep["action"] = "advise"
        act_text = (f" Night pilot (advise): would {what[0].lower() + what[1:]}; "
                    "nothing sent (night_pilot_stuck_loop_mode=advise).")
        _event(cfg, night, "advise", f"would {what}: {steps}", now, rig=rig)
    else:
        act_text = await _act(cfg, armer, io, st, rig, ep, what, steps, now, night,
                              names, target)
    msg = f"{steps}. Target {win.get('name') or '?'}. Likely: {cause}{act_text}"
    if ep["action"] != "act":
        # an act episode ends with the restart: the clock starts again there
        st.setdefault("open", {})[rig] = ep
    st.setdefault("episodes", []).append(ep)
    _event(cfg, night, "alert", msg, now, rig=rig, signals=v["signals"],
           target=win.get("name"), gap_min=v["gap_min"],
           af=[_iso(t) for t in v["af"]])
    await io.notify(cfg, msg, priority=1)
    return {**_short(v), "episode": "new", "action": ep["action"], "text": msg}


def _busy_item(names, target) -> str | None:
    """A NINA #1 running item that is a calibration / dark / flat / TPoint /
    tracking test slot (night_pilot.BUSY_WORDS), else None."""
    from photonscript.scheduler.night_pilot import BUSY_WORDS
    tgt = str(target or "").strip().lower()
    for n in names or []:
        low = str(n).lower()
        if tgt and low.strip() == tgt:
            continue
        if any(w in low for w in BUSY_WORDS):
            return str(n)
    return None


async def _act(cfg, armer, io, st, rig, ep, what, steps, now, night,
               names=None, target=None) -> str:
    from photonscript.scheduler import night_pilot as npil
    why = npil.live_guard(cfg, armer, {"running": names, "target": target, "phd2": None})
    left = npil._dark_left_min(armer, now)
    if why is None and left is not None and left < npil.RESTART_DARK_MIN:
        why = f"only {int(left)} min of dark left"
    if why:
        ep["action"] = "held"
        _event(cfg, night, "held", f"{what} held: {why}", now, rig=rig)
        return f" Night pilot: {what} held ({why})."
    st.setdefault("restarts", {})[rig] = {"t": _iso(now)}
    if rig == "rc16":
        res = await io.restart(armer, now)
        if not res.get("ok"):
            ep["action"] = "failed"
            st["restarts"].pop(rig, None)    # not tried: a later episode may
            _event(cfg, night, "failed", f"restart refused: {res.get('detail')}", now, rig=rig)
            return f" Night pilot: Restart tonight from now refused ({res.get('detail')})."
        ep["action"] = "act"
        _event(cfg, night, "act", f"Restart tonight from now: {steps}", now, rig=rig)
        return (" Night pilot: Restart tonight from now (after the current sub); "
                "if it loops again tonight the pilot only alerts.")
    ep["action"] = "act"

    def done(ok, detail):
        _event(cfg, night, "act" if ok else "failed",
               f"Piggy-600 companion re-dispatch: {detail}", datetime.utcnow(), rig=rig)
    io.piggy_restart(cfg, armer, done)
    return (" Night pilot: NINA #2 stops after its sub and the companion is "
            "re-dispatched; if it loops again tonight the pilot only alerts.")


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def morning_lines(cfg, date: str) -> list[str]:
    p = state_path(cfg, date)
    if not p.exists():
        return []
    st = load_state(cfg, date)
    out = []
    for ep in st.get("episodes") or []:
        t = _hm(cfg, _parse(ep.get("t")))
        act = {"act": "night pilot restarted it",
               "advise": "advise: the pilot would have restarted it",
               "held": "restart held", "failed": "restart refused",
               "repeat": "looped again after the restart, alert only"}.get(
                   ep.get("action") or "", "alert only")
        end = f", cleared {_hm(cfg, _parse(ep.get('cleared')))}" if ep.get("cleared") else ""
        out.append(f"Stuck loop {t}: {ep.get('steps')} ({act}{end})")
    return out


def status(cfg, night: str) -> dict:
    st = load_state(cfg, night)
    return {"mode": action_mode(cfg), "detect": detect_on(cfg),
            "open": st.get("open") or {}, "episodes": st.get("episodes") or []}


# --------------------------------------------------------------------------
# backtest (read only, nothing sent)
# --------------------------------------------------------------------------

class _ReplayIO:
    def __init__(self, doc: dict):
        self.doc = doc
        self.now = None
        self.pushes: list[str] = []

    async def history(self, cfg, rig):
        rows = (self.doc.get("nina_events") or {}).get(rig)
        if rows is None:
            return None
        return [e for e in rows
                if (event_time(cfg, e.get("Time")) or datetime.max) <= self.now]

    def subs(self, cfg, night):
        from photonscript.shared.sub_time import parse_utc
        out = []
        for r in self.doc.get("subs") or []:
            # a sub is in the log once it is graded (time), else at its end
            t = parse_utc(r.get("time") or r.get("end_utc"))
            if t is not None and t <= self.now:
                out.append(r)
        return out

    async def nina1_running(self, cfg):
        return [], None

    async def notify(self, cfg, msg, priority=1):
        self.pushes.append(msg)

    async def restart(self, armer, now):
        return {"ok": True, "pending": True}

    def piggy_restart(self, cfg, armer, done):
        done(True, "replay")


class _ReplayArmer:
    def __init__(self, cfg, doc):
        self.config = cfg
        self.plan = dict(doc.get("plan") or {})
        self.plan.setdefault("night_of", doc.get("date"))
        self.state = "RUNNING"

    def _dawn(self):
        return _parse(self.plan.get("dawn_utc")) or datetime.max

    def _recipe_tonight(self):
        return None


def backtest(doc: dict, cfg, step_s: float = 60.0, start: datetime | None = None,
             end: datetime | None = None) -> list[dict]:
    """Replay a night document minute by minute through check() with the
    armer RUNNING while NINA #1's event history shows the roof safe.
    cfg.data_dir must be a scratch dir. -> [{t, rig, text}] alerts."""
    from photonscript.scheduler.pilot_backtest import running_windows, unsafe_since
    io = _ReplayIO(doc)
    armer = _ReplayArmer(cfg, doc)
    rows = [t for evs in (doc.get("nina_events") or {}).values()
            for t in (event_time(cfg, e.get("Time")) for e in evs) if t is not None]
    start = start or min(rows)
    end = end or max(rows)
    wins = running_windows(doc)
    out = []
    t = start
    seen = 0

    async def run():
        nonlocal t, seen
        while t <= end:
            io.now = t
            running = any(a - timedelta(minutes=2) <= t and (
                b == datetime.max or t <= b + timedelta(minutes=2)) for a, b in wins)
            if running and unsafe_since(doc, t) is None:
                res = await check(cfg, armer, io, t)
                for rig in ("rc16", "piggyback"):
                    r = res.get(rig) or {}
                    if r.get("episode") == "new":
                        out.append({"t": t, "rig": rig, "text": r.get("text"),
                                    "signals": r.get("signals")})
            t += timedelta(seconds=step_s)
    asyncio.run(run())
    return out
