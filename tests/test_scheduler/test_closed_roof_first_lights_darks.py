"""2026-10-16: a whole closed-roof night produced zero darks.

NINA #2 (Piggy-600) was armed at about 20:45 MDT, after astro dusk (20:01),
after a stop: its bounded Cool Camera had FAILED (sensor 35 C), so the PS-160
cooler gate in front of OSC_DARKS_IF_UNSAFE waited its 20 min and SKIPPED at
21:08 (/api/cooler/gate: "sensor 35.0 C vs setpoint 0 C after 20 min").
SkipInstructionSetOnError ends the gate's container, so OSC_DARKS_IF_UNSAFE
FINISHED with its "OSC DARKS_450s (need 22 of 30)" block still CREATED, and
nothing ever gated again: WAIT_SAFE_FOR_FIRST_OSC_LIGHTS (a plain wait) held
all night, and the OSC_CLOSED_ROOF_DARKS with the same owed darks sits inside
OSC_LIGHTS_UNTIL_DAWN, after the first lights. The RC16 owed nothing (every
length at 30 of 30 or more), so its Start-area "Wait until safe" lost nothing
that night, but it had the same shape.

The fix: the waits before the first lights take the owed darks while unsafe.
  * WAIT_SAFE_WITH_OSC_DARKS loops over OSC_DARKS_IF_UNSAFE (night start),
  * WAIT_SAFE_FOR_FIRST_OSC_LIGHTS loops over OSC_DARKS_BEFORE_FIRST_LIGHTS
    (after astro dusk), the closed-roof darks of the light loop,
  * the RC16 Start area: STARTUP_WAIT_SAFE_WITH_DARKS over
    STARTUP_DARKS_IF_UNSAFE (now behind the cooler gate).
A gate skip ends one try, the next 30 s pass gates again; a finished set
holds in DARKS_DONE_WAIT_SAFE until safe, so a loop never shoots it twice;
the LoopWhileUnsafe watchdogs end the wait within seconds of safe.
"""
import json

import pytest

from photonscript.scheduler import calibration as cal
from photonscript.scheduler import dispatch_gate
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.sequence_lint import lint
from photonscript.scheduler.sideload import lint_companion
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget
from photonscript.shared.rigs import PIGGYBACK, rig_config

from tests.test_scheduler.test_ps61_cooler_gate import GateSim
from tests.test_scheduler.test_ps77_safety_stop import (
    HORIZON, TIMES, _Interrupt, _short, _vals, _walk)

DUSK = TIMES["DuskProvider"]
AD = TIMES["DawnProvider"]
ARM_2045 = 44 * 60          # armed 20:45, astro dusk 20:01


@pytest.fixture
def gate_on(tmp_path, monkeypatch):
    script = tmp_path / "cooler-gate.cmd"
    script.write_text("@echo off\n", encoding="ascii")
    monkeypatch.setenv("PS_COOLER_GATE_MODE", "skip")
    monkeypatch.setenv("PS_COOLER_GATE_SCRIPT", str(script))
    return script


