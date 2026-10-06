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
        from photonscript.scheduler.nina_sequence_json import _cool_camera
        # Duration is MINUTES (not seconds). Default is now 0 = instant (no ramp,
        # matching the warm); a nonzero ramp is still expressible.
        cools = [d for d in _walk(_gen()) if isinstance(d, dict)
                 and "CoolCamera" in d.get("$type", "")]
        assert cools and cools[0]["Duration"] == 0.0   # instant by default
        assert _cool_camera(-10.0, 2.0)["Duration"] == 2.0  # minutes, restorable

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
        # The imaging filter's core FilterInfo carries the NINA profile name.
        # (The very first switch is now the L autofocus filter — see the
        # autofocus_filter tests — so assert H is present, not that it's first.)
        assert filters and "H" in [f["_name"] for f in filters]

    def test_altitude_condition_has_coordinates(self):
        alts = [d for d in _walk(_gen()) if isinstance(d, dict)
                and "AltitudeCondition" in d.get("$type", "")]
        assert alts
        data_blob = alts[0]["Data"]
        assert data_blob["Offset"] == 30.0
        assert data_blob["Coordinates"]["RAHours"] == 5

    def test_unguided_has_no_guiding_but_disabled_dither_trigger(self):
        # NINA's SmartExposure requires a DitherAfterExposures trigger at
        # Triggers[0] or its validator throws ArgumentOutOfRangeException.
        # Unguided runs therefore still emit the trigger, but with
        # AfterExposures=0 so no dithering actually happens.
        data = _gen(start_guiding=False)
        types = _types(data)
        assert not any("StartGuiding" in t for t in types)
        dithers = [d for d in _walk(data) if isinstance(d, dict)
                   and "DitherAfterExposures" in d.get("$type", "")]
        assert dithers, "SmartExposure must always carry a dither trigger"
        assert all(d["AfterExposures"] == 0 for d in dithers)

    def test_guided_has_dither_and_calibration(self):
        data = _gen(start_guiding=True, dither_every_n=5)
        types = _types(data)
        assert any("StartGuiding" in t for t in types)
        assert any("DitherAfterExposures" in t for t in types)
        starts = [d for d in _walk(data) if isinstance(d, dict)
                  and "StartGuiding" in d.get("$type", "")]
        assert starts[0]["ForceCalibration"] is False   # PS-72 default

    def test_park_and_warm_in_end(self):
        types = _types(_gen())
        assert any("ParkScope" in t for t in types)
        assert any("WarmCamera" in t for t in types)
        warms = [d for d in _walk(_gen()) if isinstance(d, dict)
                 and "WarmCamera" in d.get("$type", "")]
        # Default gradual_warm_minutes=0 -> instant warm (cut the TEC, no ramp
        # that would fight the next arm/precool).
        assert warms[0]["Duration"] == 0.0   # minutes (0 = instant)

    def test_warm_camera_honors_gradual_minutes(self):
        from photonscript.scheduler.nina_sequence_json import _warm_camera
        assert _warm_camera()["Duration"] == 0.0        # default: instant
        assert _warm_camera(3.0)["Duration"] == 3.0     # ramp restorable

    def test_force_first_calibration_default_off(self):
        # PS-72: default off, the saved PHD2 calibration is trusted.
        starts = [d for d in _walk(_gen(start_guiding=True)) if isinstance(d, dict)
                  and "StartGuiding" in d.get("$type", "")]
        assert starts and all(s["ForceCalibration"] is False for s in starts)

    def test_force_first_calibration_toggle_on(self, monkeypatch):
        monkeypatch.setenv("PS_GUIDING_FORCE_FIRST_CALIBRATION", "true")
        starts = [d for d in _walk(_gen(start_guiding=True)) if isinstance(d, dict)
                  and "StartGuiding" in d.get("$type", "")]
        assert starts and starts[0]["ForceCalibration"] is True

    def test_force_first_calibration_toggle_off(self, monkeypatch):
        # PS_-prefixed env feeds the config generate_nina_json builds internally.
        monkeypatch.setenv("PS_GUIDING_FORCE_FIRST_CALIBRATION", "false")
        starts = [d for d in _walk(_gen(start_guiding=True)) if isinstance(d, dict)
                  and "StartGuiding" in d.get("$type", "")]
        assert starts and starts[0]["ForceCalibration"] is False

    def test_ps93_calibration_slot_overrides_forced_first_calibration(self, monkeypatch):
        """With a PHD2_CALIBRATION slot no target forces a calibration, even
        with the PS-72 switch on; the slot's own StartGuiding does."""
        monkeypatch.setenv("PS_GUIDING_FORCE_FIRST_CALIBRATION", "true")
        target = NinaSequenceTarget(
            name="M 42", ra_hours=5.588, dec_degrees=-5.39, start_guiding=True,
            exposures=[ExposurePlan(filter_type=FilterType.HA,
                                    exposure_seconds=300, count=20, gain=200)])
        seq = build_sequence_for_night("TestSeq", [target])
        seq.wait_until_local = "21:00:00"
        data = json.loads(generate_nina_json(seq, cal_field={
            "name": "NGC 2301", "ra_hours": 6.863, "dec_degrees": 0.47}))
        starts = [d for d in _walk(data) if isinstance(d, dict)
                  and "StartGuiding" in d.get("$type", "")]
        assert [s_["ForceCalibration"] for s_ in starts] == [True, False]
        from photonscript.scheduler.sequence_lint import lint
        assert lint(data, guided=True).ok

    def test_af_filter_type_resolves(self):
        from photonscript.scheduler.nina_sequence_json import _af_filter_type
        from photonscript.shared.config import PhotonScriptConfig
        assert _af_filter_type(PhotonScriptConfig(autofocus_filter="L")) \
            is FilterType.LUMINANCE
        assert _af_filter_type(PhotonScriptConfig(autofocus_filter="Ha")) \
            is FilterType.HA
        assert _af_filter_type(PhotonScriptConfig(autofocus_filter="")) is None
        assert _af_filter_type(PhotonScriptConfig(autofocus_filter="zzz")) is None

    def test_autofocus_runs_on_af_filter_not_narrowband(self, monkeypatch):
        # Default autofocus_filter='L': a pure-Ha night must still switch to L
        # for autofocus, so AF never starves on narrowband. Stale dawn flats off
        # so L can ONLY come from the AF switch, not a broadband flat.
        from photonscript.scheduler.nina_sequence_json import _nina_filter_name
        monkeypatch.setenv("PS_AUTO_STALE_FLATS", "false")
        lum = _nina_filter_name(FilterType.LUMINANCE)
        names = [d["Filter"].get("_name") for d in _walk(_gen(start_guiding=True))
                 if isinstance(d, dict) and "SwitchFilter" in d.get("$type", "")
                 and isinstance(d.get("Filter"), dict)]
        assert lum in names   # focused on L despite an all-Ha target

    def test_autofocus_filter_disabled_keeps_imaging_filter(self, monkeypatch):
        from photonscript.scheduler.nina_sequence_json import _nina_filter_name
        monkeypatch.setenv("PS_AUTOFOCUS_FILTER", "")
        monkeypatch.setenv("PS_AUTO_STALE_FLATS", "false")
        lum = _nina_filter_name(FilterType.LUMINANCE)
        names = [d["Filter"].get("_name") for d in _walk(_gen(start_guiding=True))
                 if isinstance(d, dict) and "SwitchFilter" in d.get("$type", "")
                 and isinstance(d.get("Filter"), dict)]
        assert lum not in names  # off -> focuses in the imaging (Ha) filter only

    def test_pushover_narration_present(self):
        types = _types(_gen())
        assert sum("SendToPushover" in t for t in types) >= 5

    def _pushover_msgs(self, data):
        return [d.get("Message", "") for d in _walk(data) if isinstance(d, dict)
                and "SendToPushover" in d.get("$type", "")]

    def test_verbosity_normal_drops_per_block_narration(self):
        msgs = self._pushover_msgs(_gen(start_guiding=True))
        assert not any("block done" in m for m in msgs)   # per-block chatter gone
        assert any("slewing" in m for m in msgs)          # per-target intro stays

    def test_verbosity_verbose_restores_per_block(self, monkeypatch):
        monkeypatch.setenv("PS_PUSHOVER_VERBOSITY", "verbose")
        msgs = self._pushover_msgs(_gen(start_guiding=True))
        assert any("block done" in m for m in msgs)       # per-block narration back

    def test_verbosity_quiet_drops_per_target_steps(self, monkeypatch):
        monkeypatch.setenv("PS_PUSHOVER_VERBOSITY", "quiet")
        msgs = self._pushover_msgs(_gen(start_guiding=True))
        assert not any("capturing" in m for m in msgs)    # step lines gone
        assert not any("block done" in m for m in msgs)

    def test_lint_passes_on_generated(self):
        from photonscript.scheduler.sequence_lint import lint
        result = lint(_gen(), guided=False)
        assert result.ok, [f"{f.rule}: {f.detail}" for f in result.findings]


