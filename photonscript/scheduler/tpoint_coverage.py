"""TPoint coverage check: where on the sky the pointing model has samples.

2026-10-14 (M33, unguided): subs were round from 22:00 to 01:00 MDT (RC16
ecc 0.49-0.55 at 120 s, Piggy-600 0.45-0.58 at 450 s), then from the
meridian flip on both rigs trailed (RC16 0.68-0.75, Piggy 0.77-0.89). The
TPoint base run in force (TheSky 10.5, Paramount MX, 99 samples, 22-term
super model, RMS 12.4") had every sample EAST of the meridian: after the
flip ProTrack ran on a model with no data on that side of the pier.

This module reads TPoint's raw input (``TPoint\\<run> in.dat``, the same
file tpoint_files.stats picks as the base model's data; read only) and
says which parts of the sky it covers.

Input format (TheSky64 10.5, seen 2026-10-15): caption / ``!`` comment
lines, ``:`` option lines (``:NODA``, ``:EQUAT``), the site line
``+31 54 25 2026 10 14 29.31 870.00 1300.00 0.50 0.5500 0.0065`` (latitude
d m s, date, temperature, pressure, height, humidity, wavelength, lapse
rate), then one line per sample:

    star RA (h m s)  star Dec (d m s)  scope RA (h m s)  scope Dec (d m s)  LST (h m.mmm)

The star position is the catalog (apparent) place; the scope position is
the mount's raw (uncorrected) reading.

Pier side convention (TPOINT "through the pole" notation): a German
equatorial mount reports one side of the pier with the scope Dec beyond
the pole (|Dec| > 90, e.g. +97 47 25 for a star at +82 59 47, and RA 12 h
away). In the 2026-10-14 run 94 of 99 rows are beyond the pole, every one
of them east of the meridian (HA -8.6 h to -0.2 h): beyond the pole = the
EAST-looking side (the mount's state before the meridian flip). The 5
rows under 90 sit within 0.14 h of the meridian (HA -0.14 to -0.02, alt
66-89 deg, scope Dec within 0.7 deg of the star): the mount was already on
the other (west-looking, post-flip) side for them, where the Paramount may
take either side. They are reported as "meridian zone" samples and do not
count as west coverage. The mapping is checked on every file: the side
that goes with HA < 0 among samples at least 1 h from the meridian wins;
with no such samples the 2026-10-14 convention above is assumed.

Grid: pier side x HA band x Dec band. Pier side follows the HA band in
normal tracking (east-looking for HA < 0, west-looking after the flip), so
the usable grid is the 16 cells

    HA band   east far (< -3 h), east (-3..0 h)   on the east-looking side
              west (0..3 h), west far (> 3 h)     on the west-looking side
    Dec band  < 0, 0-30, 30-60, 60+

(a sample on the "wrong" side for its HA, counterweight up, is counted in
"cross"). A cell is usable when at least USABLE_FRACTION of it (HA up to
tpoint_mapping_max_ha_h, Dec down to the horizon) is above
tpoint_coverage_min_alt (30 deg, the mapping floor). Verdict per usable
cell: empty (0 samples), thin (under tpoint_coverage_thin, 4) or ok.
Overall: east-only / west-only (one side has no samples at all), full
(every usable cell ok), partial (otherwise), none (no samples).

Top-up: every usable cell under tpoint_coverage_target (6) samples needs
the difference; the time comes from the run's measured pace (the TPoint
watcher's pace, else the file's own LST gaps, else tpoint_coverage_pace_s,
35 s per point).

Hooks: GET /api/thesky/tpoint (coverage block + text), `photonscript
tpoint-stats` (text and --json), the TPoint watcher (finish push, model
history entry, the Guiding page line via GET /api/thesky/tpoint/watch), the
morning report, the arm-time check of tonight's target windows
(window_check) and the learned unguided limits (cell_for: an empty cell is
unknown sky, no limit carried in from other regions).

Writes only <data_dir>/tpoint/coverage_latest.json.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

SIDES = ("east", "west")
HA_BANDS = (("east far", -12.0, -3.0, "east"), ("east", -3.0, 0.0, "east"),
            ("west", 0.0, 3.0, "west"), ("west far", 3.0, 12.0, "west"))
DEC_BANDS = (("<0", -90.0, 0.0), ("0-30", 0.0, 30.0), ("30-60", 30.0, 60.0),
             ("60+", 60.0, 90.01))
MERIDIAN_ZONE_H = 0.25        # within this of the meridian either side is normal
SIDE_VOTE_MIN_H = 1.0         # samples this far from the meridian decide the side
USABLE_FRACTION = 0.2
DEFAULT_PACE_S = 35.0
ARM_STEP_MIN = 10.0
ARM_WARN_MIN = 15.0           # minutes in empty / thin cells before a warning
OVERALLS = ("none", "east-only", "west-only", "partial", "full")


# ------------------------------------------------------------------ config

def _f(cfg, key: str, default: float) -> float:
    try:
        v = float(getattr(cfg, key, default))
        return v if math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


def thin_n(cfg) -> int:
    return max(1, int(_f(cfg, "tpoint_coverage_thin", 4)))


def target_n(cfg) -> int:
    return max(thin_n(cfg), int(_f(cfg, "tpoint_coverage_target", 6)))


def min_alt(cfg) -> float:
    return _f(cfg, "tpoint_coverage_min_alt", 30.0)


def max_ha(cfg) -> float:
    return min(12.0, max(3.5, _f(cfg, "tpoint_mapping_max_ha_h", 6.0)))


def arm_mode(cfg) -> str:
    m = str(getattr(cfg, "tpoint_coverage_arm_warn", "alert") or "alert").strip().lower()
    return m if m in ("alert", "observe", "off") else "alert"


# ------------------------------------------------------------------ parse

def _sexa(toks: list[str]) -> float:
    """'d m s' (or 'h m s') with the sign on the first token ('-00' too)."""
    neg = toks[0].strip().startswith("-")
    v = abs(float(toks[0])) + float(toks[1]) / 60.0 + float(toks[2]) / 3600.0
    return -v if neg else v


def parse(text: str) -> dict:
    """{"site": {...}, "options": [...], "samples": [{star_ra_h, star_dec,
    scope_ra_h, scope_dec, lst_h}], "unparsed": n} from TPOINT input data
    (TheSky64's in.dat; Outdat's "& extra" columns are ignored)."""
    site, options, samples, unparsed, caption = None, [], [], 0, None
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        if s.upper() == "END":
            break
        if s.startswith(":"):
            options.append(s[1:].strip().upper())
            continue
        if s.startswith(("!", "#")):
            continue
        toks = s.split("&")[0].split()
        try:
            [float(t) for t in toks]
        except ValueError:
            if caption is None and site is None:
                caption = s
            continue
        if site is None:
            site = toks
            continue
        if len(toks) < 14:
            unparsed += 1
            continue
        try:
            samples.append({"star_ra_h": _sexa(toks[0:3]), "star_dec": _sexa(toks[3:6]),
                            "scope_ra_h": _sexa(toks[6:9]), "scope_dec": _sexa(toks[9:12]),
                            "lst_h": float(toks[12]) + float(toks[13]) / 60.0})
        except (ValueError, IndexError):
            unparsed += 1
    lat = None
    if site and len(site) >= 3:
        try:
            lat = _sexa(site[0:3])
        except ValueError:
            lat = None
    return {"caption": caption, "options": options, "lat_deg": lat,
            "samples": samples, "unparsed": unparsed}


# ------------------------------------------------------------------ geometry

def wrap_ha(h: float) -> float:
    return ((h + 12.0) % 24.0) - 12.0


def alt_az(ha_h: float, dec: float, lat: float) -> tuple[float, float]:
    """(altitude, azimuth from north through east), degrees."""
    h, d, p = math.radians(ha_h * 15.0), math.radians(dec), math.radians(lat)
    sa = math.sin(d) * math.sin(p) + math.cos(d) * math.cos(p) * math.cos(h)
    alt = math.asin(max(-1.0, min(1.0, sa)))
    y = -math.cos(d) * math.sin(h)
    x = math.sin(d) * math.cos(p) - math.cos(d) * math.sin(p) * math.cos(h)
    az = math.degrees(math.atan2(y, x)) % 360.0
    return math.degrees(alt), az


def ha_band(ha_h: float) -> tuple:
    ha = wrap_ha(ha_h)
    for b in HA_BANDS:
        if b[1] <= ha < b[2]:
            return b
    return HA_BANDS[-1]


def dec_band(dec: float) -> tuple:
    for b in DEC_BANDS:
        if b[1] <= dec < b[2]:
            return b
    return DEC_BANDS[0] if dec < 0 else DEC_BANDS[-1]


def cell_key(side: str, ha_name: str, dec_name: str) -> str:
    return f"{side}|{ha_name}|{dec_name}"


def cell_label(ha_name: str, dec_name: str) -> str:
    return f"{ha_name} Dec {dec_name}"


def _usable(lat: float, hb: tuple, db: tuple, floor: float, ha_cap: float) -> dict:
    """Fraction of the cell above the altitude floor and the centre of
    that part (HA, Dec, alt, az)."""
    lo_h, hi_h = max(hb[1], -ha_cap), min(hb[2], ha_cap)
    lo_d, hi_d = max(db[1], lat - 90.0), min(db[2], 90.0)
    if hi_h <= lo_h or hi_d <= lo_d:
        return {"fraction": 0.0}
    n = up = 0
    sh = sd = 0.0
    steps_h, steps_d = 12, 12
    for i in range(steps_h):
        ha = lo_h + (i + 0.5) * (hi_h - lo_h) / steps_h
        for j in range(steps_d):
            dec = lo_d + (j + 0.5) * (hi_d - lo_d) / steps_d
            n += 1
            if alt_az(ha, dec, lat)[0] >= floor:
                up += 1
                sh += ha
                sd += dec
    if not up:
        return {"fraction": 0.0}
    ha, dec = sh / up, sd / up
    alt, az = alt_az(ha, dec, lat)
    return {"fraction": round(up / n, 2), "ha_h": round(ha, 1), "dec": round(dec),
            "alt": round(alt), "az": round(az)}


# ------------------------------------------------------------------ analysis

def side_convention(samples: list[dict]) -> dict:
    """Which pier side "beyond the pole" (|scope Dec| > 90) means: votes
    from samples at least SIDE_VOTE_MIN_H from the meridian."""
    agree = disagree = 0
    for s in samples:
        ha = wrap_ha(s["lst_h"] - s["star_ra_h"])
        if abs(ha) < SIDE_VOTE_MIN_H:
            continue
        beyond = abs(s["scope_dec"]) > 90.0
        if beyond == (ha < 0):
            agree += 1
        else:
            disagree += 1
    if agree == disagree == 0:
        return {"beyond_pole_side": "east", "source": "assumed (TheSky 10.5 / "
                "Paramount MX, 2026-10-14: no sample 1 h or more from the meridian)"}
    side = "east" if agree >= disagree else "west"
    return {"beyond_pole_side": side,
            "source": f"inferred: {max(agree, disagree)} of {agree + disagree} samples "
                      f"1 h or more from the meridian"}


def analyze(text: str, cfg=None, *, file: str | None = None,
            pace_s: float | None = None, pace_source: str | None = None) -> dict:
    """The coverage of one TPOINT input file (see the module doc)."""
    p = parse(text)
    smp = p["samples"]
    lat = p["lat_deg"] if p["lat_deg"] is not None else _f(cfg, "observatory_lat", 31.906944)
    floor, ha_cap, thin, tgt = min_alt(cfg), max_ha(cfg), thin_n(cfg), target_n(cfg)
    conv = side_convention(smp)
    rows = []
    for s in smp:
        ha = wrap_ha(s["lst_h"] - s["star_ra_h"])
        beyond = abs(s["scope_dec"]) > 90.0
        side = conv["beyond_pole_side"] if beyond else (
            "west" if conv["beyond_pole_side"] == "east" else "east")
        alt, az = alt_az(ha, s["star_dec"], lat)
        hb, db = ha_band(ha), dec_band(s["star_dec"])
        rows.append({"ha_h": round(ha, 3), "dec": round(s["star_dec"], 3),
                     "side": side, "beyond_pole": beyond, "alt": round(alt, 1),
                     "az": round(az, 1), "ha_band": hb[0], "dec_band": db[0],
                     "cross": side != hb[3]})
    counts: dict[str, int] = {}
    for r in rows:
        if not r["cross"]:
            k = cell_key(r["side"], r["ha_band"], r["dec_band"])
            counts[k] = counts.get(k, 0) + 1
    cells = []
    for hb in HA_BANDS:
        for db in DEC_BANDS:
            u = _usable(lat, hb, db, floor, ha_cap)
            n = counts.get(cell_key(hb[3], hb[0], db[0]), 0)
            usable = u["fraction"] >= USABLE_FRACTION
            verdict = ("ok" if n >= thin else "thin" if n else "empty") if usable else (
                "ok" if n >= thin else "below")
            cells.append({"key": cell_key(hb[3], hb[0], db[0]), "side": hb[3],
                          "ha_band": hb[0], "dec_band": db[0],
                          "ha_range": [hb[1], hb[2]], "dec_range": [db[1], min(db[2], 90.0)],
                          "label": cell_label(hb[0], db[0]), "n": n, "usable": usable,
                          "usable_fraction": u["fraction"], "verdict": verdict,
                          "center": {k: u[k] for k in ("ha_h", "dec", "alt", "az") if k in u},
                          "need": max(0, tgt - n) if usable else 0})
    by_side = {sd: sum(1 for r in rows if not r["cross"] and r["side"] == sd) for sd in SIDES}
    usable = [c for c in cells if c["usable"]]
    if not rows:
        overall = "none"
    elif by_side["east"] and not by_side["west"]:
        overall = "east-only"
    elif by_side["west"] and not by_side["east"]:
        overall = "west-only"
    elif all(c["verdict"] == "ok" for c in usable):
        overall = "full"
    else:
        overall = "partial"
    if pace_s is None:
        try:
            from photonscript.scheduler import tpoint_watch as tw
            pace_s = tw.pace_s([s["lst_h"] for s in smp])
            pace_source = "this run's LST gaps" if pace_s else None
        except Exception:  # noqa: BLE001
            pace_s = None
    if not pace_s:
        pace_s = _f(cfg, "tpoint_coverage_pace_s", DEFAULT_PACE_S)
        pace_source = "default"
    need = [c for c in usable if c["need"]]
    pts = sum(c["need"] for c in need)
    topup = {"points": pts, "cells": len(need), "pace_s": round(float(pace_s), 1),
             "pace_source": pace_source or "watcher",
             "minutes": round(pts * float(pace_s) / 60.0) if pts else 0,
             "by_side": {sd: sum(c["need"] for c in need if c["side"] == sd) for sd in SIDES},
             "per_cell": [{"label": c["label"], "side": c["side"], "n": c["n"],
                           "add": c["need"], **c["center"]} for c in need]}
    meridian = [r for r in rows if r["cross"] and abs(r["ha_h"]) < MERIDIAN_ZONE_H]
    out = {"ok": bool(rows), "file": file, "points": len(rows), "unparsed": p["unparsed"],
           "options": p["options"], "equatorial": "EQUAT" in p["options"] or not p["options"],
           "lat_deg": round(lat, 4), "convention": conv,
           "beyond_pole": sum(1 for r in rows if r["beyond_pole"]),
           "under_pole": sum(1 for r in rows if not r["beyond_pole"]),
           "by_side": by_side,
           "by_ha": {hb[0]: sum(1 for r in rows if r["ha_band"] == hb[0]) for hb in HA_BANDS},
           "cross": sum(1 for r in rows if r["cross"]),
           "meridian_zone": len(meridian),
           "ha_range": ([min(r["ha_h"] for r in rows), max(r["ha_h"] for r in rows)]
                        if rows else None),
           "cells": cells, "overall": overall,
           "empty": [c["label"] for c in usable if c["verdict"] == "empty"],
           "thin": [c["label"] for c in usable if c["verdict"] == "thin"],
           "usable_cells": len(usable),
           "thin_n": thin, "target_n": tgt, "min_alt": floor, "topup": topup}
    out["line"] = summary_line(out)
    if not rows:
        out["note"] = "no samples parsed" + (f" ({p['unparsed']} unparsed lines)"
                                             if p["unparsed"] else "")
    return out


# ------------------------------------------------------------------ text

def _side_cells(cov: dict, side: str, verdict: str) -> list[dict]:
    return [c for c in cov.get("cells") or []
            if c["usable"] and c["side"] == side and c["verdict"] == verdict]


def summary_line(cov: dict) -> str:
    if not cov.get("points"):
        return "TPoint coverage: no samples in the model's data file"
    bs = cov.get("by_side") or {}
    s = (f"TPoint coverage: {cov['overall']} ({cov['points']} points: east of the "
         f"meridian {bs.get('east', 0)}, west {bs.get('west', 0)}")
    if cov.get("cross"):
        s += (f", {cov['cross']} on the other pier side "
              + ("at the meridian" if cov.get("meridian_zone") == cov["cross"] else "(counterweight up)"))
    s += ")"
    bits = []
    for sd in SIDES:
        e, t = _side_cells(cov, sd, "empty"), _side_cells(cov, sd, "thin")
        if e or t:
            bits.append(f"{sd} " + ", ".join(
                ([f"{len(e)} empty"] if e else []) + ([f"{len(t)} thin"] if t else []))
                + " of " + str(sum(1 for c in cov['cells'] if c['usable'] and c['side'] == sd))
                + " cells")
    if bits:
        s += ": " + "; ".join(bits)
    tu = cov.get("topup") or {}
    if tu.get("points"):
        s += (f". Top-up: {tu['points']} points in {tu['cells']} cells, about "
              f"{tu['minutes']} min at {tu['pace_s']:g} s/point")
    return s


def grid_rows(cov: dict) -> list[dict]:
    """Rows for the dashboard grid: Dec bands (60+ first) x the four HA
    bands, each {n, verdict}."""
    idx = {(c["ha_band"], c["dec_band"]): c for c in cov.get("cells") or []}
    out = []
    for db in reversed(DEC_BANDS):
        out.append({"dec_band": db[0],
                    "cells": [{"ha_band": hb[0], "side": hb[3],
                               "n": idx.get((hb[0], db[0]), {}).get("n", 0),
                               "verdict": idx.get((hb[0], db[0]), {}).get("verdict", "?")}
                              for hb in HA_BANDS]})
    return out


def format_text(cov: dict) -> str:
    if not cov.get("ok"):
        return f"TPoint coverage: {cov.get('note') or 'no data file'}"
    conv = cov.get("convention") or {}
    lines = [f"TPoint coverage of {cov.get('file') or 'the data file'}: "
             f"{str(cov['overall']).upper()}",
             f"  {cov['points']} samples: beyond the pole {cov['beyond_pole']} "
             f"(= {conv.get('beyond_pole_side')}-looking side, {conv.get('source')}), "
             f"under {cov['under_pole']}"
             + (f"; {cov['meridian_zone']} on the other side within "
                f"{MERIDIAN_ZONE_H:g} h of the meridian (not counted)" if cov.get("meridian_zone") else "")
             + (f"; {cov['unparsed']} lines unparsed" if cov.get("unparsed") else ""),
             "  by HA: " + ", ".join(f"{k} {v}" for k, v in (cov.get("by_ha") or {}).items()),
             f"  cells (ok >= {cov['thin_n']}, thin < {cov['thin_n']}, '-' = under "
             f"{cov['min_alt']:g} deg alt):",
             "    Dec      " + "".join(f"{hb[0]:>11}" for hb in HA_BANDS)
             + "    (east of meridian | west, after the flip)"]
    for row in grid_rows(cov):
        cells = []
        for c in row["cells"]:
            v = c["verdict"]
            txt = "-" if v == "below" else f"{c['n']} {'EMPTY' if v == 'empty' else v}"
            cells.append(f"{txt:>11}")
        lines.append(f"    {row['dec_band']:8s} " + "".join(cells))
    tu = cov.get("topup") or {}
    if tu.get("points"):
        lines.append(f"  top-up to {cov['target_n']} per usable cell: {tu['points']} points "
                     f"(east {tu['by_side'].get('east', 0)}, west {tu['by_side'].get('west', 0)}), "
                     f"about {tu['minutes']} min at {tu['pace_s']:g} s/point ({tu['pace_source']})")
        for c in tu["per_cell"]:
            lines.append(f"    {c['label']:20s} has {c['n']:3d}, add {c['add']}"
                         + (f"  (around HA {c['ha_h']:+.1f} h Dec {c['dec']:+d}, "
                            f"alt {c['alt']} az {c['az']})" if "ha_h" in c else ""))
    else:
        lines.append(f"  every usable cell has {cov['target_n']}+ samples: no top-up needed")
    return "\n".join(lines)


def compact(cov: dict | None) -> dict | None:
    """The small form for the model history, the watcher state and the
    Guiding page."""
    if not cov or not cov.get("ok"):
        return None
    tu = cov.get("topup") or {}
    return {"overall": cov["overall"], "points": cov["points"], "file": cov.get("file"),
            "by_side": cov.get("by_side"), "empty": cov.get("empty"), "thin": cov.get("thin"),
            "topup_points": tu.get("points"), "topup_minutes": tu.get("minutes"),
            "topup_by_side": tu.get("by_side"), "pace_s": tu.get("pace_s"),
            "line": cov.get("line"), "grid": grid_rows(cov),
            "cells": [{k: c[k] for k in ("key", "side", "ha_band", "dec_band",
                                         "ha_range", "dec_range", "n", "usable", "verdict")}
                      for c in cov.get("cells") or []]}


# ------------------------------------------------------------------ files

def path(cfg) -> Path:
    return Path(getattr(cfg, "data_dir", ".")) / "tpoint" / "coverage_latest.json"


def save(cfg, cov: dict, now: datetime | None = None) -> None:
    if not cov or not cov.get("ok"):
        return
    try:
        p = path(cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {**compact(cov), "t_utc": (now or datetime.utcnow()).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "topup": cov.get("topup")}
        p.write_text(json.dumps(rec, indent=2, default=str), encoding="utf-8")
    except OSError as e:
        logger.warning("TPoint coverage not saved: %s", e)


def saved(cfg) -> dict | None:
    try:
        return json.loads(path(cfg).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _watch_pace(cfg) -> float | None:
    try:
        from photonscript.scheduler import tpoint_watch as tw
        v = ((tw.load_state(cfg) or {}).get("run") or {}).get("pace_s")
        return float(v) if v else None
    except Exception:  # noqa: BLE001
        return None


def from_stats(cfg, st: dict | None, *, save_it: bool = True) -> dict | None:
    """The coverage of the base model's data file (tpoint_files.stats'
    data_path), or None. Read only (plus coverage_latest.json)."""
    if not st or not st.get("data_path"):
        return None
    from photonscript.scheduler import tpoint_files as tf
    text = tf._read(st["data_path"])
    if not text:
        return None
    wp = _watch_pace(cfg)
    cov = analyze(text, cfg, file=st.get("data_file"), pace_s=wp,
                  pace_source="TPoint watcher" if wp else None)
    if save_it:
        save(cfg, cov)
    return cov


def current(cfg, *, refresh: bool = True) -> dict | None:
    """Compact coverage: from TPoint's files (refresh) else the saved one."""
    if refresh:
        try:
            from photonscript.scheduler import tpoint_files as tf
            cov = from_stats(cfg, tf.stats(cfg))
            if cov and cov.get("ok"):
                return compact(cov)
        except Exception as e:  # noqa: BLE001
            logger.warning("TPoint coverage not read: %s", e)
    return saved(cfg)


