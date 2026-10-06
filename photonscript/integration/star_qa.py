"""PS-22: per-sub star QA before stacking, incl. the second-star-set check.

Library module form of the M31_OSC4 v4b scripts (qa_v4b_120s.py,
qa_v4b_ghost_check.py, qa_v4b_ghost_stack.py). Differences: it runs on the
RAW subs (Bayer frames as 2x2 superpixels, coordinates reported in native
px), before any PixInsight pass, and registers each sub's star list to a
reference star list in Python (shift + rotation, a 180 deg pier flip tried)
instead of reading StarAlignment's output.

Per sub: star count, median HFD / FWHM / eccentricity, elongation alignment
(theta_R, 1 = every elongated star points the same way), sky level and rms,
the doubled-star test (stars paired at one common offset: a short jump during
the exposure) and, against the reference, the fraction of the sub's stars
that land on a reference star.

The second-star-set (ghost) check, the v4b lesson: a sub where the mount sat
on two pointings during one exposure registers cleanly on its main field
but carries a second, shifted copy of the stars. Those extra stars are off
the reference AND land on different positions from sub to sub, while the
extra stars of a clean sub are real faint stars the reference missed, which
every sub shares. So:
  extra_frac      = stars off the reference / stars inside its footprint
  coherent_frac   = of those extras, the share seen as extras in at least
                    `coherence_votes` other subs too (within match_tol_px)
  second_set_frac = extra_frac * (1 - coherent_frac)
Reject when second_set_frac > second_set (0.10); with too few aligned subs
for the vote, fall back to v4b's plain rule extra_frac > extra_max (0.22).
ghost_offset() reports the shift of the second copy (report only).

Relative tests use the median of the 4 subs on each side in time within the
same (filter, exposure) group, so 120 s subs are not judged against 400 s
ones. All functions on star tables are pure (tested with synthetic tables);
measure_sub() is the only one that reads a FITS file (read-only).
"""

from __future__ import annotations

import csv
import math
import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np

from photonscript.shared.star_match import _rigid_fit


@dataclass
class Thresholds:
    low_stars: float = 0.6        # stars < 0.6 x neighbors
    bright_sky: float = 1.5       # sky > 1.5 x neighbors
    soft_hfd: float = 1.35        # HFD > 1.35 x neighbors
    trailed_ecc: float = 0.70     # median eccentricity
    doubled: float = 0.12         # stars paired at one offset
    second_set: float = 0.10      # extra AND incoherent stars / stars
    extra_max: float = 0.22       # v4b rule when the coherence vote is unavailable
    min_coverage: float = 0.30    # share of the sub's stars inside the reference
    neighbors: int = 4            # subs each side for the relative tests
    coherence_votes: int = 5      # "coherent" = extra in >= this many other subs
    coherence_min_subs: int = 6   # aligned subs needed for the vote
    match_tol_px: float = 4.0     # native px (2 superpixels on OSC)


# ------------------------------------------------------------- detection

