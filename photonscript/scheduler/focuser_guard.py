"""Focuser position-loss guard: a focuser whose counter dropped to about 0
(or jumped) with nothing to explain it, on either rig.

Why: a focuser that lost its position makes autofocus start from the wrong
place and every "last good focus" position meaningless. 2026-10-12 the RC16
EAF dropped off USB and its counter reset (focus_counter_epoch); 2026-10-24
the settings ledger said "RC16 EAF position counter 0 (was 5674)" only at
dawn. That one was a false alarm: the dawn shutdown had disconnected the
devices and NINA reports Position 0 (Temperature NaN) for a disconnected
focuser. So a reading counts only when the focuser is connected
(connected_position), and a reset is judged against the last CONNECTED
reading, which is where a real reset shows up after a reconnect.

Detection (check(), every focuser_guard_poll_s while the armer is RUNNING,
PAUSED_* or WATCHING, and once at pre-config): the focuser's Position from
ninaAPI equipment/focuser/info against the last connected reading (this
night, else <data_dir>/focuser_guard/last.json from an earlier night):

    zero   now <= focuser_guard_reset_below (1000) and a drop of at least
           focuser_guard_drop_steps (3000): the counter went to about 0
    jump   |change| >= focuser_guard_jump_frac (0.25) x the rig's travel
           (focuser_guard_travel_<rig>; 0 = travel unknown, then
           focuser_guard_drop_steps is the jump size)

A reading taken while the focuser moves, while an autofocus runs (NINA's
event history: AUTOFOCUS-STARTING with no end yet) or while the sequence
runs a focus item is skipped (the baseline stays). Explained, no alarm:
an AUTOFOCUS-FINISHED between the two readings (a jump only: an AF never
ends near 0), or a commanded move (note_move(): the focus model move, the
re-entry gate's move, this guard's own move) whose target is within
MOVE_EXPLAIN_TOL steps of the new reading.

On a detection: a run event (kind focuser_reset, value "<from> -> <to>"),
one Pushover act per rig per night (title "PhotonScript focuser reset";
NOT a safety act: a lost focuser costs one rig's data, never hardware, the
pilot recovers by itself in act mode, and the existing "focuser at 0" and
"autofocus not finishing" acts are non-safety too; from 00:00 to 08:00 it
is held in the storyline and the end-of-night report lists it), the night
story line (notify() writes it, pushed or held), and a pending decision.

The decision (night only, armer RUNNING) waits for the first sub of that
rig that started after the reset, at most focuser_guard_sub_wait_min (15):

    sharp  (passed QA, or HFR within SHARP_HFR_X of the night's pre-reset
           median): only the counter moved, the focuser did not (the
           2026-10-12 failure). Moving to the old position would defocus
           it. act: an autofocus in place (unless one already finished
           after the reset), the focus table re-anchors on it.
    soft / no sub: the focuser really moved. act: move it back to the last
           good autofocus position for the current filter before the
           reset (focus_model points), else the last connected reading,
           then (focuser_guard_af_after_move) a fresh autofocus.

Never moves while the camera exposes (waits for it to go idle between
subs, at most IDLE_WAIT_S), while an autofocus runs or the sequence runs
a focus / flat / dark / TPoint / calibration item. advise: the same
decision, reported only. Pre-config: detection and the push only, no move
(nothing is imaging; the night decides on the first sub).

Mode: focuser_guard_mode off | advise (default) | act; night_pilot_mode
advise (or off) caps it at advise (detection keeps running).

State: <data_dir>/focuser_guard/<night>.json (rigs: last reading, resets,
the pending decision, steps), last.json (each rig's last connected reading
across nights), moves.jsonl (commanded moves). Morning report:
morning_lines(); the sub doctor cites the reset (run event focuser_reset)
in its focuser verdict.
"""
from __future__ import annotations

import asyncio
import json
import logging
import statistics
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

MODES = ("off", "advise", "act")
KIND_RESET = "focuser_reset"      # run event: a detection
KIND = "focuser_guard"            # run event: the guard's steps
TITLE = "PhotonScript focuser reset"
TITLE_FAIL = "PhotonScript focuser restore"
POLL_S = 60.0
MOVE_EXPLAIN_TOL = 150.0          # a commanded move this close explains it
MOVE_TOL_STEPS = 5                # read-back tolerance after our move
MOVE_WAIT_S = 180.0               # a long move (thousands of steps)
MOVE_POLL_S = 2.0
IDLE_WAIT_S = 600.0               # wait at most this for the sub to end
IDLE_POLL_S = 2.0
AF_OPEN_MAX_MIN = 15.0            # an AF with no end event for this long ended
SHARP_HFR_X = 1.25                # post-reset HFR within this of before = sharp
EXPLAIN_MARGIN_S = 30.0
DETECT_STATES = ("RUNNING", "PAUSED_UNSAFE", "PAUSED_OPERATOR", "WATCHING")
# a running sequence item holding any of these: no move, reading skipped
# when it is a focus item (lower case)
FOCUS_WORDS = ("autofocus", "auto focus", "focus", "focuser")
BUSY_WORDS = ("autofocus", "auto focus", "focus", "flat", "dark", "bias",
              "tpoint", "mapping", "tracking test", "calibrat", "optics")
