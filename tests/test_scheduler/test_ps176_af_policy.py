"""PS-176: RC16 shutter efficiency (2026-10-07: shutter open 5.4 h of 9.8 h).

Pinned here:
  1. rc16_af_policy every_block (default) keeps today's blocks; smart moves
     the focuser by the filter offset (model first, then config) with no AF,
     keeps AF at target start, on temperature / HFR / timed triggers and at
     blocks of 3 nm filters with no measured offset; both lint clean.
  2. AF trigger runners switch back to the block's filter (the 15 L subs
     inside the 10-07 R/G/B runs); lint rule af-runner-filter.
  3. HDR shorts (PS-47) are shot once per visit, never re-shot by the
     repeating imaging loop (225 of 281 subs on 10-07 were 30 s shorts).
  4. R/G/B default to rc16_rgb_exposure_s; a goal's own seconds win.
  5. The shutter estimate replays 10-07 and ranks the policies.
"""

import json

import pytest

from photonscript.scheduler import af_policy
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import shutter_efficiency as se
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.project_store import (allocate_exposures,
                                                  default_sub_seconds)
from photonscript.scheduler.sequence_lint import lint
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (ExposurePlan, FilterType as F,
                                        NinaSequenceTarget)

# The live RC16 model on 2026-10-08 (GET /api/focus): G and B have no AF of
# their own, OIII / SII one each.
LIVE_MODEL = {"ref_filter": "L", "offsets": {
    "Ha": {"steps": 118, "se": 6.0, "n": 60, "n_ref": 196},
    "OIII": {"steps": 130, "se": None, "n": 1, "n_ref": 196},
    "R": {"steps": -34, "se": 10.3, "n": 26, "n_ref": 196},
    "SII": {"steps": 136, "se": None, "n": 1, "n_ref": 196}}}


# --- helpers ---------------------------------------------------------------------

def _short(t: str) -> str:
    return t.split(",")[0].split(".")[-1]


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _vals(node, key):
    return [x for x in (node.get(key) or {}).get("$values", []) or []
            if isinstance(x, dict)]


def _exec(node):
    """Items in execution order, trigger runners excluded."""
    for it in _vals(node, "Items"):
        yield it
        yield from _exec(it)


def _plan(f, exp, n, hdr=None, hdr_n=0, acq_short=0):
    return ExposurePlan(filter_type=f, exposure_seconds=exp, count=n,
                        gain=200, offset=256, hdr_short_seconds=hdr,
                        hdr_short_count=hdr_n, hdr_short_acquired=acq_short)


def _m31(shorts=True, extra=()):
    """The 2026-10-07 M31 night's per-pass plan: L x3 @300, R/G/B x1 @300
    with 12 x 30 s HDR shorts owed."""
    hs = (30.0, 12) if shorts else (None, 0)
    t = NinaSequenceTarget(
        name="Andromeda Galaxy", ra_hours=0.712, dec_degrees=41.27,
        exposures=[_plan(F.LUMINANCE, 300, 3),
                   _plan(F.RED, 300, 1, *hs), _plan(F.GREEN, 300, 1, *hs),
                   _plan(F.BLUE, 300, 1, *hs), *extra])
    t.start_guiding = True
    return t


@pytest.fixture
def no_moon(monkeypatch):
    # a thin moon: broadband any time, no moonrise cap
    monkeypatch.setattr(nsj, "_moon_window", lambda: {
        "available": True, "down_at_dusk": False, "illum_pct": 5})


@pytest.fixture
def live_model(monkeypatch):
    monkeypatch.setattr(af_policy, "_model", lambda cfg: LIVE_MODEL)


def _gen(targets, monkeypatch, policy="every_block", **env):
    monkeypatch.setenv("PS_RC16_AF_POLICY", policy)
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    seq = build_sequence_for_night("PS176", targets)
    return json.loads(nsj.generate_nina_json(seq))


def _named(seq, suffix):
    return [d for d in _walk(seq) if str(d.get("Name", "")).endswith(suffix)]


def _imaging(seq):
    return _named(seq, nsj.TARGET_IMAGING_SUFFIX)[0]


