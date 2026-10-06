"""PS-95: RC16 per-night tilt and collimation report from the star sidecars.

Passive (no sky time): reads the PS-80 star sidecars
(``<data_dir>/stars/<night>/<rig>/<stem>.json``) that both graders already
write, and answers "is it the optics, and which part?" per sub and per night.

* ``sub_field_map(tbl, scale)``: per 3x3 zone median FWHM-eq (arcsec, from
  FWHM = 2 x HFR for a Gaussian), median eccentricity (sqrt form), the
  elongation axis and its coherence; plus a fit over the whole field of
  FWHM-eq^2 = c0 + c1 u + c2 v + c3 (u^2 + v^2) with u, v in -1..1 (3-sigma
  clipped). The fit gives the tilt (linear gradient: size and soft-side
  direction), the curvature (c3), where the sharp spot sits, and model FWHM
  at the four corners.
* ``classify(map)``: tracking / tilt / curvature / collimation / fine.
* ``night_optics(config, date)``: the subs that pass the HFR and star checks,
  preferring the PS-84 tracking-test rungs of 120 s or less (tracking error
  grows with time, optics do not), else the shortest exposure per filter,
  else all of them. Per filter and overall: medians, verdict counts, how
  many subs agree on the tilt direction, a verdict and a plain-language
  recommendation. Cached at ``<data_dir>/optics/<date>.json``.

Orientation: zone names follow the runs-page thumbnail (not flipped), so
array row 0 / y = 0 is the TOP of the image. Directions are in that view.

The axial (mod 180 deg) direction math used by the PS-84 tracking test lives
here too (``axial_stats`` / ``axial_mean``), one formula for both reports.

Eccentricity: sidecars store sqrt(1-(b/a)^2) (backfill sidecars from before
PS-94 held 1-b/a); ``_ecc_sqrt`` converts by the sidecar's ``ecc_def`` with
shared.star_shape.to_sqrt.
"""

from __future__ import annotations

import json
import logging
import math
from collections import Counter
from pathlib import Path

logger = logging.getLogger(__name__)

ELONG_FLOOR = 0.30         # PS-84: a star counts as elongated above this
OPTICS_ELONG_FLOOR = 0.40  # same, sqrt-form ecc, for the optics map
DIRECTION_R_MIN = 0.55     # axial resultant: one common direction
RADIAL_DEG = 25.0          # stretch within this of the radial = radial
TRACK_RADIAL_MAX = 0.40    # tracking: radial share under this
SHARP_OFFSET_MIN = 0.25    # tilt: sharp spot this far off center (of the
                           # half-diagonal)
FINE_TOL = 0.15            # fine: corners within 15% of the center
FINE_ECC = 0.45            # fine: center ecc under this
COLLIM_ECC = 0.50          # collimation: center ecc at least this
CURVE_MIN = 1.15           # curvature: mean corner / center at least this
SHORT_EXP_S = 120.0        # PS-84 rungs this short separate optics from
                           # tracking
MIN_GROUP = 3              # fewest subs in the shortest-exposure group
MIN_FIT_STARS = 40
DIR_AGREE = 0.60           # tilt direction "stable" at this share

ZONES = (("TL", "TC", "TR"), ("ML", "C", "MR"), ("BL", "BC", "BR"))
CORNERS = ("TL", "TR", "BL", "BR")
CORNER_UV = {"TL": (-1.0, -1.0), "TR": (1.0, -1.0),
             "BL": (-1.0, 1.0), "BR": (1.0, 1.0)}
CORNER_WORDS = {"TL": "upper-left", "TR": "upper-right",
                "BL": "lower-left", "BR": "lower-right"}
# 8 directions in the image view (y down): 0 deg = right, 90 = bottom
SECTORS = ("right", "lower-right", "bottom", "lower-left", "left",
           "upper-left", "top", "upper-right")

TRACKING, TILT, CURVATURE, COLLIMATION, FINE = (
    "tracking", "tilt", "curvature", "collimation", "fine")
UNCLEAR, NO_DATA, MIXED = "unclear", "no-data", "mixed"
_VERDICT_ORDER = (TILT, TRACKING, COLLIMATION, CURVATURE, FINE, MIXED,
                  UNCLEAR, NO_DATA)


