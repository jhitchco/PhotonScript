"""Target core saturation (2026-10-13, M31: "how can we capture whether we
are saturated or not, are we checking that?").

sat_stars_pct counts stars whose peak clips and sat_px_pct / clipped_pct
count clipped pixels over the whole 26 MP frame, so a galaxy nucleus (M31)
or a bright nebula / cluster core that clips covers a negligible fraction of
the frame and never trips either. This module measures the target's own
core on each sub, from a small cutout:

    radius_arcmin(config, target) -> float      core radius (catalog sized)
    locate(config, rig, hdr, target, shape, ...) -> dict | None
                                                 where the core should be
    measure(data, loc, sat_adu, bzero, bscale) -> dict
                                                 core_sat_px, core_peak_adu,
                                                 core_p999 (+ where)
    sub_core(config, rig, path, hdr, target, ...) -> dict
                                                 both graders: memmap the
                                                 FITS, measure, record fields
    master_core(data, r_px, ...) -> dict         the integration master check
                                                 (flat-topped plateau)

Where the core is: a WCS in the sub's header (CRVAL + CD / CDELT), else this
sub's stored plate solve for the night (scheduler.solve_store), else the
header pointing (RA / DEC or OBJCTRA / OBJCTDEC) as the frame center with a
search disk as wide as the offset from the target (no position angle), else
the frame center. Inside the search disk the brightest block median (stars
are too small to move a median) marks the core, so a pointing error or an
unknown rotation still finds the nucleus.

Radius: qa_core_radius_arcmin (1') for galaxies; other types scale with the
catalog size (10% of it, between 1x and 3x the configured radius). The
nucleus is the inner quarter of that disk: its peak and 99.9th percentile
are recorded, and only clipped pixels connected to it count (a saturated
foreground star elsewhere in the disk is not the core).

Never raises from the graders' entry point (sub_core): a failure returns {}
and the scorecard's core row is a skip.
"""

from __future__ import annotations

import logging
import math

import numpy as np

logger = logging.getLogger(__name__)

DEFAULT_RADIUS_ARCMIN = 1.0
OTHER_FRAC = 0.10          # non-galaxy core radius = this x catalog size
OTHER_MAX_X = 3.0          # ... at most this x the configured radius
FRAME_SEARCH_FRAC = 0.25   # no position: search this x the short side
MAX_SEARCH_FRAC = 0.45     # never search wider than this x the short side
BLOCKS_PER_RADIUS = 6      # block size for the search = r_px / this
INNER_FRAC = 0.25          # the nucleus: this x the core radius
PLATEAU_TOL = 0.002        # master check: within 0.2% of the core's height
WIDE_TOL = 0.02            # ... compared with the pixels within 2%
FLAT_RATIO = 0.5           # a plateau: at least half of those sit in the 0.2%
MODES = ("note", "warn", "off")
MIN_WARN_PX = 4            # another rig's scaled clipped-core limit, at least
RC16_REF_SCALE = 0.236     # "/px, when a rig view no longer carries the RC16's
RECORD_KEYS = ("core_sat_px", "core_sat_px_disk", "core_peak_adu", "core_p999",
               "core_sat_adu", "core_src", "core_r_px", "core_xy")


def mode(config) -> str:
    m = str(getattr(config, "qa_core_mode", "note") or "note").strip().lower()
    return m if m in MODES else "note"


