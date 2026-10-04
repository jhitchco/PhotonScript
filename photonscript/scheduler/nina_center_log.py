"""NINA first-slew pointing error from the Center log lines (PS-104).

Jeremy sees NINA's first slew land about 4 arcmin off and re-slew. This turns
that into a number with a direction, by side of the meridian, Dec and HA:

    parse(text)               every "Centering Solver" line of a NINA log
    group_runs(lines)         one run per Center / Slew-and-center: the first
                              solve is the first-slew error, the count is the
                              attempts NINA needed
    night_pass(config, date)  the RC16 NINA logs of a night -> runs, written to
                              <data_dir>/pointing/<night>_center.jsonl (+ a
                              _meta.json with the last filter NINA moved to)
    summary(config, nights)   median / p90 first-slew error by side, Dec band
                              and HA band over the last N nights, plus the
                              PS-67 mount-vs-solve median when that record is
                              on disk (runs/<night>_pointing.jsonl, read only)

Log format: NINA 3's CenteringSolver logs one INFO line per solve,

    <local time>|INFO|CenteringSolver.cs|Center|<n>|Centering Solver - Scope
    Position: RA: 18:53:35; Dec: 33\u00b0 01' 45"; Epoch: JNOW; Offset: ...;
    Centering Coordinates: RA: ...; Dec: ...; Epoch: J2000; Solved: RA: ...;
    Dec: ...; Epoch: J2000; Separation RA: ...; Dec: ...; Distance: ...;
    Threshold: 1

No real NINA log was available when this was written (none is synced to the
desktop), so the parser follows NINA's documented message and is tolerant:
the separation is computed from "Centering Coordinates" and "Solved" (the
Distance field is the fallback), any degree-sign mangling is accepted, and a
line that does not parse is skipped, never raised. Confidence Med until it
is checked on a real night log (MAINTENANCE.md).

Side of the meridian: a "pier side East/West" phrase in the run wins;
otherwise it is inferred from the hour angle (target east of the meridian =
"E", HA < 0). With flips at the meridian that is the pier side split.
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

_TS = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?)\|(\w+)\|([^|]*)\|")
_HMS = r"[-+]?\d{1,2}\s*[:h ]\s*\d{1,2}\s*[:m ]\s*\d{1,2}(?:\.\d+)?\s*s?"
_DMS = r"[-+]?\d{1,3}\s*\D{1,3}?\s*\d{1,2}\s*\D{1,3}?\s*\d{1,2}(?:\.\d+)?\s*\D{0,2}"
_COORD = r"RA:\s*(" + _HMS + r")\s*;\s*Dec:\s*(" + _DMS + r")"
_RE_TARGET = re.compile(r"Centering Coordinates:\s*" + _COORD, re.I)
_RE_SOLVED = re.compile(r"Solved:\s*" + _COORD, re.I)
_RE_SCOPE = re.compile(r"Scope Position:\s*" + _COORD, re.I)
_RE_DIST = re.compile(r"Separation.*?Distance:\s*(" + _DMS + r")", re.I)
_RE_THRESH = re.compile(r"Threshold:\s*([\d.]+)", re.I)
_RE_PIER = re.compile(r"pier\s*side\W{0,4}(?:pier)?\s*(East|West)", re.I)
_RE_FILTER = re.compile(r"Moving to Filter\s+(\S+)\s+at Position", re.I)

GAP_MIN = 20.0          # a longer pause starts a new run
SAME_TARGET_ARCMIN = 1.0
DEC_BANDS = ((-90, 0, "Dec < 0"), (0, 30, "Dec 0 to 30"), (30, 60, "Dec 30 to 60"),
             (60, 91, "Dec > 60"))
HA_BANDS = ((-12, -3, "HA < -3 h"), (-3, 0, "HA -3 to 0 h"), (0, 3, "HA 0 to 3 h"),
            (3, 12.01, "HA > 3 h"))


# ------------------------------------------------------------------ parsing

def _sexa(text: str) -> float | None:
    """'18:53:35', '18h53m35s', '33\u00b0 01\\' 45"', '-05 12 30' -> value in
    the unit of the first field (hours or degrees)."""
    t = (text or "").strip()
    nums = re.findall(r"\d+(?:\.\d+)?", t)
    if len(nums) < 3:
        return None
    a, b, c = (float(x) for x in nums[:3])
    v = a + b / 60.0 + c / 3600.0
    return -v if t.startswith("-") else v


def _sep_arcmin(ra1, dec1, ra2, dec2) -> float:
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    c = (math.sin(d1) * math.sin(d2)
         + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2))
    return math.degrees(math.acos(max(-1.0, min(1.0, c)))) * 60.0


def _east_north(t_ra, t_dec, s_ra, s_dec) -> tuple[float, float]:
    """Where the solve landed from the target, arcmin (east = +RA)."""
    dra = ((s_ra - t_ra + 180.0) % 360.0) - 180.0
    return (dra * math.cos(math.radians(t_dec)) * 60.0, (s_dec - t_dec) * 60.0)


def parse(text: str) -> list[dict]:
    """Every Centering Solver line: {t_local, target_ra, target_dec,
    solved_ra, solved_dec, sep_arcmin, east_arcmin, north_arcmin,
    threshold_arcmin, pier}. Degrees; unparsable lines are skipped."""
    out = []
    for line in (text or "").splitlines():
        if "centering solver" not in line.lower():
            continue
        m = _TS.match(line)
        if not m:
            continue
        try:
            t = datetime.fromisoformat(m.group(1)[:26])
        except ValueError:
            continue
        tg, sv = _RE_TARGET.search(line), _RE_SOLVED.search(line)
        if not tg:
            continue
        t_ra, t_dec = _sexa(tg.group(1)), _sexa(tg.group(2))
        if t_ra is None or t_dec is None:
            continue
        rec = {"t_local": t.isoformat(timespec="seconds"),
               "target_ra": round(t_ra * 15.0, 5), "target_dec": round(t_dec, 5),
               "solved_ra": None, "solved_dec": None, "sep_arcmin": None,
               "east_arcmin": None, "north_arcmin": None,
               "threshold_arcmin": None, "pier": None}
        if sv:
            s_ra, s_dec = _sexa(sv.group(1)), _sexa(sv.group(2))
            if s_ra is not None and s_dec is not None:
                rec["solved_ra"], rec["solved_dec"] = round(s_ra * 15.0, 5), round(s_dec, 5)
                rec["sep_arcmin"] = round(_sep_arcmin(rec["target_ra"], t_dec,
                                                      rec["solved_ra"], s_dec), 3)
                e, n = _east_north(rec["target_ra"], t_dec, rec["solved_ra"], s_dec)
                rec["east_arcmin"], rec["north_arcmin"] = round(e, 3), round(n, 3)
        if rec["sep_arcmin"] is None:
            d = _RE_DIST.search(line)
            v = _sexa(d.group(1)) if d else None
            if v is None:
                continue
            rec["sep_arcmin"] = round(abs(v) * 60.0, 3)
        th = _RE_THRESH.search(line)
        if th:
            try:
                rec["threshold_arcmin"] = float(th.group(1))
            except ValueError:
                pass
        p = _RE_PIER.search(line)
        if p:
            rec["pier"] = p.group(1)[0].upper()
        out.append(rec)
    return out


def last_filter(text: str) -> str | None:
    """The last filter NINA moved to in a log ("Moving to Filter L at
    Position 0"), or None."""
    hits = _RE_FILTER.findall(text or "")
    return hits[-1] if hits else None


# ------------------------------------------------------------------ geometry

def _jd(dt_utc: datetime) -> float:
    return dt_utc.timestamp() / 86400.0 + 2440587.5 if dt_utc.tzinfo else \
        (dt_utc - datetime(1970, 1, 1)).total_seconds() / 86400.0 + 2440587.5


def hour_angle(ra_deg: float, when_utc: datetime, lon_deg: float) -> float:
    """Hour angle in hours, -12..12 (east longitude positive)."""
    d = _jd(when_utc) - 2451545.0
    gmst = (280.46061837 + 360.98564736629 * d) % 360.0
    ha = ((gmst + lon_deg - ra_deg) % 360.0) / 15.0
    return ha - 24.0 if ha >= 12.0 else ha


def _band(v, bands):
    if v is None:
        return None
    for lo, hi, name in bands:
        if lo <= v < hi:
            return name
    return None


def group_runs(lines: list[dict], config=None) -> list[dict]:
    """One record per Center run: first solve = the first-slew error."""
    runs: list[list[dict]] = []
    for r in lines:
        cur = runs[-1] if runs else None
        if cur:
            last = cur[-1]
            gap = (datetime.fromisoformat(r["t_local"])
                   - datetime.fromisoformat(last["t_local"])).total_seconds() / 60.0
            same = _sep_arcmin(r["target_ra"], r["target_dec"], last["target_ra"],
                               last["target_dec"]) <= SAME_TARGET_ARCMIN
            if same and 0 <= gap <= GAP_MIN:
                cur.append(r)
                continue
        runs.append([r])
    lon = float(getattr(config, "observatory_lon", -109.021367)) if config else -109.021367
    out = []
    for run in runs:
        f, z = run[0], run[-1]
        t_loc = datetime.fromisoformat(f["t_local"])
        t_utc = _to_utc(config, t_loc)
        ha = hour_angle(f["target_ra"], t_utc, lon)
        pier = next((x["pier"] for x in run if x.get("pier")), None)
        side, src = (pier, "log") if pier else (("E" if ha < 0 else "W"), "ha")
        thr = z.get("threshold_arcmin")
        out.append({
            "t_local": f["t_local"], "t_utc": t_utc.replace(microsecond=0).isoformat() + "Z",
            "target_ra": f["target_ra"], "target_dec": f["target_dec"],
            "first_sep_arcmin": f["sep_arcmin"],
            "first_east_arcmin": f["east_arcmin"], "first_north_arcmin": f["north_arcmin"],
            "attempts": len(run), "final_sep_arcmin": z["sep_arcmin"],
            "threshold_arcmin": thr,
            "converged": (z["sep_arcmin"] <= thr) if thr is not None else None,
            "ha_h": round(ha, 3), "side": side, "side_src": src,
            "dec_band": _band(f["target_dec"], DEC_BANDS),
            "ha_band": _band(ha, HA_BANDS)})
    return out


def _to_utc(config, t_local: datetime) -> datetime:
    off = -7.0
    if config is not None:
        try:
            from photonscript.shared.localtime import utc_offset_hours
            off = utc_offset_hours(config, t_local + timedelta(hours=7))
        except Exception:  # noqa: BLE001
            off = float(getattr(config, "utc_offset_hours", -7.0))
    return t_local - timedelta(hours=off)


# ------------------------------------------------------------------ storage

def pointing_dir(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "pointing"


def night_file(config, night: str) -> Path:
    return pointing_dir(config) / f"{night}_center.jsonl"


def meta_file(config, night: str) -> Path:
    return pointing_dir(config) / f"{night}_center_meta.json"


def night_pass(config, night: str) -> dict:
    """Parse the RC16 NINA logs covering `night` and (re)write the night's
    center file. Never raises."""
    try:
        from photonscript.scheduler.routers.triage import _rig_night_logs
        logs, note = _rig_night_logs(config, "rc16", night)
    except Exception as e:  # noqa: BLE001
        return {"night": night, "ok": False, "runs": [], "note": f"NINA logs: {e}"}
    if not logs:
        return {"night": night, "ok": False, "runs": [], "note": note}
    lines, filt = [], None
    try:
        from photonscript.scheduler.log_files import night_window
        w0, w1 = night_window(night)
    except ValueError:
        return {"night": night, "ok": False, "runs": [], "note": f"bad night {night!r}"}
    for p in logs:
        try:
            text = Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            logger.debug("center log: %s unreadable: %s", p, e)
            continue
        # a log can span two nights: keep this night's solves (local noon
        # to noon); the last filter is the newest change in the file
        lines += [x for x in parse(text)
                  if w0 <= datetime.fromisoformat(x["t_local"]) < w1]
        filt = last_filter(text) or filt
    runs = group_runs(lines, config)
    meta = {"night": night, "logs": [Path(p).name for p in logs], "last_filter": filt,
            "solves": len(lines), "runs": len(runs),
            "parsed_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z"}
    try:
        d = pointing_dir(config)
        d.mkdir(parents=True, exist_ok=True)
        tmp = night_file(config, night).with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in runs), encoding="utf-8")
        tmp.replace(night_file(config, night))
        meta_file(config, night).write_text(json.dumps(meta, indent=1), encoding="utf-8")
    except OSError as e:
        logger.warning("center log not saved for %s: %s", night, e)
    return {"night": night, "ok": True, "runs": runs, "meta": meta,
            "note": f"{len(runs)} center runs from {len(logs)} log(s)"}


