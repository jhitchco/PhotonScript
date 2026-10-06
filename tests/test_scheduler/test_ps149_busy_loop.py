"""PS-149: the Piggy-600 companion busy loop before nautical dawn.

2026-10-06 06:20 MDT (nautical dawn 12:22Z): NINA #2 logged thousands of
LoopWhileUnsafe / SafetyMonitorCondition lines a second. NINA re-checks a
loop's conditions after a pass with no next item (estimated 0 s) and resets
it; OSC_LIGHTS_UNTIL_DAWN [TimeCondition nautical dawn] held only
conditioned containers, and in the last 30 s before dawn (a 30 s wait no
longer fits) every one of them was a no-op, so the loop spun. Same shape in
the generated companion with and without the dawn flats. The RC16 had a
sibling: SAFE_LOOP held with WaitForTime(astro dawn) while an
all-narrowband LOOP_ALL_NIGHT ends at nautical dawn -10, so once its
targets were done it re-ran unpark / Pushover / park in a busy loop.

Pinned three ways: the NINA-faithful PS-77 simulator (now with the reset
decision on the real next item, TimeCondition cut-off, wait estimates and a
per-item tick) counts passes that consume no time; the generators pace
both loops; sequence_lint rule loop-spin flags any loop with no waiting
item, in lint() and lint_companion() (the armer and sideload gates).
"""

import copy
import json

import pytest

from photonscript.scheduler import calibration as cal
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.sequence_lint import lint
from photonscript.scheduler.sideload import lint_companion
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget
from photonscript.shared.rigs import PIGGYBACK, rig_config
from tests.test_scheduler.test_ps77_safety_stop import (
    DUSK_LOCAL, LOOP_END, TIMES, NinaSim, _gen, _short, _vals, _walk)

ND = TIMES["NauticalDawnProvider"]
AD = TIMES["DawnProvider"]
TICK = 0.002            # NINA's per-item overhead: a spin is thousands/s


# --- helpers --------------------------------------------------------------------

def _companion(has_safety=True, with_lights=True, **cfg_kw):
    cfg = rig_config(PhotonScriptConfig(**cfg_kw), PIGGYBACK)
    return json.loads(cal.generate_piggyback_companion_json(
        cfg, has_safety=has_safety, with_lights=with_lights))


def _named(seq, name):
    return next(d for d in _walk(seq) if d.get("Name") == name)


def _unpaced(seq):
    """The companion as generated before PS-149: no pace wait."""
    out = copy.deepcopy(seq)
    loop = _named(out, cal.OSC_LIGHTS_UNTIL_DAWN_NAME)
    items = loop["Items"]["$values"]
    assert _short(items[-1]["$type"]) == "WaitForTimeSpan"
    items.pop()
    return out


def _noflats(seq):
    """What NINA #2 ran on 2026-10-05/06: the companion with
    WAIT_SAFE_FOR_OSC_FLATS and DAWN_SKY_FLATS_OSC removed by hand
    (C:\\dev\\wt\\tonight\\companion_noflats.py)."""
    out = copy.deepcopy(seq)
    for d in _walk(out):
        items = d.get("Items")
        if isinstance(items, dict):
            items["$values"] = [
                i for i in items["$values"]
                if not ("SKY_FLATS" in str(i.get("Name", ""))
                        or "WAIT_SAFE_FOR_OSC_FLATS" in str(i.get("Name", ""))
                        or "SkyFlat" in str(i.get("$type", "")))]
    return out


def _old_safe_loop(seq):
    """The RC16 SAFE_LOOP before PS-149: hold until astro dawn, no pace."""
    out = copy.deepcopy(seq)
    items = _named(out, nsj.SAFE_LOOP_NAME)["Items"]["$values"]
    assert _short(items[-1]["$type"]) == "WaitForTimeSpan"
    items.pop()
    wait = items[-1]
    assert _short(wait["$type"]) == "WaitForTime"
    wait["SelectedProvider"]["$type"] = wait["SelectedProvider"]["$type"].replace(
        "NauticalDawnProvider", "DawnProvider")
    wait["MinutesOffset"] = 0
    return out