def warn_px(config, rig: str = "rc16") -> int:
    """Clipped core pixels above which the core counts as clipped:
    qa_core_sat_warn_px (RC16 pixels); another rig's limit is scaled by the
    pixel area (the Piggy-600's 1.29"/px pixel holds 30x the sky of the
    RC16's 0.236"/px one), at least MIN_WARN_PX."""
    try:
        base = max(0, int(getattr(config, "qa_core_sat_warn_px", 25) or 0))
    except (TypeError, ValueError):
        base = 25
    if (rig or "rc16") == "rc16":
        return base
    try:
        from photonscript.shared.rigs import rig_config
        s0 = float(getattr(config, "pixel_scale_arcsec", 0) or 0)
        s1 = float(getattr(rig_config(config, rig), "pixel_scale_arcsec", 0) or 0)
    except Exception:  # noqa: BLE001
        return base
    if s0 <= 0 or s1 <= 0:
        return base
    if abs(s0 - s1) < 1e-9:      # already the rig's view: the RC16 scale is gone
        s0 = RC16_REF_SCALE
    return max(MIN_WARN_PX, int(round(base * (s0 / s1) ** 2)))


# --------------------------------------------------------------- geometry

def radius_arcmin(config, target) -> float:
    """Core radius for `target`: the configured radius for a galaxy (or an
    unknown target), else 10% of the catalog size between 1x and 3x it."""
    base = float(getattr(config, "qa_core_radius_arcmin", DEFAULT_RADIUS_ARCMIN)
                 or DEFAULT_RADIUS_ARCMIN)
    try:
        from photonscript.shared.astronomy import find_catalog_entry
        e = find_catalog_entry(str(target or "")) if target else None
    except Exception:  # noqa: BLE001
        e = None
    if not e:
        return base
    if "galaxy" in str(e.get("type") or "").lower():
        return base
    try:
        size = float(e.get("size") or 0)
    except (TypeError, ValueError):
        size = 0.0
    if size <= 0:
        return base
    return round(min(OTHER_MAX_X * base, max(base, OTHER_FRAC * size)), 3)


def _num(v):
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _header_wcs(hdr) -> dict | None:
    """{ra, dec, cd, crpix} from a WCS in the header, or None."""
    get = getattr(hdr, "get", None)
    if get is None or get("CRVAL1") is None or get("CRVAL2") is None:
        return None
    if all(get(k) is None for k in ("CD1_1", "CDELT1")):
        return None
    from photonscript.scheduler.solve_store import solution_from_kv
    kv = {k: get(k) for k in ("CRVAL1", "CRVAL2", "CD1_1", "CD1_2", "CD2_1",
                              "CD2_2", "CDELT1", "CDELT2", "CROTA2")
          if get(k) is not None}
    sol = solution_from_kv(kv)
    if not sol or not sol.get("cd"):
        return None
    c1, c2 = _num(get("CRPIX1")), _num(get("CRPIX2"))
    return {"ra": sol["ra"], "dec": sol["dec"], "cd": sol["cd"],
            "crpix": (c1 - 1.0, c2 - 1.0) if c1 is not None and c2 is not None
            else None}


def _stored_solve(config, rig, night, rel_file) -> dict | None:
    if not night or not rel_file:
        return None
    try:
        from photonscript.scheduler.solve_store import lookup
        from photonscript.shared.sub_file import norm_file
        rec = lookup(config, night, rig or "rc16").get(norm_file(rel_file))
    except Exception:  # noqa: BLE001
        return None
    if not rec or not rec.get("solved") or not rec.get("cd"):
        return None
    return {"ra": rec["ra"], "dec": rec["dec"], "cd": rec["cd"], "crpix": None}


def _sky_to_pix(sol: dict, ra: float, dec: float, w: int, h: int):
    """Target (ra, dec) -> (x, y) array pixel through a CD matrix (tangent
    plane, small offsets). x = column, y = row (FITS y - 1)."""
    cd = sol["cd"]
    ra0, dec0 = float(sol["ra"]), float(sol["dec"])
    dra = ((ra - ra0 + 540.0) % 360.0) - 180.0
    e = dra * math.cos(math.radians(dec0))   # degrees east
    n = dec - dec0                            # degrees north
    det = cd[0][0] * cd[1][1] - cd[0][1] * cd[1][0]
    if not det:
        return None
    dx = (cd[1][1] * e - cd[0][1] * n) / det
    dy = (-cd[1][0] * e + cd[0][0] * n) / det
    cx, cy = sol.get("crpix") or ((w - 1) / 2.0, (h - 1) / 2.0)
    return cx + dx, cy + dy


