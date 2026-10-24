"""Per-filter twilight sky brightness model for sky flats (2026-10-24).

Why: every sky flat since 09-08 failed. The dusk run of 10-23 started at
sunset + 18 and NINA's SkyFlat read Ha ~1100 ADU at 26 s (target ~32800);
the clear dawns (10-08 / 10-13 / 10-15) started each set at nautical dawn
+ 5 with RC16 L at 2-3k ADU and the OSC at 10-12k at the 30 s cap. Fixed
offsets from sunset / nautical dawn cannot fit seven filters whose needs
differ by ~100x. This module learns, per rig and filter, how bright the
twilight sky is as a function of the sun's altitude, so the flats plan
(flat_plan.py) can start each filter when the sun reaches the altitude at
which a short exposure hits the histogram target.

Model (per rig): ln(rate) = c[filter] + b * sun_alt_deg, where rate is the
sky signal in ADU/s above the bias pedestal ((ADU - bias) / exposure). One
slope b per rig (the twilight sky's brightening, shared by its filters),
one intercept per filter (its throughput). The exposure that hits the
target ADU at sun altitude h is then
    t(h) = (target - bias) / exp(c + b h)
and the altitude at which a filter needs exposure t is
    h(t) = (ln((target - bias) / t) - c) / b.

Data: NINA's own SkyFlat lines, both rigs, dusk and dawn:
  "Exposure too dim at 15.05s. ADU measured at: 889.1"     (a probe)
  "Exposure too bright at 15.1s. ADU measured at: 57087.5" (a probe)
  "Found exposure time at 8.667s with histogram ADU 34366" (in band)
  "Adding ADU=33541.9, Exposure Time=8.2639"               (a saved flat)
with the filter from "Moving to Filter H at Position 4" / the Capture
line, the time from the line (NINA logs local time) and the sun altitude
computed here (NOAA low-precision formulas). Probes at or near saturation
(>= SATURATED_ADU) or barely above the bias (< bias + MIN_SIGNAL_ADU) are
not used for the fit. Saved FLAT frames add in-band points from their
FITS headers (EXPTIME, DATE-OBS, FILTER) where the log has none.

Store: <data_dir>/sky_flat_model.json {probes: [...], fit: {...},
history: [...]}; refresh(config, date) adds that night's probes (both
rigs) from its NINA logs and saved flats, refits and returns the change
("RC16 L: 5 s at sun -4.2 deg (was -3.6), +38 probes"). Until a rig /
filter has data, DEFAULTS (fit to the evidence of 2026-09-08..10-23, see
DEFAULTS_EVIDENCE) stand in.
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

MODEL_FILE = "sky_flat_model.json"
FULL_SCALE_ADU = 65535.0
SATURATED_ADU = 60000.0
MIN_SIGNAL_ADU = 150.0
MAX_PROBES = 6000             # kept in the store (newest)
MAX_AGE_DAYS = 240            # older probes leave the fit
FIT_ALT_RANGE = (-14.0, 4.0)  # the log-linear part of twilight
MIN_FILTER_POINTS = 3         # fewer: the filter keeps its default intercept
MIN_SLOPE_POINTS = 8          # fewer: the rig keeps its default slope
SLOPE_BOUNDS = (0.35, 1.6)    # ln units per degree (sky doubles every 0.4-2 deg)
SCAN_BYTES = 1024 * 1024 * 1024
RIGS = ("rc16", "piggyback")
RC16_FILTERS = ("L", "R", "G", "B", "Ha", "OIII", "SII")
OSC = "OSC"

# Fit to NINA's SkyFlat lines 2026-09-08 .. 2026-10-23 (DEFAULTS_EVIDENCE).
# bias: the ADU pedestal at gain 200 / offset 256: the floor the RC16's
# OIII / Ha probes settled to as the sky went dark (255.9 at 30 s, 09-08
# dusk); the Piggy-600 uses the same until its own data says otherwise.
DEFAULTS = {
    "rc16": {"slope": 0.893, "bias": 250.0,
             "filters": {"L": 11.751, "R": 10.687, "G": 11.135, "B": 11.698,
                         "Ha": 8.117, "OIII": 7.864, "SII": 9.244}},
    "piggyback": {"slope": 1.001, "bias": 250.0, "filters": {OSC: 12.703}},
}
DEFAULTS_EVIDENCE = (
    "412 NINA SkyFlat lines, 2026-09-08 .. 2026-10-23, pointing alt 85 az 200: "
    "RC16 dawns 09-08 (Ha / OIII / SII saved 23 each, 5.6-8.7 s at sun "
    "-1.3..+1.1), 10-08, 10-13, 10-15 (every set too dim at 30 s), dusks 09-08 "
    "and 10-23 (Ha / OIII / R too dim); Piggy-600 dawns 10-07, 10-08, 10-13, "
    "10-15 and dusk 10-23 (too dim). Fit with bias 250 ADU: RC16 slope 0.893 "
    "ln/deg (the sky doubles every 0.78 deg, ~3.7 min), rms 0.26; Piggy-600 "
    "slope 1.001, rms 0.33")

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)\|")
_NUM = r"(\d+(?:\.\d+)?)"
_PROBE = re.compile(r"Exposure too (dim|bright) at " + _NUM + r"s\. ADU measured at: " + _NUM)
_FOUND = re.compile(r"Found exposure time at " + _NUM + r"s with histogram ADU " + _NUM)
_ADDING = re.compile(r"Adding ADU=" + _NUM + r", Exposure Time=" + _NUM)
_MOVE = re.compile(r"Moving to Filter (\S+) at Position")
_CAPTURE_F = re.compile(r"Starting Exposure - Exposure Time: [\d.]+s; Filter: ([^;]*);")
_TARGET = re.compile(r"Determining Sky Flat Exposure Time\. Min ([\d.]+), Max ([\d.]+), "
                     r"Target (\d+)%")


# ---- the sun ---------------------------------------------------------------

def sun_alt_az(lat_deg: float, lon_deg: float, when_utc: datetime) -> tuple[float, float]:
    """(altitude, azimuth from north through east) of the sun, degrees, at
    a naive-UTC (or aware) instant. NOAA low-precision formulas (~0.1 deg)."""
    t = when_utc if when_utc.tzinfo else when_utc.replace(tzinfo=timezone.utc)
    t = t.astimezone(timezone.utc)
    doy = t.timetuple().tm_yday
    hour = t.hour + t.minute / 60 + t.second / 3600
    g = 2 * math.pi / 365 * (doy - 1 + (hour - 12) / 24)
    decl = (0.006918 - 0.399912 * math.cos(g) + 0.070257 * math.sin(g)
            - 0.006758 * math.cos(2 * g) + 0.000907 * math.sin(2 * g)
            - 0.002697 * math.cos(3 * g) + 0.00148 * math.sin(3 * g))
    eqtime = 229.18 * (0.000075 + 0.001868 * math.cos(g) - 0.032077 * math.sin(g)
                       - 0.014615 * math.cos(2 * g) - 0.040849 * math.sin(2 * g))
    tst = hour * 60 + eqtime + 4 * lon_deg
    ha = math.radians(tst / 4 - 180)
    lat = math.radians(lat_deg)
    cos_zen = (math.sin(lat) * math.sin(decl)
               + math.cos(lat) * math.cos(decl) * math.cos(ha))
    zen = math.acos(max(-1.0, min(1.0, cos_zen)))
    az = math.degrees(math.atan2(math.sin(ha), math.cos(ha) * math.sin(lat)
                                 - math.tan(decl) * math.cos(lat))) + 180.0
    return 90 - math.degrees(zen), az % 360.0


def site(config) -> tuple[float, float]:
    """(lat, lon) of the observatory (AARO when config has none)."""
    return (float(getattr(config, "observatory_lat", 31.906944)),
            float(getattr(config, "observatory_lon", -109.021367)))


def sun_alt(config, when_utc: datetime) -> float:
    return sun_alt_az(*site(config), when_utc)[0]


def time_at_alt(config, alt: float, t0: datetime, t1: datetime) -> datetime | None:
    """The first instant in [t0, t1] (naive UTC) at which the sun crosses
    `alt` (bisection on a monotonic stretch: one twilight); None when it
    does not cross inside."""
    a0, a1 = sun_alt(config, t0) - alt, sun_alt(config, t1) - alt
    if a0 == 0:
        return t0
    if a0 * a1 > 0:
        return None
    lo, hi = t0, t1
    for _ in range(40):
        mid = lo + (hi - lo) / 2
        am = sun_alt(config, mid) - alt
        if (am > 0) == (a0 > 0):
            lo = mid
        else:
            hi = mid
        if (hi - lo).total_seconds() < 1:
            break
    return lo + (hi - lo) / 2


# ---- log parsing ---------------------------------------------------------------

def _canon(config, raw: str, rig: str) -> str:
    if rig != "rc16":
        return OSC
    raw = (raw or "").strip()
    try:
        return config.reverse_filter_map().get(raw, raw) or "?"
    except Exception:  # noqa: BLE001
        return raw or "?"


def parse_lines(config, lines, rig: str, offset_hours=None) -> list[dict]:
    """SkyFlat probes from NINA log lines (str or bytes, NINA local time).
    offset_hours(local_naive) -> hours (default: the observatory's UTC
    offset at that time). Each: {t (naive UTC ISO), rig, filter, exp, adu,
    kind dim|bright|found|flat, target, alt, phase dusk|dawn}."""
    from photonscript.shared.localtime import utc_offset_hours
    lat, lon = site(config)
    out: list[dict] = []
    flt = "?"
    target = 0.5
    in_flat = False
    for raw in lines:
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        if ("SkyFlat.cs" not in line and "ChangeFilter" not in line
                and "Capture|" not in line and "FlatDevice.SkyFlat" not in line):
            continue
        m = _MOVE.search(line)
        if m:
            flt = _canon(config, m.group(1), rig)
            continue
        m = _CAPTURE_F.search(line)
        if m:
            if m.group(1).strip():
                flt = _canon(config, m.group(1), rig)
            continue
        if "Starting Category: Flat Device" in line and "SkyFlat" in line:
            in_flat = True
            continue
        m = _TARGET.search(line)
        if m:
            target = float(m.group(3)) / 100.0
            in_flat = True
            continue
        kind = exp = adu = None
        m = _PROBE.search(line)
        if m:
            kind, exp, adu = m.group(1), float(m.group(2)), float(m.group(3))
        else:
            m = _FOUND.search(line)
            if m:
                kind, exp, adu = "found", float(m.group(1)), float(m.group(2))
            else:
                m = _ADDING.search(line)
                if m:
                    kind, adu, exp = "flat", float(m.group(1)), float(m.group(2))
        if kind is None or not in_flat:
            continue
        ts = _TS.match(line)
        if not ts:
            continue
        try:
            local = datetime.fromisoformat(ts.group(1)[:19])
        except ValueError:
            continue
        off = (offset_hours(local) if offset_hours
               else utc_offset_hours(config, local + timedelta(hours=6)))
        utc = local - timedelta(hours=off)
        alt, _az = sun_alt_az(lat, lon, utc)
        out.append({"t": utc.isoformat(timespec="seconds"), "rig": rig,
                    "filter": flt if rig == "rc16" else OSC, "exp": round(exp, 4),
                    "adu": round(adu, 1), "kind": kind, "target": target,
                    "alt": round(alt, 3), "phase": "dusk" if local.hour >= 12 else "dawn"})
    return out


def parse_log_file(config, path, rig: str, lo_local: datetime | None = None,
                   hi_local: datetime | None = None) -> list[dict]:
    """Probes from one NINA log, optionally only lines stamped inside
    [lo_local, hi_local]. Reads at most SCAN_BYTES."""
    lo = lo_local.isoformat(timespec="seconds") if lo_local else ""
    hi = hi_local.isoformat(timespec="seconds") if hi_local else "9"

    def lines():
        read = 0
        with open(path, "rb") as fh:
            for raw in fh:
                read += len(raw)
                if read > SCAN_BYTES:
                    break
                stamp = raw[:19].decode("ascii", "replace")
                if stamp[:2] == "20" and (stamp < lo or stamp > hi):
                    if stamp > hi:
                        break
                    continue
                yield raw
    try:
        return parse_lines(config, lines(), rig)
    except OSError as e:
        logger.debug("sky flat model: %s unreadable: %s", path, e)
        return []


def night_probes(config, date: str) -> list[dict]:
    """Every SkyFlat probe of the night of `date` (evening + the next
    morning), both rigs, from NINA's logs on this machine."""
    out: list[dict] = []
    try:
        from photonscript.scheduler.routers.triage import _rig_night_logs
    except Exception:  # noqa: BLE001
        return out
    rigs = ["rc16"] + (["piggyback"] if getattr(config, "piggyback_enabled", False) else [])
    for rig in rigs:
        try:
            paths, _note = _rig_night_logs(config, rig, date)
        except Exception as e:  # noqa: BLE001
            logger.debug("sky flat model: no logs for %s %s: %s", rig, date, e)
            continue
        d = datetime.strptime(date, "%Y-%m-%d")
        for p in paths:
            out += parse_log_file(config, p, rig, d.replace(hour=12),
                                  d.replace(hour=12) + timedelta(days=1))
    return out


