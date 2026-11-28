"""Deep night (2026-10-14): one goal for the whole dark window.

Jeremy, 2026-10-14: "it's kinda nice to be able to image a target a night
and create those deep, vivid images (lots of texture)". The planner's
default ("split", PS-180) shares each night among the goals by priority, so
every goal gets a slice. "deep" gives the dark window to ONE goal (both
rigs on it: the RC16 by its filter mix, the Piggy-600 companion shooting
OSC on the same pointing all night); when that goal sets before dawn or is
done, the rest of the night falls back to split.

Mode: config plan_night_mode ("split" default | "deep"), or a tonight-only
override (POST /api/tonight/mode, stored in <data_dir>/night_mode/<night>.json
like the night pilot's per-night ledger). The override is keyed by the
night (the local evening date, phd2_store.night_of), so it never applies to
another night, and the dawn shutdown marks it cleared.

The pick: the explicit target when one is given, else the best score over
tonight's visible goals (score_goal):

  hours_up  35  hours above the altitude floor from now to dawn, / dark hours
  fill      25  owed hours (the larger of the two rigs, they run in parallel)
                over the hours up: can the goal use the whole window?
  priority  20  goal priority / 100
  moon      10  share of the owed seconds tonight's moon allows (narrowband
                always; broadband / OSC by moon.broadband_deferred, the one
                moon rule, and only until moonrise when the moon rises
                later); 0 = not eligible tonight
  transit   10  1 at the dark-window midpoint, 0 at 6 h or more from it

On a night planned unguided (rc16_narrowband_requires_guided, 2026-10-25)
the RC16 narrowband owes nothing tonight (owed_by_kind nb_ok False): a
narrowband-only goal is not eligible unless the Piggy-600 owes time, and the
RC16 hold of a goal without an RC16 plan is broadband (L).

Mosaic panels are not auto-picked (their panels are gated in order); an
explicit pick may name one.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

MODES = ("split", "deep")
NB_FILTERS = {"Ha", "OIII", "SII"}
WEIGHTS = {"hours_up": 35.0, "fill": 25.0, "priority": 20.0, "moon": 10.0,
           "transit": 10.0}
TRANSIT_SPAN_H = 6.0
KIND = "night_mode"


# --------------------------------------------------------------------------
# mode and the tonight-only override
# --------------------------------------------------------------------------

def config_mode(cfg) -> str:
    m = str(getattr(cfg, "plan_night_mode", "split") or "split").strip().lower()
    return m if m in MODES else "split"


def night_label(cfg, dark_start: datetime) -> str:
    """The night's label (local evening date), as the runs and subs logs."""
    from photonscript.shared.phd2_store import night_of
    return night_of(cfg, dark_start)


def state_dir(cfg) -> Path:
    return Path(getattr(cfg, "data_dir", ".")) / "night_mode"


def state_path(cfg, night: str) -> Path:
    return state_dir(cfg) / f"{night}.json"


def load_state(cfg, night: str) -> dict:
    try:
        st = json.loads(state_path(cfg, night).read_text(encoding="utf-8"))
        if isinstance(st, dict) and st.get("night") == night:
            return st
    except (OSError, ValueError):
        pass
    return {"night": night}


def save_state(cfg, st: dict) -> None:
    from photonscript.shared.fileops import replace_retry
    p = state_path(cfg, st["night"])
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
        replace_retry(tmp, p)
    except OSError as e:
        logger.warning("night mode state not saved: %s", e)


def override(cfg, night: str) -> dict | None:
    """The tonight-only override {"mode", "target", "set_at"} for this
    night, None when unset or cleared."""
    ov = load_state(cfg, night).get("override")
    if isinstance(ov, dict) and ov.get("mode") in MODES and not ov.get("cleared_at"):
        return ov
    return None