class TestHDRExposures:
    """A filter with an HDR short set emits BOTH a short and a long
    SmartExposure block ("special plan per target" -> generic HDR)."""

    def _hdr_data(self, **target_kwargs):
        target = NinaSequenceTarget(
            name="NGC 6543", ra_hours=17.976, dec_degrees=66.633,
            exposures=[ExposurePlan(
                filter_type=FilterType.HA, exposure_seconds=600, count=14,
                gain=200, hdr_short_seconds=60.0, hdr_short_count=12)],
            **target_kwargs)
        seq = build_sequence_for_night("HDRSeq", [target])
        seq.wait_until_local = "21:00:00"
        return json.loads(generate_nina_json(seq))

    def _smart_exposure_lengths(self, data):
        out = []
        for sm in _walk(data):
            if not (isinstance(sm, dict) and "SmartExposure" in sm.get("$type", "")):
                continue
            te = next(d for d in _walk(sm) if isinstance(d, dict)
                      and "TakeExposure" in d.get("$type", ""))
            loop = next(d for d in _walk(sm) if isinstance(d, dict)
                        and "LoopCondition" in d.get("$type", ""))
            out.append((te["ExposureTime"], loop["Iterations"]))
        return out

    def test_emits_both_short_and_long_blocks(self):
        blocks = self._smart_exposure_lengths(self._hdr_data())
        assert (60.0, 12) in blocks   # short HDR companion set
        assert (600.0, 14) in blocks  # long set

    def test_no_hdr_emits_only_long_block(self):
        # baseline single-filter target from the module helper has no HDR
        blocks = self._smart_exposure_lengths(_gen())
        assert all(exp != 60.0 for exp, _ in blocks)
        assert len(blocks) == 1

    def test_hdr_sequence_lints_clean(self):
        from photonscript.scheduler.sequence_lint import lint
        result = lint(self._hdr_data(), guided=False)
        assert result.ok, [f"{f.rule}: {f.detail}" for f in result.findings]

    def test_hdr_short_block_keeps_dither_trigger(self):
        # every SmartExposure (short and long) must carry the dither trigger
        data = self._hdr_data(start_guiding=False)
        smarts = [d for d in _walk(data) if isinstance(d, dict)
                  and "SmartExposure" in d.get("$type", "")]
        assert len(smarts) >= 2
        for sm in smarts:
            trigs = sm["Triggers"]["$values"]
            assert any("DitherAfterExposures" in t.get("$type", "") for t in trigs)


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