def _broadband():
    t = NinaSequenceTarget(name="M33", ra_hours=1.56, dec_degrees=30.66,
                           exposures=[ExposurePlan(filter_type=FilterType.LUMINANCE,
                                                   exposure_seconds=180,
                                                   count=200, gain=100,
                                                   offset=50)])
    t.start_guiding = True
    return json.loads(nsj.generate_nina_json(
        build_sequence_for_night("PS149", [t])))


def _sim(seq, **kw):
    return NinaSim(seq, tick=TICK, max_steps=3_000_000, **kw).run()


def _spin_rules(res):
    return [f for f in res.findings if f.rule == "loop-spin"]


@pytest.fixture
def quotas_met(monkeypatch):
    """Dark quotas full and bias fresh: no dark / bias blocks anywhere."""
    monkeypatch.setattr(cal, "dark_quota",
                        lambda *a, **k: {"need": 0, "have": 30, "quota": 30})
    monkeypatch.setattr(cal, "days_since_last_bias", lambda *a, **k: 1)


@pytest.fixture
def settle_gate_on(tmp_path, monkeypatch):
    script = tmp_path / "settle-gate.cmd"
    script.write_text("@echo off\n", encoding="ascii")
    monkeypatch.setenv("PS_PIGGYBACK_SETTLE_GATE", "true")
    monkeypatch.setenv("PS_PIGGYBACK_SETTLE_SCRIPT", str(script))
    return script


SCENARIOS = {
    "clear": [],
    "unsafe_from_dawn_minus_20m": [(ND - 1200, ND + 7200)],
    "unsafe_last_15s": [(ND - 15, ND + 7200)],
    "unsafe_all_night": [(-7200, ND + 7200)],
    "flapping": [(t, t + 400) for t in range(0, ND, 1500)],
    "clears_10m_before_dawn": [(-7200, ND - 600)],
}


# --- 1. the simulator reproduces 2026-10-06 ------------------------------------------

@pytest.mark.parametrize("shape", ["generated", "noflats"])
@pytest.mark.parametrize("weather,logged", [
    ("clear", "LoopWhileUnsafe"),
    ("unsafe_from_dawn_minus_20m", "SafetyMonitorCondition")])
def test_sim_old_companion_spins_in_the_last_30s_before_nautical_dawn(
        shape, weather, logged):
    seq = _unpaced(_companion())
    if shape == "noflats":
        seq = _noflats(seq)
    sim = _sim(seq, unsafe=SCENARIOS[weather])
    spins = sim.spins()
    assert spins.get(cal.OSC_LIGHTS_UNTIL_DAWN_NAME, 0) > 1000, spins
    # every idle pass of the loop sits between dawn - 30 s and dawn + 1 s
    when = [t for n, t in sim.idle_at if n == cal.OSC_LIGHTS_UNTIL_DAWN_NAME]
    assert ND - 31 <= min(when) and max(when) <= ND + 1
    # the log line NINA wrote on every pass
    assert sim.logs[logged] > 1000


def test_sim_old_rc16_safe_loop_spins_after_its_targets_set():
    """Target below the altitude limit 3 h before astro dawn: SAFE_LOOP holds
    until astro dawn, then re-runs unpark / Pushover / park until the
    all-narrowband loop end (nautical dawn -10)."""
    sim = _sim(_old_safe_loop(_gen(oiii=200, ha=200)),
               alt_until=AD - 3 * 3600)
    assert sim.spins().get(nsj.SAFE_LOOP_NAME, 0) > 1000
    when = [t for n, t in sim.idle_at if n == nsj.SAFE_LOOP_NAME]
    assert AD <= min(when) and max(when) <= LOOP_END + 1


# --- 2. the fixed generators never spin ------------------------------------------