def set_override(cfg, night: str, mode: str | None, target: str | None = None,
                 now: datetime | None = None) -> dict:
    """Set (mode "split" | "deep") or remove (mode None / "default") the
    override for one night. Returns the state."""
    st = load_state(cfg, night)
    now = now or datetime.utcnow()
    if mode in (None, "", "default", "config"):
        if isinstance(st.get("override"), dict):
            st["override"]["cleared_at"] = now.isoformat() + "Z"
            st["override"]["cleared_by"] = "operator"
    else:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES} or default")
        st["override"] = {"mode": mode, "target": (target or None) if mode == "deep" else None,
                          "set_at": now.isoformat() + "Z"}
    save_state(cfg, st)
    return st


def clear_at_dawn(cfg, night: str | None, now: datetime | None = None) -> str | None:
    """The dawn shutdown: the override was for this night only. Returns a
    shutdown step line, None when there was none."""
    if not night:
        return None
    st = load_state(cfg, night)
    ov = st.get("override")
    if not isinstance(ov, dict) or ov.get("cleared_at"):
        return None
    ov["cleared_at"] = (now or datetime.utcnow()).isoformat() + "Z"
    ov["cleared_by"] = "dawn"
    save_state(cfg, st)
    return f"night mode override ({ov.get('mode')}) cleared"


def resolve(cfg, night: str, mode: str | None = None,
            target: str | None = None) -> tuple[str, str | None, str]:
    """(mode, explicit target id or None, source) for a night: the caller's
    explicit mode wins, then the tonight-only override, then config."""
    if mode in MODES:
        return mode, (target if mode == "deep" else None), "explicit"
    try:
        ov = override(cfg, night)
    except Exception as e:  # noqa: BLE001
        logger.warning("night mode override unreadable: %s", e)
        ov = None
    if ov:
        return ov["mode"], ov.get("target"), "tonight"
    return config_mode(cfg), None, "config"


def record_dispatch(cfg, night: str, info: dict, now: datetime | None = None) -> None:
    """The armer's dispatch: keep the mode and pick it planned (for the
    morning report). Never raises."""
    try:
        st = load_state(cfg, night)
        st["dispatched"] = {"at": (now or datetime.utcnow()).isoformat() + "Z",
                            "mode": info.get("mode"),
                            "deep_pick": info.get("deep_pick")}
        save_state(cfg, st)
    except Exception as e:  # noqa: BLE001
        logger.warning("night mode dispatch record failed: %s", e)


# --------------------------------------------------------------------------
# the score
# --------------------------------------------------------------------------

def _rig(e) -> str:
    return getattr(e, "rig", "rc16") or "rc16"


def owed_by_kind(proj, nb_ok: bool = True) -> dict:
    """Owed seconds per kind: NB / BB (RC16) and OSC (Piggy-600). nb_ok
    False (an unguided night with rc16_narrowband_requires_guided): the RC16
    narrowband is not shot tonight, so it owes nothing tonight."""
    out = {"NB": 0.0, "BB": 0.0, "OSC": 0.0}
    for e in proj.exposure_plans:
        if _rig(e) != "rc16" or e.filter_type.value == "OSC":
            owed = max(0.0, e.count * e.exposure_seconds - e.long_seconds_done())
            out["OSC"] += owed
            continue
        owed = (e.exposure_seconds * max(0, e.count - e.acquired)
                + (e.hdr_short_seconds or 0) * e.short_remaining())
        out["NB" if e.filter_type.value in NB_FILTERS else "BB"] += owed
    if not nb_ok:
        out["NB"] = 0.0
    return out


def usable_span(vis: dict, start: datetime, end: datetime) -> tuple:
    """(from, until, hours) of a goal's time above its floor in [start, end]
    (rise_time / set_time are 10-minute samples: the set sample + 10 min)."""
    lo = max(start, vis.get("rise_time") or start)
    st = vis.get("set_time")
    hi = min(end, (st + timedelta(minutes=10)) if st else end)
    hours = max(0.0, (hi - lo).total_seconds() / 3600)
    if vis.get("hours") is not None:
        hours = min(hours, float(vis["hours"]))
    return lo, hi, hours