def _imaging_gate(data):
    """The final WaitForTime inside the AARO startup container."""
    startup = next(d for d in _walk(data) if isinstance(d, dict)
                   and d.get("Name") == "AARO startup")
    waits = [i for i in startup["Items"]["$values"]
             if "WaitForTime," in i.get("$type", "")]
    return waits[-1]


class TestFilterAwareGating:
    def test_narrowband_first_gates_at_nautical_dusk(self):
        data = _gen()   # Ha-only target
        gate = _imaging_gate(data)
        assert "NauticalDuskProvider" in gate["SelectedProvider"]["$type"]
        assert gate["MinutesOffset"] == 10
        # all-narrowband night also extends to nautical dawn
        loops = [d for d in _walk(data) if isinstance(d, dict)
                 and d.get("Name") == "LOOP_ALL_NIGHT"]
        cond = loops[0]["Conditions"]["$values"][0]
        assert "NauticalDawnProvider" in cond["SelectedProvider"]["$type"]

    def test_broadband_first_gates_at_astro_dusk(self):
        target = NinaSequenceTarget(
            name="M 51", ra_hours=13.5, dec_degrees=47.2,
            exposures=[ExposurePlan(filter_type=FilterType.LUMINANCE,
                                    exposure_seconds=180, count=30)])
        seq = build_sequence_for_night("T", [target])
        seq.wait_until_local = "00:00:00"
        data = json.loads(generate_nina_json(seq))
        gate = _imaging_gate(data)
        prov = gate["SelectedProvider"]["$type"]
        assert "DuskProvider" in prov and "Nautical" not in prov
        # broadband night loop ends at astro dawn
        loops = [d for d in _walk(data) if isinstance(d, dict)
                 and d.get("Name") == "LOOP_ALL_NIGHT"]
        cond = loops[0]["Conditions"]["$values"][0]
        dawn_prov = cond["SelectedProvider"]["$type"]
        assert "DawnProvider" in dawn_prov and "Nautical" not in dawn_prov


