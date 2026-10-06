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

When ingest runs (PS-76 part 2)
-------------------------------
Until part 2 ingest ran only at the tail of the per-night backfill thread,
and that thread only starts when a night has UNGRADED frames. The live
grader grades every sub as it lands, so on a normal night the backfill never
ran and no AF report was ever read (2026-10-06: reports dir set, 0 points).
Now ingest_loop() (started with the service) ingests at startup and again
whenever the reports folder changes (polled every
focus_model_ingest_poll_s, so within a couple of minutes of each NINA AF),
plus a forced pass once a day; the backfill hook stays. POST
/api/focus/ingest and `photonscript focus-ingest` run it on demand. Each run
writes focus_ingest_status.json (what was read, parse failures, why reports
were rejected or left out) for GET /api/focus.

Model-driven focus (PS-76 part 2, focus_model_drive, default off)
-----------------------------------------------------------------
trust() says whether the RC16 model is good enough to replace the per-block
AF: enough L points, a fitted slope, and a fit scatter well inside the
critical focus zone (focus_cfz_steps, else the AF step size as a proxy).
Advisory until Jeremy turns focus_model_drive on; then the sequence moves
the focuser to the table position on filter and temperature changes
(ExternalScript -> POST /api/focus/model-move) and keeps a periodic verify
AF (focus_model_verify_af_min).
"""
from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

POINTS_FILE = "focus_af_points.json"
MODEL_FILE = "focus_model.json"
PB_POINTS_FILE = "piggyback_focus_af_points.json"
PB_MODEL_FILE = "piggyback_focus_model.json"
INGEST_STATUS_FILE = "focus_ingest_status.json"
MOVES_FILE = "focus_model_moves.jsonl"
OSC_FILTER = "OSC"   # the Piggy-600's single channel (reports carry no filter)

# Fit guards
_MIN_TEMP_SPAN_C = 3.0     # within-filter temperature spread needed for a slope
_MIN_SIGMA_STEPS = 5.0     # floor on the robust sigma (EAF steps) for clipping
_CLIP_SIGMA = 3.0
_EXTRAP_MARGIN_C = 3.0     # predictions this far outside the fitted temps stay "high"

# Trust (model-driven focus): enough reference-filter AFs, and a fit scatter
# within this fraction of the critical focus zone. Without a CFZ (no config,
# no AF step data) the old fixed 25-step bar applies.
_TRUST_MIN_POINTS = 8
_TRUST_CFZ_FRACTION = 0.5
_TRUST_FALLBACK_SIGMA = 25.0
_MOVE_DEADBAND_STEPS = 8   # model-move: closer than this, leave the focuser

# Camera names that settle which rig wrote a report when a report carries
# them (NINA's AF report JSON normally names no camera; a plugin or a newer
# NINA may). Checked in the report's top-level text fields only.
_RC16_NAME_HINTS = ("AP26MC",)
_PB_NAME_HINTS = ("AP26CC",)


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
    step = None
    if isinstance(mp, list):
        xs = sorted({x for q in mp if (x := _pos(q)) is not None})
        gaps = [b - a for a, b in zip(xs, xs[1:]) if b > a]
        step = round(_median(gaps)) if gaps else None
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
        "step": step,
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


def _name_hint(report: dict) -> str | None:
    """'rc16' / 'piggyback' when a top-level text field (camera, focuser,
    profile name, file name) names exactly one rig's camera, else None."""
    text = " ".join(str(v) for k, v in report.items()
                    if isinstance(v, str)).upper()
    rc = any(h in text for h in _RC16_NAME_HINTS)
    pb = any(h in text for h in _PB_NAME_HINTS)
    if rc != pb:
        return "rc16" if rc else "piggyback"
    return None


def classify_report(report: dict, config=None) -> str | None:
    """'rc16', 'piggyback' or None (unknown: left out of both models).

    Order: the configured matchers when set; else a camera name in the
    report (AP26MC / AP26CC); else the filter + EAF range rule."""
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
    if (hint := _name_hint(report)) is not None:
        return hint
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


_INGEST_LOCK = threading.Lock()


def _status_path(config) -> Path:
    return Path(config.data_dir) / INGEST_STATUS_FILE