def saved_flat_points(config, date: str) -> list[dict]:
    """In-band points from the night's saved FLAT frames (FITS EXPTIME,
    DATE-OBS, FILTER; the ADU taken as the target): a set the log missed
    still teaches the model."""
    from photonscript.scheduler.dusk_flats_check import dusk_frames
    from photonscript.shared.rigs import rig_config
    lat, lon = site(config)
    out = []
    d = datetime.strptime(date, "%Y-%m-%d")
    rigs = ["rc16"] + (["piggyback"] if getattr(config, "piggyback_enabled", False) else [])
    for rig in rigs:
        try:
            rcfg = rig_config(config, rig)
            frames = dusk_frames(Path(rcfg.image_watch_dir), date,
                                 d + timedelta(hours=12), d + timedelta(hours=40))
        except Exception:  # noqa: BLE001
            continue
        for _d, f in frames:
            try:
                from astropy.io import fits
                h = fits.getheader(f)
                exp = float(h.get("EXPTIME") or h.get("EXPOSURE") or 0)
                t = datetime.fromisoformat(str(h.get("DATE-OBS"))[:19])
            except Exception:  # noqa: BLE001
                continue
            if exp <= 0:
                continue
            flt = _canon(config, str(h.get("FILTER") or ""), rig)
            alt, _ = sun_alt_az(lat, lon, t)
            out.append({"t": t.isoformat(timespec="seconds"), "rig": rig,
                        "filter": flt, "exp": round(exp, 4),
                        "adu": round(0.5 * FULL_SCALE_ADU, 1), "kind": "file",
                        "target": 0.5, "alt": round(alt, 3),
                        "phase": "dawn" if (t.hour + 24 + _utc_off(config, t)) % 24 < 12
                        else "dusk"})
    return out