def _transit_offset_h(transit, mid: datetime) -> float | None:
    if not isinstance(transit, datetime):
        return None
    d = (transit - mid).total_seconds() / 3600
    d = (d + 12) % 23.934 - 12      # the nearest transit (sidereal day)
    return abs(d)


def moon_fit(owed: dict, lo: datetime, hi: datetime, bb_ok: bool,
             rise: datetime | None, moonset: datetime | None = None) -> float:
    """Share of the owed seconds tonight's moon allows (see module doc).
    moonset (moon.broadband_from): broadband only after it."""
    total = sum(owed.values())
    if total <= 0:
        return 0.0
    bb_frac = 0.0
    if bb_ok:
        bb_frac = 1.0
        if hi > lo and (rise is not None or moonset is not None):
            b_lo = max(lo, moonset) if moonset is not None else lo
            b_hi = min(hi, rise) if rise is not None else hi
            bb_frac = max(0.0, min(1.0, (b_hi - b_lo).total_seconds()
                                   / (hi - lo).total_seconds()))
    return (owed["NB"] + (owed["BB"] + owed["OSC"]) * bb_frac) / total


def score_goal(proj, vis: dict, start: datetime, end: datetime,
               dark_start: datetime, dark_end: datetime, *, bb_ok: bool = True,
               moonrise: datetime | None = None,
               moonset: datetime | None = None, nb_ok: bool = True) -> dict:
    """One goal's deep-night score with its breakdown (see module doc).
    nb_ok False: the RC16 narrowband is off tonight (unguided night,
    rc16_narrowband_requires_guided); the goal is scored on what is left."""
    owed = owed_by_kind(proj, nb_ok)
    rc16_h = (owed["NB"] + owed["BB"]) / 3600
    osc_h = owed["OSC"] / 3600
    lo, hi, up_h = usable_span(vis, start, end)
    dark_h = max(1e-6, (end - start).total_seconds() / 3600)
    mid = dark_start + (dark_end - dark_start) / 2
    toff = _transit_offset_h(vis.get("transit_time"), mid)
    mf = moon_fit(owed, lo, hi, bb_ok, moonrise, moonset)
    norm = {
        "hours_up": min(1.0, up_h / dark_h),
        "fill": min(1.0, max(rc16_h, osc_h) / up_h) if up_h > 0 else 0.0,
        "priority": max(0.0, min(1.0, float(proj.priority or 0) / 100)),
        "moon": mf,
        "transit": (0.0 if toff is None
                    else max(0.0, 1.0 - toff / TRANSIT_SPAN_H)),
    }
    value = {"hours_up": round(up_h, 2),
             "fill": {"rc16_owed_h": round(rc16_h, 2),
                      "piggy_owed_h": round(osc_h, 2)},
             "priority": proj.priority, "moon": round(mf, 2),
             "transit": None if toff is None else round(toff, 2)}
    comps = {k: {"value": value[k], "norm": round(norm[k], 3),
                 "points": round(WEIGHTS[k] * norm[k], 1)} for k in WEIGHTS}
    why = None
    if up_h <= 0:
        why = "not above its altitude floor tonight"
    elif rc16_h + osc_h <= 0:
        why = ("RC16 narrowband needs guiding (unguided night) and nothing else is owed"
               if not nb_ok and owed_by_kind(proj)["NB"] > 0
               else "nothing owed on either rig")
    elif mf <= 0:
        why = "the moon defers everything it owes tonight (broadband / OSC)"
    return {"project_id": proj.id, "target": proj.target.name,
            "score": round(sum(c["points"] for c in comps.values()), 1),
            "eligible": why is None, "why": why,
            "piggy_driven": (getattr(proj, "driving_rig", "rc16") or "rc16") != "rc16",
            "has_rc16_plan": any(_rig(e) == "rc16" for e in proj.exposure_plans),
            "up_from": lo, "up_until": hi, "components": comps}


