"""Campaign planner v2 (PS-30): 14-night slot allocation toward goal completion.

Every night's astronomical dark is cut into 10-minute slots. Per slot:

  * a goal can use it only while its target is above its altitude floor
    (project min_alt_deg, else config.campaign_min_alt_deg, flat 30 deg at
    AARO Pier 3: no roof/terrain profile);
  * broadband and OSC need the moon test: the night must pass the one moon
    rule (moon.broadband_deferred, the same rule the sequence generator and
    target planner use, PS-27) and, with the moon 20% lit or more, the moon
    must be down in that slot (the generator's moon-timed BB window);
  * moon-free slots are scarce, so a goal with broadband left claims them
    first, by priority (RC16 L/RGB, or OSC when the piggyback drives; a
    passenger OSC plan does not claim them); narrowband fills the rest, and
    a passenger OSC plan left on its own takes what is still free;
  * the mount points at one target per slot, so slot time is shared, and is
    weighted by the night's usable fraction (forecast for 7 nights, then
    CLIMATOLOGY_USABLE).

Rig-aware credit: a slot spent on project X credits the RC16 plan being shot
and, when the moon allows, X's Piggy-600 OSC plan too (the passenger rides
the same pointing). driving_rig says which rig the mount centers for; it is
stored and shown (the centering offset itself is PS-26).

Per goal: visible hours in the horizon, per-rig progress, season (a goal
with no visible slot in the horizon is `parked` with the first night it
returns; it is skipped, never deactivated), calibration (readiness.py, per
rig) and status: active | parked | lights_done_needs_calibration | complete.
`complete` needs matching flats, darks and bias when require_calibration.
check_goal_transitions() sends one Pushover when a goal turns complete or
lights_done_needs_calibration (PS-14, absorbed).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

NB_FILTERS = {"Ha", "OIII", "SII"}
CLIMATOLOGY_USABLE = 0.40  # NM July average usable fraction of dark time
SLOT_MIN = 10
SLOT_H = SLOT_MIN / 60
ETA_EPS_H = 0.2           # a goal is "done" for ETA once under this
SEASON_MIN_H = 1.5        # a night "counts" for the season with this much
SEASON_DAYS = 365
SEASON_STEP_DAYS = 7
RC16, PIGGYBACK = "rc16", "piggyback"
KINDS = ("NB", "BB", "OSC")
STATUS_FILE = "campaign_status.json"
SEASON_FILE = "season_cache.json"
NOTIFY_STATUSES = ("complete", "lights_done_needs_calibration")


# --- goal bookkeeping ---------------------------------------------------------

def plan_kind(e) -> str:
    """NB / BB (RC16 filters) or OSC (any non-RC16 rig: the Piggy-600)."""
    if (getattr(e, "rig", RC16) or RC16) != RC16 or e.filter_type.value == "OSC":
        return "OSC"
    return "NB" if e.filter_type.value in NB_FILTERS else "BB"


def plan_seconds(e) -> tuple[float, float]:
    """(goal seconds, accepted seconds) of one plan, HDR short set included.

    Seconds-based so PS-66's `acquired_s` (accepted seconds; capped unguided
    subs count for what they really hold) is honored when present; without
    it, seconds = sub length x count, which is what PS-66 derives too."""
    short = (e.hdr_short_seconds or 0) if e.hdr_short_count else 0
    goal = e.count * e.exposure_seconds + e.hdr_short_count * short
    acq_s = getattr(e, "acquired_s", None)
    if acq_s is None:
        done = (min(e.acquired, e.count) * e.exposure_seconds
                + min(e.hdr_short_acquired, e.hdr_short_count) * short)
    else:
        done = float(acq_s)
    return float(goal), float(min(done, goal))


def _min_alt(config, p) -> float:
    if p.min_alt_deg is not None:
        return float(p.min_alt_deg)
    return float(getattr(config, "campaign_min_alt_deg", 30.0) or 30.0)


def _goal(config, p) -> dict:
    rem = {k: 0.0 for k in KINDS}
    by_rig: dict[str, dict] = {}
    done_s = goal_s = 0.0
    for e in p.exposure_plans:
        g, d = plan_seconds(e)
        k = plan_kind(e)
        rem[k] += max(0.0, g - d) / 3600
        goal_s += g
        done_s += d
        rig = getattr(e, "rig", RC16) or RC16
        r = by_rig.setdefault(rig, {"goal_h": 0.0, "done_h": 0.0,
                                    "filters": [], "eta": None,
                                    "passenger": rig != p.driving_rig})
        r["goal_h"] += g / 3600
        r["done_h"] += d / 3600
        r["filters"].append(e.filter_type.value)
    return {"id": p.id, "name": p.target.name, "priority": p.priority,
            "driving_rig": p.driving_rig, "min_alt_deg": _min_alt(config, p),
            "goal_hours": round(goal_s / 3600, 1),
            "hours_done": round(done_s / 3600, 1),
            "eta": None, "_rem": rem, "_by_rig": by_rig, "_p": p}


def _rig_rem(g: dict, rig: str) -> float:
    if rig == RC16:
        return g["_rem"]["NB"] + g["_rem"]["BB"]
    return g["_rem"]["OSC"]


# --- nights and slots -----------------------------------------------------------

def _night_dates(config, now: datetime, days: int) -> list[datetime]:
    """UTC-midnight bases (get_twilight_times convention: the LOCAL evening
    date) starting with the night now belongs to (before local noon: the
    night that began yesterday evening)."""
    from photonscript.shared.localtime import utc_offset_hours
    local = now + timedelta(hours=utc_offset_hours(config, now))
    first = (local - timedelta(hours=12)).replace(hour=0, minute=0, second=0,
                                                  microsecond=0)
    return [first + timedelta(days=d) for d in range(days + 1)]


def _season(config, goals: list[dict], now: datetime) -> None:
    """goal["_season"] = {"weeks_visible", "returns"} from a yearly sweep:
    one night a week for SEASON_DAYS, hours above the goal's floor inside
    astro dark (15-min samples). Cached in <data_dir>/season_cache.json by
    RA/Dec/floor and refreshed monthly."""
    import numpy as np
    from astropy import units as u
    from astropy.coordinates import AltAz, get_sun
    from astropy.time import Time
    from photonscript.shared.astronomy import altitude_grid, get_earth_location

    path = Path(config.data_dir) / SEASON_FILE
    try:
        cache = json.loads(path.read_text(encoding="utf-8")) \
            if path.exists() else {}
    except Exception:  # noqa: BLE001
        cache = {}
    month = now.strftime("%Y-%m")

    def key(g):
        t = g["_p"].target
        return f"{t.ra_hours:.4f},{t.dec_degrees:.4f},{g['min_alt_deg']:g}"

    todo = [g for g in goals
            if (cache.get(key(g)) or {}).get("month") != month]
    if todo:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        dates = [start + timedelta(days=SEASON_STEP_DAYS * k)
                 for k in range(SEASON_DAYS // SEASON_STEP_DAYS + 1)]
        per = 57  # 23:00 UTC + 14 h at 15 min
        offs = np.arange(per) * 0.25 / 24
        base = np.array([Time(d.replace(hour=23)).jd for d in dates])
        t = Time((base[:, None] + offs[None, :]).ravel(), format="jd")
        obs = config.get_observatory()
        sun = get_sun(t).transform_to(AltAz(
            obstime=t, location=get_earth_location(obs))).alt.deg
        dark = (np.asarray(sun) < -18).reshape(len(dates), per)
        alts = altitude_grid([g["_p"].target for g in todo], obs, t)
        for g, row in zip(todo, alts):
            up = (row >= g["min_alt_deg"]).reshape(len(dates), per)
            hours = (up & dark).sum(axis=1) * 0.25
            weeks = [[d.strftime("%Y-%m-%d"), round(float(h), 2)]
                     for d, h in zip(dates, hours)]
            cache[key(g)] = {"month": month, "weeks": weeks}
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(cache), encoding="utf-8")
        except OSError as e:
            logger.debug("season cache not written: %s", e)
    today = now.strftime("%Y-%m-%d")
    for g in goals:
        weeks = (cache.get(key(g)) or {}).get("weeks") or []
        ok = [d for d, h in weeks if h >= SEASON_MIN_H and d >= today]
        g["_season"] = {"weeks_visible": len(ok),
                        "returns": ok[0] if ok else None}


def _calibration(config, goals: list[dict], cal_contexts: dict | None) -> None:
    """goal["calibration"]: per-rig readiness.calibration_missing() of the
    goal's planned filters/exposures. ok None = could not be checked."""
    from photonscript.scheduler.readiness import (calibration_context,
                                                  calibration_missing,
                                                  project_rigs,
                                                  target_readiness)
    ctxs = dict(cal_contexts or {})
    for g in goals:
        p = g["_p"]
        out = {"ok": True, "missing": [], "by_rig": {}}
        try:
            for rig in project_rigs(p):
                if rig not in ctxs:
                    ctxs[rig] = calibration_context(config, rig, max_age_s=300)
                miss = calibration_missing(target_readiness(config, p,
                                                            ctxs[rig]))
                out["by_rig"][rig] = {"missing": miss, "ok": not miss}
                out["missing"] += [f"{rig}: {m}" for m in miss]
            out["ok"] = not out["missing"]
        except Exception as e:  # noqa: BLE001 - badge only; never break the plan
            out = {"ok": None, "missing": [], "by_rig": {}, "error": str(e)}
        g["calibration"] = out