def _dso(seq):
    return [d for d in _walk(seq)
            if "DeepSkyObjectContainer" in d.get("$type", "")][0]


def _smart_filter(se_):
    return next(it["Filter"]["_name"] for it in _vals(se_, "Items")
                if _short(it["$type"]) == "SwitchFilter")


def _exp_s(se_):
    return next(it["ExposureTime"] for it in _vals(se_, "Items")
                if _short(it["$type"]) == "TakeExposure")


def _smart_exposures(node):
    return [d for d in _exec(node) if _short(d.get("$type", "")) == "SmartExposure"]


def _afs(node):
    return [d for d in _exec(node) if _short(d.get("$type", "")) == "RunAutofocus"]


def _blocks_by_filter(seq):
    """{filter name: smart-policy block container} in the imaging loop."""
    out = {}
    for b in _named(_imaging(seq), nsj.TARGET_SMART_BLOCK_SUFFIX):
        out[_smart_filter(_smart_exposures(b)[0])] = b
    return out


NAME = {f: nsj._nina_filter_name(f) for f in F}


# --- 1. every_block (the default) ------------------------------------------------

def test_policy_parsing_defaults_to_every_block(monkeypatch):
    assert PhotonScriptConfig().rc16_af_policy == "every_block"
    for v, want in (("smart", "smart"), ("SMART", "smart"),
                    ("every-block", "every_block"), ("bogus", "every_block"),
                    ("", "every_block")):
        monkeypatch.setenv("PS_RC16_AF_POLICY", v)
        assert af_policy.policy(PhotonScriptConfig()) == want


def test_every_block_m31_af_at_every_block_and_lints(monkeypatch, no_moon):
    seq = _gen([_m31()], monkeypatch)
    loop = _imaging(seq)
    assert not _named(seq, nsj.TARGET_SMART_BLOCK_SUFFIX)
    # 4 long blocks in the loop, each AF on L first
    assert len(_smart_exposures(loop)) == 4
    assert len(_afs(loop)) == 4
    kinds = [_short(i["$type"]) for i in _vals(loop, "Items")]
    assert kinds[:2] == ["SwitchFilter", "RunAutofocus"]
    for s in _smart_exposures(loop):
        trig = {_short(t["$type"]) for t in _vals(s, "Triggers")}
        assert trig == {"DitherAfterExposures",
                        "AutofocusAfterTemperatureChangeTrigger",
                        "AutofocusAfterHFRIncreaseTrigger"}
    r = lint(seq, guided=True)
    assert r.ok, [f"{f.rule}: {f.detail}" for f in r.findings]


# --- 2. trigger runners switch back -------------------------------------------------

@pytest.mark.parametrize("policy", ["every_block", "smart"])
def test_af_trigger_runner_ends_on_the_blocks_filter(monkeypatch, no_moon,
                                                     live_model, policy):
    seq = _gen([_m31()], monkeypatch, policy)
    n = 0
    for s in [d for d in _walk(seq)
              if _short(d.get("$type", "")) == "SmartExposure"]:
        own = _smart_filter(s)
        for t in _vals(s, "Triggers"):
            if "Autofocus" not in t["$type"]:
                continue
            steps = _vals(t["TriggerRunner"], "Items")
            kinds = [_short(x["$type"]) for x in steps]
            assert kinds[:2] == ["SwitchFilter", "RunAutofocus"]
            assert steps[0]["Filter"]["_name"] == NAME[F.LUMINANCE]
            if own != NAME[F.LUMINANCE]:
                assert kinds[-1] == "SwitchFilter"
                assert steps[-1]["Filter"]["_name"] == own
                n += 1
    assert n > 0


