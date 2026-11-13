"""PS-97: field rotation per night and target, measured vs predicted.

PS-96 saw the same field rotation on the RC16 and the Piggy-600 (0.05 to
0.10 deg/h). Both ride one mount, so a common rotation points at the mount
(polar alignment), a rig-only one at that rig's camera (rotating in its
adapter) or its flexure. This module answers "does the TPoint polar error
explain the rotation we see?" from records already on disk:

1. Measurement (measure_night). Per rig, consecutive subs on one target and
   pier side form a block (split at a target or pier change, a gap over
   BLOCK_GAP_MIN, a move over MOVE_ARCMIN). Two position-angle tracks per
   block, each fitted with a line (rate deg/h, its standard error and the
   residual RMS):
     * registration: each sub's PS-80 star sidecar registered to the
       previous one (shared.star_match, chained so a slow turn never
       outgrows the match tolerance); the summed turn is the PA change.
       Dense (every sub) and needs no ASTAP.
     * solves: the ASTAP position angles stored by solve_store (PS-96
       flexure solves, PS-67 sampled pointing solves), also the PS-67
       pointing record's `rotation`. Sparse, absolute.
   Registration is used when it has MIN_SUBS points over MIN_SPAN_MIN,
   else the solves. Pointing (Dec, HA, pier side) comes from the PS-67
   pointing record, else the RC16 FITS header (RC16) or the overlapping
   RC16 sub (Piggy-600). Cached per night in <data_dir>/rotation/<night>.json
   while newer than the night's sub log.

   Sign: the rate is d(PA)/dt of the image +y axis (deg E of N per hour).
   A star turn of r in pixel coordinates is a PA change of parity * r
   (parity -1 for the usual sky view, from a stored solve when there is one).

2. Model (predicted_rate). A polar axis off the true pole by a small angle
   eps turns the field at
       rate = 15.041 deg/h * eps[rad] * cos(H - H0) / cos(Dec)
   (H the hour angle, H0 the hour angle the mount pole is displaced toward).
   ME (elevation error) moves the pole toward the meridian (H0 = 0), MA
   (azimuth error) toward H0 = 6 h, so
       rate = 15.041/cos(Dec) * (ME cos H + MA sin H)       [eps in rad]
   The sign of the MA / ME terms follows TPoint's convention only up to a
   sign that is not verified on this rig, so the verdict uses magnitudes:
   the envelope 15.041 * eps / cos(Dec) (the most any HA can give) and the
   least polar error that could explain a measured rate,
   eps_min = |rate| cos(Dec) / 15.041. Latitude enters through the hour
   angle and the altitude cut of the model grid. Refraction is not modeled
   (small above 30 deg altitude).

3. Report (report): blocks of the last N nights with prediction and era
   (before / after a split, default the TPoint record's model date), a
   summary per era by rig, pier side, Dec band and HA band, rig agreement,
   a polar fit when the blocks span enough HA, the verdict, and what the
   rotation costs at the frame corners over a sub and a night.

Report only: reads records, sidecars and stored solves; never runs ASTAP.
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

RC16, PIGGY = "rc16", "piggyback"
SIDEREAL_DEG_H = 15.0411          # sidereal rate, deg of hour angle per hour
MIN_SUBS = 4                      # registered subs for a registration rate
MIN_SOLVES = 3                    # solves for a solve rate
MIN_SPAN_MIN = 20.0               # a block shorter than this is not rated
BLOCK_GAP_MIN = 30.0              # a longer pause starts a new block
MOVE_ARCMIN = 5.0                 # a pointing move this big starts a new block
MATCH_RATIO = 1.25                # observed within this x the envelope ...
MATCH_ABS_DEG_H = 0.01            # ... plus this = explained by polar error
RIG_DIFF_DEG_H = 0.02             # rigs differ by more than this AND ...
RIG_DIFF_FRAC = 0.30              # ... this share of the rate = disagree
FIT_MIN_BLOCKS = 3                # polar fit needs this many blocks ...
FIT_MIN_HA_SPAN_H = 1.5           # ... over this HA spread
DEFAULT_NIGHT_HOURS = 6.0         # "a night" on one target for the cost
CACHE_VERSION = 1


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _r(v, n=4):
    return None if v is None or not math.isfinite(float(v)) else round(float(v), n)


def _num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _median(xs):
    v = sorted(x for x in xs if x is not None)
    if not v:
        return None
    m = len(v) // 2
    return v[m] if len(v) % 2 else 0.5 * (v[m - 1] + v[m])


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def _parse_iso(s):
    if not s:
        return None
    try:
        t = datetime.fromisoformat(str(s).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is not None:
        t = t.astimezone(timezone.utc).replace(tzinfo=None)
    return t


def line_fit(ts, vs) -> dict | None:
    """Least-squares line through (t, v): slope, intercept, slope standard
    error and residual RMS. None with fewer than 2 points or no spread."""
    n = len(ts)
    if n < 2:
        return None
    mt, mv = sum(ts) / n, sum(vs) / n
    sxx = sum((t - mt) ** 2 for t in ts)
    if sxx <= 0:
        return None
    k = sum((t - mt) * (v - mv) for t, v in zip(ts, vs)) / sxx
    b = mv - k * mt
    res = [v - (k * t + b) for t, v in zip(ts, vs)]
    ss = sum(r * r for r in res)
    se = math.sqrt(ss / (n - 2) / sxx) if n > 2 else None
    return {"slope": k, "intercept": b, "se": se,
            "rms": math.sqrt(ss / n), "n": n}


def _unwrap(pas):
    """Position angles (deg) unwrapped so consecutive values differ by less
    than 180 (a solve PA is mod 360)."""
    out = []
    for p in pas:
        if out:
            d = (p - out[-1] + 180.0) % 360.0 - 180.0
            out.append(out[-1] + d)
        else:
            out.append(float(p))
    return out


def pier_name(v) -> str | None:
    from photonscript.shared.pointing import pier_name as _pn
    return _pn(v)


# --------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------

def predicted_rate(ma_arcmin, me_arcmin, ha_h, dec_deg) -> float | None:
    """Field rotation (deg/h) from a polar error of MA (azimuth) and ME
    (elevation) arcmin at hour angle ha_h (h) and Dec (deg). The sign
    convention is not verified on this rig (module doc): compare magnitudes
    or use envelope_rate."""
    if None in (ma_arcmin, me_arcmin, ha_h, dec_deg):
        return None
    c = math.cos(math.radians(dec_deg))
    if abs(c) < 1e-3:
        return None
    h = math.radians(15.0 * ha_h)
    ma, me = math.radians(ma_arcmin / 60.0), math.radians(me_arcmin / 60.0)
    return SIDEREAL_DEG_H * (me * math.cos(h) + ma * math.sin(h)) / c


def envelope_rate(eps_arcmin, dec_deg) -> float | None:
    """The largest |rate| (deg/h) a polar error of eps arcmin gives at Dec
    (at the worst hour angle). Independent of the MA / ME sign convention."""
    if eps_arcmin is None or dec_deg is None:
        return None
    c = math.cos(math.radians(dec_deg))
    if abs(c) < 1e-3:
        return None
    return SIDEREAL_DEG_H * math.radians(abs(eps_arcmin) / 60.0) / abs(c)


def min_polar_error(rate_deg_h, dec_deg) -> float | None:
    """The least polar error (arcmin) that can turn the field at |rate| at
    this Dec (the envelope solved for eps)."""
    if rate_deg_h is None or dec_deg is None:
        return None
    c = abs(math.cos(math.radians(dec_deg)))
    return math.degrees(abs(rate_deg_h) * c / SIDEREAL_DEG_H) * 60.0


def fit_polar(blocks) -> dict | None:
    """Least squares rate*cos(Dec)/15.041 = ME cos H + MA sin H over rated
    blocks (signed rates). Needs FIT_MIN_BLOCKS over FIT_MIN_HA_SPAN_H of HA.
    Returns the implied MA / ME / total (arcmin, under this module's sign
    convention), the residual RMS (deg/h) and how much of the rate it
    explains."""
    rows = [(b["ha_mid_h"], b["dec"], b["rate_deg_h"]) for b in blocks
            if b.get("rate_deg_h") is not None and b.get("ha_mid_h") is not None
            and b.get("dec") is not None]
    if len(rows) < FIT_MIN_BLOCKS:
        return None
    has = [r[0] for r in rows]
    if max(has) - min(has) < FIT_MIN_HA_SPAN_H:
        return None
    s11 = s12 = s22 = y1 = y2 = 0.0
    for ha, dec, rate in rows:
        h = math.radians(15.0 * ha)
        ch, sh = math.cos(h), math.sin(h)
        y = math.radians(rate) / math.radians(SIDEREAL_DEG_H) * math.cos(math.radians(dec))
        s11 += ch * ch
        s12 += ch * sh
        s22 += sh * sh
        y1 += ch * y
        y2 += sh * y
    det = s11 * s22 - s12 * s12
    if abs(det) < 1e-9:
        return None
    me = (y1 * s22 - y2 * s12) / det
    ma = (s11 * y2 - s12 * y1) / det
    me_am, ma_am = math.degrees(me) * 60.0, math.degrees(ma) * 60.0
    res = [rate - predicted_rate(ma_am, me_am, ha, dec) for ha, dec, rate in rows]
    rms = math.sqrt(sum(x * x for x in res) / len(res))
    tot = math.sqrt(sum(r[2] ** 2 for r in rows) / len(rows))
    return {"n": len(rows), "ma_arcmin": _r(ma_am, 2), "me_arcmin": _r(me_am, 2),
            "total_arcmin": _r(math.hypot(ma_am, me_am), 2),
            "resid_rms_deg_h": _r(rms, 4),
            "explained": _r(1.0 - rms / tot, 2) if tot > 0 else None,
            "ha_span_h": _r(max(has) - min(has), 2)}


def model_grid(ma_arcmin, me_arcmin, lat_deg, decs=(0, 20, 40, 60, 70, 80),
               has=(-4, -2, 0, 2, 4), min_alt=20.0) -> dict | None:
    """Predicted |rate| (deg/h) for a Dec x HA grid; cells below min_alt at
    this latitude are None."""
    if ma_arcmin is None or me_arcmin is None:
        return None
    la = math.radians(lat_deg)
    rows = []
    for d in decs:
        cells = []
        for h in has:
            de, hh = math.radians(d), math.radians(15.0 * h)
            alt = math.degrees(math.asin(max(-1.0, min(1.0, math.sin(la) * math.sin(de)
                                                       + math.cos(la) * math.cos(de) * math.cos(hh)))))
            p = predicted_rate(ma_arcmin, me_arcmin, h, d)
            cells.append(None if alt < min_alt or p is None else _r(abs(p), 4))
        rows.append({"dec": d, "rates": cells,
                     "envelope": _r(envelope_rate(math.hypot(ma_arcmin, me_arcmin), d), 4)})
    return {"has_h": list(has), "rows": rows, "min_alt": min_alt}


def corner_cost(rate_deg_h, seconds, scale_arcsec, w_px, h_px) -> dict | None:
    """Arc length a corner star moves while the field turns at rate for
    `seconds`: about the frame center (half diagonal) and about a pivot
    off the frame edge (the OAG guide star; a full diagonal, the upper
    bound). Pixels and arcsec."""
    if rate_deg_h is None or not seconds or not scale_arcsec:
        return None
    ang = math.radians(abs(rate_deg_h) * seconds / 3600.0)
    half = 0.5 * math.hypot(w_px, h_px)
    full = math.hypot(w_px, h_px)
    return {"seconds": _r(seconds, 0), "turn_deg": _r(math.degrees(ang), 4),
            "corner_px": _r(ang * half, 2), "corner_arcsec": _r(ang * half * scale_arcsec, 2),
            "edge_pivot_px": _r(ang * full, 2),
            "edge_pivot_arcsec": _r(ang * full * scale_arcsec, 2)}


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------

def frames_for_rig(config, records, pointing_recs, rig, read_headers=True) -> list[dict]:
    """Time-ordered frames {file, rec, start, mid, end, exp_s, target, ra,
    dec, pier, rotation} for one rig. Position: the PS-67 pointing record,
    else the record itself, else the RC16 FITS header."""
    from photonscript.scheduler.phd2_analysis import sub_start_utc
    out = []
    for r in records:
        if (r.get("rig") or RC16) != rig or not r.get("file"):
            continue
        exp = _num(r.get("exp_s")) or 0.0
        st = sub_start_utc(r, config)
        if st is None or exp <= 0:
            continue
        p = pointing_recs.get((rig, r["file"])) or {}
        ra = _num(p.get("solved_ra")) if p.get("solved_ra") is not None else _num(p.get("mount_ra"))
        dec = _num(p.get("solved_dec")) if p.get("solved_dec") is not None else _num(p.get("mount_dec"))
        ra = ra if ra is not None else _num(r.get("ra"))
        dec = dec if dec is not None else _num(r.get("dec"))
        pier = pier_name(p.get("pier") or r.get("pier_side"))
        if rig == RC16 and read_headers and (ra is None or pier is None):
            from photonscript.scheduler.flexure import _header_pointing
            h = _header_pointing(r.get("abs_path"))
            ra = ra if ra is not None else h.get("ra")
            dec = dec if dec is not None else h.get("dec")
            pier = pier or pier_name(h.get("pier"))
        out.append({"file": r["file"], "rec": r, "start": st,
                    "end": st + timedelta(seconds=exp),
                    "mid": st + timedelta(seconds=exp / 2.0), "exp_s": exp,
                    "target": r.get("target"), "ra": ra, "dec": dec, "pier": pier,
                    "rotation": _num(p.get("rotation"))})
    out.sort(key=lambda f: f["start"])
    return out


def borrow_pointing(frames, rc16_frames) -> None:
    """Piggy-600 frames with no position take the overlapping RC16 sub's."""
    for f in frames:
        if f["dec"] is not None and f["pier"] is not None:
            continue
        best, best_ov = None, 0.0
        for r in rc16_frames:
            ov = (min(f["end"], r["end"]) - max(f["start"], r["start"])).total_seconds()
            if ov > best_ov:
                best, best_ov = r, ov
        if best is not None:
            f["ra"] = f["ra"] if f["ra"] is not None else best["ra"]
            f["dec"] = f["dec"] if f["dec"] is not None else best["dec"]
            f["pier"] = f["pier"] or best["pier"]


