"""Tests for NINA Advanced Sequencer JSON generation."""

import json

from photonscript.shared.models import (
    ExposurePlan, FilterType, NinaSequenceFile, NinaSequenceTarget,
)
from photonscript.scheduler.nina_sequence_json import generate_nina_json
from photonscript.scheduler.nina_sequence import build_sequence_for_night


class TestNinaJsonGeneration:
    def test_generates_valid_json(self):
        target = NinaSequenceTarget(
            name="M 42",
            ra_hours=5.588,
            dec_degrees=-5.39,
            exposures=[
                ExposurePlan(filter_type=FilterType.HA, exposure_seconds=300, count=20),
            ],
        )
        seq = build_sequence_for_night("TestSeq", [target])
        result = generate_nina_json(seq)

        # Must be valid JSON
        data = json.loads(result)
        assert "$type" in data
        assert "NINA.Sequencer.Container.SequenceRootContainer" in data["$type"]

    def test_contains_type_annotations(self):
        target = NinaSequenceTarget(
            name="M 31",
            ra_hours=0.712,
            dec_degrees=41.27,
            exposures=[
                ExposurePlan(filter_type=FilterType.LUMINANCE, exposure_seconds=180, count=30),
            ],
        )
        seq = build_sequence_for_night("TestSeq", [target])
        result = generate_nina_json(seq)
        data = json.loads(result)

        # Should have start, target, and end areas
        items = data["Items"]["$values"]
        assert len(items) == 3  # StartArea, TargetArea, EndArea

        # Check types
        assert "StartAreaContainer" in items[0]["$type"]
        assert "TargetAreaContainer" in items[1]["$type"]
        assert "EndAreaContainer" in items[2]["$type"]

    def test_target_coordinates(self):
        target = NinaSequenceTarget(
            name="Test",
            ra_hours=12.5,
            dec_degrees=-30.75,
            exposures=[
                ExposurePlan(filter_type=FilterType.HA, count=5),
            ],
        )
        seq = build_sequence_for_night("CoordTest", [target])
        result = generate_nina_json(seq)
        data = json.loads(result)

        # Navigate to target container
        target_area = data["Items"]["$values"][1]
        target_container = target_area["Items"]["$values"][0]
        coords = target_container["Target"]["InputCoordinates"]

        assert coords["RAHours"] == 12
        assert coords["NegativeDec"] is True

    def test_exposure_details(self):
        target = NinaSequenceTarget(
            name="NGC 7000",
            ra_hours=20.981,
            dec_degrees=44.53,
            exposures=[
                ExposurePlan(filter_type=FilterType.HA, exposure_seconds=300, count=20, gain=139),
                ExposurePlan(filter_type=FilterType.OIII, exposure_seconds=300, count=15, gain=139),
            ],
        )
        seq = build_sequence_for_night("ExpTest", [target])
        result = generate_nina_json(seq)
        data = json.loads(result)

        target_area = data["Items"]["$values"][1]
        container = target_area["Items"]["$values"][0]
        items = container["Items"]["$values"]

        # Find TakeManyExposures items
        exposures = [i for i in items if "TakeManyExposures" in i.get("$type", "")]
        assert len(exposures) == 2
        assert exposures[0]["ExposureTime"] == 300
        assert exposures[0]["Gain"] == 139

    def test_park_and_warm_in_end_area(self):
        seq = NinaSequenceFile(
            name="Test", targets=[], park_on_finish=True, warm_camera_on_finish=True
        )
        result = generate_nina_json(seq)
        data = json.loads(result)

        end_area = data["Items"]["$values"][2]
        end_items = end_area["Items"]["$values"]
        end_types = [i["$type"] for i in end_items]

        assert any("WarmCamera" in t for t in end_types)
        assert any("ParkScope" in t for t in end_types)

    def test_triggers_included(self):
        target = NinaSequenceTarget(
            name="Test",
            ra_hours=5.0,
            dec_degrees=30.0,
            exposures=[ExposurePlan(filter_type=FilterType.HA, count=10)],
            dither_every_n=3,
            auto_focus_interval_minutes=60,
            meridian_flip=True,
        )
        seq = build_sequence_for_night("TriggerTest", [target])
        result = generate_nina_json(seq)
        data = json.loads(result)

        target_area = data["Items"]["$values"][1]
        container = target_area["Items"]["$values"][0]
        triggers = container["Triggers"]["$values"]

        trigger_types = [t["$type"] for t in triggers]
        assert any("MeridianFlip" in t for t in trigger_types)
        assert any("Autofocus" in t for t in trigger_types)
        assert any("Dither" in t for t in trigger_types)
