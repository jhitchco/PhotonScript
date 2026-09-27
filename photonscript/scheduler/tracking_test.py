"""PS-84: unguided tracking test (TPoint + ProTrack check on the Paramount MX).

Two halves:

* ``pick_target`` chooses a field for the test: 50 to 70 deg up, close to
  the meridian but not crossing it while the ladder runs (a flip mid-test
  would mix pier sides and re-center), from the seasonal catalog plus the
  active projects. Pure math (GMST + spherical trig), no astropy or network,
  so it is instant and deterministic. Falls back to the Heart Nebula.
* ``build_report`` reads a night's sub records, keeps the tracking-test subs
  (OBJECT "Tracking test <field>", see nina_sequence_json.TRACKING_TEST_PREFIX)
  and groups them by rig, filter and exposure length: n, eccentricity, HFR,
  FWHM, stars, the PS-21 pass rates, altitude / pier side from the FITS
  headers, and the elongation direction from the PS-80 star sidecar. The
  verdict is the longest exposure that passes unguided per filter, plus a
  recommendation (run unguided at that length / keep guiding / rebuild the
  model).

Pass rule per (filter, exposure) group, from the PS-66 A/B decision rule:
"pass" = at least 75% of subs under the PS-21 eccentricity gate (and no
doubled-star tracking jump) AND the median eccentricity at least 0.05 under
the gate (0.55 at the RC16's 0.60). "marginal" = at least half pass and the
median is under the gate. Everything else "fail".
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

FALLBACK_TARGET = {"name": "Heart Nebula", "ra_hours": 2.555,
                   "dec_degrees": 61.47}
ALT_MIN, ALT_MAX = 50.0, 70.0          # preferred test altitude band
HA_MAX_H = 2.5                         # "near the meridian"
PASS_RATE = 0.75
MARGINAL_RATE = 0.5
ECC_MARGIN = 0.05                      # median must be this far under the gate
ELONG_FLOOR = 0.30                     # a star counts as elongated above this
DIRECTION_R_MIN = 0.55                 # axial resultant: one common direction

PASS, MARGINAL, FAIL = "pass", "marginal", "fail"


# ------------------------------------------------------------- ephemeris

_J2000 = datetime(2000, 1, 1, 12, 0, 0)


def lst_hours(when_utc: datetime, lon_deg: float) -> float:
    """Local sidereal time (h) from the GMST polynomial (good to ~1 s)."""
    d = (when_utc.replace(tzinfo=None) - _J2000).total_seconds() / 86400.0
    gmst = 18.697374558 + 24.06570982441908 * d
    return (gmst + lon_deg / 15.0) % 24.0


def hour_angle(ra_hours: float, when_utc: datetime, lon_deg: float) -> float:
    """Hour angle in hours, wrapped to [-12, 12): negative = east of the
    meridian (rising), positive = west (setting)."""
    ha = lst_hours(when_utc, lon_deg) - ra_hours
    return (ha + 12.0) % 24.0 - 12.0


def altitude(ra_hours: float, dec_deg: float, when_utc: datetime,
             lat_deg: float, lon_deg: float) -> float:
    ha = math.radians(hour_angle(ra_hours, when_utc, lon_deg) * 15.0)
    lat, dec = math.radians(lat_deg), math.radians(dec_deg)
    s = (math.sin(lat) * math.sin(dec)
         + math.cos(lat) * math.cos(dec) * math.cos(ha))
    return math.degrees(math.asin(max(-1.0, min(1.0, s))))


# ------------------------------------------------------------- target pick

def _candidates(projects: Iterable[Any] | None = None) -> list[dict]:
    """Active projects first (a field Jeremy cares about), then the seasonal
    catalog. Each: name, ra_hours, dec_degrees, type."""
    out, seen = [], set()
    for p in projects or []:
        t = getattr(p, "target", None) or p
        name = getattr(t, "name", None)
        ra, dec = getattr(t, "ra_hours", None), getattr(t, "dec_degrees", None)
        if name and ra is not None and dec is not None \
                and name.lower() not in seen:
            seen.add(name.lower())
            out.append({"name": name, "ra_hours": float(ra),
                        "dec_degrees": float(dec),
                        "type": str(getattr(t, "object_type", "") or ""),
                        "source": "project"})
    try:
        from photonscript.shared.astronomy import SEASONAL_TARGETS
    except Exception:  # noqa: BLE001
        SEASONAL_TARGETS = []
    for e in SEASONAL_TARGETS:
        if e["name"].lower() in seen:
            continue
        seen.add(e["name"].lower())
        out.append({"name": e["name"], "ra_hours": float(e["ra"]),
                    "dec_degrees": float(e["dec"]), "type": e.get("type", ""),
                    "source": "catalog"})
    return out


def pick_target(config, when_utc: datetime | None = None,
                duration_s: float = 3600.0,
                projects: Iterable[Any] | None = None) -> dict:
    """The field to run the tracking test on at `when_utc` (default now).

    Eligible: altitude 50 to 70 deg at the start and still above 45 at the
    end, within 2.5 h of the meridian at mid-test, and NOT crossing the
    meridian while the ladder runs (already just past it, or far enough east
    to finish first). Best = closest to the meridian at mid-test, then
    closest to 60 deg; galaxies are penalised (few stars through 3 nm Ha).
    Falls back to the Heart Nebula with its current numbers."""
    when = (when_utc or datetime.utcnow()).replace(tzinfo=None)
    lat = float(getattr(config, "observatory_lat", 31.9))
    lon = float(getattr(config, "observatory_lon", -109.0))
    dur_h = max(0.0, float(duration_s)) / 3600.0
    mid = when + timedelta(hours=dur_h / 2)
    end = when + timedelta(hours=dur_h)

    def describe(c: dict, source: str, reason: str) -> dict:
        ha0 = hour_angle(c["ra_hours"], when, lon)
        return {
            "name": c["name"], "ra_hours": round(c["ra_hours"], 4),
            "dec_degrees": round(c["dec_degrees"], 4),
            "alt_deg": round(altitude(c["ra_hours"], c["dec_degrees"], when,
                                      lat, lon), 1),
            "alt_end_deg": round(altitude(c["ra_hours"], c["dec_degrees"],
                                          end, lat, lon), 1),
            "ha_hours": round(ha0, 2),
            "sky_side": "west (past the meridian)" if ha0 >= 0
            else "east (before the meridian)",
            "for_utc": when.strftime("%Y-%m-%dT%H:%MZ"),
            "est_minutes": round(dur_h * 60),
            "source": source, "reason": reason,
        }

    best, best_score = None, None
    for c in _candidates(projects):
        ra, dec = c["ra_hours"], c["dec_degrees"]
        a0 = altitude(ra, dec, when, lat, lon)
        a1 = altitude(ra, dec, end, lat, lon)
        if not (ALT_MIN <= a0 <= ALT_MAX) or a1 < ALT_MIN - 5:
            continue
        ha0 = hour_angle(ra, when, lon)
        ha1 = ha0 + dur_h * 1.0027
        if ha0 < 0.05 and ha1 > -0.1:
            continue  # would cross (or sit on) the meridian mid-test
        ha_mid = hour_angle(ra, mid, lon)
        if abs(ha_mid) > HA_MAX_H:
            continue
        a_mid = altitude(ra, dec, mid, lat, lon)
        score = abs(ha_mid) + 0.02 * abs(a_mid - 60.0)
        if "galax" in c.get("type", "").lower():
            score += 1.0
        if best_score is None or score < best_score:
            best, best_score = c, score
    if best is not None:
        return describe(best, "auto",
                        f"{best['source']} field 50 to 70 deg up, closest to "
                        "the meridian without crossing it during the test")
    return describe({**FALLBACK_TARGET}, "fallback",
                    "no catalog field is 50 to 70 deg up near the meridian "
                    "for the whole test at this time; using the Heart Nebula")


# ------------------------------------------------------------- report

def is_tracking_test(name: Any) -> bool:
    from photonscript.shared.target_names import target_key
    return target_key(name).startswith("trackingtest")


def default_night(config, now_utc: datetime | None = None) -> str:
    """The runs-page night a test run right now belongs to (local date of
    the evening: local time minus 12 h)."""
    from photonscript.shared.localtime import to_local
    now = (now_utc or datetime.utcnow()).replace(tzinfo=None)
    return (to_local(config, now) - timedelta(hours=12)).strftime("%Y-%m-%d")


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _median(xs):
    xs = sorted(x for x in xs if x is not None)
    if not xs:
        return None
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


_HDR_KEYS = ("CENTALT", "PIERSIDE", "RA", "DEC", "OBJCTRA", "OBJCTDEC",
             "DATE-OBS", "OBJCTROT", "ROTATANG", "ROTATOR", "CROTA2",
             "CD1_1", "CD1_2", "CD2_1", "CD2_2")


def _read_header(config, date: str, rec: dict) -> dict:
    """The few FITS header cards the report uses; {} when the file is not
    reachable from this machine (best-effort, never raises)."""
    paths = []
    if rec.get("abs_path"):
        paths.append(Path(str(rec["abs_path"])))
    if rec.get("file"):
        rel = str(rec["file"]).replace("\\", "/")
        root = getattr(config, "image_watch_dir", "") \
            if (rec.get("rig") or "rc16") == "rc16" \
            else getattr(config, "piggyback_image_watch_dir", "")
        if root:
            paths.append(Path(root) / date / rel)
    for p in paths:
        try:
            if not p.exists():
                continue
            from astropy.io import fits
            h = fits.getheader(p)
            return {k: h.get(k) for k in _HDR_KEYS if h.get(k) is not None}
        except Exception as e:  # noqa: BLE001
            logger.debug("tracking-test header read skipped for %s: %s", p, e)
    return {}


def _sexa(v) -> float | None:
    """'02 33 18.2' / '+61:28:12' / 2.55 -> float (same units as written)."""
    x = _num(v)
    if x is not None:
        return x
    try:
        parts = [abs(float(t)) for t in str(v).replace(":", " ").split()]
    except ValueError:
        return None
    if not parts:
        return None
    sign = -1.0 if str(v).strip().startswith("-") else 1.0
    return sign * (parts[0] + (parts[1] if len(parts) > 1 else 0) / 60
                   + (parts[2] if len(parts) > 2 else 0) / 3600)


def _alt_ha_from_header(config, hdr: dict) -> tuple:
    """(altitude deg, hour angle h) from CENTALT, else computed from the
    mount RA/Dec and DATE-OBS. (None, None) when the header lacks them.
    NINA writes RA/DEC in degrees and OBJCTRA ('hh mm ss') / OBJCTDEC."""
    alt = _num(hdr.get("CENTALT"))
    ha = None
    ra = _num(hdr.get("RA"))
    ra = ra / 15.0 if ra is not None else _sexa(hdr.get("OBJCTRA"))
    dec = _num(hdr.get("DEC"))
    if dec is None:
        dec = _sexa(hdr.get("OBJCTDEC"))
    try:
        t = datetime.fromisoformat(str(hdr.get("DATE-OBS", ""))[:19])
    except ValueError:
        t = None
    if ra is not None and t is not None:
        lon = float(getattr(config, "observatory_lon", -109.0))
        ha = hour_angle(ra, t, lon)
        if alt is None and dec is not None:
            alt = altitude(ra, dec, t, float(getattr(config, "observatory_lat",
                                                     31.9)), lon)
    return alt, ha


def _camera_pa(hdr: dict, pa_override: float | None) -> tuple:
    """(camera position angle deg, source) or (None, None)."""
    if pa_override is not None:
        return float(pa_override), "request"
    cd = [_num(hdr.get(k)) for k in ("CD1_1", "CD1_2", "CD2_1", "CD2_2")]
    if all(v is not None for v in cd):
        return math.degrees(math.atan2(cd[1], cd[3])) % 360, "WCS"
    for k in ("CROTA2", "OBJCTROT", "ROTATANG", "ROTATOR"):
        v = _num(hdr.get(k))
        if v is not None:
            return v % 360, k
    return None, None


def _star_axis(config, date: str, rec: dict) -> dict:
    """Elongation direction of one sub from the PS-80 star sidecar: axis
    angle in the image (deg from +x, 0 to 180), R (0 = random .. 1 = every
    star stretched the same way) and the fraction stretched radially (tilt).
    Falls back to the backfill grader's ecc_pa_R / shape fields."""
    try:
        from photonscript.shared import star_table
        tbl = star_table.read(config, date, rec.get("file") or "",
                              rec.get("rig") or "rc16")
    except Exception:  # noqa: BLE001
        tbl = None
    th = (tbl or {}).get("theta") or []
    ecc = (tbl or {}).get("ecc") or []
    xs, ys = (tbl or {}).get("x") or [], (tbl or {}).get("y") or []
    w, h = (tbl or {}).get("w"), (tbl or {}).get("h")
    pts = [(t, x, y) for t, e, x, y in zip(th, ecc, xs or [None] * len(th),
                                           ys or [None] * len(th))
           if t is not None and e is not None and e > ELONG_FLOOR]
    if len(pts) >= 15:
        c2 = sum(math.cos(2 * t) for t, _, _ in pts) / len(pts)
        s2 = sum(math.sin(2 * t) for t, _, _ in pts) / len(pts)
        r = math.hypot(c2, s2)
        ang = (math.degrees(0.5 * math.atan2(s2, c2))) % 180.0
        radial = None
        if w and h and all(x is not None and y is not None for _, x, y in pts):
            cx, cy = w / 2.0, h / 2.0
            near = 0
            for t, x, y in pts:
                rad = math.atan2(y - cy, x - cx)
                d = abs(((t - rad + math.pi / 2) % math.pi) - math.pi / 2)
                near += d < math.radians(25)
            radial = near / len(pts)
        return {"axis_deg": round(ang, 1), "R": round(r, 2),
                "radial_frac": None if radial is None else round(radial, 2),
                "n_elongated": len(pts), "source": "stars"}
    if rec.get("ecc_pa_R") is not None or rec.get("shape"):
        return {"axis_deg": None, "R": _num(rec.get("ecc_pa_R")),
                "radial_frac": _num(rec.get("ecc_radial_frac")),
                "shape": rec.get("shape"), "source": "record"}
    return {}