def make_blocks(frames, gap_min=BLOCK_GAP_MIN, move_arcmin=MOVE_ARCMIN) -> list[list[dict]]:
    """Consecutive frames on one target and pier side."""
    from photonscript.shared.pointing import sep_arcmin
    blocks, cur, prev = [], [], None
    for f in frames:
        new = prev is None
        if prev is not None:
            gap = (f["start"] - prev["end"]).total_seconds() / 60.0
            moved = (None not in (f["ra"], f["dec"], prev["ra"], prev["dec"])
                     and sep_arcmin(prev["ra"], prev["dec"], f["ra"], f["dec"]) > move_arcmin)
            new = (gap > gap_min or moved
                   or (f["target"] and prev["target"] and f["target"] != prev["target"])
                   or (f["pier"] and prev["pier"] and f["pier"] != prev["pier"]))
        if new and cur:
            blocks.append(cur)
            cur = []
        cur.append(f)
        prev = f
    if cur:
        blocks.append(cur)
    return blocks


def registration_track(frames, tables) -> list[tuple[datetime, float]]:
    """(mid time, cumulative star turn deg) per registered frame: each sub
    against the previous registered one, summed. The first frame with a
    table is 0."""
    from photonscript.shared.star_match import offset
    out, prev, acc = [], None, 0.0
    for f in frames:
        t = tables.get(f["file"])
        if not t:
            continue
        if prev is None:
            out.append((f["mid"], 0.0))
            prev = t
            continue
        o = offset(prev, t)
        if o is None:
            continue
        acc += float(o["rotation_deg"])
        out.append((f["mid"], acc))
        prev = t
    return out