def load_ingest_status(config) -> dict | None:
    """The last ingest's result (focus_ingest_status.json), or None."""
    p = _status_path(config)
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("focus_model: could not read %s: %s", p, e)
    return None


def _save_status(config, out: dict, trigger: str) -> None:
    try:
        _write_json(_status_path(config),
                    dict(out, trigger=trigger,
                         at=datetime.now().isoformat(timespec="seconds")))
    except Exception as e:  # noqa: BLE001
        logger.debug("focus_model: could not write ingest status: %s", e)


def _reason_key(rig: str, why: str) -> str:
    """'R^2 0.41 < 0.70' -> 'rc16: R^2 below af_min_r2' style buckets."""
    if why.startswith("R^2"):
        why = "R^2 below af_min_r2"
    elif why.startswith("only"):
        why = "too few measure points"
    return f"{rig}: {why}"


def ingest_af_reports(config, reports_dir: str | None = None,
                      trigger: str = "backfill") -> dict:
    """Read every NINA AF report in the reports dir, sort each by rig, add
    the good ones not already stored, then refit and persist the models.
    Only RC16 reports reach the RC16 model; Piggy-600 reports go to their
    own store (focus_model_piggyback, default on). Never raises. One ingest
    at a time (the poller, the backfill and the API share a lock).

    Returns {enabled, dir, read, added, rejected, total, other_rig,
    unclassified, piggyback: {added, total}, files, parse_errors,
    reject_reasons, bad_files} and saves it as focus_ingest_status.json
    with the trigger (startup / poll / daily / backfill / api / cli)."""
    rdir = (reports_dir if reports_dir is not None else
            (getattr(config, "nina_autofocus_reports_dir", "") or "")).strip()
    if not rdir:
        return {"enabled": False, "read": 0, "added": 0, "rejected": 0,
                "total": len(load_points(config))}
    with _INGEST_LOCK:
        out = _ingest_locked(config, rdir)
    _save_status(config, out, trigger)
    return out


def _ingest_locked(config, rdir: str) -> dict:
    stats: dict = {}
    try:
        from photonscript.scheduler.focus_reports import load_reports
        reports = load_reports(rdir, stats=stats)
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
        reasons: Counter = Counter()
        for rep in reports:
            rig = classify_report(rep, config)
            if rig is None:
                unknown += 1
                continue
            if rig != "rc16":
                other += 1
                if not want_pb:
                    continue
            pt, why = point_from_report(rep, config, min_r2, min_pts)
            if pt is None:
                if rig == "rc16":
                    rejected += 1
                reasons[_reason_key(rig, why)] += 1
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
        logger.info("focus_model: %d AF report(s) read from %d file(s) "
                    "(%d unparseable), RC16 %d added / %d rejected / %d "
                    "stored, Piggy-600 %d added, %d other-rig, %d "
                    "unclassified", len(reports), stats.get("files", 0),
                    stats.get("parse_errors", 0), added["rc16"], rejected,
                    len(stores["rc16"]), added["piggyback"], other, unknown)
        return {"enabled": True, "dir": rdir,
                "dir_exists": stats.get("dir_exists", False),
                "files": stats.get("files", 0),
                "parse_errors": stats.get("parse_errors", 0),
                "bad_files": stats.get("bad_files", []),
                "read": len(reports), "added": added["rc16"],
                "rejected": rejected, "total": len(stores["rc16"]),
                "other_rig": other, "unclassified": unknown,
                "reject_reasons": dict(reasons),
                "piggyback": {"added": added["piggyback"],
                              "total": len(stores["piggyback"])}}
    except Exception as e:  # noqa: BLE001
        logger.warning("focus_model: ingest failed: %s", e)
        return {"enabled": True, "dir": rdir, "read": 0, "added": 0,
                "rejected": 0, "total": 0, "error": str(e),
                "dir_exists": stats.get("dir_exists", False),
                "files": stats.get("files", 0)}


def reports_signature(rdir: str) -> tuple | None:
    """(file count, newest mtime) of the reports folder: changes when NINA
    writes a new AF report. None when the folder cannot be read."""
    try:
        d = Path(rdir)
        if not d.is_dir():
            return None
        mt = [f.stat().st_mtime for f in d.glob("*.json")]
        return (len(mt), max(mt) if mt else 0.0)
    except OSError:
        return None