def locate(config, rig: str, hdr, target, shape, *, night: str | None = None,
           rel_file: str | None = None) -> dict | None:
    """{x, y, r_px, search_px, src} for the target core in a frame of
    `shape` (rows, cols), or None when the target is off the frame or no
    pixel scale is known. src: wcs | solve | header | center."""
    from photonscript.shared.rigs import rig_config
    h, w = int(shape[0]), int(shape[1])
    view = rig_config(config, rig or "rc16")
    scale = _num(getattr(view, "pixel_scale_arcsec", None))
    if not scale or scale <= 0 or h < 8 or w < 8:
        return None
    r_px = radius_arcmin(config, target) * 60.0 / scale
    r_px = max(4.0, min(r_px, 0.45 * min(h, w)))
    short = min(h, w)
    tc = None
    if target:
        try:
            from photonscript.shared.pointing import target_coords
            tc = target_coords(config, target)
        except Exception:  # noqa: BLE001
            tc = None
    if tc is not None:
        hw = _header_wcs(hdr)
        sol = hw or _stored_solve(config, rig, night, rel_file)
        if sol is not None:
            src = "wcs" if hw else "solve"
            p = _sky_to_pix(sol, tc[1], tc[2], w, h)
            if p is None:
                return None
            x, y = p
            if not (0 <= x < w and 0 <= y < h):
                return None
            return {"x": x, "y": y, "r_px": r_px, "search_px": 0.5 * r_px,
                    "src": src}
        try:
            from photonscript.scheduler.identify import radec_from_header
            rd = radec_from_header(hdr)
        except Exception:  # noqa: BLE001
            rd = None
        if rd is not None:
            from photonscript.shared.pointing import sep_arcmin
            off_px = sep_arcmin(rd[0], rd[1], tc[1], tc[2]) * 60.0 / scale
            if off_px > 0.5 * math.hypot(w, h):
                return None                       # target not in the frame
            search = min(off_px + r_px, MAX_SEARCH_FRAC * short)
            return {"x": (w - 1) / 2.0, "y": (h - 1) / 2.0, "r_px": r_px,
                    "search_px": search, "src": "header"}
    return {"x": (w - 1) / 2.0, "y": (h - 1) / 2.0, "r_px": r_px,
            "search_px": FRAME_SEARCH_FRAC * short, "src": "center"}


# ------------------------------------------------------------ measurement

def _phys(a, bzero: float, bscale: float):
    a = np.asarray(a, dtype=np.float64)
    if bscale != 1.0:
        a = a * bscale
    if bzero:
        a = a + bzero
    return a


def find_peak_block(data, x0: float, y0: float, search: float, b: int,
                    bzero: float = 0.0, bscale: float = 1.0):
    """The brightest block median within `search` px of (x0, y0): (x, y)
    of the block centers at or above 98% of that maximum, weighted by their
    median (a clipped plateau is flat, so the plateau center rather than
    its first block). Reads block rows one at a time (memmap friendly)."""
    h, w = int(data.shape[-2]), int(data.shape[-1])
    b = max(1, int(b))
    xa = max(0, int(x0 - search)) // b * b
    xb = min(w, int(x0 + search) + 1)
    ya = max(0, int(y0 - search)) // b * b
    yb = min(h, int(y0 + search) + 1)
    nx = (xb - xa) // b
    if nx < 1 or yb - ya < b:
        return x0, y0
    meds, cxs, cys = [], [], []
    xs = xa + (np.arange(nx) + 0.5) * b
    for y in range(ya, yb - b + 1, b):
        rows = _phys(data[..., y:y + b, xa:xa + nx * b], bzero, bscale)
        if rows.ndim == 3:                   # color: the brightest channel
            rows = rows.max(axis=0)
        blk = np.median(rows.reshape(b, nx, b).transpose(1, 0, 2)
                        .reshape(nx, b * b), axis=1)
        cy = y + 0.5 * b
        keep = (xs - x0) ** 2 + (cy - y0) ** 2 <= search ** 2
        meds.append(blk[keep])
        cxs.append(xs[keep])
        cys.append(np.full(int(keep.sum()), cy))
    m = np.concatenate(meds) if meds else np.array([])
    if not m.size:
        return x0, y0
    cx, cy = np.concatenate(cxs), np.concatenate(cys)
    top = float(m.max())
    lo = float(np.median(m))
    sel = m >= lo + 0.98 * (top - lo)
    wts = np.maximum(m[sel] - lo, 1e-9)
    return float((cx[sel] * wts).sum() / wts.sum()), float((cy[sel] * wts).sum() / wts.sum())


