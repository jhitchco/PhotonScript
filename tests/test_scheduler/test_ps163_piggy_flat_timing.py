"""PS-163: Piggy-600 OSC dawn flats start late enough to find a bright sky.

2026-10-07 (NINA #2 log): the companion's SkyFlat started at nautical dawn
+5 (+90 s) and NINA's exposure search climbed to its 30 s max while the sky
read 557 -> 8188 ADU, then failed "sky is too dim" after 14 min. The flat
start is now nautical dawn + piggyback_flat_dawn_offset_min (default 20),
capped so the set fits the armer's dawn flat hold.
"""
import json

from photonscript.shared import rigs
from photonscript.shared.config import PhotonScriptConfig
from photonscript.scheduler.calibration import (
    generate_piggyback_companion_json, osc_flat_dawn_offset)


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _short(t):
    return t.split(",")[0].split(".")[-1]


def _companion(has_safety=True, **overrides):
    cfg = rigs.rig_config(PhotonScriptConfig(
        piggyback_enabled=True, piggyback_default_gain=100,
        piggyback_default_offset=256, piggyback_dark_exposures="120",
        piggyback_setpoint_c=0.0, piggyback_image_lights=True,
        piggyback_focus_seed=0, **overrides), "piggyback")
    return json.loads(generate_piggyback_companion_json(
        cfg, has_safety=has_safety, with_lights=True))


def _flat_wait(root):
    """The NauticalDawn WaitForTime right before the 90 s slew pause."""
    targets = next(n for n in _walk(root) if n.get("$type", "").startswith(
        "NINA.Sequencer.Container.TargetAreaContainer"))
    items = targets["Items"]["$values"]
    for i, it in enumerate(items[:-1]):
        nxt = items[i + 1]
        if (_short(it["$type"]) == "WaitForTime"
                and "NauticalDawnProvider" in it["SelectedProvider"]["$type"]
                and _short(nxt["$type"]) == "WaitForTimeSpan"
                and nxt["Time"] == 90):
            return it, items[i + 2]
    raise AssertionError("no dawn flat wait")


def test_default_offset_is_20_and_safe_wait_covers_it():
    root = _companion()
    wait, wait_safe = _flat_wait(root)
    assert wait["MinutesOffset"] == 20
    assert wait_safe["Name"] == "WAIT_SAFE_FOR_OSC_FLATS"
    tc = wait_safe["Conditions"]["$values"][1]
    # max(piggyback_flat_wait_min 25, offset 20)
    assert tc["MinutesOffset"] == 25


def test_offset_configurable_and_wait_safe_never_before_start():
    root = _companion(piggyback_flat_dawn_offset_min=30)
    wait, wait_safe = _flat_wait(root)
    assert wait["MinutesOffset"] == 30
    assert wait_safe["Conditions"]["$values"][1]["MinutesOffset"] == 30


def test_offset_capped_to_the_dawn_flat_window():
    cfg = PhotonScriptConfig(piggyback_flat_dawn_offset_min=60,
                             dawn_flats_window_min=40)
    assert osc_flat_dawn_offset(cfg) == 35      # ND + 5 + 40 - 10
    cfg = PhotonScriptConfig(piggyback_flat_dawn_offset_min=1)
    assert osc_flat_dawn_offset(cfg) == 5       # never before the RC16 window
    cfg = PhotonScriptConfig(piggyback_flat_dawn_offset_min=20,
                             dawn_flats_window_min=10)
    assert osc_flat_dawn_offset(cfg) == 5


def test_no_safety_monitor_uses_the_same_start():
    root = _companion(has_safety=False, piggyback_flat_dawn_offset_min=25)
    waits = [n for n in _walk(root) if _short(n.get("$type", "")) == "WaitForTime"
             and "NauticalDawnProvider" in
             (n.get("SelectedProvider") or {}).get("$type", "")]
    assert [w["MinutesOffset"] for w in waits] == [25]


def test_config_field_on_the_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS as CONFIG_FIELDS
    keys = [f[0] for f in CONFIG_FIELDS]
    assert "piggyback_flat_dawn_offset_min" in keys
