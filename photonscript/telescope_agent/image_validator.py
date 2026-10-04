"""Image validation — quality assessment of captured sub-frames.

Analyzes FITS files for star FWHM, HFR, eccentricity, and tracking quality
to decide whether a frame should be kept or rejected.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from photonscript.shared.models import ImageQualityMetrics
from photonscript.shared.config import PhotonScriptConfig

logger = logging.getLogger(__name__)


def _load_image_data(file_path: str) -> Optional[np.ndarray]:
    """Load image data from FITS or TIFF file."""
    path = Path(file_path)

    if path.suffix.lower() in (".fits", ".fit", ".fts"):
        try:
            from astropy.io import fits
            with fits.open(str(path)) as hdul:
                return hdul[0].data.astype(np.float32)
        except ImportError:
            logger.warning("astropy.io.fits not available for FITS reading")
            return None
    elif path.suffix.lower() in (".tif", ".tiff"):
        img = Image.open(str(path))
        return np.array(img, dtype=np.float64)
    elif path.suffix.lower() in (".png", ".jpg", ".jpeg"):
        img = Image.open(str(path))
        return np.array(img.convert("L"), dtype=np.float64)
    else:
        logger.warning("Unsupported image format: %s", path.suffix)
        return None


def _estimate_background_and_noise(data: np.ndarray) -> tuple[float, float]:
    """Estimate background level and noise using sigma-clipped statistics."""
    clipped = data.flatten()
    for _ in range(3):
        mean = np.mean(clipped)
        std = np.std(clipped)
        mask = np.abs(clipped - mean) < 3 * std
        clipped = clipped[mask]

    background = float(np.median(clipped))
    noise = float(np.std(clipped))
    return background, noise


def _detect_stars(data: np.ndarray, background: float, noise: float, threshold: float = 5.0) -> list[dict]:
    """Star detection via sep (Source Extractor), scipy fallback."""
    try:
        try:
            import sep
        except ImportError:
            import sep_pjw as sep  # maintained fork, ships Windows wheels

        data_c = np.ascontiguousarray(data, dtype=np.float32)
        # Hot pixels are single-pixel spikes on these uncalibrated frames -
        # a 3x3 median erases them completely while a seeing-limited star
        # (FWHM many px at 0.24"/px) barely notices. Without this the
        # extractor found ~36k "stars" of HFR 0.42 and QA graded the hot
        # pixels instead of the sky (2026-09-04 lesson: a month of visibly
        # trailed subs passed with ecc 0.008).
        from scipy import ndimage as _ndi
        data_c = _ndi.median_filter(data_c, size=3)
        bkg = sep.Background(data_c)
        data_sub = data_c - bkg

        objects = sep.extract(data_sub, threshold, err=bkg.globalrms,
                              minarea=9)
        # real stars only: resolved and extended; then the brightest 400
        # (all aggregate metrics are medians - 400 is plenty, and it keeps
        # the per-object flux_radius loop fast)
        _keep = (objects["npix"] >= 12) & (objects["b"] > 0.7)
        objects = objects[_keep]
        if len(objects) > 400:
            objects = objects[np.argsort(objects["flux"])[::-1][:400]]
        stars = []
        for obj in objects:
            flux_radius, _ = sep.flux_radius(
                data_sub, [obj["x"]], [obj["y"]], [6.0 * obj["a"]], 0.5
            )
            stars.append({
                "x": float(obj["x"]),
                "y": float(obj["y"]),
                "flux": float(obj["flux"]),
                "a": float(obj["a"]),
                "b": float(obj["b"]),
                "theta": float(obj["theta"]),
                "fwhm": float(obj["a"] * 2.355),  # Gaussian approx
                "hfr": float(flux_radius[0]) if len(flux_radius) > 0 else float(obj["a"]),
                # standard form: e = sqrt(1-(b/a)^2). The old 1-b/a
                # understated elongation (a 3:1 streak scored 0.67, a 1.3:1
                # star 0.23) and made the ecc gate nearly unreachable.
                "eccentricity": float(np.sqrt(max(
                    1.0 - (obj["b"] / obj["a"]) ** 2, 0.0)))
                if obj["a"] > 0 else 0,
            })
        return stars

    except ImportError:
        logger.info("sep not available, using simple threshold detection")
        from scipy import ndimage

        # Robust noise via MAD — std is inflated by stars/hot pixels, which
        # made the threshold miss everything on real frames
        sample = data[::4, ::4]
        med = float(np.median(sample))
        mad = float(np.median(np.abs(sample - med))) * 1.4826 or 1.0
        detect_level = med + threshold * mad
        binary = data > detect_level
        labeled, num_features = ndimage.label(binary)
        if num_features == 0:
            return []
        # Vectorized region sizes: the old per-label loop capped at the FIRST
        # 500 labels, which are top-of-frame noise specks — real stars were
        # never reached (the "0 stars on a 97-star frame" bug)
        sizes = ndimage.sum(binary, labeled, np.arange(1, num_features + 1))
        star_labels = np.nonzero(sizes >= 6)[0] + 1
        if len(star_labels) == 0:
            return []
        # Largest 300 regions get centroids (enough for all metrics)
        order = np.argsort(sizes[star_labels - 1])[::-1][:300]
        star_labels = star_labels[order]
        centroids = ndimage.center_of_mass(binary, labeled, star_labels)
        stars = []
        for (y_c, x_c), lbl in zip(centroids, star_labels):
            area = float(sizes[lbl - 1])
            size = float(np.sqrt(area / np.pi))
            stars.append({
                "x": float(x_c), "y": float(y_c),
                "flux": area,
                "fwhm": size * 2.355,
                "hfr": size,
                "eccentricity": 0.0,  # no moments in fallback
            })
        return stars


# --- PS-96 step 0: one-shot-color (Piggy-600) measure -----------------------
# The OSC rig was graded on the RAW RGGB mosaic through the RC16's 3x3 median.
# Its stars are only ~2 to 3 px FWHM at 1.29"/px, so the Bayer pattern (green
# on a checkerboard, R and B on every other row/column) and the median bend
# their second moments. Measuring on a 2x2 superpixel (R+G+G+B summed, so each
# output pixel is one full color cell) removes the pattern; the median is kept
# only when the stars are big enough not to notice it (on the superpixel grid
# a focused Piggy star has an HFR of ~1.6 px, which a 3x3 median visibly
# widens and squares). Coordinates, HFR and
# FWHM go back out in native pixels so sidecars, corner spread and saturation
# checks stay on the native grid.

OSC_MEDIAN_MIN_HFR_PX = 2.5  # SUPERPIXEL px (5 native): below this the 3x3
                             # median is skipped


def is_osc_frame(file_path: str, rig: str = "rc16") -> bool:
    """True for a one-shot-color frame: the piggyback rig, or a FITS header
    that carries BAYERPAT (any rig, so a mislabeled OSC frame still gets the
    color-safe measure)."""
    if rig == "piggyback":
        return True
    if Path(file_path).suffix.lower() not in (".fits", ".fit", ".fts"):
        return False
    try:
        from astropy.io import fits
        return bool(str(fits.getheader(file_path).get("BAYERPAT") or "").strip())
    except Exception:  # noqa: BLE001 - unreadable header = not OSC
        return False


def superpixel(data: np.ndarray) -> np.ndarray:
    """2x2 sum of the Bayer mosaic (odd last row/column dropped)."""
    h, w = (data.shape[0] // 2) * 2, (data.shape[1] // 2) * 2
    d = np.asarray(data[:h, :w], dtype=np.float32)
    return d[0::2, 0::2] + d[0::2, 1::2] + d[1::2, 0::2] + d[1::2, 1::2]


def _sep_stars_superpixel(sp: np.ndarray, median: bool,
                          threshold: float = 5.0) -> list[dict]:
    """sep on a superpixel image; returns stars in NATIVE pixels. The
    real-star cuts are the native ones scaled by the 2x bin (area / 4,
    lengths / 2)."""
    try:
        import sep
    except ImportError:
        import sep_pjw as sep
    d = np.ascontiguousarray(sp, dtype=np.float32)
    if median:
        from scipy import ndimage as _ndi
        d = _ndi.median_filter(d, size=3)
    bkg = sep.Background(d)
    sub = d - bkg
    objects = sep.extract(sub, threshold, err=bkg.globalrms, minarea=3)
    objects = objects[(objects["npix"] >= 3) & (objects["b"] > 0.35)]
    if len(objects) > 400:
        objects = objects[np.argsort(objects["flux"])[::-1][:400]]
    stars = []
    for obj in objects:
        fr, _ = sep.flux_radius(sub, [obj["x"]], [obj["y"]],
                                [6.0 * obj["a"]], 0.5)
        a, b = float(obj["a"]), float(obj["b"])
        stars.append({
            # superpixel i covers native 2i and 2i+1: its center is 2i+0.5
            "x": 2.0 * float(obj["x"]) + 0.5,
            "y": 2.0 * float(obj["y"]) + 0.5,
            "flux": float(obj["flux"]),
            "a": 2.0 * a, "b": 2.0 * b,
            "theta": float(obj["theta"]),
            "fwhm": 2.0 * a * 2.355,
            "hfr": 2.0 * (float(fr[0]) if len(fr) > 0 else a),
            "eccentricity": float(np.sqrt(max(1.0 - (b / a) ** 2, 0.0)))
            if a > 0 else 0.0,
        })
    return stars


def _detect_stars_osc(data: np.ndarray) -> list[dict]:
    """PS-96: superpixel star measure for one-shot-color frames. First pass
    with the 3x3 median (robust to hot pixels); if the stars are small
    (median HFR under OSC_MEDIAN_MIN_HFR_PX superpixels) measure again without
    it, since the median would widen and square them. Hot pixels stay out
    either way: a single hot photosite is a 1-superpixel spike, below the
    npix cut."""
    try:
        sp = superpixel(data)
        stars = _sep_stars_superpixel(sp, median=True)
        hfrs = [s["hfr"] for s in stars if s["hfr"] > 0]
        if hfrs and float(np.median(hfrs)) / 2.0 < OSC_MEDIAN_MIN_HFR_PX:
            raw = _sep_stars_superpixel(sp, median=False)
            if raw:
                stars = raw
        return stars
    except ImportError:
        return _detect_stars(data, *_estimate_background_and_noise(data[::4, ::4]))


def _corner_spread(stars: list[dict], shape: tuple[int, int],
                   median_fwhm: float) -> Optional[float]:
    """Corner FWHM spread relative to the frame median.

    The RC16's collimation and sensor tilt show up as asymmetric corner
    degradation long before the center goes soft. Computed passively on
    every sub — no sky time cost.
    """
    if not stars or median_fwhm <= 0:
        return None
    h, w = shape
    corner_medians = []
    for (x0, x1, y0, y1) in [(0, w / 3, 0, h / 3), (2 * w / 3, w, 0, h / 3),
                             (0, w / 3, 2 * h / 3, h), (2 * w / 3, w, 2 * h / 3, h)]:
        vals = [s["fwhm"] for s in stars
                if x0 <= s["x"] < x1 and y0 <= s["y"] < y1 and s["fwhm"] > 0]
        if len(vals) >= 3:
            corner_medians.append(float(np.median(vals)))
    if len(corner_medians) < 3:
        return None
    return float((max(corner_medians) - min(corner_medians)) / median_fwhm)


SATURATION_ADU = 65000.0


def _exposure_metrics(data: np.ndarray, stars: list, noise: float,
                      config) -> dict:
    """Exposure scoring: is the sub sky-limited without clipping?

    swamp_factor = (frame noise / read-noise floor)^2 — total background
    variance over read-noise variance. >=10: fully sky-limited; 3-10: fine;
    <3: read noise still dominates (underexposed — longer subs pay off).
    clipped_pct / sat_star_pct catch the other end: blown pixels and
    saturated star cores (RGB star color dies when cores clip).
    """
    sample = data[::4, ::4]
    clipped_pct = float((sample >= SATURATION_ADU).mean() * 100.0)
    sat_star_pct = None
    if stars:
        h, w = data.shape
        sat = 0
        for s in stars:
            x, y = int(round(s["x"])), int(round(s["y"]))
            if 1 <= x < w - 1 and 1 <= y < h - 1 and \
                    float(data[y - 1:y + 2, x - 1:x + 2].max()) >= SATURATION_ADU:
                sat += 1
        sat_star_pct = round(sat / len(stars) * 100.0, 1)
    rn = max(float(getattr(config, "camera_read_noise_adu", 8.0)), 0.1)
    swamp = round((noise / rn) ** 2, 1)
    if sat_star_pct is not None and sat_star_pct > 5.0:
        flag = "sat-stars"
    elif clipped_pct > 0.05:
        flag = "clipped"
    elif swamp < 3.0:
        flag = "under"
    else:
        flag = "ok"
    return {"clipped_pct": round(clipped_pct, 3), "sat_star_pct": sat_star_pct,
            "swamp_factor": swamp, "exposure_flag": flag}


def image_metrics(quality: ImageQualityMetrics) -> dict:
    """The qa_rules metrics this grader measures (PS-21). FWHM here is a
    real per-star estimate, so it is judged; the backfill grader has none."""
    from photonscript.shared.qa_rules import record_metrics
    return record_metrics(
        hfr=quality.hfr_pixels, fwhm_arcsec=quality.fwhm_arcsec,
        ecc=quality.eccentricity, ecc_bin=quality.ecc_bin,
        stars=quality.star_count,
        background=quality.background_adu, exposure=quality.exposure_flag,
        clipped_pct=quality.clipped_pct, sat_stars_pct=quality.sat_star_pct,
        swamp=quality.swamp_factor)


def _binned_metrics(data: np.ndarray, config, rig: str) -> dict:
    """PS-94: eccentricity and HFR on a 2x2-binned copy (0.48"/px on the
    RC16, the scale the _bin2 masters integrate at), recorded next to the
    native measure. RC16 only, and only while qa_ecc_binned is on. Which
    scale gates is qa_rules' business (qa_ecc_scale). Never costs a grade."""
    if rig != "rc16" or not bool(getattr(config, "qa_ecc_binned", True)):
        return {}
    import time
    t0 = time.monotonic()
    try:
        from photonscript.shared import star_shape
        res = star_shape.measure(star_shape.bin2x2_mean(data), binned=True)
    except Exception as e:  # noqa: BLE001
        logger.debug("binned measure skipped: %s", e)
        return {}
    if res is None:
        return {}
    logger.debug("binned measure: %d stars in %.2fs", res["n"],
                 time.monotonic() - t0)
    return {"ecc_bin": round(res["ecc"], 3) if res["ecc"] is not None else None,
            "hfr_bin_px": round(res["hfr_px"], 2)
            if res["hfr_px"] is not None else None,
            "stars_bin": res["n"]}