def solve_track(frames, sols) -> tuple[list[tuple[datetime, float]], int | None]:
    """(mid time, solved PA deg unwrapped) and the parity of the solves."""
    pts, parity = [], None
    for f in frames:
        s = sols.get(f["file"]) or {}
        pa = _num(s.get("pa")) if s.get("solved") else None
        if pa is None:
            pa = f.get("rotation")
        if pa is None:
            continue
        if parity is None and s.get("parity") in (-1, 1):
            parity = int(s["parity"])
        pts.append((f["mid"], pa))
    if not pts:
        return [], parity
    un = _unwrap([p for _, p in pts])
    return [(t, v) for (t, _), v in zip(pts, un)], parity


def _fit_track(pts, t0) -> dict | None:
    if not pts:
        return None
    ts = [(t - t0).total_seconds() / 3600.0 for t, _ in pts]
    fit = line_fit(ts, [v for _, v in pts])
    if fit is None:
        return None
    return {"rate_deg_h": fit["slope"], "err_deg_h": fit["se"],
            "resid_deg": fit["rms"], "n": fit["n"], "span_min": (max(ts) - min(ts)) * 60.0}


def analyze_block(config, night, rig, frames, tables, sols) -> dict:
    from photonscript.shared.pointing import alt_ha
    lat = float(getattr(config, "observatory_lat", 31.907))
    lon = float(getattr(config, "observatory_lon", -109.021))
    t0, t1 = frames[0]["start"], frames[-1]["end"]
    tmid = t0 + (t1 - t0) / 2
    ras = [f["ra"] for f in frames if f["ra"] is not None]
    decs = [f["dec"] for f in frames if f["dec"] is not None]
    ra, dec = _median(ras), _median(decs)
    a_mid, ha_mid = alt_ha(ra, dec, tmid, lat, lon)
    _, ha0 = alt_ha(ra, dec, t0, lat, lon)
    _, ha1 = alt_ha(ra, dec, t1, lat, lon)
    reg_pts = registration_track(frames, tables)
    sol_pts, parity = solve_track(frames, sols)
    par = parity if parity is not None else -1
    reg = _fit_track([(t, par * v) for t, v in reg_pts], t0)
    sol = _fit_track(sol_pts, t0)
    reg_ok = reg is not None and reg["n"] >= MIN_SUBS and reg["span_min"] >= MIN_SPAN_MIN
    sol_ok = sol is not None and sol["n"] >= MIN_SOLVES and sol["span_min"] >= MIN_SPAN_MIN
    use = reg if reg_ok else (sol if sol_ok else None)
    scales = [_num((sols.get(f["file"]) or {}).get("scale")) for f in frames
              if (sols.get(f["file"]) or {}).get("solved")]
    return {
        "night": night, "rig": rig,
        "target": next((f["target"] for f in frames if f["target"]), None),
        "pier": next((f["pier"] for f in frames if f["pier"]), None),
        "start_utc": _iso(t0), "end_utc": _iso(t1),
        "span_min": _r((t1 - t0).total_seconds() / 60.0, 1),
        "n_subs": len(frames), "exp_s": _median([f["exp_s"] for f in frames]),
        "n_registered": len(reg_pts), "n_solved": len(sol_pts),
        "parity": parity, "scale_arcsec": _r(_median(scales), 4),
        "ra": _r(ra, 4), "dec": _r(dec, 3),
        "alt_mid": _r(a_mid, 1), "ha_start_h": _r(ha0, 2), "ha_end_h": _r(ha1, 2),
        "ha_mid_h": _r(ha_mid, 3),
        "source": "registration" if reg_ok else ("solve" if sol_ok else None),
        "rate_deg_h": _r(use["rate_deg_h"], 5) if use else None,
        "err_deg_h": _r(use["err_deg_h"], 5) if use else None,
        "resid_deg": _r(use["resid_deg"], 5) if use else None,
        "rate_reg_deg_h": _r(reg["rate_deg_h"], 5) if reg else None,
        "rate_solve_deg_h": _r(sol["rate_deg_h"], 5) if sol else None,
    }