def detect(data: np.ndarray, osc: bool, thresh: float = 8.0,
           max_stars: int = 4000) -> dict:
    """sep on a raw frame (Bayer frames as 2x2 superpixel sums). Per-star
    arrays in NATIVE px, brightest first, plus sky / sky_rms in ADU per
    photosite."""
    from photonscript.shared.star_measure import superpixel
    from photonscript.shared.star_shape import _sep
    sep = _sep()
    k = 2 if osc else 1
    img = superpixel(data) if osc else np.asarray(data, dtype=np.float32)
    g = np.ascontiguousarray(img, dtype=np.float32)
    try:
        sep.set_extract_pixstack(10_000_000)
        sep.set_sub_object_limit(4096)
    except Exception:  # noqa: BLE001 - older sep
        pass
    bkg = sep.Background(g, bw=128 // k, bh=128 // k, fw=3, fh=3)
    back = bkg.back()
    d = g - back
    obj = sep.extract(d, thresh, err=bkg.globalrms, minarea=5, deblend_cont=0.005)
    sat = 0.9 * 65535.0 * (4 if osc else 1)
    peak = obj["peak"] + back[obj["y"].astype(int).clip(0, g.shape[0] - 1),
                              obj["x"].astype(int).clip(0, g.shape[1] - 1)]
    ok = (obj["flag"] == 0) & (peak < sat) & (obj["a"] > 0.6) & (obj["npix"] < 2000)
    obj = obj[ok]
    empty = {"x": np.zeros(0), "y": np.zeros(0), "a": np.zeros(0), "b": np.zeros(0),
             "theta": np.zeros(0), "flux": np.zeros(0), "hfd": np.zeros(0)}
    sky = float(np.median(back)) / (4 if osc else 1)
    rms = float(bkg.globalrms) / (2 if osc else 1)
    if not len(obj):
        return {**empty, "sky": sky, "sky_rms": rms}
    r50, f1 = sep.flux_radius(d, obj["x"], obj["y"], 6.0 * obj["a"], 0.5,
                              normflux=obj["flux"], subpix=5)
    hfd = 2.0 * r50
    good = (f1 == 0) & np.isfinite(hfd) & (hfd > 0.5) & (hfd < 30)
    o, hfd = obj[good], hfd[good]
    order = np.argsort(o["flux"])[::-1][:max_stars]
    o, hfd = o[order], hfd[order]
    off = 0.5 if osc else 0.0
    return {"x": k * np.asarray(o["x"], float) + off, "y": k * np.asarray(o["y"], float) + off,
            "a": k * np.asarray(o["a"], float), "b": k * np.asarray(o["b"], float),
            "theta": np.asarray(o["theta"], float), "flux": np.asarray(o["flux"], float),
            "hfd": k * np.asarray(hfd, float), "sky": sky, "sky_rms": rms}


def doubled_fraction(x, y, rmin: float = 4.0, rmax: float = 40.0,
                     bin_px: float = 2.0) -> tuple[float, tuple[float, float]]:
    """Fraction of stars with a neighbour (rmin..rmax px) at the one most
    common offset vector, and that vector (v4b doubled test)."""
    from scipy.spatial import cKDTree
    x, y = np.asarray(x, float), np.asarray(y, float)
    if len(x) < 30:
        return 0.0, (0.0, 0.0)
    pairs = cKDTree(np.c_[x, y]).query_pairs(rmax, output_type="ndarray")
    if not len(pairs):
        return 0.0, (0.0, 0.0)
    dx = x[pairs[:, 1]] - x[pairs[:, 0]]
    dy = y[pairs[:, 1]] - y[pairs[:, 0]]
    flip = (dx < 0) | ((dx == 0) & (dy < 0))
    dx[flip] *= -1
    dy[flip] *= -1
    k = np.hypot(dx, dy) >= rmin
    dx, dy = dx[k], dy[k]
    if not len(dx):
        return 0.0, (0.0, 0.0)
    xe = np.arange(0, rmax + 2 * bin_px, bin_px)
    ye = np.arange(-rmax - bin_px, rmax + 2 * bin_px, bin_px)
    h, xe, ye = np.histogram2d(dx, dy, bins=[xe, ye])
    i, j = np.unravel_index(np.argmax(h), h.shape)
    return float(h[i, j] / len(x)), (float(xe[i] + bin_px / 2), float(ye[j] + bin_px / 2))


def shape_metrics(st: dict) -> dict:
    """Median HFD / FWHM / eccentricity and elongation alignment."""
    n = len(st["x"])
    out = {"stars": int(n)}
    if n < 10:
        out.update(hfd_px=math.nan, fwhm_px=math.nan, ecc=math.nan, theta_R=math.nan)
        return out
    a, b = st["a"], st["b"]
    ecc = np.sqrt(np.clip(1 - (b / a) ** 2, 0, 1))
    out["hfd_px"] = float(np.median(st["hfd"]))
    out["fwhm_px"] = float(np.median(2.3548 * np.sqrt((a ** 2 + b ** 2) / 2)))
    out["ecc"] = float(np.median(ecc))
    e = ecc > 0.4
    out["theta_R"] = (float(np.abs(np.mean(np.exp(2j * st["theta"][e]))))
                      if e.sum() > 10 else 0.0)
    return out


# ------------------------------------------------------------- registration

def _flip_xy(xy: np.ndarray, shape) -> np.ndarray:
    w, h = shape
    return np.c_[(w - 1) - xy[:, 0], (h - 1) - xy[:, 1]]


def apply_align(al: dict, xy: np.ndarray) -> np.ndarray:
    """Sub coordinates -> reference coordinates."""
    xy = np.asarray(xy, float).reshape(-1, 2)
    if al.get("flipped"):
        xy = _flip_xy(xy, al["shape"])
    return xy @ np.asarray(al["R"]).T + np.asarray(al["t"])


def _vote_shift(a: np.ndarray, b: np.ndarray, lim: float, bin_px: float):
    from scipy.ndimage import uniform_filter
    d = (b[None, :, :] - a[:, None, :]).reshape(-1, 2)
    d = d[(np.abs(d[:, 0]) <= lim) & (np.abs(d[:, 1]) <= lim)]
    if not len(d):
        return None, 0
    edges = np.arange(-lim, lim + bin_px, bin_px)
    h, xe, ye = np.histogram2d(d[:, 0], d[:, 1], bins=[edges, edges])
    hs = uniform_filter(h, size=3, mode="constant") * 9
    i, j = np.unravel_index(np.argmax(hs), hs.shape)
    return np.array([xe[i] + bin_px / 2, ye[j] + bin_px / 2]), int(hs[i, j])


def _refine(a: np.ndarray, b: np.ndarray, shift, tol: float, min_match: int):
    from scipy.spatial import cKDTree
    tree = cKDTree(b)
    r, t = np.eye(2), np.asarray(shift, float)
    pairs = None
    for it, tl in enumerate((tol * 4, tol * 2, tol, tol)):
        moved = a @ r.T + t
        dist, idx = tree.query(moved, distance_upper_bound=tl)
        ok = np.isfinite(dist)
        if ok.sum() < min_match:
            return None
        pairs = (a[ok], b[idx[ok]])
        r, t, _ = _rigid_fit(*pairs)
    moved = pairs[0] @ r.T + t
    rms = float(np.sqrt(np.mean(np.sum((moved - pairs[1]) ** 2, axis=1))))
    return r, t, len(pairs[0]), rms


def align(sub_xy, ref_xy, shape, *, n_bright: int = 150, bin_px: float = 8.0,
          tol: float = 4.0, min_match: int = 12, try_flip: bool = True,
          max_shift: float | None = None) -> dict | None:
    """Rigid transform carrying the sub's stars onto the reference's.

    Brightest `n_bright` of each vote on the shift (pairwise differences on
    a `bin_px` grid, 3x3 summed), then a rigid fit is refined with a KD tree.
    With try_flip the sub is also tried turned 180 deg about its centre (a
    pier flip); the better match wins. `shape` = (width, height) of the sub.
    Returns {R, t, rotation_deg, flipped, n_matched, rms_px, shape} or None."""
    a0 = np.asarray(sub_xy, float).reshape(-1, 2)[:n_bright]
    b = np.asarray(ref_xy, float).reshape(-1, 2)[:n_bright]
    if len(a0) < min_match or len(b) < min_match:
        return None
    lim = float(max_shift if max_shift is not None else max(shape))
    best = None
    for flipped in ((False, True) if try_flip else (False,)):
        a = _flip_xy(a0, shape) if flipped else a0
        shift, votes = _vote_shift(a, b, lim, bin_px)
        if shift is None or votes < min_match // 2:
            continue
        res = _refine(a, b, shift, tol, min_match)
        if res is None:
            continue
        r, t, n, rms = res
        if best is None or n > best["n_matched"]:
            best = {"R": r.tolist(), "t": [float(t[0]), float(t[1])],
                    "rotation_deg": math.degrees(math.atan2(r[1, 0], r[0, 0])),
                    "flipped": flipped, "n_matched": int(n), "rms_px": rms,
                    "shape": (int(shape[0]), int(shape[1]))}
    return best


def match_stats(sub_xy, ref_xy, al: dict, ref_shape, *, tol: float = 4.0,
                margin: float = 16.0) -> dict:
    """Of the sub's stars that land inside the reference footprint, the share
    within `tol` px of a reference star; the extras (reference coords)."""
    from scipy.spatial import cKDTree
    p = apply_align(al, sub_xy)
    w, h = ref_shape
    inside = ((p[:, 0] >= margin) & (p[:, 0] < w - margin)
              & (p[:, 1] >= margin) & (p[:, 1] < h - margin))
    n_all = len(p)
    p = p[inside]
    out = {"coverage": float(inside.mean()) if n_all else 0.0, "n_inside": int(len(p))}
    if not len(p) or not len(ref_xy):
        out.update(match_frac=0.0, extra=np.zeros((0, 2)), resid_px=math.nan)
        return out
    dist, _ = cKDTree(np.asarray(ref_xy, float)).query(p, distance_upper_bound=3 * tol)
    m = dist < tol
    out["match_frac"] = float(m.mean())
    out["extra"] = p[~m]
    out["resid_px"] = float(np.median(dist[m])) if m.any() else math.nan
    return out


def coherence(extras: list[np.ndarray], radius: float = 4.0,
              min_votes: int = 5) -> list[float]:
    """Per sub: share of its extra stars that are also extras (within
    `radius`) in at least `min_votes` OTHER subs. NaN for a sub with none."""
    from scipy.spatial import cKDTree
    sizes = [len(e) for e in extras]
    if not sum(sizes):
        return [math.nan] * len(extras)
    allxy = np.concatenate([np.asarray(e, float).reshape(-1, 2) for e in extras])
    owner = np.concatenate([np.full(n, i) for i, n in enumerate(sizes)])
    nb = cKDTree(allxy).query_ball_point(allxy, radius)
    votes = np.array([len(set(owner[j].tolist()) - {owner[i]}) for i, j in enumerate(nb)])
    out = []
    for i, n in enumerate(sizes):
        out.append(float(np.mean(votes[owner == i] >= min_votes)) if n else math.nan)
    return out


def ghost_offset(extra_xy, ref_xy, *, n_bright: int = 600, lim: float = 3000.0,
                 bin_px: float = 8.0, tol: float = 6.0) -> dict:
    """Shift of a second star copy: the extras that sit on a reference star
    moved by one common vector (qa_v4b_ghost_check). Report only."""
    from scipy.spatial import cKDTree
    e = np.asarray(extra_xy, float).reshape(-1, 2)
    r = np.asarray(ref_xy, float).reshape(-1, 2)
    if len(e) < 10 or len(r) < 10:
        return {"dx": math.nan, "dy": math.nan, "explained": 0.0}
    d = (e[:n_bright, None, :] - r[None, :n_bright, :]).reshape(-1, 2)
    d = d[(np.abs(d[:, 0]) <= lim) & (np.abs(d[:, 1]) <= lim) & (np.hypot(*d.T) > 2 * bin_px)]
    if not len(d):
        return {"dx": math.nan, "dy": math.nan, "explained": 0.0}
    edges = np.arange(-lim, lim + bin_px, bin_px)
    h, xe, ye = np.histogram2d(d[:, 0], d[:, 1], bins=[edges, edges])
    i, j = np.unravel_index(np.argmax(h), h.shape)
    dx, dy = xe[i] + bin_px / 2, ye[j] + bin_px / 2
    tree = cKDTree(r)
    dist, idx = tree.query(e - [dx, dy], distance_upper_bound=2 * tol)
    ok = dist < tol
    if ok.sum() > 5:
        dx += float(np.median(e[ok, 0] - dx - r[idx[ok], 0]))
        dy += float(np.median(e[ok, 1] - dy - r[idx[ok], 1]))
        dist, _ = tree.query(e - [dx, dy], distance_upper_bound=2 * tol)
        ok = dist < tol
    return {"dx": float(dx), "dy": float(dy), "explained": float(ok.mean())}


# ------------------------------------------------------------- classification

def _nanmed(v):
    v = [x for x in v if x is not None and not (isinstance(x, float) and math.isnan(x))]
    return float(np.median(v)) if v else math.nan


def neighbor_medians(rows: list[dict], k: int = 4) -> None:
    """Add nb_stars / nb_hfd / nb_sky: medians of the k subs each side in
    time within the same (filter, exp) group, excluding the sub itself."""
    groups: dict = {}
    for r in rows:
        groups.setdefault((r.get("filter"), r.get("exp")), []).append(r)
    for g in groups.values():
        g.sort(key=lambda r: (r.get("time", ""), r.get("file", "")))
        n = len(g)
        for i, r in enumerate(g):
            nb = [g[j] for j in range(max(0, i - k), min(n, i + k + 1)) if j != i]
            if not nb:
                nb = [r]
            r["nb_stars"] = _nanmed([q.get("stars") for q in nb])
            r["nb_hfd"] = _nanmed([q.get("hfd_px") for q in nb])
            r["nb_sky"] = _nanmed([q.get("sky_adu") for q in nb])


def _bad(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


def classify(rows: list[dict], thr: Thresholds | None = None,
             coherence_ok: bool = True) -> None:
    """Set action (keep / reject) and reason on every row (in place).
    Rows need: stars, hfd_px, ecc, sky_adu, double_frac, double_vec,
    aligned, coverage, extra_frac, coherent_frac (and filter / exp / time
    for the neighbor medians)."""
    thr = thr or Thresholds()
    neighbor_medians(rows, thr.neighbors)
    for r in rows:
        why = []
        if not _bad(r.get("nb_stars")) and r["stars"] < thr.low_stars * r["nb_stars"]:
            why.append("low_stars(%d vs nb %d)" % (r["stars"], r["nb_stars"]))
        if not _bad(r.get("nb_sky")) and r["nb_sky"] > 0 and r["sky_adu"] > thr.bright_sky * r["nb_sky"]:
            why.append("bright_sky(%.0f vs nb %.0f ADU)" % (r["sky_adu"], r["nb_sky"]))
        if not _bad(r.get("hfd_px")) and not _bad(r.get("nb_hfd")) \
                and r["hfd_px"] > thr.soft_hfd * r["nb_hfd"]:
            why.append("soft(hfd %.2f vs nb %.2f px)" % (r["hfd_px"], r["nb_hfd"]))
        if not _bad(r.get("ecc")) and r["ecc"] > thr.trailed_ecc:
            why.append("trailed(ecc %.2f)" % r["ecc"])
        if not _bad(r.get("double_frac")) and r["double_frac"] > thr.doubled:
            why.append("doubled(%.2f of stars paired at %s px)" % (r["double_frac"], r.get("double_vec", "")))
        if r.get("is_reference"):
            pass
        elif not r.get("aligned") or (r.get("coverage") or 0) < thr.min_coverage:
            why.append("not_registered(no match to the reference)")
        else:
            ef = r.get("extra_frac")
            cf = r.get("coherent_frac")
            if coherence_ok and not _bad(cf):
                ss = ef * (1.0 - cf)
                r["second_set_frac"] = ss
                if ss > thr.second_set:
                    why.append("second_star_set(%.0f%% of stars off the reference, %.0f%% of those "
                               "not shared by other subs)" % (100 * ef, 100 * (1 - cf)))
            else:
                r["second_set_frac"] = ef
                if not _bad(ef) and ef > thr.extra_max:
                    why.append("second_star_set(%.0f%% of stars off the reference)" % (100 * ef))
        r["action"] = "reject" if why else "keep"
        r["reason"] = ";".join(why) if why else "stars_ok"


# ------------------------------------------------------------- I/O + driver

def load_pixels(path) -> np.ndarray:
    from astropy.io import fits
    with fits.open(path, memmap=False) as hd:
        return np.asarray(hd[0].data, dtype=np.float32)


def measure_sub(path: str, osc: bool, thresh: float = 8.0) -> dict:
    """Read one sub (read-only) and measure it. Top level so it pickles."""
    data = load_pixels(path)
    h, w = data.shape[:2]
    st = detect(data, osc, thresh)
    del data
    row = shape_metrics(st)
    row["sky_adu"] = st["sky"]
    row["sky_rms_adu"] = st["sky_rms"]
    fr, vec = doubled_fraction(st["x"], st["y"])
    row["double_frac"] = fr
    row["double_vec"] = "dx %.0f dy %.0f" % vec
    row["_xy"] = np.c_[st["x"], st["y"]].astype(np.float32)
    row["_shape"] = (int(w), int(h))
    return row


ROW_COLS = ["file", "night", "filter", "exp", "time", "action", "reason", "is_reference",
            "stars", "nb_stars", "hfd_px", "nb_hfd", "fwhm_px", "ecc", "theta_R",
            "double_frac", "double_vec", "sky_adu", "nb_sky", "sky_rms_adu",
            "aligned", "flipped", "rotation_deg", "coverage", "match_frac", "extra_frac",
            "coherent_frac", "second_set_frac", "resid_px", "ghost_dx_px", "ghost_dy_px",
            "ghost_explained"]


def pick_reference(rows: list[dict], thr: Thresholds, sample: int = 12) -> int:
    """Index of the reference sub: from the longest exposure group, the
    3 subs with the most stars among the sharp ones (HFD <= group median, no
    doubling, not trailed); of those, the one whose stars the other subs
    reproduce best (a reference with a second star copy loses here)."""
    if not rows:
        raise ValueError("no subs")
    longest = max(r["exp"] for r in rows)
    grp = [i for i, r in enumerate(rows) if r["exp"] == longest and r["stars"] >= 10]
    if not grp:
        grp = list(range(len(rows)))
    med_hfd = _nanmed([rows[i]["hfd_px"] for i in grp])
    cand = [i for i in grp
            if (_bad(med_hfd) or _bad(rows[i]["hfd_px"]) or rows[i]["hfd_px"] <= med_hfd)
            and (rows[i]["double_frac"] or 0) <= thr.doubled
            and (_bad(rows[i]["ecc"]) or rows[i]["ecc"] <= thr.trailed_ecc)] or grp
    cand = sorted(cand, key=lambda i: -rows[i]["stars"])[:3]
    if len(cand) == 1:
        return cand[0]
    others = [i for i in grp if i not in cand]
    if len(others) > sample:
        others = [others[int(k * len(others) / sample)] for k in range(sample)]
    best, best_score = cand[0], -1.0
    for c in cand:
        ref = rows[c]
        scores = []
        for o in others:
            # align the candidate onto the other sub and ask how many of the
            # candidate's stars the other sub has
            al = align(ref["_xy"], rows[o]["_xy"], ref["_shape"], tol=thr.match_tol_px)
            if al is None:
                scores.append(0.0)
                continue
            m = match_stats(ref["_xy"], rows[o]["_xy"], al, rows[o]["_shape"], tol=thr.match_tol_px)
            scores.append(m["match_frac"])
        s = float(np.median(scores)) if scores else 0.0
        if s > best_score:
            best, best_score = c, s
    return best


def assess(rows: list[dict], thr: Thresholds | None = None) -> int:
    """Register every measured row to the chosen reference, run the
    coherence vote and classify. Rows need _xy / _shape from measure_sub.
    Returns the reference index."""
    thr = thr or Thresholds()
    ref_i = pick_reference(rows, thr)
    ref = rows[ref_i]
    extras = []
    for i, r in enumerate(rows):
        r["is_reference"] = i == ref_i
        if i == ref_i:
            r.update(aligned=True, flipped=False, rotation_deg=0.0, coverage=1.0,
                     match_frac=1.0, extra_frac=0.0, resid_px=0.0)
            extras.append(np.zeros((0, 2)))
            continue
        al = align(r["_xy"], ref["_xy"], r["_shape"], tol=thr.match_tol_px)
        if al is None:
            r.update(aligned=False, coverage=0.0)
            extras.append(np.zeros((0, 2)))
            continue
        m = match_stats(r["_xy"], ref["_xy"], al, ref["_shape"], tol=thr.match_tol_px)
        r.update(aligned=True, flipped=al["flipped"], rotation_deg=al["rotation_deg"],
                 coverage=m["coverage"], match_frac=m["match_frac"],
                 extra_frac=1.0 - m["match_frac"], resid_px=m["resid_px"])
        extras.append(m["extra"] if m["coverage"] >= thr.min_coverage else np.zeros((0, 2)))
    n_aligned = sum(1 for r in rows if r.get("aligned") and not r["is_reference"])
    vote_ok = n_aligned >= thr.coherence_min_subs
    if vote_ok:
        cf = coherence(extras, radius=thr.match_tol_px, min_votes=thr.coherence_votes)
        for r, c in zip(rows, cf):
            r["coherent_frac"] = c if not r["is_reference"] else math.nan
            if r.get("aligned") and not r["is_reference"] and _bad(c):
                r["coherent_frac"] = 1.0       # no extras at all
    for r, e in zip(rows, extras):
        if len(e) >= 10 and (r.get("extra_frac") or 0) > thr.second_set:
            g = ghost_offset(e, ref["_xy"])
            r.update(ghost_dx_px=g["dx"], ghost_dy_px=g["dy"], ghost_explained=g["explained"])
    classify(rows, thr, coherence_ok=vote_ok)
    return ref_i


def run_qa(frames, *, workers: int | None = None, thr: Thresholds | None = None,
           echo=print) -> tuple[list[dict], int]:
    """Measure + assess every Frame (one star-QA pass over the raw subs).
    Returns (rows, reference index). Rows keep _xy for callers that want it."""
    thr = thr or Thresholds()
    frames = list(frames)
    workers = workers or max(1, min(8, (os.cpu_count() or 2) - 1))
    paths = [str(f.path) for f in frames]
    oscs = [f.is_osc for f in frames]
    rows: list[dict] = []
    if workers > 1 and len(frames) > 1:
        with ProcessPoolExecutor(workers) as ex:
            for i, row in enumerate(ex.map(measure_sub, paths, oscs)):
                rows.append(row)
                if (i + 1) % 20 == 0:
                    echo(f"  star QA: measured {i + 1}/{len(frames)}")
    else:
        rows = [measure_sub(p, o) for p, o in zip(paths, oscs)]
    for f, r in zip(frames, rows):
        r.update(file=f.name, night=f.night, filter=f.filter, exp=round(f.exp, 2),
                 time=f.date_obs)
    ref_i = assess(rows, thr)
    return rows, ref_i


def _fmt(v):
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return "" if math.isnan(v) else "%.4f" % v
    return "" if v is None else v


def write_csv(path: Path, rows: list[dict]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="ascii", errors="replace") as f:
        w = csv.writer(f)
        w.writerow(ROW_COLS)
        for r in rows:
            w.writerow([_fmt(r.get(k)) for k in ROW_COLS])


def thresholds_dict(thr: Thresholds) -> dict:
    return asdict(thr)