@pytest.mark.parametrize("weather", sorted(SCENARIOS))
@pytest.mark.parametrize("shape", ["generated", "noflats"])
def test_sim_companion_never_spins(weather, shape):
    seq = _companion()
    if shape == "noflats":
        seq = _noflats(seq)
    sim = _sim(seq, unsafe=SCENARIOS[weather])
    assert sim.spins() == {}, sim.spins()
    assert sum(sim.logs.values()) < 100, sim.logs


@pytest.mark.parametrize("weather", sorted(SCENARIOS))
def test_sim_companion_with_settle_gate_and_full_quotas_never_spins(
        settle_gate_on, quotas_met, weather):
    seq = _companion()
    assert not any("DARKS" in str(d.get("Name", "")) for d in _walk(seq))
    sim = _sim(seq, unsafe=SCENARIOS[weather])
    assert sim.spins() == {}, sim.spins()


def test_sim_companion_still_images_and_ends_at_dawn():
    sim = _sim(_companion())
    assert sim.lights
    assert max(x["end"] for x in sim.lights) <= ND
    # the old and the new loop shoot the same lights
    old = _sim(_unpaced(_companion()))
    assert [(round(x["start"]), x["done"]) for x in sim.lights] == \
        [(round(x["start"]), x["done"]) for x in old.lights]


@pytest.mark.parametrize("weather", sorted(SCENARIOS))
@pytest.mark.parametrize("alt_until", [None, AD - 3 * 3600])
def test_sim_rc16_never_spins(weather, alt_until):
    sim = _sim(_gen(oiii=200, ha=200), unsafe=SCENARIOS[weather],
               alt_until=alt_until)
    assert sim.spins() == {}, sim.spins()


@pytest.mark.parametrize("weather", ["clear", "unsafe_all_night",
                                     "unsafe_from_dawn_minus_20m"])
def test_sim_rc16_with_dark_quotas_met_never_spins(quotas_met, weather):
    seq = _gen(oiii=200, ha=200)
    assert not any(str(d.get("Name", "")).startswith("DARKS_")
                   for d in _walk(seq))
    sim = _sim(seq, unsafe=SCENARIOS[weather], alt_until=AD - 3 * 3600)
    assert sim.spins() == {}, sim.spins()


def test_sim_rc16_safe_loop_holds_until_the_loop_end():
    """Targets done early: one park, held until the loop end, no re-runs."""
    sim = _sim(_gen(oiii=200, ha=200), alt_until=AD - 3 * 3600)
    assert sim.lights and max(x["end"] for x in sim.lights) <= AD - 3 * 3600 + 300
    assert sim.idle.get(nsj.SAFE_LOOP_NAME, 0) <= 1


def test_sim_rc16_broadband_never_spins():
    sim = _sim(_broadband(), alt_until=AD - 3 * 3600)
    assert sim.spins() == {}, sim.spins()


# --- 3. the generated structure ---------------------------------------------------

def test_companion_light_loop_ends_with_the_pace_wait():
    loop = _named(_companion(), cal.OSC_LIGHTS_UNTIL_DAWN_NAME)
    pace = loop["Items"]["$values"][-1]
    assert _short(pace["$type"]) == "WaitForTimeSpan"
    assert pace["Time"] == cal.OSC_PASS_PACE_S == 30
    assert pace["Parent"] == {"$ref": loop["$id"]}


@pytest.mark.parametrize("seq_fn", [lambda: _gen(), _broadband])
def test_rc16_safe_loop_holds_to_the_loop_end_then_paces(seq_fn):
    seq = seq_fn()
    night = _named(seq, nsj.NIGHT_LOOP_NAME)
    end = next(c for c in _vals(night, "Conditions")
               if _short(c["$type"]) == "TimeCondition")
    items = _vals(_named(seq, nsj.SAFE_LOOP_NAME), "Items")
    wait, pace = items[-2], items[-1]
    assert _short(wait["$type"]) == "WaitForTime"
    assert wait["SelectedProvider"]["$type"] == end["SelectedProvider"]["$type"]
    assert wait["MinutesOffset"] == end["MinutesOffset"]
    assert _short(pace["$type"]) == "WaitForTimeSpan"
    assert pace["Time"] == nsj.SAFE_LOOP_PACE_S == 60


