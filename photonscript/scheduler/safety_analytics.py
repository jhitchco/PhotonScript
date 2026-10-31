"""Safety-monitor analytics (PS-1): why did the monitor read unsafe?

Both NINAs read the same AARO device ("AARO Safety Obs 2") through their own
Alpaca driver (NINA #1 AlpacaDynamic3, NINA #2 AlpacaDynamic4). NINA logs
every state change and every connect / disconnect of its safety monitor:

    ...|INFO|SafetyMonitorVM.cs|UpdateMonitorValues|95|SafetyMonitorInfo state changed to Unsafe
    ...|INFO|SafetyMonitorVM.cs|Disconnect|221|Disconnected Safety Monitor
    ...|INFO|SafetyMonitorVM.cs|Connect|175|Successfully connected Safety Monitor. Id: ...

That is enough to separate the three kinds of "unsafe":

- weather: a clean Safe <-> Unsafe change. When both NINAs log it within
  `match_s` of each other it is AARO's own state, not a driver problem.
- connection loss: NINA logs "changed to Unsafe" and then "Disconnected"
  within LOST_WINDOW_S (a failed driver read reads as unsafe). Seen on both
  NINAs at once it is the AARO Alpaca server; on one only, that NINA's driver.
- suspect: a clean change seen by one NINA while the other stayed connected
  in the opposite state. The cross-check rule: log it, never act on it.

NINA does not log the state it reads right after a connect, so a segment
after a connect is back-filled from the next logged change (a change TO
unsafe means it was safe before) and marked inferred; with no later change
it stays unknown.

Times: NINA logs the scope PC's local clock; everything returned is naive
UTC ISO with a trailing Z. Read-only: nothing here touches NINA or the armer.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

LOST_WINDOW_S = 2.0          # "changed to Unsafe" then "Disconnected" = lost
DEFAULT_MATCH_S = 60.0       # same change on the other NINA within this
DEFAULT_FLAP_S = 300.0       # a safe or unsafe window shorter than this
DEBOUNCE_CANDIDATES_S = (60, 120, 300, 600, 900)

_LINE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?)\|\w+\|"
                   r"SafetyMonitorVM\.cs\|(\w+)\|\d+\|(.*)$")

RIGS = ("rc16", "piggyback")
RIG_LABEL = {"rc16": "NINA #1 (RC16)", "piggyback": "NINA #2 (Piggy-600)"}


def _z(t: datetime) -> str:
    return t.replace(microsecond=0).isoformat() + "Z"


# -- parsing ---------------------------------------------------------------

def parse_lines(lines) -> list[tuple[datetime, str]]:
    """NINA log lines -> [(local time, kind)] with kind safe / unsafe /
    connect / disconnect. Other lines are ignored; order is kept."""
    out = []
    for line in lines:
        if "SafetyMonitorVM" not in line:
            continue
        m = _LINE.match(line.strip())
        if not m:
            continue
        ts, fn, msg = m.groups()
        try:
            t = datetime.fromisoformat(ts)
        except ValueError:
            continue
        if "state changed to" in msg:
            word = msg.rsplit(" ", 1)[-1].strip().lower()
            if word in ("safe", "unsafe"):
                out.append((t, word))
        elif fn == "Connect" and "Successfully connected" in msg:
            out.append((t, "connect"))
        elif fn == "Disconnect":
            out.append((t, "disconnect"))
    return out


_PARSE_CACHE: dict[str, tuple] = {}


def parse_file(p: Path) -> list[tuple[datetime, str]]:
    """parse_lines over one file, cached on (size, mtime): a night's NINA
    logs can be large and the multi-night summary rereads them."""
    try:
        st = Path(p).stat()
    except OSError:
        return []
    key, sig = str(p), (st.st_size, st.st_mtime)
    hit = _PARSE_CACHE.get(key)
    if hit and hit[0] == sig:
        return hit[1]
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            ev = parse_lines(fh)
    except OSError:
        return []
    _PARSE_CACHE[key] = (sig, ev)
    return ev


def classify(events: list[tuple[datetime, str]],
             lost_window_s: float = LOST_WINDOW_S) -> list[tuple[datetime, str]]:
    """Raw events -> [(t, kind)] with kind safe / unsafe / lost / offline /
    connect. An unsafe immediately followed by a disconnect is a connection
    loss ("lost"); a bare disconnect is "offline" (operator, NINA exit).
    Duplicate disconnects collapse."""
    # dedupe (overlapping log reads) but keep NINA's order within a
    # timestamp: "Unsafe" then "Disconnected" can share one millisecond
    ev = sorted(dict.fromkeys(events), key=lambda e: e[0])
    out: list[tuple[datetime, str]] = []
    skip = set()
    for i, (t, k) in enumerate(ev):
        if i in skip:
            continue
        if k == "unsafe":
            j = next((j for j in range(i + 1, min(i + 4, len(ev)))
                      if ev[j][1] == "disconnect"
                      and (ev[j][0] - t).total_seconds() <= lost_window_s), None)
            if j is not None:
                out.append((t, "lost"))
                skip.update(range(i + 1, j + 1))
                continue
        if k == "disconnect":
            if out and out[-1][1] in ("lost", "offline"):
                continue
            out.append((t, "offline"))
            continue
        out.append((t, k))
    return out


# -- segments --------------------------------------------------------------

def segments(classified: list[tuple[datetime, str]], start: datetime,
             end: datetime, initial: str = "unknown",
             initial_after_connect: bool = False) -> list[dict]:
    """Classified events (UTC) -> contiguous segments covering [start, end):
    {"start", "end", "state", "inferred"} with state safe / unsafe / offline /
    unknown. The span after a connect takes the opposite of the next logged
    change (inferred) or stays unknown."""
    cur, cur_t = initial, start
    inferred = "after_connect" if (initial == "unknown"
                                   and initial_after_connect) else False
    raw: list[list] = []

    def close(t):
        if t > cur_t:
            raw.append([cur_t, t, cur, inferred])

    for t, k in sorted(classified):
        if t >= end:
            break
        tt = max(t, start)
        if k in ("safe", "unsafe"):
            close(tt)
            if raw and raw[-1][2] == "unknown" and raw[-1][3] == "after_connect":
                raw[-1][2] = "safe" if k == "unsafe" else "unsafe"
                raw[-1][3] = True
            cur, cur_t, inferred = k, tt, False
        elif k in ("lost", "offline"):
            close(tt)
            cur, cur_t, inferred = "offline", tt, False
        elif k == "connect":
            close(tt)
            cur, cur_t, inferred = "unknown", tt, "after_connect"
    close(end)
    out = []
    for a, b, st, inf in raw:
        seg = {"start": a, "end": b, "state": st, "inferred": inf is True}
        if out and out[-1]["state"] == st and out[-1]["end"] == a \
                and out[-1]["inferred"] == seg["inferred"]:
            out[-1]["end"] = b
        else:
            out.append(seg)
    return out


def _state_at(segs: list[dict], t: datetime) -> str | None:
    for s in segs:
        if s["start"] <= t < s["end"]:
            return s["state"]
    return None


def flaps(segs: list[dict], flap_s: float = DEFAULT_FLAP_S) -> list[dict]:
    """Short windows bounded by the opposite state on both sides:
    unsafe -> safe -> unsafe (a safe blip) or safe -> unsafe -> safe."""
    out = []
    for i in range(1, len(segs) - 1):
        a, s, b = segs[i - 1], segs[i], segs[i + 1]
        if s["state"] not in ("safe", "unsafe"):
            continue
        other = "unsafe" if s["state"] == "safe" else "safe"
        dur = (s["end"] - s["start"]).total_seconds()
        if a["state"] == other and b["state"] == other and dur < flap_s:
            out.append({"start": s["start"], "end": s["end"],
                        "state": s["state"], "seconds": round(dur)})
    return out


def _minutes(segs, state) -> float:
    return round(sum((s["end"] - s["start"]).total_seconds()
                     for s in segs if s["state"] == state) / 60, 1)


# -- cross-check -----------------------------------------------------------

def cross_check(rigs: dict, match_s: float = DEFAULT_MATCH_S) -> dict:
    """Pair every clean change and connection loss on one NINA with the other
    NINA's log. Returns {"agreed", "suspects", "unverified", "losses"}:
    agreed = both NINAs logged the same change within match_s; suspect = the
    other NINA was connected and stayed in the opposite state; unverified =
    the other NINA was offline / unknown / has no log."""
    names = [r for r in RIGS if r in rigs]
    agreed, suspects, unverified, losses = [], [], [], []
    seen_pairs = set()
    for r in names:
        other = next((o for o in names if o != r), None)
        mine = rigs[r]
        theirs = rigs[other] if other else None
        for t, k in mine["events"]:
            if k not in ("safe", "unsafe", "lost"):
                continue
            match = None
            if theirs is not None:
                match = next((u for u, kk in theirs["events"] if kk == k
                              and abs((u - t).total_seconds()) <= match_s), None)
            if k == "lost":
                losses.append({"t": _z(t), "rig": r,
                               "both": match is not None})
                continue
            if match is not None:
                key = tuple(sorted([(r, t), (other, match)]))
                if key not in seen_pairs:
                    seen_pairs.add(key)
                    agreed.append({"t": _z(min(t, match)), "state": k,
                                   "lag_s": round(abs((match - t).total_seconds()), 1)})
                continue
            row = {"t": _z(t), "rig": r, "state": k}
            if theirs is None:
                unverified.append({**row, "why": "no log for the other NINA"})
                continue
            later = t + timedelta(seconds=match_s)
            st0, st1 = _state_at(theirs["segments"], t), _state_at(theirs["segments"], later)
            opposite = "safe" if k == "unsafe" else "unsafe"
            if st0 == opposite and st1 == opposite:
                suspects.append({**row, "other": other, "other_state": opposite})
            else:
                unverified.append({**row, "why": f"other NINA {st0 or 'no data'}"})
    losses.sort(key=lambda x: x["t"])
    return {"agreed": sorted(agreed, key=lambda x: x["t"]),
            "suspects": sorted(suspects, key=lambda x: x["t"]),
            "unverified": sorted(unverified, key=lambda x: x["t"]),
            "losses": losses}


def debounce_table(segs: list[dict], current_s: int,
                   candidates=DEBOUNCE_CANDIDATES_S) -> list[dict]:
    """For each confirm-safe hold: how many safe windows it would have
    swallowed (no unpark / park cycle) and how much safe time the hold costs
    over the night. Only safe windows that follow an unsafe one count: the
    hold starts when the monitor turns safe again."""
    wins = [(s["end"] - s["start"]).total_seconds()
            for i, s in enumerate(segs)
            if s["state"] == "safe" and i > 0 and segs[i - 1]["state"] == "unsafe"]
    rows = []
    for c in sorted(set(list(candidates) + [int(current_s)])):
        rows.append({"hold_s": c, "current": c == int(current_s),
                     "resumes": len(wins),
                     "swallowed": sum(1 for w in wins if w < c),
                     "safe_min_spent": round(sum(min(w, c) for w in wins) / 60, 1)})
    return rows


# -- one night -------------------------------------------------------------

def analyze(raw: dict, start_utc: datetime, end_utc: datetime,
            utc_offset_h: float, armer: list | None = None,
            match_s: float = DEFAULT_MATCH_S, flap_s: float = DEFAULT_FLAP_S,
            confirm_s: int = 120) -> dict:
    """raw = {rig: [(local time, kind), ...]} from parse_lines. Returns the
    per-rig segments, transitions, flaps and connection losses, the
    cross-check, the debounce table and the local hour of each unsafe
    onset. armer = [(utc, "safe"|"unsafe"|"unknown")] from
    safety_history.jsonl, shown as a third row."""
    shift = timedelta(hours=-float(utc_offset_h))
    rigs = {}
    for r in RIGS:
        if r not in raw:
            continue
        ev = [(t + shift, k) for t, k in classify(raw[r])]
        ev = [(t, k) for t, k in ev
              if start_utc - timedelta(hours=12) <= t < end_utc]
        before = [k for t, k in ev if t < start_utc]
        init = "unknown"
        for k in before:
            init = {"safe": "safe", "unsafe": "unsafe", "lost": "offline",
                    "offline": "offline", "connect": "unknown"}[k]
        ev_in = [(t, k) for t, k in ev if t >= start_utc]
        segs = segments(ev_in, start_utc, end_utc, initial=init,
                        initial_after_connect=bool(before)
                        and before[-1] == "connect")
        rigs[r] = {"events": ev_in, "segments": segs}
    xc = cross_check(rigs, match_s=match_s)
    out_rigs = {}
    for r, d in rigs.items():
        segs = d["segments"]
        ev = d["events"]
        out_rigs[r] = {
            "label": RIG_LABEL[r],
            "segments": [{**s, "start": _z(s["start"]), "end": _z(s["end"])}
                         for s in segs],
            "transitions": sum(1 for _t, k in ev if k in ("safe", "unsafe")),
            "to_unsafe": sum(1 for _t, k in ev if k == "unsafe"),
            "connection_losses": sum(1 for _t, k in ev if k == "lost"),
            "offline_events": sum(1 for _t, k in ev if k == "offline"),
            "flaps": [{**f, "start": _z(f["start"]), "end": _z(f["end"])}
                      for f in flaps(segs, flap_s)],
            "minutes": {st: _minutes(segs, st)
                        for st in ("safe", "unsafe", "offline", "unknown")},
        }
    ref = rigs.get("rc16") or rigs.get("piggyback")
    onsets = []
    if ref:
        for t, k in ref["events"]:
            if k == "unsafe":
                onsets.append((t - shift).strftime("%H:%M"))
    armer_row = None
    if armer:
        ev = sorted((t, st) for t, st in armer if t < end_utc)
        init = "unknown"
        for t, st in ev:
            if t < start_utc:
                init = st
        cl = [(t, st if st != "unknown" else "offline")
              for t, st in ev if t >= start_utc]
        segs = segments(cl, start_utc, end_utc, initial=init)
        armer_row = {"label": "armer (NINA #1 via API)",
                     "segments": [{**s, "start": _z(s["start"]),
                                   "end": _z(s["end"])} for s in segs]}
    ref_name = "rc16" if "rc16" in rigs else "piggyback"
    verdict = _verdict(out_rigs, xc, ref_name)
    return {"ok": True, "t0": _z(start_utc), "t1": _z(end_utc),
            "utc_offset_h": utc_offset_h, "match_s": match_s, "flap_s": flap_s,
            "rigs": out_rigs, "armer": armer_row, "cross_check": xc,
            "debounce": debounce_table(ref["segments"], confirm_s) if ref else [],
            "unsafe_onsets_local": onsets, "verdict": verdict}


def _verdict(rigs: dict, xc: dict, ref: str = "rc16") -> str:
    """One line for the runs page."""
    if not rigs:
        return "no NINA safety-monitor lines for this night"
    parts = []
    n_ag, n_sus = len(xc["agreed"]), len(xc["suspects"])
    if len(rigs) == 2:
        parts.append(f"{n_ag} change(s) seen by both NINAs (AARO's own state)")
    else:
        only = next(iter(rigs.values()))
        parts.append(f"{only['transitions']} change(s), {only['label']} only "
                     "(no log for the other NINA, no cross-check)")
    if n_sus:
        parts.append(f"{n_sus} SUSPECT change(s) seen by one NINA only")
    lost = xc["losses"]
    if lost:
        both = sum(1 for x in lost if x["both"])
        parts.append(f"{len(lost)} connection loss(es) read as unsafe"
                     + (f" ({both} on both NINAs at once)" if both else ""))
    nfl = len((rigs.get(ref) or next(iter(rigs.values())))["flaps"])
    if nfl:
        parts.append(f"{nfl} short flap(s)")
    return "; ".join(parts) or "no safety changes logged"


# -- glue: config, files ---------------------------------------------------

def night_bounds_utc(config, date: str) -> tuple[datetime, datetime, float]:
    """[date 12:00, date+1 12:00) local as naive UTC, plus the offset used."""
    from photonscript.scheduler.log_files import night_window
    from photonscript.shared.localtime import utc_offset_hours
    w0, w1 = night_window(date)
    off = utc_offset_hours(config, w0 + timedelta(hours=7))
    shift = timedelta(hours=-off)
    return w0 + shift, w1 + shift, off


def armer_history(config) -> list[tuple[datetime, str]]:
    from photonscript.shared.safety_history import _history_events
    try:
        return _history_events(config)
    except Exception as e:  # noqa: BLE001
        logger.debug("safety history unreadable: %s", e)
        return []


def night_report(config, date: str, rig_logs: dict,
                 match_s: float = DEFAULT_MATCH_S,
                 flap_s: float = DEFAULT_FLAP_S) -> dict:
    """rig_logs = {rig: [Path, ...]} (oldest first). The whole analysis for
    one night; notes say which NINA had no log."""
    start, end, off = night_bounds_utc(config, date)
    raw, notes = {}, []
    for r in RIGS:
        paths = rig_logs.get(r) or []
        if not paths:
            notes.append(f"no NINA log for {RIG_LABEL[r]}")
            continue
        ev = []
        for p in paths:
            ev.extend(parse_file(p))
        raw[r] = ev
    rep = analyze(raw, start, end, off, armer=armer_history(config),
                  match_s=match_s, flap_s=flap_s,
                  confirm_s=int(getattr(config, "safety_confirm_seconds", 120)))
    rep["date"] = date
    rep["notes"] = notes
    rep["logs"] = {r: [Path(p).name for p in rig_logs.get(r) or []] for r in RIGS}
    return rep


def summary_row(rep: dict) -> dict:
    """One line per night for the multi-night view."""
    rigs = rep.get("rigs") or {}
    xc = rep.get("cross_check") or {}
    ref = rigs.get("rc16") or rigs.get("piggyback") or {}
    return {"date": rep.get("date"),
            "rigs": sorted(rigs),
            "to_unsafe": ref.get("to_unsafe", 0),
            "safe_min": (ref.get("minutes") or {}).get("safe", 0),
            "unsafe_min": (ref.get("minutes") or {}).get("unsafe", 0),
            "offline_min": (ref.get("minutes") or {}).get("offline", 0),
            "agreed": len(xc.get("agreed") or []),
            "suspects": len(xc.get("suspects") or []),
            "losses": len(xc.get("losses") or []),
            "losses_both": sum(1 for x in xc.get("losses") or [] if x["both"]),
            "flaps": len(ref.get("flaps") or []),
            "unsafe_onsets_local": rep.get("unsafe_onsets_local") or [],
            "verdict": rep.get("verdict")}