class TestDawnSkyFlats:
    def _night_json(self):
        import json as _json
        from datetime import datetime
        from photonscript.shared.config import PhotonScriptConfig
        from photonscript.shared.astronomy import get_seasonal_targets
        from photonscript.scheduler.target_planner import (
            create_project_from_target, plan_night_sequence)
        from photonscript.scheduler.nina_sequence import build_sequence_for_night
        from photonscript.scheduler.nina_sequence_json import generate_nina_json

        config = PhotonScriptConfig()
        projects = [create_project_from_target(t)
                    for t in get_seasonal_targets(7)]
        targets = plan_night_sequence(projects, config, datetime.utcnow())[:2]
        for t in targets:
            t.start_guiding = False
        seq = build_sequence_for_night("flats_test", targets)
        # Isolate the BASE dawn-flat behavior from the stale-flat augmentation
        # (which would otherwise add every filter here, since this throwaway
        # config has no flat library). The augmentation has its own test below.
        from unittest.mock import patch
        with patch("photonscript.scheduler.calibration.stale_flat_filters",
                   return_value=[]):
            txt = generate_nina_json(seq)
        return txt, _json.loads(txt), targets

    def test_one_skyflat_block_per_filter(self):
        txt, _, targets = self._night_json()
        filters = []
        for t in targets:
            for e in t.exposures:
                if e.filter_type.value not in filters:
                    filters.append(e.filter_type.value)
        assert txt.count(
            '"NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat') == len(filters)

    def test_stale_flats_added_to_dawn_set(self):
        """Filters whose library flats are stale get reshot at dawn even if
        tonight didn't image them (auto_stale_flats)."""
        import json as _json
        from datetime import datetime
        from unittest.mock import patch
        from photonscript.shared.config import PhotonScriptConfig
        from photonscript.shared.astronomy import get_seasonal_targets
        from photonscript.scheduler.target_planner import (
            create_project_from_target, plan_night_sequence)
        from photonscript.scheduler.nina_sequence import build_sequence_for_night
        from photonscript.scheduler.nina_sequence_json import generate_nina_json

        config = PhotonScriptConfig()
        projects = [create_project_from_target(t)
                    for t in get_seasonal_targets(7)]
        targets = plan_night_sequence(projects, config, datetime.utcnow())[:2]
        for t in targets:
            t.start_guiding = False
        seq = build_sequence_for_night("flats_test", targets)
        tonight = {e.filter_type.value for t in targets for e in t.exposures}
        stale = next((f for f in ("L", "R", "G", "B", "Ha", "OIII", "SII")
                      if f not in tonight), "L")
        with patch("photonscript.scheduler.calibration.stale_flat_filters",
                   return_value=[stale]):
            txt = generate_nina_json(seq)
        # the stale filter now appears in the dawn flat set as an extra block
        assert txt.count('"NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat') \
            == len(tonight) + 1

    def test_flats_run_before_park_in_end_area(self):
        txt, _, _ = self._night_json()
        end = txt[txt.index('"Name": "End"'):]
        assert end.index("FlatDevice.SkyFlat") < end.index("Park")

    def test_flats_wait_for_nautical_dawn_window(self):
        txt, _, _ = self._night_json()
        end = txt[txt.index('"Name": "End"'):]
        assert "NauticalDawnProvider" in end

    def test_generated_sequence_still_lints(self):
        import json as _json
        from photonscript.scheduler.sequence_lint import lint as lint_seq
        txt, _, _ = self._night_json()
        assert lint_seq(_json.loads(txt), guided=False).ok

    def test_unsafe_branch_takes_quota_darks(self):
        txt, _, _ = self._night_json()
        assert "NINA.Sequencer.Conditions.LoopWhileUnsafe" in txt
        i = txt.index("DARKS_600s")
        blk = txt[i:i + 3000]
        assert '"ImageType": "DARK"' in blk
        assert "DARKS_180s" in txt  # second exposure epoch also queued
        # dark blocks come before the WaitUntilSafe hold
        assert i < txt.index("SafetyMonitor.WaitUntilSafe", i)


