"""Tests for NINA Advanced Sequencer JSON generation (proven-schema edition)."""

import json

from photonscript.shared.models import (
    ExposurePlan, FilterType, NinaSequenceFile, NinaSequenceTarget,
)
from photonscript.scheduler.nina_sequence_json import generate_nina_json
from photonscript.scheduler.nina_sequence import build_sequence_for_night


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _types(data):
    return [d["$type"] for d in _walk(data) if isinstance(d, dict) and "$type" in d]


def _gen(**target_kwargs):
    target = NinaSequenceTarget(
        name="M 42", ra_hours=5.588, dec_degrees=-5.39,
        exposures=[ExposurePlan(filter_type=FilterType.HA,
                                exposure_seconds=300, count=20, gain=200)],
        **target_kwargs)
    seq = build_sequence_for_night("TestSeq", [target])
    seq.wait_until_local = "21:00:00"
    return json.loads(generate_nina_json(seq))


class TestNinaJsonGeneration:
    def test_valid_json_root(self):
        data = _gen()
        assert "NINA.Sequencer.Container.SequenceRootContainer" in data["$type"]
        areas = [i["$type"] for i in data["Items"]["$values"]]
        assert any("StartAreaContainer" in t for t in areas)
        assert any("EndAreaContainer" in t for t in areas)

    def test_no_unknown_instructions(self):
        """SlewScopeAndCenter does not exist in NINA 3.2 — must never appear."""
        types = _types(_gen())
        assert not any("SlewScopeAndCenter" in t for t in types)
        assert any("SlewScopeToRaDec" in t for t in types)
        assert any("Platesolving.Center" in t for t in types)

    def test_cooling_duration_is_minutes(self):
        cools = [d for d in _walk(_gen()) if isinstance(d, dict)
                 and "CoolCamera" in d.get("$type", "")]
        assert cools and cools[0]["Duration"] == 2.0   # minutes, not seconds

    def test_dusk_provider_gate(self):
        waits = [d for d in _walk(_gen()) if isinstance(d, dict)
                 and "WaitForTime" in d.get("$type", "")]
        assert waits
        assert "DuskProvider" in waits[0]["SelectedProvider"]["$type"]

    def test_equipment_connect_block(self):
        devices = [d["SelectedDevice"] for d in _walk(_gen())
                   if isinstance(d, dict) and "ConnectEquipment" in d.get("$type", "")]
        for dev in ("Camera", "Filter Wheel", "Focuser", "Mount",
                    "Safety Monitor", "Guider", "Weather"):
            assert dev in devices

    def test_smart_exposure_with_loop_and_core_filterinfo(self):
        data = _gen()
        smarts = [d for d in _walk(data) if isinstance(d, dict)
                  and "SmartExposure" in d.get("$type", "")]
        assert smarts
        loop = smarts[0]["Conditions"]["$values"][0]
        assert "LoopCondition" in loop["$type"]
        assert loop["Iterations"] == 20
        filters = [d for d in _walk(data) if isinstance(d, dict)
                   and d.get("$type", "").startswith("NINA.Core.Model.Equipment.FilterInfo")]
        assert filters and filters[0]["_name"] == "H"   # NINA profile name

    def test_altitude_condition_has_coordinates(self):
        alts = [d for d in _walk(_gen()) if isinstance(d, dict)
                and "AltitudeCondition" in d.get("$type", "")]
        assert alts
        data_blob = alts[0]["Data"]
        assert data_blob["Offset"] == 30.0
        assert data_blob["Coordinates"]["RAHours"] == 5

    def test_unguided_has_no_dither_or_guiding(self):
        types = _types(_gen(start_guiding=False))
        assert not any("StartGuiding" in t for t in types)
        assert not any("DitherAfterExposures" in t for t in types)

    def test_guided_has_dither_and_calibration(self):
        data = _gen(start_guiding=True, dither_every_n=5)
        types = _types(data)
        assert any("StartGuiding" in t for t in types)
        assert any("DitherAfterExposures" in t for t in types)
        starts = [d for d in _walk(data) if isinstance(d, dict)
                  and "StartGuiding" in d.get("$type", "")]
        assert starts[0]["ForceCalibration"] is True

    def test_park_and_warm_in_end(self):
        types = _types(_gen())
        assert any("ParkScope" in t for t in types)
        assert any("WarmCamera" in t for t in types)
        warms = [d for d in _walk(_gen()) if isinstance(d, dict)
                 and "WarmCamera" in d.get("$type", "")]
        assert warms[0]["Duration"] == 3.0   # minutes

    def test_pushover_narration_present(self):
        types = _types(_gen())
        assert sum("SendToPushover" in t for t in types) >= 5

    def test_lint_passes_on_generated(self):
        from photonscript.scheduler.sequence_lint import lint
        result = lint(_gen(), guided=False)
        assert result.ok, [f"{f.rule}: {f.detail}" for f in result.findings]