def test_lint_flags_a_runner_that_stays_on_the_af_filter(monkeypatch, no_moon):
    seq = _gen([_m31()], monkeypatch)
    assert lint(seq, guided=True).ok
    for s in [d for d in _walk(seq)
              if _short(d.get("$type", "")) == "SmartExposure"]:
        for t in _vals(s, "Triggers"):
            items = t.get("TriggerRunner", {}).get("Items", {}).get("$values")
            if items and _short(items[-1]["$type"]) == "SwitchFilter" \
                    and len(items) > 2:
                items.pop()   # the pre-PS-176 runner
    r = lint(seq, guided=True)
    assert "af-runner-filter" in {f.rule for f in r.findings
                                  if f.level == "ERROR"}


def test_tracking_test_runner_switches_back_too():
    seq = json.loads(nsj.generate_tracking_test_json())
    assert not [f for f in lint(seq).findings if f.rule == "af-runner-filter"]


# --- 3. smart ---------------------------------------------------------------------

def test_offset_table_model_first_then_config(monkeypatch):
    cfg = PhotonScriptConfig(focus_filter_offsets="Ha:120,OIII:120,SII:120")
    tab = af_policy.offset_table(cfg, "L", LIVE_MODEL)
    assert tab["L"] == {"steps": 0, "measured": True, "source": "reference",
                        "n": None, "n_ref": None}
    assert (tab["R"]["steps"], tab["R"]["measured"], tab["R"]["source"]) == \
        (-34, True, "model")
    assert (tab["Ha"]["steps"], tab["Ha"]["source"]) == (118, "model")
    # one AF is not a measurement: the configured offset stands
    assert (tab["OIII"]["steps"], tab["OIII"]["measured"],
            tab["OIII"]["source"]) == (120, False, "config")
    assert (tab["G"]["steps"], tab["G"]["source"]) == (0, "none")
    # a model fitted on another reference filter is not used
    tab2 = af_policy.offset_table(cfg, "R", LIVE_MODEL)
    assert not any(r["source"] == "model" for r in tab2.values())


def test_smart_spec_needs_an_af_filter_and_the_policy(monkeypatch):
    monkeypatch.setenv("PS_RC16_AF_POLICY", "smart")
    cfg = PhotonScriptConfig()
    assert af_policy.smart_spec(cfg, None, LIVE_MODEL) is None
    spec = af_policy.smart_spec(cfg, F.LUMINANCE, LIVE_MODEL)
    assert spec["af_blocks"] == {"OIII", "SII"}       # unmeasured 3 nm only
    assert spec["interval_min"] == 60.0 and spec["temp_c"] == 2.0
    monkeypatch.setenv("PS_RC16_AF_POLICY", "every_block")
    cfg = PhotonScriptConfig()
    assert af_policy.smart_spec(cfg, F.LUMINANCE, LIVE_MODEL) is None
    assert af_policy.smart_spec(cfg, F.LUMINANCE, LIVE_MODEL, force=True)