def analyze_night(config, night, records, pointing_recs, tables, sols,
                  read_headers=True) -> dict:
    """Blocks of one night from loaded inputs. tables / sols: {rig: {file:
    ...}}."""
    rc = frames_for_rig(config, records, pointing_recs, RC16, read_headers)
    blocks = []
    for rig in (RC16, PIGGY):
        fr = rc if rig == RC16 else frames_for_rig(config, records, pointing_recs,
                                                   rig, read_headers)
        if rig != RC16:
            borrow_pointing(fr, rc)
        for blk in make_blocks(fr):
            blocks.append(analyze_block(config, night, rig, blk,
                                        tables.get(rig, {}), sols.get(rig, {})))
    return {"v": CACHE_VERSION, "night": night, "subs": len(records),
            "blocks": blocks}


def cache_path(config, night: str) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "rotation" / f"{night}.json"


def measure_night(config, night: str, refresh: bool = False, write: bool = True) -> dict:
    """The night's blocks: the cache while newer than the sub log, the
    pointing record and the solves, else a recompute. Never raises for
    missing inputs."""
    from photonscript.scheduler import solve_store
    from photonscript.scheduler.runs import _load_subs, runs_dir
    from photonscript.shared import pointing, star_table
    cp = cache_path(config, night)
    srcs = [runs_dir(config) / f"{night}_subs.jsonl", pointing.sidecar_path(config, night),
            solve_store.store_path(config, night, RC16),
            solve_store.store_path(config, night, PIGGY)]
    if not refresh:
        try:
            if cp.exists():
                m = cp.stat().st_mtime
                if all(not s.exists() or s.stat().st_mtime <= m for s in srcs):
                    hit = json.loads(cp.read_text(encoding="utf-8"))
                    if hit.get("v") == CACHE_VERSION:
                        return hit
        except (OSError, ValueError):
            pass
    records = _load_subs(config, night)
    if not records:
        return {"v": CACHE_VERSION, "night": night, "subs": 0, "blocks": []}
    tables: dict = {RC16: {}, PIGGY: {}}
    for r in records:
        rig = r.get("rig") or RC16
        if rig in tables and r.get("file"):
            t = star_table.read(config, night, r["file"], rig)
            if t:
                tables[rig][r["file"]] = t
    sols = {rig: solve_store.lookup(config, night, rig) for rig in (RC16, PIGGY)}
    out = analyze_night(config, night, records, pointing.load(config, night),
                        tables, sols)
    if write:
        try:
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps(out), encoding="utf-8")
        except OSError as e:
            logger.debug("rotation cache skipped: %s", e)
    return out


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------

