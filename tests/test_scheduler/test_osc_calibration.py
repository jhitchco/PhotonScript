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


def test_companion_calibration_locks_piggyback_params():
    """OSC darks + bias in the companion use the PIGGYBACK gain/offset (100/256),
    never the mono default (200/…) — even if called with the main config, so a
    missing rig_config remap can't silently mismatch the OSC lights."""
    cfg = PhotonScriptConfig(
        piggyback_enabled=True,
        piggyback_default_gain=100, piggyback_default_offset=256,
        default_gain=200, default_offset=50,   # mono — must NOT leak in
    )
    root = json.loads(generate_piggyback_companion_json(
        cfg, has_safety=False, with_lights=False))
    cal = [n for n in _walk(root) if n.get("ExposureTime") is not None
           and n.get("ImageType") in ("DARK", "BIAS")]
    assert cal, "no OSC dark/bias exposures generated"
    assert all(e.get("Gain") == 100 and e.get("Offset") == 256 for e in cal), \
        [(e.get("ImageType"), e.get("Gain"), e.get("Offset")) for e in cal]


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
    # the OSC sky-flat block is present, with no filter-wheel switching: its
    # one SwitchFilter (NINA's SkyFlat needs it, PS-132) selects no filter
    assert "NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat, NINA.Sequencer" in types
    switches = [n for n in _walk(root)
                if "FilterWheel.SwitchFilter" in n.get("$type", "")]
    assert len(switches) == 1 and switches[0]["Filter"] is None
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


def test_companion_no_safety_fires_darks_bias_unconditionally():
    # Jeremy 2026-09-25: without a safety monitor the OSC would otherwise get
    # ZERO matching calibration, so darks/bias now fire ANYWAY — ungated (no
    # LoopWhileUnsafe), time-capped at dusk. Only lights stay off.
    txt = generate_piggyback_companion_json(_pb_cfg(), has_safety=False)
    root = json.loads(txt)
    types = _types(root)
    # dawn OSC flats present, exactly one set
    flats = [n for n in _walk(root)
             if n.get("$type", "").startswith(
                 "NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat")]
    assert len(flats) == 1
    # darks AND bias ARE present now (the whole point of the change)
    exps = [n.get("ImageType") for n in _walk(root)
            if n.get("ImageType") in ("DARK", "BIAS")]
    assert "DARK" in exps and "BIAS" in exps
    # but UNGATED — no safety conditions without a monitor
    assert not any("LoopWhileUnsafe" in t for t in types)
    assert not any("WaitUntilSafe" in t for t in types)
    # an annotation explains the unconditional mode
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
    # PS-36: no UNBOUNDED WaitUntilSafe (a pre-dawn roof close wedged the
    # companion in one on 2026-09-26); the flats wait is LoopWhileUnsafe +
    # TimeCondition, and the flats are skipped (not wedged) if still unsafe
    assert not any("SafetyMonitor.WaitUntilSafe" in t for t in types)
    wait = next(n for n in _walk(root)
                if n.get("Name") == "WAIT_SAFE_FOR_OSC_FLATS")
    conds = [c["$type"] for c in wait["Conditions"]["$values"]]
    assert any("LoopWhileUnsafe" in c for c in conds)
    assert any("TimeCondition" in c for c in conds)
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


_MOVE_FOCUSER = "NINA.Sequencer.SequenceItem.Focuser.MoveFocuserAbsolute"
_RUN_AF = "NINA.Sequencer.SequenceItem.Autofocus.RunAutofocus"


def _osc_lights_root(**overrides):
    cfg = rigs.rig_config(PhotonScriptConfig(
        piggyback_enabled=True, piggyback_default_gain=100,
        piggyback_default_offset=256, piggyback_dark_exposures="120",
        piggyback_setpoint_c=0.0, piggyback_image_lights=True,
        **overrides), "piggyback")
    return json.loads(generate_piggyback_companion_json(
        cfg, has_safety=True, with_lights=True))


def test_osc_lights_seed_moves_focuser_before_first_autofocus():
    # With a cold-start seed set, the OSC light loop moves its own focuser to
    # that absolute position, then autofocuses from the ballpark.
    root = _osc_lights_root(piggyback_focus_seed=5200)
    moves = [n for n in _walk(root)
             if n.get("$type", "").startswith(_MOVE_FOCUSER)]
    assert len(moves) == 1, "expected exactly one focuser seed before AF"
    assert moves[0].get("Position") == 5200
    # the seed precedes the first RunAutofocus in the OSC image pass
    lights = next(n for n in _walk(root)
                  if n.get("Name") == "OSC_IMAGE_PASS")
    order = [c.get("$type", "") for c in lights["Items"]["$values"]]
    move_i = next(i for i, t in enumerate(order) if t.startswith(_MOVE_FOCUSER))
    af_i = next(i for i, t in enumerate(order) if t.startswith(_RUN_AF))
    assert move_i < af_i