def _utc_off(config, t: datetime) -> float:
    from photonscript.shared.localtime import utc_offset_hours
    return utc_offset_hours(config, t)


# ---- the fit ---------------------------------------------------------------------

def _usable(p: dict, bias: float) -> bool:
    try:
        adu, exp, alt = float(p["adu"]), float(p["exp"]), float(p["alt"])
    except (KeyError, TypeError, ValueError):
        return False
    return (exp > 0 and bias + MIN_SIGNAL_ADU <= adu < SATURATED_ADU
            and FIT_ALT_RANGE[0] <= alt <= FIT_ALT_RANGE[1]
            and p.get("filter") not in (None, "?", ""))


def fit_rig(probes: list[dict], rig: str, defaults: dict | None = None) -> dict:
    """{slope, bias, filters: {f: c}, n: {f: points}, rms} for one rig.
    The slope is pooled over its filters (within-filter regression: each
    filter's own mean removed), bounded to SLOPE_BOUNDS; a filter with
    fewer than MIN_FILTER_POINTS keeps its default intercept, a rig with
    fewer than MIN_SLOPE_POINTS its default slope."""
    base = defaults or DEFAULTS.get(rig) or DEFAULTS["rc16"]
    bias = float(base.get("bias", 250.0))
    pts: dict[str, list[tuple[float, float]]] = {}
    for p in probes:
        if p.get("rig") != rig or not _usable(p, bias):
            continue
        y = math.log((float(p["adu"]) - bias) / float(p["exp"]))
        pts.setdefault(p["filter"], []).append((float(p["alt"]), y))
    slope = float(base["slope"])
    sxx = sxy = 0.0
    total = 0
    for xs in pts.values():
        if len(xs) < 2:
            continue
        mx = sum(x for x, _ in xs) / len(xs)
        my = sum(y for _, y in xs) / len(xs)
        sxx += sum((x - mx) ** 2 for x, _ in xs)
        sxy += sum((x - mx) * (y - my) for x, y in xs)
        total += len(xs)
    if total >= MIN_SLOPE_POINTS and sxx > 1.0:
        slope = min(SLOPE_BOUNDS[1], max(SLOPE_BOUNDS[0], sxy / sxx))
    filters = dict(base.get("filters") or {})
    n = {}
    sq = 0.0
    cnt = 0
    for f, xs in pts.items():
        n[f] = len(xs)
        if len(xs) >= MIN_FILTER_POINTS:
            filters[f] = sum(y - slope * x for x, y in xs) / len(xs)
            sq += sum((y - filters[f] - slope * x) ** 2 for x, y in xs)
            cnt += len(xs)
    # filters with no data of their own follow the rig's learned level:
    # keep their default offset from the learned filters' default level
    learned = [f for f in filters if n.get(f, 0) >= MIN_FILTER_POINTS
               and f in (base.get("filters") or {})]
    if learned:
        shift = sum(filters[f] - base["filters"][f] for f in learned) / len(learned)
        for f, c in (base.get("filters") or {}).items():
            if n.get(f, 0) < MIN_FILTER_POINTS:
                filters[f] = c + shift
    return {"slope": round(slope, 4), "bias": bias,
            "filters": {f: round(c, 4) for f, c in filters.items()},
            "n": n, "rms": round(math.sqrt(sq / cnt), 3) if cnt else None}