def _cutout(data, x: float, y: float, r: float, bzero: float, bscale: float):
    """(values, disk mask, inner mask) of the square around the disk of
    radius r at (x, y), physical units (color: the brightest channel); the
    inner disk is the nucleus, INNER_FRAC x r. None when off the frame."""
    h, w = int(data.shape[-2]), int(data.shape[-1])
    x1, x2 = max(0, int(math.floor(x - r))), min(w, int(math.ceil(x + r)) + 1)
    y1, y2 = max(0, int(math.floor(y - r))), min(h, int(math.ceil(y + r)) + 1)
    if x2 <= x1 or y2 <= y1:
        return None
    cut = _phys(data[..., y1:y2, x1:x2], bzero, bscale)
    if cut.ndim == 3:
        cut = cut.max(axis=0)
    yy, xx = np.mgrid[y1:y2, x1:x2]
    d2 = (xx - x) ** 2 + (yy - y) ** 2
    disk = d2 <= r * r
    ri = max(3.0, INNER_FRAC * r)
    inner = d2 <= ri * ri
    if not inner.any():
        return None
    return cut, disk, inner


def _connected_to(mask, seed) -> int:
    """Pixels of `mask` in the 8-connected regions that touch `seed`."""
    if not (mask & seed).any():
        return 0
    from scipy import ndimage
    lab, _n = ndimage.label(mask, structure=np.ones((3, 3), dtype=bool))
    keep = np.unique(lab[mask & seed])
    keep = keep[keep > 0]
    return int(np.isin(lab, keep).sum()) if keep.size else 0


def measure(data, loc: dict, sat_adu: float, bzero: float = 0.0,
            bscale: float = 1.0) -> dict:
    """Core numbers on a frame (raw + bzero / bscale, or physical ADU).
    core_sat_px: pixels >= sat_adu inside the core disk that belong to a
    clipped region touching the nucleus (the inner quarter of the disk), so
    a saturated foreground star elsewhere in the disk is not the core;
    core_sat_px_disk: every clipped pixel in the disk; core_peak_adu and
    core_p999 (99.9th percentile) of the nucleus (inner disk); core_sat_adu,
    core_src, core_r_px, core_xy ([x, y] after the search). {} when nothing
    could be read."""
    bzero, bscale = float(bzero or 0.0), float(bscale or 1.0)
    x, y, r = float(loc["x"]), float(loc["y"]), float(loc["r_px"])
    search = float(loc.get("search_px") or 0.0)
    b = max(2, int(r / BLOCKS_PER_RADIUS))
    if search > b:
        x, y = find_peak_block(data, x, y, search, b, bzero, bscale)
    c = _cutout(data, x, y, r, bzero, bscale)
    if c is None:
        return {}
    cut, disk, inner = c
    sat = (cut >= float(sat_adu)) & disk
    vin = cut[inner]
    return {"core_sat_px": _connected_to(sat, inner),
            "core_sat_px_disk": int(np.count_nonzero(sat)),
            "core_peak_adu": round(float(vin.max()), 1),
            "core_p999": round(float(np.percentile(vin, 99.9)), 1),
            "core_sat_adu": float(sat_adu), "core_src": loc.get("src"),
            "core_r_px": round(r, 1), "core_xy": [round(x, 1), round(y, 1)]}