# ------------------------------------------------------------- helpers

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


def _r(v, nd=2):
    return None if v is None else round(v, nd)


def _ecc_sqrt(e, ecc_def: str | None):
    """Sidecar ecc in sqrt(1-(b/a)^2) form. "1-b/a" (old backfill) converts;
    the live form and unknown pass through (shared.star_shape.to_sqrt)."""
    from photonscript.shared.star_shape import to_sqrt
    e = _num(e)
    if e is None:
        return None
    return to_sqrt(e, ecc_def)


# ------------------------------------------------------------- axial math

def axial_stats(theta, ecc, xs=None, ys=None, w=None, h=None,
                floor: float = ELONG_FLOOR, min_n: int = 15) -> dict:
    """Elongation direction of one star list (moved from
    tracking_test._star_axis, PS-95): axis angle in the image (deg from +x,
    0 to 180), R (0 = random .. 1 = every star stretched the same way) and
    the fractions stretched radially / tangentially to the frame center.
    Only stars with ecc above `floor` count. {} under `min_n` such stars."""
    theta = list(theta or [])
    ecc = list(ecc or [])
    xs = list(xs or []) or [None] * len(theta)
    ys = list(ys or []) or [None] * len(theta)
    pts = [(t, x, y) for t, e, x, y in zip(theta, ecc, xs, ys)
           if t is not None and e is not None and e > floor]
    if len(pts) < min_n:
        return {}
    c2 = sum(math.cos(2 * t) for t, _, _ in pts) / len(pts)
    s2 = sum(math.sin(2 * t) for t, _, _ in pts) / len(pts)
    r = math.hypot(c2, s2)
    ang = (math.degrees(0.5 * math.atan2(s2, c2))) % 180.0
    radial = tangential = None
    if w and h and all(x is not None and y is not None for _, x, y in pts):
        cx, cy = w / 2.0, h / 2.0
        near = far = 0
        for t, x, y in pts:
            rad = math.atan2(y - cy, x - cx)
            d = abs(((t - rad + math.pi / 2) % math.pi) - math.pi / 2)
            near += d < math.radians(RADIAL_DEG)
            far += d > math.radians(90.0 - RADIAL_DEG)
        radial = near / len(pts)
        tangential = far / len(pts)
    return {"axis_deg": round(ang, 1), "R": round(r, 2),
            "radial_frac": None if radial is None else round(radial, 2),
            "tangential_frac": None if tangential is None
            else round(tangential, 2),
            "n_elongated": len(pts)}


def axial_mean(angles) -> float | None:
    """Mean of axial angles (deg, mod 180)."""
    a = [x for x in angles if x is not None]
    if not a:
        return None
    c = sum(math.cos(math.radians(2 * x)) for x in a)
    s = sum(math.sin(math.radians(2 * x)) for x in a)
    return round((math.degrees(0.5 * math.atan2(s, c))) % 180.0, 1)