def _scorecard_status(rec: dict, check_id: str) -> str | None:
    for row in ((rec.get("scorecard") or {}).get("rows") or []):
        if isinstance(row, (list, tuple)) and row and row[0] == check_id:
            return row[3] if len(row) > 3 else None
    return None


def _group_status(n_track_pass: int, n: int, ecc_med, ecc_max) -> str:
    if not n or ecc_med is None:
        return FAIL
    rate = n_track_pass / n
    if rate >= PASS_RATE and ecc_med <= ecc_max - ECC_MARGIN + 1e-9:
        return PASS
    if rate >= MARGINAL_RATE and ecc_med <= ecc_max + 1e-9:
        return MARGINAL
    return FAIL


def _axial_mean(angles: list[float]) -> float | None:
    a = [x for x in angles if x is not None]
    if not a:
        return None
    c = sum(math.cos(math.radians(2 * x)) for x in a)
    s = sum(math.sin(math.radians(2 * x)) for x in a)
    return round((math.degrees(0.5 * math.atan2(s, c))) % 180.0, 1)


def _axis_label(axis_deg, pa_deg) -> str | None:
    """RA or Dec for an image-frame axis, only when the camera sits within
    15 deg of square to the sky (sign and mirror conventions then do not
    matter): PA ~0/180 -> image x is RA, PA ~90/270 -> image x is Dec."""
    if axis_deg is None or pa_deg is None:
        return None
    off = pa_deg % 90.0
    if min(off, 90.0 - off) > 15.0:
        return None
    x_is_ra = round((pa_deg % 180.0) / 90.0) % 2 == 0
    along_x = min(axis_deg % 180.0, 180.0 - axis_deg % 180.0) <= 30.0
    along_y = abs(axis_deg % 180.0 - 90.0) <= 30.0
    if along_x:
        return "RA" if x_is_ra else "Dec"
    if along_y:
        return "Dec" if x_is_ra else "RA"
    return "diagonal"