def polar_source(config, ma=None, me=None) -> dict:
    """MA / ME (arcmin): query values win, else the manual TPoint record
    (Guiding tab, polar error az = MA, alt = ME)."""
    rec = {}
    try:
        from photonscript.scheduler.thesky_audit import load_manual
        rec = load_manual(config) or {}
    except Exception:  # noqa: BLE001
        rec = {}
    if ma is not None or me is not None:
        ma = ma if ma is not None else _num(rec.get("polar_az_arcmin")) or 0.0
        me = me if me is not None else _num(rec.get("polar_alt_arcmin")) or 0.0
        src = "query"
    else:
        ma, me = _num(rec.get("polar_az_arcmin")), _num(rec.get("polar_alt_arcmin"))
        src = "TPoint record" if ma is not None or me is not None else None
        if src:
            ma, me = ma or 0.0, me or 0.0
    return {"ma_arcmin": ma, "me_arcmin": me,
            "total_arcmin": _r(math.hypot(ma, me), 2) if src else None,
            "source": src, "model_date": rec.get("model_date"),
            "entered_at": rec.get("entered_at"),
            "protrack_on": rec.get("protrack_on")}


def era_of(block, split: str | None) -> str | None:
    """before / after the split. A YYYY-MM-DD split compares the night (the
    evening date): that night and later are 'after'. A full timestamp
    compares the block start (UTC)."""
    if not split:
        return None
    s = str(split).strip()
    if len(s) == 10:
        return "after" if block["night"] >= s else "before"
    t, b = _parse_iso(s), _parse_iso(block.get("start_utc"))
    if t is None or b is None:
        return None
    return "after" if b >= t else "before"


def _band(v, bands):
    if v is None:
        return None
    for lo, hi, name in bands:
        if lo <= v < hi:
            return name
    return None


