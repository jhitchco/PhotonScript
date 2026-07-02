"""Tests for the image processor agent."""

from datetime import datetime
from pathlib import Path

from photonscript.shared.config import PhotonScriptConfig
from photonscript.image_processor.agent import ImageProcessor


class TestSirilScriptGeneration:
    def test_generates_valid_script(self, tmp_path):
        config = PhotonScriptConfig(stacking_output_dir=str(tmp_path))
        processor = ImageProcessor(config)

        images = [
            "/data/M42/Ha/M42_Ha_300s_001.fits",
            "/data/M42/Ha/M42_Ha_300s_002.fits",
            "/data/M42/Ha/M42_Ha_300s_003.fits",
        ]
        output_dir = tmp_path / "M42" / "Ha"
        output_dir.mkdir(parents=True)

        script = processor._generate_siril_script(images, output_dir, "M42", "Ha")

        assert "requires 1.2.0" in script
        assert "M42" in script
        assert "Ha" in script
        assert "register" in script
        assert "stack" in script
        assert "rej 3 3" in script  # sigma clipping


class TestProcessorSummary:
    def test_empty_summary(self, tmp_path):
        config = PhotonScriptConfig(stacking_output_dir=str(tmp_path))
        processor = ImageProcessor(config)
        summary = processor.get_summary()
        assert summary == {}