def _image_axis_label(axis_deg) -> str | None:
    if axis_deg is None:
        return None
    a = axis_deg % 180.0
    if min(a, 180.0 - a) <= 30.0:
        return "image x (horizontal)"
    if abs(a - 90.0) <= 30.0:
        return "image y (vertical)"
    return "diagonal"


def group_records(config, records: list[dict], date: str = "",
                  read_headers: bool = True,
                  pa_override: float | None = None) -> list[dict]:
    """Per (rig, filter, exposure) statistics for tracking-test subs."""
    from photonscript.shared import qa_rules
    groups: dict[tuple, list[dict]] = {}
    for r in records:
        exp = _num(r.get("exp_s"))
        if exp is None:
            continue
        key = (r.get("rig") or "rc16", str(r.get("filter") or "?"),
               round(exp, 1))
        groups.setdefault(key, []).append(r)
    out = []
    order = {f: i for i, f in enumerate(("L", "R", "G", "B", "Ha", "OIII",
                                         "SII", "OSC"))}
    for (rig, flt, exp), recs in sorted(groups.items(),
                                        key=lambda kv: (kv[0][0] != "rc16",
                                                        kv[0][0],
                                                        order.get(kv[0][1], 99),
                                                        kv[0][1], kv[0][2])):
        th = qa_rules.thresholds(config, rig, recs[0].get("target"), flt)
        ecc_max = float(th["ecc_max"])
        eccs = [_num(r.get("ecc")) for r in recs]
        n_track = 0
        for r, e in zip(recs, eccs):
            jump = _scorecard_status(r, "tracking_jump") == qa_rules.FAIL
            if e is not None and e <= ecc_max + 1e-9 and not jump:
                n_track += 1
        n = len(recs)
        ecc_med = _median(eccs)
        alts, has, piers, axes = [], [], set(), []
        pa, pa_src = (pa_override, "request") if pa_override is not None \
            else (None, None)
        for r in recs:
            hdr = _read_header(config, date, r) if read_headers else {}
            a, ha = _alt_ha_from_header(config, hdr)
            alts.append(a)
            has.append(ha)
            if hdr.get("PIERSIDE"):
                piers.add(str(hdr["PIERSIDE"]))
            if pa is None:
                pa, pa_src = _camera_pa(hdr, None)
            ax = _star_axis(config, date, r) if date else {}
            if ax:
                axes.append(ax)
        axis = _axial_mean([a.get("axis_deg") for a in axes])
        r_med = _median([a.get("R") for a in axes])
        radial_med = _median([a.get("radial_frac") for a in axes])
        status = _group_status(n_track, n, ecc_med, ecc_max)
        direction = None
        if axes:
            if radial_med is not None and radial_med > 0.5:
                direction = "radial from the frame center (tilt / collimation)"
            elif r_med is not None and r_med >= DIRECTION_R_MIN:
                direction = "one common direction (tracking drift or wind)"
            elif r_med is not None:
                direction = "mixed / no common direction"
        out.append({
            "rig": rig, "filter": flt, "exp_s": exp, "n": n,
            "ecc_median": None if ecc_med is None else round(ecc_med, 3),
            "ecc_max": None if not [e for e in eccs if e is not None]
            else round(max(e for e in eccs if e is not None), 3),
            "ecc_gate": ecc_max,
            "hfr_median": _r(_median([_num(r.get("hfr")) for r in recs]), 2),
            "fwhm_median": _r(_median([_num(r.get("fwhm_arcsec"))
                                       for r in recs]), 2),
            "stars_median": _r(_median([_num(r.get("stars")) for r in recs]), 0),
            "tracking_pass": n_track,
            "tracking_pass_rate": round(n_track / n, 2) if n else None,
            "qa_pass": sum(1 for r in recs if r.get("passed_qa")),
            "qa_pass_rate": round(sum(1 for r in recs if r.get("passed_qa"))
                                  / n, 2) if n else None,
            "alt_median": _r(_median(alts), 1),
            "ha_median_h": _r(_median(has), 2),
            "pier_side": sorted(piers) or None,
            "elongation": {
                "axis_deg": axis, "image_axis": _image_axis_label(axis),
                "sky_axis": _axis_label(axis, pa),
                "camera_pa_deg": None if pa is None else round(pa, 1),
                "camera_pa_source": pa_src,
                "R_median": r_med, "radial_frac_median": radial_med,
                "direction": direction, "n_subs_measured": len(axes),
            } if axes else None,
            "status": status,
            "time_first": min((str(r.get("time") or "") for r in recs),
                              default=""),
            "reasons": sorted({str(r.get("reason")) for r in recs
                               if r.get("reason")})[:4],
        })
    return out


