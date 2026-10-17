"""PS-175: the Piggy-600 shoots lights only in astronomical dark.

2026-10-07: the Piggy shot 4 x 300 s lights 18:54-19:14 MST, before astro
dusk, at the RC16's startup position (no target yet); all rejected by hand.
The companion now holds the first light until astro dusk (the RC16's night
loop starts there, or earlier at nautical dusk +10 on a narrowband-first
night) and ends lights at astro dawn. The OSC dawn flats keep their
nautical-dawn timing (PS-163), and the companion stays lint-clean (PS-149
loop-spin, PS-154 cooling, PS-27 / PS-158 settle and tracking gate)."""
import copy
import json

import pytest

from photonscript.scheduler import calibration as cal
from photonscript.scheduler.sideload import lint_companion
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.rigs import PIGGYBACK, rig_config
from tests.test_scheduler.test_ps77_safety_stop import TIMES, NinaSim, _short, _walk

AD = TIMES["DawnProvider"]
ND = TIMES["NauticalDawnProvider"]
DUSK = TIMES["DuskProvider"]


def _companion(has_safety=True, with_lights=True, **cfg_kw):
    cfg = rig_config(PhotonScriptConfig(_env_file=None, **cfg_kw), PIGGYBACK)
    return json.loads(cal.generate_piggyback_companion_json(
        cfg, has_safety=has_safety, with_lights=with_lights))


def _targets(seq):
    return next(n for n in _walk(seq) if n.get("$type", "").startswith(
        "NINA.Sequencer.Container.TargetAreaContainer"))["Items"]["$values"]


def _provider(d):
    return d["SelectedProvider"]["$type"].split("DateTimeProvider.")[1].split(",")[0]


def _without_dusk_wait(seq):
    out = copy.deepcopy(seq)
    for d in _walk(out):
        items = d.get("Items")
        if isinstance(items, dict):
            items["$values"] = [i for i in items["$values"]
                                if i.get("Name") != cal.OSC_WAIT_DUSK_NAME]
    return out


def _sim(seq, **kw):
    return NinaSim(seq, tick=0.002, max_steps=3_000_000, **kw).run()


def test_first_light_waits_for_astro_dusk_in_order():
    items = _targets(_companion())
    names = [i.get("Name") for i in items]
    i = names.index(cal.OSC_WAIT_DUSK_NAME)
    assert names[i + 1] == "WAIT_SAFE_FOR_FIRST_OSC_LIGHTS"
    assert names[i + 3] == cal.OSC_LIGHTS_UNTIL_DAWN_NAME
    wait = items[i]
    inner = wait["Items"]["$values"]
    assert [_short(x["$type"]) for x in inner] == ["WaitForTime"]
    assert _provider(inner[0]) == "DuskProvider" and inner[0]["MinutesOffset"] == 0
    # a plain run-once container: no loop conditions, so nothing to spin
    assert not wait["Conditions"]["$values"]


def test_every_light_time_bound_is_astro_dawn_and_flats_stay_nautical():
    items = _targets(_companion())
    names = [i.get("Name") for i in items]
    lights = items[names.index(cal.OSC_LIGHTS_UNTIL_DAWN_NAME)]
    notice = items[names.index(cal.OSC_ROOF_OPEN_NOTICE_NAME)]
    first = items[names.index("WAIT_SAFE_FOR_FIRST_OSC_LIGHTS")]
    provs = {_provider(c) for blk in (lights, notice, first) for c in _walk(blk)
             if _short(c.get("$type", "")) == "TimeCondition"}
    assert provs == {"DawnProvider"}
    # PS-163: the flat wait still keys on nautical dawn + the flat offset
    flat_wait = [i for i in items[names.index(cal.OSC_LIGHTS_UNTIL_DAWN_NAME):]
                 if _short(i["$type"]) == "WaitForTime"][0]
    assert _provider(flat_wait) == "NauticalDawnProvider"
    assert flat_wait["MinutesOffset"] == cal.osc_flat_dawn_offset(
        rig_config(PhotonScriptConfig(_env_file=None), PIGGYBACK))
    push = [n["Message"] for n in _walk(notice) if "Message" in n]
    assert push and "astro dawn" in push[0]


def test_no_lights_no_dusk_wait():
    for kw in ({"with_lights": False}, {"has_safety": False}):
        names = [i.get("Name") for i in _targets(_companion(**kw))]
        assert cal.OSC_WAIT_DUSK_NAME not in names


@pytest.mark.parametrize("weather", ["clear", "unsafe_until_after_dusk"])
def test_sim_lights_only_between_astro_dusk_and_astro_dawn(weather):
    # cool_lead 90 min: the Start area releases before the sim starts, so
    # without the gate the first light would come about an hour before dusk
    seq = _companion(cool_lead_minutes=90)
    unsafe = [] if weather == "clear" else [(-7200, DUSK + 900)]
    sim = _sim(seq, unsafe=unsafe)
    assert sim.lights
    assert min(x["start"] for x in sim.lights) >= DUSK
    assert max(x["end"] for x in sim.lights) <= AD
    assert sim.spins() == {}, sim.spins()
    early = _sim(_without_dusk_wait(seq), unsafe=unsafe)
    if weather == "clear":
        assert min(x["start"] for x in early.lights) < DUSK - 1800


def test_companion_with_dusk_wait_is_lint_clean(tmp_path, monkeypatch):
    script = tmp_path / "settle-gate.cmd"
    script.write_text("@echo off\n", encoding="ascii")
    monkeypatch.setenv("PS_PIGGYBACK_SETTLE_GATE", "true")
    monkeypatch.setenv("PS_PIGGYBACK_SETTLE_SCRIPT", str(script))
    monkeypatch.setenv("PS_PIGGYBACK_TRACKING_GATE", "true")
    seq = _companion()
    res = lint_companion(seq)
    errors = [f for f in res.findings if f.level == "ERROR"]
    assert res.ok and not errors, errors
    assert not [f for f in res.findings if f.rule == "loop-spin"]