RIG_APP = {"rc16": "nina1", "piggyback": "nina2"}


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and abs(x) != float("inf") else None


def _iso(dt: datetime | None) -> str | None:
    return dt.replace(microsecond=0).isoformat() + "Z" if dt else None


def _parse(v) -> datetime | None:
    if isinstance(v, datetime):
        return v
    from photonscript.scheduler.af_stall import parse_time
    return parse_time(v)


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def own_mode(cfg) -> str:
    m = str(getattr(cfg, "focuser_guard_mode", "advise") or "advise").strip().lower()
    return m if m in MODES else "advise"


def mode(cfg) -> str:
    """The effective mode: focuser_guard_mode, capped at advise by
    night_pilot_mode advise / off (detection keeps running)."""
    m = own_mode(cfg)
    if m == "off":
        return "off"
    pm = str(getattr(cfg, "night_pilot_mode", "act") or "act").strip().lower()
    if pm in ("advise", "off"):
        return "advise"
    return m


def _f(cfg, key: str, default: float) -> float:
    v = _num(getattr(cfg, key, default))
    return default if v is None else v


def thresholds(cfg, rig: str) -> dict:
    below = _f(cfg, "focuser_guard_reset_below", 1000.0)
    drop = _f(cfg, "focuser_guard_drop_steps", 3000.0)
    travel = _f(cfg, f"focuser_guard_travel_{rig}", 0.0)
    frac = _f(cfg, "focuser_guard_jump_frac", 0.25)
    jump = frac * travel if travel > 0 and frac > 0 else drop
    return {"reset_below": below, "drop_steps": drop, "jump_steps": jump,
            "travel": travel or None}


def rigs(cfg) -> list[str]:
    return ["rc16"] + (["piggyback"] if getattr(cfg, "piggyback_enabled", False) else [])


def _label(cfg, rig: str) -> str:
    try:
        from photonscript.shared.rigs import rig_label
        return rig_label(cfg, rig)
    except Exception:  # noqa: BLE001
        return rig


def _hm(cfg, t: datetime | None) -> str:
    try:
        from photonscript.scheduler.nina_watchdog import _local_hm
        return _local_hm(cfg, t)
    except Exception:  # noqa: BLE001
        return t.strftime("%H:%MZ") if t else "?"


# --------------------------------------------------------------------------
# pure pieces
# --------------------------------------------------------------------------

def connected_position(info) -> float | None:
    """The focuser Position when the reading is valid: a dict, Connected
    not False and a numeric Position. Without a Connected field, Position 0
    with a NaN / missing Temperature reads as disconnected (2026-10-24: the
    dawn shutdown disconnected the devices; NINA then reports Position 0,
    Temperature NaN). None = not a reading to compare."""
    if not isinstance(info, dict):
        return None
    if info.get("Connected") is False:
        return None
    pos = _num(info.get("Position"))
    if pos is None:
        return None
    if "Connected" not in info and pos == 0 and _num(info.get("Temperature")) is None:
        return None
    return pos


def classify(prev: float | None, cur: float | None, th: dict) -> str | None:
    """'zero' | 'jump' | None (see the module docstring)."""
    if prev is None or cur is None:
        return None
    if cur <= th["reset_below"] and prev - cur >= th["drop_steps"]:
        return "zero"
    if abs(cur - prev) >= th["jump_steps"]:
        return "jump"
    return None


def _ev_name(e) -> str:
    return str((e or {}).get("Event") or "").upper() if isinstance(e, dict) else ""


def af_running(history, now: datetime) -> bool:
    """NINA's event history: the newest AUTOFOCUS-STARTING has no later
    AUTOFOCUS end (FINISHED or another AUTOFOCUS-* that is not a point) and
    is under AF_OPEN_MAX_MIN old."""
    last_start = None
    for e in history or []:
        n = _ev_name(e)
        if n == "AUTOFOCUS-STARTING":
            last_start = _parse(e.get("Time"))
        elif n.startswith("AUTOFOCUS-") and n != "AUTOFOCUS-POINT-ADDED":
            last_start = None
    return last_start is not None and \
        (now - last_start).total_seconds() / 60.0 < AF_OPEN_MAX_MIN


def _in_window(t, t0: datetime | None, t1: datetime) -> bool:
    if t is None:
        return False
    lo = (t0 - timedelta(seconds=EXPLAIN_MARGIN_S)) if t0 else None
    return (lo is None or t >= lo) and t <= t1 + timedelta(seconds=EXPLAIN_MARGIN_S)