def test_smart_m31_moves_by_offset_with_no_block_af(monkeypatch, no_moon,
                                                    live_model):
    oiii = _plan(F.OIII, 600, 2)
    ha = _plan(F.HA, 600, 2)
    seq = _gen([_m31(extra=(ha, oiii))], monkeypatch, "smart")
    dso = _dso(seq)
    # target start: seed + AF on L before centering (unchanged)
    top = [_short(i["$type"]) for i in _vals(dso, "Items")]
    assert top.index("RunAutofocus") < top.index("Center")
    note = [i["Text"] for i in _vals(dso, "Items")
            if _short(i["$type"]) == "Annotation"]
    assert any("AF policy smart" in t and "R -34 (model" in t for t in note)
    blocks = _blocks_by_filter(seq)
    assert set(blocks) == {NAME[f] for f in
                           (F.LUMINANCE, F.RED, F.GREEN, F.BLUE, F.HA, F.OIII)}

    def moves(b):
        return [d["RelativePosition"] for d in _exec(b)
                if _short(d["$type"]) == "MoveFocuserRelative"]
    # L: reference, nothing to move; G/B: unmeasured broadband, 0
    for f in (F.LUMINANCE, F.GREEN, F.BLUE):
        assert not _afs(blocks[NAME[f]]) and moves(blocks[NAME[f]]) == []
    # R and Ha: measured, model offset before the lights and back after
    for f, off in ((F.RED, -34), (F.HA, 118)):
        b = blocks[NAME[f]]
        assert not _afs(b)
        kinds = [_short(i["$type"]) for i in _vals(b, "Items")]
        assert kinds[0] == "SwitchFilter"
        assert _vals(b, "Items")[0]["Filter"]["_name"] == NAME[f]
        k = kinds.index("SmartExposure")
        assert kinds[k - 1] == "MoveFocuserRelative"
        assert moves(b) == [off, -off]
    # OIII: one AF, so not measured: AF on L at every block, config +120
    b = blocks[NAME[F.OIII]]
    kinds = [_short(i["$type"]) for i in _vals(b, "Items")]
    assert kinds[:2] == ["SwitchFilter", "RunAutofocus"]
    assert _vals(b, "Items")[0]["Filter"]["_name"] == NAME[F.LUMINANCE]
    assert moves(b) == [120, -120]
    # triggers: temperature, HFR and the timed refocus, AF recipe + back
    for s in _smart_exposures(_imaging(seq)):
        trig = {_short(t["$type"]): t for t in _vals(s, "Triggers")}
        assert set(trig) == {"DitherAfterExposures",
                             "AutofocusAfterTemperatureChangeTrigger",
                             "AutofocusAfterHFRIncreaseTrigger",
                             "AutofocusAfterTimeTrigger"}
        assert trig["AutofocusAfterTemperatureChangeTrigger"]["Amount"] == 2.0
        assert trig["AutofocusAfterHFRIncreaseTrigger"]["Amount"] == 10.0
        assert trig["AutofocusAfterTimeTrigger"]["Amount"] == 60.0
    # the only AF left in the loop is OIII's (it paces the loop, PS-149)
    loop = _imaging(seq)
    assert len(_afs(loop)) == 1
    r = lint(seq, guided=True)
    assert r.ok, [f"{f.rule}: {f.detail}" for f in r.findings]
    assert not [f for f in r.findings if f.rule in ("loop-spin", "focus-seed",
                                                    "focus-offset", "cooling")]


def test_smart_trigger_thresholds_come_from_config(monkeypatch, no_moon,
                                                   live_model):
    seq = _gen([_m31(shorts=False)], monkeypatch, "smart",
               PS_RC16_AF_TEMP_CHANGE_C=1.5, PS_RC16_AF_HFR_INCREASE_PCT=15,
               PS_RC16_AF_INTERVAL_MIN=0)
    for s in _smart_exposures(_imaging(seq)):
        trig = {_short(t["$type"]): t for t in _vals(s, "Triggers")}
        assert "AutofocusAfterTimeTrigger" not in trig      # 0 = off
        assert trig["AutofocusAfterTemperatureChangeTrigger"]["Amount"] == 1.5
        assert trig["AutofocusAfterHFRIncreaseTrigger"]["Amount"] == 15.0


def test_smart_af_count_vs_every_block(monkeypatch, no_moon, live_model):
    """The 10-07 M31 plan: every_block AFs at all 4 loop blocks per pass;
    smart only at the target start."""
    eb = _gen([_m31(shorts=False)], monkeypatch, "every_block")
    sm = _gen([_m31(shorts=False)], monkeypatch, "smart")
    assert len(_afs(_dso(eb))) == 1 + 4
    assert len(_afs(_dso(sm))) == 1
    # no AF in the smart loop: the PS-149 pace wait ends each pass
    loop = _imaging(sm)
    assert _short(_vals(loop, "Items")[-1]["$type"]) == "WaitForTimeSpan"
    assert _vals(loop, "Items")[-1]["Time"] == nsj.TARGET_IMAGING_PACE_S
    assert lint(sm, guided=True).ok


