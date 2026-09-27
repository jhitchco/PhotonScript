"""RC16 focus model (PS-76): a temperature-compensated focus lookup table and
measured filter offsets, learned from NINA's own autofocus reports.

Why this exists
---------------
focus_seeds.json holds six July records, and the post-night harvester that was
meant to grow it reads FOCPOS from sharp subs (HFR <= 4 px). Only 28 of 779
RC16 subs on record ever met that bar, and since PS-65 a narrowband sub's
FOCPOS is just "L autofocus + configured offset", so harvesting it would only
echo the configured offset back. The table has never grown past its July seed.

The clean signal is the autofocus run itself: every NINA AF writes a JSON
report with the filter, focuser temperature, best-focus position and the
curve fit quality. This module turns those reports into points and fits

    position = a[filter] + b * temperature

with ONE temperature slope b shared by every filter (the tube's thermal
expansion does not depend on the filter) and a per-filter intercept a[filter].
The filter offset is then a[filter] - a[ref] (ref = config.autofocus_filter),
measured at any temperature, and every AF (whatever its filter) helps pin the
slope. Robust: iterated MAD-sigma clipping drops a bad AF before it bends the
fit, and reports with a poor curve (R^2 below af_min_r2, too few points) never
enter at all.

What uses it
------------
- focus_seeds.seed_for(): when the model is at least "med" confidence for a
  filter, its prediction is the AF starting point (AF still always runs).
- runs.py backfill: ingest_af_reports() after each night (a no-op until
  config.nina_autofocus_reports_dir is set).
- GET /api/focus: summary() read-out (rc16.model).

Files (config.data_dir): focus_af_points.json (ingested AF points, append-only
with de-dup) and focus_model.json (the fitted model + lookup table, rebuilt on
every ingest). Nothing here talks to NINA or moves hardware.

Which rig (PS-76 follow-up)
---------------------------
Both NINA instances run as one Windows user, so both write AF reports into the
same %LOCALAPPDATA%/NINA/AutoFocus folder. Only RC16 reports may enter the RC16
model: classify_report() sorts each report by rig. With the matchers empty the
rule is: an RC16 report names an RC16 filter-wheel filter (nina_filter_names)
and lands inside the RC16 EAF range (focus_seeds clamp, 4000 to 7000);
anything else is the Piggy-600 (one-shot colour, no filter wheel, its own EAF
near piggyback_focus_seed) when its filter is empty or foreign, or its
position is nearer the Piggy-600 seed; otherwise it is left out. When NINA's
reports carry something better (camera, focuser or profile names, the file
name), focus_model_rc16_match / focus_model_piggyback_match take over, e.g.
"Filter=L|R|G|B|H|O|S;position:4000-7000" or "any~AP26MC". Piggy-600 reports
feed a separate read-only model (piggyback_focus_af_points.json /
piggyback_focus_model.json), shown in GET /api/focus; nothing seeds from it.
"""
from __future__ import annotations

import json
import logging
import math
import re
from pathlib import Path

logger = logging.getLogger(__name__)

POINTS_FILE = "focus_af_points.json"
MODEL_FILE = "focus_model.json"
PB_POINTS_FILE = "piggyback_focus_af_points.json"
PB_MODEL_FILE = "piggyback_focus_model.json"
OSC_FILTER = "OSC"   # the Piggy-600's single channel (reports carry no filter)

# Fit guards
_MIN_TEMP_SPAN_C = 3.0     # within-filter temperature spread needed for a slope
_MIN_SIGMA_STEPS = 5.0     # floor on the robust sigma (EAF steps) for clipping
_CLIP_SIGMA = 3.0
_EXTRAP_MARGIN_C = 3.0     # predictions this far outside the fitted temps stay "high"


# --------------------------------------------------------------------------
# Report -> point
# --------------------------------------------------------------------------

def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _duration_s(v) -> float | None:
    """NINA writes Duration as a .NET TimeSpan string ('00:03:12.4410000')."""
    if v is None:
        return None
    if (x := _num(v)) is not None:
        return x
    m = re.match(r"^(?:(\d+)\.)?(\d+):(\d+):(\d+(?:\.\d+)?)$", str(v).strip())
    if not m:
        return None
    d, h, mi, s = m.groups()
    return (int(d or 0) * 86400 + int(h) * 3600 + int(mi) * 60 + float(s))


def _pos(fp) -> float | None:
    return _num(fp.get("Position")) if isinstance(fp, dict) else None


