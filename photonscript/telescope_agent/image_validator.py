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


def validate_image(
    file_path: str,
    config: PhotonScriptConfig,
    pixel_scale: Optional[float] = None,  # arcsec/pixel; defaults to config value
) -> ImageQualityMetrics:
    """Analyze an image and return quality metrics."""
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

    stars = _detect_stars(data, background, noise)
    exposure = _exposure_metrics(data, stars, noise, config)
    if len(stars) < 5:
        return ImageQualityMetrics(
            star_count=len(stars),
            background_adu=background,
            noise_adu=noise,
            snr=snr,
            passed_qa=False,
            rejection_reason=f"Only {len(stars)} stars detected (minimum 5)",
            **exposure,
        )

    # Compute aggregate metrics
    fwhm_values = [s["fwhm"] for s in stars if s["fwhm"] > 0]
    hfr_values = [s["hfr"] for s in stars if s["hfr"] > 0]
    ecc_values = [s.get("eccentricity", 0) for s in stars]

    median_fwhm_px = float(np.median(fwhm_values)) if fwhm_values else 0
    median_hfr_px = float(np.median(hfr_values)) if hfr_values else 0
    median_ecc = float(np.median(ecc_values)) if ecc_values else 0
    fwhm_arcsec = median_fwhm_px * pixel_scale
    corner_spread = _corner_spread(stars, data.shape, median_fwhm_px)

    # Quality assessment
    passed = True
    reasons = []

    if fwhm_arcsec > config.quality_fwhm_max:
        passed = False
        reasons.append(f"FWHM {fwhm_arcsec:.1f}\" > {config.quality_fwhm_max}\"")

    if median_ecc > config.quality_eccentricity_max:
        passed = False
        reasons.append(f"Eccentricity {median_ecc:.2f} > {config.quality_eccentricity_max}"
                       " (trailing/drift)")

    star_max = int(getattr(config, "quality_star_max", 5000))
    if len(stars) > star_max:
        passed = False
        reasons.append(f"{len(stars)} stars > {star_max} "
                       "(defocus/false detections)")

    hfr_abs_max = float(getattr(config, "quality_hfr_abs_max", 8.0))
    if median_hfr_px > hfr_abs_max:
        passed = False
        reasons.append(f"HFR {median_hfr_px:.1f}px > {hfr_abs_max:g}px "
                       "(out of focus)")

    return ImageQualityMetrics(
        fwhm_arcsec=round(fwhm_arcsec, 2),
        hfr_pixels=round(median_hfr_px, 2),
        star_count=len(stars),
        eccentricity=round(median_ecc, 3),
        background_adu=round(background, 1),
        noise_adu=round(noise, 2),
        snr=round(snr, 1),
        corner_spread=round(corner_spread, 3) if corner_spread is not None else None,
        passed_qa=passed,
        rejection_reason="; ".join(reasons),
        **exposure,
    )
