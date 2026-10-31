"""AF on HFR rise: NINA's "Autofocus after HFR increase" trigger
(AutofocusAfterHFRIncreaseTrigger, Amount % over SampleSize subs) on every
RC16 light block, any AF policy, and on the Piggy-600 OSC light loop, from
per-rig config (rc16_ / piggyback_af_hfr_increase_pct 10,
rc16_ / piggyback_af_hfr_sample_size 4, floors 5 % and 3 subs).

It must not fight the re-entry gates (2026-10-15): the trigger lives only
on the light SmartExposures (never in a gate container, never on the
target container where it could fire before the start AF), and its sample
floor keeps it from refocusing a couple of subs after a fresh AF or after
the AF a gate SKIP relied on. Lint rule af-hfr enforces the floors, and the
dispatch gate refuses a night that breaks them."""
import json

import pytest

from photonscript.scheduler import af_policy, dispatch_gate
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.sequence_lint import lint
from photonscript.scheduler.sideload import lint_companion
from photonscript.shared import rigs
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import FilterType
from photonscript.scheduler.calibration import generate_piggyback_companion_json
from tests.test_scheduler.test_ps77_safety_stop import _walk
from tests.test_scheduler.test_reentry import _gated, _ngc604

HFR_TYPE = ("NINA.Sequencer.Trigger.Autofocus.AutofocusAfterHFRIncreaseTrigger, "
            "NINA.Sequencer")


@pytest.fixture
def no_moon(monkeypatch):
    monkeypatch.setattr(nsj, "_moon_window", lambda: {
        "available": True, "down_at_dusk": False, "illum_pct": 5})


@pytest.fixture
def script(tmp_path, monkeypatch):
    p = tmp_path / "reentry-gate.cmd"
    p.write_text("@echo off\n", encoding="ascii")
    monkeypatch.setenv("PS_REENTRY_GATE_SCRIPT", str(p))
    return p


def _gen(monkeypatch, targets=None, **env):
    env.setdefault("PS_REENTRY_OPTIMIZE", "off")
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    seq = build_sequence_for_night("HFR", targets or [_ngc604()])
    return json.loads(nsj.generate_nina_json(seq))


def _hfr_triggers(node):
    return [d for d in _walk(node) if d.get("$type") == HFR_TYPE]


def _smart_exposures(node):
    return [d for d in _walk(node)
            if str(d.get("$type", "")).startswith(
                "NINA.Sequencer.SequenceItem.Imaging.SmartExposure")]


def _short(t):
    return t.split(",")[0].split(".")[-1]


# --- config -------------------------------------------------------------------

def test_config_defaults_per_rig():
    c = PhotonScriptConfig()
    assert c.rc16_af_hfr_increase_pct == 10.0
    assert c.rc16_af_hfr_sample_size == 4
    assert c.piggyback_af_hfr_increase_pct == 10.0
    assert c.piggyback_af_hfr_sample_size == 4
    for rig in ("rc16", "piggyback"):
        assert af_policy.hfr_trigger(c, rig) == {"pct": 10.0, "samples": 4}


def test_hfr_trigger_reads_each_rig_and_clamps():
    c = PhotonScriptConfig(rc16_af_hfr_increase_pct=20,
                           rc16_af_hfr_sample_size=6,
                           piggyback_af_hfr_increase_pct=12,
                           piggyback_af_hfr_sample_size=8)
    assert af_policy.hfr_trigger(c, "rc16") == {"pct": 20.0, "samples": 6}
    assert af_policy.hfr_trigger(c, "piggyback") == {"pct": 12.0, "samples": 8}
    low = PhotonScriptConfig(rc16_af_hfr_increase_pct=1,
                             rc16_af_hfr_sample_size=1)
    assert af_policy.hfr_trigger(low, "rc16") == {
        "pct": af_policy.HFR_MIN_PCT, "samples": af_policy.HFR_MIN_SAMPLES}
    high = PhotonScriptConfig(rc16_af_hfr_increase_pct=500,
                              rc16_af_hfr_sample_size=999)
    assert af_policy.hfr_trigger(high, "rc16") == {
        "pct": af_policy.HFR_MAX_PCT, "samples": af_policy.HFR_MAX_SAMPLES}

    class Junk:
        rc16_af_hfr_increase_pct = "x"
        rc16_af_hfr_sample_size = None
    assert af_policy.hfr_trigger(Junk(), "rc16") == {"pct": 10.0, "samples": 4}


# --- RC16 generator -----------------------------------------------------------

@pytest.mark.parametrize("policy", ["every_block", "smart"])
def test_every_rc16_light_block_carries_the_trigger(monkeypatch, no_moon,
                                                    policy):
    monkeypatch.setattr(af_policy, "_model", lambda cfg: None)
    seq = _gen(monkeypatch, PS_RC16_AF_POLICY=policy)
    smarts = _smart_exposures(seq)
    assert smarts
    for s in smarts:
        trig = [t for t in s["Triggers"]["$values"] if t["$type"] == HFR_TYPE]
        assert len(trig) == 1, [_short(t["$type"])
                                for t in s["Triggers"]["$values"]]
        t = trig[0]
        assert (t["Amount"], t["SampleSize"]) == (10.0, 4)
        runner = [_short(i["$type"])
                  for i in t["TriggerRunner"]["Items"]["$values"]]
        assert "RunAutofocus" in runner
    # nowhere else: not on the target / imaging containers (it could fire
    # before the start AF), not in the Start or End areas
    assert len(_hfr_triggers(seq)) == len(smarts)
    r = lint(seq, guided=False)
    assert not [f for f in r.findings if f.rule == "af-hfr"], r.findings


