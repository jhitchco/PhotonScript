"""PS-94: one eccentricity formula and one star-shape measure for every grader.

Until PS-94 the live grader used e = sqrt(1-(b/a)^2) on the native frame and
the backfill grader used e = 1-b/a on a 2x2-binned frame, both judged against
one ecc gate. 1-b/a = 0.20 is the same star as sqrt-form 0.60, so the two
disagreed far more about formula than about scale. Everything now speaks the
sqrt form; records and sidecars say which one they hold in ``ecc_def``.

    ecc_sqrt(a, b)          sqrt(1-(b/a)^2) (scalar or numpy arrays)
    lin_to_sqrt(e)          1-b/a value -> sqrt form (monotonic, so a median
                            converts exactly: rescore fixes history without
                            reopening FITS)
    to_sqrt(e, ecc_def)     normalize a stored value by its ecc_def
    bin2x2_mean(arr)        2x2 software bin (mean), float32, no full-res
                            float copy (works on a memmap)
    measure(data, binned)   the live grader's sep pipeline (3x3 median, real
                            stars only, brightest 400): medians + per-star
                            arrays. binned=True means `data` is a 2x2-binned
                            frame; sizes and coordinates come back in native
                            pixels either way.
"""

from __future__ import annotations

import math

import numpy as np

ECC_DEF = "sqrt(1-(b/a)^2)"   # what every grader writes from PS-94 on
LIN_DEF = "1-b/a"             # pre-PS-94 backfill ("sep-binned") records


def ecc_sqrt(a, b):
    """sqrt(1-(b/a)^2). Scalars give a float (0.0 when a <= 0); arrays give
    an array (nan where a <= 0)."""
    if np.ndim(a) == 0 and np.ndim(b) == 0:
        a, b = float(a), float(b)
        if not a > 0:
            return 0.0
        return math.sqrt(max(1.0 - (b / a) ** 2, 0.0))
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    with np.errstate(divide="ignore", invalid="ignore"):
        q = np.where(a > 0, b / a, np.nan)
    return np.sqrt(np.clip(1.0 - q * q, 0.0, None))


def lin_to_sqrt(e):
    """1-b/a -> sqrt(1-(b/a)^2): with q = b/a = 1-e, sqrt(1-q^2) =
    sqrt(e(2-e)). lin 0.20 -> 0.60, 0.25 -> 0.66, 0.30 -> 0.71. None stays
    None; arrays convert element-wise."""
    if e is None:
        return None
    if np.ndim(e) == 0:
        x = min(max(float(e), 0.0), 1.0)
        return math.sqrt(x * (2.0 - x))
    x = np.clip(np.asarray(e, dtype=np.float64), 0.0, 1.0)
    return np.sqrt(x * (2.0 - x))


def to_sqrt(e, ecc_def: str | None):
    """A stored eccentricity in sqrt form. ecc_def "1-b/a" converts; anything
    else (the sqrt form, or unknown) is returned as is."""
    if e is None:
        return None
    if str(ecc_def or "").replace(" ", "") == LIN_DEF:
        return lin_to_sqrt(e)
    return e


def bin2x2_mean(arr) -> np.ndarray:
    """2x2 mean bin as float32 (odd last row / column dropped). Built from
    four strided views, so a uint16 memmap never gets a full-res float copy
    (same scheme as runs._load_binned)."""
    h2, w2 = arr.shape[0] // 2 * 2, arr.shape[1] // 2 * 2
    out = arr[0:h2:2, 0:w2:2].astype(np.float32)
    out += arr[1:h2:2, 0:w2:2]
    out += arr[0:h2:2, 1:w2:2]
    out += arr[1:h2:2, 1:w2:2]
    out *= 0.25
    return out


def _sep():
    try:
        import sep
        return sep
    except ImportError:
        try:
            import sep_pjw as sep
            return sep
        except ImportError:
            return None


def measure(data, binned: bool = False, threshold: float = 5.0,
            max_stars: int = 400) -> dict | None:
    """Star shape on one frame with the live grader's sep pipeline.

    Returns None without sep. Otherwise {"n", "n_detected", "ecc", "hfr_px",
    "fwhm_px", "scale", "stars"}: medians over the kept stars (ecc in sqrt
    form; HFR and FWHM in NATIVE pixels, so x2 when binned) and the per-star
    arrays (x, y, a, b, theta, flux, hfr, fwhm, ecc; x, y, a, b, hfr, fwhm in
    native pixels). n is the kept stars (at most max_stars, the brightest);
    n_detected the stars that passed the cuts before that cap. n == 0 gives
    None medians. PS-83: shared.star_measure builds both graders' metrics
    on top of this."""
    sep = _sep()
    if sep is None:
        return None
    from scipy import ndimage
    scale = 2.0 if binned else 1.0
    data_c = np.ascontiguousarray(data, dtype=np.float32)
    # 3x3 median: erases single-pixel hot pixels, barely touches a star
    # (see image_validator._detect_stars, the 2026-09-04 lesson)
    data_c = ndimage.median_filter(data_c, size=3)
    bkg = sep.Background(data_c)
    data_sub = data_c - bkg
    del data_c
    objs = sep.extract(data_sub, threshold, err=bkg.globalrms, minarea=9)
    objs = objs[(objs["npix"] >= 12) & (objs["b"] > 0.7)]
    n_detected = int(len(objs))
    if len(objs) > max_stars:
        objs = objs[np.argsort(objs["flux"])[::-1][:max_stars]]
    empty = {"n": 0, "n_detected": n_detected, "ecc": None, "hfr_px": None,
             "fwhm_px": None, "scale": scale, "stars": None}
    if not len(objs):
        return empty
    r, _ = sep.flux_radius(data_sub, objs["x"], objs["y"],
                           6.0 * objs["a"], 0.5)
    r = np.where(np.isfinite(r) & (r > 0), r, objs["a"])
    e = ecc_sqrt(objs["a"], objs["b"])
    fwhm = objs["a"] * 2.355
    stars = {"x": objs["x"] * scale, "y": objs["y"] * scale,
             "a": objs["a"] * scale, "b": objs["b"] * scale,
             "theta": np.asarray(objs["theta"], dtype=np.float64),
             "flux": np.asarray(objs["flux"], dtype=np.float64),
             "hfr": r * scale, "fwhm": fwhm * scale, "ecc": e}
    return {"n": int(len(objs)), "n_detected": n_detected,
            "ecc": float(np.median(e)),
            "hfr_px": float(np.median(r)) * scale,
            "fwhm_px": float(np.median(fwhm)) * scale,
            "scale": scale, "stars": stars}