def norm_filter(name, config=None) -> str:
    """NINA profile filter name ('H') -> our filter class ('Ha'). Unknown
    names pass through unchanged."""
    name = str(name or "").strip()
    if config is not None:
        try:
            rev = config.reverse_filter_map()
            if name in rev:
                return rev[name]
        except Exception as e:  # noqa: BLE001
            logger.debug("focus_model: filter map unavailable: %s", e)
    return name


def point_from_report(report: dict, config=None, min_r2: float = 0.7,
                      min_points: int = 5) -> tuple[dict | None, str]:
    """One NINA AF report -> (point, "ok") or (None, reason it was rejected).

    A point is {filter, position, temp, r2, hfr, points, initial, duration_s,
    time, source, file}. Rejected: no best-focus position, fewer than
    min_points measure points, a parsable R^2 below min_r2. A report with no
    temperature is kept (temp=None) for the AF-run stats, but fit() ignores
    it because the model needs a temperature."""
    from photonscript.scheduler.focus_reports import _best_r2
    if not isinstance(report, dict):
        return None, "not a report"
    pos = _pos(report.get("CalculatedFocusPoint"))
    if pos is None or pos <= 0:
        return None, "no calculated focus position"
    mp = report.get("MeasurePoints")
    npts = len(mp) if isinstance(mp, list) else None
    if npts is not None and npts < min_points:
        return None, f"only {npts} measure points"
    r2 = _best_r2(report)
    if r2 is not None and r2 < min_r2:
        return None, f"R^2 {r2:.2f} < {min_r2:.2f}"
    cfp = report.get("CalculatedFocusPoint") or {}
    return ({
        "filter": norm_filter(report.get("Filter")
                              or report.get("AutoFocusFilter"), config),
        "position": round(pos),
        "temp": (round(t, 2) if (t := _num(report.get("Temperature")))
                 is not None else None),
        "r2": (round(r2, 4) if r2 is not None else None),
        "hfr": _num(cfp.get("Value")),
        "points": npts,
        "initial": (round(p) if (p := _pos(report.get("InitialFocusPoint")))
                    is not None else None),
        "duration_s": _duration_s(report.get("Duration")),
        "time": str(report.get("Timestamp") or report.get("Time") or ""),
        "source": "nina_af_report",
        "file": report.get("_file"),
    }, "ok")


# --------------------------------------------------------------------------
# Which rig wrote the report
# --------------------------------------------------------------------------

def _report_field(report: dict, name: str):
    """Field for a matcher clause: 'filter', 'position', 'temp', 'file',
    'any' (the whole report as text), or any dotted report key
    (e.g. CalculatedFocusPoint.Position, AutoFocuserName)."""
    low = name.strip().lower()
    if low == "any":
        return json.dumps(report, default=str)
    if low == "filter":
        return report.get("Filter") or report.get("AutoFocusFilter") or ""
    if low == "position":
        return _pos(report.get("CalculatedFocusPoint"))
    if low in ("temp", "temperature"):
        return report.get("Temperature")
    if low == "file":
        return report.get("_file") or ""
    cur = report
    for part in name.strip().split("."):
        if not isinstance(cur, dict):
            return None
        hit = next((k for k in cur if str(k).lower() == part.lower()), None)
        if hit is None:
            return None
        cur = cur[hit]
    return cur


_CLAUSE = re.compile(r"^\s*([A-Za-z_][\w.]*)\s*([~=:])\s*(.*?)\s*$")


def match_report(report: dict, spec: str) -> bool:
    """True when every ';'-separated clause of spec holds:
      field~a|b     substring (case-insensitive), any alternative
      field=a|b     exact (case-insensitive), any alternative
      field:lo-hi   number in range (either end may be empty)
    An unparseable clause never matches (fail closed)."""
    clauses = [c for c in (spec or "").split(";") if c.strip()]
    if not clauses:
        return False
    for c in clauses:
        m = _CLAUSE.match(c)
        if not m:
            return False
        field, op, arg = m.groups()
        v = _report_field(report, field)
        if op == ":":
            x = _num(v)
            lo, _, hi = arg.partition("-")
            if x is None:
                return False
            if lo.strip() and x < float(lo):
                return False
            if hi.strip() and x > float(hi):
                return False
            continue
        text = "" if v is None else str(v).strip().lower()
        alts = [a.strip().lower() for a in arg.split("|")]
        if op == "=" and text not in alts:
            return False
        if op == "~" and not any(a and a in text for a in alts):
            return False
    return True