def _r(v, nd):
    if v is None:
        return None
    return round(v) if nd == 0 else round(v, nd)


def filter_verdicts(groups: list[dict]) -> list[dict]:
    """Per (rig, filter): the longest exposure that passes unguided. A length
    only counts if no SHORTER length failed (a pass after a fail is noise:
    seeing, wind or a cloud, and is flagged as non-monotonic)."""
    by: dict[tuple, list[dict]] = {}
    for g in groups:
        by.setdefault((g["rig"], g["filter"]), []).append(g)
    out = []
    for (rig, flt), gs in by.items():
        gs = sorted(gs, key=lambda g: g["exp_s"])
        longest, blocked, nonmono, marginal = None, False, False, None
        for g in gs:
            if g["status"] == FAIL:
                blocked = True
            elif blocked and g["status"] == PASS:
                nonmono = True
            elif not blocked and g["status"] == PASS:
                longest = g["exp_s"]
            elif not blocked and g["status"] == MARGINAL and marginal is None:
                marginal = g["exp_s"]
        short, long_ = gs[0], gs[-1]
        slope = None
        if (len(gs) > 1 and short["ecc_median"] is not None
                and long_["ecc_median"] is not None
                and long_["exp_s"] > short["exp_s"]):
            slope = round((long_["ecc_median"] - short["ecc_median"])
                          / (long_["exp_s"] - short["exp_s"]) * 100.0, 3)
        # Elongated already at a short length (<= 120 s, with longer ones
        # to compare against): tracking error grows with time, optics and
        # tilt do not, so this points away from the mount.
        static = (len(gs) > 1 and short["exp_s"] <= 120.0
                  and short["ecc_median"] is not None
                  and short["ecc_median"] > short["ecc_gate"] - ECC_MARGIN)
        out.append({
            "rig": rig, "filter": flt,
            "tested_s": [g["exp_s"] for g in gs],
            "statuses": {f"{g['exp_s']:g}": g["status"] for g in gs},
            "longest_pass_s": longest,
            "first_marginal_s": marginal,
            "non_monotonic": nonmono,
            "ecc_per_100s": slope,
            "elongated_at_shortest": bool(static),
        })
    return out


