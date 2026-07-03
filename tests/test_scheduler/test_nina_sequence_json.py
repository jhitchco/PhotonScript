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


class TestNightLoopArchitecture:
    """The Jerry Macon / Patriot Astro safety-loop pattern (all core NINA)."""

    def test_night_loop_structure(self):
        data = _gen()
        names = [d.get("Name") for d in _walk(data) if isinstance(d, dict)]
        for expected in ("LOOP_ALL_NIGHT", "SAFE_LOOP", "UNSAFE",
                         "TARGETS_CONTAINER", "RESET_EQUIPMENT_ONCE_SAFE"):
            assert expected in names

    def test_wait_until_safe_present(self):
        types = _types(_gen())
        assert any("WaitUntilSafe" in t for t in types)

    def test_dawn_bounded(self):
        conds = [d for d in _walk(_gen()) if isinstance(d, dict)
                 and "TimeCondition" in d.get("$type", "")]
        assert any("DawnProvider" in json.dumps(c.get("SelectedProvider", {}))
                   for c in conds)

    def test_unsafe_branch_parks_then_waits(self):
        data = _gen()
        unsafe = next(d for d in _walk(data) if isinstance(d, dict)
                      and d.get("Name") == "UNSAFE")
        seq_types = [i["$type"] for i in unsafe["Items"]["$values"]]
        park_idx = next(i for i, t in enumerate(seq_types) if "ParkScope" in t)
        wait_idx = next(i for i, t in enumerate(seq_types) if "WaitUntilSafe" in t)
        assert park_idx < wait_idx   # park FIRST, then wait for weather

    def test_no_external_script(self):
        """The blank-script hack blocked sequence start on NINA 3.2 —
        replaced with park-and-hold-until-dawn after the last target."""
        types = _types(_gen())
        assert not any("ExternalScript" in t for t in types)

    def test_targets_done_parks_and_holds_until_dawn(self):
        data = _gen()
        safe_loop = next(d for d in _walk(data) if isinstance(d, dict)
                         and d.get("Name") == "SAFE_LOOP")
        seq_types = [i.get("$type", "") for i in safe_loop["Items"]["$values"]]
        park_idx = next(i for i, x in enumerate(seq_types) if "ParkScope" in x)
        wait_idx = next(i for i, x in enumerate(seq_types) if "WaitForTime," in x)
        assert park_idx < wait_idx
        wait = safe_loop["Items"]["$values"][wait_idx]
        assert "DawnProvider" in wait["SelectedProvider"]["$type"]

    def test_safety_monitor_connects_before_wait_until_safe(self):
        data = _gen()
        startup = next(d for d in _walk(data) if isinstance(d, dict)
                       and d.get("Name") == "AARO startup")
        types_order = []
        for item in startup["Items"]["$values"]:
            t = item.get("$type", "")
            if "ConnectEquipment" in t:
                types_order.append(f"connect:{item['SelectedDevice']}")
            elif "WaitUntilSafe" in t:
                types_order.append("wait_safe")
        assert types_order.index("connect:Safety Monitor") \
            < types_order.index("wait_safe")

    def test_twilight_autofocus_before_astro_dusk_gate(self):
        data = _gen()
        startup = next(d for d in _walk(data) if isinstance(d, dict)
                       and d.get("Name") == "AARO startup")
        seq = [i.get("$type", "") for i in startup["Items"]["$values"]]
        af = next(i for i, t in enumerate(seq) if "RunAutofocus" in t)
        # the astro-dusk (DuskProvider) gate must come after the twilight AF
        waits = [i for i, t in enumerate(seq) if "WaitForTime," in t]
        astro_gate = max(waits)
        assert af < astro_gate