def _rc16_filter_names(config) -> set[str]:
    names = {"L", "R", "G", "B", "Ha", "OIII", "SII", "H", "O", "S"}
    if config is not None:
        try:
            fmap = config.filter_name_map()
            names |= set(fmap) | set(fmap.values())
        except Exception as e:  # noqa: BLE001
            logger.debug("focus_model: filter map unavailable: %s", e)
    return {n.strip().lower() for n in names if n and n.strip()}


def _piggy_ref(config) -> float | None:
    try:
        from photonscript.scheduler.piggyback_focus import current_seed
        v = current_seed(config)
    except Exception:  # noqa: BLE001
        v = getattr(config, "piggyback_focus_seed", 0)
    return float(v) if v and float(v) > 0 else None


def classify_report(report: dict, config=None) -> str | None:
    """'rc16', 'piggyback' or None (unknown: left out of both models)."""
    rc = (getattr(config, "focus_model_rc16_match", "") or "").strip()
    pb = (getattr(config, "focus_model_piggyback_match", "") or "").strip()
    if rc or pb:
        if rc and match_report(report, rc):
            return "rc16"
        if pb and match_report(report, pb):
            return "piggyback"
        if rc and pb:
            return None
        return "piggyback" if rc else "rc16"
    from photonscript.scheduler.focus_seeds import _FOCPOS_MAX, _FOCPOS_MIN
    filt = str(_report_field(report, "filter") or "").strip().lower()
    pos = _report_field(report, "position")
    rc_filter = bool(filt) and filt in _rc16_filter_names(config)
    rc_range = pos is not None and _FOCPOS_MIN <= pos <= _FOCPOS_MAX
    if rc_filter and rc_range:
        return "rc16"
    if not rc_filter:
        return "piggyback"
    ref = _piggy_ref(config)
    if pos is not None and ref is not None:
        mid = (_FOCPOS_MIN + _FOCPOS_MAX) / 2
        if abs(pos - ref) < abs(pos - mid):
            return "piggyback"
    return None


def _point_rig(p: dict, config=None) -> str | None:
    """Rig of a stored point (points stored before the rig filter carry no
    tag: classify them from their filter and position)."""
    if p.get("rig"):
        return p["rig"]
    return classify_report({"Filter": p.get("filter"),
                            "CalculatedFocusPoint": {"Position": p.get("position")},
                            "_file": p.get("file")}, config)


def _key(p: dict) -> tuple:
    return (p.get("time"), p.get("filter"), p.get("position"))


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

def _points_path(config, rig: str = "rc16") -> Path:
    return Path(config.data_dir) / (PB_POINTS_FILE if rig == "piggyback"
                                    else POINTS_FILE)


def _model_path(config, rig: str = "rc16") -> Path:
    return Path(config.data_dir) / (PB_MODEL_FILE if rig == "piggyback"
                                    else MODEL_FILE)


def load_points(config, rig: str = "rc16") -> list[dict]:
    p = _points_path(config, rig)
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("focus_model: could not read %s: %s", p, e)
    return []


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1), encoding="utf-8")
    tmp.replace(path)