def _direction_hint(groups: list[dict]) -> str | None:
    longest = {}
    for g in groups:
        if g["rig"] == "rc16" and g.get("elongation"):
            k = g["filter"]
            if k not in longest or g["exp_s"] > longest[k]["exp_s"]:
                longest[k] = g
    parts = []
    for flt, g in longest.items():
        el = g["elongation"]
        if not el.get("direction"):
            continue
        where = el.get("sky_axis") or el.get("image_axis")
        s = f"{flt} {g['exp_s']:g} s: stars stretched in {el['direction']}"
        if el.get("axis_deg") is not None:
            s += f", axis {el['axis_deg']:g} deg in the image"
        if where:
            s += f" ({where}"
            if el.get("sky_axis") in ("RA", "Dec"):
                s += (": periodic error or the model's RA rate (ProTrack)"
                      if el["sky_axis"] == "RA" else
                      ": polar alignment or flexure")
            s += ")"
        parts.append(s)
    if not parts:
        return None
    mapped = any((g.get("elongation") or {}).get("sky_axis") in ("RA", "Dec")
                 for g in groups)
    tail = ("" if mapped
            else " Camera angle unknown, so RA vs Dec is not split: pass "
                 "?pa=<NINA plate-solve rotation> to map the axis (RA drift "
                 "= periodic error / model RA rate, Dec drift = polar "
                 "alignment or flexure).")
    return "; ".join(parts) + "." + tail