def test_zero_confirm_hold_still_paces_the_night_loop(monkeypatch):
    monkeypatch.setattr(nsj, "_gen_cfg_cache",
                        PhotonScriptConfig(safety_confirm_seconds=0))
    seq = _gen()
    unsafe = _vals(_named(seq, nsj.UNSAFE_BRANCH_NAME), "Items")
    assert [i["Time"] for i in unsafe
            if _short(i["$type"]) == "WaitForTimeSpan"] == [1]
    assert not _spin_rules(lint(seq, guided=True))


# --- 4. lint rule loop-spin -------------------------------------------------------

@pytest.mark.parametrize("kw", [
    dict(has_safety=True, with_lights=True),
    dict(has_safety=True, with_lights=False),
    dict(has_safety=False, with_lights=False),
    dict(has_safety=False, with_lights=True),
])
def test_generated_companion_variants_pass_loop_spin(kw):
    seq = _companion(**kw)
    assert not _spin_rules(lint_companion(seq))
    assert not _spin_rules(lint(seq))


def test_generated_companion_with_gates_and_full_quotas_passes(
        settle_gate_on, quotas_met):
    res = lint_companion(_companion())
    assert res.ok, [f.detail for f in res.findings if f.level == "ERROR"]


@pytest.mark.parametrize("seq_fn", [lambda: _gen(), _broadband])
def test_generated_rc16_passes_loop_spin(seq_fn, quotas_met):
    assert not _spin_rules(lint(seq_fn(), guided=True))


def test_lint_flags_the_companion_ninas_ran_on_2026_10_06():
    for seq in (_unpaced(_companion()), _noflats(_unpaced(_companion()))):
        for res in (lint_companion(seq), lint_companion(seq, hand_built=True),
                    lint(seq)):
            found = _spin_rules(res)
            assert found and found[0].level == "ERROR" and not res.ok
            assert cal.OSC_LIGHTS_UNTIL_DAWN_NAME in found[0].detail


def test_lint_flags_the_old_rc16_safe_loop():
    found = _spin_rules(lint(_old_safe_loop(_gen()), guided=True))
    assert found and nsj.SAFE_LOOP_NAME in found[0].detail
    assert nsj.NIGHT_LOOP_NAME not in found[0].detail     # UNSAFE paces it


# hand-built shapes ------------------------------------------------------------

def _take(kind="LIGHT", exp=60):
    return nsj._make_typed(
        "NINA.Sequencer.SequenceItem.Imaging.TakeExposure, NINA.Sequencer",
        ExposureTime=exp, ImageType=kind, ExposureCount=0)


def _loop_n(n):
    return nsj._make_typed("NINA.Sequencer.Conditions.LoopCondition, "
                           "NINA.Sequencer", CompletedIterations=0, Iterations=n)


def _unsafe_loop():
    return nsj._make_typed("NINA.Sequencer.Conditions.LoopWhileUnsafe, "
                           "NINA.Sequencer")


def _root(loop):
    return nsj.link_parents(nsj._seq_container("R", [loop]))


def _spins(loop):
    return bool(_spin_rules(lint(_root(loop))))


C = nsj._seq_container
T = nsj._time_condition("NauticalDawnProvider", 0)
S = nsj._safety_condition()