def ingest_af_reports(config, reports_dir: str | None = None) -> dict:
    """Read every NINA AF report in the reports dir, sort each by rig, add
    the good ones not already stored, then refit and persist the models.
    Only RC16 reports reach the RC16 model; Piggy-600 reports go to their
    own store (focus_model_piggyback, default on). Never raises.

    Returns {enabled, read, added, rejected, total, other_rig, unclassified,
    piggyback: {added, total}}."""
    rdir = (reports_dir if reports_dir is not None else
            (getattr(config, "nina_autofocus_reports_dir", "") or "")).strip()
    if not rdir:
        return {"enabled": False, "read": 0, "added": 0, "rejected": 0,
                "total": len(load_points(config))}
    try:
        from photonscript.scheduler.focus_reports import load_reports
        reports = load_reports(rdir)
        min_r2 = float(getattr(config, "af_min_r2", 0.7))
        min_pts = int(getattr(config, "focus_model_min_af_points", 5))
        want_pb = bool(getattr(config, "focus_model_piggyback", True))
        stores = {"rc16": [], "piggyback": load_points(config, "piggyback")}
        moved = 0
        # points stored before the rig filter: re-sort them once
        for p in load_points(config, "rc16"):
            rig = _point_rig(p, config)
            if rig == "rc16":
                stores["rc16"].append(dict(p, rig="rc16"))
            elif rig == "piggyback":
                moved += 1
                stores["piggyback"].append(dict(p, rig="piggyback",
                                                filter=OSC_FILTER))
            else:
                moved += 1
        seen = {r: {_key(p) for p in pts} for r, pts in stores.items()}
        added = {"rc16": 0, "piggyback": 0}
        rejected = other = unknown = 0
        for rep in reports:
            rig = classify_report(rep, config)
            if rig is None:
                unknown += 1
                continue
            if rig != "rc16":
                other += 1
                if not want_pb:
                    continue
            pt, _why = point_from_report(rep, config, min_r2, min_pts)
            if pt is None:
                if rig == "rc16":
                    rejected += 1
                continue
            pt["rig"] = rig
            if rig == "piggyback":
                pt["filter"] = OSC_FILTER
            if _key(pt) in seen[rig]:
                continue
            seen[rig].add(_key(pt))
            stores[rig].append(pt)
            added[rig] += 1
        for rig, pts in stores.items():
            pts.sort(key=lambda p: str(p.get("time") or ""))
            if added[rig] or moved:
                _write_json(_points_path(config, rig), pts)
            _write_json(_model_path(config, rig),
                        fit(pts, ref_filter=(OSC_FILTER if rig == "piggyback"
                                             else _ref_filter(config))))
        logger.info("focus_model: %d AF report(s) read, RC16 %d added / %d "
                    "rejected / %d stored, Piggy-600 %d added, %d other-rig, "
                    "%d unclassified", len(reports), added["rc16"], rejected,
                    len(stores["rc16"]), added["piggyback"], other, unknown)
        return {"enabled": True, "read": len(reports), "added": added["rc16"],
                "rejected": rejected, "total": len(stores["rc16"]),
                "other_rig": other, "unclassified": unknown,
                "piggyback": {"added": added["piggyback"],
                              "total": len(stores["piggyback"])}}
    except Exception as e:  # noqa: BLE001
        logger.warning("focus_model: ingest failed: %s", e)
        return {"enabled": True, "read": 0, "added": 0, "rejected": 0,
                "total": 0, "error": str(e)}


def _ref_filter(config) -> str:
    return (getattr(config, "autofocus_filter", "") or "L").strip() or "L"


# --------------------------------------------------------------------------
# Fit
# --------------------------------------------------------------------------

def _median(xs):
    s = sorted(xs)
    n = len(s)
    if not n:
        return None
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


def _robust_sigma(res):
    if len(res) < 2:
        return None
    med = _median(res)
    return 1.4826 * _median([abs(r - med) for r in res])


def _pooled(pts):
    """Common-slope, per-filter-intercept least squares on (filter, T, P).
    Returns (slope or None, {filter: intercept}, within-filter Sxx)."""
    groups: dict[str, list[tuple[float, float]]] = {}
    for p in pts:
        groups.setdefault(p["filter"], []).append((p["temp"], p["position"]))
    sxx = sxy = 0.0
    means = {}
    for f, tp in groups.items():
        tb = sum(t for t, _ in tp) / len(tp)
        pb = sum(q for _, q in tp) / len(tp)
        means[f] = (tb, pb)
        sxx += sum((t - tb) ** 2 for t, _ in tp)
        sxy += sum((t - tb) * (q - pb) for t, q in tp)
    # need a real within-filter temperature spread to trust a slope
    spans = [max(t for t, _ in tp) - min(t for t, _ in tp)
             for tp in groups.values() if len(tp) >= 2]
    slope = (sxy / sxx) if (sxx > 0 and spans and max(spans) >= _MIN_TEMP_SPAN_C
                            and sum(len(v) for v in groups.values())
                            - len(groups) >= 2) else None
    b = slope or 0.0
    inter = {f: pb - b * tb for f, (tb, pb) in means.items()}
    return slope, inter, sxx


