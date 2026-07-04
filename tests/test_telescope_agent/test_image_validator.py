"""Tests for image quality validation."""

import numpy as np
import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ImageQualityMetrics
from photonscript.telescope_agent.image_validator import (
    _estimate_background_and_noise,
)


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