@pytest.mark.parametrize("loop", [
    # all children conditioned (the 2026-10-06 shape)
    C("L", [C("a", [nsj._wait_for_timespan(30)], conditions=[_unsafe_loop(), T]),
            C("b", [_take()], conditions=[S, T])], conditions=[T]),
    # a SafetyMonitorCondition repeat over a skippable child
    C("L", [C("a", [_take()], conditions=[nsj._loop_once(), T])], conditions=[S]),
    # LoopWhileUnsafe over instant items only
    C("L", [nsj._pushover("x", "y"), nsj._wait_until_safe()],
      conditions=[_unsafe_loop()]),
    # WaitForTime is not a pace (instant once its time has passed)
    C("L", [nsj._park(), nsj._wait_for_provider("DawnProvider", 0)],
      conditions=[S]),
    # a 0 s WaitForTimeSpan is not one either
    C("L", [nsj._wait_for_timespan(0)], conditions=[T]),
    # the pace sits inside a child that can be a no-op
    C("L", [C("c", [nsj._wait_for_timespan(30)], conditions=[S])],
      conditions=[T]),
])
def test_lint_positives(loop):
    assert _spins(loop)


@pytest.mark.parametrize("loop", [
    # a direct pace wait
    C("L", [C("a", [_take()], conditions=[S, T]), nsj._wait_for_timespan(30)],
      conditions=[T]),
    # bounded by a LoopCondition
    C("L", [C("a", [_take()], conditions=[S])], conditions=[_loop_n(14), T]),
    # an exposure / autofocus right in the loop
    C("L", [_take()], conditions=[S, T]),
    C("L", [nsj._autofocus(), C("a", [_take()], conditions=[S])],
      conditions=[T]),
    # the pace inside an unconditioned child or a LoopCondition-only child
    C("L", [C("c", [nsj._wait_for_timespan(30)])], conditions=[T]),
    C("L", [C("se", [_take()], conditions=[_loop_n(20)])], conditions=[T]),
    # no conditions at all: runs once
    C("L", [nsj._pushover("x", "y")]),
])
def test_lint_negatives(loop):
    assert not _spins(loop)


def test_lint_counts_and_names_every_spinning_loop():
    a = C("A", [C("x", [_take()], conditions=[S])], conditions=[T])
    b = C("B", [nsj._pushover("x", "y")], conditions=[_unsafe_loop()])
    found = _spin_rules(lint(nsj.link_parents(C("R", [a, b]))))
    assert len(found) == 1
    assert found[0].detail.startswith("2 loop(s)")
    assert "A (TimeCondition)" in found[0].detail
    assert "B (LoopWhileUnsafe)" in found[0].detail


# --- 5. NINA #1, 2026-10-06: a moon-capped broadband target spun until dawn ------
#
# M31 LRGB (300 s + 30 s HDR) on a night with the moon down at dusk: every
# block sits in "<filter> until moonrise" [TimeCondition 04:17], so after
# moonrise the M31 imaging loop (Safety + Altitude + loop end) held only
# no-op containers and spun ("TimeCondition finished" hundreds of times a
# second, 04:19 to dawn); NGC 604 and the Heart, after it, never imaged.

MOON = {"available": True, "down_at_dusk": True, "illum_pct": 13,
        "rise_local_hh": 4, "rise_local_mm": 17}


@pytest.fixture
def moon_rises_0417(monkeypatch):
    monkeypatch.setattr(nsj, "_moon_window", lambda: dict(MOON))


def _tgt(name, ra, dec, plans):
    t = NinaSequenceTarget(name=name, ra_hours=ra, dec_degrees=dec, exposures=[
        ExposurePlan(filter_type=f, exposure_seconds=e, count=n, gain=100,
                     offset=50, **kw) for f, e, n, kw in plans])
    t.start_guiding = True
    return t


