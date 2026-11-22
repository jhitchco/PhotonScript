"""Targets page board (2026-11-21): sections, tonight's campaign line and the
night altitude sparkline per card.

Jeremy, 2026-11-21: "fonts a little bit smaller ... see what the campaign
calls [for], and then underneath, show where it is in the sky over the
night ... active projects, which means that they're still ongoing, and then
projects which are done. Then ... a whole set of test ones ... expandable,
collapsible, or hideable."

Sections (section_of, from the /api/targets card fields only):

  test    a tracking test, optics test, focus calibration, guider
          commission or TPoint run name (target_names.is_test_target /
          is_calibration_run_target, tracking_test.is_tracking_test), and
          the unattributed bucket (loop-container names, not a target)
  done    a goal at 100% (goal pct, accepted seconds capped per plan), or a
          target imaged with no goal at all (past imaging, nothing owed)
  active  a goal still short of 100% with active=True (the scheduler and
          the campaign plan it)
  paused  a goal still short of 100% with active=False: its own group so
          Active lists only what can be shot tonight

The tonight board (build) reuses what the server already computed: the
/api/tonight payload (target_planner via app.api_tonight_plan: windows and
per-filter sets) and the campaign (campaign.build_campaign: tonight's
assigned hours, hours over the horizon, ETA, status). Nothing is planned
here. The altitude series is one altitude_grid transform for every goal
over astro dark at STEP_MIN steps, plus the moon (moon.moon_series).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

SECTIONS = ("active", "paused", "done", "test")
STEP_MIN = 15
RC16 = "rc16"


# --- sections ---------------------------------------------------------------

def is_test_name(name: Any) -> bool:
    from photonscript.scheduler.tracking_test import is_tracking_test
    from photonscript.shared.target_names import (is_calibration_run_target,
                                                  is_test_target)
    return bool(is_tracking_test(name) or is_test_target(name)
                or is_calibration_run_target(name))


def section_of(card: dict) -> tuple[str, str]:
    """(section, why) for one /api/targets card."""
    if card.get("unattributed"):
        return "test", "subs recorded under a loop container name"
    if is_test_name(card.get("name")):
        return "test", "test or calibration run"
    goal = card.get("goal")
    if card.get("project_id") is None:
        return "done", "no goal: imaged before goals or as a one-off"
    if goal and (goal.get("pct") or 0) >= 100:
        return "done", "goal met"
    if card.get("active"):
        return "active", "goal active, short of 100%"
    return "paused", "goal paused (inactive), short of 100%"


def annotate(cards: Iterable[dict]) -> list[dict]:
    """Adds card["section"] and card["section_why"] in place; returns the
    list."""
    out = list(cards)
    for c in out:
        c["section"], c["section_why"] = section_of(c)
    return out


def section_counts(cards: Iterable[dict]) -> dict:
    n = {s: 0 for s in SECTIONS}
    for c in cards:
        n[c.get("section") or section_of(c)[0]] += 1
    return n


# --- time helpers -----------------------------------------------------------

def _parse(v) -> datetime | None:
    if not v:
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(v).replace("Z", "")).replace(
            tzinfo=None)
    except ValueError:
        return None


def tz_abbr(config, when_utc: datetime) -> str:
    """MST / MDT for the observatory at that instant ('' without tzdata)."""
    try:
        from datetime import timezone
        from zoneinfo import ZoneInfo
        return when_utc.replace(tzinfo=timezone.utc).astimezone(
            ZoneInfo(config.observatory_tz)).tzname() or ""
    except Exception:  # noqa: BLE001
        return ""


def _hm(config, dt: datetime) -> str:
    from photonscript.shared.localtime import to_local
    return to_local(config, dt).strftime("%H:%M")


def _frac(dt: datetime | None, t0: datetime, t1: datetime) -> float | None:
    if dt is None or t1 <= t0:
        return None
    return round(max(0.0, min(1.0, (dt - t0).total_seconds()
                              / (t1 - t0).total_seconds())), 4)


# --- the campaign line --------------------------------------------------------

def _fmt_sets(exposures: list[dict]) -> str:
    """'Ha 7 / OIII 11 / SII 15 x 300 s' when every set shares a length,
    else 'L 10x180 s / R 6x120 s'."""
    sets = [e for e in exposures if (e.get("count") or 0) > 0]
    if not sets:
        return ""
    lens = {float(e.get("exposure_seconds") or 0) for e in sets}
    if len(lens) == 1:
        s = " / ".join(f"{e['filter_type']} {e['count']}" for e in sets)
        return f"{s} x {lens.pop():g} s"
    return " / ".join(f"{e['filter_type']} {e['count']}x"
                      f"{float(e.get('exposure_seconds') or 0):g} s"
                      for e in sets)


def _campaign_night(campaign: dict | None, date: str) -> dict | None:
    nights = (campaign or {}).get("nights") or []
    return next((n for n in nights if n.get("date") == date),
                nights[0] if nights else None)


def campaign_summary(goal: dict | None, campaign: dict | None,
                     night_date: str) -> dict | None:
    """Tonight's and the horizon's assigned hours for one campaign goal."""
    if goal is None:
        return None
    name = goal.get("name")
    tonight_h = horizon_h = 0.0
    nights = set()
    tonight_rigs: list[str] = []
    for n in (campaign or {}).get("nights") or []:
        for a in n.get("assigned") or []:
            if a.get("goal") != name:
                continue
            h = float(a.get("hours") or 0)
            horizon_h += h
            nights.add(n.get("date"))
            if n.get("date") == night_date:
                tonight_h += h
                if a.get("rig") and a["rig"] not in tonight_rigs:
                    tonight_rigs.append(a["rig"])
    nb = float(goal.get("nb_remaining_h") or 0)
    bb = float(goal.get("bb_remaining_h") or 0)     # RC16 L/RGB + OSC
    remaining = round(nb + bb, 1) if ("nb_remaining_h" in goal
                                      or "bb_remaining_h" in goal) else         round(max(0.0, float(goal.get("goal_hours") or 0)
                  - float(goal.get("hours_done") or 0)), 1)
    return {"status": goal.get("status"), "returns": goal.get("returns"),
            "priority": goal.get("priority"),
            "driving_rig": goal.get("driving_rig"),
            "min_alt_deg": goal.get("min_alt_deg"),
            "remaining_h": remaining, "nb_remaining_h": round(nb, 1),
            "bb_remaining_h": round(bb, 1), "eta": goal.get("eta"),
            "tonight_h": round(tonight_h, 1),
            "tonight_rigs": tonight_rigs,
            "horizon_h": round(horizon_h, 1), "nights": len(nights),
            "horizon_days": len((campaign or {}).get("nights") or [])}


def _short_date(d: str | None) -> str:
    if not d:
        return ""
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%b %d").replace(
            " 0", " ")
    except ValueError:
        return d


def campaign_text(c: dict | None) -> str:
    """'Campaign: 7.1 h left, 6.6 h over 3 of 14 nights, ETA Nov 23, p50'."""
    if not c:
        return ""
    if c["status"] in ("complete", "lights_done_needs_calibration"):
        return "Campaign: lights done" + (
            ", calibration owed" if c["status"] != "complete" else "")
    parts = [f"{c['remaining_h']:g} h left"]
    if c["horizon_h"]:
        parts.append(f"{c['horizon_h']:g} h planned on {c['nights']} of the "
                     f"next {c['horizon_days']} nights")
    else:
        parts.append(f"no slot in the next {c['horizon_days']} nights")
    if c.get("eta"):
        parts.append("ETA " + _short_date(c["eta"]))
    elif c["remaining_h"] > 0:
        parts.append(f"ETA past {c['horizon_days']} nights")
    if c.get("priority") is not None:
        parts.append(f"p{c['priority']}")
    return "Campaign: " + ", ".join(parts)


def tonight_line(config, card: dict, plan: dict | None, camp: dict | None,
                 *, paused_tonight: bool = False, unguided: dict | None = None,
                 night: dict | None = None, up_h: float | None = None,
                 floor: float = 30.0) -> dict:
    """{status, text, reason} for one goal: planned (window and sets per
    rig from the planner) or why not."""
    from photonscript.shared.rigs import rig_label
    if plan is not None:
        t0 = _parse(plan.get("window_start_utc"))
        t1 = _parse(plan.get("window_end_utc"))
        by_rig: dict[str, list] = {}
        for e in plan.get("exposures") or []:
            by_rig.setdefault(e.get("rig") or RC16, []).append(e)
        chunks = []
        for i, (rig, exps) in enumerate(by_rig.items()):
            win = ""
            if i == 0 and t0 and t1:
                win = f" {_hm(config, t0)}-{_hm(config, t1)} {tz_abbr(config, t0)}".rstrip()
            sets = _fmt_sets(exps)
            sep = ", " if win else " "
            chunks.append(f"{rig_label(config, rig)}{win}"
                          + (f"{sep}{sets}" if sets else ""))
        text = "Tonight: " + "; ".join(chunks) if chunks else "Tonight: planned"
        if plan.get("carrier"):
            text += " (pointing carrier)"
        return {"status": "planned", "text": text, "reason": None,
                "carrier": bool(plan.get("carrier"))}

    def no(reason: str, status: str = "not_planned") -> dict:
        return {"status": status, "text": f"Not tonight: {reason}",
                "reason": reason}

    if card.get("section") == "done":
        return no("goal met", "done")
    if card.get("section") == "paused" or card.get("active") is False:
        return no("goal paused", "paused")
    if paused_tonight:
        return no("paused for tonight", "paused")
    if camp and camp["status"] == "parked":
        return no("out of season" + (f", returns {_short_date(camp['returns'])}"
                                     if camp.get("returns") else ""))
    if camp and camp["status"] in ("complete", "lights_done_needs_calibration"):
        return no("lights done")
    if unguided:
        return no(f"{'/'.join(unguided.get('filters') or [])} needs guiding; "
                  "tonight is unguided")
    if up_h is not None and up_h <= 0:
        return no(f"below {floor:g} deg all night")
    if camp and camp["tonight_h"] > 0:
        return no(f"campaign has {camp['tonight_h']:g} h, the planner "
                  "filled the night with others")
    moon = (night or {}).get("moon") or {}
    if (camp and camp["bb_remaining_h"] > 0 and camp["nb_remaining_h"] <= 0
            and (moon.get("illum_pct") or 0) >= 20):
        if moon.get("broadband_deferred"):
            return no(f"moon {moon.get('illum_pct')}%, broadband deferred")
        return no(f"moon {moon.get('illum_pct')}%, broadband left "
                  f"({moon.get('moon_free_h', 0):g} h moon-free went "
                  "elsewhere)")
    return no("higher-priority goals fill its hours")


# --- the board ---------------------------------------------------------------

def _window_fracs(t0, t1, dusk, dawn):
    a, b = _frac(t0, dusk, dawn), _frac(t1, dusk, dawn)
    return [a, b] if a is not None and b is not None and b > a else None


def build(config, projects: Iterable, tonight: dict, campaign: dict | None,
          *, cards: Iterable[dict] = (), paused_ids: Iterable[str] = (),
          now: datetime | None = None) -> dict:
    """The tonight board for every goal: campaign line, tonight line and
    altitude sparkline. tonight: the /api/tonight payload; campaign: the
    /api/campaign payload (or build_campaign's dict), None when it failed.
    cards: /api/targets cards (annotated or not) for the section of each
    goal; paused_ids: tonight_pause.paused_ids for the night."""
    import numpy as np
    from astropy.time import Time

    from photonscript.scheduler.moon import moon_series
    from photonscript.shared.astronomy import altitude_grid
    from photonscript.shared.target_names import target_key

    now = (now or datetime.utcnow()).replace(tzinfo=None)
    projects = list(projects or ())
    tw = (tonight or {}).get("twilight") or {}
    dusk, dawn = _parse(tw.get("astro_dark_start")), _parse(
        tw.get("astro_dark_end"))
    date = (tonight or {}).get("date") or ""
    night = _campaign_night(campaign, date)
    if (dusk is None or dawn is None) and night:
        dusk, dawn = _parse(night.get("dark_start")), _parse(
            night.get("dark_end"))
    out: dict = {"date": date, "dusk_utc": None, "dawn_utc": None,
                 "tz": "", "moon": [], "moon_illum_pct": None,
                 "now_frac": None, "targets": {},
                 "campaign_ok": campaign is not None,
                 "step_min": STEP_MIN}
    if dusk is None or dawn is None or dawn <= dusk:
        return out

    n = int((dawn - dusk).total_seconds() // (STEP_MIN * 60)) + 1
    times = [dusk + timedelta(minutes=STEP_MIN * i) for i in range(n)]
    if times[-1] < dawn:
        times.append(dawn)
    tt = Time(times)
    alt = altitude_grid([p.target for p in projects], config.get_observatory(),
                        tt) if projects else np.zeros((0, len(times)))
    try:
        m_alt, m_ill = moon_series(config, tt)
        moon = [round(float(a), 1) for a in m_alt]
        illum = round(float(np.median(m_ill)))
    except Exception:  # noqa: BLE001 - the moon line is optional
        moon, illum = [], None
    out.update({
        "dusk_utc": dusk.isoformat() + "Z", "dawn_utc": dawn.isoformat() + "Z",
        "dusk_local": _hm(config, dusk), "dawn_local": _hm(config, dawn),
        "tz": tz_abbr(config, dusk),
        "ticks": _ticks(config, dusk, dawn),
        "moon": moon, "moon_illum_pct": illum,
        "now_frac": _frac(now, dusk, dawn) if dusk <= now <= dawn else None})

    plans = {target_key(t.get("name")): t
             for t in (tonight or {}).get("targets") or []}
    goals = {g.get("id"): g for g in (campaign or {}).get("goals") or []}
    unguided = {target_key(d.get("target")): d
                for d in (tonight or {}).get("rc16_nb_unguided") or []}
    by_key = {c.get("key"): c for c in cards or ()}
    paused = set(paused_ids or ())
    default_floor = float(getattr(config, "campaign_min_alt_deg", 30.0) or 30.0)
    step_h = (dawn - dusk).total_seconds() / 3600 / max(1, len(times) - 1)

    for i, p in enumerate(projects):
        k = target_key(p.target.name)
        card = dict(by_key.get(k) or {"key": k, "name": p.target.name,
                                      "active": bool(p.active),
                                      "project_id": p.id})
        if "section" not in card:
            card["section"], card["section_why"] = section_of(card)
        if card["section"] == "test":
            continue
        floor = float(p.min_alt_deg) if p.min_alt_deg is not None \
            else default_floor
        a = alt[i]
        up = a >= floor
        up_h = round(float(up[:-1].sum()) * step_h, 1) if len(a) > 1 else 0.0
        plan = plans.get(k)
        camp = campaign_summary(goals.get(p.id), campaign, date)
        line = tonight_line(config, card, plan, camp,
                            paused_tonight=p.id in paused,
                            unguided=unguided.get(k), night=night,
                            up_h=up_h, floor=floor)
        win = None
        if plan is not None:
            win = _window_fracs(_parse(plan.get("window_start_utc")),
                                _parse(plan.get("window_end_utc")), dusk, dawn)
        camp_win = None
        for asg in (night or {}).get("assigned") or []:
            if asg.get("goal") == p.target.name:
                w = _window_fracs(_parse(asg.get("start")),
                                  _parse(asg.get("end")), dusk, dawn)
                if w:
                    camp_win = [min(w[0], camp_win[0]) if camp_win else w[0],
                                max(w[1], camp_win[1]) if camp_win else w[1]]
        peak = int(np.argmax(a)) if len(a) else 0
        out["targets"][k] = {
            "name": p.target.name, "project_id": p.id,
            "section": card["section"],
            "tonight": line, "campaign": camp,
            "campaign_text": campaign_text(camp),
            "alts": [round(float(x), 1) for x in a],
            "floor": floor, "up_h": up_h,
            "peak_alt": round(float(a[peak]), 1) if len(a) else None,
            "peak_local": _hm(config, times[peak]) if len(a) else None,
            "window": win, "campaign_window": camp_win}
    return out


def _ticks(config, dusk: datetime, dawn: datetime) -> list[dict]:
    """Whole local hours inside the night, every 2 h, as axis ticks."""
    from photonscript.shared.localtime import utc_offset_hours
    off = utc_offset_hours(config, dusk)
    loc = dusk + timedelta(hours=off)
    t = (loc.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
    if t.hour % 2:
        t += timedelta(hours=1)
    out = []
    while t - timedelta(hours=off) < dawn:
        out.append({"frac": _frac(t - timedelta(hours=off), dusk, dawn),
                    "label": t.strftime("%H")})
        t += timedelta(hours=2)
    return out