def explain(kind: str, cur: float, prev_t: datetime | None, now: datetime,
            history=None, moves=None) -> str | None:
    """Why the change is not a reset, else None: a commanded move to about
    the new reading, or (a jump only) an autofocus that finished between
    the two readings."""
    for m in moves or []:
        t = _parse(m.get("t"))
        tgt = _num(m.get("position"))
        if tgt is not None and _in_window(t, prev_t, now) and abs(tgt - cur) <= MOVE_EXPLAIN_TOL:
            return f"commanded move to {tgt:g} ({m.get('source') or '?'}, {_iso(t)})"
    if kind == "jump":
        for e in history or []:
            if _ev_name(e) == "AUTOFOCUS-FINISHED" and _in_window(_parse(e.get("Time")), prev_t, now):
                return f"autofocus finished at {e.get('Time')}"
    return None


def reconnects(history, prev_t: datetime | None, now: datetime) -> list[str]:
    """Focuser connect / disconnect events between the readings (evidence
    for the push; ninaAPI names FOCUSER-CONNECTED / -DISCONNECTED)."""
    out = []
    for e in history or []:
        n = _ev_name(e)
        if n.startswith("FOCUSER-") and "CONNECT" in n and \
                _in_window(_parse(e.get("Time")), prev_t, now):
            out.append(f"{n.lower()} {e.get('Time')}")
    return out


def busy_item(names, words=BUSY_WORDS, target: str | None = None) -> str | None:
    """The first running item name holding one of `words` (the running
    target's own name is not an item: a "Dark Shark Nebula" is no dark)."""
    tgt = str(target or "").strip().lower()
    for n in names or []:
        low = str(n).strip().lower()
        if tgt and (low == tgt or low.startswith(tgt)):
            continue
        if any(w in low for w in words):
            return str(n)
    return None


def restore_target(cfg, rig: str, filt: str | None, before: datetime | None,
                   points: list[dict], fallback: float | None) -> tuple[float | None, str]:
    """(position, source): the newest good AF of the current filter (the
    Piggy-600: any) that finished before the reset; else the newest good AF
    of any filter before it; else the last connected reading."""
    from photonscript.scheduler.focus_model import norm_filter
    want = norm_filter(filt, cfg) if filt else None
    rows = []
    for p in points or []:
        t = _parse(p.get("time"))
        pos = _num(p.get("position"))
        if pos is None or pos <= 0 or t is None or (before is not None and t >= before):
            continue
        rows.append((t, pos, str(p.get("filter") or "")))
    rows.sort()
    same = [r for r in rows if rig == "piggyback" or not want or r[2] == want]
    if same:
        t, pos, f = same[-1]
        return pos, f"last good {f or 'OSC'} AF {t:%m-%d %H:%MZ}"
    if rows:
        t, pos, f = rows[-1]
        return pos, f"last good AF ({f}, no {want} AF) {t:%m-%d %H:%MZ}"
    if fallback is not None:
        return fallback, "last known position"
    return None, "no AF and no earlier position"


def sub_verdict(subs: list[dict], rig: str, reset_t: datetime) -> tuple[str | None, str]:
    """('sharp' | 'soft' | None, why) from the first graded sub of the rig
    that started after the reset (None: no such sub yet)."""
    from photonscript.shared.sub_time import sub_start
    mine = [r for r in subs or [] if (r.get("rig") or "rc16") == rig
            and not r.get("is_test")]
    before = [h for r in mine if (s := sub_start(r)) is not None and s < reset_t
              and r.get("passed_qa") and (h := _num(r.get("hfr"))) is not None]
    after = sorted([(s, r) for r in mine if (s := sub_start(r)) is not None and s >= reset_t
                    and (_num(r.get("hfr")) is not None or "passed_qa" in r)],
                   key=lambda x: x[0])
    if not after:
        return None, "no sub after the reset yet"
    r = after[0][1]
    h = _num(r.get("hfr"))
    base = statistics.median(before) if before else None
    if r.get("passed_qa"):
        return "sharp", f"first sub after the reset passed QA (HFR {h if h is not None else '?'})"
    if base and h is not None and h <= SHARP_HFR_X * base:
        return "sharp", f"first sub after the reset HFR {h:.2f} vs {base:.2f} before"
    return "soft", (f"first sub after the reset HFR {h:.2f}" if h is not None else
                    "first sub after the reset rejected") + \
        (f" vs {base:.2f} before" if base else "")


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def state_dir(cfg) -> Path:
    return Path(getattr(cfg, "data_dir", ".")) / "focuser_guard"


def _read(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write(p: Path, obj) -> None:
    try:
        from photonscript.shared.fileops import replace_retry
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, indent=1, default=str), encoding="utf-8")
        replace_retry(tmp, p)
    except OSError as e:
        logger.warning("focuser guard state not saved: %s", e)


