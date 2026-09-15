"""OSC (one-shot-color) piggyback calibration generation.

The piggyback rig shares the RC16 mount and owns only its camera. Its darks
must be shot at the OSC gain/offset/setpoint (via rig_config), and its dusk
flats must be a single OSC set that never connects/slews/parks the shared
mount — it rides the RC16's slew.
"""

import json

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared import rigs
from photonscript.scheduler.calibration import (
    generate_darks_json, generate_dusk_flats_json,
    generate_piggyback_companion_json)


def _walk(node):
    """Yield every dict node in a NINA sequence JSON tree."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _types(root):
    return {n["$type"] for n in _walk(root) if isinstance(n.get("$type"), str)}


def test_osc_darks_use_piggyback_gain_offset_and_setpoint():
    cfg = PhotonScriptConfig(
        piggyback_enabled=True,
        piggyback_default_gain=100,
        piggyback_default_offset=256,
        piggyback_setpoint_c=-5.0,
    )
    pc = rigs.rig_config(cfg, "piggyback")
    txt, minutes = generate_darks_json(pc, [(120.0, 5)], bias_count=10)
    root = json.loads(txt)
    exps = [n for n in _walk(root) if n.get("ExposureTime") is not None]
    assert exps, "no TakeExposure items generated"
    assert all(e.get("Gain") == 100 for e in exps)
    assert all(e.get("Offset") == 256 for e in exps)
    assert minutes > 0
    # cools to the piggyback setpoint, not the RC16 default (0.0)
    assert "-5" in txt


def test_osc_flats_single_set_and_never_touches_the_mount():
    cfg = PhotonScriptConfig(piggyback_enabled=True)
    pc = rigs.rig_config(cfg, "piggyback")
    txt, start_local = generate_dusk_flats_json(pc, osc=True, owns_mount=False)
    root = json.loads(txt)
    types = _types(root)
    # the OSC sky-flat block is present, with no filter-wheel switching
    assert "NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat, NINA.Sequencer" in types
    assert not any("FilterWheel.SwitchFilter" in t for t in types)
    # riding the shared mount: no slew / unpark / park / safety-wait
    assert not any("Telescope." in t for t in types)
    assert not any("SafetyMonitor.WaitUntilSafe" in t for t in types)
    # exactly one flat block (one OSC set, not seven per-filter sets)
    flats = [n for n in _walk(root)
             if n.get("$type", "").startswith(
                 "NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat")]
    assert len(flats) == 1
    assert ":" in start_local  # HH:MM


def test_filtered_flats_still_slew_the_mount():
    # the RC16 (owns the mount) still gets the full slew/park flat routine.
    cfg = PhotonScriptConfig()
    txt, _ = generate_dusk_flats_json(cfg, osc=False, owns_mount=True)
    types = _types(json.loads(txt))
    assert any("Telescope.SlewScopeToAltAz" in t for t in types)
    assert any("Telescope.ParkScope" in t for t in types)
    assert any("FilterWheel.SwitchFilter" in t for t in types)


def _pb_cfg():
    return rigs.rig_config(PhotonScriptConfig(
        piggyback_enabled=True, piggyback_default_gain=100,
        piggyback_default_offset=256, piggyback_dark_exposures="120",
        piggyback_setpoint_c=0.0), "piggyback")


def test_companion_no_safety_is_flats_only():
    txt = generate_piggyback_companion_json(_pb_cfg(), has_safety=False)
    root = json.loads(txt)
    types = _types(root)
    # dawn OSC flats present, exactly one set
    flats = [n for n in _walk(root)
             if n.get("$type", "").startswith(
                 "NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat")]
    assert len(flats) == 1
    # no darks/bias and no safety gating without a safety monitor
    assert not any("LoopWhileUnsafe" in t for t in types)
    assert not any("WaitUntilSafe" in t for t in types)
    exps = [n.get("ImageType") for n in _walk(root)
            if n.get("ImageType") in ("DARK", "BIAS")]
    assert exps == []
    # an annotation explains why darks/bias were skipped
    assert any("Annotation" in t for t in types)
    # never touches the shared mount
    assert not any("Telescope." in t for t in types)


def test_companion_with_safety_adds_darks_bias_gated():
    txt = generate_piggyback_companion_json(_pb_cfg(), has_safety=True)
    root = json.loads(txt)
    types = _types(root)
    # darks + bias present, gated LoopWhileUnsafe, plus a WaitUntilSafe hold
    imgtypes = {n.get("ImageType") for n in _walk(root)
                if n.get("ImageType") in ("DARK", "BIAS")}
    assert imgtypes == {"DARK", "BIAS"}
    assert any("LoopWhileUnsafe" in t for t in types)
    assert any("SafetyMonitor.WaitUntilSafe" in t for t in types)
    # OSC gain/offset on the dark/bias frames
    cal = [n for n in _walk(root) if n.get("ImageType") in ("DARK", "BIAS")]
    assert all(n.get("Gain") == 100 and n.get("Offset") == 256 for n in cal)
    # still one flat set, still never slews the shared mount
    flats = [n for n in _walk(root)
             if n.get("$type", "").startswith(
                 "NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat")]
    assert len(flats) == 1
    assert not any("Telescope." in t for t in types)


def test_companion_always_warms_the_camera():
    for hs in (True, False):
        types = _types(json.loads(
            generate_piggyback_companion_json(_pb_cfg(), has_safety=hs)))
        assert any("Camera.WarmCamera" in t for t in types)


def test_dusk_flats_only_filters_refreshes_just_broadband():
    # only_filters lets the RC16 re-shoot a subset (e.g. the stale LRGB masters)
    # without re-doing fresh narrowband: exactly 4 SkyFlat blocks, still slews.
    cfg = PhotonScriptConfig()
    txt, _ = generate_dusk_flats_json(
        cfg, only_filters=["L", "R", "G", "B"], osc=False, owns_mount=True)
    root = json.loads(txt)
    flats = [n for n in _walk(root)
             if n.get("$type", "").startswith(
                 "NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat")]
    assert len(flats) == 4
    types = _types(root)
    assert any("Telescope.SlewScopeToAltAz" in t for t in types)