def test_smart_with_cooler_gate_and_per_block_guiding(monkeypatch, no_moon,
                                                      live_model, tmp_path):
    script = tmp_path / "cooler-gate.cmd"
    script.write_text("@echo off\n")
    monkeypatch.setenv("PS_COOLER_GATE_MODE", "skip")
    monkeypatch.setenv("PS_COOLER_GATE_SCRIPT", str(script))
    t = _m31(shorts=False)
    t.unguided_filters = ["B"]
    seq = _gen([t], monkeypatch, "smart")
    blocks = _blocks_by_filter(seq)
    for name, b in blocks.items():
        items = _vals(b, "Items")
        assert "cooler-gate" in items[0].get("Script", "")   # gate first
        kinds = [_short(i["$type"]) for i in items]
        if name == NAME[F.RED]:
            # guiding starts before the offset move, the move right before
            # the lights
            assert kinds.index("StartGuiding") < kinds.index(
                "MoveFocuserRelative") < kinds.index("SmartExposure")
        if name == NAME[F.BLUE]:
            assert "StopGuiding" in kinds
    r = lint(seq, guided=True)
    assert r.ok, [f"{f.rule}: {f.detail}" for f in r.findings]


def test_lint_flags_an_unbalanced_offset_block(monkeypatch, no_moon,
                                               live_model):
    seq = _gen([_m31(shorts=False)], monkeypatch, "smart")
    b = _blocks_by_filter(seq)[NAME[F.RED]]
    items = b["Items"]["$values"]
    last = [i for i in items if _short(i["$type"]) == "MoveFocuserRelative"][-1]
    items.remove(last)
    r = lint(seq, guided=True)
    assert "focus-offset" in {f.rule for f in r.findings if f.level == "ERROR"}


def test_smart_falls_back_without_af_filter(monkeypatch, no_moon, live_model):
    seq = _gen([_m31(shorts=False)], monkeypatch, "smart",
               PS_AUTOFOCUS_FILTER="")
    assert not _named(seq, nsj.TARGET_SMART_BLOCK_SUFFIX)
    assert len(_afs(_imaging(seq))) == 4


def test_focus_drive_wins_over_smart(monkeypatch, no_moon, live_model):
    monkeypatch.setattr(nsj, "_focus_drive_spec", lambda cfg: {
        "script": "C:\\x\\focus-model-move.cmd", "filters": {"L", "R"},
        "verify_min": 120.0})
    seq = _gen([_m31(shorts=False)], monkeypatch, "smart")
    assert not _named(seq, nsj.TARGET_SMART_BLOCK_SUFFIX)


# --- 4. HDR shorts once per visit ----------------------------------------------------

@pytest.mark.parametrize("policy", ["every_block", "smart"])
def test_hdr_shorts_once_per_visit_not_in_the_loop(monkeypatch, no_moon,
                                                   live_model, policy):
    seq = _gen([_m31()], monkeypatch, policy)
    dso = _dso(seq)
    names = [i.get("Name", "") for i in _vals(dso, "Items")]
    hdr = [n for n in names if n.endswith(nsj.TARGET_HDR_SHORTS_SUFFIX)]
    loop = [n for n in names if n.endswith(nsj.TARGET_IMAGING_SUFFIX)]
    assert len(hdr) == 1 and len(loop) == 1
    assert names.index(hdr[0]) < names.index(loop[0])
    shorts = _named(seq, nsj.TARGET_HDR_SHORTS_SUFFIX)[0]
    assert not shorts.get("Conditions", {}).get("$values")   # runs once
    got = sorted((_smart_filter(s), _exp_s(s),
                  _vals(s, "Conditions")[0]["Iterations"])
                 for s in _smart_exposures(shorts))
    assert got == sorted((NAME[f], 30.0, 12) for f in (F.RED, F.GREEN, F.BLUE))
    assert {_exp_s(s) for s in _smart_exposures(_imaging(seq))} == {300.0}
    r = lint(seq, guided=True)
    assert r.ok, [f"{f.rule}: {f.detail}" for f in r.findings]


def test_shorts_only_plan_has_no_repeating_loop(monkeypatch, no_moon):
    t = NinaSequenceTarget(name="M42", ra_hours=5.59, dec_degrees=-5.39,
                           start_guiding=False,
                           exposures=[_plan(F.LUMINANCE, 300, 0, 10.0, 12)])
    seq = _gen([t], monkeypatch)
    assert _named(seq, nsj.TARGET_HDR_SHORTS_SUFFIX)
    assert not _named(seq, nsj.TARGET_IMAGING_SUFFIX)
    r = lint(seq)
    assert r.ok, [f"{f.rule}: {f.detail}" for f in r.findings]