def load_state(cfg, night: str) -> dict:
    st = _read(state_dir(cfg) / f"{night}.json")
    if not isinstance(st, dict):
        st = {}
    st.setdefault("night", night)
    st.setdefault("rigs", {})
    return st


def save_state(cfg, st: dict) -> None:
    _write(state_dir(cfg) / f"{st['night']}.json", st)


def load_last(cfg) -> dict:
    d = _read(state_dir(cfg) / "last.json")
    return d if isinstance(d, dict) else {}


def save_last(cfg, d: dict) -> None:
    _write(state_dir(cfg) / "last.json", d)


def note_move(cfg, rig: str, position, source: str, now: datetime | None = None) -> None:
    """Log a commanded focuser move (so the guard does not take it for a
    reset). Never raises."""
    try:
        from photonscript.shared.phd2_store import append_jsonl
        append_jsonl(state_dir(cfg) / "moves.jsonl",
                     {"t": _iso(now or datetime.utcnow()), "rig": rig,
                      "position": _num(position), "source": source})
    except Exception as e:  # noqa: BLE001
        logger.debug("focuser guard: move not logged: %s", e)


def recent_moves(cfg, rig: str, since: datetime | None) -> list[dict]:
    try:
        from photonscript.shared.phd2_store import read_jsonl
        rows = read_jsonl(state_dir(cfg) / "moves.jsonl")[-200:]
    except Exception:  # noqa: BLE001
        return []
    out = []
    for m in rows:
        if (m.get("rig") or "rc16") != rig:
            continue
        t = _parse(m.get("t"))
        if since is None or (t is not None and t >= since - timedelta(seconds=EXPLAIN_MARGIN_S)):
            out.append(m)
    return out


def _event(cfg, night: str, kind: str, value: str, detail: str, now: datetime,
           rig: str, **extra) -> None:
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl
        append_jsonl(events_path(cfg, night),
                     {"t": _iso(now), "rig": rig, "src": "photonscript",
                      "kind": kind, "value": value, "detail": detail, **extra})
    except Exception as e:  # noqa: BLE001
        logger.warning("focuser guard event not logged: %s", e)


def _step(cfg, st, rs: dict, now, rig, value, text, **extra) -> None:
    rs.setdefault("steps", []).append({"t": _iso(now), "value": value, "text": text})
    _event(cfg, st["night"], KIND, value, text, now, rig, **extra)


# --------------------------------------------------------------------------
# I/O (tests replace it)
# --------------------------------------------------------------------------

class GuardIO:
    """ninaAPI GETs on each rig's NINA and the logs. Never raises."""

    async def get(self, cfg, rig: str, path: str, timeout: float = 15.0):
        from photonscript.scheduler.nina_watchdog import rig_base
        from photonscript.scheduler.night_pause import nina_get
        base = rig_base(cfg, rig)
        if not base:
            return None
        try:
            return await nina_get(base, path, timeout=timeout)
        except Exception:  # noqa: BLE001
            return None

    async def focuser(self, cfg, rig):
        return await self.get(cfg, rig, "/equipment/focuser/info")

    async def camera(self, cfg, rig):
        return await self.get(cfg, rig, "/equipment/camera/info")

    async def history(self, cfg, rig):
        h = await self.get(cfg, rig, "/event-history", timeout=20.0)
        return h if isinstance(h, list) else None

    async def filter_name(self, cfg, rig):
        fw = await self.get(cfg, rig, "/equipment/filterwheel/info")
        sel = (fw or {}).get("SelectedFilter") if isinstance(fw, dict) else None
        return (sel or {}).get("Name") if isinstance(sel, dict) else None

    async def running(self, cfg, rig):
        """(names of the RUNNING items, the running target) or (None, None)."""
        from photonscript.scheduler.nina_watchdog import rig_base
        from photonscript.scheduler.sideload import nina_running, read_sequence_state
        from photonscript.scheduler.where_panel import sequence_position
        base = rig_base(cfg, rig)
        if not base:
            return None, None
        try:
            tree, _e = await asyncio.wait_for(read_sequence_state(base), 20)
            if tree is None:
                return None, None
            return nina_running(tree), sequence_position(tree).get("target")
        except Exception:  # noqa: BLE001
            return None, None

    async def move(self, cfg, rig, position: int):
        """ninaAPI GET /equipment/focuser/move?position=N (the path the
        focus model move and the re-entry gate already use)."""
        return await self.get(cfg, rig, f"/equipment/focuser/move?position={int(position)}",
                              timeout=30.0)

    async def autofocus(self, cfg, rig, armer) -> tuple[bool, str]:
        """A direct ninaAPI autofocus between subs (night_pilot.direct_af:
        camera idle, an imaging block, verified on AUTOFOCUS-FINISHED). The
        Piggy-600 falls back to the pilot's stop + companion re-dispatch
        (its image pass starts with AF)."""
        from photonscript.scheduler import night_pilot as npil
        if rig == "rc16":
            return await npil.PilotIO().nina1_autofocus(cfg)

        async def get(path):
            return await self.get(cfg, rig, path, timeout=30.0)

        async def tree():
            from photonscript.scheduler.nina_watchdog import rig_base
            from photonscript.scheduler.sideload import read_sequence_state
            try:
                t, _e = await asyncio.wait_for(read_sequence_state(rig_base(cfg, rig)), 20)
                return t
            except Exception:  # noqa: BLE001
                return None
        res = await npil.direct_af(get, tree)
        if res.get("ok") or not res.get("refused"):
            return bool(res.get("ok")), str(res.get("detail") or "")
        return await npil.PilotIO().piggy_af(cfg, armer)

    def subs(self, cfg, night):
        try:
            from photonscript.scheduler.runs import _load_subs
            return _load_subs(cfg, night)
        except Exception:  # noqa: BLE001
            return []

    def points(self, cfg, rig):
        try:
            from photonscript.scheduler.focus_model import load_points
            return load_points(cfg, rig)
        except Exception:  # noqa: BLE001
            return []

    def calibration_busy(self) -> str | None:
        from photonscript.scheduler.night_pilot import _calibration_busy
        return _calibration_busy()

    def claim_zero_alert(self, cfg, rig: str, night: str) -> None:
        """The telescope agent's "focuser at 0" push (focuser-zero-<rig>-
        <night>) is the same news: one push, ours."""
        try:
            from photonscript.shared.phd2_store import alert_once
            alert_once(cfg, f"focuser-zero-{rig}-{night}")
        except Exception:  # noqa: BLE001
            pass

    async def notify(self, cfg, msg: str, *, title: str = TITLE, priority: int = 1,
                     kind: str = "act", fact: str | None = None) -> None:
        try:
            from photonscript.shared.pushover import notify
            await notify(cfg, msg, title=title, priority=priority, kind=kind, fact=fact)
        except Exception as e:  # noqa: BLE001
            logger.warning("focuser guard push failed: %s", e)

    async def sleep(self, s: float):
        await asyncio.sleep(s)