def test_osc_lights_no_seed_leaves_focuser_untouched():
    # Explicitly disabled (seed 0, no harvest history): AF with no absolute
    # move, so a wrong seed can never drive the OSC focuser to a bad position.
    root = _osc_lights_root(piggyback_focus_seed=0)
    assert not any(n.get("$type", "").startswith(_MOVE_FOCUSER)
                   for n in _walk(root))
    assert any(n.get("$type", "").startswith(_RUN_AF) for n in _walk(root))


# ---- PS-25: NINA #2 resume debounce + one roof-open push --------------------

_WAIT_TS = "NINA.Sequencer.SequenceItem.Utility.WaitForTimeSpan"
_PUSHOVER = "DaleGhent.NINA.GroundStation.SendToPushover.SendToPushover"


def _short(t):
    return t.split(",")[0].split(".")[-1]


def _named(root, name):
    return next(n for n in _walk(root) if n.get("Name") == name)


def _hold_seconds(hold):
    """Total hold of a LoopCondition(n) x WaitForTimeSpan(step) container."""
    loop = next(c for c in hold["Conditions"]["$values"]
                if _short(c["$type"]) == "LoopCondition")
    step = hold["Items"]["$values"][0]
    assert step["$type"].startswith(_WAIT_TS)
    return loop["Iterations"] * step["Time"]


def test_osc_resume_hold_orders_wait_hold_wait_before_autofocus():
    # Each pass: bounded wait safe, the hold, bounded wait safe again, and only
    # then the image pass that runs AF and lights (mirrors NINA #1's resume).
    root = _osc_lights_root(piggyback_focus_seed=0)
    lights = _named(root, "OSC_LIGHTS_UNTIL_DAWN")
    items = lights["Items"]["$values"]
    names = [i.get("Name") for i in items]
    assert names[0] == "WAIT_SAFE_OR_NAUTICAL_DAWN"
    assert names[1].startswith("OSC_RESUME_HOLD")
    assert names[2] == "WAIT_SAFE_CONFIRM_OR_NAUTICAL_DAWN"
    assert names[3] == "OSC_IMAGE_PASS"
    for wait in (items[0], items[2]):
        assert [_short(c["$type"]) for c in wait["Conditions"]["$values"]] == [
            "LoopWhileUnsafe", "TimeCondition"]
    # the first AF of a pass is a direct item of the image pass, after the
    # hold (the refocus triggers inside the light loop carry their own AF)
    assert any(i.get("$type", "").startswith(_RUN_AF)
               for i in items[3]["Items"]["$values"])
    assert not any(n.get("$type", "").startswith(_RUN_AF)
                   for i in items[:3] for n in _walk(i))


def test_osc_resume_hold_is_confirm_plus_grace_and_ends_at_dawn():
    root = _osc_lights_root(piggyback_focus_seed=0)
    hold = next(n for n in _walk(root)
                if str(n.get("Name", "")).startswith("OSC_RESUME_HOLD"))
    cfg = PhotonScriptConfig()
    assert cfg.piggyback_resume_grace_s == 300
    assert _hold_seconds(hold) >= cfg.safety_confirm_seconds + 300
    assert _hold_seconds(hold) < cfg.safety_confirm_seconds + 300 + 30
    # bounded by nautical dawn so it cannot push lights or flats past dawn
    tc = next(c for c in hold["Conditions"]["$values"]
              if _short(c["$type"]) == "TimeCondition")
    assert "NauticalDawnProvider" in tc["SelectedProvider"]["$type"]
    assert tc["MinutesOffset"] == 0
    # configurable
    root = _osc_lights_root(piggyback_focus_seed=0, safety_confirm_seconds=60,
                            piggyback_resume_grace_s=240)
    hold = next(n for n in _walk(root)
                if str(n.get("Name", "")).startswith("OSC_RESUME_HOLD"))
    assert _hold_seconds(hold) == 300


def test_osc_roof_open_push_fires_once_before_the_light_loop():
    root = _osc_lights_root(piggyback_focus_seed=0)
    roof = [n for n in _walk(root) if n.get("$type", "").startswith(_PUSHOVER)
            and "roof open" in n.get("Message", "")]
    assert len(roof) == 1
    # not inside the looping container (it resets on every safe re-entry)
    lights = _named(root, "OSC_LIGHTS_UNTIL_DAWN")
    assert not any(n is roof[0] for n in _walk(lights))
    notice = _named(root, "OSC_ROOF_OPEN_NOTICE")
    assert [_short(c["$type"]) for c in notice["Conditions"]["$values"]] == [
        "SafetyMonitorCondition", "LoopCondition", "TimeCondition"]
    targets = next(n for n in _walk(root) if n.get("$type", "").startswith(
        "NINA.Sequencer.Container.TargetAreaContainer"))
    names = [i.get("Name") for i in targets["Items"]["$values"]]
    i = names.index("OSC_ROOF_OPEN_NOTICE")
    assert names[i - 1] == "WAIT_SAFE_FOR_FIRST_OSC_LIGHTS"
    assert names[i + 1] == "OSC_LIGHTS_UNTIL_DAWN"


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