def fit(probes: list[dict]) -> dict:
    return {rig: fit_rig(probes, rig) for rig in RIGS}


def exposure_at(rig_fit: dict, flt: str, alt: float, target_frac: float = 0.5) -> float | None:
    """Exposure (s) that hits the target at sun altitude `alt`."""
    c = (rig_fit.get("filters") or {}).get(flt)
    if c is None:
        return None
    sig = target_frac * FULL_SCALE_ADU - float(rig_fit.get("bias", 0))
    return sig / math.exp(c + float(rig_fit["slope"]) * alt)


def alt_for_exposure(rig_fit: dict, flt: str, exp_s: float,
                     target_frac: float = 0.5) -> float | None:
    """Sun altitude at which `flt` needs `exp_s` to hit the target."""
    c = (rig_fit.get("filters") or {}).get(flt)
    if c is None or exp_s <= 0:
        return None
    sig = target_frac * FULL_SCALE_ADU - float(rig_fit.get("bias", 0))
    return (math.log(sig / exp_s) - c) / float(rig_fit["slope"])


# ---- the store -----------------------------------------------------------------

def _path(config) -> Path:
    return Path(config.data_dir) / MODEL_FILE


def load(config) -> dict:
    """The stored model ({probes, fit, history, nights}); a fresh one
    (DEFAULTS, no probes) when there is none or it is unreadable."""
    try:
        data = json.loads(_path(config).read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("fit"), dict):
            data.setdefault("probes", [])
            data.setdefault("history", [])
            data.setdefault("nights", [])
            for rig in RIGS:
                data["fit"].setdefault(rig, json.loads(json.dumps(DEFAULTS[rig])))
            return data
    except (OSError, ValueError):
        pass
    return {"probes": [], "nights": [], "history": [],
            "fit": json.loads(json.dumps(DEFAULTS)), "source": "defaults"}


