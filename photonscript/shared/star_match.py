"""PS-96: register two star lists (sidecar tables) without opening a FITS.

offset(tbl_a, tbl_b) finds the shift (and small rotation) that carries the
stars of sub A onto the stars of sub B. Both tables are PS-80 star sidecars
(shared.star_table: x, y lists in native px, brightest first). Method:

1. brightest `n_bright` stars of each;
2. every pairwise (dx, dy) = B - A within `max_shift_px` votes on a 1 px
   grid; the peak (3x3 summed, so a shift on a bin edge is not split) is the
   coarse shift: true pairs pile up in one bin, chance pairs spread out;
3. refine with a cKDTree: match A + shift to B within `tol_px`, fit a
   rigid transform (rotation + translation, least squares), repeat.

The returned dx, dy are the shift at the frame center, so a pure rotation
about the center reads as (0, 0) plus `rotation_deg`. A positive rotation
turns +x toward +y in the stored pixel coordinates. Returns
None when too few stars match (clouds, a slew, a flip: a 180 deg turn is not
searched).
"""

from __future__ import annotations

import math

import numpy as np

N_BRIGHT = 150
MIN_MATCH = 8


def _xy(tbl, n: int) -> np.ndarray:
    xs, ys = tbl.get("x") or [], tbl.get("y") or []
    pts = [(float(x), float(y)) for x, y in zip(xs, ys)
           if x is not None and y is not None]
    return np.asarray(pts[:n], dtype=float).reshape(-1, 2)


def _rigid_fit(a: np.ndarray, b: np.ndarray):
    """Least-squares rotation R and translation t with b = R a + t."""
    ca, cb = a.mean(axis=0), b.mean(axis=0)
    h = (a - ca).T @ (b - cb)
    ang = math.atan2(h[0, 1] - h[1, 0], h[0, 0] + h[1, 1])
    c, s = math.cos(ang), math.sin(ang)
    r = np.array([[c, -s], [s, c]])
    return r, cb - r @ ca, ang


def offset(tbl_a: dict, tbl_b: dict, max_shift_px: float = 60.0,
           n_bright: int = N_BRIGHT, tol_px: float = 1.5,
           min_match: int = MIN_MATCH) -> dict | None:
    """Shift and rotation from star table A to star table B (see module
    doc). Returns {dx, dy, rotation_deg, n_matched, rms_px} or None."""
    if not tbl_a or not tbl_b:
        return None
    a, b = _xy(tbl_a, n_bright), _xy(tbl_b, n_bright)
    if len(a) < min_match or len(b) < min_match:
        return None
    d = (b[None, :, :] - a[:, None, :]).reshape(-1, 2)
    d = d[(np.abs(d[:, 0]) <= max_shift_px) & (np.abs(d[:, 1]) <= max_shift_px)]
    if len(d) == 0:
        return None
    nb = int(math.ceil(max_shift_px)) * 2 + 1
    edges = np.arange(nb + 1) - (nb / 2.0)
    hist, xe, ye = np.histogram2d(d[:, 0], d[:, 1], bins=[edges, edges])
    k = np.ones((3, 3))
    from scipy.signal import convolve2d
    sm = convolve2d(hist, k, mode="same")
    i, j = np.unravel_index(int(np.argmax(sm)), sm.shape)
    if sm[i, j] < min_match:
        return None
    # coarse shift: mean of the votes in the winning 3x3 neighborhood
    x0, x1 = xe[max(i - 1, 0)], xe[min(i + 2, nb)]
    y0, y1 = ye[max(j - 1, 0)], ye[min(j + 2, nb)]
    sel = (d[:, 0] >= x0) & (d[:, 0] < x1) & (d[:, 1] >= y0) & (d[:, 1] < y1)
    shift = d[sel].mean(axis=0)

    from scipy.spatial import cKDTree
    tree = cKDTree(b)
    r, t = np.eye(2), shift
    ia = ib = None
    for tol in (max(tol_px * 2, 3.0), tol_px, tol_px):
        moved = a @ r.T + t
        dist, idx = tree.query(moved, distance_upper_bound=tol)
        ok = np.isfinite(dist)
        if ok.sum() < min_match:
            return None
        # one-to-one: keep the closest A for each B
        best: dict[int, int] = {}
        for ai in np.nonzero(ok)[0]:
            bi = int(idx[ai])
            if bi not in best or dist[ai] < dist[best[bi]]:
                best[bi] = int(ai)
        ia = np.array(list(best.values()))
        ib = np.array(list(best.keys()))
        if len(ia) < min_match:
            return None
        r, t, _ = _rigid_fit(a[ia], b[ib])
    resid = b[ib] - (a[ia] @ r.T + t)
    ang = math.degrees(math.atan2(r[1, 0], r[0, 0]))
    w, h = tbl_a.get("w"), tbl_a.get("h")
    if w and h:
        c = np.array([w / 2.0, h / 2.0])
    else:
        c = a.mean(axis=0)
    dc = (r @ c + t) - c
    return {"dx": round(float(dc[0]), 3), "dy": round(float(dc[1]), 3),
            "rotation_deg": round(ang, 5), "n_matched": int(len(ia)),
            "rms_px": round(float(np.sqrt((resid ** 2).sum(axis=1).mean())), 3)}
