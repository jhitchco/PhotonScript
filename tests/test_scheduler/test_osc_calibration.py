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
    generate_darks_json, generate_dusk_flats_json)


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