async def ingest_loop(get_config, sleep=None) -> None:
    """Background ingest (started with the service): once at startup, then
    whenever the reports folder changes (a new NINA AF report), checked
    every focus_model_ingest_poll_s, plus a forced pass every 24 h. A poll
    of 0 turns the loop off (the backfill hook and the API still ingest).
    The ingest itself runs in a worker thread. Never raises.

    sleep: test hook (an awaitable taking seconds); when it returns False
    the loop stops."""
    import asyncio
    sleep = sleep or asyncio.sleep
    last_sig = None
    last_full = 0.0
    first = True
    while True:
        poll = 120.0
        try:
            cfg = get_config()
            poll = float(getattr(cfg, "focus_model_ingest_poll_s", 120) or 0)
            rdir = (getattr(cfg, "nina_autofocus_reports_dir", "")
                    or "").strip()
            if poll > 0 and rdir:
                sig = await asyncio.to_thread(reports_signature, rdir)
                daily = time.time() - last_full >= 86400
                if first or daily or (sig is not None and sig != last_sig):
                    trig = "startup" if first else ("daily" if daily
                                                    else "poll")
                    res = await asyncio.to_thread(ingest_af_reports, cfg,
                                                  None, trig)
                    if res.get("added") or res.get("error"):
                        logger.info("focus_model: %s ingest: added %s, "
                                    "error %s", trig, res.get("added"),
                                    res.get("error"))
                    if res.get("added"):
                        # PS-144: configured offset vs same-night pairs
                        chk = await asyncio.to_thread(offset_check, cfg)
                        await offset_alert(cfg, chk)
                    if first or daily:
                        last_full = time.time()
                    first = False
                last_sig = sig
        except Exception as e:  # noqa: BLE001
            logger.warning("focus_model: ingest loop error: %s", e)
        if poll <= 0:
            poll = 600.0   # off: look again later in case it is turned on
        if await sleep(max(15.0, poll)) is False:
            return


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


def af_skip_readiness(model: dict, min_points: int = _TRUST_MIN_POINTS,
                      max_sigma: float = _TRUST_FALLBACK_SIGMA) -> dict:
    """Does the data support replacing AF with model moves? ready = enough
    reference-filter AF points, a fitted slope and a fit scatter at or below
    max_sigma. trust() picks max_sigma from the critical focus zone."""
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


def _rc16_points(config) -> list[dict]:
    return [p for p in load_points(config) if _point_rig(p, config) == "rc16"]


def cfz_steps(config, pts: list[dict] | None = None) -> tuple:
    """(critical focus zone in EAF steps, source). focus_cfz_steps when set;
    else the median AF step size of the stored AF runs (NINA's AF step is
    normally set near one CFZ, so it is a fair proxy); else (None, None)."""
    v = _num(getattr(config, "focus_cfz_steps", 0))
    if v and v > 0:
        return float(v), "config focus_cfz_steps"
    pts = _rc16_points(config) if pts is None else pts
    steps = [x for p in pts if (x := _num(p.get("step"))) and x > 0]
    if steps:
        return float(_median(steps)), "median AF step size"
    return None, None


def drive_filters(model: dict, min_n: int = 5) -> list[str]:
    """Filters with enough AF points of their own for a model move."""
    return sorted(f for f, row in ((model or {}).get("filters") or {}).items()
                  if row.get("n", 0) >= min_n)


def trust(config, model: dict | None = None,
          pts: list[dict] | None = None) -> dict:
    """Is the RC16 model good enough to move the focuser instead of running
    AF (PS-76 part 2)? trusted = af_skip_readiness with the scatter bar at
    _TRUST_CFZ_FRACTION x CFZ (or 25 steps with no CFZ). mode = "drive" only
    when focus_model_drive is on AND the model is trusted; otherwise
    "advisory" (the sequence keeps its AFs)."""
    pts = _rc16_points(config) if pts is None else pts
    if model is None:
        model = fit(pts, ref_filter=_ref_filter(config))
    cfz, src = cfz_steps(config, pts)
    limit = (round(_TRUST_CFZ_FRACTION * cfz, 1) if cfz
             else _TRUST_FALLBACK_SIGMA)
    rd = af_skip_readiness(model, _TRUST_MIN_POINTS, limit)
    on = bool(getattr(config, "focus_model_drive", False))
    return {"trusted": rd["ready"], "reasons": rd["reasons"],
            "cfz_steps": (round(cfz) if cfz else None), "cfz_source": src,
            "sigma_steps": model.get("sigma_steps"), "sigma_limit": limit,
            "drive_enabled": on,
            "mode": "drive" if (on and rd["ready"]) else "advisory",
            "filters": drive_filters(model),
            "verify_af_min": float(getattr(config, "focus_model_verify_af_min",
                                           120.0) or 0)}