def load_night(config, night: str) -> list[dict] | None:
    p = night_file(config, night)
    if not p.exists():
        return None
    out = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def load_meta(config, night: str) -> dict | None:
    try:
        return json.loads(meta_file(config, night).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def ps67_model_errors(config, night: str) -> list[dict]:
    """PS-67's RC16 mount-vs-solve offsets for a night, when its record is on
    disk (runs/<night>_pointing.jsonl; the last line per file wins). Read
    only; empty when PS-67 has not written one."""
    p = Path(getattr(config, "data_dir", ".")) / "runs" / f"{night}_pointing.jsonl"
    if not p.exists():
        return []
    by: dict = {}
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if (r.get("rig") or "rc16") != "rc16":
                continue
            by[r.get("file")] = r
    except OSError:
        return []
    out = []
    for r in by.values():
        try:
            v = float(r.get("model_err_arcmin"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(v):
            out.append({"err": v, "pier": str(r.get("pier") or "")[:1].upper() or None})
    return out


# ------------------------------------------------------------------ summary

def _pct(vals: list[float], q: float) -> float | None:
    v = sorted(vals)
    if not v:
        return None
    k = (len(v) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    return round(v[lo] + (v[hi] - v[lo]) * (k - lo), 2)


def _stats(runs: list[dict]) -> dict:
    seps = [r["first_sep_arcmin"] for r in runs if r.get("first_sep_arcmin") is not None]
    es = [r["first_east_arcmin"] for r in runs if r.get("first_east_arcmin") is not None]
    ns = [r["first_north_arcmin"] for r in runs if r.get("first_north_arcmin") is not None]
    att = [r["attempts"] for r in runs if r.get("attempts")]
    return {"n": len(seps), "median_arcmin": _pct(seps, 0.5), "p90_arcmin": _pct(seps, 0.9),
            "median_east_arcmin": _pct(es, 0.5), "median_north_arcmin": _pct(ns, 0.5),
            "median_attempts": _pct(att, 0.5)}


def _nights(end_night: str, n: int) -> list[str]:
    d = datetime.strptime(end_night, "%Y-%m-%d")
    return [(d - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


def summary(config, nights: int = 14, end_night: str | None = None,
            refresh: bool = False, parse_missing: bool = True) -> dict:
    """First-slew error over the last `nights` nights (newest first). Nights
    with no stored center file are parsed from the NINA logs when the logs
    folder exists (parse_missing); the two newest nights are re-parsed when
    their logs are newer than the stored file. Never raises."""
    from photonscript.shared import phd2_store as store
    end_night = end_night or store.night_of(config, datetime.utcnow())
    nights = max(1, min(int(nights or 14), 60))
    logs_ok = Path(str(getattr(config, "nina_logs_dir", "") or "")).is_dir()
    runs, per_night, ps67 = [], [], []
    for i, n in enumerate(_nights(end_night, nights)):
        rs = None if refresh else load_night(config, n)
        stale = False
        if rs is not None and i < 2 and logs_ok:
            try:
                f = night_file(config, n).stat().st_mtime
                stale = any(p.stat().st_mtime > f
                            for p in Path(config.nina_logs_dir).glob("*.log"))
            except OSError:
                stale = False
        if (rs is None or stale) and parse_missing and logs_ok:
            rs = night_pass(config, n).get("runs")
        rs = rs or []
        for r in rs:
            r["night"] = n
        runs += rs
        st = _stats(rs)
        me = ps67_model_errors(config, n)
        ps67 += me
        if st["n"] or me:
            per_night.append({"night": n, **st,
                              "ps67_median_arcmin": _pct([x["err"] for x in me], 0.5),
                              "ps67_n": len(me)})

    def by(key, names):
        return {name: _stats([r for r in runs if r.get(key) == name]) for name in names}
    sides = by("side", ("E", "W"))
    return {"nights": nights, "end_night": end_night, "logs_dir_found": logs_ok,
            "overall": _stats(runs), "by_side": sides,
            "by_dec": by("dec_band", [b[2] for b in DEC_BANDS]),
            "by_ha": by("ha_band", [b[2] for b in HA_BANDS]),
            "side_src": sorted({r.get("side_src") for r in runs if r.get("side_src")}),
            "per_night": per_night,
            "ps67": {"n": len(ps67), "median_arcmin": _pct([x["err"] for x in ps67], 0.5),
                     "by_pier": {p: _pct([x["err"] for x in ps67 if x["pier"] == p], 0.5)
                                 for p in ("E", "W")}},
            "runs": len(runs)}