def save(config, data: dict) -> None:
    p = _path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(p)


def rig_fit(config, rig: str, model: dict | None = None) -> dict:
    m = model or load(config)
    return (m.get("fit") or {}).get(rig) or DEFAULTS[rig]


def _ref_exposure(config) -> float:
    lo, hi = exposure_range(config)
    return round(math.sqrt(lo * hi), 2)


def exposure_range(config, phase: str = "dawn") -> tuple[float, float]:
    """The planned exposure range of a sky flat set:
    flats_target_exposure_min_s .. flats_target_exposure_max_s (2 .. 10 s);
    at dusk up to flats_dusk_exposure_max_s (25 s) instead: the AARO roof
    opens about sunset + 15, when a 10 s window has passed for every filter
    (10-23: the OSC read 44 % of full scale at 15 s at 18:55)."""
    lo = float(getattr(config, "flats_target_exposure_min_s", 2.0) or 2.0)
    hi = float(getattr(config, "flats_target_exposure_max_s", 10.0) or 10.0)
    if phase == "dusk":
        hi = float(getattr(config, "flats_dusk_exposure_max_s", 25.0) or hi)
    lo = max(0.1, lo)
    return lo, max(lo * 1.5, hi)


def describe_change(config, old: dict, new: dict, added: dict) -> list[str]:
    """One line per rig / filter whose reference altitude moved or that
    gained points: "RC16 L: 4.5 s at sun -4.2 deg (was -3.6), +38 probes"."""
    from photonscript.shared.rigs import rig_label
    ref = _ref_exposure(config)
    out = []
    for rig in RIGS:
        o, n = old.get(rig) or DEFAULTS[rig], new.get(rig) or DEFAULTS[rig]
        for f in sorted(set((n.get("filters") or {})), key=_order):
            a0, a1 = alt_for_exposure(o, f, ref), alt_for_exposure(n, f, ref)
            k = added.get((rig, f), 0)
            if a1 is None or (k == 0 and a0 is not None and abs(a1 - a0) < 0.05):
                continue
            was = f" (was {a0:+.1f})" if a0 is not None and abs(a1 - a0) >= 0.05 else ""
            try:
                name = rig_label(config, rig)
            except Exception:  # noqa: BLE001
                name = rig
            out.append(f"{name} {f}: {ref:g} s at sun {a1:+.1f} deg{was}"
                       + (f", +{k} probes" if k else ""))
    return out


