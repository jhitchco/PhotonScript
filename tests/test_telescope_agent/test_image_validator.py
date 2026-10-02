"""Tests for image quality validation."""

import numpy as np
import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ImageQualityMetrics
from photonscript.telescope_agent.image_validator import (
    _estimate_background_and_noise,
    validate_image,
)


def _write_star_field(path, seed=0):
    """A clean frame of round, well-separated Gaussian stars: tight HFR, low
    eccentricity — the only thing that can flag it is the FWHM gate."""
    from astropy.io import fits
    rng = np.random.default_rng(seed)
    data = rng.normal(500, 8, (220, 220)).astype(np.float32)
    yy, xx = np.mgrid[0:9, 0:9]
    g = np.exp(-((xx - 4) ** 2 + (yy - 4) ** 2) / (2 * 1.6 ** 2))
    for cy in range(30, 200, 40):
        for cx in range(30, 200, 40):
            data[cy - 4:cy + 5, cx - 4:cx + 5] += (8000.0 * g).astype(np.float32)
    fits.PrimaryHDU(data).writeto(path, overwrite=True)
    return str(path)


class TestBackgroundEstimation:
    def test_uniform_background(self):
        data = np.random.normal(1000, 10, (100, 100))
        bg, noise = _estimate_background_and_noise(data)
        assert abs(bg - 1000) < 50
        assert abs(noise - 10) < 5

    def test_background_with_stars(self):
        # Background + a few bright "stars"
        data = np.random.normal(500, 8, (200, 200))
        # Add synthetic stars
        for _ in range(20):
            x, y = np.random.randint(10, 190, 2)
            data[y-2:y+2, x-2:x+2] += 5000
        bg, noise = _estimate_background_and_noise(data)
        # Sigma clipping should reject stars
        assert abs(bg - 500) < 50


class TestExposureScoring:
    def _cfg(self):
        return PhotonScriptConfig(_env_file=None, camera_read_noise_adu=8.0)

    def test_underexposed_frame(self):
        from photonscript.telescope_agent.image_validator import _exposure_metrics
        # noise barely above read noise floor -> read-noise dominated
        data = np.random.normal(300, 9, (200, 200)).astype(np.float32)
        m = _exposure_metrics(data, [], 9.0, self._cfg())
        assert m["exposure_flag"] == "under"
        assert m["swamp_factor"] < 3
        assert m["clipped_pct"] == 0.0

    def test_sky_limited_frame(self):
        from photonscript.telescope_agent.image_validator import _exposure_metrics
        data = np.random.normal(2000, 40, (200, 200)).astype(np.float32)
        m = _exposure_metrics(data, [], 40.0, self._cfg())
        assert m["exposure_flag"] == "ok"
        assert m["swamp_factor"] >= 10

    def test_saturated_stars(self):
        from photonscript.telescope_agent.image_validator import _exposure_metrics
        data = np.random.normal(2000, 40, (200, 200)).astype(np.float32)
        stars = []
        for i, x in enumerate(range(20, 180, 16)):
            if i % 2 == 0:  # half the stars saturated
                data[x - 1:x + 2, x - 1:x + 2] = 65535.0
            stars.append({"x": float(x), "y": float(x)})
        m = _exposure_metrics(data, stars, 40.0, self._cfg())
        assert m["sat_star_pct"] >= 40
        assert m["exposure_flag"] == "sat-stars"


class TestFwhmSoftGate:
    """FWHM as a hard reject (RC16) vs advisory score-factor (OSC piggyback).

    quality_fwhm_max is forced tiny so the detected FWHM always exceeds it,
    isolating the soft-gate branch from star-detection specifics.
    """

    def _cfg(self, soft):
        return PhotonScriptConfig(
            _env_file=None, camera_read_noise_adu=8.0,
            quality_fwhm_max=0.001, quality_fwhm_soft=soft)

    def test_hard_gate_rejects_on_fwhm(self, tmp_path):
        path = _write_star_field(tmp_path / "hard.fits")
        m = validate_image(path, self._cfg(soft=False), pixel_scale=1.0)
        assert m.passed_qa is False
        assert "FWHM" in m.rejection_reason

    def test_soft_gate_does_not_reject_on_fwhm(self, tmp_path):
        path = _write_star_field(tmp_path / "soft.fits")
        m = validate_image(path, self._cfg(soft=True), pixel_scale=1.0)
        # tight round stars: HFR/ecc/star-count gates all pass, and FWHM is now
        # advisory, so the sub is kept and FWHM never appears as a rejection.
        assert m.passed_qa is True
        assert "FWHM" not in (m.rejection_reason or "")