def _axial_diff(a, b) -> float:
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def sector(angle_deg: float) -> str:
    """8-way image direction (y down) for an angle from +x."""
    return SECTORS[int(((angle_deg % 360.0) + 22.5) // 45.0) % 8]


def _sector_close(a: str, b: str) -> bool:
    """Same or neighbouring 45 deg sector."""
    if a not in SECTORS or b not in SECTORS:
        return False
    d = abs(SECTORS.index(a) - SECTORS.index(b)) % 8
    return min(d, 8 - d) <= 1


# ------------------------------------------------------------- per sub

def _fit(us, vs, f2, iters: int = 3):
    """Least squares f2 = c0 + c1 u + c2 v + c3 (u^2+v^2), 3-sigma clipped.
    Returns (coeffs, n_used) or (None, 0)."""
    import numpy as np
    u, v, y = np.asarray(us, float), np.asarray(vs, float), np.asarray(f2, float)
    keep = np.isfinite(y)
    coef = None
    for _ in range(iters + 1):
        if int(keep.sum()) < MIN_FIT_STARS:
            return None, 0
        A = np.column_stack([np.ones(int(keep.sum())), u[keep], v[keep],
                             u[keep] ** 2 + v[keep] ** 2])
        coef, *_ = np.linalg.lstsq(A, y[keep], rcond=None)
        model = (coef[0] + coef[1] * u + coef[2] * v
                 + coef[3] * (u ** 2 + v ** 2))
        res = y - model
        sd = float(np.std(res[keep]))
        new = np.isfinite(y) & (np.abs(res) <= 3.0 * sd if sd > 0 else True)
        if bool((new == keep).all()):
            break
        keep = new
    return [float(c) for c in coef], int(keep.sum())


def sub_field_map(tbl: dict | None, scale: float,
                  min_zone: int = 8) -> dict:
    """Field map of one sub from its star sidecar. `scale` = arcsec/px.
    {"ok": False, "why": ...} when the sidecar cannot support it."""
    if not tbl:
        return {"ok": False, "why": "no star sidecar"}
    xs, ys = tbl.get("x") or [], tbl.get("y") or []
    hfr, ecc_raw = tbl.get("hfr") or [], tbl.get("ecc") or []
    th = tbl.get("theta") or [None] * len(xs)
    ecc_def = tbl.get("ecc_def") or ""
    stars = []
    for i in range(min(len(xs), len(ys), len(hfr), len(ecc_raw))):
        x, y, hf = _num(xs[i]), _num(ys[i]), _num(hfr[i])
        if x is None or y is None or hf is None or hf <= 0:
            continue
        stars.append((x, y, 2.0 * hf * scale, _ecc_sqrt(ecc_raw[i], ecc_def),
                      _num(th[i]) if i < len(th) else None))
    if not stars:
        return {"ok": False, "why": "no stars in the sidecar"}
    w = _num(tbl.get("w")) or max(s[0] for s in stars) + 1.0
    h = _num(tbl.get("h")) or max(s[1] for s in stars) + 1.0
    hw, hh = w / 2.0, h / 2.0

    zone_stars: dict[str, list] = {z: [] for row in ZONES for z in row}
    for s in stars:
        zx = min(2, max(0, int(s[0] / w * 3)))
        zy = min(2, max(0, int(s[1] / h * 3)))
        zone_stars[ZONES[zy][zx]].append(s)
    zones = {}
    for name, ss in zone_stars.items():
        ok = len(ss) >= min_zone
        ax = axial_stats([s[4] for s in ss], [s[3] for s in ss],
                         floor=OPTICS_ELONG_FLOOR, min_n=5) if ok else {}
        zones[name] = {
            "n": len(ss), "ok": ok,
            "fwhm": _r(_median([s[2] for s in ss]), 2) if ok else None,
            "ecc": _r(_median([s[3] for s in ss]), 3) if ok else None,
            "axis_deg": ax.get("axis_deg"), "R": ax.get("R"),
        }
    n_ok = sum(1 for z in zones.values() if z["ok"])
    if not zones["C"]["ok"] or sum(zones[c]["ok"] for c in CORNERS) < 3:
        return {"ok": False, "why": f"too few stars per zone (need "
                f"{min_zone} in the center and 3 corners)",
                "n_stars": len(stars), "zones": zones}

    coef, n_fit = _fit([(s[0] - hw) / hw for s in stars],
                       [(s[1] - hh) / hh for s in stars],
                       [s[2] ** 2 for s in stars])
    out = {"ok": True, "n_stars": len(stars), "n_zones_ok": n_ok,
           "w": int(w), "h": int(h), "zones": zones,
           "ecc_def": ecc_def or None}
    if coef is not None:
        c0, c1, c2, c3 = coef

        def fw(u, v):
            return math.sqrt(max(1e-6, c0 + c1 * u + c2 * v
                                 + c3 * (u * u + v * v)))
        center = fw(0.0, 0.0)
        corner = {k: fw(*uv) for k, uv in CORNER_UV.items()}
        soft = max(corner, key=corner.get)
        sharp = min(corner, key=corner.get)
        gx, gy = c1 / hw, c2 / hh            # d(FWHM^2)/dpx, image axes
        grad_dir = math.degrees(math.atan2(gy, gx)) % 360.0
        if c3 > 1e-9:
            u0, v0 = -c1 / (2 * c3), -c2 / (2 * c3)
            off = math.hypot(u0 * hw, v0 * hh) / math.hypot(hw, hh)
        else:
            u0 = v0 = off = None              # no minimum inside: an edge
        out.update({
            "fit": {"c": [round(c, 4) for c in coef], "n": n_fit},
            "center_fwhm": round(center, 2),
            "corner_fwhm": {k: round(v, 2) for k, v in corner.items()},
            "corner_ratio": round(corner[soft] / corner[sharp], 3),
            "corner_mean_ratio": round(
                math.sqrt(max(1e-6, c0 + 2 * c3)) / center, 3),
            "max_corner_ratio": round(corner[soft] / center, 3),
            "soft_corner": soft, "sharp_corner": sharp,
            "tilt_dir_deg": round(grad_dir, 1),
            "tilt_dir": sector(grad_dir),
            "sharp_spot": None if u0 is None else
            {"u": round(u0, 2), "v": round(v0, 2)},
            "sharp_offset": None if off is None else round(off, 2),
        })
    else:
        out["fit"] = None
    ax = axial_stats([s[4] for s in stars], [s[3] for s in stars],
                     [s[0] for s in stars], [s[1] for s in stars], w, h,
                     floor=OPTICS_ELONG_FLOOR)
    out["elongation"] = ax or None
    out["center_ecc"] = zones["C"]["ecc"]
    return out


def classify(m: dict, tilt_warn: float = 1.20) -> dict:
    """Verdict for one sub's field map: tracking / tilt / curvature /
    collimation / fine, else unclear. Returns {"verdict", "why"}."""
    if not m or not m.get("ok") or m.get("fit") is None:
        return {"verdict": NO_DATA, "why": (m or {}).get("why")
                or "no field fit"}
    el = m.get("elongation") or {}
    R, radial = el.get("R"), el.get("radial_frac")
    cz = (m.get("zones") or {}).get("C") or {}
    c_ecc = m.get("center_ecc")
    # tracking: one direction across the frame, center included, not radial
    if R is not None and R >= DIRECTION_R_MIN \
            and (radial is None or radial < TRACK_RADIAL_MAX):
        centre_agrees = (cz.get("axis_deg") is None
                         or el.get("axis_deg") is None
                         or _axial_diff(cz["axis_deg"], el["axis_deg"]) <= 30)
        if centre_agrees:
            return {"verdict": TRACKING,
                    "why": f"one common stretch direction (R {R:.2f}, "
                           f"radial {radial if radial is not None else '-'})"}
    ratio = m.get("corner_ratio") or 1.0
    off = m.get("sharp_offset")
    if ratio >= tilt_warn and (off is None or off > SHARP_OFFSET_MIN):
        return {"verdict": TILT,
                "why": f"{CORNER_WORDS[m['soft_corner']]} corner "
                       f"{(ratio - 1) * 100:.0f}% softer than "
                       f"{CORNER_WORDS[m['sharp_corner']]}, soft side "
                       f"{m['tilt_dir']}"}
    mean_r = m.get("corner_mean_ratio") or 1.0
    if mean_r >= CURVE_MIN and ratio < tilt_warn:
        return {"verdict": CURVATURE,
                "why": f"all corners about {(mean_r - 1) * 100:.0f}% softer "
                       "than the center, evenly"}
    if c_ecc is not None and c_ecc >= COLLIM_ECC \
            and (R is None or R < DIRECTION_R_MIN):
        return {"verdict": COLLIMATION,
                "why": f"center stars elongated (ecc {c_ecc:.2f}) with no "
                       "common direction"}
    if (m.get("max_corner_ratio") or 9) < 1.0 + FINE_TOL \
            and (c_ecc is None or c_ecc < FINE_ECC):
        return {"verdict": FINE, "why": "corners within 15% of the center"}
    return {"verdict": UNCLEAR, "why": "no single signature"}


# ------------------------------------------------------------- per night

def _scorecard_status(rec: dict, check_id: str) -> str | None:
    for row in ((rec.get("scorecard") or {}).get("rows") or []):
        if isinstance(row, (list, tuple)) and row and row[0] == check_id:
            return row[3] if len(row) > 3 else None
    return None


def usable(rec: dict, config) -> bool:
    """A sub that passes the HFR and star checks (so its stars are stars
    and in focus); everything else about it may have failed."""
    rows = (rec.get("scorecard") or {}).get("rows")
    if rows:
        return not any(_scorecard_status(rec, c) == "fail"
                       for c in ("hfr", "hfr_rel", "stars"))
    hfr, stars = _num(rec.get("hfr")), _num(rec.get("stars"))
    if hfr is None or stars is None:
        return False
    return (hfr <= float(getattr(config, "quality_hfr_abs_max", 10.0))
            and stars >= float(getattr(config, "quality_star_min", 5)))


def select_subs(records: list[dict]) -> tuple[list[dict], dict]:
    """Per filter: PS-84 tracking-test rungs of 120 s or less, else the
    shortest exposure (if at least MIN_GROUP subs), else all.
    Returns (subs, {filter: {"basis", "exp_s", "tracking_mixed"}})."""
    from photonscript.scheduler.tracking_test import is_tracking_test
    by: dict[str, list] = {}
    for r in records:
        if _num(r.get("exp_s")) is None:
            continue
        by.setdefault(str(r.get("filter") or "?"), []).append(r)
    out, basis = [], {}
    for flt, rs in by.items():
        tt = [r for r in rs if is_tracking_test(r.get("target"))
              and _num(r["exp_s"]) <= SHORT_EXP_S]
        if tt:
            pick, why = tt, f"PS-84 tracking-test rungs <= {SHORT_EXP_S:g} s"
        else:
            shortest = min(_num(r["exp_s"]) for r in rs)
            grp = [r for r in rs if _num(r["exp_s"]) == shortest]
            if len(grp) >= MIN_GROUP or len(grp) == len(rs):
                pick, why = grp, f"shortest exposure ({shortest:g} s)"
            else:
                pick, why = rs, "all subs (too few at the shortest length)"
        exps = sorted({_num(r["exp_s"]) for r in pick})
        basis[flt] = {"basis": why, "exp_s": exps,
                      "tracking_mixed": max(exps) > SHORT_EXP_S}
        out.extend(pick)
    return out, basis


def _summarize(items: list[dict], tilt_warn: float) -> dict:
    """Verdict + numbers for a list of measured subs ({"map", "verdict"})."""
    measured = [i for i in items if i["verdict"] != NO_DATA]
    n = len(measured)
    counts = Counter(i["verdict"] for i in measured)
    if not n:
        return {"n_measured": 0, "n_subs": len(items), "verdict": NO_DATA,
                "counts": {}, "zones": None}
    real = [(v, c) for v, c in counts.items() if v not in (UNCLEAR,)]
    real.sort(key=lambda vc: (-vc[1], _VERDICT_ORDER.index(vc[0])))
    top, top_n = real[0] if real else (UNCLEAR, counts.get(UNCLEAR, 0))
    verdict = top if top_n * 2 >= n else MIXED
    maps = [i["map"] for i in measured]
    tilt = {}
    dirs = [m.get("tilt_dir") for m in maps if m.get("tilt_dir")]
    # tilt-sized corner ratio, whatever the verdict (trailing can win the
    # verdict on long subs while one side is still soft underneath)
    tilt_subs = [i["map"] for i in measured if i["verdict"] == TILT
                 or (i["map"].get("corner_ratio") or 0) >= tilt_warn]
    if tilt_subs:
        mode = Counter(m["tilt_dir"] for m in tilt_subs).most_common(1)[0][0]
        agree = sum(1 for d in dirs if _sector_close(d, mode))
        soft = Counter(m["soft_corner"] for m in tilt_subs).most_common(1)[0][0]
        sharp = Counter(m["sharp_corner"] for m in tilt_subs
                        if m["soft_corner"] == soft).most_common(1)[0][0]
        tilt = {"direction": mode, "agree": agree, "n": n,
                "share": round(agree / n, 2),
                "stable": agree / n >= DIR_AGREE, "n_soft": len(tilt_subs),
                "soft_corner": soft, "sharp_corner": sharp,
                "ratio_median": _r(_median([m["corner_ratio"]
                                            for m in tilt_subs]), 3)}
        if verdict == TILT and not tilt["stable"]:
            verdict = MIXED
    # zone grid: medians across subs, FWHM also as a ratio to each sub's
    # own center so filters and seeing changes do not smear it
    zones = {}
    for row in ZONES:
        for z in row:
            fw = [m["zones"][z]["fwhm"] for m in maps
                  if m["zones"][z]["fwhm"] is not None]
            rat = [m["zones"][z]["fwhm"] / m["zones"]["C"]["fwhm"]
                   for m in maps if m["zones"][z]["fwhm"] is not None
                   and m["zones"]["C"]["fwhm"]]
            zones[z] = {
                "fwhm": _r(_median(fw), 2), "ratio": _r(_median(rat), 3),
                "ecc": _r(_median([m["zones"][z]["ecc"] for m in maps]), 3),
                "axis_deg": axial_mean([m["zones"][z]["axis_deg"]
                                        for m in maps]),
                "R": _r(_median([m["zones"][z]["R"] for m in maps]), 2),
                "n_subs": len(fw)}
    el = [m.get("elongation") or {} for m in maps]
    return {
        "n_subs": len(items), "n_measured": n, "verdict": verdict,
        "counts": dict(counts),
        "center_fwhm": _r(_median([m.get("center_fwhm") for m in maps]), 2),
        "center_ecc": _r(_median([m.get("center_ecc") for m in maps]), 3),
        "corner_ratio": _r(_median([m.get("corner_ratio") for m in maps]), 3),
        "corner_mean_ratio": _r(_median([m.get("corner_mean_ratio")
                                         for m in maps]), 3),
        "sharp_offset": _r(_median([m.get("sharp_offset") for m in maps]), 2),
        "R": _r(_median([e.get("R") for e in el]), 2),
        "radial_frac": _r(_median([e.get("radial_frac") for e in el]), 2),
        "tangential_frac": _r(_median([e.get("tangential_frac")
                                       for e in el]), 2),
        "axis_deg": axial_mean([e.get("axis_deg") for e in el]),
        "tilt": tilt or None, "zones": zones,
    }


def recommendation(s: dict, tracking_mixed: bool = False) -> str:
    """Plain-language advice for a summary from _summarize."""
    v, n = s.get("verdict"), s.get("n_measured") or 0
    c = s.get("counts") or {}
    tail = (" These subs are longer than 120 s, so tracking can hide or mimic "
            "optics; the PS-84 short rungs separate them."
            if tracking_mixed else "")
    if v == NO_DATA:
        return ("No star sidecars to measure for this night (PS-80 sidecars "
                "are written as subs are graded).")
    if v == TILT:
        t = s["tilt"]
        return (f"Tilt: {CORNER_WORDS[t['soft_corner']]} "
                f"{(t['ratio_median'] - 1) * 100:.0f}% softer than "
                f"{CORNER_WORDS[t['sharp_corner']]} in {c.get(TILT, 0)} of {n} "
                f"subs, direction stable ({t['agree']} of {n} toward the "
                f"{t['direction']}). Check the camera tilt plate or spacer on "
                f"the {t['direction']} side. Confirm with the NINA Aberration "
                "Inspector before the site visit." + tail)
    if v == TRACKING:
        t = s.get("tilt") or {}
        under = ""
        if t.get("stable"):
            under = (f" Underneath it the {CORNER_WORDS[t['soft_corner']]} "
                     f"corner is {(t['ratio_median'] - 1) * 100:.0f}% softer "
                     f"than the {CORNER_WORDS[t['sharp_corner']]} in "
                     f"{t['n_soft']} of {n} subs: possible tilt, check it on "
                     "short or well-guided subs.")
        return (f"Stars stretched one way across the whole frame, center "
                f"included, in {c.get(TRACKING, 0)} of {n} subs: tracking or "
                "wind, not the optics. See the Guiding section and the PS-84 "
                "tracking test." + under + tail)
    if v == CURVATURE:
        return (f"Field curvature only: corners about "
                f"{((s.get('corner_mean_ratio') or 1) - 1) * 100:.0f}% softer "
                "than the center, evenly. Expected on an RC16 with no "
                "flattener; not a fault." + tail)
    if v == COLLIMATION:
        return (f"Possible collimation: center stars elongated (ecc "
                f"{s.get('center_ecc')}) with no common direction in "
                f"{c.get(COLLIMATION, 0)} of {n} subs. Weak signal from star "
                "shapes alone: confirm with a defocused star or the "
                "Aberration Inspector before touching the secondary." + tail)
    if v == FINE:
        return (f"Optics look fine: corners within 15% of the center, center "
                f"ecc {s.get('center_ecc')} ({c.get(FINE, 0)} of {n} subs)."
                + tail)
    parts = ", ".join(f"{k} {c[k]}" for k in _VERDICT_ORDER if c.get(k))
    extra = ""
    if s.get("tilt") and not s["tilt"]["stable"]:
        extra = (f" Tilt-like subs do not agree on a direction "
                 f"({s['tilt']['agree']} of {n} toward the "
                 f"{s['tilt']['direction']}), so it is not a fixed tilt.")
    return f"No consistent optics signature ({parts}).{extra}" + tail


def cache_path(config, date: str, rig: str = "rc16") -> Path:
    name = f"{date}.json" if (rig or "rc16") == "rc16" else f"{date}_{rig}.json"
    return Path(getattr(config, "data_dir", ".")) / "optics" / name


def _subs_signature(config, date: str) -> list:
    try:
        from photonscript.scheduler.runs import runs_dir
        st = (runs_dir(config) / f"{date}_subs.jsonl").stat()
        return [st.st_mtime_ns, st.st_size]
    except OSError:
        return [0, 0]


def night_optics(config, date: str, rig: str = "rc16",
                 records: list | None = None, write_cache: bool = True) -> dict:
    """The optics report for one night (runs-page date)."""
    from photonscript.shared import star_table
    from photonscript.shared.rigs import rig_config
    sig = _subs_signature(config, date)
    if records is None:
        from photonscript.scheduler.runs import _load_subs
        records = _load_subs(config, date)
    rcfg = rig_config(config, rig)
    scale = float(getattr(rcfg, "pixel_scale_arcsec", 0.236) or 0.236)
    min_zone = int(getattr(config, "optics_min_stars_zone", 8) or 8)
    tilt_warn = float(getattr(config, "optics_tilt_warn", 1.20) or 1.20)
    mine = [r for r in records if (r.get("rig") or "rc16") == rig]
    ok = [r for r in mine if usable(r, config)]
    picked, basis = select_subs(ok)
    items, by_f = [], {}
    for r in picked:
        tbl = star_table.read(config, date, r.get("file") or "", rig)
        m = sub_field_map(tbl, scale, min_zone)
        c = classify(m, tilt_warn)
        it = {"map": m, "verdict": c["verdict"], "why": c["why"], "rec": r}
        items.append(it)
        by_f.setdefault(str(r.get("filter") or "?"), []).append(it)
    order = {f: i for i, f in enumerate(("L", "R", "G", "B", "Ha", "OIII",
                                         "SII"))}
    filters = []
    for flt in sorted(by_f, key=lambda f: (order.get(f, 99), f)):
        s = _summarize(by_f[flt], tilt_warn)
        s.update({"filter": flt, **basis.get(flt, {})})
        s["recommendation"] = recommendation(s, s.get("tracking_mixed", False))
        filters.append(s)
    overall = _summarize(items, tilt_warn)
    mixed = any(b.get("tracking_mixed") for b in basis.values())
    overall["tracking_mixed"] = mixed
    overall["recommendation"] = recommendation(overall, mixed)
    subs = [{
        "file": i["rec"].get("file"), "filter": i["rec"].get("filter"),
        "exp_s": i["rec"].get("exp_s"), "time": i["rec"].get("time"),
        "verdict": i["verdict"], "why": i["why"],
        "corner_ratio": i["map"].get("corner_ratio"),
        "soft_corner": i["map"].get("soft_corner"),
        "tilt_dir": i["map"].get("tilt_dir"),
        "center_fwhm": i["map"].get("center_fwhm"),
        "center_ecc": i["map"].get("center_ecc"),
        "sharp_offset": i["map"].get("sharp_offset"),
        "R": (i["map"].get("elongation") or {}).get("R"),
        "radial_frac": (i["map"].get("elongation") or {}).get("radial_frac"),
    } for i in items]
    rep = {
        "v": 1, "date": date, "rig": rig, "pixel_scale": scale,
        "n_records": len(mine), "n_usable": len(ok), "n_selected": len(picked),
        "headline": overall["recommendation"], "overall": overall,
        "filters": filters, "subs": subs, "sig": sig,
        "thresholds": {"tilt_warn": tilt_warn, "min_stars_zone": min_zone,
                       "fine_tol": FINE_TOL, "collimation_ecc": COLLIM_ECC,
                       "short_exp_s": SHORT_EXP_S},
        "orientation": "zones as the runs-page thumbnail shows them: "
                       "row 0 is the top of the image",
    }
    if write_cache and mine:
        try:
            p = cache_path(config, date, rig)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(rep, separators=(",", ":"), default=str),
                         encoding="utf-8")
        except OSError as e:
            logger.debug("optics cache write skipped for %s: %s", date, e)
    return rep


def load_cached(config, date: str, rig: str = "rc16") -> dict | None:
    try:
        return json.loads(cache_path(config, date, rig).read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return None


def night_optics_cached(config, date: str, rig: str = "rc16") -> dict:
    """The cached report while the night's subs log is unchanged, else a
    fresh one (sidecar reads only, so cheap)."""
    c = load_cached(config, date, rig)
    if c and c.get("sig") == _subs_signature(config, date):
        return c
    return night_optics(config, date, rig)


def compact(rep: dict) -> dict:
    """The optics block for /api/runs/<date>."""
    o = rep.get("overall") or {}
    if not o.get("n_measured"):
        return {"ok": False, "note": o.get("recommendation")
                or rep.get("headline") or "no optics data"}
    return {"ok": True, "verdict": o["verdict"], "headline": rep["headline"],
            "n_measured": o["n_measured"], "counts": o["counts"],
            "center_fwhm": o.get("center_fwhm"),
            "center_ecc": o.get("center_ecc"),
            "corner_ratio": o.get("corner_ratio"), "tilt": o.get("tilt"),
            "zones": o.get("zones"), "tracking_mixed": o.get("tracking_mixed"),
            "filters": [{"filter": f["filter"], "verdict": f["verdict"],
                         "n_measured": f["n_measured"], "basis": f.get("basis"),
                         "recommendation": f["recommendation"]}
                        for f in rep.get("filters", [])],
            "detail_url": f"/api/optics/report?date={rep.get('date') or ''}"}


def format_report(rep: dict) -> str:
    """Plain-text rendering for the CLI."""
    o = rep.get("overall") or {}
    lines = [f"Optics {rep['date']} ({rep['rig']}): {rep['n_records']} subs, "
             f"{rep['n_usable']} pass HFR/stars, {rep['n_selected']} used, "
             f"{o.get('n_measured', 0)} measured"]
    if o.get("n_measured"):
        lines.append(f"Overall: {o['verdict']}  center FWHM "
                     f"{o.get('center_fwhm')}\"  center ecc "
                     f"{o.get('center_ecc')}  corner ratio "
                     f"{o.get('corner_ratio')}  R {o.get('R')}  radial "
                     f"{o.get('radial_frac')}")
        z = o.get("zones") or {}
        for row in ZONES:
            lines.append("   " + "  ".join(
                f"{n:>2s} {z[n]['fwhm'] if z[n]['fwhm'] is not None else '-':>5}"
                f"\" x{z[n]['ratio'] if z[n]['ratio'] is not None else '-':<5}"
                for n in row))
    for f in rep.get("filters", []):
        lines.append(f"  {f['filter']:4s} {f['verdict']:11s} "
                     f"n={f['n_measured']}/{f['n_subs']}  "
                     f"({f.get('basis')})  {dict(f['counts'])}")
    lines.append("Verdict: " + rep["headline"])
    return "\n".join(lines)
