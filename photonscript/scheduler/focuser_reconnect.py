"""Stuck-focuser recovery: disconnect and reconnect the focuser, then AF.

Jeremy (2026-10-26): "If you get stuck with a focuser, then you can always
reset it." The same rule as the cooler-stall recovery (shared/
cooler_recovery.py, "disconnect and reconnect the camera"), for the
focuser: 2026-10-08 to 10-11 the RC16 EAF stopped moving and every
autofocus quit (55 starts, no finish) for three nights; an EAF that is
connected but frozen answers ninaAPI with the same position and the same
temperature forever.

Detection (check(), every POLL_S while the armer is RUNNING or WATCHING),
per rig, from ninaAPI GETs (focuser info, event history, last AF report):

    af_stuck        af_stall_runs (3) autofocus runs in a row that did not
                    finish (af_stall.stall), each with points, and the
                    focuser never moved: every AF point at one position, or
                    no point positions and the focuser reading unchanged
                    since before the first of those runs
    moving_stuck    IsMoving true for focuser_reconnect_moving_min (5) with
                    the position unchanged
    move_ignored    a commanded move (focuser_guard moves.jsonl: the focus
                    model, the re-entry gate, the guard) MOVE_SETTLE_S old
                    with the position still where it was before the move
                    and more than MOVE_TOL_STEPS from the target
    temp_frozen_af  the focuser temperature identical for
                    frozen_sensor_minutes (30) AND a streak of failed AF
                    runs that measured stars (not every point HFR 0, which
                    is clouds) AND the position reading unchanged through
                    those runs (a stale driver). A frozen temperature ALONE
                    never triggers: the Piggy EAF serves a constant 21.29 C
                    on good nights, and on 2026-10-25 its AF failed on
                    clouds while the focuser moved.

AF runs a closed roof cut (af_stall "cut") do not count; clouds move the
focuser normally (AF points at different positions), so they are not
af_stuck.

Action (focuser_reconnect_mode act, night_pilot_mode act, armer RUNNING):
once per rig per night, never while the camera exposes (fresh read; an
unreadable camera counts as busy), while an autofocus runs, while the
sequence runs a focus item, or while a calibration capture job or the arm
recipe runs (retried on the next tick). Disconnects ONLY the focuser
(ninaAPI equipment/focuser/disconnect), waits focuser_reconnect_wait_s,
connects it, reads the position back and compares it with the focuser
guard's last connected reading:
  - no reading after the connect: failed, one act push;
  - a counter change the guard would call a reset (zero / jump): the guard
    runs at once (focuser_guard.check: its detection and push; in act it
    restores and autofocuses); with the guard in advise / off this module
    asks for the autofocus in place (AF steps are relative, so a counter
    reset does not hurt it);
  - otherwise: an autofocus is requested (focuser_guard GuardIO.autofocus:
    the RC16 direct AF between subs, the Piggy stop + companion re-dispatch).
Results: a run event (kind focuser_reconnect), a story line, an info push
when it worked, an act push only when the reconnect or the AF failed.
advise (or WATCHING): the same detection, one info line per rig per night,
nothing sent. Morning report: morning_lines().

State: <data_dir>/focuser_reconnect/<night>.json.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

MODES = ("off", "advise", "act")
KIND = "focuser_reconnect"            # run event
TITLE = "PhotonScript focuser reconnect"
POLL_S = 90.0
DETECT_STATES = ("RUNNING", "WATCHING")
MOVE_SETTLE_S = 90.0                  # a commanded move this old should show
MOVE_TOL_STEPS = 20.0
MOVE_LOOKBACK_MIN = 30.0
AFTER_CONNECT_S = 3.0
REPORT_KEEP = 20


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
    m = str(getattr(cfg, "focuser_reconnect_mode", "act") or "act").strip().lower()
    return m if m in MODES else "act"


def mode(cfg) -> str:
    """focuser_reconnect_mode, capped at advise by night_pilot_mode advise /
    off (detection keeps running), as the focuser guard."""
    m = own_mode(cfg)
    if m == "off":
        return "off"
    pm = str(getattr(cfg, "night_pilot_mode", "act") or "act").strip().lower()
    return "advise" if pm in ("advise", "off") else m


def settings(cfg) -> dict:
    def f(key, default):
        v = _num(getattr(cfg, key, default))
        return default if v is None else v
    return {"runs": max(1, int(f("af_stall_runs", 3))),
            "min_points": max(1, int(f("af_stall_min_points", 4))),
            "moving_s": max(60.0, f("focuser_reconnect_moving_min", 5.0) * 60),
            "frozen_s": max(300.0, f("frozen_sensor_minutes", 30.0) * 60),
            "wait_s": max(0.0, f("focuser_reconnect_wait_s", 5.0))}


def _label(cfg, rig: str) -> str:
    try:
        from photonscript.shared.rigs import rig_label
        return rig_label(cfg, rig)
    except Exception:  # noqa: BLE001
        return rig


def _nina(rig: str) -> str:
    return "NINA #2" if rig == "piggyback" else "NINA #1"


# --------------------------------------------------------------------------
# pure pieces
# --------------------------------------------------------------------------

def track(rs: dict, info: dict | None, now: datetime) -> None:
    """Update the rig's reading history: the position and since when it has
    read the same, the same for the temperature, and since when IsMoving
    has been true."""
    if not isinstance(info, dict):
        return
    pos = _num(info.get("Position"))
    if pos is not None:
        if rs.get("pos") != pos:
            rs["pos"], rs["pos_since"] = pos, _iso(now)
    temp = _num(info.get("Temperature"))
    if temp is None:
        rs.pop("temp", None)
        rs.pop("temp_since", None)
    elif rs.get("temp") != temp:
        rs["temp"], rs["temp_since"] = temp, _iso(now)
    if info.get("IsMoving") is True:
        rs.setdefault("moving_since", _iso(now))
    else:
        rs.pop("moving_since", None)


def _since(rs: dict, key: str) -> datetime | None:
    return _parse(rs.get(key))


def af_streak(runs: list[dict], s: dict, now: datetime,
              watching_since: datetime | None) -> list[dict] | None:
    from photonscript.scheduler.af_stall import stall
    return stall(runs, s["runs"], s["min_points"], now=now,
                 watching_since=watching_since)


def diagnose(rs: dict, runs: list[dict], moves: list[dict], s: dict,
             now: datetime, watching_since: datetime | None = None
             ) -> tuple[str, str] | None:
    """(signal, reason) when the focuser looks stuck, else None. Pure."""
    pos = _num(rs.get("pos"))
    pos_since = _since(rs, "pos_since")
    streak = af_streak(runs, s, now, watching_since)
    if streak and all(r.get("points", 0) >= 1 for r in streak):
        posset = {p for r in streak for p in r.get("positions") or []}
        npos = sum(len(r.get("positions") or []) for r in streak)
        first = streak[0]["start"]
        if (len(posset) == 1 and npos >= 2) or (
                not posset and pos_since is not None and pos_since <= first):
            at = (f"{next(iter(posset))}" if posset else
                  (f"{pos:g}" if pos is not None else "?"))
            pts = ", ".join(str(r["points"]) for r in streak)
            return "af_stuck", (f"{len(streak)} autofocus runs in a row did not "
                                f"finish (points {pts}) and the focuser never "
                                f"moved from {at}")
    mv = _since(rs, "moving_since")
    if mv is not None and (now - mv).total_seconds() >= s["moving_s"] \
            and pos_since is not None and pos_since <= mv:
        mins = (now - mv).total_seconds() / 60
        return "moving_stuck", (f"IsMoving for {mins:.0f} min with the position "
                                f"stuck at {pos:g}" if pos is not None else
                                f"IsMoving for {mins:.0f} min")
    for m in reversed(moves or []):
        t = _parse(m.get("t"))
        tgt = _num(m.get("position"))
        if t is None or tgt is None or pos is None:
            continue
        age = (now - t).total_seconds()
        if age < MOVE_SETTLE_S or age > MOVE_LOOKBACK_MIN * 60:
            continue
        if pos_since is not None and pos_since <= t and abs(tgt - pos) > MOVE_TOL_STEPS:
            return "move_ignored", (f"a move to {tgt:g} ({m.get('source') or 'commanded'}, "
                                    f"{age / 60:.0f} min ago) left the focuser at {pos:g}")
        break       # only the newest settled move is judged
    ts = _since(rs, "temp_since")
    if ts is not None and (now - ts).total_seconds() >= s["frozen_s"] and streak \
            and pos_since is not None and pos_since <= streak[0]["start"]:
        # the position reading did not change through the failed runs
        # either (an AF that really moves the focuser shows on the reading:
        # 2026-10-25 Piggy, 21.29 C all night, AF failed on clouds, the
        # focuser moved: no reconnect)
        pts = sum(r.get("points", 0) for r in streak)
        zero = sum(r.get("hfr_zero", 0) for r in streak)
        if pts and zero < pts:      # stars were measured: not clouds
            return "temp_frozen_af", (
                f"focuser temperature frozen at {rs.get('temp'):g} C for "
                f"{(now - ts).total_seconds() / 60:.0f} min and {len(streak)} "
                "autofocus runs in a row did not finish")
    return None


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def state_dir(cfg) -> Path:
    return Path(getattr(cfg, "data_dir", ".")) / "focuser_reconnect"


def _path(cfg, night: str) -> Path:
    return state_dir(cfg) / f"{night}.json"


def load_state(cfg, night: str) -> dict:
    try:
        d = json.loads(_path(cfg, night).read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("rigs", {})
            d["night"] = night
            return d
    except Exception:  # noqa: BLE001
        pass
    return {"night": night, "rigs": {}}


def save_state(cfg, st: dict) -> None:
    try:
        p = _path(cfg, st["night"])
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:  # noqa: BLE001
        logger.warning("focuser reconnect state not written: %s", e)


def _event(cfg, night: str, rig: str, value: str, detail: str, now: datetime,
           **extra) -> None:
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl
        append_jsonl(events_path(cfg, night),
                     {"t": _iso(now), "rig": rig, "src": "photonscript",
                      "kind": KIND, "value": value, "detail": detail, **extra})
    except Exception as e:  # noqa: BLE001
        logger.warning("focuser reconnect event not logged: %s", e)


def baseline(cfg, night: str, rig: str) -> dict | None:
    """The focuser guard's last connected reading of this rig (tonight,
    else across nights): {"pos", "t"}."""
    from photonscript.scheduler import focuser_guard as fg
    rec = ((fg.load_state(cfg, night).get("rigs") or {}).get(rig) or {}).get("last")
    return rec or fg.load_last(cfg).get(rig)


# --------------------------------------------------------------------------
# I/O (tests replace it)
# --------------------------------------------------------------------------

def _guard_io():
    from photonscript.scheduler.focuser_guard import GuardIO

    class ReconnectIO(GuardIO):
        """The focuser guard's ninaAPI GETs plus the focuser disconnect /
        connect and the last AF report."""

        async def disconnect(self, cfg, rig):
            return await self.get(cfg, rig, "/equipment/focuser/disconnect", timeout=30.0)

        async def connect(self, cfg, rig):
            return await self.get(cfg, rig, "/equipment/focuser/connect", timeout=60.0)

        async def last_af(self, cfg, rig):
            r = await self.get(cfg, rig, "/equipment/focuser/last-af", timeout=20.0)
            return r if isinstance(r, dict) else None

        async def notify(self, cfg, msg: str, *, title: str = TITLE, priority: int = 0,
                         kind: str = "info", fact: str | None = None) -> None:
            await GuardIO.notify(self, cfg, msg, title=title, priority=priority,
                                 kind=kind, fact=fact)

        async def guard_check(self, cfg, armer):
            from photonscript.scheduler import focuser_guard as fg
            return await fg.check(cfg, armer)

        def guard_mode(self, cfg) -> str:
            from photonscript.scheduler import focuser_guard as fg
            return fg.mode(cfg)

        def moves(self, cfg, rig, since):
            from photonscript.scheduler import focuser_guard as fg
            return fg.recent_moves(cfg, rig, since)

        def baseline(self, cfg, night, rig):
            return baseline(cfg, night, rig)

        def recipe_busy(self, armer) -> str | None:
            try:
                from photonscript.scheduler.night_pilot import _recipe_busy
                return _recipe_busy(armer)
            except Exception:  # noqa: BLE001
                return None

    return ReconnectIO()


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


async def check(cfg, armer, io=None, now: datetime | None = None) -> dict:
    """One pass over both rigs. Never raises."""
    io = io or _guard_io()
    now = now or datetime.utcnow()
    m = mode(cfg)
    if m == "off":
        return {"mode": "off"}
    state = str(getattr(armer, "state", "") or "")
    if state not in DETECT_STATES:
        return {"mode": m, "state": state, "skipped": "not a live night"}
    from photonscript.scheduler.focuser_guard import rigs
    night = _night(cfg, armer, now)
    st = load_state(cfg, night)
    st.setdefault("watch_since", _iso(now))
    out = {"mode": m, "night": night, "rigs": {}}
    for rig in rigs(cfg):
        try:
            out["rigs"][rig] = await _check_rig(cfg, armer, io, st, rig, now, m, state)
        except Exception as e:  # noqa: BLE001
            logger.warning("focuser reconnect (%s) failed: %s", rig, e)
            out["rigs"][rig] = {"error": str(e)}
    save_state(cfg, st)
    return out


async def _check_rig(cfg, armer, io, st, rig, now, m, state) -> dict:
    from photonscript.scheduler import af_stall
    from photonscript.scheduler.focuser_guard import connected_position
    rs = st["rigs"].setdefault(rig, {})
    if rs.get("done"):
        return {"skipped": "reconnected once tonight"}
    info = await io.focuser(cfg, rig)
    if connected_position(info) is None:
        return {"skipped": "focuser not connected / not read"}
    track(rs, info, now)
    rep = await io.last_af(cfg, rig)
    if af_stall.report_ok(rep) is not None:
        reps = rs.setdefault("reports", {})
        reps[str(rep.get("Timestamp"))] = rep
        for k in sorted(reps)[:-REPORT_KEEP]:
            reps.pop(k, None)
    hist = await io.history(cfg, rig)
    runs = af_stall.tonight(cfg, af_stall.af_runs(
        hist or [], list((rs.get("reports") or {}).values())), st["night"])
    since = now - timedelta(minutes=MOVE_LOOKBACK_MIN)
    diag = diagnose(rs, runs, io.moves(cfg, rig, since), settings(cfg), now,
                    _parse(st.get("watch_since")))
    if not diag:
        rs.pop("deferred", None)
        return {"stuck": False}
    signal, reason = diag
    lab = _label(cfg, rig)
    if m != "act" or state != "RUNNING":
        if not rs.get("advised"):
            rs["advised"] = {"t": _iso(now), "signal": signal, "reason": reason}
            why = ("night pilot advise" if m != "act" else f"armer {state}: watch only")
            _event(cfg, st["night"], rig, "advised", f"{signal}: {reason}", now)
            await io.notify(cfg, f"{lab}: the focuser looks stuck ({reason}). "
                            f"Would disconnect and reconnect it in {_nina(rig)}, "
                            f"then autofocus ({why}; nothing was sent).",
                            title=TITLE, priority=0, kind="info")
        return {"stuck": True, "signal": signal, "advised": True}
    why_not = await _why_not(cfg, armer, io, rig, hist, now)
    if why_not:
        if rs.get("deferred") != why_not:
            logger.info("focuser reconnect [%s]: stuck (%s); waiting: %s",
                        rig, reason, why_not)
        rs["deferred"] = why_not
        return {"stuck": True, "signal": signal, "deferred": why_not}
    return await _reconnect(cfg, armer, io, st, rs, rig, now, signal, reason)


async def _why_not(cfg, armer, io, rig, hist, now) -> str | None:
    """Why the reconnect must wait (None = go): the camera exposing or
    unreadable, an AF running, a focus / calibration item, a capture job,
    the arm recipe."""
    from photonscript.scheduler.focuser_guard import FOCUS_WORDS, af_running, busy_item
    from photonscript.shared.cooler_recovery import camera_busy
    if af_running(hist, now):
        return "an autofocus is running"
    cal = io.calibration_busy()
    if cal:
        return f"a calibration capture job runs on the {cal}"
    rec = io.recipe_busy(armer)
    if rec:
        return f"the arm recipe {rec} is in progress"
    names, target = await io.running(cfg, rig)
    b = busy_item(names, FOCUS_WORDS + ("flat", "dark", "bias", "calibrat"), target)
    if b:
        return f"NINA runs '{b}'"
    if camera_busy(await io.camera(cfg, rig)):
        return "the camera is exposing (or unreadable)"
    return None


async def _reconnect(cfg, armer, io, st, rs, rig, now, signal, reason) -> dict:
    from photonscript.scheduler.focuser_guard import classify, connected_position, thresholds
    s = settings(cfg)
    lab = _label(cfg, rig)
    night = st["night"]
    base = io.baseline(cfg, night, rig) or {}
    base_pos = _num(base.get("pos"))
    rec = {"t": _iso(now), "signal": signal, "reason": reason,
           "baseline": base_pos, "baseline_t": base.get("t"), "result": "pending"}
    rs["done"] = rec           # once per rig per night, whatever happens next
    save_state(cfg, st)
    logger.warning("focuser reconnect [%s]: %s; disconnecting the focuser", rig, reason)
    _event(cfg, night, rig, "stuck", f"{signal}: {reason}", now)
    err = None
    try:
        if await io.disconnect(cfg, rig) is None:
            err = "the disconnect call failed"
        await io.sleep(s["wait_s"])
        if await io.connect(cfg, rig) is None:
            err = (err + "; " if err else "") + "the connect call failed"
        await io.sleep(AFTER_CONNECT_S)
        info = await io.focuser(cfg, rig)
    except Exception as e:  # noqa: BLE001
        err, info = f"{type(e).__name__}: {e}", None
    pos = connected_position(info)
    rec["after"] = pos
    if pos is None:
        rec["result"] = "failed"
        detail = f"{err or 'no position read back'}; focuser not connected after it"
        _event(cfg, night, rig, "failed", detail, now)
        await io.notify(cfg, _fail_text(cfg, rig, reason, detail),
                        title=TITLE, priority=1, kind="act",
                        fact=f"focuser-reconnect:{rig}:{night}")
        return {"stuck": True, "signal": signal, "action": "failed", "detail": detail}
    kind = classify(base_pos, pos, thresholds(cfg, rig))
    base_txt = "?" if base_pos is None else f"{base_pos:g}"
    read = f"position reads {pos:g} (last good {base_txt})"
    if kind:
        # the counter moved: the focuser guard judges it now, before an AF
        # could explain the change away
        rec["counter"] = kind
        _event(cfg, night, rig, "counter", f"{base_txt} -> {pos:g} ({kind})", now)
        try:
            await io.guard_check(cfg, armer)
        except Exception as e:  # noqa: BLE001
            logger.warning("focuser reconnect: guard check failed: %s", e)
        if io.guard_mode(cfg) == "act":
            rec["result"] = "guard"
            text = (f"{lab}: the focuser looked stuck ({reason}); disconnected and "
                    f"reconnected it in {_nina(rig)}. The counter came back changed "
                    f"({base_txt} -> {pos:g}): the focuser guard restores it and "
                    "autofocuses.")
            _event(cfg, night, rig, "guard", text, now)
            await io.notify(cfg, text, title=TITLE, priority=0, kind="info")
            return {"stuck": True, "signal": signal, "action": "guard", "after": pos}
        read += f"; the counter changed ({kind}), flagged by the focuser guard"
    ok, detail = await io.autofocus(cfg, rig, armer)
    rec["af"] = {"ok": bool(ok), "detail": detail}
    rec["result"] = "recovered" if ok else "af_failed"
    if ok:
        text = (f"{lab}: the focuser looked stuck ({reason}); disconnected and "
                f"reconnected it in {_nina(rig)}, {read}, then autofocused: {detail}.")
        _event(cfg, night, rig, "recovered", text, now)
        await io.notify(cfg, text, title=TITLE, priority=0, kind="info")
        return {"stuck": True, "signal": signal, "action": "recovered", "after": pos}
    detail2 = f"reconnected ({read}) but the autofocus failed: {detail}"
    _event(cfg, night, rig, "af_failed", detail2, now)
    await io.notify(cfg, _fail_text(cfg, rig, reason, detail2), title=TITLE,
                    priority=1, kind="act", fact=f"focuser-reconnect:{rig}:{night}")
    return {"stuck": True, "signal": signal, "action": "af_failed", "after": pos}


def _fail_text(cfg, rig: str, reason: str, detail: str) -> str:
    from photonscript.shared.pushover import act_text
    lab = _label(cfg, rig)
    return act_text(
        f"{lab} focuser looked stuck ({reason}); the pilot reconnected it once in "
        f"{_nina(rig)}: {detail}. No more reconnects tonight",
        f"check the focuser in {_nina(rig)} (Equipment > Focuser: connected, "
        "moves a few steps; reseat the EAF USB) and rerun autofocus; that saves "
        f"the {lab} subs for the rest of the night",
        f"autofocus keeps failing and every {lab} sub stays soft")


# --------------------------------------------------------------------------
# reports, loop
# --------------------------------------------------------------------------

def morning_lines(cfg, date: str) -> list[str]:
    st = load_state(cfg, date)
    out = []
    for rig, rs in sorted((st.get("rigs") or {}).items()):
        d = rs.get("done")
        if not d:
            continue
        res = {"recovered": "reconnected, autofocus ok",
               "guard": "reconnected, counter changed: focuser guard restore",
               "af_failed": "reconnected, autofocus FAILED",
               "failed": "reconnect FAILED (no reading after it)"}.get(
                   d.get("result"), d.get("result") or "?")
        out.append(f"Focuser reconnect {_label(cfg, rig)}: {res} ({d.get('reason')}).")
    return out


def status(cfg, night: str | None = None) -> dict:
    from photonscript.shared.phd2_store import night_of
    night = night or night_of(cfg)
    st = load_state(cfg, night)
    return {"mode": mode(cfg), "night": night, "rigs": st.get("rigs") or {},
            "lines": morning_lines(cfg, night)}


async def run_loop(get_config, get_armer) -> None:
    """Background loop (app startup), every POLL_S. Never raises."""
    while True:
        cfg = get_config()
        try:
            await check(cfg, get_armer())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("focuser reconnect tick failed: %s", e)
        await asyncio.sleep(POLL_S)