# --------------------------------------------------------------------------
# texts
# --------------------------------------------------------------------------

def _dark_left(cfg, now) -> str:
    try:
        from photonscript.shared.pushover import dark_left_h
        h = dark_left_h(cfg, now)
        return f" for the remaining ~{h:.1f} h of dark" if h else ""
    except Exception:  # noqa: BLE001
        return ""


def detect_text(cfg, rig: str, r: dict, m: str, phase: str, now: datetime) -> str:
    """The act push. Pure apart from the dark-left lookup."""
    from photonscript.shared.pushover import act_text
    lab = _label(cfg, rig)
    name = r.get("name") or "the focuser"
    kind = ("dropped" if r["kind"] == "zero" else "jumped")
    prob = (f"{lab} focuser {name} position counter {kind} {r['from']:g} -> "
            f"{r['to']:g} at {_hm(cfg, _parse(r['t']))} local "
            f"(last good reading {_hm(cfg, _parse(r.get('prev_t')))}"
            + (", " + "; ".join(r["reconnects"][:2]) if r.get("reconnects") else "")
            + "); no autofocus or commanded move explains it, so autofocus "
            "starts from the wrong place and the last good focus positions "
            "no longer match")
    tgt = r.get("restore")
    plan = (f"move it back to {tgt:g} ({r.get('restore_src')}) then autofocus"
            if tgt is not None else "autofocus")
    check = ("The guard checks the first sub after the reset: sharp = only "
             "the counter moved, then an autofocus in place; soft = the "
             f"focuser moved, then {plan}")
    if phase == "preconfig":
        what = "Found at pre-config: nothing is moved before imaging. " + check
    elif m == "act":
        what = "Pilot (act): " + check[0].lower() + check[1:]
    else:
        what = "Pilot (advise, nothing is sent): " + check[0].lower() + check[1:]
    up = (f"check {name} in NINA (Equipment > Focuser: connected, moving), "
          "compare the next AF with "
          + (f"{tgt:g}" if tgt is not None else "the focus seeds")
          + "; if only the counter moved set focus_counter_epoch to "
          + _hm(cfg, _parse(r["t"])) + " local so the focus table re-anchors"
          + f"; that saves the {lab} subs" + _dark_left(cfg, now))
    down = (f"the pilot's restore is all that runs; if it fails every {lab} "
            "sub stays soft and autofocus keeps failing" if m == "act" and phase != "preconfig" else
            f"nothing is moved, autofocus keeps starting from the wrong "
            f"place and {lab} subs are rejected for the rest of the night")
    return act_text(prob + ". " + what, up, down)


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------

def _night(cfg, armer, now: datetime) -> str:
    plan = getattr(armer, "plan", None)
    n = plan.get("night_of") if isinstance(plan, dict) else None
    if n:
        return n
    from photonscript.shared.phd2_store import night_of
    return night_of(cfg, now)