def test_rc16_trigger_follows_config_under_every_block(monkeypatch, no_moon):
    """Before this change only the smart policy read the percent; every_block
    hard-coded 10 % / 4 subs."""
    seq = _gen(monkeypatch, PS_RC16_AF_POLICY="every_block",
               PS_RC16_AF_HFR_INCREASE_PCT=20, PS_RC16_AF_HFR_SAMPLE_SIZE=6)
    trig = _hfr_triggers(seq)
    assert trig and all((t["Amount"], t["SampleSize"]) == (20.0, 6)
                        for t in trig)


def test_rc16_config_below_the_floor_is_clamped(monkeypatch, no_moon):
    seq = _gen(monkeypatch, PS_RC16_AF_HFR_INCREASE_PCT=2,
               PS_RC16_AF_HFR_SAMPLE_SIZE=1)
    trig = _hfr_triggers(seq)
    assert trig and all((t["Amount"], t["SampleSize"]) == (5.0, 3)
                        for t in trig)
    assert not [f for f in lint(seq, guided=False).findings
                if f.rule == "af-hfr"]


def test_smart_annotation_names_the_sample_size(monkeypatch):
    spec = af_policy.smart_spec(PhotonScriptConfig(rc16_af_policy="smart"),
                                FilterType.LUMINANCE, model={})
    assert (spec["hfr_pct"], spec["hfr_n"]) == (10.0, 4)
    assert "HFR rise of 10% over 4 subs" in af_policy.describe(spec)


# --- the re-entry gates -------------------------------------------------------

@pytest.mark.parametrize("mode", ["af", "on"])
def test_trigger_stays_out_of_the_reentry_gates(monkeypatch, no_moon, script,
                                                mode):
    seq = _gen(monkeypatch, PS_REENTRY_OPTIMIZE=mode)
    gated = _gated(seq)
    assert "NGC 604 start AF (re-entry gate)" in gated
    for c in gated.values():
        assert not _hfr_triggers(c)
        assert not c.get("Triggers", {}).get("$values")
    # the gate decides the start AF; the trigger only watches the lights
    # after it, on the SmartExposure, with a sample of 4 subs
    smarts = _smart_exposures(seq)
    assert len(_hfr_triggers(seq)) == len(smarts) >= 1
    assert all(t["SampleSize"] >= af_policy.HFR_MIN_SAMPLES
               for t in _hfr_triggers(seq))
    r = lint(seq, guided=False)
    assert not [f for f in r.findings if f.level == "ERROR"], r.findings
    assert dispatch_gate.check(seq, guided=False)["ok"]


# --- Piggy-600 ----------------------------------------------------------------

def _companion(**kw):
    cfg = rigs.rig_config(PhotonScriptConfig(
        piggyback_enabled=True, piggyback_image_lights=True,
        piggyback_focus_seed=0, **kw), "piggyback")
    return json.loads(generate_piggyback_companion_json(
        cfg, has_safety=True, with_lights=True))


def test_piggy_light_loop_trigger_defaults_and_config():
    loop = next(n for n in _walk(_companion())
                if n.get("Name") == "OSC_LIGHT_LOOP")
    t = [x for x in loop["Triggers"]["$values"] if x["$type"] == HFR_TYPE]
    assert len(t) == 1 and (t[0]["Amount"], t[0]["SampleSize"]) == (10.0, 4)
    trig = _hfr_triggers(_companion(piggyback_af_hfr_increase_pct=25,
                                    piggyback_af_hfr_sample_size=1))
    assert [(x["Amount"], x["SampleSize"]) for x in trig] == [(25.0, 3)]
    assert not [f for f in lint_companion(_companion()).findings
                if f.rule == "af-hfr"]


# --- lint rule af-hfr + the dispatch gate -------------------------------------

def _tamper(seq, **fields):
    for t in _hfr_triggers(seq):
        t.update(fields)
    return seq


@pytest.mark.parametrize("fields", [{"SampleSize": 2}, {"Amount": 3.0},
                                    {"SampleSize": "x"}])
def test_lint_refuses_a_trigger_below_the_floor(monkeypatch, no_moon, fields):
    seq = _tamper(_gen(monkeypatch), **fields)
    errs = [f for f in lint(seq, guided=False).findings if f.rule == "af-hfr"]
    assert errs and errs[0].level == "ERROR"
    assert "refocus on noise" in errs[0].detail
    lint_errors = [f.detail for f in lint(seq, guided=False).findings
                   if f.level == "ERROR"]
    v = dispatch_gate.check(seq, guided=False, lint_errors=lint_errors)
    assert not v["ok"]
    # a hand-built sequence only warns
    hb = [f for f in lint(seq, guided=False, hand_built=True).findings
          if f.rule == "af-hfr"]
    assert hb and hb[0].level == "WARN"


def test_lint_skips_a_trigger_without_fields():
    """A ninaAPI tree reports triggers without Amount / SampleSize."""
    seq = {"$type": "NINA.Sequencer.Container.SequenceRootContainer, "
                    "NINA.Sequencer",
           "Triggers": {"$values": [{"$type": HFR_TYPE}]}}
    assert not [f for f in lint(seq, guided=False).findings
                if f.rule == "af-hfr"]


def test_companion_lint_checks_the_floor_too():
    seq = _tamper(_companion(), SampleSize=1)
    errs = [f for f in lint_companion(seq).findings if f.rule == "af-hfr"]
    assert errs and errs[0].level == "ERROR"
    hb = [f for f in lint_companion(seq, hand_built=True).findings
          if f.rule == "af-hfr"]
    assert hb and hb[0].level == "WARN"