def _night_of(ts: str):
    """Local night (evening date) of a NINA timestamp; None if unparseable.
    NINA writes local time with an offset; the first 19 chars suffice."""
    try:
        t = datetime.strptime(str(ts)[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    return (t - timedelta(hours=12)).date()


def expected_temp(config, today=None, max_age_days: int = 21) -> dict | None:
    """A stand-in for tonight's focuser temperature when NINA cannot be asked
    (sequences are built before the focuser is connected): the temperature
    of the FIRST RC16 AF of the most recent night with AF reports, if that
    night is at most max_age_days old. {temp, night, basis} or None."""
    by_night: dict = {}
    for p in _rc16_points(config):
        t = _num(p.get("temp"))
        n = _night_of(p.get("time") or "")
        if t is None or n is None:
            continue
        by_night.setdefault(n, []).append((str(p.get("time")), t))
    if not by_night:
        return None
    night = max(by_night)
    today = today or datetime.now().date()
    if (today - night).days > max_age_days:
        return None
    first = min(by_night[night])
    return {"temp": round(first[1], 2), "night": night.isoformat(),
            "basis": f"first RC16 AF of the night of {night.isoformat()}"}


def model_move_target(config, filter_name: str, temp) -> dict:
    """Where a model-driven move (focus_model_drive) should put the focuser
    for this filter at this focuser temperature, or why it should not move.
    {move: bool, position?, reason, confidence?}. Read-only."""
    if not bool(getattr(config, "focus_model_drive", False)):
        return {"move": False, "reason": "focus_model_drive is off (advisory)"}
    t = _num(temp)
    if t is None:
        return {"move": False, "reason": "no focuser temperature"}
    pts = _rc16_points(config)
    model = fit(pts, ref_filter=_ref_filter(config))
    tr = trust(config, model, pts)
    if not tr["trusted"]:
        return {"move": False, "reason": "model not trusted: "
                + "; ".join(tr["reasons"])}
    filt = norm_filter(filter_name, config)
    if filt not in tr["filters"]:
        return {"move": False, "reason": f"{filt}: too few AF points of its "
                "own for a model move"}
    pr = predict(model, filt, t)
    if pr is None or pr["confidence"] != "high":
        return {"move": False, "reason": f"{filt} at {t:.1f} C: prediction "
                f"confidence {pr and pr['confidence']} (needs high)"}
    from photonscript.scheduler.focus_seeds import _clamp
    return {"move": True, "position": _clamp(pr["position"]),
            "filter": filt, "temp": t, "confidence": pr["confidence"],
            "reason": f"model {filt} at {t:.1f} C"}


def record_move(config, entry: dict) -> None:
    """Append one model-move outcome to focus_model_moves.jsonl."""
    try:
        path = Path(config.data_dir) / MOVES_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(dict(entry, at=datetime.now().isoformat(
                timespec="seconds"))) + "\n")
    except OSError as e:
        logger.debug("focus_model: could not log move: %s", e)


def recent_moves(config, n: int = 10) -> list[dict]:
    path = Path(config.data_dir) / MOVES_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-n:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def residuals(pts: list[dict], model: dict, n: int = 12) -> list[dict]:
    """The last n AF points with the model's prediction and the residual
    (measured minus model, EAF steps): how well the table would have done."""
    slope = model.get("slope_steps_per_c") or 0.0
    rows = (model.get("filters") or {})
    sig = model.get("sigma_steps")
    out = []
    usable = [p for p in pts if _num(p.get("temp")) is not None
              and _num(p.get("position")) is not None]
    for p in sorted(usable, key=lambda q: str(q.get("time") or ""))[-n:]:
        row = rows.get(p.get("filter"))
        pred = (round(row["intercept"] + slope * float(p["temp"]))
                if row else None)
        res = (int(round(float(p["position"]))) - pred
               if pred is not None else None)
        out.append({"time": p.get("time"), "filter": p.get("filter"),
                    "temp": p.get("temp"), "position": p.get("position"),
                    "initial": p.get("initial"), "predicted": pred,
                    "residual": res,
                    "outlier": bool(res is not None and sig
                                    and abs(res) > _CLIP_SIGMA
                                    * max(sig, _MIN_SIGMA_STEPS))})
    return out


def plot_points(pts: list[dict], n: int = 300) -> list[dict]:
    """The last n AF points, compact, for the dashboard scatter."""
    use = [p for p in pts if _num(p.get("temp")) is not None
           and _num(p.get("position")) is not None]
    use.sort(key=lambda q: str(q.get("time") or ""))
    return [{"f": p.get("filter"), "t": p.get("temp"), "p": p.get("position"),
             "time": p.get("time")} for p in use[-n:]]


def summary(config) -> dict:
    """Read-only view for GET /api/focus: the fitted model (refit from the
    stored points, nothing written), AF run stats, the last AF residuals,
    the last ingest, trust / drive state and phase-2 readiness."""
    pts = _rc16_points(config)
    model = fit(pts, ref_filter=_ref_filter(config))
    durs = [d for p in pts if (d := _num(p.get("duration_s"))) is not None]
    walks = [abs(p["position"] - p["initial"]) for p in pts
             if p.get("initial") is not None and p.get("position") is not None]
    tr = trust(config, model, pts)
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
        "residuals": residuals(pts, model),
        "points": plot_points(pts),
        "expected_temp": expected_temp(config),
        "ingest": load_ingest_status(config),
        "trust": tr,
        "moves": recent_moves(config),
        "offset_check": offset_check(config, pts, model),
        "af_skip_readiness": {"ready": tr["trusted"],
                              "reasons": tr["reasons"]},
    }