async def check(cfg, armer, io: GuardIO | None = None, now: datetime | None = None,
                phase: str = "night") -> dict:
    """One pass over both rigs: read, detect, decide. phase "night" (the
    loop) or "preconfig" (the armer, before dispatch: detection only).
    Never raises."""
    io = io or GuardIO()
    now = now or datetime.utcnow()
    m = mode(cfg)
    if m == "off":
        return {"mode": "off"}
    state = str(getattr(armer, "state", "") or "")
    if phase == "night" and state not in DETECT_STATES:
        return {"mode": m, "state": state, "skipped": "not a live night"}
    night = _night(cfg, armer, now)
    st = load_state(cfg, night)
    last = load_last(cfg)
    out = {"mode": m, "night": night, "rigs": {}}
    for rig in rigs(cfg):
        try:
            out["rigs"][rig] = await _check_rig(cfg, armer, io, st, last, rig, now, m, phase)
        except Exception as e:  # noqa: BLE001
            logger.warning("focuser guard (%s) failed: %s", rig, e)
            out["rigs"][rig] = {"error": str(e)}
    save_state(cfg, st)
    save_last(cfg, last)
    return out


async def _check_rig(cfg, armer, io, st, last, rig, now, m, phase) -> dict:
    rs = st["rigs"].setdefault(rig, {"resets": []})
    info = await io.focuser(cfg, rig)
    pos = connected_position(info)
    res: dict = {"position": pos}
    if pos is None:
        rs["unread_t"] = _iso(now)
        res["skipped"] = "focuser not connected / not read"
    elif info.get("IsMoving") is True:
        res["skipped"] = "focuser moving"
    else:
        await _detect(cfg, armer, io, st, rs, last, rig, now, m, phase, info, pos, res)
    if phase == "night" and rs.get("pending") and \
            str(getattr(armer, "state", "")) == "RUNNING":
        await _decide(cfg, armer, io, st, rs, rig, now, m)
    return res


async def _detect(cfg, armer, io, st, rs, last, rig, now, m, phase, info, pos, res):
    prev = rs.get("last") or last.get(rig)
    prev_pos = _num((prev or {}).get("pos"))
    prev_t = _parse((prev or {}).get("t"))

    def keep():
        rec = {"pos": pos, "t": _iso(now), "night": st["night"]}
        rs["last"] = rec
        last[rig] = rec

    kind = classify(prev_pos, pos, thresholds(cfg, rig))
    if kind is None:
        keep()
        return
    hist = await io.history(cfg, rig)
    names, target = await io.running(cfg, rig)
    if af_running(hist, now) or busy_item(names, FOCUS_WORDS, target):
        res["skipped"] = "autofocus / focus item running"
        return            # judged again after it, against the same baseline
    why = explain(kind, pos, prev_t, now, hist, recent_moves(cfg, rig, prev_t))
    if why:
        _step(cfg, st, rs, now, rig, "explained",
              f"{_label(cfg, rig)} focuser {prev_pos:g} -> {pos:g}: {why}")
        res["explained"] = why
        keep()
        return
    filt = None if rig == "piggyback" else await io.filter_name(cfg, rig)
    tgt, src = restore_target(cfg, rig, filt, now, io.points(cfg, rig), prev_pos)
    r = {"t": _iso(now), "from": prev_pos, "to": pos, "kind": kind,
         "prev_t": _iso(prev_t), "phase": phase, "filter": filt,
         "name": str(info.get("DisplayName") or info.get("Name") or "") or None,
         "restore": tgt, "restore_src": src,
         "reconnects": reconnects(hist, prev_t, now), "mode": m}
    rs.setdefault("resets", []).append(r)
    rs["pending"] = {"reset_t": r["t"], "wait_from": None}
    keep()
    res["reset"] = r
    lab = _label(cfg, rig)
    detail = (f"{lab} focuser counter {prev_pos:g} -> {pos:g} ({kind}, {phase}); "
              f"restore candidate {tgt if tgt is None else f'{tgt:g}'} ({src})")
    _event(cfg, st["night"], KIND_RESET, f"{prev_pos:g} -> {pos:g}", detail, now, rig,
           reset_kind=kind, prev_t=r["prev_t"], restore=tgt, phase=phase)
    if pos == 0:
        io.claim_zero_alert(cfg, rig, st["night"])
    await io.notify(cfg, detect_text(cfg, rig, r, m, phase, now), title=TITLE,
                    priority=1, kind="act", fact=f"focuser-reset:{rig}:{st['night']}")
    rs["pushed"] = True


# --------------------------------------------------------------------------
# the decision (night, RUNNING)
# --------------------------------------------------------------------------