def recommend(verdicts: list[dict], groups: list[dict]) -> dict:
    rc = [v for v in verdicts if v["rig"] == "rc16"] or verdicts
    if not rc:
        return {"headline": "No tracking-test subs yet", "action": "none",
                "unguided_s": None, "detail": []}
    tested_max = max(max(v["tested_s"]) for v in rc)
    shortest = min(min(v["tested_s"]) for v in rc)
    bests = {v["filter"]: v["longest_pass_s"] for v in rc}
    detail = []
    static = [v["filter"] for v in rc if v["elongated_at_shortest"]]
    if static:
        detail.append("Stars are already elongated at the shortest length in "
                      + ", ".join(static) + ": that points at optics (tilt, "
                      "collimation) or wind, not tracking. Fix that before "
                      "judging ProTrack.")
    for v in rc:
        if v["non_monotonic"]:
            detail.append(f"{v['filter']}: a longer length passed after a "
                          "shorter one failed (seeing, wind or cloud). Rerun "
                          "that step before trusting it.")
    hint = _direction_hint(groups)
    if hint:
        detail.append(hint)
    detail.append("PS-66 rule: make unguided the default only if the unguided "
                  "pass rate at that length is at least the guided one on "
                  "the same target and hour.")
    worst = None if any(b is None for b in bests.values()) \
        else min(bests.values())
    per = ", ".join(f"{f} up to {b:g} s" if b else f"{f} none"
                    for f, b in bests.items())
    if worst is None:
        if static:
            headline = (f"Keep guiding: nothing passes unguided ({per}), and "
                        "stars are elongated even at the shortest length")
            action = "keep-guiding-fix-optics"
        else:
            headline = (f"Keep guiding: unguided fails even at {shortest:g} s "
                        f"({per}). Confirm ProTrack is ON and the TPoint model "
                        "is loaded in TheSky, check polar alignment, then "
                        "rebuild the model")
            action = "rebuild-model"
        return {"headline": headline, "action": action, "unguided_s": None,
                "per_filter": bests, "detail": detail}
    if worst >= tested_max:
        headline = (f"Run unguided at {worst:g} s: the longest tested length "
                    f"passes in every filter ({per}). Test 600 s Ha next "
                    "before switching the default")
        action = "run-unguided"
    else:
        headline = (f"Run unguided at {worst:g} s ({per}); keep PHD2 for "
                    "longer subs, or rebuild / densify the TPoint model if you "
                    "need longer unguided subs")
        action = "run-unguided-capped"
    return {"headline": headline, "action": action, "unguided_s": worst,
            "per_filter": bests, "detail": detail}