# --------------------------------------------------------------------------
# Configured vs measured filter offsets (PS-144)
# --------------------------------------------------------------------------
# The configured focus_filter_offsets ran at -187 for nine nights while the
# model put Ha at +123 (every NB sub about 2.8 CFZ inside focus). These read
# the offset straight from same-night reference/NB AF pairs (the dusk focus
# calibration produces exactly these) and flag a configured offset more than
# one CFZ away. Read-only; offset_alert() pushes once per measured night.

PAIR_MAX_GAP_MIN = 90.0    # an NB AF pairs with the nearest ref AF this close
OFFSET_ALERT_FILE = "focus_offset_alert.json"


def _when(p: dict):
    try:
        return datetime.strptime(str(p.get("time") or "")[:19],
                                 "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None


def pair_offsets(pts: list[dict], ref_filter: str = "L",
                 slope: float | None = None,
                 max_gap_min: float = PAIR_MAX_GAP_MIN) -> dict:
    """Per filter, the offset from the reference filter measured on the most
    recent night with pairs: each AF in that filter pairs with the ref AFs
    of the same night just before and just after it (within max_gap_min;
    the mean of both when bracketed), delta = filter position - ref
    position, corrected by slope x (temperature difference) when both
    temperatures and a slope are known. {filter: {steps, n, night, spread,
    deltas}}; steps is the median delta."""
    by_night: dict = {}
    for p in pts or []:
        t = _when(p)
        pos = _num(p.get("position"))
        if t is None or pos is None or not p.get("filter"):
            continue
        by_night.setdefault((t - timedelta(hours=12)).date(), []).append(
            (t, p["filter"], pos, _num(p.get("temp"))))
    found: dict = {}
    for night in sorted(by_night):
        rows = by_night[night]
        refs = [r for r in rows if r[1] == ref_filter]
        if not refs:
            continue
        per: dict = {}
        for t, f, pos, temp in rows:
            if f == ref_filter:
                continue
            gap = max_gap_min * 60
            before = [r for r in refs if 0 <= (t - r[0]).total_seconds() <= gap]
            after = [r for r in refs if 0 < (r[0] - t).total_seconds() <= gap]
            near = ([max(before, key=lambda r: r[0])] if before else []) + \
                   ([min(after, key=lambda r: r[0])] if after else [])
            if not near:
                continue
            # bracketed (the calibration's L,X,L order): the mean of the ref
            # AF just before and just after; else the one ref AF in reach
            ds = []
            for r in near:
                d = pos - r[2]
                if slope and temp is not None and r[3] is not None:
                    d -= slope * (temp - r[3])
                ds.append(d)
            per.setdefault(f, []).append(sum(ds) / len(ds))
        for f, ds in per.items():
            found[f] = {"steps": round(_median(ds)), "n": len(ds),
                        "night": night.isoformat(),
                        "spread": round(max(ds) - min(ds)),
                        "deltas": [round(d) for d in ds]}
    return found


def offset_check(config, pts: list[dict] | None = None,
                 model: dict | None = None) -> dict:
    """Configured focus_filter_offsets vs the same-night pair measurement
    (pair_offsets) and the model offset, per filter. A filter alerts when it
    has a pair measurement and |configured - measured| > one CFZ (cfz_steps:
    focus_cfz_steps, else the AF step size). Unlisted filters are
    configured 0 (the sequence applies no move). Never raises."""
    try:
        pts = _rc16_points(config) if pts is None else pts
        ref = _ref_filter(config)
        if model is None:
            model = fit(pts, ref_filter=ref)
        cfz, src = cfz_steps(config, pts)
        try:
            configured = config.focus_offset_map()
        except Exception:  # noqa: BLE001
            configured = {}
        pairs = pair_offsets(pts, ref, model.get("slope_steps_per_c"))
        mo = model.get("offsets") or {}
        out = {}
        for f in sorted(set(configured) | set(pairs) | set(mo)):
            if f == ref:
                continue
            conf = int(configured.get(f, 0))
            meas = pairs.get(f)
            diff = (conf - meas["steps"]) if meas else None
            out[f] = {"configured": conf, "listed": f in configured,
                      "measured": meas,
                      "model": (mo.get(f) or {}).get("steps"),
                      "model_se": (mo.get(f) or {}).get("se"),
                      "diff": diff,
                      "alert": bool(cfz and diff is not None
                                    and abs(diff) > cfz)}
        alerts = [f for f, r in out.items() if r["alert"]]
        return {"ref_filter": ref, "cfz_steps": (round(cfz) if cfz else None),
                "cfz_source": src, "filters": out, "alerts": alerts,
                "alert": bool(alerts),
                "night": max((out[f]["measured"]["night"] for f in alerts),
                             default=None)}
    except Exception as e:  # noqa: BLE001
        logger.warning("focus_model: offset check failed: %s", e)
        return {"error": str(e), "filters": {}, "alerts": [], "alert": False}


def offset_alert_text(chk: dict) -> str:
    parts = []
    for f in chk.get("alerts") or []:
        r = chk["filters"][f]
        m = r["measured"]
        parts.append(f"{f}: configured {r['configured']:+d}, measured "
                     f"{m['steps']:+d} ({m['n']} pair(s), {m['night']})")
    return ("Focus offset from " + str(chk.get("ref_filter")) + " is off by "
            "more than one CFZ (" + str(chk.get("cfz_steps")) + " steps): "
            + "; ".join(parts) + ". Update PS_FOCUS_FILTER_OFFSETS on the "
            "System page.")


async def offset_alert(config, chk: dict | None = None, notify=None) -> bool:
    """One Pushover per measured night when offset_check() alerts (state in
    <data_dir>/focus_offset_alert.json). True when a push went out. Never
    raises."""
    try:
        chk = offset_check(config) if chk is None else chk
        if not chk.get("alert"):
            return False
        path = Path(config.data_dir) / OFFSET_ALERT_FILE
        try:
            last = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            last = {}
        if last.get("night") == chk.get("night"):
            return False
        if notify is None:
            from photonscript.shared.pushover import notify
        msg = offset_alert_text(chk)
        await notify(config, msg, title="PhotonScript focus offsets")
        _write_json(path, {"night": chk.get("night"), "alerts": chk["alerts"],
                           "at": datetime.utcnow().isoformat() + "Z",
                           "message": msg})
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("focus_model: offset alert failed: %s", e)
        return False


def piggyback_summary(config) -> dict:
    """Read-only Piggy-600 model from the AF reports sorted to it: one channel
    (OSC), its temperature slope once the data spans 3 C, the lookup table
    and the last AF residuals. Informational: nothing seeds from it yet."""
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
        "residuals": residuals(pts, model),
        "points": plot_points(pts),
    }
