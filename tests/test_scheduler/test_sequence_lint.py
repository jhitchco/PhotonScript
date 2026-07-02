"""Tests for the AARO sequence linter."""

import json

from photonscript.shared.models import (
    ExposurePlan, FilterType, NinaSequenceFile, NinaSequenceTarget,
)
from photonscript.scheduler.nina_sequence_json import generate_nina_json
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.sequence_lint import lint


def _make_seq(**target_kwargs):
    target = NinaSequenceTarget(
        name="M51",
        ra_hours=13.4980,
        dec_degrees=47.1953,
        exposures=[
            ExposurePlan(filter_type=FilterType.LUMINANCE, exposure_seconds=180,
                         count=30, gain=200, offset=50),
        ],
        **target_kwargs,
    )
    return build_sequence_for_night("LintTest", [target])


class TestSequenceLint:
    def test_generated_unguided_sequence_passes(self):
        data = json.loads(generate_nina_json(_make_seq()))
        result = lint(data, guided=False)
        assert result.ok, [f"{f.rule}: {f.detail}" for f in result.findings]

    def test_generated_guided_sequence_passes(self):
        data = json.loads(generate_nina_json(_make_seq(start_guiding=True)))
        result = lint(data, guided=True)
        assert result.ok, [f"{f.rule}: {f.detail}" for f in result.findings]

    def test_catches_zero_cooling(self):
        data = json.loads(generate_nina_json(_make_seq()))
        blob = json.dumps(data).replace('"Temperature": -10.0', '"Temperature": 0')
        result = lint(json.loads(blob))
        assert not result.ok
        assert any(f.rule == "cooling" for f in result.findings)

    def test_catches_guiding_in_unguided_run(self):
        data = json.loads(generate_nina_json(_make_seq(start_guiding=True)))
        result = lint(data, guided=False)
        assert not result.ok
        assert any(f.rule == "guiding" for f in result.findings)

    def test_catches_missing_safety(self):
        data = json.loads(generate_nina_json(_make_seq()))
        blob = json.dumps(data).replace("SafetyMonitorCondition", "NoOpCondition")
        result = lint(json.loads(blob))
        assert not result.ok
        assert any(f.rule == "safety" for f in result.findings)

    def test_filter_before_autofocus_order(self):
        data = json.loads(generate_nina_json(_make_seq()))
        result = lint(data)
        assert not any(f.rule == "filter-af" for f in result.findings)