async def _af_since(io, cfg, rig, t: datetime) -> bool:
    for e in await io.history(cfg, rig) or []:
        if _ev_name(e) == "AUTOFOCUS-FINISHED":
            et = _parse(e.get("Time"))
            if et is not None and et >= t:
                return True
    return False


async def _decide(cfg, armer, io, st, rs, rig, now, m) -> None:
    p = rs["pending"]
    r = rs["resets"][-1] if rs.get("resets") else {}
    reset_t = _parse(p.get("reset_t")) or now
    subs = io.subs(cfg, st["night"])
    if rig == "rc16":
        # 2026-10-25 quiet carriers: an unguided moon-fill carrier's subs
        # are expected to be rejected (trailed), so they say nothing about
        # focus: no verdict from them, and no decision while the RC16 is on
        # one (the wait starts again once it is off the carrier)
        try:
            from photonscript.scheduler import carrier_quiet as cq
            row = cq.carrier_for(cfg, when=now, armer=armer)
            quiet = cq.quiet_targets(cfg, armer=armer)
        except Exception:  # noqa: BLE001
            row, quiet = None, {}
        if row is not None:
            p["wait_from"] = None
            if not p.get("carrier_noted"):
                p["carrier_noted"] = _iso(now)
                _step(cfg, st, rs, now, rig, "carrier",
                      f"{_label(cfg, rig)} focuser reset: decision waits, the RC16 "
                      f"is an unguided pointing carrier on {row.get('target')} "
                      "(subs not judged)")
            return
        if quiet:
            subs = [r for r in subs or [] if not cq.is_carrier_sub(r, quiet)]
    if not p.get("wait_from"):
        p["wait_from"] = _iso(now)
    verdict, why = sub_verdict(subs, rig, reset_t)
    waited = (now - (_parse(p["wait_from"]) or now)).total_seconds() / 60.0
    if verdict is None and waited < _f(cfg, "focuser_guard_sub_wait_min", 15.0):
        return
    lab = _label(cfg, rig)
    rs["pending"] = None
    r["verdict"] = verdict or "no_sub"
    r["verdict_why"] = why if verdict else f"no sub within {waited:.0f} min"
    if verdict == "sharp":
        af_done = await _af_since(io, cfg, rig, reset_t)
        plan = ("nothing to do: an autofocus already finished after the reset"
                if af_done else "an autofocus in place")
        text = (f"{lab} focuser reset: only the counter moved ({r['verdict_why']}); "
                f"{plan}. The focus seeds / table are {r.get('from', 0) - r.get('to', 0):g} "
                "steps off until the focus table re-anchors on the new AF")
        if m != "act" or af_done:
            r["outcome"] = ("advise: " if m != "act" else "") + plan
            _step(cfg, st, rs, now, rig, "advise" if m != "act" else "counter_only", text)
            await io.notify(cfg, text + (" (advise: nothing was sent)." if m != "act" else "."),
                            title=TITLE, priority=0, kind="info")
            return
        await _restore(cfg, armer, io, st, rs, r, rig, now, None, text)
        return
    tgt = _num(r.get("restore"))
    text = (f"{lab} focuser reset: the focuser moved ({r['verdict_why']}); "
            + (f"move back to {tgt:g} ({r.get('restore_src')})" if tgt is not None
               else "no position to move back to")
            + (" then autofocus" if getattr(cfg, "focuser_guard_af_after_move", True) else ""))
    if m != "act":
        r["outcome"] = "advise: " + text
        _step(cfg, st, rs, now, rig, "advise", text)
        await io.notify(cfg, text + " (advise: nothing was sent).", title=TITLE,
                        priority=0, kind="info")
        return
    await _restore(cfg, armer, io, st, rs, r, rig, now, tgt, text)


def _why_not(cfg, armer, io, names, target) -> str | None:
    state = str(getattr(armer, "state", "") or "")
    if state != "RUNNING":
        return f"armer is {state or 'unknown'}: moves only while RUNNING"
    try:
        from photonscript.scheduler.night_pilot import _recipe_busy
        cal = io.calibration_busy()
        if cal:
            return f"a calibration capture job runs on the {cal}"
        rec = _recipe_busy(armer)
        if rec:
            return f"the arm recipe {rec} is in progress"
    except Exception:  # noqa: BLE001
        pass
    b = busy_item(names, BUSY_WORDS, target)
    return f"NINA runs '{b}'" if b else None