def build_report(config, date: str | None = None, records: list | None = None,
                 read_headers: bool = True,
                 pa_override: float | None = None) -> dict:
    """The tracking-test report for one night (runs-page date)."""
    date = date or default_night(config)
    if records is None:
        from photonscript.scheduler.runs import _load_subs
        records = _load_subs(config, date)
    subs = [r for r in records if is_tracking_test(r.get("target"))]
    groups = group_records(config, subs, date, read_headers=read_headers,
                           pa_override=pa_override)
    verdicts = filter_verdicts(groups)
    rec = recommend(verdicts, groups)
    if not subs:
        rec["detail"] = [f"No subs named 'Tracking test ...' in the {date} "
                         "runs log. Run the sequence from "
                         "/api/tracking-test/sequence, or pass ?date= for the "
                         "night it ran."]
    return {
        "date": date, "n_subs": len(subs),
        "targets": sorted({str(r.get("target")) for r in subs}),
        "rules": {"pass_rate": PASS_RATE, "marginal_rate": MARGINAL_RATE,
                  "ecc_margin": ECC_MARGIN,
                  "text": ("pass = at least 75% of subs under the PS-21 "
                           "eccentricity gate (no tracking jump) and the "
                           "median at least 0.05 under the gate; marginal = "
                           "at least half pass and the median is under the "
                           "gate")},
        "groups": groups, "verdicts": verdicts, "recommendation": rec,
    }


def format_report(rep: dict) -> str:
    """Plain-text rendering for the CLI."""
    lines = [f"Tracking test {rep['date']}: {rep['n_subs']} subs"
             + (f" ({', '.join(rep['targets'])})" if rep["targets"] else "")]
    if rep["groups"]:
        lines.append(f"{'rig':6s} {'filt':4s} {'exp':>5s} {'n':>2s} "
                     f"{'eccMed':>6s} {'eccMax':>6s} {'HFR':>5s} {'FWHM':>5s} "
                     f"{'stars':>5s} {'trk':>5s} {'QA':>5s} {'alt':>5s} "
                     f"{'pier':6s} status")
    for g in rep["groups"]:
        def f(v, fmt):
            return format(v, fmt) if v is not None else "-"
        lines.append(
            f"{g['rig']:6s} {g['filter']:4s} {g['exp_s']:5g} {g['n']:2d} "
            f"{f(g['ecc_median'], '6.3f')} {f(g['ecc_max'], '6.3f')} "
            f"{f(g['hfr_median'], '5.2f')} {f(g['fwhm_median'], '5.2f')} "
            f"{f(g['stars_median'], '5.0f')} "
            f"{g['tracking_pass']}/{g['n']:<3d} {g['qa_pass']}/{g['n']:<3d} "
            f"{f(g['alt_median'], '5.1f')} "
            f"{','.join(g['pier_side'] or ['-']):6s} {g['status']}")
    for v in rep["verdicts"]:
        lp = f"{v['longest_pass_s']:g} s" if v["longest_pass_s"] else "none"
        lines.append(f"  {v['rig']} {v['filter']}: longest unguided pass {lp}"
                     + (f", ecc +{v['ecc_per_100s']}/100 s"
                        if v.get("ecc_per_100s") is not None else ""))
    r = rep["recommendation"]
    lines.append("Verdict: " + r["headline"])
    for d in r.get("detail", []):
        lines.append("  - " + d)
    return "\n".join(lines)