def choose(visible_projects: list, start: datetime, end: datetime,
           dark_start: datetime, dark_end: datetime, *, target: str | None = None,
           bb_ok: bool = True, moonrise: datetime | None = None,
           moonset: datetime | None = None, nb_ok: bool = True) -> tuple:
    """(pick or None, all scores best first). visible_projects: the
    planner's [{"project", "visibility"}]. target: a project id (or target
    name) that wins whatever its score."""
    scores = []
    by_id = {}
    for vp in visible_projects:
        p = vp["project"]
        s = score_goal(p, vp["visibility"], start, end, dark_start, dark_end,
                       bb_ok=bb_ok, moonrise=moonrise, moonset=moonset, nb_ok=nb_ok)
        if getattr(p, "mosaic", None) and not _matches(p, target):
            s["eligible"], s["why"] = False, "mosaic panel (not auto-picked)"
        scores.append(s)
        by_id[id(s)] = p
    scores.sort(key=lambda s: (not s["eligible"], -s["score"]))
    if target:
        for s in scores:
            if _matches(by_id[id(s)], target):
                s["explicit"] = True
                return s, scores
        return None, scores
    pick = next((s for s in scores if s["eligible"]), None)
    return pick, scores


def _matches(proj, target: str | None) -> bool:
    if not target:
        return False
    t = str(target).strip().lower()
    return t in (str(proj.id or "").lower(), str(proj.target.name).lower(),
                 str(getattr(proj.target, "catalog_id", "") or "").lower())


def json_pick(s: dict | None) -> dict | None:
    """A score row with JSON-ready times."""
    if not s:
        return None
    out = dict(s)
    for k in ("up_from", "up_until", "window_start_utc", "window_end_utc"):
        v = out.get(k)
        if isinstance(v, datetime):
            out[k] = v.isoformat() + "Z"
    return out


# --------------------------------------------------------------------------
# the RC16 hold for a goal without an RC16 plan (Piggy-driven)
# --------------------------------------------------------------------------

def rc16_hold(proj, seconds: float, nb_ok: bool = True):
    """A goal with no RC16 plan picked for a deep night: the mount still
    has to point at it for the Piggy-600, so the RC16 shoots bonus subs of
    the default plan for the target type (suggest_exposure_plan's first
    set), sized to `seconds`. Not counted toward the goal (it has no RC16
    plan to credit)."""
    from photonscript.scheduler.target_planner import (broadband_hold,
                                                       suggest_exposure_plan)
    base = suggest_exposure_plan(proj.target)[0]
    if not nb_ok:
        base = broadband_hold(base)
    exp = float(base.exposure_seconds or 300)
    return base.model_copy(update={
        "count": max(1, math.ceil(max(seconds, exp) / exp - 1e-9)),
        "acquired": 0, "acquired_s": 0.0, "rig": "rc16"})


# --------------------------------------------------------------------------
# morning report
# --------------------------------------------------------------------------

def morning_line(cfg, date: str, rigs: list[dict]) -> str | None:
    """'Deep night on <target>: X h RC16, Y h Piggy' when the night was
    dispatched as a deep night; hours are the accepted lights on the pick
    (the card's per-rig hours)."""
    if not date:
        return None
    d = load_state(cfg, date).get("dispatched") or {}
    pick = d.get("deep_pick") or {}
    if d.get("mode") != "deep" or not pick.get("target"):
        return None
    from photonscript.shared.target_names import target_key
    key = target_key(pick["target"])
    hrs = {}
    for r in rigs or []:
        rows = (r.get("lights") or {}).get("hours") or []
        hrs[r.get("rig")] = sum(float(h.get("hours") or 0) for h in rows
                                if target_key(h.get("target") or "") == key)
    pig = sum(v for k, v in hrs.items() if k != "rc16")
    line = (f"Deep night on {pick['target']}: {hrs.get('rc16', 0.0):.1f} h RC16, "
            f"{pig:.1f} h Piggy")
    if pick.get("fallback"):
        line += f" ({pick['fallback']})"
    return line