# ------------------------------------------------------------------ lookups

def cell_for(cov: dict | None, dec: float, ha_h: float) -> dict | None:
    """The coverage cell (compact or full) a sky position falls in on the
    side the mount is on there (east of the meridian before the flip, west
    after it)."""
    if not cov:
        return None
    hb, db = ha_band(ha_h), dec_band(dec)
    k = cell_key(hb[3], hb[0], db[0])
    return next((c for c in cov.get("cells") or [] if c.get("key") == k), None)


def uncovered(cov: dict | None, dec: float, ha_h: float) -> dict | None:
    """The cell when it has no samples (empty, or below the floor with
    none), else None. Only meaningful when the coverage is known."""
    c = cell_for(cov, dec, ha_h)
    if c and not c.get("n"):
        return c
    return None


def region_cells(cov: dict | None, dec_lo: float, dec_hi: float,
                 ha_lo: float, ha_hi: float) -> list[dict]:
    """The coverage cells overlapping an unguided-limits region (Dec band x
    HA band; its meridian band overlaps both sides)."""
    out = []
    for c in (cov or {}).get("cells") or []:
        a, b = c["ha_range"]
        d0, d1 = c["dec_range"]
        if a < ha_hi and ha_lo < b and d0 < dec_hi and dec_lo < d1:
            out.append(c)
    return out