def fit(points: list[dict], ref_filter: str = "L") -> dict:
    """Fit position = a[filter] + b*temp with robust outlier rejection.

    Points without a temperature or filter are ignored. With too little
    temperature spread the slope is None and each filter's intercept is its
    mean position (a pure lookup, no temperature compensation)."""
    use = [p for p in points
           if p.get("filter") and _num(p.get("position")) is not None
           and _num(p.get("temp")) is not None]
    use = [dict(p, position=float(p["position"]), temp=float(p["temp"]))
           for p in use]
    rejected: list[dict] = []
    slope, inter, sxx = _pooled(use) if use else (None, {}, 0.0)
    sigma = None
    # Remove the single worst point per pass and refit (one at a time: a big
    # outlier drags its own filter's intercept, so a bulk cut would also throw
    # away that filter's good points). At most a third of the points go.
    max_drop = len(use) // 3
    while len(use) >= 4 and len(rejected) < max_drop:
        res = [p["position"] - (inter[p["filter"]] + (slope or 0.0) * p["temp"])
               for p in use]
        sigma = max(_robust_sigma(res) or 0.0, _MIN_SIGMA_STEPS)
        worst = max(range(len(use)), key=lambda i: abs(res[i]))
        if abs(res[worst]) <= _CLIP_SIGMA * sigma:
            break
        rejected.append(use.pop(worst))
        slope, inter, sxx = _pooled(use)

    b = slope or 0.0
    res_all = [p["position"] - (inter[p["filter"]] + b * p["temp"]) for p in use]
    sigma_all = _robust_sigma(res_all)
    slope_se = (sigma_all / math.sqrt(sxx)
                if (slope is not None and sigma_all and sxx > 0) else None)

    filters = {}
    for f in sorted(inter):
        fp = [p for p in use if p["filter"] == f]
        fres = [p["position"] - (inter[f] + b * p["temp"]) for p in fp]
        last = max(fp, key=lambda p: str(p.get("time") or ""))
        temps = [p["temp"] for p in fp]
        filters[f] = {
            "n": len(fp),
            "n_rejected": sum(1 for p in rejected if p["filter"] == f),
            "intercept": round(inter[f], 1),
            "scatter": (round(s, 1) if (s := _robust_sigma(fres)) is not None
                        else None),
            "t_min": round(min(temps), 2), "t_max": round(max(temps), 2),
            "median_temp": round(_median(temps), 2),
            "last_time": last.get("time"), "last_position": int(last["position"]),
            "last_temp": last["temp"],
        }

    offsets = {}
    if ref_filter in filters:
        ra = filters[ref_filter]
        for f, row in filters.items():
            if f == ref_filter:
                continue
            se = None
            if row["scatter"] is not None and ra["scatter"] is not None:
                se = math.sqrt(row["scatter"] ** 2 / row["n"]
                               + ra["scatter"] ** 2 / ra["n"])
            offsets[f] = {"steps": round(row["intercept"] - ra["intercept"]),
                          "se": (round(se, 1) if se is not None else None),
                          "n": row["n"], "n_ref": ra["n"]}

    all_temps = [p["temp"] for p in use]
    return {
        "version": 1,
        "ref_filter": ref_filter,
        "n_points": len(use),
        "n_rejected": len(rejected),
        "slope_steps_per_c": (round(slope, 2) if slope is not None else None),
        "slope_se": (round(slope_se, 2) if slope_se is not None else None),
        "sigma_steps": (round(sigma_all, 1) if sigma_all is not None else None),
        "temp_range": ([round(min(all_temps), 2), round(max(all_temps), 2)]
                       if all_temps else None),
        "filters": filters,
        "offsets": offsets,
        "lookup": lookup_table({"slope_steps_per_c": slope, "filters": filters}),
    }


# --------------------------------------------------------------------------
# Predict / lookup
# --------------------------------------------------------------------------

def predict(model: dict, filter_name: str, temp: float | None) -> dict | None:
    """Best-focus prediction for a filter at a temperature.

    Returns {position, confidence, basis} or None when the model has nothing
    for this filter. Confidence:
      high: >=5 AF points for the filter, a fitted slope, and temp inside the
            fitted range +/- 3 C
      med:  >=3 AF points and (a fitted slope, or temp inside the filter's
            own measured range), or temp unknown with >=3 points
      low:  anything else the model can still answer."""
    if not model:
        return None
    row = (model.get("filters") or {}).get(str(filter_name))
    if not row:
        return None
    slope = model.get("slope_steps_per_c")
    t = _num(temp)
    if t is None:
        pos = row["intercept"] + (slope or 0.0) * row["median_temp"]
        conf = "med" if row["n"] >= 3 else "low"
        return {"position": round(pos), "confidence": conf,
                "basis": "median temperature (no live temp)"}
    pos = row["intercept"] + (slope or 0.0) * t
    in_own = row["t_min"] - 1.0 <= t <= row["t_max"] + 1.0
    tr = model.get("temp_range") or [row["t_min"], row["t_max"]]
    near_fit = tr[0] - _EXTRAP_MARGIN_C <= t <= tr[1] + _EXTRAP_MARGIN_C
    if slope is not None and row["n"] >= 5 and near_fit:
        conf = "high"
    elif row["n"] >= 3 and ((slope is not None and near_fit) or in_own):
        conf = "med"
    else:
        conf = "low"
    return {"position": round(pos), "confidence": conf,
            "basis": ("temperature fit" if slope is not None
                      else "per-filter mean (no slope yet)")}