def _group_stats(blocks) -> dict:
    rated = [b for b in blocks if b.get("rate_deg_h") is not None]
    return {"n": len(rated),
            "median_rate_deg_h": _r(_median([b["rate_deg_h"] for b in rated]), 4),
            "median_abs_rate_deg_h": _r(_median([abs(b["rate_deg_h"]) for b in rated]), 4),
            "median_envelope_deg_h": _r(_median([b.get("pred_env_deg_h") for b in rated]), 4),
            "median_min_polar_arcmin": _r(_median([b.get("min_polar_arcmin") for b in rated]), 2),
            "median_dec": _r(_median([b.get("dec") for b in rated]), 1)}


def rig_agreement(blocks) -> dict:
    """RC16 vs Piggy-600 blocks of one night, target and pier side that
    overlap in time: the rate difference."""
    rc = [b for b in blocks if b["rig"] == RC16 and b.get("rate_deg_h") is not None]
    pg = [b for b in blocks if b["rig"] == PIGGY and b.get("rate_deg_h") is not None]
    pairs = []
    for p in pg:
        ps, pe = _parse_iso(p["start_utc"]), _parse_iso(p["end_utc"])
        for r in rc:
            if r["night"] != p["night"] or (r["pier"] and p["pier"] and r["pier"] != p["pier"]):
                continue
            if r["target"] and p["target"] and r["target"] != p["target"]:
                continue
            rs, re_ = _parse_iso(r["start_utc"]), _parse_iso(r["end_utc"])
            ov = (min(pe, re_) - max(ps, rs)).total_seconds()
            if ov <= 0:
                continue
            pairs.append({"night": p["night"], "target": p["target"], "pier": p["pier"],
                          "rc16_deg_h": r["rate_deg_h"], "piggy_deg_h": p["rate_deg_h"],
                          "diff_deg_h": _r(p["rate_deg_h"] - r["rate_deg_h"], 4)})
    if not pairs:
        return {"n": 0, "agree": None, "pairs": []}
    d = _median([abs(x["diff_deg_h"]) for x in pairs])
    lvl = _median([0.5 * (abs(x["rc16_deg_h"]) + abs(x["piggy_deg_h"])) for x in pairs]) or 0.0
    agree = not (d > RIG_DIFF_DEG_H and d > RIG_DIFF_FRAC * lvl)
    off = None
    if not agree:
        rcm = _median([abs(x["rc16_deg_h"]) for x in pairs]) or 0.0
        pgm = _median([abs(x["piggy_deg_h"]) for x in pairs]) or 0.0
        off = PIGGY if pgm > rcm else RC16
    return {"n": len(pairs), "agree": agree, "median_abs_diff_deg_h": _r(d, 4),
            "faster_rig": off, "pairs": pairs}


def verdict(stats: dict, agreement: dict, polar: dict) -> dict:
    """ok / warn / unknown and one line."""
    if not stats["n"]:
        return {"level": "unknown", "text": "no rated blocks: needs at least "
                f"{MIN_SUBS} registered subs (PS-80 star sidecars) or {MIN_SOLVES} "
                f"solves over {MIN_SPAN_MIN:g} min on one target and pier side"}
    obs = stats["median_abs_rate_deg_h"]
    env = stats["median_envelope_deg_h"]
    need = stats["median_min_polar_arcmin"]
    if agreement.get("agree") is False:
        rig = "Piggy-600" if agreement.get("faster_rig") == PIGGY else "RC16"
        return {"level": "warn",
                "text": f"the rigs disagree by {agreement['median_abs_diff_deg_h']} deg/h "
                        f"(same mount, so not polar): check the {rig} camera for "
                        "rotation in its adapter, or its flexure"}
    if polar.get("source") is None or env is None:
        return {"level": "unknown",
                "text": f"observed {obs} deg/h; it needs at least {need}' of polar "
                        "error. Enter the TPoint polar error (Guiding tab, TPoint "
                        "record) to compare"}
    eps = polar["total_arcmin"]
    if obs <= env * MATCH_RATIO + MATCH_ABS_DEG_H:
        return {"level": "ok",
                "text": f"observed {obs} deg/h matches {eps}' polar error "
                        f"(predicts up to {env} deg/h at Dec {stats['median_dec']}): no action"}
    txt = (f"observed {obs} deg/h exceeds the polar prediction (up to {env} deg/h "
           f"for {eps}'; it would need at least {need}'): check camera / flexure")
    if agreement.get("agree"):
        txt += ("; both rigs turn together, so it is common to the mount: "
                "re-check the polar error with a TPoint polar alignment report "
                "(or whether the record's MA / ME are current)")
    return {"level": "warn", "text": txt}


