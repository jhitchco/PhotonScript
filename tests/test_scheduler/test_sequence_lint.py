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

    def test_ps93_force_calibration_only_inside_the_calibration_slot(self):
        seq = _make_seq(start_guiding=True)
        data = json.loads(generate_nina_json(seq, cal_field={
            "name": "M67", "ra_hours": 8.855, "dec_degrees": 11.82}))
        assert lint(data, guided=True).ok
        blob = json.dumps(data).replace('"ForceCalibration": false',
                                        '"ForceCalibration": true')
        result = lint(json.loads(blob), guided=True)
        assert not result.ok
        assert any(f.rule == "phd2-calibration" for f in result.findings)

    def test_catches_warm_cooling_setpoint(self):
        data = json.loads(generate_nina_json(_make_seq()))
        blob = json.dumps(data).replace('"Temperature": -10.0', '"Temperature": 5')
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


# --- PS-65: focuser seed moves must be followed by autofocus -----------------

def _item(t, **kw):
    return {"$type": f"NINA.Sequencer.SequenceItem.{t}, NINA.Sequencer", **kw}


def _container(*items):
    return {"$type": "NINA.Sequencer.Container.SequentialContainer, NINA.Sequencer",
            "Items": {"$values": list(items)}}


def _smart(image_type="LIGHT"):
    return {"$type": "NINA.Sequencer.SequenceItem.Imaging.SmartExposure, "
                     "NINA.Sequencer",
            "Items": {"$values": [
                _item("FilterWheel.SwitchFilter", Filter={"_name": "H"}),
                _item("Imaging.TakeExposure", ImageType=image_type)]}}


def _focus_findings(seq):
    return [f for f in lint(seq).findings if f.rule == "focus-seed"]


class TestFocusSeedRule:
    def test_seed_then_light_without_af_is_error(self):
        # the 2026-09-26 pattern: block seed move straight into a SmartExposure
        seq = _container(_container(_item("Focuser.MoveFocuserAbsolute",
                                          Position=5853), _smart()))
        found = _focus_findings(seq)
        assert len(found) == 1 and found[0].level == "ERROR"
        assert "5853" in found[0].detail
        assert not lint(seq).ok

    def test_seed_then_af_then_light_passes(self):
        seq = _container(_item("Focuser.MoveFocuserAbsolute", Position=6040),
                         _item("Autofocus.RunAutofocus"), _smart())
        assert _focus_findings(seq) == []

    def test_af_in_trigger_runner_does_not_count(self):
        # a trigger may never fire, so only an AF in the item flow satisfies it
        tgt = _container(_item("Focuser.MoveFocuserAbsolute", Position=5853),
                         _smart())
        tgt["Triggers"] = {"$values": [{"$type": "X.Trigger", "TriggerRunner": {
            "Items": {"$values": [_item("Autofocus.RunAutofocus")]}}}]}
        assert len(_focus_findings(_container(tgt))) == 1

    def test_seed_then_darks_is_not_flagged(self):
        seq = _container(_item("Focuser.MoveFocuserAbsolute", Position=5853),
                         _smart("DARK"), _smart("BIAS"))
        assert _focus_findings(seq) == []

    def test_relative_offset_after_af_passes(self):
        seq = _container(_item("Focuser.MoveFocuserAbsolute", Position=6040),
                         _item("Autofocus.RunAutofocus"),
                         _item("Focuser.MoveFocuserRelative", RelativePosition=-187),
                         _smart())
        assert _focus_findings(seq) == []

    def test_generated_sequences_have_no_focus_seed_findings(self):
        for guided in (False, True):
            data = json.loads(generate_nina_json(_make_seq(start_guiding=guided)))
            assert _focus_findings(data) == []