def _status(g: dict, parked: bool) -> str:
    lights_done = sum(g["_rem"].values()) * 3600 < 1.0
    if lights_done:
        if not g["_p"].require_calibration or g["calibration"].get("ok"):
            return "complete"
        return "lights_done_needs_calibration"
    return "parked" if parked else "active"


def build_campaign(config, store, forecast: dict | None = None,
                   days: int = 14, now: datetime | None = None,
                   cal_contexts: dict | None = None,
                   with_calibration: bool = True,
                   with_season: bool = True) -> dict:
    """The 14-night plan (see module doc). now / cal_contexts are for tests;
    with_calibration / with_season = False skip those scans."""
    import numpy as np
    from astropy.time import Time
    from photonscript.scheduler.moon import (broadband_deferred, moon_series,
                                             moon_tag)
    from photonscript.shared.astronomy import altitude_grid, dark_windows
    from photonscript.shared.localtime import utc_offset_hours

    now = now or datetime.utcnow()
    obs = config.get_observatory()
    fc_by_date = {n["date"]: n for n in (forecast or {}).get("nights", [])}

    goals = [_goal(config, p) for p in sorted(
        store.projects.values(), key=lambda x: (-x.priority, x.target.name))
        if p.active]

    # Nights: dark windows for all of them in one sun transform; slots that
    # already ended are dropped (night 0 may be under way).
    dates = _night_dates(config, now, days)
    nights_raw = []
    for base, (start, end) in zip(dates, dark_windows(obs, dates)):
        if len(nights_raw) >= days:
            break
        if not start or not end or end <= start:
            continue
        n_slots = int((end - start).total_seconds() // (SLOT_MIN * 60))
        slots = [start + timedelta(minutes=SLOT_MIN * i)
                 for i in range(n_slots)]
        live = [s for s in slots if s + timedelta(minutes=SLOT_MIN) > now]
        if not live:
            continue
        nights_raw.append({"base": base, "start": start, "end": end,
                           "slots": slots, "live": live})

    # One altitude transform for every goal x every slot midpoint, one moon
    # transform for every slot midpoint plus each night's dark start.
    mids = [s + timedelta(minutes=SLOT_MIN / 2)
            for n in nights_raw for s in n["slots"]]
    dusks = [n["start"] for n in nights_raw]
    alt = (altitude_grid([g["_p"].target for g in goals], obs, Time(mids))
           if goals and mids else np.zeros((len(goals), len(mids))))
    m_alt, m_ill = (moon_series(config, Time(mids + dusks)) if mids
                    else (np.zeros(0), np.zeros(0)))

    # visibility per goal per night, then park goals with none in the horizon
    off = 0
    for n in nights_raw:
        k = len(n["slots"])
        n["sl"] = slice(off, off + k)
        off += k
    for gi, g in enumerate(goals):
        g["_vis"] = alt[gi] >= g["min_alt_deg"] if len(mids) else np.zeros(0)
        g["visible_h_14n"] = round(float(g["_vis"].sum()) * SLOT_H, 1)
    if with_season and goals:
        try:
            _season(config, goals, now)
        except Exception as e:  # noqa: BLE001
            logger.warning("season sweep failed: %s", e)
    for g in goals:
        g.setdefault("_season", {"weeks_visible": None, "returns": None})
        g["_parked"] = g["visible_h_14n"] <= 0
        g["season"] = {"in_season": not g["_parked"], **g["_season"]}

    nights = []
    nb_cap = bb_cap = total_cap = 0.0
    for ni, n in enumerate(nights_raw):
        start, end = n["start"], n["end"]
        utc_off = utc_offset_hours(config, start)
        date = (start + timedelta(hours=utc_off)).strftime("%Y-%m-%d")
        dark_h = (end - start).total_seconds() / 3600
        sl = n["sl"]
        malt = m_alt[sl]
        illum = float(np.median(m_ill[sl])) if len(malt) else 0.0
        moon_free = float(np.mean(malt < 0)) * dark_h if len(malt) else 0.0
        mw = {"available": True, "down_at_dusk": bool(m_alt[len(mids) + ni] < 0),
              "illum_pct": round(illum)}
        if illum < 20:
            bb_ok = np.ones(len(malt), dtype=bool)
        elif broadband_deferred(mw):
            bb_ok = np.zeros(len(malt), dtype=bool)
        else:
            bb_ok = malt < 0  # moon-timed BB window
        moon = {"illum_pct": round(illum), "moon_free_h": round(moon_free, 1),
                "tag": moon_tag(illum, moon_free),
                "broadband_deferred": bool(illum >= 20
                                           and broadband_deferred(mw))}
        fc = fc_by_date.get(date)
        if fc and fc.get("dark_hours"):
            frac = min(1.0, fc["usable_hours"] / fc["dark_hours"])
            frac_src = "forecast"
        else:
            frac, frac_src = CLIMATOLOGY_USABLE, "climatology"
        credit = SLOT_H * frac
        first_live = len(n["slots"]) - len(n["live"])

        runs: dict[tuple, dict] = {}  # (goal idx, kind) -> assignment
        mount_slots = 0
        for i in range(first_live, len(n["slots"])):
            j = sl.start + i
            total_cap += credit
            if bb_ok[i]:
                bb_cap += credit
            else:
                nb_cap += credit
            pick = None
            if bb_ok[i]:
                pick = next((gi for gi, g in enumerate(goals)
                             if not g["_parked"] and g["_vis"][j]
                             and (g["_rem"]["BB"] > 1e-9
                                  or (g["driving_rig"] != RC16
                                      and g["_rem"]["OSC"] > 1e-9))), None)
            if pick is None:
                pick = next((gi for gi, g in enumerate(goals)
                             if not g["_parked"] and g["_vis"][j]
                             and g["_rem"]["NB"] > 1e-9), None)
            if pick is None and bb_ok[i]:  # passenger OSC left on its own
                pick = next((gi for gi, g in enumerate(goals)
                             if not g["_parked"] and g["_vis"][j]
                             and g["_rem"]["OSC"] > 1e-9), None)
            if pick is None:
                continue
            g = goals[pick]
            mount_slots += 1
            shot = []
            if bb_ok[i] and g["_rem"]["BB"] > 1e-9:
                shot.append(("BB", RC16))
            elif g["_rem"]["NB"] > 1e-9:
                shot.append(("NB", RC16))
            if bb_ok[i] and g["_rem"]["OSC"] > 1e-9:
                shot.append(("OSC", PIGGYBACK))
            s0 = n["slots"][i]
            for kind, rig in shot:
                take = min(credit, g["_rem"][kind])
                g["_rem"][kind] -= take
                a = runs.get((pick, kind))
                if a is None:
                    a = runs[(pick, kind)] = {
                        "goal": g["name"], "kind": kind, "rig": rig,
                        "passenger": rig != g["driving_rig"],
                        "hours": 0.0, "start": s0,
                        "_order": len(runs)}
                a["hours"] += take
                a["end"] = s0 + timedelta(minutes=SLOT_MIN)
        assigned = []
        for a in sorted(runs.values(), key=lambda x: x["_order"]):
            if a["hours"] < 0.05:
                continue
            a.pop("_order")
            a["hours"] = round(a["hours"], 2)
            a["start"] = a["start"].isoformat(timespec="minutes") + "Z"
            a["end"] = a["end"].isoformat(timespec="minutes") + "Z"
            assigned.append(a)
        for g in goals:
            if g["eta"] is None and sum(g["_rem"].values()) < ETA_EPS_H:
                g["eta"] = date
            for rig, r in g["_by_rig"].items():
                if r["eta"] is None and _rig_rem(g, rig) < ETA_EPS_H:
                    r["eta"] = date
        nights.append({"date": date, "dark_h": round(dark_h, 1),
                       "dark_start": start.isoformat(timespec="minutes") + "Z",
                       "dark_end": end.isoformat(timespec="minutes") + "Z",
                       "usable_frac": round(frac, 2), "frac_source": frac_src,
                       "mount_h": round(mount_slots * credit, 2),
                       "moon": moon, "assigned": assigned})

    if with_calibration and goals:
        _calibration(config, goals, cal_contexts)
    for g in goals:
        g.setdefault("calibration", {"ok": None, "missing": [], "by_rig": {},
                                     "error": "not checked"})

    # Remaining demand is reported from the store, not the simulation.
    nb_demand = bb_demand = 0.0
    for g in goals:
        p = g["_p"]
        rem0 = _goal(config, p)["_rem"]
        g["nb_remaining_h"] = round(rem0["NB"], 1)
        g["bb_remaining_h"] = round(rem0["BB"] + rem0["OSC"], 1)
        g["osc_remaining_h"] = round(rem0["OSC"], 1)
        nb_demand += rem0["NB"]
        # OSC rides along as a passenger; it only claims mount time when the
        # piggyback drives
        bb_demand += rem0["BB"] + (rem0["OSC"] if p.driving_rig != RC16
                                   else 0.0)
        g["status"] = _status({**g, "_rem": rem0}, g["_parked"])
        g["returns"] = g["season"]["returns"] if g["status"] == "parked" \
            else None
        g["by_rig"] = {rig: {"goal_h": round(r["goal_h"], 1),
                             "done_h": round(r["done_h"], 1),
                             "remaining_h": round(r["goal_h"] - r["done_h"], 1),
                             "filters": r["filters"], "eta": r["eta"],
                             "passenger": r["passenger"]}
                       for rig, r in g["_by_rig"].items()}
        for k in [k for k in g if k.startswith("_")]:
            g.pop(k)
    nb_capacity = total_cap - min(bb_demand, bb_cap)
    return {"nights": nights, "goals": goals,
            "totals": {"nb_capacity_h": round(nb_capacity, 1),
                       "nb_demand_h": round(nb_demand, 1),
                       "bb_capacity_h": round(bb_cap, 1),
                       "bb_demand_h": round(bb_demand, 1)},
            "climatology_usable": CLIMATOLOGY_USABLE,
            "slot_min": SLOT_MIN,
            "note": "v2: 10-min slots inside astro dark, target above its "
                    "altitude floor, one moon rule for broadband/OSC; "
                    "hours weighted by forecast/climatology usable fraction"}


# --- completion notifications (PS-14, absorbed) -----------------------------

def _status_path(config) -> Path:
    return Path(config.data_dir) / STATUS_FILE


def notify_transitions(config, goals: list[dict]) -> list[str]:
    """Persist each goal's status; Pushover once when one turns `complete`
    or `lights_done_needs_calibration`. The first call only records a
    baseline (no alert storm on deploy). Returns the messages sent."""
    path = _status_path(config)
    try:
        prev = json.loads(path.read_text(encoding="utf-8")) \
            if path.exists() else None
    except Exception:  # noqa: BLE001
        prev = None
    cur = dict(prev or {})
    msgs = []
    for g in goals:
        key = str(g.get("id") or g["name"])
        st = g.get("status")
        if not st:
            continue
        old = (prev or {}).get(key)
        cur[key] = st
        if prev is None or old is None or old == st \
                or st not in NOTIFY_STATUSES:
            continue
        if st == "complete":
            msgs.append(f"Campaign complete: {g['name']} "
                        f"({g.get('hours_done', '?')} h of lights, "
                        "calibration on hand)")
        else:
            miss = ", ".join((g.get("calibration") or {}).get("missing")
                             or []) or "calibration unknown"
            msgs.append(f"Campaign lights done: {g['name']} "
                        f"({g.get('hours_done', '?')} h); still needs {miss}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(cur, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("campaign status not saved: %s", e)
    if msgs and getattr(config, "campaign_notify", True):
        _fire(config, msgs)
    for m in msgs:
        logger.info(m)
    return msgs


def _fire(config, msgs: list[str]) -> None:
    """Send from sync or async context (goal sync runs in worker threads)."""
    import asyncio
    from photonscript.shared.pushover import notify

    async def _send():
        for m in msgs:
            await notify(config, m, title="PhotonScript campaign")
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_send())
        return
    loop.create_task(_send())


def check_goal_transitions(config, store) -> list[str]:
    """Cheap status check after goal progress changes (sync_goal_progress):
    lights from the store, calibration only for goals whose lights are done
    (readiness context reused for 5 min). Parked is only known to the full
    planner, so other goals keep their recorded status."""
    path = _status_path(config)
    try:
        prev = json.loads(path.read_text(encoding="utf-8")) \
            if path.exists() else {}
    except Exception:  # noqa: BLE001
        prev = {}
    goals = []
    for p in store.projects.values():
        if not p.active:
            continue
        g = _goal(config, p)
        if sum(g["_rem"].values()) * 3600 >= 1.0:
            old = prev.get(str(p.id or p.target.name))
            g["status"] = old if old in ("active", "parked") else "active"
        else:
            _calibration(config, [g], None)
            g["status"] = _status(g, False)
        goals.append(g)
    return notify_transitions(config, goals)


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