def test_hdr_shorts_respect_moonrise(monkeypatch):
    monkeypatch.setattr(nsj, "_moon_window", lambda: {
        "available": True, "down_at_dusk": True, "illum_pct": 60,
        "rise_local_hh": 1, "rise_local_mm": 30})
    seq = _gen([_m31()], monkeypatch)
    shorts = _named(seq, nsj.TARGET_HDR_SHORTS_SUFFIX)[0]
    wraps = [i for i in _vals(shorts, "Items")]
    assert wraps and all(w["Name"].endswith(nsj.FILTER_UNTIL_MOONRISE_SUFFIX)
                         for w in wraps)
    assert lint(seq, guided=True).ok


def test_new_container_names_map_back_to_the_target():
    from photonscript.shared.target_names import strip_container_name
    for suffix in (nsj.TARGET_SMART_BLOCK_SUFFIX, nsj.TARGET_HDR_SHORTS_SUFFIX):
        assert strip_container_name("Andromeda Galaxy" + suffix) == \
            "Andromeda Galaxy"


# --- 5. RGB sub length -------------------------------------------------------------

def test_rgb_default_sub_length(tmp_path):
    cfg = PhotonScriptConfig(data_dir=str(tmp_path))
    assert cfg.rc16_rgb_exposure_s == 120.0
    assert [default_sub_seconds(f, cfg) for f in ("L", "R", "G", "B", "Ha")] \
        == [180.0, 120.0, 120.0, 120.0, 600.0]
    by = {p.filter_type.value: p for p in allocate_exposures("broadband", 6.0, cfg)}
    assert by["L"].exposure_seconds == 180 and by["R"].exposure_seconds == 120
    # a goal's own seconds win (M31: 300 s on every filter)
    by = {p.filter_type.value: p for p in allocate_exposures(
        "broadband", 8.0, cfg, custom_mix={"L": 50, "R": 17, "G": 17, "B": 17},
        hdr={"R": 30.0}, overrides={"L": 300, "R": 300, "G": 300, "B": 300})}
    assert {f: p.exposure_seconds for f, p in by.items()} == \
        {"L": 300, "R": 300, "G": 300, "B": 300}
    assert by["R"].hdr_short_seconds == 30.0 and by["R"].hdr_short_count == 12
    # 0 = the old rule, bb_exposure_s for RGB too
    cfg0 = PhotonScriptConfig(data_dir=str(tmp_path), rc16_rgb_exposure_s=0)
    assert default_sub_seconds("G", cfg0) == 180.0


def test_catalog_creation_defaults_use_the_rgb_length(tmp_path):
    from photonscript.scheduler.catalog import creation_defaults
    cfg = PhotonScriptConfig(data_dir=str(tmp_path))
    d = creation_defaults({"name": "M 33", "ra": 1.56, "dec": 30.66,
                           "type": "galaxy", "size": 70}, cfg)
    assert d["sub_seconds"] == {"L": 180.0, "R": 120.0, "G": 120.0, "B": 120.0}


# --- 6. shutter estimate ------------------------------------------------------------

OH_1007 = {"af_s": 250.3, "per_sub_s": 10.9, "filter_change_s": 4.0}
NIGHT_S = 9.77 * 3600


def test_estimate_replays_2026_10_07():
    """The pre-PS-176 shape (shorts in every pass, AF at every block) over
    the 10-07 dark time: about 30 AF runs (28 seen as Run Autofocus) and
    about 65% shutter before the ~15 triggered AFs NINA hid inside the
    Smart Exposures (another ~62 min: 55% measured)."""
    r = se.target_estimate(_m31(), NIGHT_S, "every_block", OH_1007,
                           shorts_once=False)
    assert 27 <= r["af_count"] <= 33
    assert 60 <= r["pct"] <= 68


