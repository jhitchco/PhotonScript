"""Campaign planner v1: 14-night moon-aware allocation toward goal completion.

Scarcity logic: moon-free dark hours are the rare resource, so broadband
(L/RGB) deficits claim them first; narrowband tolerates moonlight and
fills everything else. First 7 nights use the real cloud forecast; beyond
that, monsoon-season climatology. Capacity-based v1 — per-target
visibility windows are not yet folded in, so ETAs are estimates.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

NB_FILTERS = {"Ha", "OIII", "SII"}
CLIMATOLOGY_USABLE = 0.40  # NM July average usable fraction of dark time


def build_campaign(config, store, forecast: dict | None = None,
                   days: int = 14) -> dict:
    from photonscript.shared.astronomy import get_twilight_times
    from photonscript.scheduler.moon import night_moon

    obs = config.get_observatory()
    fc_by_date = {}
    for n in (forecast or {}).get("nights", []):
        fc_by_date[n["date"]] = n

    # Goal deficits, priority order
    goals = []
    for p in sorted(store.projects.values(), key=lambda x: -x.priority):
        if not p.active:
            continue
        nb = sum(max(0, e.count - e.acquired) * e.exposure_seconds
                 for e in p.exposure_plans
                 if e.filter_type.value in NB_FILTERS) / 3600
        bb = sum(max(0, e.count - e.acquired) * e.exposure_seconds
                 for e in p.exposure_plans
                 if e.filter_type.value not in NB_FILTERS) / 3600
        done = sum(e.acquired * e.exposure_seconds
                   for e in p.exposure_plans) / 3600
        goals.append({"name": p.target.name, "priority": p.priority,
                      "nb_remaining_h": round(nb, 1),
                      "bb_remaining_h": round(bb, 1),
                      "hours_done": round(done, 1),
                      "goal_hours": p.budget_hours,
                      "eta": None,
                      "_nb": nb, "_bb": bb})

    nights = []
    now = datetime.utcnow()
    for d in range(days):
        night_dt = (now + timedelta(days=d)).replace(hour=0, minute=0,
                                                     second=0, microsecond=0)
        tw = get_twilight_times(obs, night_dt)
        start, end = tw.get("astro_dark_start"), tw.get("astro_dark_end")
        if not start or not end:
            continue
        date = fc_date = None
        # local evening date, matching the forecast convention
        from photonscript.shared.localtime import utc_offset_hours
        off = utc_offset_hours(config, start)
        date = (start + timedelta(hours=off)).strftime("%Y-%m-%d")
        dark_h = (end - start).total_seconds() / 3600
        moon = night_moon(config, date, start, end)
        fc = fc_by_date.get(date)
        if fc and fc.get("dark_hours"):
            frac = min(1.0, fc["usable_hours"] / fc["dark_hours"])
            frac_src = "forecast"
        else:
            frac, frac_src = CLIMATOLOGY_USABLE, "climatology"
        capacity = dark_h * frac
        illum = moon.get("illum_pct") or 0
        moon_free = moon.get("moon_free_h") or 0
        bb_cap = capacity if illum < 20 else min(capacity, moon_free * frac)

        assigned = []
        bb_used = 0.0
        for g in goals:
            take = min(g["_bb"], bb_cap - bb_used)
            if take > 0.2:
                g["_bb"] -= take
                bb_used += take
                assigned.append({"goal": g["name"], "kind": "BB",
                                 "hours": round(take, 1)})
        nb_left = capacity - bb_used
        for g in goals:
            take = min(g["_nb"], nb_left)
            if take > 0.2:
                g["_nb"] -= take
                nb_left -= take
                assigned.append({"goal": g["name"], "kind": "NB",
                                 "hours": round(take, 1)})
        for g in goals:
            if g["eta"] is None and g["_nb"] + g["_bb"] < 0.2:
                g["eta"] = date
        nights.append({"date": date, "dark_h": round(dark_h, 1),
                       "usable_frac": round(frac, 2),
                       "frac_source": frac_src,
                       "moon": moon, "assigned": assigned})

    nb_demand = sum(g["nb_remaining_h"] for g in goals)
    bb_demand = sum(g["bb_remaining_h"] for g in goals)
    bb_capacity = sum(
        (n["dark_h"] * n["usable_frac"]) if (n["moon"].get("illum_pct") or 0) < 20
        else min(n["dark_h"], (n["moon"].get("moon_free_h") or 0)) * n["usable_frac"]
        for n in nights)
    total_capacity = sum(n["dark_h"] * n["usable_frac"] for n in nights)
    nb_capacity = total_capacity - min(bb_demand, bb_capacity)
    for g in goals:
        g.pop("_nb"), g.pop("_bb")
    return {"nights": nights, "goals": goals,
            "totals": {"nb_capacity_h": round(nb_capacity, 1),
                       "nb_demand_h": round(nb_demand, 1),
                       "bb_capacity_h": round(bb_capacity, 1),
                       "bb_demand_h": round(bb_demand, 1)},
            "climatology_usable": CLIMATOLOGY_USABLE,
            "note": "v1 capacity-based: per-target visibility windows not "
                    "yet folded in; ETAs are estimates"}


def _dismissed_path(config):
    from pathlib import Path
    return Path(config.data_dir) / "dismissed_targets.json"


def load_dismissed(config) -> set:
    import json
    p = _dismissed_path(config)
    try:
        return set(json.loads(p.read_text(encoding="utf-8")))
    except Exception:  # noqa: BLE001
        return set()


def dismiss_target(config, name: str) -> None:
    import json
    d = load_dismissed(config)
    d.add(name.strip().lower())
    _dismissed_path(config).write_text(json.dumps(sorted(d)),
                                       encoding="utf-8")


def suggest_targets(config, store, campaign: dict, limit: int = 3) -> list:
    """Fill spare capacity: bright-moon surplus wants narrowband emission
    targets, dark-window surplus wants broadband. Excludes existing goals
    and dismissed suggestions."""
    from photonscript.shared.astronomy import get_seasonal_targets
    from photonscript.scheduler.project_store import target_kind

    t = campaign.get("totals", {})
    nb_spare = t.get("nb_capacity_h", 0) - t.get("nb_demand_h", 0)
    bb_spare = t.get("bb_capacity_h", 0) - t.get("bb_demand_h", 0)
    wanted = []
    if bb_spare > 3:
        wanted.append(("broadband", round(bb_spare, 1)))
    if nb_spare > 3:
        wanted.append(("narrowband", round(nb_spare, 1)))
    if not wanted:
        return []
    existing = {p.target.name.strip().lower()
                for p in store.projects.values()}
    dismissed = load_dismissed(config)
    month = datetime.now().month
    out = []
    for kind, spare in wanted:
        for tgt in get_seasonal_targets(month):
            if len([s for s in out if s["kind"] == kind]) >= limit:
                break
            n = tgt.name.strip().lower()
            if n in existing or n in dismissed:
                continue
            if target_kind(tgt) != kind:
                continue
            out.append({"name": tgt.name, "catalog": tgt.catalog_id,
                        "type": tgt.object_type, "kind": kind,
                        "spare_h": spare,
                        "reason": (f"{spare}h of unclaimed "
                                   f"{'dark-moon' if kind == 'broadband' else 'bright-moon'}"
                                   " capacity in the next 14 nights")})
    return out
