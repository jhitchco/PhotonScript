"""PS-96: Piggy-600 vs RC16 differential flexure, per night.

Both rigs ride one mount. Anything the mount does (tracking error, polar
drift, a dither) moves the stars of BOTH cameras by the same angle on the
sky; only a change in how the 600 mm is held relative to the RC16 (rings,
dovetail, bracket, cable drag, focuser) moves one without the other. So:

1. pair_subs: each Piggy sub with the RC16 sub covering at least 80% of its
   exposure (the Piggy FITS has no mount position or pier side: those come
   from the RC16 sub's header). Piggy subs straddling an RC16 move
   (slew_gate.infer_slew_windows) or a PHD2 dither are tagged and left out
   of the fits.
2. blocks: consecutive Piggy subs on one target and pier side.
3. tracks: each sub's star field registered to the block's reference sub
   from the PS-80 star sidecars (shared.star_match, no FITS read), turned
   into sky arcsec (east, north) with each rig's plate solution
   (solve_store: first, middle and last Piggy sub plus one RC16 sub per
   block are solved; all Piggy subs with flexure_solve_all).
4. rates ("/min) from linear fits per dither-free segment. Differential =
   Piggy rate minus RC16 rate: common motion cancels. Without a solve for
   both rigs the two tracks are in different pixel frames, so only the
   rate MAGNITUDES are compared.
5. per pair: eccentricity (as an axis ratio, so old 1-b/a backfill grades
   compare with sqrt-form ones) and the elongation's sky direction: both elongated
   the same way = common motion; Piggy-only = differential.
6. flag the night when a block's differential rate (or Piggy drift minus
   RC16 drift) exceeds flexure_warn_arcsec_min, and rank likely causes.

Pure functions over records + sidecars + stored solves; build_report reads
them for a night and caches the result in <data_dir>/flexure/<date>.json.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

RC16, PIGGY = "rc16", "piggyback"
MIN_COVER = 0.8         # RC16 sub must cover this share of the Piggy exposure
BLOCK_GAP_MIN = 30.0    # a longer pause between Piggy subs starts a new block
Q_ELONG = 0.80          # axis ratio below this = elongated (ecc sqrt form 0.6)
Q_ROUND = 0.88          # axis ratio above this = round
SAME_DIR_DEG = 25.0     # elongation directions this close = the same


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _r(v, n=3):
    return None if v is None or not math.isfinite(v) else round(float(v), n)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


LIN_DEF = "1-b/a"


def record_ecc_def(rec: dict, table: dict | None = None) -> str:
    """Which form a stored ecc is in: the record's own ecc_def (PS-94 on),
    else its sidecar's, else 1-b/a for an old backfill record ("sep-binned",
    graded before PS-94), else the live grader's sqrt form."""
    d = rec.get("ecc_def") or (table or {}).get("ecc_def")
    if d:
        return str(d)
    return LIN_DEF if rec.get("graded_by") == "sep-binned" else "sqrt(1-(b/a)^2)"


def axis_ratio(ecc, ecc_def: str = "") -> float | None:
    """b/a from either grader's eccentricity: the live grader's sqrt form
    e = sqrt(1-(b/a)^2), or 1-b/a for backfill grades from before PS-94."""
    if ecc is None:
        return None
    e = min(max(float(ecc), 0.0), 1.0)
    if "1-b/a" in str(ecc_def).replace(" ", ""):
        return 1.0 - e
    return math.sqrt(1.0 - e * e)


def _linfit(ts, vs):
    """Least-squares slope and intercept; None with fewer than 2 points."""
    n = len(ts)
    if n < 2:
        return None
    mt, mv = sum(ts) / n, sum(vs) / n
    sxx = sum((t - mt) ** 2 for t in ts)
    if sxx <= 0:
        return None
    k = sum((t - mt) * (v - mv) for t, v in zip(ts, vs)) / sxx
    return k, mv - k * mt


