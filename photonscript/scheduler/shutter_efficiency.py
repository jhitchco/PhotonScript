"""PS-176: predicted RC16 shutter-open share for tonight's plan.

2026-10-07: the shutter was open 5.4 h of 9.8 h dark (55%). The losses were
autofocus (28 AF runs, 118 min, one per filter block) and per-sub overhead
(225 of 281 subs were 30 s HDR shorts with about 11 s of download, settle
and dither between same-filter subs).

measured_overheads() reads the last nights' RC16 timelines (PS-67) for the
mean AF run, the gap between same-filter subs and the extra gap at a filter
change; estimate() runs tonight's targets through the shape the sequence
generator emits (a once-per-visit HDR short set, then the repeating imaging
loop of long blocks) under both AF policies (af_policy) and returns the
predicted shutter-open share of the dark time. A model, not a simulator:
the per-target windows are the night plan's.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

# Fallbacks when no recent night has a timeline (2026-10-07 values, rounded)
DEFAULT_AF_S = 250.0          # one RunAutofocus (median 3.4 min, mean 4.2)
DEFAULT_PER_SUB_S = 11.0      # same-filter gap: download, dither, settle
DEFAULT_FILTER_CHANGE_S = 5.0    # gap at a filter change, AF excluded
ACQUIRE_S = 180.0             # slew, center, start guiding per visit
PACE_S = 60.0                 # nina_sequence_json.TARGET_IMAGING_PACE_S
MAX_GAP_S = 300.0             # longer same-filter gaps are pauses, not overhead
MAX_CHANGE_S = 120.0          # filter-change gaps above this held an AF

_CACHE: dict[str, dict] = {}
_LOCK = threading.Lock()


def _p(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def night_overheads(tl: dict, rig: str = "rc16") -> dict | None:
    """One night's {af_s [..], same [..], change [..]} from a timeline()
    dict, or None when it has no subs for the rig."""
    subs = sorted((tl.get("subs") or {}).get(rig) or [],
                  key=lambda s: s.get("start") or "")
    if len(subs) < 2:
        return None
    row = next((r for r in tl.get("rows") or [] if r.get("id") == rig), {})
    afs = [(_p(s["start"]), _p(s["end"])) for s in row.get("segments") or []
           if s.get("state") == "autofocus"]
    af_s = [(b - a).total_seconds() for a, b in afs
            if 60 <= (b - a).total_seconds() <= 900]
    same, change = [], []
    for a, b in zip(subs, subs[1:]):
        try:
            gap = (_p(b["start"]) - _p(a["end"])).total_seconds()
        except (KeyError, ValueError):
            continue
        if gap < 0:
            continue
        has_af = any(s < _p(b["start"]) and e > _p(a["end"]) for s, e in afs)
        if a.get("filter") == b.get("filter") and not has_af:
            if gap <= MAX_GAP_S:
                same.append(gap)
        elif not has_af and gap <= MAX_CHANGE_S:
            # a filter change; longer gaps hide a triggered AF (NINA runs it
            # inside the Smart Exposure, so the timeline shows no AF there)
            change.append(gap)
    return {"af_s": af_s, "same": same, "change": change}


def _night_list(cfg, n: int, before: str | None) -> list[str]:
    from photonscript.scheduler.runs import runs_dir
    dates = sorted({p.name[:10] for p in runs_dir(cfg).glob("*_subs.jsonl")},
                   reverse=True)
    return [d for d in dates if before is None or d < before][:n]


def _night(cfg, date: str) -> dict | None:
    with _LOCK:
        if date in _CACHE:
            return _CACHE[date]
    try:
        from photonscript.scheduler.night_timeline import timeline
        out = night_overheads(timeline(cfg, date))
    except Exception as e:  # noqa: BLE001 - an estimate never breaks a page
        logger.warning("shutter_efficiency: %s timeline failed: %s", date, e)
        out = None
    with _LOCK:
        _CACHE[date] = out
    return out


def measured_overheads(cfg, nights: int = 5, before: str | None = None,
                       look_back: int = 10) -> dict:
    """{af_s, per_sub_s, filter_change_s, nights, n_af, n_gaps, source}
    from up to `nights` recent nights with RC16 subs (dates < before;
    timelines cached per date), else the 2026-10-07 defaults."""
    af, same, change, used = [], [], [], []
    try:
        dates = _night_list(cfg, look_back, before)
    except Exception as e:  # noqa: BLE001
        logger.warning("shutter_efficiency: no runs dir: %s", e)
        dates = []
    for d in dates:
        if len(used) >= nights:
            break
        o = _night(cfg, d)
        if not o:
            continue
        used.append(d)
        af += o["af_s"]
        same += o["same"]
        change += o["change"]

    def _mean(xs, default):
        return round(sum(xs) / len(xs), 1) if xs else default
    change.sort()
    fc = (round(change[len(change) // 2], 1) if change
          else DEFAULT_FILTER_CHANGE_S)    # median: a few long ones are pauses
    return {"af_s": _mean(af, DEFAULT_AF_S),
            "per_sub_s": _mean(same, DEFAULT_PER_SUB_S),
            "filter_change_s": fc,
            "nights": used, "n_af": len(af), "n_gaps": len(same),
            "source": "timelines" if used else "defaults (2026-10-07)"}


def _blocks(t, shorts_once: bool = True) -> tuple[list, list]:
    """(once-per-visit blocks, repeating-pass blocks), each block
    (filter, [(n, seconds), ...]) as owed. shorts_once False is the
    pre-PS-176 shape: the HDR shorts ride in their filter's block in every
    pass of the loop."""
    once, loop = [], []
    for e in t.exposures:
        f = e.filter_type.value
        short = ((e.short_remaining(), float(e.hdr_short_seconds))
                 if e.short_remaining() > 0 else None)
        long_ = ((e.count - e.acquired, float(e.exposure_seconds))
                 if e.count - e.acquired > 0 else None)
        if shorts_once:
            if short:
                once.append((f, [short]))
            if long_:
                loop.append((f, [long_]))
        else:
            sets = [x for x in (short, long_) if x]
            if sets:
                loop.append((f, sets))
    return once, loop


def target_estimate(t, window_s: float, policy: str, oh: dict,
                    smart: dict | None = None,
                    shorts_once: bool = True) -> dict:
    """Shutter seconds, AF count and overhead for one target window.

    The visit: acquire (ACQUIRE_S) + the start AF, the HDR short sets once
    (shorts_once; False replays the pre-PS-176 loop that re-shot them every
    pass), then passes of the long blocks until the window is used up.
    every_block: an AF at every block. smart: an AF only at blocks of
    3 nm filters whose offset is not measured (af_blocks), plus a timed AF every
    interval_min (temperature / HFR triggers are not predicted), and a
    PACE_S wait on a pass with no AF."""
    af_s, per_sub, fc = oh["af_s"], oh["per_sub_s"], oh["filter_change_s"]
    is_smart = policy == "smart" and smart is not None
    af_blocks = set((smart or {}).get("af_blocks") or ())
    interval = float((smart or {}).get("interval_min") or 0) * 60
    once, loop = _blocks(t, shorts_once)
    state = {"t": ACQUIRE_S + af_s, "shutter": 0.0, "af": 1}

    def _run(blocks) -> bool:
        """False once the window is used up."""
        for f, sets in blocks:
            if state["t"] >= window_s:
                return False
            if is_smart and f not in af_blocks:
                state["t"] += fc
            else:
                state["t"] += af_s + fc
                state["af"] += 1
            for n, sec in sets:
                for _ in range(n):
                    if state["t"] + sec > window_s:
                        return False
                    state["t"] += sec + per_sub
                    state["shutter"] += sec
        return True

    _run(once)
    last_af = state["t"]
    for _ in range(10000):
        if not loop or state["t"] >= window_s:
            break
        af_before, t_before = state["af"], state["t"]
        go = _run(loop)
        if state["af"] != af_before:
            last_af = state["t"]
        elif is_smart:
            state["t"] += PACE_S
        if is_smart and interval > 0:
            while state["t"] - last_af >= interval:
                state["t"] += af_s
                state["af"] += 1
                last_af += interval
        if not go or state["t"] <= t_before:
            break
    used = min(window_s, state["t"])
    n_af, shutter = state["af"], state["shutter"]
    return {"name": t.name, "window_h": round(window_s / 3600, 2),
            "shutter_h": round(shutter / 3600, 2),
            "pct": round(100 * shutter / window_s, 1) if window_s else 0.0,
            "af_count": n_af, "af_min": round(n_af * af_s / 60, 1),
            "overhead_min": round(max(0.0, used - shutter - n_af * af_s) / 60,
                                  1)}


def estimate(targets_windows: list, dark_s: float, oh: dict,
             smart: dict | None, active_policy: str) -> dict:
    """targets_windows: [(NinaSequenceTarget, window seconds)]. Both
    policies, so the night plan can show what the switch would buy."""
    out = {"active_policy": active_policy, "overheads": oh,
           "dark_h": round(dark_s / 3600, 2), "policies": {}}
    for pol in ("every_block", "smart"):
        rows = [target_estimate(t, w, pol, oh, smart) for t, w in
                targets_windows if w > 0]
        sh = sum(r["shutter_h"] for r in rows)
        out["policies"][pol] = {
            "shutter_h": round(sh, 2),
            "pct": round(100 * sh * 3600 / dark_s, 1) if dark_s else 0.0,
            "af_count": sum(r["af_count"] for r in rows),
            "af_min": round(sum(r["af_min"] for r in rows), 1),
            "targets": rows}
    if smart is None:
        out["note"] = ("no AF filter configured: the smart column equals "
                       "every_block")
    return out