def test_estimate_ranks_smart_over_every_block_for_tonights_m31():
    """2026-10-08 night plan: M31 L x2, R/G/B x1 @300 s, no shorts owed."""
    t = NinaSequenceTarget(name="M31", ra_hours=0.712, dec_degrees=41.27,
                           exposures=[_plan(F.LUMINANCE, 300, 2),
                                      _plan(F.RED, 300, 1),
                                      _plan(F.GREEN, 300, 1),
                                      _plan(F.BLUE, 300, 1)])
    spec = {"ref": "L", "af_blocks": {"OIII", "SII"}, "interval_min": 60}
    eb = se.target_estimate(t, NIGHT_S, "every_block", OH_1007, spec)
    sm = se.target_estimate(t, NIGHT_S, "smart", OH_1007, spec)
    assert eb["pct"] < 65 and sm["pct"] > 80
    assert sm["af_count"] <= 12 < eb["af_count"]
    # unmeasured 3 nm filters keep their AF in smart too
    nb = NinaSequenceTarget(name="Heart", ra_hours=2.55, dec_degrees=61.5,
                            exposures=[_plan(F.OIII, 600, 3)])
    a = se.target_estimate(nb, 3600 * 3, "smart", OH_1007, spec)
    b = se.target_estimate(nb, 3600 * 3, "every_block", OH_1007, spec)
    assert a["af_count"] >= b["af_count"]


def test_estimate_both_policies_and_overheads_from_a_timeline():
    def sub(s, e, f):
        return {"start": s, "end": e, "filter": f}
    tl = {"rows": [{"id": "rc16", "segments": [
        {"start": "2026-10-08T03:00:00Z", "end": "2026-10-08T03:04:00Z",
         "state": "autofocus", "label": "Run Autofocus"}]}],
        "subs": {"rc16": [
            sub("2026-10-08T02:50:00Z", "2026-10-08T02:55:00Z", "L"),
            sub("2026-10-08T02:55:10Z", "2026-10-08T03:00:00Z", "L"),
            sub("2026-10-08T03:04:20Z", "2026-10-08T03:09:20Z", "R"),
            sub("2026-10-08T03:09:24Z", "2026-10-08T03:14:24Z", "G"),
            sub("2026-10-08T03:14:36Z", "2026-10-08T03:19:36Z", "G")]}}
    o = se.night_overheads(tl)
    assert o["af_s"] == [240.0]
    assert o["same"] == [10.0, 12.0]      # L->L, G->G
    assert o["change"] == [4.0]           # R->G; L->R held the AF
    out = se.estimate([(_m31(shorts=False), 3600.0)], 3600.0,
                      {"af_s": 240.0, "per_sub_s": 11.0,
                       "filter_change_s": 4.0},
                      {"ref": "L", "af_blocks": set(), "interval_min": 60},
                      "every_block")
    assert set(out["policies"]) == {"every_block", "smart"}
    assert out["policies"]["smart"]["pct"] > out["policies"]["every_block"]["pct"]


def test_measured_overheads_fall_back_to_defaults(tmp_path):
    cfg = PhotonScriptConfig(data_dir=str(tmp_path))
    oh = se.measured_overheads(cfg)
    assert oh["source"].startswith("defaults")
    assert oh["af_s"] == se.DEFAULT_AF_S and oh["nights"] == []


def test_night_plan_carries_the_estimate(monkeypatch, tmp_path):
    from photonscript.scheduler import night_plan
    monkeypatch.setenv("PS_DATA_DIR", str(tmp_path))
    cfg = PhotonScriptConfig()
    from datetime import datetime, timedelta
    dusk = datetime(2026, 10, 9, 2, 10)
    dawn = dusk + timedelta(hours=9.77)
    t = _m31(shorts=False)
    out = night_plan._shutter_estimate(cfg, [[t, 1800.0]], dusk, dawn,
                                       dusk + timedelta(seconds=1800))
    assert out["active_policy"] == "every_block"
    assert out["policies"]["every_block"]["targets"][0]["window_h"] == 9.77
    assert out["policies"]["smart"]["pct"] >= out["policies"]["every_block"]["pct"]
