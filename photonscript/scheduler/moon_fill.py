"""Moon fill (2026-10-25): a bright-moon night is never left empty.

2026-10-25 (100% moon, unguided) GET /api/nightplan returned zero targets:
the moon tag was NB, so _fit_by_moon deferred all broadband; the night was
unguided, so rc16_narrowband_requires_guided removed the RC16 narrowband;
every active goal was LRGB only (M31, Heart Nebula, NGC 604) and the
Piggy-600 OSC goals of M31 and NGC 604 were met. No RC16 target meant no
pointing for the Piggy-600 either (it rides the same mount): a GREEN night
with both rigs idle.

The rule (config plan_moon_piggy_fill, default on; needs piggyback_enabled):
on a bright-moon night (tonight's moon tag NB, or the moon defers broadband
all night, moon.broadband_deferred), when the plan comes out empty or its
windows use less than plan_moon_fill_min_used_pct (50) of the dark time
left, every gap of at least plan_window_min_minutes goes to a "carrier":
the best active goal up in that gap, ranked

  1. the moon at least plan_moon_fill_min_sep_deg (30) away (a goal closer
     to the moon only when nothing else is up),
  2. the Piggy-600 OSC goal still owes time (Piggy OSC owed first),
  3. the most hours above its floor in the gap, then moon separation, then
     priority.

A goal whose OSC goal is met is still a carrier when nothing better is up:
the Piggy-600 keeps imaging rather than idling. The carrier keeps the mount
for its whole time up in the gap (not budget_hours x filter share); the time
before it rises / after it sets goes to the next best carrier.

The RC16 on a carrier shoots Ha at plan_moon_fill_carrier_s (narrowband is
never moon-deferred, so the generator keeps it): a pointing carrier. On an
unguided night its subs are expected to be rejected (the 2026-10-24 Ha
unguided night: 126 rejects in a row) and it is said so; guided, they are
bonus Ha. Either way they are not the goal's plan (an LRGB goal has no Ha
plan to credit). The Piggy-600 companion images OSC on the pointing, as on
every night.

Shown: info["moon_fill"] (reason, carriers with windows, moon separation,
Piggy owed) and info["moon_fill_note"] (one line) in GET /api/tonight and
/api/nightplan; the evening plan heads-up carries the note and marks the
carrier rows.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

NB_FILTERS = {"Ha", "OIII", "SII"}


def enabled(config) -> bool:
    return (bool(getattr(config, "plan_moon_piggy_fill", True))
            and bool(getattr(config, "piggyback_enabled", False))
            and bool(getattr(config, "moon_aware_planning", True)))


def bright_moon(moon_tag, mw: dict | None) -> bool:
    """Tonight's moon is bright enough to defer broadband: the night tag is
    NB, or the one moon rule defers broadband for the whole night."""
    if moon_tag == "NB":
        return True
    if mw and mw.get("available"):
        from photonscript.scheduler.moon import broadband_deferred
        return bool(broadband_deferred(mw))
    return False


def gaps(spans: list[tuple], start: datetime, end: datetime,
         min_s: float) -> list[tuple]:
    """The parts of [start, end] no span covers, each at least min_s long."""
    out = []
    cur = start
    for a, b in sorted((max(start, a), min(end, b)) for a, b in spans
                       if a is not None and b is not None and b > a):
        if a > cur and (a - cur).total_seconds() >= min_s:
            out.append((cur, a))
        cur = max(cur, b)
    if end > cur and (end - cur).total_seconds() >= min_s:
        out.append((cur, end))
    return out


def moon_radec(config, when: datetime):
    """(ra_deg, dec_deg) of the moon from the observatory at `when`, or None."""
    try:
        from astropy.coordinates import get_body
        from astropy.time import Time
        from photonscript.shared.astronomy import get_earth_location
        loc = get_earth_location(config.get_observatory())
        m = get_body("moon", Time(when), loc)
        return float(m.ra.deg), float(m.dec.deg)
    except Exception as e:  # noqa: BLE001
        logger.warning("moon position unavailable: %s", e)
        return None


def separation_deg(ra_h: float, dec_d: float, moon) -> float | None:
    """Great-circle separation (deg) of a target from the moon."""
    if moon is None:
        return None
    r1, d1 = math.radians(ra_h * 15.0), math.radians(dec_d)
    r2, d2 = math.radians(moon[0]), math.radians(moon[1])
    c = (math.sin(d1) * math.sin(d2)
         + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def osc_owed_s(proj) -> float:
    """Seconds the Piggy-600 OSC goal still owes."""
    return sum(max(0.0, e.count * e.exposure_seconds - e.long_seconds_done())
               for e in proj.exposure_plans
               if (getattr(e, "rig", "rc16") or "rc16") != "rc16")


def candidates(projects, visible_projects, config, date_utc, twilight,
               start: datetime, end: datetime, exclude: set,
               moon=None) -> list[dict]:
    """Every active goal (not a mosaic panel, not already planned) with its
    usable span tonight, moon separation and Piggy OSC owed. A goal the
    planner skipped as complete is a candidate too (the Piggy-600 can still
    use it)."""
    from photonscript.scheduler.deep_night import usable_span
    from photonscript.shared.astronomy import compute_visibility_window
    obs = config.get_observatory()
    vis_of = {id(vp["project"]): vp["visibility"] for vp in visible_projects}
    out = []
    for p in projects:
        if not p.active or getattr(p, "mosaic", None):
            continue
        if p.target.name in exclude:
            continue
        vis = vis_of.get(id(p))
        if vis is None:
            try:
                vis = compute_visibility_window(p.target, obs, date_utc,
                                                twilight=twilight)
            except Exception as e:  # noqa: BLE001
                logger.warning("moon fill: no visibility for %s: %s",
                               p.target.name, e)
                continue
        if not vis.get("visible"):
            continue
        lo, hi, hours = usable_span(vis, start, end)
        if hours <= 0:
            continue
        sep = separation_deg(p.target.ra_hours, p.target.dec_degrees, moon)
        out.append({"project": p, "vis": vis, "lo": lo, "hi": hi,
                    "sep": sep, "osc_owed_s": osc_owed_s(p)})
    return out


def _rank(c: dict, g0: datetime, g1: datetime, min_sep: float):
    lo, hi = max(g0, c["lo"]), min(g1, c["hi"])
    overlap = max(0.0, (hi - lo).total_seconds())
    sep = c["sep"]
    sep_ok = sep is None or sep >= min_sep
    return (sep_ok, c["osc_owed_s"] > 0, round(overlap / 600.0),
            sep if sep is not None else 0.0, c["project"].priority or 0), lo, hi


def choose(cands: list[dict], gaps_: list[tuple], min_s: float,
           min_sep: float) -> list[tuple]:
    """[(candidate, start, end)] covering the gaps: per gap the best
    candidate (see module doc) for its overlap, the rest of the gap
    (before it rises, after it sets) filled the same way. A goal carries
    once per night."""
    used: set = set()
    picks: list[tuple] = []

    def fill(g0, g1):
        if (g1 - g0).total_seconds() < min_s:
            return
        best = None
        for c in cands:
            if id(c) in used:
                continue
            key, lo, hi = _rank(c, g0, g1, min_sep)
            if (hi - lo).total_seconds() < min_s:
                continue
            if best is None or key > best[0]:
                best = (key, c, lo, hi)
        if best is None:
            return
        _key, c, lo, hi = best
        used.add(id(c))
        picks.append((c, lo, hi))
        fill(g0, lo)
        fill(hi, g1)

    for g0, g1 in gaps_:
        fill(g0, g1)
    picks.sort(key=lambda x: x[1])
    return picks


def carrier_exposure(proj, config, seconds: float):
    """The RC16 carrier set: Ha at plan_moon_fill_carrier_s, sized to the
    window (85%, the planner's overhead factor); gain / offset from the
    goal's own RC16 plan (else the configured defaults)."""
    from photonscript.shared.models import ExposurePlan, FilterType
    exp = float(getattr(config, "plan_moon_fill_carrier_s", 300.0) or 300.0)
    rc16 = [e for e in proj.exposure_plans
            if (getattr(e, "rig", "rc16") or "rc16") == "rc16"]
    ref = next((e for e in rc16 if e.filter_type.value == "Ha"),
               rc16[0] if rc16 else None)
    gain = ref.gain if ref is not None else int(getattr(config, "default_gain", 200))
    offset = (ref.offset if ref is not None
              else int(getattr(config, "default_offset", 256)))
    return ExposurePlan(filter_type=FilterType.HA, exposure_seconds=exp,
                        count=max(1, math.ceil(seconds * 0.85 / exp - 1e-9)),
                        gain=gain, offset=offset, rig="rc16")


def _hm(dt: datetime) -> str:
    return f"{dt:%H:%M}Z"


def carrier_note(name: str, guided, piggy_owed_h: float, sep) -> str:
    how = ("unguided: the RC16 subs are expected to be rejected"
           if guided is False else
           "guided: the RC16 Ha is bonus data, not the goal's plan"
           if guided else "the RC16 subs may be rejected if unguided")
    pig = (f"Piggy-600 OSC owes {piggy_owed_h:.1f} h" if piggy_owed_h > 0
           else "Piggy-600 OSC goal met, extra OSC data")
    moon = f", {sep:.0f} deg from the moon" if sep is not None else ""
    return (f"Moon fill: {name} is a pointing carrier for the Piggy-600 "
            f"(RC16 Ha, {how}; {pig}{moon}).")


def apply(sequence_targets: list, *, projects, visible_projects, config,
          date_utc: datetime, twilight: dict, dark_start: datetime,
          dark_end: datetime, moon_tag, moon_window: dict | None, guided,
          skipped: dict, info: dict) -> list:
    """The moon fill over tonight's planned targets (see module doc).
    Returns the targets, carriers included, in run order. info gets
    moon_fill / moon_fill_note (None when nothing was filled)."""
    info.setdefault("moon_fill", None)
    info.setdefault("moon_fill_note", None)
    if not enabled(config) or not bright_moon(moon_tag, moon_window):
        return sequence_targets
    start, end = max(dark_start, date_utc), dark_end
    total_s = (end - start).total_seconds()
    from photonscript.scheduler.target_planner import _min_window_s
    min_s = _min_window_s(config)
    if total_s < min_s:
        return sequence_targets
    if any(t.window_start_utc is None or t.window_end_utc is None
           for t in sequence_targets):
        return sequence_targets   # no windows (plan_target_windows off)
    spans = [(t.window_start_utc, t.window_end_utc) for t in sequence_targets]
    spans += [(t.fill_from_utc, t.fill_end_utc or end) for t in sequence_targets
              if getattr(t, "fill_from_utc", None) is not None]
    open_ = gaps(spans, start, end, min_s)
    open_s = sum((b - a).total_seconds() for a, b in open_)
    used_pct = 100.0 * (1 - open_s / total_s) if total_s > 0 else 100.0
    min_used = float(getattr(config, "plan_moon_fill_min_used_pct", 50.0) or 0)
    if not open_ or (sequence_targets and used_pct >= min_used):
        return sequence_targets
    moon = moon_radec(config, start + (end - start) / 2)
    cands = candidates(projects, visible_projects, config, date_utc, twilight,
                       start, end, {t.name for t in sequence_targets}, moon)
    min_sep = float(getattr(config, "plan_moon_fill_min_sep_deg", 30.0) or 0)
    picks = choose(cands, open_, min_s, min_sep)
    illum = (moon_window or {}).get("illum_pct")
    lit = f"{illum:.0f}% moon" if isinstance(illum, (int, float)) else "bright moon"
    why_bits = []
    for name, why in skipped.items():
        why_bits.append(f"{name}: {why}")
    reason = (f"{lit} defers broadband"
              + (" and the RC16 narrowband needs guiding (unguided night)"
                 if info.get("rc16_nb_unguided") else "")
              + (": nothing planned" if not sequence_targets
                 else f": the plan used {used_pct:.0f}% of the dark time left"))
    if not picks:
        info["moon_fill"] = {"active": False, "reason": reason, "skipped": why_bits,
                             "carriers": [], "used_pct": round(used_pct, 1)}
        info["moon_fill_note"] = (f"Moon fill: {reason}; no active goal is up "
                                  "long enough to carry the Piggy-600")
        logger.info("%s", info["moon_fill_note"])
        return sequence_targets
    from photonscript.shared.models import NinaSequenceTarget
    carriers = []
    rows = []
    for c, lo, hi in picks:
        p = c["project"]
        exp = carrier_exposure(p, config, (hi - lo).total_seconds())
        owed_h = c["osc_owed_s"] / 3600
        t = NinaSequenceTarget(
            name=p.target.name, ra_hours=p.target.ra_hours,
            dec_degrees=p.target.dec_degrees, exposures=[exp],
            dither_every_n=5, auto_focus_interval_minutes=60,
            camera_temp_c=config.camera_setpoint_c,
            driving_rig=getattr(p, "driving_rig", "rc16") or "rc16",
            frame_center_ra_hours=getattr(p, "frame_center_ra_hours", None),
            frame_center_dec_degrees=getattr(p, "frame_center_dec_degrees", None),
            transit_utc=c["vis"].get("transit_time"),
            window_start_utc=lo, window_end_utc=hi,
            carrier=True,
            carrier_note=carrier_note(p.target.name, guided, owed_h, c["sep"]))
        carriers.append(t)
        rows.append({"target": p.target.name, "project_id": p.id,
                     "window_start_utc": lo.isoformat() + "Z",
                     "window_end_utc": hi.isoformat() + "Z",
                     "hours": round((hi - lo).total_seconds() / 3600, 2),
                     "filter": exp.filter_type.value,
                     "exposure_s": exp.exposure_seconds, "count": exp.count,
                     "piggy_owed_h": round(owed_h, 2),
                     "moon_sep_deg": (round(c["sep"], 1) if c["sep"] is not None
                                      else None),
                     "expect_rejected": guided is not True})
    out = sorted(list(sequence_targets) + carriers,
                 key=lambda t: t.window_start_utc)
    has_fill = any(getattr(t, "fill_from_utc", None) is not None for t in out)
    for i, t in enumerate(out):
        later = i < len(out) - 1
        if later or has_fill:
            t.handoff_utc = t.window_end_utc
        elif t.carrier:
            t.handoff_utc = None   # the last one keeps the mount to the loop end
    covered_s = sum((b - a).total_seconds() for a, b in spans) + sum(
        (t.window_end_utc - t.window_start_utc).total_seconds() for t in carriers)
    info["moon_fill"] = {
        "active": True, "reason": reason, "skipped": why_bits, "carriers": rows,
        "used_pct": round(used_pct, 1),
        "filled_pct": round(min(100.0, 100.0 * covered_s / total_s), 1),
        "guided": guided,
    }
    car = "; ".join(f"{r['target']} {_hm(datetime.fromisoformat(r['window_start_utc'][:-1]))}"
                    f"-{_hm(datetime.fromisoformat(r['window_end_utc'][:-1]))}"
                    for r in rows)
    expect = ("RC16 Ha as pointing carrier, subs expected to be rejected"
              if guided is not True else "RC16 Ha as pointing carrier")
    info["moon_fill_note"] = (f"Moon fill (Piggy-driven night): {reason}; "
                              f"{car} ({expect})")
    logger.info("%s", info["moon_fill_note"])
    return out