def _summarize(blocks, polar) -> dict:
    from photonscript.scheduler.nina_center_log import DEC_BANDS, HA_BANDS
    stats = _group_stats(blocks)
    agr = rig_agreement(blocks)
    return {
        **stats,
        "by_rig": {rig: _group_stats([b for b in blocks if b["rig"] == rig])
                   for rig in (RC16, PIGGY)},
        "by_pier": {p: _group_stats([b for b in blocks if b.get("pier") == p])
                    for p in ("East", "West")},
        "by_dec": {name: _group_stats([b for b in blocks
                                       if _band(b.get("dec"), DEC_BANDS) == name])
                   for _, _, name in DEC_BANDS},
        "by_ha": {name: _group_stats([b for b in blocks
                                      if _band(b.get("ha_mid_h"), HA_BANDS) == name])
                  for _, _, name in HA_BANDS},
        "rig_agreement": agr,
        "polar_fit": fit_polar(blocks),
        "verdict": verdict(stats, agr, polar),
    }


def _rig_geom(config, rig):
    from photonscript.shared.rigs import rig_config
    rc = rig_config(config, rig)
    w = int(getattr(config, "sensor_width_px", 6224) or 6224)
    h = int(getattr(config, "sensor_height_px", 4168) or 4168)
    if rig == PIGGY:
        w = int(getattr(config, "piggyback_sensor_width_px", 0) or 0) or w
        h = int(getattr(config, "piggyback_sensor_height_px", 0) or 0) or h
    return float(getattr(rc, "pixel_scale_arcsec", 0.236 if rig == RC16 else 1.29)), w, h


def costs(config, blocks, stats, polar, night_hours, sols_scale=None) -> dict:
    """Corner cost per rig over its median sub and over night_hours, for the
    measured median rate and for the polar prediction (envelope)."""
    out = {}
    for rig in (RC16, PIGGY):
        scale, w, h = _rig_geom(config, rig)
        if sols_scale and sols_scale.get(rig):
            scale = sols_scale[rig]
        rb = [b for b in blocks if b["rig"] == rig]
        sub = _median([b.get("exp_s") for b in rb]) or (600.0 if rig == RC16 else 120.0)
        meas = (stats["by_rig"][rig]["median_abs_rate_deg_h"]
                or stats.get("median_abs_rate_deg_h"))
        dec = stats["by_rig"][rig]["median_dec"] or stats.get("median_dec")
        pred = (envelope_rate(polar.get("total_arcmin"), dec)
                if polar.get("source") and dec is not None else None)
        out[rig] = {"scale_arcsec": _r(scale, 4), "width_px": w, "height_px": h,
                    "sub_s": sub, "night_h": night_hours,
                    "measured_deg_h": meas, "predicted_deg_h": _r(pred, 4),
                    "measured": {"sub": corner_cost(meas, sub, scale, w, h),
                                 "night": corner_cost(meas, night_hours * 3600.0, scale, w, h)},
                    "predicted": {"sub": corner_cost(pred, sub, scale, w, h),
                                  "night": corner_cost(pred, night_hours * 3600.0, scale, w, h)}}
    return out