def sub_core(config, rig: str, path, hdr, target, *, night: str | None = None,
             rel_file: str | None = None) -> dict:
    """Both graders: the core fields of one sub (RECORD_KEYS), read from a
    memmapped FITS (only the search blocks and the core cutout are read).
    {} when qa_core_mode is off, the sub is a test or the target is off the
    frame; never raises."""
    if mode(config) == "off" or str(target or "").strip() in ("", "?"):
        return {}
    try:
        from photonscript.shared.qa_rules import is_test_record
        if is_test_record({"target": target}):
            return {}
        from astropy.io import fits as _fits
        from photonscript.shared.pixel_stats import saturation_level
        with _fits.open(str(path), memmap=True,
                        do_not_scale_image_data=True) as hdul:
            raw = hdul[0].data
            h0 = hdul[0].header
            if raw is None or raw.ndim < 2:
                return {}
            hh = hdr if hdr is not None else h0
            loc = locate(config, rig, hh, target, raw.shape[-2:],
                         night=night, rel_file=rel_file)
            if loc is None:
                return {}
            out = measure(raw, loc, saturation_level(hh, config),
                          float(h0.get("BZERO", 0) or 0),
                          float(h0.get("BSCALE", 1) or 1))
        return out
    except Exception as e:  # noqa: BLE001 - never costs a grade
        logger.debug("core measure skipped for %s: %s", path, e)
        return {}


# ----------------------------------------------------------------- master

def master_core(data, r_px: float, *, tol: float = PLATEAU_TOL,
                search_frac: float = FRAME_SEARCH_FRAC) -> dict:
    """Integration master check: the brightest core near the frame center
    (a master has no WCS before the finish solves it) and whether the
    nucleus (inner disk) is a flat plateau. A clipped core stays
    flat-topped through calibration and integration (each sub clipped at
    the same level); an unclipped nucleus is peaked. With h = the nucleus
    top above the disk's 5th percentile, n_tight pixels sit within tol x h
    (0.2%) of the top and n_wide within WIDE_TOL x h (2%): a smooth peak has
    n_tight about a tenth of n_wide, a plateau nearly all of it.
    clipped_px = n_tight when n_tight >= FLAT_RATIO x n_wide, else 0. The
    top is the 5th brightest nucleus pixel (one hot pixel does not set it).
    Returns {clipped_px, n_tight, n_wide, peak, p999, x, y, r_px}."""
    h, w = int(data.shape[-2]), int(data.shape[-1])
    r = max(4.0, min(float(r_px), 0.45 * min(h, w)))
    x, y = (w - 1) / 2.0, (h - 1) / 2.0
    b = max(2, int(r / BLOCKS_PER_RADIUS))
    x, y = find_peak_block(data, x, y, search_frac * min(h, w), b)
    c = _cutout(data, x, y, r, 0.0, 1.0)
    if c is None:
        return {}
    cut, disk, inner = c
    fin = np.isfinite(cut)
    v = cut[inner & fin]
    vd = cut[disk & fin]
    if v.size < 10:
        return {}
    top = float(np.partition(v, v.size - 5)[v.size - 5])
    height = top - float(np.percentile(vd, 5))
    if height <= 0:
        n_tight = n_wide = 0
    else:
        n_tight = int(np.count_nonzero(v >= top - tol * height))
        n_wide = int(np.count_nonzero(v >= top - WIDE_TOL * height))
    flat = n_wide > 0 and n_tight >= FLAT_RATIO * n_wide
    return {"clipped_px": n_tight if flat else 0, "n_tight": n_tight,
            "n_wide": n_wide, "peak": round(float(v.max()), 6),
            "p999": round(float(np.percentile(v, 99.9)), 6),
            "x": round(x, 1), "y": round(y, 1), "r_px": round(r, 1)}
