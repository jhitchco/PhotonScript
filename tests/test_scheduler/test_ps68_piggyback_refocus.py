"""PS-68: the Piggy-600 light loop refocuses on temperature, HFR rise and a
timer (the stand-in for the RC16 meridian flip NINA #2 can't see, and the only
in-sequence repair for a bad AF: the HFR trigger baselines on the last AF).
Same trigger JSON helpers as the RC16 target containers."""
import json

from photonscript.shared import rigs
from photonscript.shared.config import PhotonScriptConfig
from photonscript.scheduler.calibration import generate_piggyback_companion_json

_AF = "NINA.Sequencer.Trigger.Autofocus."
_RUN_AF = "NINA.Sequencer.SequenceItem.Autofocus.RunAutofocus"


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _light_loop_triggers(**overrides):
    cfg = rigs.rig_config(PhotonScriptConfig(
        piggyback_enabled=True, piggyback_image_lights=True,
        piggyback_focus_seed=0, **overrides), "piggyback")
    root = json.loads(generate_piggyback_companion_json(
        cfg, has_safety=True, with_lights=True))
    loop = next(n for n in _walk(root) if n.get("Name") == "OSC_LIGHT_LOOP")
    return {t["$type"].split(",")[0].split(".")[-1]: t
            for t in loop["Triggers"]["$values"]}


def test_default_refocus_triggers():
    trig = _light_loop_triggers()
    assert set(trig) == {"AutofocusAfterTemperatureChangeTrigger",
                         "AutofocusAfterHFRIncreaseTrigger",
                         "AutofocusAfterTimeTrigger"}
    assert trig["AutofocusAfterTemperatureChangeTrigger"]["Amount"] == 1.5
    hfr = trig["AutofocusAfterHFRIncreaseTrigger"]
    assert (hfr["Amount"], hfr["SampleSize"]) == (10.0, 4)
    assert trig["AutofocusAfterTimeTrigger"]["Amount"] == 60.0
    for t in trig.values():
        assert t["$type"].startswith(_AF)
        runner = t["TriggerRunner"]["Items"]["$values"]
        assert [i["$type"].split(",")[0] for i in runner] == [_RUN_AF]


def test_refocus_triggers_follow_config():
    trig = _light_loop_triggers(piggyback_af_temp_change_c=1.0,
                                piggyback_af_hfr_increase_pct=15.0,
                                piggyback_af_interval_min=45)
    assert trig["AutofocusAfterTemperatureChangeTrigger"]["Amount"] == 1.0
    assert trig["AutofocusAfterHFRIncreaseTrigger"]["Amount"] == 15.0
    assert trig["AutofocusAfterTimeTrigger"]["Amount"] == 45.0


def test_periodic_refocus_can_be_disabled():
    trig = _light_loop_triggers(piggyback_af_interval_min=0)
    assert "AutofocusAfterTimeTrigger" not in trig
    assert "AutofocusAfterHFRIncreaseTrigger" in trig


def test_start_of_lights_still_autofocuses():
    cfg = rigs.rig_config(PhotonScriptConfig(
        piggyback_enabled=True, piggyback_image_lights=True,
        piggyback_focus_seed=0), "piggyback")
    root = json.loads(generate_piggyback_companion_json(
        cfg, has_safety=True, with_lights=True))
    image_pass = next(n for n in _walk(root) if n.get("Name") == "OSC_IMAGE_PASS")
    kinds = [i["$type"].split(",")[0] for i in image_pass["Items"]["$values"]]
    assert _RUN_AF in kinds
    assert kinds.index(_RUN_AF) < len(kinds) - 1  # AF before the light loop
