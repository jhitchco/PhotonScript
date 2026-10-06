"""PS-83: ONE star measure for both graders.

Until PS-83 the live grader (telescope_agent.image_validator) and the
backfill grader (scheduler.runs._fast_grade) each had their own detector,
background, HFR and exposure code: backfill measured HFR on a 2x2-binned
frame with a local noise map and minarea 6 (a 10 px HFR gate meant two
different things), had no true FWHM, counted stars without the live cap and
computed the swamp factor from a different noise estimate. Both graders now
call ``measure_frame`` on the same full-resolution frame and record what it
returns, so the same sub gets the same numbers whichever grader saw it.

The definitions are the live grader's (they set tonight's verdicts and the
PS-21 gates were tuned on them):

  detection    3x3 median (hot pixels), sep.Background, extract at 5 sigma of
               the global rms, minarea 9; real stars only: npix >= 12 and
               b > 0.7 px on the measured grid (shared.star_shape.measure).
               One-shot-color frames (Piggy-600, or any BAYERPAT header) are
               measured on the 2x2 superpixel (PS-96), cuts scaled by the bin.
               No saturation or edge cut: saturated stars stay in the medians
               and are counted in sat_stars_pct.
  stars        the kept stars: the brightest MAX_STARS (400). This is the
               gated star count (as live always recorded); stars_detected is
               the count before the cap (info only).
  hfr          median sep.flux_radius(0.5) in an aperture of 6a, native px
  fwhm         median 2.355 a (Gaussian sigma from sep's second moments),
               native px; fwhm_arcsec = fwhm x pixel scale
  ecc          median sqrt(1-(b/a)^2) (shared.star_shape.ECC_DEF)
  ecc_bin      RC16 only, qa_ecc_binned on: the same measure on a 2x2-mean
  hfr_bin      copy (0.47"/px). Known offset (PS-94 synthetic tests): the
               binned moments read 0.04 to 0.06 rounder than truth at FWHM
               8 px. qa_ecc_scale stays "native" by default for that reason.
  background,  3-pass 3-sigma clipped median / std of every 4th pixel
  noise
  swamp        (noise / read noise)^2, the read noise of the frame's rig
               and readout mode (PS-117, shared.rigs.camera_constants: RC16
               HCG 5.66 ADU, RC16 LCG 4.27, Piggy-600 3.27)
  clipped_pct  pixels >= SATURATION_ADU in every 4th pixel
  sat_stars_pct  kept stars whose 3x3 core peak is >= SATURATION_ADU
  corner_spread  (max - min corner median FWHM) / median FWHM

The backfill grader may fall back to a 2x2-binned frame (MemoryError on the
RAM-tight scope PC): ``binned_input=True`` measures that frame with the same
pipeline and reports sizes in native px (``measure_at`` "binned"), which is
close to but not equal to the native measure.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

MEASURE_VERSION = "ps83.1"   # recorded as measure_v on every graded sub
MAX_STARS = 400
SATURATION_ADU = 65000.0
OSC_MEDIAN_MIN_HFR_PX = 2.5  # SUPERPIXEL px (5 native): below this the 3x3
                             # median is skipped on OSC frames (PS-96)

# what both graders must agree on for the same frame (the parity test)
PARITY_KEYS = ("stars", "stars_detected", "hfr", "fwhm_px", "fwhm_arcsec",
               "ecc", "ecc_bin", "hfr_bin", "stars_bin", "background",
               "noise", "swamp", "clipped_pct", "sat_stars_pct", "exposure",
               "corner_spread", "measure_at", "osc")


# ------------------------------------------------------------------ helpers

def _sep():
    from photonscript.shared.star_shape import _sep as s
    return s()


def is_osc(rig: str = "rc16", header=None) -> bool:
    """A one-shot-color frame: the piggyback rig, or a header with
    BAYERPAT (so a mislabeled OSC frame still gets the color-safe measure)."""
    if rig == "piggyback":
        return True
    try:
        return bool(str((header or {}).get("BAYERPAT") or "").strip())
    except Exception:  # noqa: BLE001
        return False


def superpixel(data: np.ndarray) -> np.ndarray:
    """2x2 sum of the Bayer mosaic (odd last row/column dropped)."""
    h, w = (data.shape[0] // 2) * 2, (data.shape[1] // 2) * 2
    d = np.asarray(data[:h, :w], dtype=np.float32)
    return d[0::2, 0::2] + d[0::2, 1::2] + d[1::2, 0::2] + d[1::2, 1::2]


def background_noise(sample: np.ndarray) -> tuple[float, float]:
    """Background and noise from 3 passes of 3-sigma clipping (callers pass
    every 4th pixel: same answer, ~16x less memory)."""
    clipped = np.asarray(sample).flatten()
    for _ in range(3):
        mean = np.mean(clipped)
        std = np.std(clipped)
        clipped = clipped[np.abs(clipped - mean) < 3 * std]
    return float(np.median(clipped)), float(np.std(clipped))


def _empty_arrays() -> dict:
    z = np.zeros(0, dtype=np.float64)
    return {k: z for k in ("x", "y", "a", "b", "theta", "flux", "hfr",
                           "fwhm", "ecc")}


def _sep_superpixel(sp: np.ndarray, median: bool,
                    threshold: float = 5.0) -> dict:
    """sep on a superpixel image (PS-96); per-star arrays in NATIVE px. The
    real-star cuts are the native ones scaled by the 2x bin (area / 4,
    lengths / 2)."""
    sep = _sep()
    from photonscript.shared.star_shape import ecc_sqrt
    d = np.ascontiguousarray(sp, dtype=np.float32)
    if median:
        from scipy import ndimage as _ndi
        d = _ndi.median_filter(d, size=3)
    bkg = sep.Background(d)
    sub = d - bkg
    objs = sep.extract(sub, threshold, err=bkg.globalrms, minarea=3)
    objs = objs[(objs["npix"] >= 3) & (objs["b"] > 0.35)]
    n_detected = int(len(objs))
    if len(objs) > MAX_STARS:
        objs = objs[np.argsort(objs["flux"])[::-1][:MAX_STARS]]
    if not len(objs):
        return {"n_detected": n_detected, **_empty_arrays()}
    r, _ = sep.flux_radius(sub, objs["x"], objs["y"], 6.0 * objs["a"], 0.5)
    r = np.where(np.isfinite(r) & (r > 0), r, objs["a"])
    a = np.asarray(objs["a"], dtype=np.float64)
    b = np.asarray(objs["b"], dtype=np.float64)
    return {
        "n_detected": n_detected,
        # superpixel i covers native 2i and 2i+1: its center is 2i+0.5
        "x": 2.0 * np.asarray(objs["x"], dtype=np.float64) + 0.5,
        "y": 2.0 * np.asarray(objs["y"], dtype=np.float64) + 0.5,
        "a": 2.0 * a, "b": 2.0 * b,
        "theta": np.asarray(objs["theta"], dtype=np.float64),
        "flux": np.asarray(objs["flux"], dtype=np.float64),
        "hfr": 2.0 * np.asarray(r, dtype=np.float64),
        "fwhm": 2.0 * a * 2.355,
        "ecc": ecc_sqrt(a, b),
    }


def detect_osc(sp: np.ndarray) -> dict:
    """PS-96: first pass with the 3x3 median (robust to hot pixels); if the
    stars are small (median HFR under OSC_MEDIAN_MIN_HFR_PX superpixels)
    measure again without it, since the median would widen and square them.
    A single hot photosite is a 1-superpixel spike, below the npix cut."""
    st = _sep_superpixel(sp, median=True)
    hfr = st["hfr"][st["hfr"] > 0]
    if len(hfr) and float(np.median(hfr)) / 2.0 < OSC_MEDIAN_MIN_HFR_PX:
        raw = _sep_superpixel(sp, median=False)
        if len(raw["x"]):
            st = raw
    return st


def detect_mono(data: np.ndarray, binned: bool = False) -> dict:
    """The live sep pipeline (shared.star_shape.measure) as per-star arrays
    in native px."""
    from photonscript.shared import star_shape
    res = star_shape.measure(data, binned=binned, max_stars=MAX_STARS)
    st = res.get("stars") if res else None
    out = {"n_detected": int(res.get("n_detected") or 0) if res else 0}
    if not st:
        out.update(_empty_arrays())
        return out
    out.update({k: np.asarray(st[k], dtype=np.float64)
                for k in ("x", "y", "a", "b", "theta", "flux", "hfr",
                          "fwhm", "ecc")})
    return out


def detect_fallback(data: np.ndarray, threshold: float = 5.0) -> dict:
    """No sep: threshold + label for a star COUNT and positions only. No
    HFR, FWHM or ecc is invented (the old area estimate quantized to one
    value for every frame)."""
    from scipy import ndimage
    sample = data[::4, ::4]
    med = float(np.median(sample))
    mad = float(np.median(np.abs(sample - med))) * 1.4826 or 1.0
    binary = data > med + threshold * mad
    labeled, num = ndimage.label(binary)
    out = _empty_arrays()
    out["n_detected"] = 0
    if num == 0:
        return out
    sizes = ndimage.sum(binary, labeled, np.arange(1, num + 1))
    lbl = np.nonzero(sizes >= 6)[0] + 1
    out["n_detected"] = int(len(lbl))
    if not len(lbl):
        return out
    lbl = lbl[np.argsort(sizes[lbl - 1])[::-1][:MAX_STARS]]
    cents = ndimage.center_of_mass(binary, labeled, lbl)
    out["y"] = np.array([c[0] for c in cents], dtype=np.float64)
    out["x"] = np.array([c[1] for c in cents], dtype=np.float64)
    out["flux"] = np.asarray(sizes[lbl - 1], dtype=np.float64)
    return out


def exposure(data: np.ndarray, xs, ys, noise: float, config,
             coord_scale: float = 1.0,
             read_noise: float | None = None) -> dict:
    """Is the sub sky-limited without clipping?

    swamp = (noise / read-noise floor)^2: >= 10 fully sky-limited, 3 to 10
    fine, < 3 read noise dominates (longer subs pay off). clipped_pct and
    sat_stars_pct catch the other end (blown pixels, saturated cores).
    `noise` is native-equivalent; star coordinates are native px and
    `coord_scale` maps them onto `data` (0.5 for a binned frame).
    `read_noise` (ADU) is the frame's read noise (PS-117); None = the
    config view's camera_read_noise_adu."""
    sample = data[::4, ::4]
    clipped_pct = float((sample >= SATURATION_ADU).mean() * 100.0)
    sat_star_pct = None
    n = len(xs)
    if n:
        h, w = data.shape
        sat = 0
        for x, y in zip(xs, ys):
            x, y = int(round(x * coord_scale)), int(round(y * coord_scale))
            if 1 <= x < w - 1 and 1 <= y < h - 1 and \
                    float(data[y - 1:y + 2, x - 1:x + 2].max()) >= SATURATION_ADU:
                sat += 1
        sat_star_pct = round(sat / n * 100.0, 1)
    if read_noise is None:
        read_noise = getattr(config, "camera_read_noise_adu", 8.0)
    rn = max(float(read_noise), 0.1)
    swamp = round((noise / rn) ** 2, 1)
    if sat_star_pct is not None and sat_star_pct > 5.0:
        flag = "sat-stars"
    elif clipped_pct > 0.05:
        flag = "clipped"
    elif swamp < 3.0:
        flag = "under"
    else:
        flag = "ok"
    return {"clipped_pct": round(clipped_pct, 3),
            "sat_stars_pct": sat_star_pct, "swamp": swamp,
            "exposure": flag}