# --- PS-96 step 0: superpixel measure for one-shot-color frames ------------

def _rggb_star_field(sigma=1.0, seed=1, shape=(400, 400), bayer=True):
    """Round Gaussian stars (native sigma px) seen through an RGGB mosaic:
    R and B photosites respond less than G, as on the AP26CC."""
    rng = np.random.default_rng(seed)
    h, w = shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    sky = np.zeros(shape, np.float32)
    pos = []
    for cy in range(25, h - 20, 35):
        for cx in range(25, w - 20, 35):
            x0, y0 = cx + rng.uniform(-0.5, 0.5), cy + rng.uniform(-0.5, 0.5)
            pos.append((x0, y0))
            sky += 6000.0 * np.exp(-((xx - x0) ** 2 + (yy - y0) ** 2)
                                   / (2 * sigma ** 2))
    if bayer:
        resp = np.ones(shape, np.float32)
        resp[0::2, 0::2] = 0.45  # R
        resp[1::2, 1::2] = 0.30  # B
        sky *= resp
    data = rng.normal(400, 6, shape).astype(np.float32) + sky
    return data, pos


class TestOscSuperpixel:
    def test_superpixel_sums_cells(self):
        from photonscript.telescope_agent.image_validator import superpixel
        d = np.arange(20, dtype=np.float32).reshape(4, 5)
        sp = superpixel(d)
        assert sp.shape == (2, 2)
        assert sp[0, 0] == 0 + 1 + 5 + 6
        assert sp[1, 1] == 12 + 13 + 17 + 18

    def test_round_osc_stars_read_round(self):
        from photonscript.telescope_agent.image_validator import _detect_stars_osc
        data, pos = _rggb_star_field(sigma=1.0)
        stars = _detect_stars_osc(data)
        assert len(stars) >= 0.8 * len(pos)
        assert float(np.median([s["eccentricity"] for s in stars])) < 0.3
        # coordinates come back on the native grid
        px = np.array(pos)
        for s in stars[:20]:
            d = np.hypot(px[:, 0] - s["x"], px[:, 1] - s["y"]).min()
            assert d < 0.75

    def test_validate_image_uses_superpixel_for_piggyback(self, tmp_path):
        from astropy.io import fits
        data, _ = _rggb_star_field(sigma=1.0)
        p = tmp_path / "osc.fits"
        fits.PrimaryHDU(data).writeto(p)
        cfg = PhotonScriptConfig(_env_file=None, camera_read_noise_adu=8.0)
        m = validate_image(str(p), cfg, pixel_scale=1.29, rig="piggyback")
        assert m.star_table["grader"] == "live-sep-superpixel"
        assert m.eccentricity < 0.3
        assert m.star_table["w"] == 400

    def test_bayerpat_header_marks_osc(self, tmp_path):
        from astropy.io import fits
        from photonscript.telescope_agent.image_validator import is_osc_frame
        p1, p2 = tmp_path / "osc.fits", tmp_path / "mono.fits"
        h = fits.Header()
        h["BAYERPAT"] = "RGGB"
        fits.PrimaryHDU(np.zeros((4, 4), np.float32), header=h).writeto(p1)
        fits.PrimaryHDU(np.zeros((4, 4), np.float32)).writeto(p2)
        assert is_osc_frame(str(p1), "rc16") is True
        assert is_osc_frame(str(p2), "rc16") is False
        assert is_osc_frame(str(p2), "piggyback") is True

    def test_mono_path_unchanged(self, tmp_path):
        path = _write_star_field(tmp_path / "mono.fits")
        cfg = PhotonScriptConfig(_env_file=None, camera_read_noise_adu=8.0)
        m = validate_image(path, cfg, pixel_scale=1.0, rig="rc16")
        assert m.star_table["grader"] == "live-sep"


def test_ps96_check_script_direction_stats():
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / "scripts" / "ps96_osc_measure_check.py"
    spec = importlib.util.spec_from_file_location("ps96_check", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    trail = mod.direction_stats([np.radians(30.0)] * 50)
    assert trail["dir_R"] == 1.0 and abs(trail["dir_deg"] - 30.0) < 0.1
    rng = np.random.default_rng(0)
    rand = mod.direction_stats(list(rng.uniform(-np.pi / 2, np.pi / 2, 2000)))
    assert rand["dir_R"] < 0.1 and 25 < rand["grid_pct"] < 42
