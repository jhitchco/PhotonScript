"""PS-122: Calibration owed, the live version of the hand-made checklist.

    owed_report(config, rig=None, projects=None, now=None) -> dict

Which calibration frames each rig still needs for the lights it has already
shot (and tonight's plan). Inputs, per rig:

  * lights: the subs logs (runs/<night>_subs.jsonl) of the last
    calibration_owed_lookback_days nights, active goals only, rejected subs
    left out. Each light is bucketed by its dark-matching epoch (exposure,
    gain, offset, set temp, binning and, PS-128, readout mode HCG / LCG).
    Records from before PS-122 have no gain / offset / binning / readout:
    they take the rig's current epoch (readout: camera_readout_mode /
    piggyback_readout_mode) and are marked "assumed".
  * tonight's plan: the RC16 plan snapshot (calibration_plan.tonight_plan_rows,
    the same source the gap report uses) and, for the Piggy-600 with OSC
    lights on, piggyback_exposure_s.
  * the calibration library through PS-113 QA (PS-128: darks and bias only
    at the bucket's readout mode; a frame whose header has no readout
    keyword is assumed to be at the rig's and counted in
    "frames_readout_assumed"): darks counted by
    calibration.dark_quota (the very function the armer's unsafe darks and
    the Piggy-600 companion size their blocks with, so this view and the
    night quota always agree); flats and bias from QA-passed sessions (the
    header scan when a rig has no QA store yet).

Output per rig: darks owed by (exposure, gain, offset, temp) with have / need
(dark_target_count) and the nights whose lights use them; a config fix when a
light length is not in the rig's night quota list (the night quota would
never fill it); flats per filter (last set, count, age vs 45 d, lights shot
since); bias; which nights' lights are uncalibrated; and the constraints.
Report only: nothing here shoots or moves anything.

PS-160 (calibration coverage): the same lights also drive the night plan.
light_dark_lengths() gives the on-epoch lengths the lights used that the
config dark list lacks (the Piggy-600's 300 s lights against 120 / 400 s
darks); calibration.night_dark_exposures() adds them to the RC16 unsafe
darks and the Piggy-600 companion (calibration_darks_follow_lights, at most
calibration_darks_follow_lights_max per rig and night). used_filters() and
flats_reset_date() restrict the RC16 stale-flat reshoots to the filters the
lights used and mark flats older than an optics change
(calibration_flats_reset) owed. Each dark item also carries the newest
matching dark (age) and the sensor temperature the counted darks were shot
at; each rig carries "plan" (what tonight's sequences will fill) and
morning_note() is the line the dawn "Night complete" push carries.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

TEMP_TOL_C = 1.5        # same set-temp match count_matching_darks uses
EXP_TOL_S = 0.5

CONSTRAINT_DARKS = ("Darks and bias need the roof closed at night (no "
                    "shutter): the RC16 armer fills its quota in unsafe "
                    "time, the Piggy-600 companion while the roof is closed.")
CONSTRAINT_DAYTIME = ("Daytime darks are off: light leak in the dome (Known "
                      "Operational Lessons).")
CONSTRAINT_FLATS = ("Flats need a safe (open) roof at dawn: sky flats only, "
                    "no flat panel. A closed roof at dawn skips them.")


def _fmt(v) -> str:
    return f"{float(v):g}"


def _same_exp(a, b) -> bool:
    return abs(float(a) - float(b)) < EXP_TOL_S


def _nights_in_window(config, days: int, now: datetime) -> list[str]:
    from photonscript.scheduler.runs import runs_dir
    cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
    try:
        names = sorted(p.name[:10] for p in runs_dir(config).glob("*_subs.jsonl"))
    except OSError:
        return []
    return [n for n in names if n >= cutoff]


def _active_index(projects) -> tuple:
    from photonscript.shared.target_names import known_target_index, target_key
    by_key = {target_key(p.target.name): p for p in projects}
    known = known_target_index(list(projects)) if projects else None
    return by_key, known


def collect_lights(config, rig: str, projects, *, days: int,
                   now: datetime) -> list[dict]:
    """Lights of active goals for one rig over the lookback window, one dict
    per sub: night, target, filter, exp_s, gain, offset, settemp, xbin,
    readout (normalized), assumed (epoch fields filled from the rig's
    epoch), readout_assumed (no readout logged: the rig's mode)."""
    from photonscript.shared.rigs import normalize_readout
    from photonscript.scheduler.calibration import dark_epoch
    from photonscript.scheduler.runs import _load_subs
    from photonscript.scheduler.sub_index import REJECTED, verdict_of
    from photonscript.shared.target_names import canonical_target, target_key
    if not projects:
        return []
    ep = dark_epoch(config, rig)
    by_key, known = _active_index(projects)
    out = []
    for night in _nights_in_window(config, days, now):
        for rec in _load_subs(config, night):
            if (rec.get("rig") or "rc16") != rig:
                continue
            if verdict_of(rec)[0] == REJECTED:
                continue
            canon = canonical_target(rec.get("target"), known)
            proj = by_key.get(target_key(canon)) if canon else None
            if proj is None:
                continue
            try:
                exp = round(float(rec.get("exp_s") or 0), 1)
            except (TypeError, ValueError):
                continue
            if exp <= 0.01:
                continue
            assumed = rec.get("gain") is None or rec.get("offset") is None
            ro = normalize_readout(rec.get("readout"))
            ro_assumed = ro is None
            if ro is None or not ep["readout"]:
                ro = ep["readout"]
            st = rec.get("set_temp")
            try:
                st = float(st) if st is not None else ep["setpoint"]
            except (TypeError, ValueError):
                st = ep["setpoint"]
            out.append({
                "night": night, "target": proj.target.name,
                "filter": "OSC" if rig != "rc16" else (rec.get("filter") or "?"),
                "exp_s": exp,
                "gain": int(rec["gain"]) if rec.get("gain") is not None else ep["gain"],
                "offset": (int(rec["offset"]) if rec.get("offset") is not None
                           else ep["offset"]),
                "settemp": st, "xbin": int(rec.get("xbin") or 1),
                "readout": ro, "readout_assumed": ro_assumed,
                "assumed": assumed})
    return out


def _active_projects(config) -> list:
    from photonscript.scheduler.calibration_plan import load_projects
    return [p for p in load_projects(config) if getattr(p, "active", True)]


def _lookback(config) -> int:
    return max(1, int(getattr(config, "calibration_owed_lookback_days", 60) or 60))


def light_dark_lengths(config, rig: str, lights: list | None = None, *,
                       quota: list | None = None,
                       now: datetime | None = None) -> list[float]:
    """PS-160: on-epoch exposure lengths of the rig's lights (active goals,
    owed lookback) that the quota list lacks, most-used first, at most
    calibration_darks_follow_lights_max. [] when
    calibration_darks_follow_lights is off. `lights` from collect_lights
    (read here when None); `quota` defaults to quota_exposures."""
    if not getattr(config, "calibration_darks_follow_lights", True):
        return []
    cap = int(getattr(config, "calibration_darks_follow_lights_max", 2) or 0)
    if cap <= 0:
        return []
    from photonscript.scheduler.calibration import dark_epoch, quota_exposures
    ep = dark_epoch(config, rig)
    if quota is None:
        quota = quota_exposures(config, rig)
    if lights is None:
        lights = collect_lights(config, rig, _active_projects(config),
                                days=_lookback(config), now=now or datetime.now())
    counts: dict[float, int] = {}
    for li in lights:
        if not _on_epoch(li, ep):
            continue
        e = float(li["exp_s"])
        if any(_same_exp(e, q) for q in quota):
            continue
        k = next((x for x in counts if _same_exp(x, e)), e)
        counts[k] = counts.get(k, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [float(e) for e, _n in ranked[:cap]]


def used_filters(config, rig: str = "rc16", *,
                 now: datetime | None = None) -> set:
    """PS-160: canonical filters the rig's lights used in the owed lookback
    (active goals). Empty when no lights are logged."""
    lights = collect_lights(config, rig, _active_projects(config),
                            days=_lookback(config), now=now or datetime.now())
    return {li["filter"] for li in lights if li.get("filter")}


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_RIG_ALIASES = {"piggy": "piggyback", "piggy-600": "piggyback", "osc": "piggyback",
                "rc16": "rc16", "piggyback": "piggyback"}


def flats_reset_date(config, rig: str) -> str | None:
    """PS-160: the rig's optics-change date from calibration_flats_reset
    ("rc16:YYYY-MM-DD,piggyback:YYYY-MM-DD"; a bare date = every rig).
    Flats older than it are owed. None when unset or unparseable."""
    raw = str(getattr(config, "calibration_flats_reset", "") or "").strip()
    out = None
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        if ":" in tok:
            name, date = (x.strip() for x in tok.split(":", 1))
            if _RIG_ALIASES.get(name.lower()) != rig:
                continue
        else:
            date = tok
        if _DATE_RE.match(date) and (out is None or date > out):
            out = date
    return out


def _dark_newest(store: dict, b: dict) -> dict:
    """PS-160: newest QA-passed dark matching a bucket and the sensor
    temperatures (CCD-TEMP) of the matching frames."""
    from photonscript.scheduler import calibration_qa as cq
    newest, temps = None, []
    for r in (store or {}).get("frames", {}).values():
        if r.get("type") != "DARK" or not cq.passed(r):
            continue
        if abs((r.get("exptime") or -1) - b["exp_s"]) >= EXP_TOL_S:
            continue
        if r.get("gain") != b["gain"] or r.get("offset") != b["offset"]:
            continue
        st = r.get("settemp")
        if st is None or abs(st - b["settemp"]) >= TEMP_TOL_C:
            continue
        if b.get("readout") and r.get("readout") and r["readout"] != b["readout"]:
            continue
        d = r.get("date")
        if d and (newest is None or d > newest):
            newest = d
        if r.get("ccdtemp") is not None:
            temps.append(float(r["ccdtemp"]))
    temps.sort()
    return {"newest": newest,
            "ccd_temp_c": (round(temps[len(temps) // 2], 1) if temps else None),
            "ccd_temp_max_off_c": (round(max(abs(t - b["settemp"]) for t in temps), 1)
                                   if temps else None)}


def planned_lights(config, rig: str) -> list[dict]:
    """Tonight's planned exposures at the rig's epoch."""
    from photonscript.scheduler.calibration import dark_epoch
    ep = dark_epoch(config, rig)
    rows = []
    if rig == "rc16":
        from photonscript.scheduler.calibration_plan import tonight_plan_rows
        for exp, flt, label in tonight_plan_rows(config):
            rows.append((exp, flt, label))
    elif getattr(config, "piggyback_image_lights", True):
        rows.append((float(getattr(config, "piggyback_exposure_s", 120.0)), "OSC",
                     "tonight: OSC lights (piggyback_exposure_s)"))
    return [{"exp_s": round(float(e), 1), "filter": f, "label": lb,
             "gain": ep["gain"], "offset": ep["offset"],
             "settemp": ep["setpoint"], "xbin": 1, "readout": ep["readout"]}
            for e, f, lb in rows]


def _on_epoch(b: dict, ep: dict) -> bool:
    return (b["gain"] == ep["gain"] and b["offset"] == ep["offset"]
            and abs(b["settemp"] - ep["setpoint"]) < TEMP_TOL_C
            and b["xbin"] == 1 and b.get("readout") == ep["readout"])


def _ro_txt(ro) -> str:
    return f", {ro}" if ro else ""


def _quota_env(rig: str) -> tuple[str, str]:
    return (("PS_DARK_EXPOSURES", "dark_exposures") if rig == "rc16"
            else ("PS_PIGGYBACK_DARK_EXPOSURES", "piggyback_dark_exposures"))


def _config_list(config, rig: str) -> list[float]:
    """The raw config dark list (no PS-66 cap), for the fix text."""
    from photonscript.scheduler.calibration_plan import _floats
    key = _quota_env(rig)[1]
    default = "600,180" if rig == "rc16" else "120"
    return _floats(getattr(config, key, default))


def _dark_items(config, view, rig, store, lights, planned) -> tuple[list, list]:
    """(dark items, config fixes)."""
    from photonscript.scheduler.calibration import (dark_epoch, dark_quota,
                                                    quota_exposures)
    ep = dark_epoch(config, rig)
    qlist = quota_exposures(view, rig)
    # PS-160: the lengths the lights used join the night quota (the same
    # rule calibration.night_dark_exposures applies at generation)
    extra = light_dark_lengths(view, rig, lights, quota=qlist)
    buckets: dict[tuple, dict] = {}

    def bucket(src: dict) -> dict:
        key = (src["exp_s"], src["gain"], src["offset"],
               round(src["settemp"] * 2) / 2, src["xbin"], src.get("readout"))
        for k, b in buckets.items():
            if (_same_exp(k[0], key[0]) and k[1:3] == key[1:3]
                    and abs(k[3] - key[3]) < TEMP_TOL_C and k[4:] == key[4:]):
                return b
        return buckets.setdefault(key, {
            "exp_s": src["exp_s"], "gain": src["gain"], "offset": src["offset"],
            "settemp": src["settemp"], "xbin": src["xbin"],
            "readout": src.get("readout"), "lights": 0,
            "nights": set(), "targets": set(), "readouts": set(),
            "assumed": False, "readout_assumed": 0,
            "planned": [], "quota_list": False})

    for e in qlist:
        bucket({"exp_s": round(float(e), 1), "gain": ep["gain"],
                "offset": ep["offset"], "settemp": ep["setpoint"],
                "xbin": 1, "readout": ep["readout"]})["quota_list"] = True
    for e in extra:
        b = bucket({"exp_s": round(float(e), 1), "gain": ep["gain"],
                    "offset": ep["offset"], "settemp": ep["setpoint"],
                    "xbin": 1, "readout": ep["readout"]})
        b["quota_list"] = True
        b["from_lights"] = True
    for li in lights:
        b = bucket(li)
        b["lights"] += 1
        b["nights"].add(li["night"])
        b["targets"].add(li["target"])
        if li.get("readout"):
            b["readouts"].add(str(li["readout"]))
        b["assumed"] = b["assumed"] or li["assumed"]
        b["readout_assumed"] += 1 if li.get("readout_assumed") else 0
    for p in planned:
        b = bucket(p)
        if p["label"] not in b["planned"]:
            b["planned"].append(p["label"])

    items, missing = [], []
    for b in sorted(buckets.values(), key=lambda x: (x["exp_s"], x["gain"])):
        on = _on_epoch(b, ep)
        try:
            q = dark_quota(view, rig, b["exp_s"], store=store, gain=b["gain"],
                           offset=b["offset"], setpoint=b["settemp"],
                           readout=b["readout"] or "")
        except Exception:  # noqa: BLE001 - one bad bucket must not hide the rest
            logger.warning("dark count failed for %s", b, exc_info=True)
            q = {"have": 0, "quota": int(getattr(config, "dark_target_count", 30))}
            q["need"] = q["quota"]
        used = bool(b["lights"] or b["planned"])
        auto = on and b["quota_list"]
        fix = None
        if used and on and not b["quota_list"]:
            missing.append(b["exp_s"])
        elif used and not on:
            fix = (f"off-epoch lights (gain {b['gain']}, offset {b['offset']}, "
                   f"{_fmt(b['settemp'])} C, bin {b['xbin']}{_ro_txt(b['readout'])}"
                   f"): the night quota only fills gain {ep['gain']} / offset "
                   f"{ep['offset']} / {_fmt(ep['setpoint'])} C bin 1"
                   f"{_ro_txt(ep['readout'])}, so shoot these by hand or "
                   "drop the lights")
        owed = q["need"] if (used or b["quota_list"]) else 0
        nd = _dark_newest(store, b)
        label = (f"{_fmt(b['exp_s'])} s (gain {b['gain']}, offset {b['offset']}, "
                 f"{_fmt(b['settemp'])} C" + ("" if b["xbin"] == 1
                                              else f", bin {b['xbin']}")
                 + _ro_txt(b["readout"]) + ")")
        items.append({
            "exp_s": b["exp_s"], "gain": b["gain"], "offset": b["offset"],
            "settemp": b["settemp"], "xbin": b["xbin"],
            "readout": b["readout"],
            "lights_readout_assumed": b["readout_assumed"],
            "readouts": sorted(b["readouts"]),
            "have": q["have"], "need": q["quota"], "owed": owed,
            "lights": b["lights"], "nights": sorted(b["nights"]),
            "targets": sorted(b["targets"]), "planned": b["planned"],
            "assumed_epoch": b["assumed"], "on_epoch": on,
            "in_quota_list": b["quota_list"] and on, "auto_fill": auto,
            "from_lights": bool(b.get("from_lights")),
            "newest": nd["newest"], "age_days": _age(nd["newest"], datetime.now()),
            "ccd_temp_c": nd["ccd_temp_c"],
            "ccd_temp_max_off_c": nd["ccd_temp_max_off_c"],
            "at_setpoint": abs(b["settemp"] - ep["setpoint"]) < TEMP_TOL_C,
            "fix": fix, "label": label})

    fixes = []
    if missing:
        env, key = _quota_env(rig)
        cur = _config_list(config, rig)
        want = sorted({*cur, *missing})
        line = f"{env}={','.join(_fmt(x) for x in want)}"
        for it in items:
            if it["on_epoch"] and any(_same_exp(it["exp_s"], m) for m in missing):
                it["fix"] = (f"lights at {_fmt(it['exp_s'])} s but "
                             f"{_fmt(it['exp_s'])} s not in {key}: set {line}")
        fixes.append({
            "key": key, "env": env, "current": [float(x) for x in cur],
            "add": sorted(float(m) for m in missing), "set": line,
            "text": (f"lights at {', '.join(_fmt(m) + ' s' for m in sorted(missing))}"
                     f" but not in {key} ({','.join(_fmt(x) for x in cur) or 'empty'})"
                     f": set {line} on the scope so the night quota fills them")})
    return items, fixes


def _flat_sessions(config, view, rig, store, ep) -> dict:
    """{filter: {date: count}} of QA-passed flats; the header scan when the
    rig has no QA store yet (or QA is off)."""
    from photonscript.scheduler import calibration_qa as cq
    if cq.mode(config) != "off" and store["frames"]:
        return cq.passed_flat_sessions(view, rig, gain=ep["gain"],
                                       offset=ep["offset"], store=store)
    from photonscript.scheduler.calibration import calibration_health
    h = calibration_health(view).get("FLAT") or {}
    if rig != "rc16":
        return ({"OSC": {h["latest"]: int(h.get("count_latest") or 0)}}
                if h.get("latest") else {})
    return {f: {b["date"]: int(b.get("count") or 0)}
            for f, b in (h.get("by_bucket") or {}).items() if b.get("date")}


def _bias_sessions(config, view, rig, store, ep) -> dict:
    from photonscript.scheduler import calibration_qa as cq
    if cq.mode(config) != "off" and store["frames"]:
        return cq.passed_bias_sessions(view, rig, gain=ep["gain"],
                                       offset=ep["offset"],
                                       readout=ep["readout"] or "", store=store)
    from photonscript.scheduler.calibration import calibration_health
    h = calibration_health(view).get("BIAS") or {}
    return {h["latest"]: int(h.get("count_latest") or 0)} if h.get("latest") else {}


def _age(date: str | None, now: datetime) -> int | None:
    if not date:
        return None
    try:
        return (now.date() - datetime.strptime(date, "%Y-%m-%d").date()).days
    except ValueError:
        return None


def _flat_items(config, rig, sessions, lights, planned, now) -> list[dict]:
    from photonscript.scheduler.calibration import STALE_DAYS
    reset = flats_reset_date(config, rig)   # PS-160
    need = int(getattr(config, "flat_count", 15) if rig == "rc16"
               else getattr(config, "piggyback_flat_count", 25))
    stale_after = STALE_DAYS["FLAT"]
    filters: dict[str, set] = {}
    for li in lights:
        filters.setdefault(li["filter"], set()).add(li["night"])
    for p in planned:
        filters.setdefault(p["filter"], set())
    if rig != "rc16" and not filters:
        filters["OSC"] = set()
    out = []
    for f in sorted(filters):
        by_date = sessions.get(f) or {}
        last = max(by_date) if by_date else None
        count = by_date.get(last, 0) if last else 0
        age = _age(last, now)
        since = sorted(n for n in filters[f] if last is None or n > last)
        reasons = []
        if last is None:
            reasons.append("no flats")
        else:
            if age is not None and age > stale_after:
                reasons.append(f"{age} d old (stale after {stale_after} d)")
            if count < need:
                reasons.append(f"newest set has {count} of {need}")
            if reset and last < reset:
                reasons.append(f"taken before the optics change on {reset} "
                               "(calibration_flats_reset)")
        out.append({
            "filter": f, "last": last, "count": count, "need": need,
            "age_days": age, "stale_after_days": stale_after,
            "stale": (last is None or (age is not None and age > stale_after)
                      or bool(reset and last < reset)),
            "optics_reset": reset,
            "lights_since": since, "light_nights": sorted(filters[f]),
            "owed": bool(reasons), "reasons": reasons,
            "label": f"flats {f}"})
    return out


def _bias_item(config, rig, sessions, now, readout=None) -> dict:
    from photonscript.scheduler.calibration import STALE_DAYS
    from photonscript.scheduler.calibration_plan import BIAS_COUNT
    last = max(sessions) if sessions else None
    count = sessions.get(last, 0) if last else 0
    age = _age(last, now)
    reasons = []
    if last is None:
        reasons.append("no bias")
    else:
        if count < BIAS_COUNT:
            reasons.append(f"newest set has {count} of {BIAS_COUNT}")
        if age is not None and age > STALE_DAYS["BIAS"]:
            reasons.append(f"{age} d old (stale after {STALE_DAYS['BIAS']} d)")
    return {"last": last, "count": count, "need": BIAS_COUNT, "age_days": age,
            "stale_after_days": STALE_DAYS["BIAS"], "owed": bool(reasons),
            "reasons": reasons, "readout": readout,
            "label": "bias" + (f" ({readout})" if readout else "")}


def _night_items(lights, darks, flats, bias) -> list[dict]:
    """Per night with lights: what is missing (none at all) and what is short
    (below quota / stale)."""
    nights: dict[str, dict] = {}
    for li in lights:
        n = nights.setdefault(li["night"], {"night": li["night"], "lights": 0,
                                            "targets": set(), "keys": set(),
                                            "filters": set()})
        n["lights"] += 1
        n["targets"].add(li["target"])
        n["filters"].add(li["filter"])
    out = []
    for night in sorted(nights, reverse=True):
        n = nights[night]
        missing, short = [], []
        for d in darks:
            if night not in d["nights"]:
                continue
            if d["have"] == 0:
                missing.append(f"darks {d['label']}")
            elif d["owed"]:
                short.append(f"darks {_fmt(d['exp_s'])} s ({d['have']} of {d['need']})")
        for f in flats:
            if f["filter"] not in n["filters"]:
                continue
            if f["last"] is None:
                missing.append(f"flats {f['filter']}")
            elif f["owed"]:
                short.append(f"flats {f['filter']} ({'; '.join(f['reasons'])})")
        if bias["last"] is None:
            missing.append("bias")
        elif bias["owed"]:
            short.append("bias (" + "; ".join(bias["reasons"]) + ")")
        out.append({"night": night, "lights": n["lights"],
                    "targets": sorted(n["targets"]),
                    "uncalibrated": bool(missing), "missing": missing,
                    "short": short})
    return out


def _constraints(config, rig: str) -> list[str]:
    from photonscript.scheduler import calibration_qa as cq
    day = (cq.daytime_state(config, rig) or {}).get("status") or "untested"
    auto = bool(getattr(config, "calibration_autofill", False))
    return [CONSTRAINT_DARKS,
            CONSTRAINT_DAYTIME + f" Daytime capture state for this rig: {day}; "
            f"calibration_autofill {'on' if auto else 'off'}.",
            CONSTRAINT_FLATS]


def _items_text(rig_out: dict) -> list[str]:
    """The short owed list the dashboard card shows."""
    lines = []
    for d in rig_out["darks"]:
        if not d["owed"] and not d["fix"]:
            continue
        when = (f"lights {', '.join(d['nights'][-3:])}" if d["nights"]
                else ("planned tonight" if d["planned"] else "quota list"))
        txt = (f"Darks {d['label']}: {d['have']} of {d['need']}"
               + (f", owe {d['owed']}" if d["owed"] else "") + f" ({when})")
        if d["fix"]:
            txt += f". Fix: {d['fix']}"
        elif not d["auto_fill"] and d["owed"]:
            txt += ". Not filled automatically"
        elif d.get("from_lights") and d["owed"]:
            txt += ". Filled at night from the lights (calibration_darks_follow_lights)"
        lines.append(txt)
    for f in rig_out["flats"]:
        if f["owed"]:
            txt = f"Flats {f['filter']}: " + "; ".join(f["reasons"])
            if f["lights_since"]:
                txt += f" (lights since: {len(f['lights_since'])} night(s))"
            lines.append(txt)
    b = rig_out["bias"]
    if b["owed"]:
        lines.append(("Bias" + (f" ({b['readout']})" if b.get("readout") else "")
                      + ": ") + "; ".join(b["reasons"]))
    return lines


def _readout_note(rig_out: dict) -> str | None:
    """PS-128: a note (not an owed item) when darks / bias were counted at
    an assumed readout mode."""
    n = rig_out.get("frames_readout_assumed") or 0
    if not n:
        return None
    return (f"Readout assumed {rig_out['epoch'].get('readout')} for {n} dark / bias "
            "frame(s) with no readout recorded (run calibration-qa --backfill "
            "--dry-run to read READOUTM, nothing moves)")


def _readout_assumed_frames(store: dict, ep: dict) -> int:
    """PS-128: QA records (darks / bias of the rig's gain and offset) with no
    readout recorded: counted as the rig's readout mode (assumed)."""
    if not ep.get("readout"):
        return 0
    return sum(1 for r in store["frames"].values()
               if r.get("type") in ("DARK", "BIAS") and not r.get("readout")
               and r.get("gain") == ep["gain"] and r.get("offset") == ep["offset"])


def _plan(config, rig: str, darks: list, flats: list, planned: list) -> dict:
    """PS-160: what tonight's sequences will fill from this coverage: the
    dark lengths with need (config list + lengths from the lights, roof
    closed only, behind the cooler gate when calibration_darks_gated), and
    the flats a dawn shoots (RC16: tonight's filters plus at most
    calibration_dawn_flat_extra_max owed filters the lights used, most owed
    first; Piggy-600: one OSC set every safe dawn, companion)."""
    from photonscript.scheduler.cooler_gate import gate_mode
    gated = bool(getattr(config, "calibration_darks_gated", True))
    mode = gate_mode(config)
    dark_sets = [{"exp_s": d["exp_s"], "owed": d["owed"],
                  "from_lights": d.get("from_lights", False)}
                 for d in darks if d["auto_fill"] and d["owed"]]
    not_auto = [d["label"] for d in darks if d["owed"] and not d["auto_fill"]]
    if rig == "rc16":
        tonight = sorted({p["filter"] for p in planned})
        cap = max(0, int(getattr(config, "calibration_dawn_flat_extra_max", 3) or 0))
        owed = [f for f in flats if f["owed"] and f["filter"] not in tonight
                and (f["light_nights"]
                     or not getattr(config, "calibration_flats_as_used", True))]
        owed.sort(key=lambda f: (f["last"] is not None, -(f["age_days"] or 0)))
        extra = [f["filter"] for f in owed[:cap]]
        later = [f["filter"] for f in owed[cap:]]
        flat_txt = ("dawn flats: " + (", ".join(tonight + extra) or "none")
                    + (f" (owed later: {', '.join(later)})" if later else ""))
        if not getattr(config, "auto_stale_flats", True):
            extra, later = [], [f["filter"] for f in owed]
            flat_txt = ("dawn flats: tonight's filters only (auto_stale_flats "
                        "off)" + (f"; owed: {', '.join(later)}" if later else ""))
    else:
        tonight = ["OSC"] if planned else []
        extra, later = [], []
        flat_txt = ("dawn flats: one OSC set every safe dawn (companion)"
                    + ("; owed now" if any(f["owed"] for f in flats) else ""))
    dark_txt = ("night darks (roof closed): "
                + (", ".join(f"{_fmt(x['exp_s'])} s x {x['owed']}"
                             + (" (from lights)" if x["from_lights"] else "")
                             for x in dark_sets) or "none owed")
                + ("; only at the setpoint (cooler gate, "
                   f"{mode} mode)" if gated and mode != "off" else
                   "; not gated on the setpoint"))
    return {"dark_sets": dark_sets, "darks_not_automatic": not_auto,
            "darks_gated": gated and mode != "off", "gate_mode": mode,
            "dawn_flats_tonight": tonight, "dawn_flats_extra": extra,
            "flats_owed_later": later, "text": [dark_txt, flat_txt]}


def morning_note(config, rep: dict | None = None) -> str | None:
    """PS-160: one line for the dawn "Night complete" push: what each rig
    still owes, or None when nothing is owed. Never raises."""
    try:
        rep = rep or owed_report(config)
    except Exception as e:  # noqa: BLE001
        logger.debug("calibration morning note unavailable: %s", e)
        return None
    parts = []
    for r in rep.get("rigs") or []:
        bits = []
        for d in r["darks"]:
            if d["owed"] and (d["lights"] or d["planned"]):
                bits.append(f"darks {_fmt(d['exp_s'])} s {d['have']}/{d['need']}")
        for f in r["flats"]:
            if f["owed"]:
                bits.append(f"flats {f['filter']} "
                            + (f"{f['age_days']} d" if f["last"] else "none"))
        if r["bias"]["owed"]:
            bits.append("bias")
        if bits:
            more = f" +{len(bits) - 4}" if len(bits) > 4 else ""
            parts.append(f"{r['name']}: " + ", ".join(bits[:4]) + more)
    return ("Calibration owed: " + "; ".join(parts)) if parts else None


def owed_report(config, rig: str | None = None, *, projects=None,
                now: datetime | None = None) -> dict:
    from photonscript.scheduler import calibration_qa as cq
    from photonscript.scheduler.calibration import dark_epoch
    from photonscript.scheduler.calibration_plan import load_projects
    from photonscript.shared.rigs import rig_ids, rig_label
    now = now or datetime.now()
    days = max(1, int(getattr(config, "calibration_owed_lookback_days", 60) or 60))
    if projects is None:
        projects = load_projects(config)
    projects = [p for p in projects if getattr(p, "active", True)]
    out = {"generated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
           "lookback_days": days, "qa_mode": cq.mode(config),
           "dark_target_count": int(getattr(config, "dark_target_count", 30)),
           "active_goals": sorted(p.target.name for p in projects), "rigs": []}
    for rg in ([rig] if rig else rig_ids(config)):
        view = cq.rig_view(config, rg)
        ep = dark_epoch(config, rg)
        store = cq.load_store(config, rg)
        lights = collect_lights(config, rg, projects, days=days, now=now)
        planned = planned_lights(config, rg)
        darks, fixes = _dark_items(config, view, rg, store, lights, planned)
        flats = _flat_items(config, rg, _flat_sessions(config, view, rg, store, ep),
                            lights, planned, now)
        bias = _bias_item(config, rg, _bias_sessions(config, view, rg, store, ep), now,
                          readout=ep["readout"])
        nights = _night_items(lights, darks, flats, bias)
        r = {"rig": rg, "name": rig_label(config, rg),
             "epoch": {**ep, "binning": 1},
             "counted": "QA-passed" if (cq.mode(config) != "off" and store["frames"])
                        else "header scan (no QA store yet)",
             "lights": len(lights),
             "lights_assumed_epoch": sum(1 for li in lights if li["assumed"]),
             "lights_readout_assumed": sum(1 for li in lights
                                           if li.get("readout_assumed")),
             "frames_readout_assumed": (_readout_assumed_frames(store, ep)
                                        if cq.mode(config) != "off" else 0),
             "darks": darks, "flats": flats, "bias": bias,
             "config_fixes": fixes, "nights": nights,
             "constraints": _constraints(config, rg)}
        r["summary"] = {
            "dark_sets_owed": sum(1 for d in darks if d["owed"]),
            "dark_frames_owed": sum(d["owed"] for d in darks),
            "flats_owed": sum(1 for f in flats if f["owed"]),
            "bias_owed": bias["owed"],
            "config_fixes": len(fixes),
            "uncalibrated_nights": sum(1 for n in nights if n["uncalibrated"])}
        r["items"] = _items_text(r)
        r["readout_note"] = _readout_note(r)
        try:   # PS-160
            r["plan"] = _plan(config, rg, darks, flats, planned)
        except Exception:  # noqa: BLE001 - the report stands without it
            logger.warning("coverage plan failed for %s", rg, exc_info=True)
            r["plan"] = None
        out["rigs"].append(r)
    out["total_items"] = sum(len(r["items"]) for r in out["rigs"])
    return out


def format_report(rep: dict) -> str:
    lines = [f"Calibration owed ({rep['generated']}, lights of active goals, last "
             f"{rep['lookback_days']} nights, QA mode {rep['qa_mode']})"]
    for r in rep["rigs"]:
        ep, s = r["epoch"], r["summary"]
        lines.append("")
        lines.append(f"{r['name']} ({r['rig']}): gain {ep['gain']} offset {ep['offset']} "
                     f"{_fmt(ep['setpoint'])} C"
                     + (f" {ep['readout']}" if ep.get("readout") else "")
                     + f"; {r['lights']} lights; counting {r['counted']}")
        lines.append(f"  owed: {s['dark_sets_owed']} dark sets ({s['dark_frames_owed']} "
                     f"frames), {s['flats_owed']} flat filters, bias "
                     f"{'yes' if s['bias_owed'] else 'no'}, {s['config_fixes']} config "
                     f"fix(es), {s['uncalibrated_nights']} uncalibrated night(s)")
        for t in r["items"]:
            lines.append(f"  - {t}")
        for t in (r.get("plan") or {}).get("text") or []:
            lines.append(f"  plan: {t}")
        if r.get("readout_note"):
            lines.append(f"  note: {r['readout_note']}")
        for n in r["nights"]:
            if n["uncalibrated"]:
                lines.append(f"  night {n['night']}: {n['lights']} lights uncalibrated "
                             f"(missing {', '.join(n['missing'])})")
        for c in r["constraints"]:
            lines.append(f"  * {c}")
    return "\n".join(lines)
