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


def _write_rc16_field(path, seed=0):
    """FWHM 8 px stars (the RC16 at 0.24"/px in 2" seeing), stretched 5:4."""
    from astropy.io import fits
    rng = np.random.default_rng(seed)
    data = rng.normal(600, 9, (300, 300)).astype(np.float32)
    yy, xx = np.mgrid[-15:16, -15:16]
    g = np.exp(-0.5 * (xx ** 2 / 3.8 ** 2 + yy ** 2 / 3.04 ** 2))
    for cy in range(30, 280, 40):
        for cx in range(30, 280, 40):
            data[cy - 15:cy + 16, cx - 15:cx + 16] += (6000.0 * g).astype(
                np.float32)
    fits.PrimaryHDU(data.astype(np.uint16)).writeto(path, overwrite=True)
    return str(path)


class TestBinnedMeasurePS94:
    """PS-94: RC16 subs also get ecc / HFR on a 2x2-binned copy; the
    Piggy-600 does not; qa_ecc_binned=False turns it off."""

    def _cfg(self, **kw):
        return PhotonScriptConfig(_env_file=None, camera_read_noise_adu=8.0,
                                  **kw)

    def test_rc16_gets_ecc_bin(self, tmp_path):
        pytest.importorskip("sep")
        m = validate_image(_write_rc16_field(tmp_path / "a.fits"), self._cfg(),
                           pixel_scale=0.24, rig="rc16")
        assert m.ecc_bin is not None and m.stars_bin and m.stars_bin >= 30
        assert m.hfr_bin_px == pytest.approx(m.hfr_pixels, rel=0.2)
        # both sqrt form, near the true 0.6 (b/a 0.8)
        assert m.eccentricity == pytest.approx(0.6, abs=0.04)
        assert m.ecc_bin == pytest.approx(0.6, abs=0.08)

    def test_piggyback_and_switch_off_have_none(self, tmp_path):
        p = _write_rc16_field(tmp_path / "b.fits")
        m = validate_image(p, self._cfg(), pixel_scale=1.29, rig="piggyback")
        assert m.ecc_bin is None and m.hfr_bin_px is None and m.stars_bin is None
        m = validate_image(p, self._cfg(qa_ecc_binned=False), pixel_scale=0.24,
                           rig="rc16")
        assert m.ecc_bin is None

    def test_ecc_bin_is_info_only_by_default(self, tmp_path):
        pytest.importorskip("sep")
        from photonscript.shared import qa_rules
        from photonscript.telescope_agent.image_validator import image_metrics
        m = validate_image(_write_rc16_field(tmp_path / "c.fits"), self._cfg(),
                           pixel_scale=0.24, rig="rc16")
        card = qa_rules.evaluate(image_metrics(m),
                                 qa_rules.context(self._cfg(), "rc16"))
        rows = {c.id: c for c in card.checks}
        assert rows["ecc_bin"].status == "skip" and rows["ecc_bin"].value is not None
        assert rows["ecc"].status in ("pass", "warn", "fail")