def _nights(end_night: str, n: int) -> list[str]:
    d = datetime.strptime(end_night, "%Y-%m-%d")
    return [(d - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(n)]


def report(config, nights: int = 14, end_night: str | None = None,
           split: str | None = None, ma: float | None = None, me: float | None = None,
           refresh: bool = False, night_hours: float = DEFAULT_NIGHT_HOURS,
           measured: dict | None = None) -> dict:
    """Field rotation over the last `nights` nights ending at end_night
    (default the night now running or just ended). `measured` = {night:
    measure_night output} injects blocks (tests)."""
    from photonscript.shared import phd2_store
    end_night = end_night or phd2_store.night_of(config, datetime.utcnow())
    nights = max(1, min(int(nights or 14), 60))
    polar = polar_source(config, ma, me)
    split_src = "query" if split else None
    if not split and polar.get("model_date"):
        split, split_src = polar["model_date"], "TPoint model date"
    lat = float(getattr(config, "observatory_lat", 31.907))
    blocks, per_night = [], []
    for n in _nights(end_night, nights):
        m = (measured or {}).get(n) if measured is not None else measure_night(config, n, refresh)
        if not m:
            continue
        bs = m.get("blocks") or []
        if bs:
            per_night.append({"night": n, "blocks": len(bs),
                              "rated": sum(1 for b in bs if b.get("rate_deg_h") is not None)})
        blocks += bs
    for b in blocks:
        b["era"] = era_of(b, split)
        p = predicted_rate(polar["ma_arcmin"], polar["me_arcmin"], b.get("ha_mid_h"),
                           b.get("dec")) if polar.get("source") else None
        b["pred_deg_h"] = _r(p, 4)
        b["pred_env_deg_h"] = _r(envelope_rate(polar.get("total_arcmin"), b.get("dec")), 4) \
            if polar.get("source") else None
        b["min_polar_arcmin"] = _r(min_polar_error(b.get("rate_deg_h"), b.get("dec")), 2)
        env = b["pred_env_deg_h"]
        b["ratio"] = (_r(abs(b["rate_deg_h"]) / env, 2)
                      if b.get("rate_deg_h") is not None and env else None)
    blocks.sort(key=lambda b: (b.get("start_utc") or "", b["rig"]))
    eras = {"all": _summarize(blocks, polar)}
    if split:
        for e in ("before", "after"):
            eras[e] = _summarize([b for b in blocks if b.get("era") == e], polar)
    main = "after" if split and eras["after"]["n"] else "all"
    notes = []
    if not blocks:
        notes.append("no subs in these nights on this machine")
    elif not any(b.get("rate_deg_h") is not None for b in blocks):
        notes.append("blocks found but none rated: star sidecars (PS-80) or "
                     "stored solves (flexure-report / pointing-backfill --solve) "
                     "are needed")
    if split and not eras.get("after", {}).get("n"):
        notes.append(f"no rated blocks on or after {split} yet: the verdict uses "
                     "all nights (read the report again after a post-model night)")
    notes.append("rate sign = d(PA)/dt; the MA / ME sign convention is not "
                 "verified on this rig, so verdicts compare magnitudes "
                 "(envelope = the most any hour angle gives)")
    sc = {}
    for rig in (RC16, PIGGY):
        sc_vals = [b.get("scale_arcsec") for b in blocks
                  if b["rig"] == rig and b.get("scale_arcsec")]
        if sc_vals:
            sc[rig] = _median(sc_vals)
    return {
        "ok": True, "nights": nights, "end_night": end_night,
        "latitude": lat, "polar": polar,
        "split": {"value": split, "source": split_src,
                  "rule": "night (evening date) on or after = after" if split and len(str(split)) == 10
                  else ("block start UTC on or after = after" if split else None)},
        "main_era": main, "verdict": eras[main]["verdict"],
        "eras": eras, "per_night": per_night,
        "blocks": blocks,
        "cost": costs(config, blocks, eras[main], polar, night_hours, sc),
        "model_grid": model_grid(polar["ma_arcmin"], polar["me_arcmin"], lat)
        if polar.get("source") else None,
        "notes": notes,
        "generated_utc": _iso(datetime.now(timezone.utc).replace(tzinfo=None)),
    }


def format_report(rep: dict) -> str:
    p = rep["polar"]
    lines = [f"Field rotation, {rep['nights']} nights to {rep['end_night']} "
             f"(lat {rep['latitude']:.3f})",
             f"  polar error: MA {p['ma_arcmin']}' ME {p['me_arcmin']}' total "
             f"{p['total_arcmin']}' ({p['source'] or 'none entered'}"
             f"{', model ' + p['model_date'] if p.get('model_date') else ''})",
             f"  split: {rep['split']['value'] or '-'} ({rep['split']['source'] or '-'})",
             f"  VERDICT [{rep['verdict']['level']}] ({rep['main_era']}): {rep['verdict']['text']}",
             ""]
    for e in ("before", "after", "all"):
        s = rep["eras"].get(e)
        if not s:
            continue
        agr = s["rig_agreement"]
        lines.append(f"  {e}: {s['n']} rated block(s), median |rate| "
                     f"{s['median_abs_rate_deg_h']} deg/h, envelope {s['median_envelope_deg_h']}, "
                     f"needs >= {s['median_min_polar_arcmin']}'; rigs "
                     f"{'agree' if agr.get('agree') else ('DISAGREE' if agr.get('agree') is False else '-')}"
                     f" ({agr['n']} pair(s))")
        f = s.get("polar_fit")
        if f:
            lines.append(f"    polar fit: MA {f['ma_arcmin']}' ME {f['me_arcmin']}' total "
                         f"{f['total_arcmin']}' (n {f['n']}, resid {f['resid_rms_deg_h']} deg/h)")
    lines.append("")
    for b in rep["blocks"]:
        lines.append(
            f"  {b['night']} {b['rig']:9s} {(b['target'] or '?')[:22]:22s} pier {b['pier'] or '?':4s} "
            f"Dec {b['dec']} HA {b['ha_start_h']}->{b['ha_end_h']}h "
            f"{b['n_registered']}/{b['n_subs']} reg {b['n_solved']} solved: "
            f"rate {b['rate_deg_h']} +/- {b['err_deg_h']} deg/h (resid {b['resid_deg']} deg, "
            f"{b['source'] or 'unrated'}); predicted {b['pred_deg_h']} env {b['pred_env_deg_h']} "
            f"[{b.get('era') or '-'}]")
    lines.append("")
    for rig, c in rep["cost"].items():
        ms, mn = c["measured"]["sub"] or {}, c["measured"]["night"] or {}
        lines.append(f"  cost {rig} ({c['scale_arcsec']}\"/px, {c['sub_s']} s sub, "
                     f"{c['night_h']} h): measured {c['measured_deg_h']} deg/h -> "
                     f"corner {ms.get('corner_px')} px per sub, {mn.get('corner_px')} px per night; "
                     f"predicted {c['predicted_deg_h']} deg/h -> "
                     f"{(c['predicted']['sub'] or {}).get('corner_px')} / "
                     f"{(c['predicted']['night'] or {}).get('corner_px')} px")
    for n in rep["notes"]:
        lines.append(f"  note: {n}")
    return "\n".join(lines)
