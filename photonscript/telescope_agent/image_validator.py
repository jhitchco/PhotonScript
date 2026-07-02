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
                return hdul[0].data.astype(np.float64)
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
        bkg = sep.Background(data_c)
        data_sub = data_c - bkg

        objects = sep.extract(data_sub, threshold, err=bkg.globalrms)
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
                "eccentricity": float(1 - obj["b"] / obj["a"]) if obj["a"] > 0 else 0,
            })
        return stars

    except ImportError:
        logger.info("sep not available, using simple threshold detection")
        detect_level = background + threshold * noise
        binary = data > detect_level
        from scipy import ndimage
        labeled, num_features = ndimage.label(binary)

        stars = []
        for i in range(1, min(num_features + 1, 500)):  # cap at 500 stars
            region = np.where(labeled == i)
            if len(region[0]) < 4:  # too small
                continue
            y_center = float(np.mean(region[0]))
            x_center = float(np.mean(region[1]))
            flux = float(np.sum(data[region] - background))
            size = float(np.sqrt(len(region[0]) / np.pi))
            stars.append({
                "x": x_center,
                "y": y_center,
                "flux": flux,
                "fwhm": size * 2.355,
                "hfr": size,
                "eccentricity": 0.0,  # can't measure without moments
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

    background, noise = _estimate_background_and_noise(data)
    snr = background / noise if noise > 0 else 0

    stars = _detect_stars(data, background, noise)
    if len(stars) < 5:
        return ImageQualityMetrics(
            star_count=len(stars),
            background_adu=background,
            noise_adu=noise,
            snr=snr,
            passed_qa=False,
            rejection_reason=f"Only {len(stars)} stars detected (minimum 5)",
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
        reasons.append(f"Eccentricity {median_ecc:.2f} > {config.quality_eccentricity_max}")

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
    )