def _order(f: str) -> int:
    order = RC16_FILTERS + (OSC,)
    return order.index(f) if f in order else 99


def add_probes(config, model: dict, probes: list[dict], night: str | None = None,
               now: datetime | None = None) -> dict:
    """Merge probes (deduped by rig + time + kind + exposure), refit, keep a
    history row. Returns {"added": n, "changes": [lines]}."""
    now = now or datetime.utcnow()
    seen = {(p.get("rig"), p.get("t"), p.get("kind"), p.get("exp"))
            for p in model.get("probes", [])}
    added: dict = {}
    fresh = []
    for p in probes:
        key = (p.get("rig"), p.get("t"), p.get("kind"), p.get("exp"))
        if key in seen:
            continue
        seen.add(key)
        fresh.append(p)
        added[(p["rig"], p["filter"])] = added.get((p["rig"], p["filter"]), 0) + 1
    cutoff = (now - timedelta(days=MAX_AGE_DAYS)).isoformat()
    allp = [p for p in model.get("probes", []) + fresh if str(p.get("t")) >= cutoff]
    allp.sort(key=lambda p: str(p.get("t")))
    model["probes"] = allp[-MAX_PROBES:]
    old = model.get("fit") or DEFAULTS
    new = fit(model["probes"])
    changes = describe_change(config, old, new, added) if fresh else []
    model["fit"] = new
    model["source"] = "learned" if model["probes"] else "defaults"
    if night and night not in model.setdefault("nights", []):
        model["nights"] = (model["nights"] + [night])[-120:]
    if fresh:
        model.setdefault("history", []).append(
            {"at": now.isoformat(timespec="seconds") + "Z", "night": night,
             "added": len(fresh), "changes": changes})
        model["history"] = model["history"][-60:]
    return {"added": len(fresh), "changes": changes}


def refresh(config, date: str, *, with_files: bool = True) -> dict:
    """Learn from the night of `date`: its NINA SkyFlat lines (both rigs)
    and saved FLAT frames. Stores the model; returns {"added", "changes"}.
    Never raises."""
    try:
        probes = night_probes(config, date)
        if with_files:
            have = {(p["rig"], p["filter"]) for p in probes if p["kind"] in ("flat", "found")}
            probes += [p for p in saved_flat_points(config, date)
                       if (p["rig"], p["filter"]) not in have]
        model = load(config)
        res = add_probes(config, model, probes, night=date)
        save(config, model)
        if res["changes"]:
            logger.info("sky flat model %s: %s", date, "; ".join(res["changes"]))
        return res
    except Exception as e:  # noqa: BLE001
        logger.warning("sky flat model refresh failed: %s", e, exc_info=True)
        return {"added": 0, "changes": [], "error": str(e)}


def summary(config, model: dict | None = None) -> dict:
    """For the API / report: per rig and filter the sun altitudes at the
    min / reference / max target exposure, points and the slope."""
    m = model or load(config)
    lo, hi = exposure_range(config)
    ref = _ref_exposure(config)
    out = {"source": m.get("source", "defaults"), "probes": len(m.get("probes", [])),
           "exposure_range_s": [lo, hi], "rigs": {}, "history": m.get("history", [])[-5:]}
    for rig in RIGS:
        rf = rig_fit(config, rig, m)
        out["rigs"][rig] = {
            "slope_per_deg": rf.get("slope"), "rms": rf.get("rms"),
            "filters": {f: {"alt_at_max_exp": _r(alt_for_exposure(rf, f, hi)),
                            "alt_at_ref_exp": _r(alt_for_exposure(rf, f, ref)),
                            "alt_at_min_exp": _r(alt_for_exposure(rf, f, lo)),
                            "points": (rf.get("n") or {}).get(f, 0)}
                        for f in sorted(rf.get("filters") or {}, key=_order)}}
    return out


def _r(x):
    return None if x is None else round(x, 2)