def _night_0605(cal=False):
    """The 2026-10-05/06 RC16 night: M31 LRGB + HDR, NGC 604, the Heart
    (tonight's armed night puts a PS-144 focus calibration first)."""
    F = FilterType
    hdr = dict(hdr_short_seconds=30, hdr_short_count=10)
    targets = [
        _tgt("Andromeda Galaxy", 0.71, 41.27,
             [(F.LUMINANCE, 300, 60, hdr), (F.RED, 300, 30, hdr),
              (F.GREEN, 300, 30, hdr), (F.BLUE, 300, 30, hdr)]),
        _tgt("NGC 604", 1.58, 30.78, [(F.LUMINANCE, 300, 40, {}),
                                      (F.HA, 600, 20, {})]),
        _tgt("Heart Nebula", 2.55, 61.5, [(F.HA, 300, 40, {}),
                                          (F.OIII, 300, 40, {})]),
    ]
    if cal:
        targets.insert(0, NinaSequenceTarget(
            name="Focus calibration NGC 7789", ra_hours=23.957,
            dec_degrees=56.708, focus_calibration=True,
            focus_calibration_rounds=1,
            focus_calibration_filters=["Ha", "L", "OIII", "L", "SII", "L"]))
    return json.loads(nsj.generate_nina_json(
        build_sequence_for_night("PS149_0605", targets)))


def _is_clock_time(c):
    return _short(str((c.get("SelectedProvider") or {}).get("$type", ""))) \
        == "TimeProvider"


def _moon_cap_removed(seq):
    """Before PS-149: no moonrise TimeCondition on the target or its imaging
    loop (only on the per-filter "until moonrise" containers)."""
    out = copy.deepcopy(seq)
    for d in _walk(out):
        name = str(d.get("Name", ""))
        conds = d.get("Conditions")
        if name.endswith(nsj.FILTER_UNTIL_MOONRISE_SUFFIX) \
                or not isinstance(conds, dict):
            continue
        conds["$values"] = [c for c in conds["$values"]
                            if not (_short(c.get("$type", "")) == "TimeCondition"
                                    and _is_clock_time(c))]
        items = d["Items"]["$values"] if isinstance(d.get("Items"), dict) else []
        if name.endswith(nsj.TARGET_IMAGING_SUFFIX) and items \
                and _short(items[-1]["$type"]) == "WaitForTimeSpan":
            items.pop()                                     # and no pace
    return out


def _cal_repeats(seq):
    """Before PS-149: the calibration AF series repeated while safe and up."""
    out = copy.deepcopy(seq)
    for d in _walk(out):
        if str(d.get("Name", "")).endswith(nsj.TARGET_FOCUS_CAL_SUFFIX):
            d["Conditions"]["$values"] = [
                c for c in d["Conditions"]["$values"]
                if _short(c["$type"]) != "LoopCondition"]
    return out


class _PerTarget(NinaSim):
    """NinaSim that tags each light with its DSO container."""

    def run_instruction(self, n):
        k = len(self.lights)
        try:
            return super().run_instruction(n)
        finally:
            dso = [c.d.get("Name") for c in self.stack
                   if c.type == "DeepSkyObjectContainer"]
            for x in self.lights[k:]:
                x["target"] = dso[-1] if dso else None

    def per_target(self):
        out = {}
        for x in self.lights:
            out[x["target"]] = out.get(x["target"], 0) + 1
        return out


def test_sim_old_night_0605_spins_from_moonrise_and_starves_later_targets(
        moon_rises_0417):
    sim = _PerTarget(_moon_cap_removed(_night_0605()), tick=TICK,
                     max_steps=400_000)
    with pytest.raises(AssertionError, match="runaway loop"):
        sim.run()
    imaging = "Andromeda Galaxy" + nsj.TARGET_IMAGING_SUFFIX
    assert sim.max_idle.get(imaging, 0) > 10000
    assert set(sim.per_target()) == {"Andromeda Galaxy"}


def test_sim_night_0605_hands_over_at_moonrise(moon_rises_0417):
    sim = _PerTarget(_night_0605(), tick=TICK, max_steps=3_000_000).run()
    assert sim.spins() == {}, sim.spins()
    got = sim.per_target()
    assert got.get("Andromeda Galaxy", 0) > 50
    assert got.get("NGC 604", 0) > 0 and got.get("Heart Nebula", 0) > 0
    moonrise = 4 * 3600 + 17 * 60 + 24 * 3600 - DUSK_LOCAL
    assert max(x["end"] for x in sim.lights
               if x["target"] == "Andromeda Galaxy") <= moonrise + 6