def test_dew_and_cooler_on_at_start_off_at_end():
    # Jeremy's request: cooler + dew heater ON before imaging, OFF after.
    data = _gen()
    dew = [d for d in _walk(data) if "Camera.DewHeater" in d.get("$type", "")]
    onoff = [d.get("OnOff") for d in dew]
    assert onoff, "no DewHeater instructions generated"
    assert onoff[0] is True        # turned ON in the start area
    assert onoff[-1] is False      # turned OFF in the end area
    types = _types(data)
    assert any("CoolCamera" in t for t in types)   # cools before imaging
    assert any("WarmCamera" in t for t in types)   # warms (cooler off) after


class TestHDRRemaining:
    """The short set is emitted only while owed, and a finished long set is not
    re-shot just because shorts remain."""

    def _blocks(self, **exp_kw):
        target = NinaSequenceTarget(
            name="NGC 6543", ra_hours=17.976, dec_degrees=66.633,
            exposures=[ExposurePlan(filter_type=FilterType.HA,
                                    exposure_seconds=600, gain=200,
                                    hdr_short_seconds=60.0, **exp_kw)])
        seq = build_sequence_for_night("HDRSeq", [target])
        seq.wait_until_local = "21:00:00"
        return TestHDRExposures()._smart_exposure_lengths(
            json.loads(generate_nina_json(seq)))

    def test_short_set_counts_down(self):
        b = self._blocks(count=14, acquired=0, hdr_short_count=12,
                         hdr_short_acquired=9)
        assert (60.0, 3) in b and (600.0, 14) in b

    def test_short_set_done_emits_long_only(self):
        b = self._blocks(count=14, acquired=4, hdr_short_count=12,
                         hdr_short_acquired=12)
        assert b == [(600.0, 10)]

    def test_long_done_emits_short_only(self):
        b = self._blocks(count=14, acquired=14, hdr_short_count=12,
                         hdr_short_acquired=2)
        assert b == [(60.0, 10)]


# --- PS-65: filter blocks autofocus on the AF filter, then apply the offset ---

_ABS = "Focuser.MoveFocuserAbsolute"
_REL = "Focuser.MoveFocuserRelative"
_AF = "Autofocus.RunAutofocus"


def _exec_items(node):
    """Items in execution order (no trigger runners / conditions)."""
    for it in (node.get("Items") or {}).get("$values", []):
        yield it
        yield from _exec_items(it)


def _gen_multi(filters=(FilterType.LUMINANCE, FilterType.HA, FilterType.OIII)):
    target = NinaSequenceTarget(
        name="IC 1805", ra_hours=2.55, dec_degrees=61.5, start_guiding=True,
        exposures=[ExposurePlan(filter_type=f, exposure_seconds=300, count=20,
                                gain=200) for f in filters])
    seq = build_sequence_for_night("PS65", [target])
    seq.wait_until_local = "21:00:00"
    return json.loads(generate_nina_json(seq))


def _imaging_loop(data):
    return next(d for d in _walk(data) if isinstance(d, dict)
                and "imaging (repeats" in str(d.get("Name", "")))


def _block_steps(data):
    """[(kind, detail)] for the imaging loop's top-level items."""
    out = []
    for it in _imaging_loop(data)["Items"]["$values"]:
        t = it["$type"]
        if "SwitchFilter" in t:
            out.append(("switch", it["Filter"]["_name"]))
        elif _AF in t:
            out.append(("af", None))
        elif _REL in t:
            out.append(("rel", it["RelativePosition"]))
        elif _ABS in t:
            out.append(("abs", it["Position"]))
        elif "SmartExposure" in t:
            out.append(("expose", it["Items"]["$values"][0]["Filter"]["_name"]))
    return out