def validate_image(
    file_path: str,
    config: PhotonScriptConfig,
    pixel_scale: Optional[float] = None,  # arcsec/pixel; defaults to config value
    rig: str = "rc16",
) -> ImageQualityMetrics:
    """Measure an image and grade what the frame alone can show.

    Pass/fail comes from shared.qa_rules.evaluate (PS-21), the same rules the
    backfill grader uses; the telescope agent re-evaluates with the sensor
    temperature, guiding and safety context before it records the sub.
    `config` is the rig's config view (rigs.rig_config)."""
    if pixel_scale is None:
        pixel_scale = getattr(config, "pixel_scale_arcsec", 1.0)

    data = _load_image_data(file_path)
    if data is None:
        return ImageQualityMetrics(
            passed_qa=False,
            rejection_reason="Could not load image data",
        )

    # Subsample for background stats: identical result, ~16x less memory
    background, noise = _estimate_background_and_noise(data[::4, ::4])
    snr = background / noise if noise > 0 else 0

    osc = is_osc_frame(file_path, rig)
    stars = (_detect_stars_osc(data) if osc
             else _detect_stars(data, background, noise))
    exposure = _exposure_metrics(data, stars, noise, config)

    median_fwhm_px = median_hfr_px = median_ecc = None
    fwhm_arcsec = corner_spread = None
    if stars:
        fwhm_values = [s["fwhm"] for s in stars if s["fwhm"] > 0]
        hfr_values = [s["hfr"] for s in stars if s["hfr"] > 0]
        ecc_values = [s.get("eccentricity", 0) for s in stars]
        median_fwhm_px = float(np.median(fwhm_values)) if fwhm_values else 0
        median_hfr_px = float(np.median(hfr_values)) if hfr_values else 0
        median_ecc = float(np.median(ecc_values)) if ecc_values else 0
        fwhm_arcsec = median_fwhm_px * pixel_scale
        corner_spread = _corner_spread(stars, data.shape, median_fwhm_px)

    # PS-80: the stars behind the medians, for the review overlay. Kept on
    # the metrics object (excluded from bus payloads); the agent writes it.
    star_table = None
    try:
        from photonscript.shared.star_table import build
        n_max = int(getattr(config, "qa_star_sidecar_max", 500) or 0)
        if stars and n_max > 0:
            star_table = build(
                [s["x"] for s in stars], [s["y"] for s in stars],
                [s["hfr"] for s in stars],
                [s.get("eccentricity", 0.0) for s in stars],
                theta=[s.get("theta") for s in stars],
                flux=[s.get("flux") for s in stars],
                w=data.shape[1], h=data.shape[0], limit=n_max,
                grader="live-sep-superpixel" if osc else "live-sep",
                rig=rig, ecc_def="sqrt(1-(b/a)^2)")
    except Exception as e:  # noqa: BLE001
        logger.debug("star table skipped: %s", e)

    binned = _binned_metrics(data, config, rig)

    quality = ImageQualityMetrics(
        fwhm_arcsec=round(fwhm_arcsec, 2) if fwhm_arcsec is not None else None,
        hfr_pixels=round(median_hfr_px, 2) if median_hfr_px is not None else None,
        star_count=len(stars),
        eccentricity=round(median_ecc, 3) if median_ecc is not None else None,
        background_adu=round(background, 1),
        noise_adu=round(noise, 2),
        snr=round(snr, 1),
        corner_spread=round(corner_spread, 3) if corner_spread is not None else None,
        star_table=star_table,
        **binned,
        **exposure,
    )
    from photonscript.shared.qa_rules import context, evaluate
    card = evaluate(image_metrics(quality), context(config, rig))
    quality.passed_qa = card.passed
    quality.rejection_reason = card.reason
    return quality