def test_night_0605_structure_and_lint(moon_rises_0417):
    seq = _night_0605()
    for name in ("Andromeda Galaxy", "Andromeda Galaxy" + nsj.TARGET_IMAGING_SUFFIX):
        tc = [x for x in _vals(_named(seq, name), "Conditions")
              if _short(x["$type"]) == "TimeCondition" and _is_clock_time(x)]
        assert [(x["Hours"], x["Minutes"]) for x in tc] == [(4, 17)]
    # a mixed target keeps imaging its narrowband after moonrise
    ngc = _named(seq, "NGC 604" + nsj.TARGET_IMAGING_SUFFIX)
    assert not [x for x in _vals(ngc, "Conditions") if _is_clock_time(x)]
    assert not _spin_rules(lint(seq, guided=True))
    old = _spin_rules(lint(_moon_cap_removed(seq), guided=True))
    assert old and "Andromeda Galaxy imaging" in old[0].detail


def test_sim_tonight_focus_calibration_runs_once_then_the_targets(
        moon_rises_0417):
    seq = _night_0605(cal=True)
    series = _named(seq, "Focus calibration NGC 7789"
                    + nsj.TARGET_FOCUS_CAL_SUFFIX)
    assert [_short(c["$type"]) for c in _vals(series, "Conditions")] == [
        "SafetyMonitorCondition", "LoopCondition"]
    res = lint(seq, guided=True)
    assert res.ok, [f.detail for f in res.findings if f.level == "ERROR"]
    assert any("runs its AF series once" in f.detail for f in res.findings
               if f.rule == "focus-calibration")
    sim = _PerTarget(seq, tick=TICK, max_steps=3_000_000).run()
    assert sim.spins() == {}, sim.spins()
    assert sim.per_target().get("Andromeda Galaxy", 0) > 50
    # before: the series repeated while safe and the targets never imaged
    old = _PerTarget(_cal_repeats(seq), tick=TICK).run()
    assert old.per_target() == {}


def test_standalone_focus_calibration_still_repeats():
    seq = json.loads(nsj.generate_focus_calibration_json())
    series = next(d for d in _walk(seq) if str(d.get("Name", "")).endswith(
        nsj.TARGET_FOCUS_CAL_SUFFIX))
    assert [_short(c["$type"]) for c in _vals(series, "Conditions")] == [
        "SafetyMonitorCondition"]
    res = lint(seq)
    assert res.ok and any("repeats while safe" in f.detail
                          for f in res.findings if f.rule == "focus-calibration")


def test_imaging_loop_without_af_gets_a_pace(tmp_path):
    """Focus-model blocks (PS-76 part 2) carry no RunAutofocus: the imaging
    loop holds only SmartExposures, so it gets a pace wait."""
    t = _tgt("Heart Nebula", 2.55, 61.5, [(FilterType.HA, 300, 40, {}),
                                          (FilterType.OIII, 300, 40, {})])
    end = ("NauticalDawnProvider", -10)
    drive = {"script": str(tmp_path / "focus-model-move.cmd"),
             "filters": {"Ha", "OIII"}, "verify_min": 60}
    c = nsj._build_target_container(t, 30.0, loop_end=end, focus_drive=drive)
    imaging = _named(c, "Heart Nebula" + nsj.TARGET_IMAGING_SUFFIX)
    last = _vals(imaging, "Items")[-1]
    assert _short(last["$type"]) == "WaitForTimeSpan"
    assert last["Time"] == nsj.TARGET_IMAGING_PACE_S
    assert not _spins(c)
    # an AF block needs none
    c2 = nsj._build_target_container(t, 30.0, loop_end=end)
    imaging2 = _named(c2, "Heart Nebula" + nsj.TARGET_IMAGING_SUFFIX)
    assert _short(_vals(imaging2, "Items")[-1]["$type"]) != "WaitForTimeSpan"
