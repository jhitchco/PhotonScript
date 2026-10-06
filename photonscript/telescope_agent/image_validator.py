"""Image validation: quality assessment of captured sub-frames.

Analyzes FITS files for star FWHM, HFR, eccentricity, and tracking quality
to decide whether a frame should be kept or rejected. PS-83: the numbers
come from shared.star_measure, the same measure the backfill grader uses.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

from photonscript.shared import star_measure as _sm
from photonscript.shared.models import ImageQualityMetrics
from photonscript.shared.config import PhotonScriptConfig

logger = logging.getLogger(__name__)


def _load_image_data(file_path: str) -> Optional[np.ndarray]:
    """Load image data from FITS or TIFF file."""
    path = Path(file_path)

    if path.suffix.lower() in (".fits", ".fit", ".fts"):
        try:
            # PS-83: the same loader as the backfill grader
            return _sm.load_native(path)
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


# PS-83: the measurement itself lives in shared.star_measure, which the
# backfill grader (scheduler.runs._fast_grade) calls too. The helpers below
# keep their old names and shapes for callers and tests.

SATURATION_ADU = _sm.SATURATION_ADU
OSC_MEDIAN_MIN_HFR_PX = _sm.OSC_MEDIAN_MIN_HFR_PX
superpixel = _sm.superpixel


def _estimate_background_and_noise(data: np.ndarray) -> tuple[float, float]:
    """Background level and noise from sigma-clipped statistics."""
    return _sm.background_noise(data)


def _as_dicts(st: dict) -> list[dict]:
    """star_measure per-star arrays -> the old list of star dicts."""
    n = len(st["x"])
    has_shape = len(st["a"]) == n and n > 0
    out = []
    for i in range(n):
        out.append({
            "x": float(st["x"][i]), "y": float(st["y"][i]),
            "flux": float(st["flux"][i]),
            "a": float(st["a"][i]) if has_shape else None,
            "b": float(st["b"][i]) if has_shape else None,
            "theta": float(st["theta"][i]) if has_shape else None,
            "fwhm": float(st["fwhm"][i]) if has_shape else 0.0,
            "hfr": float(st["hfr"][i]) if has_shape else 0.0,
            "eccentricity": float(st["ecc"][i]) if has_shape else 0.0,
        })
    return out


def _detect_stars(data: np.ndarray, background: float = 0.0,
                  noise: float = 1.0, threshold: float = 5.0) -> list[dict]:
    """Mono star list (shared.star_measure.detect_mono; the threshold-label
    fallback without sep gives positions only)."""
    if _sm._sep() is None:
        return _as_dicts(_sm.detect_fallback(data, threshold))
    return _as_dicts(_sm.detect_mono(data))


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
        return _sm.is_osc(rig, fits.getheader(file_path))
    except Exception:  # noqa: BLE001 - unreadable header = not OSC
        return False


def _fits_header(file_path: str):
    """The FITS header (READOUTM picks the read noise, PS-117), or None
    for a non-FITS or unreadable file (the config's HCG read noise)."""
    if Path(file_path).suffix.lower() not in (".fits", ".fit", ".fts"):
        return None
    try:
        from astropy.io import fits
        return fits.getheader(file_path)
    except Exception:  # noqa: BLE001
        return None


def _detect_stars_osc(data: np.ndarray) -> list[dict]:
    """PS-96: superpixel star measure for one-shot-color frames, native px."""
    if _sm._sep() is None:
        return _detect_stars(data)
    return _as_dicts(_sm.detect_osc(_sm.superpixel(data)))


def _exposure_metrics(data: np.ndarray, stars: list, noise: float,
                      config) -> dict:
    """Exposure scoring (shared.star_measure.exposure) in the old key names."""
    ex = _sm.exposure(data, [s["x"] for s in stars],
                      [s["y"] for s in stars], noise, config)
    return {"clipped_pct": ex["clipped_pct"],
            "sat_star_pct": ex["sat_stars_pct"],
            "swamp_factor": ex["swamp"], "exposure_flag": ex["exposure"]}


def _corner_spread(stars: list[dict], shape: tuple[int, int],
                   median_fwhm: float) -> Optional[float]:
    """Corner FWHM spread relative to the frame median."""
    return _sm.corner_spread([s["x"] for s in stars], [s["y"] for s in stars],
                             [s["fwhm"] for s in stars], shape, median_fwhm)


def image_metrics(quality: ImageQualityMetrics) -> dict:
    """The qa_rules metrics this grader measures (PS-21). FWHM is a real
    per-star estimate (shared.star_measure; the backfill grader records the
    same one since PS-83), so it is judged."""
    from photonscript.shared.qa_rules import record_metrics
    return record_metrics(
        hfr=quality.hfr_pixels, fwhm_arcsec=quality.fwhm_arcsec,
        ecc=quality.eccentricity, ecc_bin=quality.ecc_bin,
        stars=quality.star_count,
        background=quality.background_adu, exposure=quality.exposure_flag,
        clipped_pct=quality.clipped_pct, sat_stars_pct=quality.sat_star_pct,
        swamp=quality.swamp_factor, sat_px_pct=quality.sat_px_pct,
        zero_px_pct=quality.zero_px_pct, max_adu=quality.max_adu)


def _pixel_stats(data: np.ndarray, file_path: str, config) -> dict:
    """PS-108: saturated / zero pixel counts, max ADU and background median
    + MAD on the full-resolution frame (shared.pixel_stats, same function
    the backfill grader uses). Never costs a grade."""
    try:
        from photonscript.shared.pixel_stats import frame_stats, saturation_level
        hdr = None
        if Path(file_path).suffix.lower() in (".fits", ".fit", ".fts"):
            try:
                from astropy.io import fits
                hdr = fits.getheader(file_path)
            except Exception:  # noqa: BLE001
                hdr = None
        st = frame_stats(data, saturation_level(hdr, config))
        st.pop("n_px", None)
        return st
    except Exception as e:  # noqa: BLE001
        logger.debug("pixel stats skipped: %s", e)
        return {}


def validate_image(
    file_path: str,
    config: PhotonScriptConfig,
    pixel_scale: Optional[float] = None,  # arcsec/pixel; defaults to config value
    rig: str = "rc16",
) -> ImageQualityMetrics:
    """Measure an image and grade what the frame alone can show.

    PS-83: the numbers come from shared.star_measure.measure_frame, the same
    function the backfill grader calls on the same frame. Pass/fail comes
    from shared.qa_rules.evaluate (PS-21), the same rules the backfill grader
    uses; the telescope agent re-evaluates with the sensor temperature,
    guiding and safety context before it records the sub.
    `config` is the rig's config view (rigs.rig_config)."""
    if pixel_scale is None:
        pixel_scale = getattr(config, "pixel_scale_arcsec", 1.0)

    data = _load_image_data(file_path)
    if data is None:
        return ImageQualityMetrics(
            passed_qa=False,
            rejection_reason="Could not load image data",
        )

    m = _sm.measure_frame(data, config, rig, pixel_scale=pixel_scale,
                          osc=is_osc_frame(file_path, rig), grader="live-sep",
                          header=_fits_header(file_path))
    pixels = _pixel_stats(data, file_path, config)

    quality = ImageQualityMetrics(
        fwhm_arcsec=m["fwhm_arcsec"],
        hfr_pixels=m["hfr"],
        star_count=m["stars"],
        eccentricity=m["ecc"],
        background_adu=m["background"],
        noise_adu=m["noise"],
        snr=m["snr"],
        corner_spread=m["corner_spread"],
        star_table=m["star_table"],
        ecc_bin=m["ecc_bin"], hfr_bin_px=m["hfr_bin"], stars_bin=m["stars_bin"],
        clipped_pct=m["clipped_pct"], sat_star_pct=m["sat_stars_pct"],
        swamp_factor=m["swamp"], exposure_flag=m["exposure"],
        sky_adu=m.get("sky_adu"), sky_e_s=m.get("sky_e_s"),
        sky_e_s_ch=m.get("sky_e_s_ch"),
        rn_penalty_pct=m.get("rn_penalty_pct"),
        **pixels,
    )
    from photonscript.shared.qa_rules import context, evaluate
    card = evaluate(image_metrics(quality), context(config, rig))
    quality.passed_qa = card.passed
    quality.rejection_reason = card.reason
    return quality