class TestFocusBlocksPS65:
    def test_no_absolute_seed_inside_imaging_loop(self):
        loop = _imaging_loop(_gen_multi())
        assert not any(_ABS in d.get("$type", "") for d in _walk(loop)
                       if isinstance(d, dict))

    def test_every_light_exposure_follows_an_af_after_last_seed(self):
        data = _gen_multi()
        pending = False
        for it in _exec_items(data):
            t = it.get("$type", "")
            if _ABS in t:
                pending = True
            elif _AF in t:
                pending = False
            elif "TakeExposure" in t and it.get("ImageType") == "LIGHT":
                assert not pending, "LIGHT exposure at a seed with no AF after it"

    def test_blocks_af_on_l_then_offset_for_narrowband(self):
        from photonscript.scheduler.nina_sequence_json import _nina_filter_name
        L = _nina_filter_name(FilterType.LUMINANCE)
        H = _nina_filter_name(FilterType.HA)
        O = _nina_filter_name(FilterType.OIII)
        assert _block_steps(_gen_multi()) == [
            ("switch", L), ("af", None), ("expose", L),               # no offset
            ("switch", L), ("af", None), ("rel", 120), ("expose", H),
            ("switch", L), ("af", None), ("rel", 120), ("expose", O),
        ]

    def test_offsets_come_from_config(self, monkeypatch):
        monkeypatch.setenv("PS_FOCUS_FILTER_OFFSETS", "Ha:-150,OIII:-210")
        rels = [v for k, v in _block_steps(_gen_multi()) if k == "rel"]
        assert rels == [-150, -210]

    def test_offsets_disabled_emits_no_relative_move(self, monkeypatch):
        monkeypatch.setenv("PS_FOCUS_FILTER_OFFSETS", "")
        steps = _block_steps(_gen_multi())
        assert not any(k == "rel" for k, _ in steps)
        assert sum(k == "af" for k, _ in steps) == 3   # still one AF per block

    def test_no_af_filter_focuses_in_block_filter_without_offset(self, monkeypatch):
        from photonscript.scheduler.nina_sequence_json import _nina_filter_name
        monkeypatch.setenv("PS_AUTOFOCUS_FILTER", "")
        H = _nina_filter_name(FilterType.HA)
        O = _nina_filter_name(FilterType.OIII)
        steps = _block_steps(_gen_multi((FilterType.HA, FilterType.OIII)))
        assert steps == [("switch", H), ("af", None), ("expose", H),
                         ("switch", O), ("af", None), ("expose", O)]

    def test_filter_change_trigger_removed_temp_and_hfr_kept(self):
        types = _types(_gen_multi())
        assert not any("AutofocusAfterFilterChange" in t for t in types)
        assert any("AutofocusAfterTemperatureChangeTrigger" in t for t in types)
        assert any("AutofocusAfterHFRIncreaseTrigger" in t for t in types)

    def test_target_start_seed_still_followed_by_af(self):
        dso = next(d for d in _walk(_gen_multi()) if isinstance(d, dict)
                   and "DeepSkyObjectContainer" in d.get("$type", ""))
        order = [i["$type"] for i in dso["Items"]["$values"]]
        abs_i = next(i for i, t in enumerate(order) if _ABS in t)
        assert _AF in order[abs_i + 1]

    def test_generated_multi_filter_sequence_passes_lint(self):
        from photonscript.scheduler.sequence_lint import lint
        result = lint(_gen_multi(), guided=True)
        assert result.ok, [f"{f.rule}: {f.detail}" for f in result.findings]

    def test_focus_offset_map_parsing(self):
        from photonscript.shared.config import PhotonScriptConfig
        cfg = PhotonScriptConfig(focus_filter_offsets="Ha:-187, OIII : -190,x,SII:abc")
        assert cfg.focus_offset_map() == {"Ha": -187, "OIII": -190}
        assert PhotonScriptConfig(focus_filter_offsets="").focus_offset_map() == {}

    def test_focus_offset_delta(self):
        from photonscript.scheduler.nina_sequence_json import _focus_offset
        offs = {"Ha": -187, "OIII": -190}
        assert _focus_offset(FilterType.LUMINANCE, FilterType.HA, offs) == -187
        assert _focus_offset(FilterType.HA, FilterType.OIII, offs) == -3
        assert _focus_offset(FilterType.HA, FilterType.HA, offs) == 0
        assert _focus_offset(None, FilterType.HA, offs) == 0
        assert _focus_offset(FilterType.LUMINANCE, FilterType.RED, offs) == 0