def _corr(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx <= 0 or sy <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def _axial_mean(thetas):
    """Mean direction (rad) and resultant length of axial angles."""
    t = [x for x in thetas if x is not None and math.isfinite(x)]
    if not t:
        return None, 0.0
    c = sum(math.cos(2 * x) for x in t) / len(t)
    s = sum(math.sin(2 * x) for x in t) / len(t)
    return math.atan2(s, c) / 2.0, math.hypot(c, s)


def _sky_pa(cd, theta) -> float | None:
    """Sky position angle (deg E of N, 0..180) of a pixel direction."""
    if cd is None or theta is None:
        return None
    dx, dy = math.cos(theta), math.sin(theta)
    e = cd[0][0] * dx + cd[0][1] * dy
    n = cd[1][0] * dx + cd[1][1] * dy
    return math.degrees(math.atan2(e, n)) % 180.0


def _dpa(a, b) -> float | None:
    if a is None or b is None:
        return None
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


def alt_ha(ra_deg, dec_deg, when: datetime, lat_deg, lon_deg):
    """(altitude deg, hour angle h) from a GMST approximation; enough to
    correlate drift with pointing."""
    if ra_deg is None or dec_deg is None or when is None:
        return None, None
    jd = (when - datetime(2000, 1, 1, 12)).total_seconds() / 86400.0
    gmst = (280.46061837 + 360.98564736629 * jd) % 360.0
    ha = ((gmst + lon_deg - ra_deg + 180.0) % 360.0) - 180.0
    la, de, h = map(math.radians, (lat_deg, dec_deg, ha))
    alt = math.asin(max(-1.0, min(1.0, math.sin(la) * math.sin(de)
                                  + math.cos(la) * math.cos(de) * math.cos(h))))
    return math.degrees(alt), ha / 15.0


# --------------------------------------------------------------------------
# frames and pairing
# --------------------------------------------------------------------------

_HDR_CACHE: dict = {}


def _header_pointing(path) -> dict:
    """RA/DEC (deg) and pier side from an RC16 FITS header (cached)."""
    if not path:
        return {}
    if path in _HDR_CACHE:
        return _HDR_CACHE[path]
    out = {}
    try:
        from astropy.io import fits as _fits
        from photonscript.scheduler.identify import radec_from_header
        hdr = _fits.getheader(path)
        rd = radec_from_header(hdr)
        if rd:
            out["ra"], out["dec"] = rd
        ps = str(hdr.get("PIERSIDE") or "").strip().upper()
        if ps:
            out["pier"] = ps
    except Exception:  # noqa: BLE001 - not on this machine / unreadable
        pass
    if len(_HDR_CACHE) > 5000:
        _HDR_CACHE.clear()
    _HDR_CACHE[path] = out
    return out


def frames_from_records(records, config, rig: str, read_headers=True) -> list[dict]:
    """Time-ordered frames {file, rec, start, end, mid, exp_s, ra, dec,
    pier, target} for one rig's records. Records may already carry ra, dec
    and pier_side (tests, a future header pass); else the FITS header is
    read when reachable."""
    from photonscript.scheduler.phd2_analysis import sub_start_utc
    out = []
    for r in records:
        if (r.get("rig") or RC16) != rig or not r.get("file"):
            continue
        exp = float(r.get("exp_s") or 0)
        st = sub_start_utc(r, config)
        if st is None or exp <= 0:
            continue
        f = {"file": r["file"], "rec": r, "start": st,
             "end": st + timedelta(seconds=exp),
             "mid": st + timedelta(seconds=exp / 2.0), "exp_s": exp,
             "target": r.get("target"), "ra": r.get("ra"), "dec": r.get("dec"),
             "pier": (str(r.get("pier_side")).upper()
                      if r.get("pier_side") else None)}
        if rig == RC16 and read_headers and (f["ra"] is None or f["pier"] is None):
            h = _header_pointing(r.get("abs_path"))
            f["ra"] = f["ra"] if f["ra"] is not None else h.get("ra")
            f["dec"] = f["dec"] if f["dec"] is not None else h.get("dec")
            f["pier"] = f["pier"] or h.get("pier")
        out.append(f)
    out.sort(key=lambda f: f["start"])
    return out


def pair_subs(piggy_frames, rc16_frames, min_cover: float = MIN_COVER) -> list[dict]:
    """Each Piggy frame with the RC16 frame covering the largest share of its
    exposure, when that share is at least `min_cover`; else rc16 None.
    Returns [{piggy, rc16, cover}] in Piggy time order."""
    out = []
    for p in piggy_frames:
        best, best_c = None, 0.0
        for r in rc16_frames:
            ov = (min(p["end"], r["end"]) - max(p["start"], r["start"])).total_seconds()
            if ov <= 0:
                continue
            c = ov / max(p["exp_s"], 1e-6)
            if c > best_c:
                best, best_c = r, c
        out.append({"piggy": p, "rc16": best if best_c >= min_cover else None,
                    "cover": _r(best_c, 2)})
    return out


def tag_straddles(pairs, rc16_frames, dither_times=()) -> dict:
    """Mark each pair's Piggy frame straddled_slew / straddled_dither."""
    from photonscript.scheduler.slew_gate import infer_slew_windows, straddles
    wins = infer_slew_windows([{"start": f["start"], "end": f["end"],
                                "ra": f["ra"], "dec": f["dec"],
                                "target": f["target"]} for f in rc16_frames])
    dts = sorted(dither_times or ())
    n_s = n_d = 0
    for pr in pairs:
        p = pr["piggy"]
        p["straddled_slew"] = straddles(p["start"], p["end"], wins)
        p["straddled_dither"] = any(p["start"] <= t <= p["end"] for t in dts)
        n_s += p["straddled_slew"]
        n_d += p["straddled_dither"]
    return {"slew_windows": wins, "straddled_slew": n_s, "straddled_dither": n_d}


def make_blocks(pairs, slew_windows, gap_min: float = BLOCK_GAP_MIN) -> list[list[dict]]:
    """Consecutive pairs on one target and pier side, split at RC16 moves
    and long gaps."""
    blocks: list[list[dict]] = []
    cur: list[dict] = []
    prev = None
    for pr in pairs:
        p = pr["piggy"]
        pier = (pr["rc16"] or {}).get("pier")
        tgt = p["target"] or (pr["rc16"] or {}).get("target")
        new = prev is None
        if prev is not None:
            pp = prev["piggy"]
            ppier = (prev["rc16"] or {}).get("pier")
            ptgt = pp["target"] or (prev["rc16"] or {}).get("target")
            gap = (p["start"] - pp["end"]).total_seconds() / 60.0
            moved = any(w0 < p["mid"] and w1 > pp["mid"] for w0, w1 in slew_windows)
            new = (gap > gap_min or moved or (tgt and ptgt and tgt != ptgt)
                   or (pier and ppier and pier != ppier))
        if new and cur:
            blocks.append(cur)
            cur = []
        cur.append(pr)
        prev = pr
    if cur:
        blocks.append(cur)
    return blocks


# --------------------------------------------------------------------------
# tracks and rates
# --------------------------------------------------------------------------

def _track(frames, tables, cd, scale, t0):
    """Registered positions of `frames` against the frame with the most
    stars. Returns ({file: point}, frame_label) where a point is {t_min,
    e, n, rot_deg, n_matched}; e/n are sky arcsec when `cd` is known, else
    pixel x/y times `scale`."""
    from photonscript.shared.star_match import offset
    from photonscript.scheduler.solve_store import pix_to_sky
    have = [f for f in frames if tables.get(f["file"])]
    if not have:
        return {}, None
    ref = max(have, key=lambda f: len(tables[f["file"]].get("x") or []))
    rt = tables[ref["file"]]
    pts = {}
    for f in have:
        if f is ref:
            o = {"dx": 0.0, "dy": 0.0, "rotation_deg": 0.0,
                 "n_matched": len(rt.get("x") or [])}
        else:
            o = offset(rt, tables[f["file"]])
        if o is None:
            continue
        if cd is not None:
            e, n = pix_to_sky(cd, o["dx"], o["dy"])
        else:
            e, n = o["dx"] * scale, o["dy"] * scale
        pts[f["file"]] = {"t_min": (f["mid"] - t0).total_seconds() / 60.0,
                          "e": e, "n": n, "rot_deg": o["rotation_deg"],
                          "n_matched": o["n_matched"]}
    return pts, ("sky" if cd is not None else "pixel")


def _segment_rates(points, cuts, min_pts: int):
    """Rate vector ("/min) from linear fits per segment between `cuts`
    (minutes, e.g. dithers), weighted by segment duration. Also returns the
    residuals of every fitted point (for step detection)."""
    pts = sorted(points, key=lambda p: p["t_min"])
    segs, cur = [], []
    cuts = sorted(cuts)
    for p in pts:
        if cur and any(cur[-1]["t_min"] < c <= p["t_min"] for c in cuts):
            segs.append(cur)
            cur = []
        cur.append(p)
    if cur:
        segs.append(cur)
    we = wn = wsum = 0.0
    resid = []
    for s in segs:
        if len(s) < min_pts:
            continue
        ts = [p["t_min"] for p in s]
        fe, fn = _linfit(ts, [p["e"] for p in s]), _linfit(ts, [p["n"] for p in s])
        if fe is None or fn is None:
            continue
        w = max(ts[-1] - ts[0], 1e-6)
        we += fe[0] * w
        wn += fn[0] * w
        wsum += w
        for p in s:
            resid.append((p["t_min"], p["e"] - (fe[0] * p["t_min"] + fe[1]),
                          p["n"] - (fn[0] * p["t_min"] + fn[1])))
    if wsum <= 0:
        return None, resid, len(segs)
    return (we / wsum, wn / wsum), resid, len(segs)


def _steps(resid, min_arcsec: float = 2.0) -> int:
    """Jumps between consecutive fit residuals well above the scatter."""
    if len(resid) < 4:
        return 0
    jumps = [math.hypot(b[1] - a[1], b[2] - a[2]) for a, b in zip(resid, resid[1:])]
    med = sorted(jumps)[len(jumps) // 2]
    return sum(1 for j in jumps if j > max(3.0 * med, min_arcsec))


def _rot_rate(points) -> float | None:
    """Field rotation rate (deg/h) from the registration rotations."""
    pts = sorted(points, key=lambda p: p["t_min"])
    fit = _linfit([p["t_min"] for p in pts], [p["rot_deg"] for p in pts])
    return fit[0] * 60.0 if fit and len(pts) >= 3 else None


def _guided(rc16_frames) -> bool:
    """Was the RC16 guiding through these subs (live record fields)?"""
    for f in rc16_frames:
        r = f["rec"]
        if r.get("guide_rms") is not None or "guid" in str(r.get("guide_state") or "").lower():
            return True
    return False


def _mag(v):
    return math.hypot(*v) if v is not None else None


def analyze_block(idx, block, rc16_frames, tables_p, tables_r, cd_p, cd_r,
                  scale_p, scale_r, dither_times, config) -> dict:
    """One target / pier-side block: tracks, rates, shapes, evidence."""
    lat = float(getattr(config, "observatory_lat", 31.9))
    lon = float(getattr(config, "observatory_lon", -109.0))
    pfr = [pr["piggy"] for pr in block]
    t0, t1 = pfr[0]["start"], pfr[-1]["end"]
    tgt = next((f["target"] for f in pfr if f["target"]), None)
    pier = next(((pr["rc16"] or {}).get("pier") for pr in block
                 if (pr["rc16"] or {}).get("pier")), None)
    rfr = [r for r in rc16_frames if r["start"] < t1 and r["end"] > t0
           and (not tgt or not r["target"] or r["target"] == tgt)]
    good = [f for f in pfr if not f.get("straddled_slew") and not f.get("straddled_dither")]
    ptrack, pframe = _track(good, tables_p, cd_p, scale_p, t0)
    rtrack, rframe = _track(rfr, tables_r, cd_r, scale_r, t0)
    cuts = [(t - t0).total_seconds() / 60.0 for t in dither_times if t0 <= t <= t1]
    prate, presid, nseg = _segment_rates(ptrack.values(), cuts, 3)
    rrate, _, _ = _segment_rates(rtrack.values(), cuts, 2)
    rc16_assumed = False
    if rrate is None and prate is not None and _guided(rfr):
        # the OAG guides the RC16: with no RC16 track, take it as holding
        rrate, rc16_assumed = (0.0, 0.0), True
    both_sky = pframe == "sky" and (rframe == "sky" or rc16_assumed)
    diff = None
    if prate is not None and rrate is not None and (both_sky or rc16_assumed):
        diff = (prate[0] - rrate[0], prate[1] - rrate[1])
    # pointing for the block
    ras = [r["ra"] for r in rfr if r.get("ra") is not None]
    decs = [r["dec"] for r in rfr if r.get("dec") is not None]
    ra = ras[len(ras) // 2] if ras else None
    dec = decs[len(decs) // 2] if decs else None
    a0, ha0 = alt_ha(ra, dec, t0, lat, lon)
    a1, ha1 = alt_ha(ra, dec, t1, lat, lon)
    hrs = max((t1 - t0).total_seconds() / 3600.0, 1e-6)
    dalt = (a1 - a0) / hrs if a0 is not None and a1 is not None else None
    # shapes per pair
    shapes = []
    for pr in block:
        p, r = pr["piggy"], pr["rc16"]
        tp = tables_p.get(p["file"]) or {}
        qp = axis_ratio(p["rec"].get("ecc"), record_ecc_def(p["rec"], tp))
        dp, _ = _axial_mean(tp.get("theta") or [])
        row = {"piggy": p["file"], "start_utc": _iso(p["start"]),
               "piggy_ecc": p["rec"].get("ecc"), "piggy_q": _r(qp),
               "piggy_hfr": p["rec"].get("hfr"),
               "piggy_dir_deg": _r(_sky_pa(cd_p, dp), 1),
               "straddled": bool(p.get("straddled_slew") or p.get("straddled_dither")),
               "rc16": r["file"] if r else None, "rc16_ecc": None, "rc16_q": None,
               "rc16_dir_deg": None, "verdict": None}
        if r:
            tr = tables_r.get(r["file"]) or {}
            qr = axis_ratio(r["rec"].get("ecc"), record_ecc_def(r["rec"], tr))
            dr, _ = _axial_mean(tr.get("theta") or [])
            row.update(rc16_ecc=r["rec"].get("ecc"), rc16_q=_r(qr),
                       rc16_dir_deg=_r(_sky_pa(cd_r, dr), 1))
            if qp is not None and qr is not None:
                dd = _dpa(row["piggy_dir_deg"], row["rc16_dir_deg"])
                if qp < Q_ELONG and qr < Q_ELONG and (dd is None or dd <= SAME_DIR_DEG):
                    row["verdict"] = "common"
                elif qp < Q_ELONG and qr >= Q_ROUND:
                    row["verdict"] = "piggy-only"
                elif qp < Q_ELONG:
                    row["verdict"] = "both, different direction"
                else:
                    row["verdict"] = "round"
        shapes.append(row)
    # focuser evidence: HFR and ecc trends inside the block
    ts = [(f["mid"] - t0).total_seconds() / 60.0 for f in pfr]
    hfr_r = _corr([t for t, f in zip(ts, pfr) if f["rec"].get("hfr") is not None],
                  [f["rec"]["hfr"] for f in pfr if f["rec"].get("hfr") is not None])
    ecc_r = _corr([t for t, f in zip(ts, pfr) if f["rec"].get("ecc") is not None],
                  [f["rec"]["ecc"] for f in pfr if f["rec"].get("ecc") is not None])
    warn = float(getattr(config, "flexure_warn_arcsec_min", 0.5) or 0.5)
    pm, rm, dm = _mag(prate), _mag(rrate), _mag(diff)
    excess = (pm - rm) if pm is not None and rm is not None else None
    flagged = bool((dm is not None and dm > warn)
                   or (excess is not None and excess > warn))
    return {
        "block": idx, "target": tgt, "pier_side": pier,
        "start_utc": _iso(t0), "end_utc": _iso(t1),
        "piggy_subs": len(pfr), "piggy_registered": len(ptrack),
        "rc16_subs": len(rfr), "rc16_registered": len(rtrack),
        "straddled": sum(1 for f in pfr if f.get("straddled_slew")
                         or f.get("straddled_dither")),
        "segments": nseg,
        "frame": "sky" if both_sky else ("pixel" if pframe else "none"),
        "rc16_assumed_still": rc16_assumed,
        "piggy_rate": [_r(v) for v in prate] if prate else None,
        "rc16_rate": [_r(v) for v in rrate] if rrate else None,
        "diff_rate": [_r(v) for v in diff] if diff else None,
        "piggy_rate_arcsec_min": _r(pm), "rc16_rate_arcsec_min": _r(rm),
        "diff_rate_arcsec_min": _r(dm), "excess_arcsec_min": _r(excess),
        "diff_pa_deg": (_r(math.degrees(math.atan2(*diff)) % 360, 1)
                        if diff and both_sky else None),
        "rotation_deg_h": _r(_rot_rate(ptrack.values()), 4),
        "steps": _steps(presid),
        "alt_start": _r(a0, 1), "alt_end": _r(a1, 1),
        "ha_start_h": _r(ha0, 2), "ha_end_h": _r(ha1, 2),
        "dalt_deg_h": _r(dalt, 2),
        "hfr_trend_r": _r(hfr_r, 2), "ecc_trend_r": _r(ecc_r, 2),
        "flagged": flagged,
        "shapes": shapes,
        "track": [{"file": k, **{kk: _r(vv, 3) for kk, vv in v.items()}}
                  for k, v in sorted(ptrack.items(), key=lambda kv: kv[1]["t_min"])],
    }


# --------------------------------------------------------------------------
# cause ranking
# --------------------------------------------------------------------------

def rank_causes(blocks, warn: float) -> list[dict]:
    """Rank hardware causes by what the drift pattern points to. Scores are
    0..1 heuristics, kept with their evidence."""
    out = []
    rated = [b for b in blocks if b.get("diff_rate_arcsec_min") is not None
             or b.get("piggy_rate_arcsec_min") is not None]

    def drate(b):
        return (b.get("diff_rate_arcsec_min") if b.get("diff_rate_arcsec_min")
                is not None else b.get("excess_arcsec_min")) or 0.0
    # (1) gravity: rate follows d(alt)/dt and flips across the meridian
    xs = [abs(b["dalt_deg_h"]) for b in rated if b.get("dalt_deg_h") is not None]
    ys = [drate(b) for b in rated if b.get("dalt_deg_h") is not None]
    c = _corr(xs, ys)
    east = [b["diff_rate"] for b in rated if b.get("diff_rate") and b.get("pier_side") == "EAST"]
    west = [b["diff_rate"] for b in rated if b.get("diff_rate") and b.get("pier_side") == "WEST"]
    flip = None
    if east and west:
        me = (sum(v[0] for v in east) / len(east), sum(v[1] for v in east) / len(east))
        mw = (sum(v[0] for v in west) / len(west), sum(v[1] for v in west) / len(west))
        den = (_mag(me) or 0) * (_mag(mw) or 0)
        flip = (me[0] * mw[0] + me[1] * mw[1]) / den if den > 0 else None
    s1 = 0.5 * max(c or 0.0, 0.0) + (0.5 if flip is not None and flip < -0.3 else 0.0)
    out.append({"cause": "rings / dovetail / bracket stiffness",
                "score": _r(s1, 2),
                "evidence": f"drift vs |d(alt)/dt| r={_r(c, 2)}; "
                            f"east/west differential direction cos={_r(flip, 2)} "
                            "(negative = flips across the meridian)",
                "check": "tighten tube rings and the dovetail clamp; stiffen or "
                         "brace the piggyback bracket"})
    # (2) steps: clamp slip or cable drag
    steps = sum(b.get("steps") or 0 for b in rated)
    s2 = min(1.0, steps / 3.0)
    out.append({"cause": "clamp slip or cable drag (step jumps)",
                "score": _r(s2, 2),
                "evidence": f"{steps} step jump(s) in the Piggy tracks",
                "check": "dress and strain-relieve the Piggy-600 USB/power "
                         "cables; check the dovetail clamp for slip marks"})
    # (3) focuser sag: drift with rising ecc and changing HFR
    fb = [b for b in rated if drate(b) > warn]
    s3 = 0.0
    if fb:
        s3 = sum(max(abs(b.get("hfr_trend_r") or 0), 0) * max(b.get("ecc_trend_r") or 0, 0)
                 for b in fb) / len(fb)
    out.append({"cause": "focuser sag / draw-tube lock",
                "score": _r(s3, 2),
                "evidence": "drifting blocks with HFR changing and ecc rising: "
                            f"{sum(1 for b in fb if abs(b.get('hfr_trend_r') or 0) > 0.6 and (b.get('ecc_trend_r') or 0) > 0.5)}"
                            f" of {len(fb)}",
                "check": "lock the Piggy focuser draw tube; check the camera "
                         "adapter and focuser tension"})
    # (4) polar error through the RC16 guide axis (PS-84), not hardware
    rot = [abs(b["rotation_deg_h"]) for b in rated if b.get("rotation_deg_h") is not None]
    s4 = 0.0
    if rot and max(rot) > 0.05:
        s4 = 0.5
        if flip is not None and flip > 0.5:
            s4 = 1.0
    out.append({"cause": "polar alignment error seen through the RC16 guide "
                         "axis (field rotation, PS-84), not the piggyback",
                "score": _r(s4, 2),
                "evidence": f"Piggy field rotation up to {_r(max(rot), 3) if rot else None} deg/h; "
                            f"east/west cos={_r(flip, 2)} (positive = same on both sides)",
                "check": "re-measure polar alignment (TPoint) before touching "
                         "the piggyback hardware"})
    out.sort(key=lambda d: -(d["score"] or 0))
    return out


# --------------------------------------------------------------------------
# the night
# --------------------------------------------------------------------------

def _solution(sols: dict, files) -> dict | None:
    for f in files:
        s = sols.get(f)
        if s and s.get("solved") and s.get("cd"):
            return s
    return None


def _solve_block(config, date, block, rfr, sols_p, sols_r, runner, solve_all):
    """Solve first / middle / last Piggy sub (all with solve_all) and the
    middle RC16 sub of a block when not already stored."""
    from photonscript.scheduler.solve_store import solve
    pfr = [pr for pr in block if not pr["piggy"].get("straddled_slew")]
    if not pfr:
        return
    pick = pfr if solve_all else [pfr[0], pfr[len(pfr) // 2], pfr[-1]]
    seen = set()
    for pr in pick:
        p = pr["piggy"]
        if p["file"] in seen or p["file"] in sols_p:
            continue
        seen.add(p["file"])
        r = pr["rc16"] or {}
        hint = (r["ra"], r["dec"]) if r.get("ra") is not None else None
        res = solve(config, p["rec"].get("abs_path") or "", rig=PIGGY, night=date,
                    hint=hint, rel_file=p["file"], start_utc=_iso(p["start"]),
                    runner=runner)
        sols_p[p["file"]] = res or {"solved": False}
    if rfr and not _solution(sols_r, [f["file"] for f in rfr]):
        r = rfr[len(rfr) // 2]
        if r["file"] not in sols_r:
            hint = (r["ra"], r["dec"]) if r.get("ra") is not None else None
            res = solve(config, r["rec"].get("abs_path") or "", rig=RC16, night=date,
                        hint=hint, rel_file=r["file"], start_utc=_iso(r["start"]),
                        runner=runner)
            sols_r[r["file"]] = res or {"solved": False}


def analyze(config, date: str, records, tables_p: dict, tables_r: dict,
            sols_p: dict, sols_r: dict, dither_times=(), solve: bool = False,
            runner=None) -> dict:
    """The night from already-loaded inputs (records, sidecars by file,
    stored solves by file, dither UTC times). With solve=True missing
    solves are run (and stored) through solve_store."""
    from photonscript.shared.rigs import rig_config
    warn = float(getattr(config, "flexure_warn_arcsec_min", 0.5) or 0.5)
    pf = frames_from_records(records, config, PIGGY)
    rf = frames_from_records(records, config, RC16)
    if not pf:
        return {"ok": False, "date": date, "note": "no Piggy-600 subs this night"}
    if not rf:
        return {"ok": False, "date": date, "note": "no RC16 subs to pair with"}
    _rec_by_file = {r["file"]: r for r in records if r.get("file")}
    pairs = pair_subs(pf, rf)
    st = tag_straddles(pairs, rf, dither_times)
    blocks_raw = make_blocks(pairs, st["slew_windows"])
    scale_p = float(getattr(rig_config(config, PIGGY), "pixel_scale_arcsec", 1.29))
    scale_r = float(getattr(config, "pixel_scale_arcsec", 0.239))
    solve_all = bool(getattr(config, "flexure_solve_all", False))
    blocks = []
    for i, blk in enumerate(blocks_raw):
        t0, t1 = blk[0]["piggy"]["start"], blk[-1]["piggy"]["end"]
        tgt = next((pr["piggy"]["target"] for pr in blk if pr["piggy"]["target"]), None)
        rfr = [r for r in rf if r["start"] < t1 and r["end"] > t0
               and (not tgt or not r["target"] or r["target"] == tgt)]
        if solve:
            try:
                _solve_block(config, date, blk, rfr, sols_p, sols_r, runner, solve_all)
            except Exception as e:  # noqa: BLE001 - solves are best-effort
                logger.warning("flexure solves failed for %s block %d: %s", date, i, e)
        files_p = [pr["piggy"]["file"] for pr in blk]
        mid = len(files_p) // 2
        sp = _solution(sols_p, files_p[mid:] + files_p[:mid])
        sr = _solution(sols_r, [r["file"] for r in rfr])
        blocks.append(analyze_block(
            i, blk, rf, tables_p, tables_r, sp["cd"] if sp else None,
            sr["cd"] if sr else None, scale_p, scale_r, dither_times, config))
        blocks[-1]["piggy_pa"] = sp.get("pa") if sp else None
        blocks[-1]["rc16_pa"] = sr.get("pa") if sr else None
    shapes = [s for b in blocks for s in b["shapes"]]
    verdicts: dict[str, int] = {}
    for s in shapes:
        if s["verdict"]:
            verdicts[s["verdict"]] = verdicts.get(s["verdict"], 0) + 1
    rated = [b for b in blocks if b["diff_rate_arcsec_min"] is not None
             or b["excess_arcsec_min"] is not None]
    worst = max(rated, key=lambda b: (b["diff_rate_arcsec_min"]
                                      if b["diff_rate_arcsec_min"] is not None
                                      else b["excess_arcsec_min"] or 0),
                default=None)
    notes = []
    if not any(b["frame"] == "sky" for b in blocks):
        notes.append("no plate solution for one or both rigs: only drift "
                     "magnitudes are compared (run flexure-report on the "
                     "scope PC so ASTAP can solve sampled subs)")
    if not any(b["piggy_registered"] for b in blocks):
        notes.append("no Piggy star sidecars registered (sidecars are written "
                     "by the live grader since PS-80)")
    n_lin = sum(1 for b in blocks for sh in b["shapes"] for f, t in
                ((sh["piggy"], tables_p), (sh["rc16"], tables_r))
                if f and f in _rec_by_file
                and LIN_DEF in record_ecc_def(_rec_by_file[f], t.get(f)).replace(" ", ""))
    if n_lin:
        notes.append(f"{n_lin} sub(s) are older backfill grades whose stored ecc "
                     "is 1-b/a (before PS-94 unified the form); shapes are "
                     "compared as axis ratios (q = b/a), so this is accounted for")
    flagged = any(b["flagged"] for b in blocks)
    return {
        "ok": True, "date": date, "warn_arcsec_min": warn,
        "flagged": flagged,
        "summary": {
            "piggy_subs": len(pf), "rc16_subs": len(rf),
            "paired": sum(1 for p in pairs if p["rc16"]),
            "straddled_slew": st["straddled_slew"],
            "straddled_dither": st["straddled_dither"],
            "blocks": len(blocks), "flagged_blocks": sum(1 for b in blocks if b["flagged"]),
            "max_diff_rate_arcsec_min": max((b["diff_rate_arcsec_min"] for b in blocks
                                             if b["diff_rate_arcsec_min"] is not None),
                                            default=None),
            "max_excess_arcsec_min": max((b["excess_arcsec_min"] for b in blocks
                                          if b["excess_arcsec_min"] is not None),
                                         default=None),
            "worst_block": worst["block"] if worst else None,
            "shape_verdicts": verdicts,
        },
        "causes": rank_causes(blocks, warn) if rated else [],
        "blocks": blocks,
        "notes": notes,
    }


def report_path(config, date: str) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "flexure" / f"{date}.json"


def build_report(config, date: str, solve: bool = False, runner=None,
                 dither_times=None, write: bool = True) -> dict:
    """Load a night's records, sidecars, stored solves and PHD2 dithers,
    analyze, and cache the result. Never raises for missing inputs."""
    from photonscript.scheduler.runs import _load_subs
    from photonscript.scheduler.solve_store import lookup
    from photonscript.shared import star_table
    records = _load_subs(config, date)
    tables_p, tables_r = {}, {}
    for r in records:
        rig = r.get("rig") or RC16
        if rig not in (RC16, PIGGY) or not r.get("file"):
            continue
        t = star_table.read(config, date, r["file"], rig)
        if t:
            (tables_p if rig == PIGGY else tables_r)[r["file"]] = t
    if dither_times is None:
        dither_times = []
        try:
            from photonscript.scheduler.phd2_analysis import night_timeline
            tl = night_timeline(config, date)
            if tl is not None:
                dither_times = list(getattr(tl, "dithers", []))
        except Exception as e:  # noqa: BLE001 - no PHD2 logs here
            logger.debug("flexure: no guide timeline for %s: %s", date, e)
    sols_p = lookup(config, date, PIGGY)
    sols_r = lookup(config, date, RC16)
    rep = analyze(config, date, records, tables_p, tables_r, sols_p, sols_r,
                  dither_times=dither_times, solve=solve, runner=runner)
    rep["generated_utc"] = _iso(datetime.now(timezone.utc))
    if write and rep.get("ok"):
        try:
            p = report_path(config, date)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(rep, default=str), encoding="utf-8")
        except OSError as e:
            logger.debug("flexure report cache skipped: %s", e)
    return rep


def cached_report(config, date: str) -> dict | None:
    try:
        return json.loads(report_path(config, date).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def fresh_report(config, date: str) -> dict:
    """The cached report while it is newer than the night's sub log, else a
    recompute from stored solves (no ASTAP). For the night page, which may
    be opened while subs are still landing."""
    from photonscript.scheduler.runs import runs_dir
    try:
        rp = report_path(config, date)
        sp = runs_dir(config) / f"{date}_subs.jsonl"
        if rp.exists() and (not sp.exists()
                            or rp.stat().st_mtime >= sp.stat().st_mtime):
            hit = cached_report(config, date)
            if hit is not None:
                return hit
    except OSError:
        pass
    return build_report(config, date, solve=False)


def compact(rep: dict) -> dict:
    """The block for the night page (api_run_detail d["flexure"])."""
    if not rep or not rep.get("ok"):
        return {"ok": False, "note": (rep or {}).get("note")}
    return {"ok": True, "flagged": rep["flagged"],
            "warn_arcsec_min": rep["warn_arcsec_min"], "summary": rep["summary"],
            "causes": rep["causes"][:2], "notes": rep["notes"],
            "blocks": [{k: b.get(k) for k in (
                "block", "target", "pier_side", "start_utc", "end_utc",
                "piggy_subs", "piggy_registered", "rc16_subs", "frame",
                "piggy_rate_arcsec_min", "rc16_rate_arcsec_min",
                "diff_rate_arcsec_min", "excess_arcsec_min", "diff_pa_deg",
                "rotation_deg_h", "steps", "alt_start", "alt_end",
                "ha_start_h", "ha_end_h", "rc16_assumed_still", "flagged")}
                for b in rep["blocks"]]}


def format_report(rep: dict) -> str:
    if not rep.get("ok"):
        return f"flexure {rep.get('date')}: {rep.get('note')}"
    s = rep["summary"]
    lines = [f"Piggy-600 vs RC16 flexure, night {rep['date']}"
             f"  ({'FLAGGED' if rep['flagged'] else 'ok'}; warn above "
             f"{rep['warn_arcsec_min']}\"/min)",
             f"  {s['piggy_subs']} Piggy subs, {s['paired']} paired with an RC16 sub; "
             f"{s['straddled_slew']} straddle an RC16 move, "
             f"{s['straddled_dither']} a dither; {s['blocks']} block(s)",
             f"  pair shapes: {s['shape_verdicts'] or '-'}", ""]
    for b in rep["blocks"]:
        lines.append(
            f"  block {b['block']}: {b['target'] or '?'} pier {b['pier_side'] or '?'} "
            f"{(b['start_utc'] or '')[11:16]}-{(b['end_utc'] or '')[11:16]}Z "
            f"alt {b['alt_start']}->{b['alt_end']} HA {b['ha_start_h']}->{b['ha_end_h']}h"
            f"{'  FLAGGED' if b['flagged'] else ''}")
        lines.append(
            f"    Piggy {b['piggy_rate_arcsec_min']}\"/min ({b['piggy_registered']}/"
            f"{b['piggy_subs']} registered), RC16 {b['rc16_rate_arcsec_min']}\"/min"
            f"{' (assumed held by guiding)' if b['rc16_assumed_still'] else ''}, "
            f"differential {b['diff_rate_arcsec_min']}\"/min at PA {b['diff_pa_deg']}, "
            f"excess {b['excess_arcsec_min']}; rotation {b['rotation_deg_h']} deg/h; "
            f"steps {b['steps']}; frame {b['frame']}")
    if rep["causes"]:
        lines += ["", "  Likely causes (score 0..1):"]
        for c in rep["causes"]:
            lines.append(f"    {c['score']}: {c['cause']}. {c['evidence']}. "
                         f"Check: {c['check']}")
    for n in rep["notes"]:
        lines.append(f"  note: {n}")
    return "\n".join(lines)