def lookup_table(model: dict, temps=None) -> dict:
    """{filter: {temp_c(str): position}} over whole-degree temps (the EAF
    reports FOCTEMP in 1 C steps). Only filters the model has."""
    temps = list(temps) if temps is not None else list(range(0, 36, 2))
    slope = model.get("slope_steps_per_c") or 0.0
    out = {}
    for f, row in (model.get("filters") or {}).items():
        out[f] = {str(t): round(row["intercept"] + slope * t) for t in temps}
    return out


def load_model(config) -> dict | None:
    p = _model_path(config)
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("focus_model: could not read %s: %s", p, e)
    return None


def seed_position(config, filter_name: str, temp: float | None) -> int | None:
    """AF starting point from the model, or None to fall back to focus_seeds.
    Only 'med' or 'high' confidence predictions are used, and only when
    config.focus_model_seed is on."""
    if not bool(getattr(config, "focus_model_seed", True)):
        return None
    model = load_model(config)
    pr = predict(model or {}, filter_name, temp)
    if pr is None or pr["confidence"] not in ("med", "high"):
        return None
    return pr["position"]


def af_skip_readiness(model: dict, min_points: int = 8,
                      max_sigma: float = 25.0) -> dict:
    """Does the data support skipping or shortening AF (a phase-2 feature)?
    Informational only; nothing acts on it yet."""
    reasons = []
    if not model or not model.get("filters"):
        return {"ready": False, "reasons": ["no AF reports ingested yet"]}
    if model.get("slope_steps_per_c") is None:
        reasons.append(f"no temperature slope yet (needs a >= "
                       f"{_MIN_TEMP_SPAN_C:.0f} C spread within one filter)")
    ref = model.get("ref_filter", "L")
    fr = model["filters"].get(ref)
    if not fr or fr["n"] < min_points:
        reasons.append(f"{ref}: {fr['n'] if fr else 0} AF points, needs "
                       f"{min_points}")
    sig = model.get("sigma_steps")
    if sig is None or sig > max_sigma:
        reasons.append(f"fit scatter {sig} steps, needs <= {max_sigma:.0f}")
    return {"ready": not reasons, "reasons": reasons}


def summary(config) -> dict:
    """Read-only view for GET /api/focus: the fitted model (refit from the
    stored points, nothing written), AF run stats and phase-2 readiness."""
    pts = [p for p in load_points(config) if _point_rig(p, config) == "rc16"]
    model = fit(pts, ref_filter=_ref_filter(config))
    durs = [d for p in pts if (d := _num(p.get("duration_s"))) is not None]
    walks = [abs(p["position"] - p["initial"]) for p in pts
             if p.get("initial") is not None and p.get("position") is not None]
    return {
        "enabled": bool((getattr(config, "nina_autofocus_reports_dir", "")
                         or "").strip()),
        "use_for_seeds": bool(getattr(config, "focus_model_seed", True)),
        "model": model,
        "af_runs": {
            "stored": len(pts),
            "median_duration_s": (round(_median(durs), 1) if durs else None),
            "median_seed_error_steps": (_median(walks) if walks else None),
        },
        "af_skip_readiness": af_skip_readiness(model),
    }


def piggyback_summary(config) -> dict:
    """Read-only Piggy-600 model from the AF reports sorted to it: one channel
    (OSC), its temperature slope once the data spans 3 C, and the lookup
    table. Informational: nothing seeds from it yet."""
    pts = load_points(config, "piggyback")
    model = fit(pts, ref_filter=OSC_FILTER)
    durs = [d for p in pts if (d := _num(p.get("duration_s"))) is not None]
    return {
        "enabled": bool((getattr(config, "nina_autofocus_reports_dir", "")
                         or "").strip())
                   and bool(getattr(config, "focus_model_piggyback", True)),
        "model": model,
        "af_runs": {"stored": len(pts),
                    "median_duration_s": (round(_median(durs), 1) if durs
                                          else None)},
    }
