"""Tests for NINA sequence XML generation."""

import xml.etree.ElementTree as ET

from photonscript.shared.models import (
    ExposurePlan, FilterType, NinaSequenceFile, NinaSequenceTarget,
)
from photonscript.scheduler.nina_sequence import generate_nina_xml, build_sequence_for_night


class TestNinaSequenceGeneration:
    def test_generates_valid_xml(self):
        target = NinaSequenceTarget(
            name="M 42",
            ra_hours=5.588,
            dec_degrees=-5.39,
            exposures=[
                ExposurePlan(filter_type=FilterType.HA, exposure_seconds=300, count=20),
                ExposurePlan(filter_type=FilterType.OIII, exposure_seconds=300, count=15),
            ],
        )
        seq = build_sequence_for_night("TestSeq", [target])
        xml = generate_nina_xml(seq)

        # Should be valid XML
        root = ET.fromstring(xml)
        assert root.tag == "AdvancedSequence"

    def test_contains_target_info(self):
        target = NinaSequenceTarget(
            name="M 31",
            ra_hours=0.712,
            dec_degrees=41.27,
            exposures=[
                ExposurePlan(filter_type=FilterType.LUMINANCE, exposure_seconds=180, count=30),
            ],
        )
        seq = build_sequence_for_night("AndromedaSeq", [target])
        xml = generate_nina_xml(seq)

        assert "M 31" in xml
        assert "AndromedaSeq" in xml

    def test_skips_completed_exposures(self):
        target = NinaSequenceTarget(
            name="M 42",
            ra_hours=5.588,
            dec_degrees=-5.39,
            exposures=[
                ExposurePlan(
                    filter_type=FilterType.HA,
                    exposure_seconds=300,
                    count=20,
                    acquired=20,  # fully acquired
                ),
                ExposurePlan(
                    filter_type=FilterType.OIII,
                    exposure_seconds=300,
                    count=15,
                    acquired=5,  # 10 remaining
                ),
            ],
        )
        seq = build_sequence_for_night("TestSeq", [target])
        xml = generate_nina_xml(seq)

        root = ET.fromstring(xml)
        # Ha should not appear (fully acquired), OIII should have 10 remaining
        exposures = root.findall(".//TakeExposures")
        assert len(exposures) == 1  # only OIII
        total = exposures[0].find("TotalExposures")
        assert total is not None
        assert total.text == "10"

    def test_park_and_warm_on_finish(self):
        seq = NinaSequenceFile(
            name="Test", targets=[], park_on_finish=True, warm_camera_on_finish=True
        )
        xml = generate_nina_xml(seq)
        assert "ParkScope" in xml
        assert "WarmCamera" in xml

    def test_multiple_targets(self):
        targets = [
            NinaSequenceTarget(
                name="M 42", ra_hours=5.588, dec_degrees=-5.39,
                exposures=[ExposurePlan(filter_type=FilterType.HA, count=10)],
            ),
            NinaSequenceTarget(
                name="M 31", ra_hours=0.712, dec_degrees=41.27,
                exposures=[ExposurePlan(filter_type=FilterType.LUMINANCE, count=20)],
            ),
        ]
        seq = build_sequence_for_night("MultiTarget", targets)
        xml = generate_nina_xml(seq)

        root = ET.fromstring(xml)
        containers = root.findall(".//DeepSkyObjectContainer")
        assert len(containers) == 2