def _cfg(tmp_path, **kw):
    kw.setdefault("observatory_tz", "America/Denver")
    kw.setdefault("piggyback_enabled", True)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                              image_watch_dir=str(tmp_path / "fits"),
                              library_dir=str(tmp_path / "lib"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _companion(tmp_path, **kw):
    """The 10-16 Piggy-600: 450 s lights, 450 s (and 120 s) darks owed (an
    empty library here: 30 of 30 owed each). The live rig has a usable bias,
    so no BIAS_AT_SETPOINT (and its gate) runs first."""
    kw.setdefault("calibration_bias_when_missing", False)
    kw.setdefault("piggyback_dark_exposures", "120,450")
    kw.setdefault("piggyback_exposure_s", 450.0)
    return json.loads(cal.generate_piggyback_companion_json(
        rig_config(_cfg(tmp_path, **kw), PIGGYBACK), has_safety=True,
        with_lights=True))


def _targets(seq):
    return next(n for n in _walk(seq) if n.get("$type", "").startswith(
        "NINA.Sequencer.Container.TargetAreaContainer"))["Items"]["$values"]


def _named(seq, name):
    return [n for n in _walk(seq) if n.get("Name") == name]


def _conds(d):
    return [_short(c["$type"]) for c in _vals(d, "Conditions")]


def _is_gate(it):
    return "cooler-gate" in str(it.get("Script", ""))


class DarkSim(GateSim):
    """GateSim that records every dark: (start, end, exposure, done)."""

    def __init__(self, seq, cold_at, arm_at=None, **kw):
        kw.setdefault("tick", 0.002)
        kw.setdefault("max_steps", 3_000_000)
        super().__init__(seq, cold_at, **kw)
        if arm_at is not None:
            self.t = float(arm_at)
        self.dark_log = []

    def run_instruction(self, n):
        if n.type == "TakeExposure" and str(n.d.get("ImageType")).upper() == "DARK":
            start = self.t
            try:
                super().run_instruction(n)
            except _Interrupt:
                self.dark_log.append((start, self.t, n.d["ExposureTime"], False))
                raise
            self.dark_log.append((start, self.t, n.d["ExposureTime"], True))
            return
        return super().run_instruction(n)

    @property
    def done(self):
        return [x for x in self.dark_log if x[3]]


# --- the Piggy-600 companion: structure -------------------------------------------

def test_companion_waits_before_first_lights_carry_the_owed_darks(tmp_path, gate_on):
    seq = _companion(tmp_path)
    items = _targets(seq)
    names = [i.get("Name") for i in items]
    i = names.index(cal.OSC_DARKS_WAIT_NAME)
    assert names[i + 1:i + 5] == [cal.OSC_WAIT_DUSK_NAME, cal.OSC_WAIT_FIRST_LIGHTS_NAME,
                                  cal.OSC_ROOF_OPEN_NOTICE_NAME,
                                  cal.OSC_LIGHTS_UNTIL_DAWN_NAME]
    assert "OSC_DARKS_IF_UNSAFE" not in names     # inside the retry loop now
    for wait_name, darks_name in ((cal.OSC_DARKS_WAIT_NAME, "OSC_DARKS_IF_UNSAFE"),
                                  (cal.OSC_WAIT_FIRST_LIGHTS_NAME,
                                   cal.OSC_FIRST_LIGHTS_DARKS_NAME)):
        wait = items[names.index(wait_name)]
        # loop while unsafe until astro dawn, a 30 s step after the darks
        assert _conds(wait) == ["LoopWhileUnsafe", "TimeCondition"]
        kids = _vals(wait, "Items")
        assert [k.get("Name") for k in kids[:1]] == [darks_name]
        assert _short(kids[1]["$type"]) == "WaitForTimeSpan" and kids[1]["Time"] == 30
        darks = kids[0]
        assert _conds(darks)[0] == "LoopWhileUnsafe" and "LoopCondition" in _conds(darks)
        inner = _vals(darks, "Items")
        # the gate first (its skip ends this try only), the hold last
        assert _is_gate(inner[0]) and inner[0]["ErrorBehavior"] == 1
        assert inner[0]["Attempts"] == 1
        assert inner[-1].get("Name") == cal.DARKS_DONE_HOLD_NAME
        assert _conds(inner[-1]) == ["LoopWhileUnsafe", "TimeCondition"]
        exp = [n["ExposureTime"] for n in _walk(darks) if n.get("ImageType") == "DARK"]
        assert sorted(exp) == [120.0, 450.0]
        assert not [n for n in _walk(wait) if n.get("ImageType") == "LIGHT"]
        txt = json.dumps(wait)
        for word in ("Slew", "Park", "Guiding", "Center", "OpenCover"):
            assert word not in txt, word
    # before the first lights: tonight's light length first, ending one
    # exposure before astro dawn (same blocks as OSC_CLOSED_ROOF_DARKS)
    first = _named(seq, cal.OSC_FIRST_LIGHTS_DARKS_NAME)[0]
    assert [n["ExposureTime"] for n in _walk(first)
            if n.get("ImageType") == "DARK"] == [450.0, 120.0]
    # the bias still follows the night-start darks (inside, before the hold)
    nd = _named(seq, "OSC_DARKS_IF_UNSAFE")[0]
    kinds = [k.get("Name") for k in _vals(nd, "Items")]
    assert kinds.index("OSC_BIAS_IF_UNSAFE") == len(kinds) - 2
    # the mid-night closed-roof darks are unchanged
    crd = _named(seq, cal.OSC_CLOSED_ROOF_DARKS_NAME)[0]
    assert _vals(crd, "Items")[-1].get("Name") != cal.DARKS_DONE_HOLD_NAME


def test_companion_is_lint_clean(tmp_path, gate_on, monkeypatch):
    script = tmp_path / "settle-gate.cmd"
    script.write_text("@echo off\n", encoding="ascii")
    monkeypatch.setenv("PS_PIGGYBACK_SETTLE_GATE", "true")
    monkeypatch.setenv("PS_PIGGYBACK_SETTLE_SCRIPT", str(script))
    res = lint_companion(_companion(tmp_path))
    errors = [f for f in res.findings if f.level == "ERROR"]
    assert res.ok and not errors, errors
    assert not [f for f in res.findings if f.rule in ("loop-spin", "cooler-gate")]


def test_companion_nothing_owed_or_off_keeps_the_plain_waits(tmp_path, gate_on,
                                                             monkeypatch):
    monkeypatch.setattr(cal, "dark_quota", lambda *a, **k: {"need": 0})
    seq = _companion(tmp_path)
    names = [i.get("Name") for i in _targets(seq)]
    assert cal.OSC_DARKS_WAIT_NAME not in names
    wait = _targets(seq)[names.index(cal.OSC_WAIT_FIRST_LIGHTS_NAME)]
    assert [_short(k["$type"]) for k in _vals(wait, "Items")] == ["WaitForTimeSpan"]
    monkeypatch.undo()
    for kw in ({"closed_roof_darks": "off"}, {"piggyback_dark_capture_window": "manual"}):
        seq = _companion(tmp_path / str(len(kw)), **kw)
        assert not _named(seq, cal.OSC_FIRST_LIGHTS_DARKS_NAME), kw


def test_companion_without_lights_keeps_the_night_start_darks(tmp_path, gate_on):
    seq = json.loads(cal.generate_piggyback_companion_json(
        rig_config(_cfg(tmp_path, piggyback_dark_exposures="450"), PIGGYBACK),
        has_safety=True))
    names = [i.get("Name") for i in _targets(seq)]
    assert "OSC_DARKS_IF_UNSAFE" in names and cal.OSC_DARKS_WAIT_NAME not in names


# --- the Piggy-600 companion: the PS-77 simulator ---------------------------------

def test_sim_10_16_after_dusk_arm_late_cooler_takes_the_owed_darks(tmp_path, gate_on):
    """The 10-16 night: armed 20:45 (after dusk), roof closed all night, the
    cooler at the setpoint only after the first gate timed out. The first
    try skips, the loop gates again, the owed set is shot once (no double
    from the first-lights wait), and no light is taken. The deployed main
    (8f3b42e) shoots 0 darks here, as NINA #2 did."""
    seq = _companion(tmp_path)
    cold = ARM_2045 + 1800
    sim = DarkSim(seq, cold_at=cold, arm_at=ARM_2045,
                  unsafe=[(-7200, HORIZON)]).run()
    assert sim.skips >= 1
    assert sim.lights == []
    assert len(sim.done) == 60                   # 30 x 450 s + 30 x 120 s, once
    assert min(x[0] for x in sim.dark_log) >= cold
    assert sim.t >= AD                           # never wedged
    assert sim.spins() == {}, sim.spins()


def test_sim_dead_cooler_takes_no_darks_and_never_wedges(tmp_path, gate_on):
    sim = DarkSim(_companion(tmp_path), cold_at=10 ** 9, arm_at=ARM_2045,
                  unsafe=[(-7200, HORIZON)]).run()
    assert sim.dark_log == [] and sim.lights == []
    assert sim.skips >= 10                       # gated again all night
    assert sim.t >= AD
    assert sim.spins() == {}, sim.spins()


def test_sim_roof_closes_after_a_safe_evening_darks_before_first_lights(tmp_path,
                                                                       gate_on):
    """Safe at the night start (OSC_DARKS_IF_UNSAFE skipped), closed from
    before dusk to 23:01: the first-lights wait takes the darks (tonight's
    450 s first), stops within seconds of safe, and the lights follow."""
    opens = DUSK + 3 * 3600
    sim = DarkSim(_companion(tmp_path), cold_at=-10 ** 6,
                  unsafe=[(DUSK - 600, opens)]).run()
    assert sim.done and sim.dark_log[0][2] == 450.0
    assert min(x[0] for x in sim.dark_log) >= DUSK   # in the first-lights wait
    assert not [x for x in sim.dark_log if x[0] >= opens]
    assert max(x[1] for x in sim.dark_log) <= opens + 6
    assert sim.lights and min(x["start"] for x in sim.lights) >= opens
    assert sim.spins() == {}, sim.spins()


def test_sim_after_dusk_arm_safe_reacts_within_seconds(tmp_path, gate_on):
    opens = ARM_2045 + 3600 + 100            # mid-exposure
    sim = DarkSim(_companion(tmp_path), cold_at=-10 ** 6, arm_at=ARM_2045,
                  unsafe=[(-7200, opens)]).run()
    assert sim.done
    in_flight = [x for x in sim.dark_log if x[0] < opens < x[1] + 1]
    assert in_flight and not in_flight[-1][3]   # the dark in progress is cut
    assert max(x[1] for x in sim.dark_log) <= opens + 6
    assert sim.lights and min(x["start"] for x in sim.lights) >= opens
    # the first light starts after the resume hold and the AF, nothing else
    hold = cal._osc_resume_hold_seconds(rig_config(_cfg(tmp_path), PIGGYBACK))
    assert min(x["start"] for x in sim.lights) <= opens + 6 + hold + 30 + 60 + 60


def test_sim_flapping_roof_never_shoots_while_safe(tmp_path, gate_on):
    unsafe = [(-7200, DUSK + 1200), (DUSK + 1500, DUSK + 9000),
              (DUSK + 9100, DUSK + 20000)]
    sim = DarkSim(_companion(tmp_path), cold_at=-10 ** 6, unsafe=unsafe).run()
    assert sim.done
    for s, e, _, done in sim.dark_log:
        win = [(a, b) for a, b in unsafe if a <= s < b]
        assert win, (s, e)
        assert e <= win[0][1] + 6
    assert sim.spins() == {}, sim.spins()


# --- the RC16 -------------------------------------------------------------------

def _heart():
    t = NinaSequenceTarget(name="Heart Nebula", ra_hours=2.55, dec_degrees=61.5,
                           exposures=[ExposurePlan(filter_type=FilterType.HA,
                                                   exposure_seconds=300, count=40,
                                                   gain=200, offset=256)],
                           camera_temp_c=0.0)
    t.start_guiding = True
    return t


def _rc16(tmp_path, monkeypatch, owed=True):
    cfg = _cfg(tmp_path, dark_exposures="180")
    monkeypatch.setattr(nsj, "_gen_cfg", lambda: cfg)
    if not owed:
        monkeypatch.setattr(nsj, "_dark_quota_blocks", lambda *a, **k: [])
    return json.loads(nsj.generate_nina_json(
        build_sequence_for_night("CRD", [_heart()])))


def _start(seq):
    return next(n for n in _walk(seq) if "StartAreaContainer" in n.get("$type", ""))


def test_rc16_wait_until_safe_takes_the_owed_darks(tmp_path, monkeypatch, gate_on):
    seq = _rc16(tmp_path, monkeypatch)
    start = _start(seq)
    parent = next(n for n in _walk(start) if isinstance(n.get("Items"), dict)
                  and any(i.get("Name") == nsj.STARTUP_DARKS_WAIT_NAME
                          for i in _vals(n, "Items")))
    flat = _vals(parent, "Items")
    names = [i.get("Name") for i in flat]
    i = names.index(nsj.STARTUP_DARKS_WAIT_NAME)
    after = [x for x in flat[i + 1:] if _short(x["$type"]) != "Annotation"]
    assert _short(after[0]["$type"]) == "WaitUntilSafe"      # the last guard
    assert _short(after[2]["$type"]) == "UnparkScope"
    loop = flat[i]
    assert _conds(loop) == ["LoopWhileUnsafe"]               # no time: never unparks unsafe
    kids = _vals(loop, "Items")
    assert kids[0]["Name"] == "STARTUP_DARKS_IF_UNSAFE"
    assert _short(kids[1]["$type"]) == "WaitForTimeSpan"
    inner = _vals(kids[0], "Items")
    assert _short(inner[0]["$type"]) == "WaitForTime"        # astro dusk first
    assert _is_gate(inner[1]) and inner[1]["ErrorBehavior"] == 1
    assert inner[-1]["Name"] == cal.DARKS_DONE_HOLD_NAME
    assert 180.0 in [n["ExposureTime"] for n in _walk(kids[0])
                     if n.get("ImageType") == "DARK"]
    if "BIAS_IF_STILL_UNSAFE" in json.dumps(seq):
        assert _named(kids[0], "BIAS_IF_STILL_UNSAFE") or \
            "BIAS_IF_STILL_UNSAFE skipped" in json.dumps(start)
    # lint and the dispatch gate stay clean
    r = lint(seq)
    bad = [f for f in r.findings if f.level == "ERROR" and f.rule in (
        "loop-spin", "cooler-gate", "parent-links", "safety")]
    assert not bad, bad
    errs = [f.message for f in r.findings if f.level == "ERROR"]
    verdict = dispatch_gate.check(seq, guided=True, lint_errors=errs)
    assert verdict["ok"], verdict["errors"]


def test_rc16_nothing_owed_keeps_the_start_area_as_before(tmp_path, monkeypatch, gate_on):
    seq = _rc16(tmp_path, monkeypatch, owed=False)
    assert not _named(seq, nsj.STARTUP_DARKS_WAIT_NAME)
    sd = _named(seq, "STARTUP_DARKS_IF_UNSAFE")[0]
    assert _vals(sd, "Items") == []


def test_sim_rc16_closed_night_late_cooler_shoots_the_set_once(tmp_path, monkeypatch,
                                                               gate_on):
    seq = _rc16(tmp_path, monkeypatch)
    need = sum(c["Iterations"] for n in _walk(_named(seq, nsj.STARTUP_DARKS_WAIT_NAME)[0])
               for c in _vals(n, "Conditions") if _short(c["$type"]) == "LoopCondition"
               and str(n.get("Name", "")).startswith("DARKS_"))
    sim = DarkSim(seq, cold_at=DUSK + 1800, unsafe=[(-7200, HORIZON)]).run()
    assert sim.skips >= 1 and sim.lights == []
    assert len(sim.done) == need                 # the owed set, once
    assert min(x[0] for x in sim.dark_log) >= DUSK + 1800


def test_sim_rc16_opens_after_startup_darks_images(tmp_path, monkeypatch, gate_on):
    opens = DUSK + 2 * 3600 + 50
    sim = DarkSim(_rc16(tmp_path, monkeypatch), cold_at=-10 ** 6,
                  unsafe=[(-7200, opens)]).run()
    assert sim.done
    assert max(x[1] for x in sim.dark_log if x[0] < opens) <= opens + 6
    assert sim.lights and min(x["start"] for x in sim.lights) >= opens