def region_status(cov: dict | None, dec_lo, dec_hi, ha_lo, ha_hi) -> str | None:
    """"empty" (no TPoint sample in any overlapping cell), "partial" (some
    overlapping cell empty), "covered", or None (coverage unknown)."""
    cs = region_cells(cov, dec_lo, dec_hi, ha_lo, ha_hi)
    if not cs:
        return None
    if all(not c.get("n") for c in cs):
        return "empty"
    if any(not c.get("n") for c in cs):
        return "partial"
    return "covered"


# ------------------------------------------------------------------ arm check

def _fmt_min(m: float) -> str:
    m = int(round(m))
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def window_check(cfg, cov: dict | None, targets, *, step_min: float = ARM_STEP_MIN,
                 warn_min: float = ARM_WARN_MIN, now: datetime | None = None) -> dict:
    """Tonight's target windows against the coverage: minutes each target
    spends in empty / thin cells on the side the mount will be on there
    (approximate: east of the meridian before the flip, west after it).
    targets: objects or dicts with name, ra_hours, dec_degrees,
    window_start_utc, window_end_utc (naive UTC). {"warn", "targets",
    "line"}; warn when any target has warn_min or more in such cells."""
    out = {"warn": False, "targets": [], "line": None,
           "overall": (cov or {}).get("overall")}
    if not cov or not cov.get("cells"):
        out["line"] = "TPoint coverage unknown (no data file read): window check skipped"
        return out
    from photonscript.shared.pointing import alt_ha as _alt_ha
    lat = _f(cfg, "observatory_lat", 31.906944)
    lon = _f(cfg, "observatory_lon", -109.021367)

    def g(t, k):
        return t.get(k) if isinstance(t, dict) else getattr(t, k, None)
    for t in targets or []:
        if g(t, "focus_calibration") or g(t, "optics_test"):
            continue
        try:
            ra, dec = float(g(t, "ra_hours")), float(g(t, "dec_degrees"))
        except (TypeError, ValueError):
            continue
        w0 = g(t, "window_start_utc") or now
        w1 = g(t, "window_end_utc") or w0
        if isinstance(w0, str):
            w0 = datetime.fromisoformat(w0.rstrip("Z"))
        if isinstance(w1, str):
            w1 = datetime.fromisoformat(w1.rstrip("Z"))
        if w0 is None or w1 is None or w1 <= w0:
            continue
        total, bad, cells, sides = 0.0, 0.0, {}, {"east": 0.0, "west": 0.0}
        tt = w0
        while tt < w1:
            dt = min(step_min, (w1 - tt).total_seconds() / 60.0)
            alt, ha = _alt_ha(ra * 15.0, dec, tt + timedelta(minutes=dt / 2), lat, lon)
            if alt is not None and alt > 0:
                total += dt
                c = cell_for(cov, dec, ha)
                if c:
                    sides[c["side"]] += dt
                if c and c.get("verdict") in ("empty", "thin", "below") and \
                        (c.get("n") or 0) < thin_n(cfg):
                    bad += dt
                    e = cells.setdefault(c["key"], {"label": cell_label(c["ha_band"], c["dec_band"]),
                                                    "side": c["side"], "n": c.get("n") or 0,
                                                    "verdict": c.get("verdict"), "minutes": 0.0})
                    e["minutes"] += dt
            tt += timedelta(minutes=step_min)
        rec = {"target": str(g(t, "name") or "?"), "minutes": round(total),
               "minutes_uncovered": round(bad), "minutes_east": round(sides["east"]),
               "minutes_west": round(sides["west"]),
               "unguided": not bool(g(t, "start_guiding")),
               "cells": [{**v, "minutes": round(v["minutes"])} for v in cells.values()]}
        rec["warn"] = bad >= warn_min
        out["targets"].append(rec)
    warned = [r for r in out["targets"] if r["warn"]]
    out["warn"] = bool(warned)
    if warned:
        parts = []
        for r in warned:
            cs = sorted(r["cells"], key=lambda c: -c["minutes"])
            west = sum(c["minutes"] for c in cs if c["side"] == "west")
            parts.append(
                f"{r['target']} spends {_fmt_min(r['minutes_uncovered'])} of its "
                f"{_fmt_min(r['minutes'])} window in TPoint cells with "
                + ("no samples" if all(c["n"] == 0 for c in cs) else "few or no samples")
                + (" (after the flip, west of the meridian)" if west >= r["minutes_uncovered"] - 0.5
                   else "")
                + ": " + ", ".join(f"{c['label']} {c['n']} pts" for c in cs[:3]))
        tu_side = {}
        for r in warned:
            for c in r["cells"]:
                tu_side[c["side"]] = True
        line = ("TPoint coverage " + str(cov.get("overall")) + ": " + "; ".join(parts)
                + ". The pointing model extrapolates there: expect trailed unguided subs "
                  "(2026-10-14 M33 trailed after the flip on an east-only model)")
        tpp = cov.get("topup_points") or (cov.get("topup") or {}).get("points")
        tmin = cov.get("topup_minutes") or (cov.get("topup") or {}).get("minutes")
        if tpp:
            line += (f". Top-up {'/'.join(sorted(tu_side))}: TPoint run of about {tpp} "
                     f"points (~{tmin} min) fills every thin cell (photonscript tpoint-stats)")
        out["line"] = line
    else:
        out["line"] = ("TPoint coverage " + str(cov.get("overall")) + ": tonight's windows stay "
                       "in covered cells")
    return out


# ------------------------------------------------------------------ reports

def morning_line(cfg) -> str | None:
    """A line while the saved coverage is not full."""
    cov = saved(cfg)
    if not cov or cov.get("overall") in (None, "full", "none"):
        return None
    return str(cov.get("line") or "")