def corner_spread(xs, ys, fwhm, shape: tuple[int, int],
                  median_fwhm: float | None):
    """Corner FWHM spread relative to the frame median (collimation / tilt
    watch, passive on every sub). `shape` is the native (h, w)."""
    if not len(xs) or not median_fwhm or median_fwhm <= 0:
        return None
    h, w = shape
    meds = []
    for (x0, x1, y0, y1) in [(0, w / 3, 0, h / 3), (2 * w / 3, w, 0, h / 3),
                             (0, w / 3, 2 * h / 3, h),
                             (2 * w / 3, w, 2 * h / 3, h)]:
        vals = [f for x, y, f in zip(xs, ys, fwhm)
                if x0 <= x < x1 and y0 <= y < y1 and f > 0]
        if len(vals) >= 3:
            meds.append(float(np.median(vals)))
    if len(meds) < 3:
        return None
    return float((max(meds) - min(meds)) / median_fwhm)


def _med(arr, positive: bool = False):
    a = np.asarray(arr, dtype=np.float64)
    a = a[np.isfinite(a)]
    if positive:
        a = a[a > 0]
    return float(np.median(a)) if len(a) else None


def _r(v, nd):
    return None if v is None else round(float(v), nd)


# --------------------------------------------------------------- the measure

def measure_frame(data: np.ndarray, config, rig: str = "rc16", *,
                  pixel_scale: float | None = None, osc: bool = False,
                  binned_input: bool = False, grader: str = "",
                  header=None) -> dict:
    """Every star and exposure metric both graders record, from one frame.

    `data`: the full-resolution frame (BZERO / BSCALE applied), or with
    binned_input=True its 2x2 mean (fallback). `config` is the rig's view
    (shared.rigs.rig_config): pixel scale, read noise, qa_ecc_binned,
    qa_star_sidecar_max. `header` (the FITS header, optional) picks the
    read noise for the readout mode (READOUTM, PS-117). Returns
    record-named keys (PARITY_KEYS plus graded_by, ecc_def, measure_v, snr) and `star_table` (PS-80 sidecar
    dict, or None) and `_stars` (the per-star arrays, native px)."""
    from photonscript.shared.star_shape import ECC_DEF, bin2x2_mean
    if pixel_scale is None:
        pixel_scale = float(getattr(config, "pixel_scale_arcsec", 1.0))
    k = 2.0 if binned_input else 1.0          # native px per input px
    native_shape = (int(data.shape[0] * k), int(data.shape[1] * k))

    # background + noise (native-equivalent: a 2x2 mean halves the noise)
    step = 2 if binned_input else 4
    background, noise = background_noise(data[::step, ::step])
    noise *= k

    have_sep = _sep() is not None
    if not have_sep:
        st = detect_fallback(data)
        st["x"], st["y"] = st["x"] * k, st["y"] * k
    elif osc:
        # a 2x2 mean of the mosaic IS the superpixel / 4 (same grid)
        sp = (np.asarray(data, dtype=np.float32) * 4.0 if binned_input
              else superpixel(data))
        st = detect_osc(sp)
        del sp
    else:
        st = detect_mono(data, binned=binned_input)

    n = int(len(st["x"]))
    hfr = fwhm_px = ecc = None
    if have_sep and n:
        hfr = _med(st["hfr"], positive=True)
        fwhm_px = _med(st["fwhm"], positive=True)
        ecc = _med(st["ecc"])
    fwhm_arcsec = fwhm_px * pixel_scale if fwhm_px is not None else None
    cs = corner_spread(st["x"], st["y"], st["fwhm"], native_shape, fwhm_px) \
        if have_sep else None
    from photonscript.shared.rigs import camera_constants
    ex = exposure(data, st["x"], st["y"], noise, config,
                  coord_scale=1.0 / k,
                  read_noise=camera_constants(config, header)["read_noise_adu"])

    # PS-94: the 0.47"/px measure (RC16 only, qa_ecc_binned)
    ecc_bin = hfr_bin = stars_bin = None
    if rig == "rc16" and not osc and have_sep and \
            bool(getattr(config, "qa_ecc_binned", True)):
        if binned_input:
            ecc_bin, hfr_bin, stars_bin = ecc, hfr, n
        else:
            try:
                from photonscript.shared import star_shape
                rb = star_shape.measure(bin2x2_mean(data), binned=True,
                                        max_stars=MAX_STARS)
                if rb is not None:
                    ecc_bin, hfr_bin, stars_bin = (rb["ecc"], rb["hfr_px"],
                                                   rb["n"])
            except Exception as e:  # noqa: BLE001 - never costs a grade
                logger.debug("binned measure skipped: %s", e)

    table = None
    try:
        from photonscript.shared.star_table import build
        n_max = int(getattr(config, "qa_star_sidecar_max", 500) or 0)
        if have_sep and n and n_max > 0:
            table = build(st["x"], st["y"], st["hfr"], st["ecc"],
                          theta=st["theta"], flux=st["flux"],
                          w=native_shape[1], h=native_shape[0], limit=n_max,
                          grader=grader + ("-superpixel" if osc else ""),
                          rig=rig, ecc_def=ECC_DEF)
    except Exception as e:  # noqa: BLE001
        logger.debug("star table skipped: %s", e)

    return {
        "stars": n,
        "stars_detected": int(st.get("n_detected") or 0),
        "hfr": _r(hfr, 2),
        "fwhm_px": _r(fwhm_px, 2),
        "fwhm_arcsec": _r(fwhm_arcsec, 2),
        "ecc": _r(ecc, 3),
        "ecc_bin": _r(ecc_bin, 3), "hfr_bin": _r(hfr_bin, 2),
        "stars_bin": stars_bin,
        "background": round(background, 1),
        "noise": round(noise, 2),
        "snr": round(background / noise, 1) if noise > 0 else 0.0,
        "corner_spread": _r(cs, 3),
        **ex,
        "measure_at": "binned" if binned_input else "native",
        "osc": bool(osc),
        "ecc_def": ECC_DEF,
        "measure_v": MEASURE_VERSION,
        "graded_by": grader if have_sep
        else "no-sep (install sep for HFR/ecc)",
        "star_table": table,
        "_stars": st,
    }


def load_native(path) -> np.ndarray:
    """Full-resolution float32 frame of a FITS file, BZERO / BSCALE applied
    (about 104 MB for a 26 MP frame). Both graders load through here."""
    from astropy.io import fits as _fits
    with _fits.open(Path(path), memmap=True,
                    do_not_scale_image_data=True) as hdul:
        hdr = hdul[0].header
        data = np.array(hdul[0].data, dtype=np.float32)
        bscale, bzero = float(hdr.get("BSCALE", 1)), float(hdr.get("BZERO", 0))
    if bscale != 1.0:
        data *= bscale
    if bzero:
        data += bzero
    return data
