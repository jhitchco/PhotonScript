"""Target planning engine — selects optimal targets for a given night.

Considers: seasonal visibility, project completion, priority, and
produces a time-ordered imaging plan that maximizes telescope utilization.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional
from uuid import uuid4

from photonscript.shared.models import (
    CelestialTarget, ExposurePlan, FilterType, ImagingProject,
    NinaSequenceTarget, TargetTier,
)
from photonscript.shared.astronomy import (
    compute_visibility_window, get_seasonal_targets, rank_targets_for_night,
    get_twilight_times,
)
from photonscript.shared.config import PhotonScriptConfig

logger = logging.getLogger(__name__)


# Default narrowband exposure plan for emission nebulae
NARROWBAND_PLAN = [
    ExposurePlan(filter_type=FilterType.HA, exposure_seconds=300, count=40, gain=200, offset=50),
    ExposurePlan(filter_type=FilterType.OIII, exposure_seconds=300, count=30, gain=200, offset=50),
    ExposurePlan(filter_type=FilterType.SII, exposure_seconds=300, count=30, gain=200, offset=50),
]

# Default broadband exposure plan for galaxies / clusters
BROADBAND_PLAN = [
    ExposurePlan(filter_type=FilterType.LUMINANCE, exposure_seconds=180, count=60, gain=200, offset=50),
    ExposurePlan(filter_type=FilterType.RED, exposure_seconds=180, count=20, gain=200, offset=50),
    ExposurePlan(filter_type=FilterType.GREEN, exposure_seconds=180, count=20, gain=200, offset=50),
    ExposurePlan(filter_type=FilterType.BLUE, exposure_seconds=180, count=20, gain=200, offset=50),
]


def suggest_exposure_plan(target: CelestialTarget) -> list[ExposurePlan]:
    """Suggest a default exposure plan based on the target type."""
    obj_type = target.object_type.lower()
    if any(kw in obj_type for kw in ["nebula", "remnant", "emission", "planetary"]):
        return [p.model_copy() for p in NARROWBAND_PLAN]
    else:
        return [p.model_copy() for p in BROADBAND_PLAN]


def create_project_from_target(target: CelestialTarget) -> ImagingProject:
    """Create a new imaging project from a target with suggested exposures."""
    plans = suggest_exposure_plan(target)
    total_secs = sum(p.exposure_seconds * p.count for p in plans)
    return ImagingProject(
        id=str(uuid4()),
        target=target,
        exposure_plans=plans,
        priority=50,
        total_integration_hours=round(total_secs / 3600, 1),
    )


_NB_FILTERS = {"Ha", "OIII", "SII"}
# On a dark (moonless) night, cap broadband at this share of the night so it
# is protected from the uniform time-scaling that otherwise crushes a
# minority filter set to a single sub — but doesn't hog the whole night.
BB_SHARE_DARK = 0.5


def _scale_group(group, budget_s):
    """Scale a filter group's long counts down to fit budget_s. Returns
    seconds used. PS-112: a plan whose long set is done (count 0, only HDR
    shorts owed) stays at 0; the max(1, ...) floor is only for a set that
    still owes subs."""
    total = sum(e.exposure_seconds * e.count for e in group)
    if total <= budget_s or total == 0:
        return total
    scale = budget_s / total
    for e in group:
        if e.count > 0:
            e.count = max(1, int(e.count * scale))
    return sum(e.exposure_seconds * e.count for e in group)


def owed_seconds(e) -> float:
    """PS-112: seconds a per-night copy still owes, long set plus HDR shorts
    (the copy's acquired fields are 0, see remaining_copy)."""
    return (e.exposure_seconds * max(0, e.count - e.acquired)
            + (e.hdr_short_seconds or 0) * e.short_remaining())


def _fit_by_moon(exposures, available_seconds, moon_tag):
    """Fit a target's remaining exposures into tonight's time, weighting
    broadband vs narrowband by the night's moon tag (from scheduler/moon.py):

      "BB"       dark / moonless -> protect a broadband set (BB_SHARE_DARK of
                 the night), narrowband fills the rest
      "NB"       bright moon     -> narrowband only; broadband deferred to a
                 darker night (returns [] for a broadband-only target)
      "NB+OIII"  intermediate    -> narrowband priority, broadband fills leftover
      None       moon-aware off  -> legacy uniform scale-down

    Returns the exposures to shoot tonight (order is cosmetic; the sequence
    generator re-sorts broadband-first by the live moon window).
    """
    bb = [e for e in exposures if e.filter_type.value not in _NB_FILTERS]
    nb = [e for e in exposures if e.filter_type.value in _NB_FILTERS]

    if moon_tag == "NB":
        _scale_group(nb, available_seconds)
        return nb
    if moon_tag == "BB":
        bb_used = _scale_group(bb, available_seconds * BB_SHARE_DARK)
        _scale_group(nb, max(0.0, available_seconds - bb_used))
        return bb + nb
    if moon_tag == "NB+OIII":
        nb_used = _scale_group(nb, available_seconds)
        _scale_group(bb, max(0.0, available_seconds - nb_used))
        return nb + bb
    # moon-aware disabled / unknown tag: preserve the legacy uniform behavior
    _scale_group(exposures, available_seconds)
    return exposures


def _rc16_plan(proj, filt: str):
    return next((e for e in getattr(proj, "exposure_plans", None) or []
                 if (getattr(e, "rig", "rc16") or "rc16") == "rc16"
                 and e.filter_type.value == filt), None)


def feeds_nb_blend(proj) -> bool:
    """A two-rig narrowband goal: an RC16 Ha plan (the NB blend's main input,
    integration/blend.py --mode nb) and a Piggy-600 plan (the frame)."""
    return (_rc16_plan(proj, "Ha") is not None
            and any((getattr(e, "rig", "rc16") or "rc16") != "rc16"
                    for e in proj.exposure_plans))


def ha_first_seconds(proj, config) -> float:
    """RC16 Ha seconds this goal should hold before OIII / SII share a night
    by the mix; 0 = off (the default). The goal's ha_first_h wins (0 = off,
    > 0 = on at that many hours, any goal with an RC16 Ha plan); unset, a
    two-rig narrowband goal follows plan_nb_blend_ha_first at
    blend_nb_min_ha_h."""
    if _rc16_plan(proj, "Ha") is None:
        return 0.0
    v = getattr(proj, "ha_first_h", None)
    if v is None:
        if not bool(getattr(config, "plan_nb_blend_ha_first", False)):
            return 0.0
        if not feeds_nb_blend(proj):
            return 0.0
        v = getattr(config, "blend_nb_min_ha_h", 2.0)
    try:
        return max(0.0, float(v)) * 3600.0
    except (TypeError, ValueError):
        return 0.0


def _fit_ha_first(exposures, available_seconds, moon_tag, ha_done_s: float,
                  min_s: float):
    """Ha first: tonight's time goes to RC16 Ha until the goal would hold
    min_s accepted Ha seconds, then what is left is shared by the usual moon
    rule (_fit_by_moon: the remaining counts, i.e. the mix). Ha is listed
    first, so the sequence shoots it first. Without an Ha deficit this is
    _fit_by_moon unchanged."""
    import math
    ha = next((e for e in exposures if e.filter_type.value == "Ha"
               and e.count > 0), None)
    deficit = min_s - ha_done_s
    if ha is None or deficit <= 0 or not ha.exposure_seconds:
        return _fit_by_moon(exposures, available_seconds, moon_tag)
    exp = float(ha.exposure_seconds)
    reserve = min(ha.count, math.ceil(deficit / exp - 1e-9),
                  int(available_seconds // exp))
    if reserve <= 0:
        return _fit_by_moon(exposures, available_seconds, moon_tag)
    left = available_seconds - reserve * exp
    rest = []
    for e in exposures:
        if e is ha:
            if e.count - reserve > 0:
                rest.append(e.model_copy(update={"count": e.count - reserve}))
        else:
            rest.append(e)
    shortest = min((float(e.exposure_seconds) for e in rest
                    if e.exposure_seconds), default=0.0)
    # the reserve can take the whole night: then nothing else is planned
    fitted = (_fit_by_moon(rest, left, moon_tag)
              if rest and shortest and left >= shortest else [])
    extra = next((e for e in fitted if e.filter_type.value == "Ha"), None)
    out_ha = ha.model_copy(update={
        "count": reserve + (extra.count if extra is not None else 0)})
    return [out_ha] + [e for e in fitted if e.filter_type.value != "Ha"]


def remaining_copy(plan: ExposurePlan) -> ExposurePlan:
    """PS-105: tonight's copy of a plan carries only what is still owed.

    count = long subs still owed, hdr_short_count = short subs still owed,
    and every acquired field is reset to 0, so the planner is the ONE owner
    of "remaining": the sequence generator's `count - acquired`, the plan
    snapshot, the night plan text and cap_unguided all read the copy as is.
    The old copy kept `acquired`, so the generator subtracted it a second
    time: 30 planned with 10 accepted shot 10, and a project at least half
    done dropped out of the night."""
    return plan.model_copy(update={
        "count": max(0, plan.count - plan.acquired),
        "acquired": 0,
        "acquired_s": 0.0,
        "hdr_short_count": plan.short_remaining(),
        "hdr_short_acquired": 0,
    })


NB_UNGUIDED_REASON = ("RC16 narrowband needs guiding: unguided night "
                      "(rc16_narrowband_requires_guided)")


def nb_needs_guiding(config, guided: Optional[bool]) -> bool:
    """2026-10-25: True when tonight is planned unguided and the RC16
    narrowband must not be scheduled (config rc16_narrowband_requires_guided,
    default on). 2026-10-24 the RC16 shot 3 nm Ha at 120 s unguided all
    night: 126 rejects in a row, too few stars for AF. guided None (a
    caller that does not say) never drops anything."""
    return guided is False and bool(getattr(config, "rc16_narrowband_requires_guided", True))


def broadband_hold(plan: ExposurePlan) -> ExposurePlan:
    """A narrowband hold set as L (same length, no HDR shorts): the RC16
    keeps a pointing for the Piggy-600 on an unguided night without shooting
    narrowband. A broadband plan is returned as is."""
    if plan.filter_type.value not in _NB_FILTERS:
        return plan
    return plan.model_copy(update={"filter_type": FilterType.LUMINANCE,
                                   "hdr_short_seconds": None, "hdr_short_count": 0,
                                   "hdr_short_acquired": 0})


def piggy_hold_exposure(proj: ImagingProject, nb_ok: bool = True) -> Optional[ExposurePlan]:
    """PS-134: a goal the Piggy-600 drives is planned while EITHER rig owes
    time. The Piggy companion shoots OSC on whatever the mount points at, so
    once the RC16 plan is done the RC16 keeps the pointing with extra subs of
    its largest plan (bonus core data), sized to the OSC seconds still owed.
    None when the RC16 drives, the OSC owes nothing, or the goal has no RC16
    plan (a Piggy-only goal stays off the RC16 sequence, as before).
    nb_ok False (unguided night, nb_needs_guiding): the largest broadband
    RC16 plan, else the largest plan as an L hold (broadband_hold)."""
    import math
    if (getattr(proj, "driving_rig", "rc16") or "rc16") == "rc16":
        return None
    rc16 = [e for e in proj.exposure_plans
            if (getattr(e, "rig", "rc16") or "rc16") == "rc16"]
    owed_s = sum(max(0.0, e.count * e.exposure_seconds - e.long_seconds_done())
                 for e in proj.exposure_plans
                 if (getattr(e, "rig", "rc16") or "rc16") != "rc16")
    if not rc16 or owed_s <= 0:
        return None
    pool = rc16
    if not nb_ok:
        pool = [e for e in rc16 if e.filter_type.value not in _NB_FILTERS] or rc16
    base = max(pool, key=lambda e: e.count * e.exposure_seconds)
    if not nb_ok:
        base = broadband_hold(base)
    if not base.exposure_seconds or base.exposure_seconds <= 0:
        return None
    return base.model_copy(update={
        "count": max(1, math.ceil(owed_s / base.exposure_seconds - 1e-9)),
        "acquired": 0, "acquired_s": 0.0, "hdr_short_seconds": None,
        "hdr_short_count": 0, "hdr_short_acquired": 0})


def cap_unguided(targets, cap_s) -> list[str]:
    """PS-66: cap sub length on every UNGUIDED target (start_guiding False) at
    cap_s seconds (config unguided_max_exposure_s; 0 or None = off), keeping
    the same integration: a long set over the cap becomes cap_s x
    ceil(owed * old / cap_s). HDR short sets under the cap are untouched.
    Guided targets, focus-calibration and tracking-test targets are left
    alone. Edits the targets' exposure lists in place (they are per-night
    copies from plan_night_sequence) and returns one note per capped set."""
    import math
    try:
        cap = float(cap_s or 0)
    except (TypeError, ValueError):
        cap = 0.0
    notes: list[str] = []
    if cap <= 0:
        return notes
    for t in targets or []:
        if (getattr(t, "start_guiding", False) or getattr(t, "focus_calibration", False)
                or getattr(t, "tracking_test", False)):
            continue
        capped = []
        for e in t.exposures:
            upd = {}
            owed = e.count - e.acquired
            if e.exposure_seconds > cap and owed > 0:
                upd.update(exposure_seconds=cap, acquired=0, acquired_s=0.0,
                           count=math.ceil(owed * e.exposure_seconds / cap - 1e-9))
                notes.append(f"{t.name} {e.filter_type.value}: {owed}x"
                             f"{e.exposure_seconds:.0f}s -> {upd['count']}x{cap:.0f}s")
            short_owed = e.short_remaining()
            if e.hdr_short_seconds and e.hdr_short_seconds > cap and short_owed > 0:
                upd.update(hdr_short_seconds=cap, hdr_short_acquired=0,
                           hdr_short_count=math.ceil(
                               short_owed * e.hdr_short_seconds / cap - 1e-9))
            capped.append(e.model_copy(update=upd) if upd else e)
        t.exposures = capped
    if notes:
        logger.info("Unguided cap %.0fs: %s", cap, "; ".join(notes))
    return notes


def meridian_safe_order(pairs, dark_start, guard_min: int = 20):
    """Transit-order targets west->east, but push any target crossing the
    meridian within `guard_min` minutes of dark-start PAST the meridian so the
    run doesn't open on an immediate flip + recenter failure (the 2026-07-07
    dead-night). `pairs` = [(transit_time | datetime.max, target), ...].
    Returns (ordered_targets, deferred_names)."""
    from datetime import timedelta
    guard = timedelta(minutes=int(guard_min))
    deferred: list[str] = []

    def _key(item):
        transit, tgt = item
        if (isinstance(transit, datetime) and dark_start is not None
                and dark_start - guard <= transit <= dark_start + guard):
            deferred.append(getattr(tgt, "name", None))
            return transit + guard * 2
        return transit

    ordered = [t for _, t in sorted(pairs, key=_key)]
    return ordered, deferred


def allocate_windows(entries: list[dict], start: datetime, end: datetime,
                     min_window_s: float = 1800.0) -> dict:
    """PS-180: split tonight's dark time into one window per target.

    entries, in run order (transit order), each {"name", "up_from",
    "up_until" (datetimes: when the target is usable, above the altitude
    floor and, for a broadband-only target, before moonrise), "priority",
    "need_s" (goal seconds still owed, overhead included)}.

    Rule (Needs decision): walk the targets in run order from `start`. Each
    gets a priority-weighted share of the time left, among itself and the
    later targets that can still use some of it, capped by what its goal
    still needs and by its own usable time, never shorter than min_window_s.
    A window is stretched to the next target's rise so the mount never sits
    idle between two targets. A target that cannot get min_window_s gets no
    window. Leftover time at the end of the night goes to the
    highest-priority target still up: the last target's window just grows,
    any other target gets a final fill window.

    Returns {"windows": {name: (start, end)}, "handoff": {name: datetime}
    (the window end of every windowed target except the one that keeps the
    mount to the end of the night), "fill": None or (name, start, end),
    "dropped": [names]}. Windows never overlap and lie inside [start, end]."""
    min_win = timedelta(seconds=max(60.0, float(min_window_s or 0)))
    rows = []
    for e in entries:
        lo = max(start, e.get("up_from") or start)
        hi = min(end, e.get("up_until") or end)
        rows.append(dict(e, lo=lo, hi=hi,
                         w=max(1.0, float(e.get("priority") or 0)),
                         need=timedelta(seconds=max(0.0, float(
                             e.get("need_s") or 0)))))

    def usable_from(r, t):
        return r["hi"] - max(t, r["lo"]) >= min_win

    windows: dict = {}
    order: list[str] = []
    dropped: list[str] = []
    cursor = start
    for i, r in enumerate(rows):
        s = max(cursor, r["lo"])
        if r["hi"] - s < min_win:
            dropped.append(r["name"])
            continue
        rest = [x for x in rows[i + 1:] if usable_from(x, s)]
        share = (end - s) * (r["w"] / (r["w"] + sum(x["w"] for x in rest)))
        e_end = s + max(min_win, min(share, r["need"]))
        if rest and rest[0]["lo"] > e_end:
            e_end = rest[0]["lo"]   # no idle gap before the next one rises
        e_end = min(e_end, r["hi"])
        windows[r["name"]] = (s, e_end)
        order.append(r["name"])
        cursor = e_end

    fill = None
    if order and end - cursor >= min_win:
        rank = {r["name"]: (-r["w"], k) for k, r in enumerate(rows)}
        cands = sorted((r for r in rows if r["name"] in windows
                        and usable_from(r, cursor)),
                       key=lambda r: rank[r["name"]])
        if cands:
            c = cands[0]
            if c["name"] == order[-1]:
                windows[c["name"]] = (windows[c["name"]][0], c["hi"])
            else:
                fill = (c["name"], max(cursor, c["lo"]), c["hi"])
    handoff = {n: windows[n][1] for n in order
               if fill is not None or n != order[-1]}
    return {"windows": windows, "handoff": handoff, "fill": fill,
            "dropped": dropped}


def _apply_windows(sequence_targets, vis_by_name: dict, need_by_name: dict,
                   prio_by_name: dict, config, dark_start: datetime,
                   dark_end: datetime, now: datetime, moon: dict | None,
                   ha_first_by_name: dict | None = None):
    """PS-180: allocate_windows over tonight's ordered targets, put the
    windows on them, drop the targets that get none, and fit each target's
    per-pass counts into its window (filter balance: every filter of a pass
    is shot inside the window; the loop repeats the pass until the
    handoff). Returns the targets that keep a window."""
    start = max(dark_start, now)
    from photonscript.scheduler.moon import broadband_from
    if broadband_from(moon or {}) is not None:
        # 2026-10-16: broadband-only targets open at moonset, so the
        # targets with narrowband go first and use the moonlit start
        sequence_targets = sorted(sequence_targets, key=lambda t: all(
            e.filter_type.value not in _NB_FILTERS for e in t.exposures))
    entries = _window_entries(sequence_targets, vis_by_name, need_by_name,
                              prio_by_name, dark_end, moon)
    alloc = allocate_windows(entries, start, dark_end, _min_window_s(config))
    return _put_windows(sequence_targets, alloc, ha_first_by_name)


def _min_window_s(config) -> float:
    return 60.0 * float(getattr(config, "plan_window_min_minutes", 30) or 30)


def _window_entries(sequence_targets, vis_by_name: dict, need_by_name: dict,
                    prio_by_name: dict, dark_end: datetime,
                    moon: dict | None) -> list[dict]:
    """allocate_windows entries for tonight's targets (usable span, the
    moonrise cap for a broadband-only target, priority, need)."""
    moon = moon or {}
    rise = moon.get("rise_utc") if moon.get("down_at_dusk") else None
    # 2026-10-16: a bright moon up at dusk that sets before dawn opens
    # broadband at moonset (the generator waits for it too)
    from photonscript.scheduler.moon import broadband_from
    bb_from = broadband_from(moon)
    entries = []
    for t in sequence_targets:
        vis = vis_by_name.get(t.name) or {}
        up_until = vis.get("set_time")
        if up_until is not None:
            up_until = up_until + timedelta(minutes=10)   # 10-min samples
        bb_only = all(e.filter_type.value not in _NB_FILTERS
                      for e in t.exposures)
        if bb_only and rise is not None:
            # broadband before moonrise (the generator ends it there too)
            up_until = min(up_until or dark_end, rise)
        up_from = vis.get("rise_time")
        if bb_only and bb_from is not None:
            # broadband after moonset (the generator waits for it)
            up_from = max(up_from or bb_from, bb_from)
        entries.append({"name": t.name, "up_from": up_from,
                        "up_until": up_until,
                        "priority": prio_by_name.get(t.name, 50),
                        "need_s": need_by_name.get(t.name, 0.0)})
    return entries


def _put_windows(sequence_targets, alloc: dict,
                 ha_first_by_name: dict | None = None):
    """Put allocate_windows' windows on the targets (in list order), drop
    the ones without a window, fit each target's per-pass counts into its
    window. Returns the kept targets."""
    kept = []
    for t in sequence_targets:
        win = alloc["windows"].get(t.name)
        if win is None:
            logger.info("Target windows: %s gets no window tonight (less "
                        "than the minimum usable time left)", t.name)
            continue
        t.window_start_utc, t.window_end_utc = win
        t.handoff_utc = alloc["handoff"].get(t.name)
        span_s = (win[1] - win[0]).total_seconds()
        if alloc["fill"] and alloc["fill"][0] == t.name:
            t.fill_from_utc, t.fill_end_utc = alloc["fill"][1:]
        if getattr(t, "repeat_while_up", True):
            # a run-once mosaic panel (PS-111) keeps its owed subs: the
            # handoff still bounds it
            longs = [e for e in t.exposures if e.count - e.acquired > 0]
            hf = (ha_first_by_name or {}).get(t.name)
            if hf:
                # Ha first holds inside the window too: the Ha reserve
                # first, the rest of the pass scaled into what is left
                done_s, min_s = hf
                t.exposures = (_fit_ha_first(longs, span_s * 0.85, None,
                                             done_s, min_s)
                               + [e for e in t.exposures if e not in longs])
            else:
                _scale_group(longs, span_s * 0.85)
        kept.append(t)
    logger.info("Target windows: %s%s", "; ".join(
        f"{t.name} {t.window_start_utc:%H:%M}-{t.window_end_utc:%H:%M}Z"
        + (" then hand off" if t.handoff_utc else "") for t in kept),
        (f"; fill {alloc['fill'][0]} from {alloc['fill'][1]:%H:%M}Z"
         if alloc["fill"] else ""))
    return kept


def allocate_deep(entries: list[dict], pick: str, start: datetime,
                  end: datetime, min_window_s: float = 1800.0) -> dict | None:
    """Deep night: the pick gets the dark window while it is up and owes
    time; split (allocate_windows) fills the time before it rises and after
    it sets or is done. Same entries and result shape as allocate_windows,
    plus "pick_window" and "fallback" (None, or "sets HH:MMZ" / "done
    HH:MMZ" when the rest of the night is split). None when the pick cannot
    get min_window_s tonight (the caller plans split)."""
    min_win = timedelta(seconds=max(60.0, float(min_window_s or 0)))
    pe = next((e for e in entries if e["name"] == pick), None)
    if pe is None:
        return None
    lo = max(start, pe.get("up_from") or start)
    hi = min(end, pe.get("up_until") or end)
    if hi - lo < min_win:
        return None
    need = timedelta(seconds=max(0.0, float(pe.get("need_s") or 0)))
    p_end = min(hi, lo + max(min_win, need))
    others = [e for e in entries if e["name"] != pick]
    windows: dict = {}
    handoff: dict = {}
    fill = None
    if others and lo - start >= min_win:
        b = allocate_windows(others, start, lo, min_window_s)
        windows.update(b["windows"])
        if windows:
            # the time before the pick rises: every window hands off, the
            # last one stretched to the pick's rise when still up
            last = max(windows, key=lambda n: windows[n][1])
            le = next(e for e in others if e["name"] == last)
            if windows[last][1] < lo and (le.get("up_until") or end) >= lo:
                windows[last] = (windows[last][0], lo)
            handoff.update({n: w[1] for n, w in windows.items()})
    rest = [e for e in others if e["name"] not in windows]
    after = None
    if rest and end - p_end >= min_win:
        after = allocate_windows(rest, p_end, end, min_window_s)
        if not after["windows"]:
            after = None
    fallback = None
    if after is None:
        # nothing else can use the rest: the pick keeps the mount while up
        p_end = hi
    else:
        windows.update(after["windows"])
        handoff.update(after["handoff"])
        fill = after["fill"]
        fallback = ("done" if p_end < hi else "sets") + f" {p_end:%H:%M}Z"
    windows[pick] = (lo, p_end)
    if after is not None:
        handoff[pick] = p_end
    dropped = [e["name"] for e in others if e["name"] not in windows]
    return {"windows": windows, "handoff": handoff, "fill": fill,
            "dropped": dropped, "pick_window": (lo, p_end),
            "fallback": fallback}


def _apply_deep_windows(sequence_targets, pick: str, vis_by_name: dict,
                        need_by_name: dict, prio_by_name: dict, config,
                        dark_start: datetime, dark_end: datetime,
                        now: datetime, moon: dict | None,
                        ha_first_by_name: dict | None = None):
    """allocate_deep over tonight's targets: (kept targets in window order,
    the allocation), or (None, None) when the pick gets no window (the
    caller plans split)."""
    start = max(dark_start, now)
    entries = _window_entries(sequence_targets, vis_by_name, need_by_name,
                              prio_by_name, dark_end, moon)
    alloc = allocate_deep(entries, pick, start, dark_end, _min_window_s(config))
    if alloc is None:
        return None, None
    ordered = sorted((t for t in sequence_targets if t.name in alloc["windows"]),
                     key=lambda t: alloc["windows"][t.name][0])
    ordered += [t for t in sequence_targets if t.name not in alloc["windows"]]
    return _put_windows(ordered, alloc, ha_first_by_name), alloc


def _deep_moon(config, date_utc: datetime) -> dict:
    """moon.moon_window_tonight for the deep-night score ({} when
    moon-aware planning is off or the moon is unavailable)."""
    if not getattr(config, "moon_aware_planning", True):
        return {}
    try:
        from photonscript.scheduler.moon import moon_window_tonight
        return moon_window_tonight(config, now=date_utc) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("moon window unavailable: %s", e)
        return {}


def _panel_note(proj, held: list) -> str:
    m = proj.mosaic
    note = (f"Mosaic {m.get('name')}: panel {m.get('panel')} of {m.get('of')} "
            f"(row {m.get('row')}, col {m.get('col')}), finished in order. "
            "The RC16 centers on this panel; the Piggy-600 rides along, its "
            "frame shifted by the panel offset from the mosaic center.")
    if held:
        note += " Held for a later night: " + ", ".join(
            p.target.name for p in held) + "."
    return note


def _mosaic_gate(visible_projects: list) -> list:
    """PS-111: per mosaic, keep the first unfinished panels (capture order)
    whose owed RC16 hours fill what the night gives the mosaic (its first
    panel's visible hours x 0.85, the planner's overhead factor), hold the
    rest. The admitted panels are kept together at the position of the
    first one; each carries the held names and the shared transit time."""
    from photonscript.scheduler.mosaic import mosaic_of, tonight_panels
    groups: dict[str, list] = {}
    for vp in visible_projects:
        m = mosaic_of(vp["project"])
        if m:
            groups.setdefault(m["id"], []).append(vp)
    if not groups:
        return visible_projects
    keep: dict[str, list] = {}
    for mid, vps in groups.items():
        vps.sort(key=lambda vp: int(mosaic_of(vp["project"]).get("panel") or 0))
        usable = vps[0]["visibility"]["hours"] * 0.85
        admitted, held = tonight_panels([vp["project"] for vp in vps], usable)
        ids = {id(p) for p in admitted}
        kept = [vp for vp in vps if id(vp["project"]) in ids]
        transit = kept[0]["visibility"].get("transit_time") if kept else None
        for vp in kept:
            vp["held"] = held
            vp["mosaic_transit"] = transit
        keep[mid] = kept
        if held:
            logger.info("Mosaic %s: tonight %s; held for later nights (in "
                        "order): %s", mosaic_of(vps[0]["project"]).get("name"),
                        ", ".join(p.target.name for p in admitted),
                        ", ".join(p.target.name for p in held))
    out, placed = [], set()
    for vp in visible_projects:
        m = mosaic_of(vp["project"])
        if not m:
            out.append(vp)
        elif m["id"] not in placed:
            placed.add(m["id"])
            out.extend(keep[m["id"]])
    return out


def plan_night_sequence(
    projects: list[ImagingProject],
    config: PhotonScriptConfig,
    date_utc: Optional[datetime] = None,
    *,
    night_mode: Optional[str] = None,
    deep_target: Optional[str] = None,
    info: Optional[dict] = None,
    guided: Optional[bool] = None,
) -> list[NinaSequenceTarget]:
    """Build an ordered list of NINA sequence targets for tonight.

    Strategy:
    1. Compute visibility window for each active project's target
    2. Filter to targets visible tonight (> 30° altitude)
    3. Order by transit time so the scope moves west-to-east through the night
    4. Allocate exposures proportionally to remaining needs

    Deep night (scheduler/deep_night.py): night_mode "split" | "deep"
    (None = tonight's override, else config plan_night_mode); deep_target
    a project id that wins the deep pick. info (a dict, filled in place):
    mode, mode_source, night, deep_pick (score breakdown, window,
    fallback), deep_scores, deep_note.

    guided (PS-197): the arm's guiding mode. When given, every target this
    returns carries it (apply_guiding), whatever the mode (split, deep, the
    split before / after a deep pick, a Piggy-driven hold, the fill), so
    no planner mode can hand the generator a guiding flag the arm did not
    choose. None leaves start_guiding at the model default (the caller
    sets it).

    guided False with rc16_narrowband_requires_guided (2026-10-25,
    nb_needs_guiding): no RC16 Ha / OIII / SII set is scheduled, in split
    and in the deep pick alike; info["rc16_nb_unguided"] lists each goal's
    dropped filters with the reason. What is left (broadband) follows the
    moon rule as usual, so on a moon-up night the RC16 idles (or shoots
    owed calibration) when the moon defers broadband. Piggy-600 plans are
    not touched; a Piggy-driven goal keeps its pointing with a broadband /
    L hold.
    """
    from photonscript.scheduler import deep_night
    if date_utc is None:
        date_utc = datetime.utcnow()

    obs = config.get_observatory()
    # Anchor to the LOCAL evening date — after ~6 PM local the UTC date has
    # already rolled over and a UTC anchor plans TOMORROW's night (the same
    # bug fixed in night_plan; this was the last copy).
    # 2026-10-13: and noon to noon, so after local midnight it is still the
    # night that began yesterday evening (00:04 local planned the next one).
    from photonscript.shared.localtime import night_base_date
    _base = night_base_date(config, date_utc)
    twilight = get_twilight_times(obs, _base)
    if twilight.get("astro_dark_end") and twilight["astro_dark_end"] < date_utc:
        twilight = get_twilight_times(obs, _base + timedelta(days=1))
    dark_start = twilight.get("astro_dark_start")
    dark_end = twilight.get("astro_dark_end")

    if not dark_start or not dark_end:
        logger.warning("Could not compute darkness window for %s", date_utc.date())
        return []

    dark_hours = (dark_end - dark_start).total_seconds() / 3600
    logger.info("Dark window: %s to %s (%.1f hours)", dark_start, dark_end, dark_hours)

    # Moon tag for the whole night (BB=dark, NB=bright, NB+OIII=intermediate).
    moon_tag = None
    if getattr(config, "moon_aware_planning", True):
        try:
            from photonscript.scheduler.moon import night_moon
            moon_tag = night_moon(config, dark_start.strftime("%Y-%m-%d"),
                                  dark_start, dark_end).get("tag")
            logger.info("Moon-aware planning: night tag = %s", moon_tag)
        except Exception as e:  # noqa: BLE001
            logger.warning("moon-aware planning unavailable, using uniform "
                           "scale: %s", e)
            moon_tag = None

    # Compute visibility for each project
    visible_projects = []
    for project in projects:
        if not project.active:
            continue
        project.compute_completion()
        if project.completion_pct >= 100:
            continue

        # PS-180: tonight's twilight, so rise/set (the target windows) are
        # tonight's even after the UTC date rolled over in the evening
        vis = compute_visibility_window(project.target, obs, date_utc,
                                        twilight=twilight)
        if not vis["visible"] or vis["hours"] < 0.5:
            continue

        visible_projects.append({
            "project": project,
            "visibility": vis,
        })

    if not visible_projects:
        logger.info("No targets visible tonight, checking seasonal catalog")
        # Fall back to seasonal suggestions
        month = date_utc.month
        seasonal = get_seasonal_targets(month)
        ranked = rank_targets_for_night(seasonal, obs, date_utc)
        for r in ranked[:5]:
            proj = create_project_from_target(r["target"])
            vis = compute_visibility_window(proj.target, obs, date_utc)
            if vis["visible"] and vis["hours"] >= 0.5:
                visible_projects.append({"project": proj, "visibility": vis})

    # PS-111: mosaic panels are finished in order; only the first unfinished
    # panels that fill the mosaic's visible time come along tonight.
    visible_projects = _mosaic_gate(visible_projects)

    # Select by PRIORITY (highest wins scarce dark time); the chosen set is
    # transit-ordered at the end to minimize slewing.
    visible_projects.sort(key=lambda vp: -vp["project"].priority)

    # Deep night: one goal for the whole dark window (split before it rises
    # and after it sets or is done)
    info = info if info is not None else {}
    night = deep_night.night_label(config, dark_start)
    mode, deep_target, mode_src = deep_night.resolve(config, night, night_mode,
                                                     deep_target)
    info.update(mode=mode, mode_source=mode_src, night=night, deep_pick=None,
                deep_scores=[], deep_note=None)
    nb_off = nb_needs_guiding(config, guided)
    info["rc16_nb_unguided"] = []
    pick = pick_proj = None
    _gen_moon = None  # generator's moon window, fetched only if needed
    if mode == "deep":
        _gen_moon = _deep_moon(config, date_utc)
        bb_ok = True
        if getattr(config, "moon_aware_planning", True):
            from photonscript.scheduler.moon import broadband_deferred
            bb_ok = not broadband_deferred(_gen_moon)
        rise = (_gen_moon.get("rise_utc") if _gen_moon.get("down_at_dusk")
                else None)
        from photonscript.scheduler.moon import broadband_from
        pick, scores = deep_night.choose(
            visible_projects, max(dark_start, date_utc), dark_end, dark_start,
            dark_end, target=deep_target, bb_ok=bb_ok, moonrise=rise,
            moonset=broadband_from(_gen_moon), nb_ok=not nb_off)
        info["deep_scores"] = [deep_night.json_pick(s) for s in scores]
        if pick is None:
            info["deep_note"] = (
                f"deep target {deep_target} is not up or not active tonight: split"
                if deep_target else "no goal can use tonight: split")
        else:
            pick_proj = next(vp["project"] for vp in visible_projects
                             if vp["project"].id == pick["project_id"]
                             and vp["project"].target.name == pick["target"])
            visible_projects.sort(key=lambda vp: vp["project"] is not pick_proj)
            logger.info("Deep night pick: %s (score %.1f%s)", pick["target"],
                        pick["score"], ", explicit" if pick.get("explicit") else "")

    # Build sequence targets
    sequence_targets = []
    remaining_hours = dark_hours
    # PS-180: per target, for the window allocation
    vis_by_name, need_by_name, prio_by_name = {}, {}, {}
    ha_first_by_name = {}   # name -> (Ha seconds done, Ha first seconds)

    for vp in visible_projects:
        if remaining_hours <= 0.3:
            break

        proj: ImagingProject = vp["project"]
        vis_hours = min(vp["visibility"]["hours"], remaining_hours)

        # Figure out how many exposures we can fit
        remaining_exposures = []
        for plan in proj.exposure_plans:
            if getattr(plan, "rig", "rc16") != "rc16":
                continue  # PS-30: a Piggy-600 OSC plan is never an RC16 filter
            # PS-112: keep a plan while EITHER set is owed; an HDR plan whose
            # long set is done still has its short subs to shoot
            if plan.count - plan.acquired > 0 or plan.short_remaining() > 0:
                remaining_exposures.append(remaining_copy(plan))
        if nb_off:
            # 2026-10-25: an unguided night shoots no RC16 narrowband
            nb = [e for e in remaining_exposures if e.filter_type.value in _NB_FILTERS]
            if nb:
                remaining_exposures = [e for e in remaining_exposures
                                       if e.filter_type.value not in _NB_FILTERS]
                info["rc16_nb_unguided"].append({
                    "target": proj.target.name,
                    "filters": [e.filter_type.value for e in nb],
                    "owed_h": round(sum(owed_seconds(e) for e in nb) / 3600, 2),
                    "reason": NB_UNGUIDED_REASON})
                logger.info("Not scheduling %s RC16 %s tonight: %s", proj.target.name,
                            "/".join(e.filter_type.value for e in nb), NB_UNGUIDED_REASON)
        if not remaining_exposures:
            # PS-134: the Piggy-600 drives and still owes time
            hold = piggy_hold_exposure(proj, nb_ok=not nb_off)
            if hold is None and proj is pick_proj:
                # deep night on a goal without an RC16 plan (Piggy-driven):
                # the mount points at it all night, the RC16 shoots bonus subs
                hold = deep_night.rc16_hold(proj, vis_hours * 3600 * 0.85,
                                            nb_ok=not nb_off)
            if hold is not None:
                remaining_exposures.append(hold)

        if not remaining_exposures:
            continue
        # PS-180: what the goal still needs (before tonight's scale-down)
        need_s = sum(owed_seconds(e) for e in remaining_exposures) * 1.15
        if proj is pick_proj:
            # deep: the goal keeps the mount while EITHER rig owes time
            owed = deep_night.owed_by_kind(proj, nb_ok=not nb_off)
            need_s = max(owed["NB"] + owed["BB"], owed["OSC"]) * 1.15

        # Fit into tonight's time, weighting broadband/narrowband by the moon.
        available_seconds = vis_hours * 3600 * 0.85  # 15% overhead for slewing/dithering
        ha_min_s = 0.0 if nb_off else ha_first_seconds(proj, config)
        if ha_min_s > 0:
            # Ha first (two-rig NB blend): RC16 Ha until the goal holds
            # ha_min_s accepted, then OIII / SII by the mix
            ha_plan = _rc16_plan(proj, "Ha")
            ha_done_s = (ha_plan.long_seconds_done() if ha_plan is not None
                         else 0.0)
            remaining_exposures = _fit_ha_first(
                remaining_exposures, available_seconds, moon_tag,
                ha_done_s, ha_min_s)
            if ha_done_s < ha_min_s:
                ha_first_by_name[proj.target.name] = (ha_done_s, ha_min_s)
        else:
            remaining_exposures = _fit_by_moon(
                remaining_exposures, available_seconds, moon_tag)
        if not remaining_exposures:
            # e.g. a broadband-only target on a bright-moon night -> skip tonight
            continue
        # Same moon rule as the sequence generator (moon.broadband_deferred):
        # a broadband-only target the generator would empty is dropped here, so
        # the plan and the sequence agree (PS-27).
        if (getattr(config, "moon_aware_planning", True)
                and all(e.filter_type.value not in _NB_FILTERS
                        for e in remaining_exposures)):
            if _gen_moon is None:
                try:
                    from photonscript.scheduler.moon import moon_window_tonight
                    _gen_moon = moon_window_tonight(config, now=date_utc)
                except Exception as e:  # noqa: BLE001
                    logger.warning("moon window unavailable: %s", e)
                    _gen_moon = {}
            if _gen_moon.get("available"):
                from photonscript.scheduler.moon import (broadband_deferred,
                                                         describe_moon)
                if broadband_deferred(_gen_moon):
                    logger.info("Skipping %s tonight: broadband-only and the "
                                "moon defers broadband (%s)", proj.target.name,
                                describe_moon(_gen_moon))
                    continue

        alloc_time = sum(owed_seconds(e) for e in remaining_exposures) / 3600
        if proj is pick_proj:
            # the pick holds its time up (or until done); the rest stays for
            # the split before / after it
            remaining_hours -= min(vis_hours, need_s / 3600)
        else:
            remaining_hours -= alloc_time * 1.15  # account for overhead

        seq_target = NinaSequenceTarget(
            name=proj.target.name,
            ra_hours=proj.target.ra_hours,
            dec_degrees=proj.target.dec_degrees,
            exposures=remaining_exposures,
            dither_every_n=5,
            auto_focus_interval_minutes=60,
            camera_temp_c=config.camera_setpoint_c,
            # PS-26: a Piggy-600-driven target centers for the 600 mm frame
            driving_rig=getattr(proj, "driving_rig", "rc16") or "rc16",
            frame_center_ra_hours=getattr(proj, "frame_center_ra_hours", None),
            frame_center_dec_degrees=getattr(proj, "frame_center_dec_degrees",
                                             None),
            transit_utc=vp["visibility"].get("transit_time"),
        )
        transit = vp["visibility"].get("transit_time") or datetime.max
        m = getattr(proj, "mosaic", None)
        if m and m.get("id"):
            # PS-111: the panels of one mosaic stay together, in capture
            # order, at the transit of the mosaic's first panel tonight
            seq_target.mosaic_id = m["id"]
            seq_target.mosaic_note = _panel_note(proj, vp.get("held") or [])
            transit = vp.get("mosaic_transit") or transit
            if transit != datetime.max:
                transit = transit + timedelta(seconds=int(m.get("panel") or 0))
        sequence_targets.append((transit, seq_target))
        vis_by_name[seq_target.name] = vp["visibility"]
        need_by_name[seq_target.name] = need_s
        prio_by_name[seq_target.name] = proj.priority

    # Transit-order the selected targets (west-to-east through the night), with
    # a meridian guard so the run doesn't open on an immediate flip (see helper).
    sequence_targets, _deferred = meridian_safe_order(
        sequence_targets, dark_start,
        int(getattr(config, "meridian_guard_min", 20)))
    if _deferred:
        logger.info("Meridian guard: deferred %s past the meridian so the run "
                    "doesn't open on an immediate flip", ", ".join(_deferred))
    # PS-111: every panel but a mosaic's last one tonight shoots its owed
    # subs once and hands the mount to the next panel
    last = {t.mosaic_id: i for i, t in enumerate(sequence_targets)
            if t.mosaic_id}
    for i, t in enumerate(sequence_targets):
        if t.mosaic_id and last[t.mosaic_id] != i:
            t.repeat_while_up = False

    # PS-180: each target gets a time window and hands the mount on at its
    # end, so the first repeating target no longer holds the RC16 all night
    # (2026-10-07: 281 M31 subs, NGC 604 and the Heart never ran)
    deep_on = (pick is not None
               and any(t.name == pick["target"] for t in sequence_targets))
    if pick is not None and not deep_on:
        info["deep_note"] = (f"{pick['target']} has nothing the moon allows "
                             "tonight: split")
    if sequence_targets and (deep_on or bool(getattr(config, "plan_target_windows", True))):
        moon = None
        if getattr(config, "moon_aware_planning", True):
            # the generator's own moon window: the window cap and the
            # sequence's "until moonrise" conditions use the same moonrise
            try:
                from photonscript.scheduler.nina_sequence_json import (
                    _moon_window)
                moon = _moon_window()
            except Exception as e:  # noqa: BLE001
                logger.warning("moon window unavailable: %s", e)
                moon = {}
        deep_alloc = None
        if deep_on:
            kept, deep_alloc = _apply_deep_windows(
                sequence_targets, pick["target"], vis_by_name, need_by_name,
                prio_by_name, config, dark_start, dark_end, date_utc, moon,
                ha_first_by_name)
            if deep_alloc is None:
                info["deep_note"] = (f"{pick['target']} gets less than the "
                                     "minimum window tonight: split")
            else:
                sequence_targets = kept
                w0, w1 = deep_alloc["pick_window"]
                pick.update(window_start_utc=w0, window_end_utc=w1,
                            fallback=(f"{pick['target']} {deep_alloc['fallback']}"
                                      ", split after" if deep_alloc["fallback"]
                                      else None),
                            split_targets=[t.name for t in kept
                                           if t.name != pick["target"]])
                info["deep_pick"] = deep_night.json_pick(pick)
                logger.info("Deep night: %s %s-%sZ%s", pick["target"],
                            f"{w0:%H:%M}", f"{w1:%H:%M}",
                            f"; {pick['fallback']}" if pick.get("fallback") else "")
        if deep_alloc is None and bool(getattr(config, "plan_target_windows", True)):
            sequence_targets = _apply_windows(
                sequence_targets, vis_by_name, need_by_name, prio_by_name, config,
                dark_start, dark_end, date_utc, moon, ha_first_by_name)

    logger.info(
        "Night plan: %d targets, %.1f hours allocated",
        len(sequence_targets),
        dark_hours - remaining_hours,
    )
    if guided is not None:
        apply_guiding(sequence_targets, guided)
        info["guided"] = bool(guided)
    if info["rc16_nb_unguided"]:
        info["rc16_nb_note"] = "; ".join(
            f"{d['target']} {'/'.join(d['filters'])} not scheduled" for d in info["rc16_nb_unguided"]
        ) + f" ({NB_UNGUIDED_REASON})"
    return sequence_targets


def apply_guiding(targets, guided: bool) -> list:
    """PS-197: the arm's guiding mode on every planned target, the one place
    it is set. Unguided also clears PS-85 per-block flags (a per-block
    decision only exists inside a guided target). Returns the targets."""
    for t in targets or []:
        t.start_guiding = bool(guided)
        if not guided and getattr(t, "unguided_filters", None):
            t.unguided_filters = []
    return targets


def plan_with_info(planner, projects, config, now, **kw) -> tuple[list, dict]:
    """(targets, info) from a plan_night_sequence-like callable: info (deep
    night mode and pick) when the planner takes it, {} for a stand-in that
    does not (a caller's own wrapper or a test double). kw (guided,
    night_mode, deep_target) go to a planner that takes them; the caller
    applies guiding itself for one that does not (apply_guiding)."""
    import inspect
    info: dict = {}
    try:
        params = list(inspect.signature(planner).parameters.values())
    except (TypeError, ValueError):
        params = []
    var_kw = any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params)
    names = {p.name for p in params}
    extra = {k: v for k, v in kw.items()
             if v is not None and (var_kw or k in names)}
    if var_kw or "info" in names:
        extra["info"] = info
    return planner(projects, config, now, **extra), info