async def safe_move(cfg, armer, io, rig: str, target: float, now: datetime) -> tuple[bool, str]:
    """Move the focuser to `target` between subs: the camera idle (waits for
    the running sub, at most IDLE_WAIT_S), no autofocus running, no focus /
    flat / dark / TPoint / calibration item; then the move and a read-back.
    Never raises."""
    from photonscript.scheduler.night_pause import camera_busy
    try:
        waited = 0.0
        busy = camera_busy(await io.camera(cfg, rig))
        while busy is not False:
            if busy is None:
                return False, "camera unreadable: not moved"
            if waited >= IDLE_WAIT_S:
                return False, "the camera never went idle between subs: not moved"
            await io.sleep(IDLE_POLL_S)
            waited += IDLE_POLL_S
            busy = camera_busy(await io.camera(cfg, rig))
        if af_running(await io.history(cfg, rig), now + timedelta(seconds=waited)):
            return False, "an autofocus is running: not moved"
        names, target_name = await io.running(cfg, rig)
        why = _why_not(cfg, armer, io, names, target_name)
        if why:
            return False, why + ": not moved"
        tgt = int(round(target))
        note_move(cfg, rig, tgt, "focuser_guard", now)
        if await io.move(cfg, rig, tgt) is None:
            return False, "ninaAPI refused the focuser move"
        t = 0.0
        pos = None
        while t <= MOVE_WAIT_S:
            await io.sleep(MOVE_POLL_S)
            t += MOVE_POLL_S
            info = await io.focuser(cfg, rig)
            pos = connected_position(info)
            if isinstance(info, dict) and info.get("IsMoving") is not True and \
                    pos is not None and abs(pos - tgt) <= MOVE_TOL_STEPS:
                return True, f"focuser at {pos:g}"
        return False, (f"focuser read {pos if pos is not None else '?'} after the "
                       f"move to {tgt} (not moving: dropped off USB?)")
    except Exception as e:  # noqa: BLE001
        return False, f"move error: {e}"


async def _restore(cfg, armer, io, st, rs, r, rig, now, tgt, text) -> None:
    lab = _label(cfg, rig)
    steps = []
    ok = True
    if tgt is not None:
        ok, d = await safe_move(cfg, armer, io, rig, tgt, now)
        steps.append(("moved " if ok else "move failed: ") + d)
    if ok and (tgt is None or getattr(cfg, "focuser_guard_af_after_move", True)):
        af_ok, d = await io.autofocus(cfg, rig, armer)
        ok = ok and af_ok
        steps.append(("autofocus: " if af_ok else "autofocus failed: ") + d)
    r["outcome"] = "; ".join(steps)
    msg = f"{text}: {r['outcome']}."
    _step(cfg, st, rs, now, rig, "restored" if ok else "failed", msg)
    if ok:
        await io.notify(cfg, msg, title=TITLE, priority=0, kind="info")
    else:
        from photonscript.shared.pushover import act_text
        await io.notify(cfg, act_text(
            f"{lab} focuser restore after the counter reset did not finish ({r['outcome']})",
            f"check the focuser in NINA, move it to "
            f"{tgt:g} if it moved, then run autofocus" if tgt is not None else
            "check the focuser in NINA and run autofocus",
            f"{lab} subs stay soft for the rest of the night"),
            title=TITLE_FAIL, priority=1, kind="act",
            fact=f"focuser-restore:{rig}:{st['night']}")


# --------------------------------------------------------------------------
# readers for the morning report, the sub doctor, the pilot, the API
# --------------------------------------------------------------------------

def resets(cfg, night: str, rig: str | None = None) -> list[dict]:
    st = load_state(cfg, night)
    out = []
    for rg, rs in (st.get("rigs") or {}).items():
        if rig and rg != rig:
            continue
        for r in rs.get("resets") or []:
            out.append(dict(r, rig=rg))
    return sorted(out, key=lambda x: str(x.get("t")))


def pushed_tonight(cfg, night: str, rig: str) -> bool:
    return bool(((load_state(cfg, night).get("rigs") or {}).get(rig) or {}).get("pushed"))


def morning_lines(cfg, date: str) -> list[str]:
    """One line per reset of the night (rig, time, from -> to, verdict,
    outcome). Empty on a night without one."""
    out = []
    for r in resets(cfg, date):
        t = _parse(r.get("t"))
        line = (f"Focuser reset: {_label(cfg, r['rig'])} {r.get('from', 0):g} -> "
                f"{r.get('to', 0):g} at {_hm(cfg, t)} local ({r.get('phase')})")
        if r.get("verdict"):
            line += f"; {r.get('verdict_why') or r['verdict']}"
        line += f"; {r['outcome']}" if r.get("outcome") else "; no restore ran"
        out.append(line)
    return out


def status(cfg, night: str | None = None) -> dict:
    from photonscript.shared.phd2_store import night_of
    night = night or night_of(cfg)
    st = load_state(cfg, night)
    return {"mode": mode(cfg), "night": night, "rigs": st.get("rigs") or {},
            "last": load_last(cfg), "lines": morning_lines(cfg, night)}


async def run_guard(get_config, get_armer) -> None:
    """Background loop (app startup), every focuser_guard_poll_s. Never
    raises."""
    while True:
        cfg = get_config()
        try:
            await check(cfg, get_armer())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("focuser guard tick failed: %s", e)
        await asyncio.sleep(max(10.0, _f(cfg, "focuser_guard_poll_s", POLL_S)))
