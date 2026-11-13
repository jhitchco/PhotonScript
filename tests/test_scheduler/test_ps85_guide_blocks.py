"""PS-85 guided + ProTrack, per filter block (scheduler side).

The decision table, the unguided sub length per filter (the 2026-10-05
tracking test split at ProTrack-on 04:12Z, else the config), history at the
same guide setup, applying decisions to the planner's targets, the sequence
shape of a guided target with unguided blocks (StopGuiding, capped subs, no
active dither; guided blocks start guiding themselves), the armer's dispatch
and block_unguided (observe vs auto, one push per target, re-dispatch cap),
the NB tuner band and binning advice, the API, the Guiding tab and config."""
import json

import pytest

from photonscript.scheduler import armer as armer_mod
from photonscript.scheduler import guide_blocks as gb
from photonscript.scheduler.armer import Armer
from photonscript.scheduler.nina_sequence_json import (
    TARGET_GUIDE_BLOCK_SUFFIX, generate_nina_json)
from photonscript.scheduler.sequence_lint import _exec_items, _find_type, lint
from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (
    ExposurePlan, FilterType, NinaSequenceFile, NinaSequenceTarget)

NIGHT = "2026-10-06"


def _cfg(tmp_path, **kw):
    kw.setdefault("data_dir", tmp_path / "data")
    kw.setdefault("guide_fallback_test_date", "")   # no runs log in tests
    return PhotonScriptConfig(_env_file=None, **kw)


def _plan(f=FilterType.HA, exp=600, count=10, acquired=0):
    return ExposurePlan(filter_type=f, exposure_seconds=exp, count=count,
                        acquired=acquired, gain=200, offset=256)


def _heart(guided=True, unguided=(), exposures=None):
    return NinaSequenceTarget(
        name="Heart Nebula", ra_hours=2.55, dec_degrees=61.5, start_guiding=guided,
        unguided_filters=list(unguided),
        exposures=exposures or [_plan(), _plan(FilterType.OIII)])


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    sent = []

    async def _notify(cfg, msg, **k):
        sent.append(msg)
        return True
    monkeypatch.setattr("photonscript.shared.pushover.notify", _notify)
    monkeypatch.setattr("photonscript.shared.pushover.record", lambda *a, **k: None)
    gb._report_cache.clear()
    return sent


# ---- sub length per filter ---------------------------------------------------

# The 2026-10-05 NGC 6934 report (GET /api/tracking-test/report?date=2026-10-05)
REPORT_1005 = {"groups": [
    {"rig": "rc16", "filter": "L", "exp_s": 60.0, "status": "pass", "ecc_median": 0.435,
     "ecc_gate": 0.6, "time_first": "2026-10-06T03:53:46.586273Z"},
    {"rig": "rc16", "filter": "L", "exp_s": 120.0, "status": "fail", "ecc_median": 0.605,
     "ecc_gate": 0.6, "time_first": "2026-10-06T03:56:50.677283Z"},
    {"rig": "rc16", "filter": "L", "exp_s": 180.0, "status": "fail", "ecc_median": 0.652,
     "ecc_gate": 0.6, "time_first": "2026-10-06T04:01:50.982625Z"},
    {"rig": "rc16", "filter": "L", "exp_s": 300.0, "status": "fail", "ecc_median": 0.788,
     "ecc_gate": 0.6, "time_first": "2026-10-06T04:16:56.660645Z"},
] + [{"rig": "rc16", "filter": "Ha", "exp_s": e, "status": "fail", "ecc_median": m,
      "ecc_gate": 0.6, "time_first": t}
     for e, m, t in ((60.0, 0.714, "2026-10-06T04:27:42Z"), (120.0, 0.734, "2026-10-06T04:30:48Z"),
                     (180.0, 0.722, "2026-10-06T04:35:46Z"), (300.0, 0.682, "2026-10-06T04:43:47Z"))]}


def test_parse_lengths_and_config_defaults(tmp_path):
    assert gb.parse_lengths("L:60, Ha:300,junk,OIII:x,SII:0") == {"L": 60.0, "Ha": 300.0}
    cfg = _cfg(tmp_path)
    assert gb.fallback_exposure_s(cfg, "L", {}) == (60.0, "config")
    assert gb.fallback_exposure_s(cfg, "Ha", {}) == (300.0, "config")
    assert gb.fallback_exposure_s(cfg, "Lum?", {}) == (300.0, "unguided cap")
    capped = _cfg(tmp_path, unguided_max_exposure_s=200)
    assert gb.fallback_exposure_s(capped, "Ha", {}) == (200.0, "config, capped")
    none = _cfg(tmp_path, unguided_max_exposure_s=0, guide_fallback_exposure_s="")
    assert gb.fallback_exposure_s(none, "Ha", {}) == (None, "none")


def test_proven_lengths_split_at_protrack_on(tmp_path):
    cfg = _cfg(tmp_path, guide_fallback_test_date="2026-10-05")
    # after 04:12Z (ProTrack on) nothing passed: config lengths apply
    assert gb.proven_lengths(cfg, REPORT_1005) == {}
    assert gb.fallback_exposure_s(cfg, "L", gb.proven_lengths(cfg, REPORT_1005))[0] == 60.0
    # without the split, L 60 s is the proven pass
    nosplit = _cfg(tmp_path, guide_fallback_test_date="2026-10-05",
                   guide_fallback_test_since_utc="")
    assert gb.proven_lengths(nosplit, REPORT_1005) == {"L": 60.0}
    # a later ladder that passes Ha 180 s overrides the config 300 s
    rep = {"groups": [dict(g, status="pass") if (g["filter"], g["exp_s"]) in
                      (("Ha", 60.0), ("Ha", 120.0), ("Ha", 180.0)) else g
                      for g in REPORT_1005["groups"]]}
    proven = gb.proven_lengths(cfg, rep)
    assert proven == {"Ha": 180.0}
    assert gb.fallback_exposure_s(cfg, "Ha", proven) == (180.0, "tracking test 2026-10-05")


def test_proven_lengths_reads_the_report_once(tmp_path, monkeypatch):
    from photonscript.scheduler import tracking_test
    calls = []

    def _build(config, date, read_headers=True, **k):
        calls.append((date, read_headers))
        return REPORT_1005
    monkeypatch.setattr(tracking_test, "build_report", _build)
    cfg = _cfg(tmp_path, guide_fallback_test_date="2026-10-05",
               guide_fallback_test_since_utc="")
    assert gb.proven_lengths(cfg) == {"L": 60.0}
    assert gb.proven_lengths(cfg) == {"L": 60.0}
    assert calls == [("2026-10-05", False)]
    assert gb.proven_lengths(_cfg(tmp_path)) == {}            # no test date set


# ---- decision table -------------------------------------------------------------

BAD = {"viable": False, "reason": "SNR 26 under 30"}
GOOD = {"viable": True, "reason": "SNR 45"}


@pytest.mark.parametrize("kw, decision, act, source", [
    (dict(mode="auto", night_guided=False, verdict=BAD), "unguided", False, "night"),
    (dict(mode="off", night_guided=True, verdict=BAD), "guided", False, "off"),
    (dict(mode="auto", night_guided=True, verdict=GOOD), "guided", False, "live"),
    (dict(mode="auto", night_guided=True, verdict=BAD, headroom=True), "pending", False, "live"),
    (dict(mode="observe", night_guided=True, verdict=BAD), "unguided", False, "live"),
    (dict(mode="auto", night_guided=True, verdict=BAD), "unguided", True, "live"),
    (dict(mode="auto", night_guided=True, history={"night": "2026-10-05", "reason": "x"}),
     "unguided", True, "history"),
    (dict(mode="observe", night_guided=True, history={"night": "2026-10-05", "reason": "x"}),
     "unguided", False, "history"),
    (dict(mode="auto", night_guided=True), "guided", False, "default"),
])
def test_decision_table(kw, decision, act, source):
    d = gb.decide(**kw)
    assert (d["decision"], d["act"], d["source"]) == (decision, act, source)
    assert d["reason"]


def test_block_mode_parsing(tmp_path):
    assert gb.block_mode(_cfg(tmp_path)) == "observe"
    assert gb.block_mode(_cfg(tmp_path, guide_block_mode="AUTO")) == "auto"
    assert gb.block_mode(_cfg(tmp_path, guide_block_mode="nonsense")) == "observe"


# ---- history -------------------------------------------------------------------

def _check(cfg, night, viable, target="Heart Nebula", filt="Ha", **kw):
    rec = {"event": "check", "target": target, "filter": filt, "viable": viable,
           "final": True, "reason": "SNR 26 under 30", "binning": 2, "gain": 100}
    rec.update(kw)
    return gb.append(cfg, night, rec)


def test_history_same_setup_only_and_newest_wins(tmp_path):
    cfg = _cfg(tmp_path)
    setup = {"binning": 2, "gain": 100}
    _check(cfg, "2026-10-05", False)
    h = gb.history_unguided(cfg, "Heart Nebula", "Ha", NIGHT, setup)
    assert h["night"] == "2026-10-05" and not h["viable"]
    assert gb.history_unguided(cfg, "Heart Nebula", "OIII", NIGHT, setup) is None
    assert gb.history_unguided(cfg, "Heart Nebula", "Ha", NIGHT, {"binning": 3, "gain": 100}) is None
    assert gb.history_unguided(cfg, "Heart Nebula", "Ha", NIGHT, setup, days=0) is None
    _check(cfg, "2026-10-05", True)                     # later the same night: viable
    assert gb.history_unguided(cfg, "Heart Nebula", "Ha", NIGHT, setup) is None
    _check(cfg, "2026-09-20", False)                    # too old (7 days)
    assert gb.history_unguided(cfg, "Heart Nebula", "Ha", "2026-09-30", setup) is None


def test_dispatch_decisions_live_plus_history(tmp_path):
    cfg = _cfg(tmp_path)
    _check(cfg, "2026-10-05", False, filt="OIII")
    t = _heart()
    dec = gb.dispatch_decisions(cfg, NIGHT, [t], live={("Heart Nebula", "Ha"): {
        "source": "live", "reason": "SNR 22"}}, setup={"binning": 2, "gain": 100})
    assert set(dec) == {("Heart Nebula", "Ha"), ("Heart Nebula", "OIII")}
    assert dec[("Heart Nebula", "OIII")]["source"] == "history"
    off = _cfg(tmp_path, guide_block_history_days=0)
    assert set(gb.dispatch_decisions(off, NIGHT, [t], live={}, setup={})) == set()


# ---- apply to the planner's targets --------------------------------------------------

def test_apply_marks_blocks_and_caps_them(tmp_path):
    cfg = _cfg(tmp_path)
    t = _heart(exposures=[_plan(), _plan(FilterType.OIII), _plan(FilterType.LUMINANCE, 180, 20)])
    notes = gb.apply_to_targets(cfg, [t], {("Heart Nebula", "Ha"): {"source": "live", "reason": "x"},
                                           ("Heart Nebula", "L"): {"source": "live", "reason": "y"}},
                                proven={})
    assert t.start_guiding and t.unguided_filters == ["Ha", "L"]
    ha, oiii, lum = t.exposures
    assert (ha.exposure_seconds, ha.count) == (300, 20)       # same integration
    assert (oiii.exposure_seconds, oiii.count) == (600, 10)   # guided, untouched
    assert (lum.exposure_seconds, lum.count) == (60, 60)
    assert len(notes) == 2 and "300 s (config" in notes[0]
    every = _heart()
    gb.apply_to_targets(cfg, [every], {("Heart Nebula", "Ha"): {}, ("Heart Nebula", "OIII"): {}},
                        proven={})
    assert every.start_guiding is False and every.unguided_filters == []


# ---- sequence shape --------------------------------------------------------------------

def _seq(targets):
    return json.loads(generate_nina_json(NinaSequenceFile(name="t", targets=targets)))


def _blocks(seq):
    return [d for d in _find_type(seq, "Container")
            if str(d.get("Name", "")).endswith(TARGET_GUIDE_BLOCK_SUFFIX)]


def _types(node):
    return [d.get("$type", "").split(",")[0].split(".")[-1] for d in _exec_items(node)]


def test_sequence_shape_of_unguided_and_guided_blocks():
    seq = _seq([_heart(unguided=["Ha"], exposures=[_plan(exp=300, count=20),
                                                   _plan(FilterType.OIII)])])
    blocks = _blocks(seq)
    assert len(blocks) == 2
    ha = next(b for b in blocks if {i["ExposureTime"] for i in _find_type(b, "TakeExposure")} == {300})
    oiii = next(b for b in blocks if b is not ha)
    # unguided: StopGuiding first, no StartGuiding, capped subs, no active dither
    assert _types(ha)[0] == "StopGuiding" and "StartGuiding" not in _types(ha)
    assert {i["ExposureTime"] for i in _find_type(ha, "TakeExposure")} == {300}
    assert [d["AfterExposures"] for d in _find_type(ha, "DitherAfterExposures")] == [0]
    # guided: StartGuiding (no forced calibration) right before its lights
    t = _types(oiii)
    assert "StopGuiding" not in t
    assert t.index("StartGuiding") < t.index("TakeExposure")
    assert t.index("RunAutofocus") < t.index("StartGuiding")
    sg = _find_type(oiii, "StartGuiding")[0]
    assert sg["ForceCalibration"] is False and sg["ErrorBehavior"] == 1
    assert [d["AfterExposures"] for d in _find_type(oiii, "DitherAfterExposures")] == [5]
    # no target-level StartGuiding; the run lints clean as guided
    assert len(_find_type(seq, "StartGuiding")) == 1
    assert "PS-85 per-block guiding: Ha unguided" in json.dumps(seq)
    res = lint(seq, guided=True)
    assert res.ok, [f.detail for f in res.findings if f.level == "ERROR"]


def test_all_guided_target_keeps_the_old_shape():
    seq = _seq([_heart()])
    assert not _blocks(seq)
    assert len(_find_type(seq, "StartGuiding")) == 1
    # a decision for a filter the target does not carry changes nothing
    seq2 = _seq([_heart(unguided=["SII"])])
    assert not _blocks(seq2) and len(_find_type(seq2, "StartGuiding")) == 1


def test_block_containers_map_back_to_the_target():
    from photonscript.shared.target_names import canonical_target
    assert canonical_target("Heart Nebula" + TARGET_GUIDE_BLOCK_SUFFIX) == "Heart Nebula"


def test_selftest_runs_before_every_block_start_guiding(tmp_path, monkeypatch):
    script = tmp_path / "phd2-selftest.cmd"
    script.write_text("@echo off\n")
    monkeypatch.setenv("PS_PHD2_SELFTEST_ENABLED", "true")
    monkeypatch.setenv("PS_PHD2_SELFTEST_SCRIPT", str(script))
    seq = _seq([_heart(unguided=["Ha"], exposures=[
        _plan(exp=300, count=20), _plan(FilterType.OIII), _plan(FilterType.SII)])])
    assert len(_find_type(seq, "StartGuiding")) == 2
    res = lint(seq, guided=True)
    assert not [f for f in res.findings if f.rule == "phd2-selftest" and f.level == "ERROR"]


# ---- armer dispatch ---------------------------------------------------------------------

def _armer(tmp_path, monkeypatch, targets, **cfg):
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler import target_planner

    class _Store:
        projects: dict = {}
    monkeypatch.setattr(app_mod, "_store", _Store())
    monkeypatch.setattr(target_planner, "plan_night_sequence",
                        lambda projects, config, now: targets)
    tmp_path.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(gb, "current_setup", lambda config: {"binning": 2, "gain": 100})
    a = Armer(_cfg(tmp_path, **cfg))
    a.plan = {"night_of": NIGHT, "dusk_utc": "2026-10-07T01:30:00Z",
              "dawn_utc": "2026-10-07T11:30:00Z"}
    monkeypatch.setattr(a, "_calibration_slot", lambda targets, now: None)
    return a


def _lengths(path):
    seq = json.loads(path.read_text(encoding="utf-8"))
    return sorted({(i["ExposureTime"]) for i in _find_type(seq, "TakeExposure")
                   if i.get("ImageType") == "LIGHT"})


def test_dispatch_auto_applies_live_decisions_and_snapshots(tmp_path, monkeypatch):
    a = _armer(tmp_path, monkeypatch, [_heart()], guide_block_mode="auto")
    a.block_decisions = {"night": NIGHT, "blocks": {"Heart Nebula|Ha": {
        "source": "live", "reason": "SNR 22"}}, "redispatches": 1}
    assert a._dispatch() is True
    assert _lengths(a.sequence_path) == [300.0, 600.0]
    snap = json.loads((tmp_path / "data" / "runs" / f"{NIGHT}_plan.json").read_text())
    assert snap["targets"][0]["guided"] is True
    assert snap["targets"][0]["unguided_filters"] == ["Ha"]


def test_dispatch_all_blocks_unguided_makes_the_night_lint_unguided(tmp_path, monkeypatch):
    a = _armer(tmp_path, monkeypatch, [_heart()], guide_block_mode="auto")
    a.block_decisions = {"night": NIGHT, "blocks": {"Heart Nebula|Ha": {},
                                                    "Heart Nebula|OIII": {}}}
    assert a._dispatch() is True, a.detail
    seq = json.loads(a.sequence_path.read_text(encoding="utf-8"))
    assert not _find_type(seq, "StartGuiding")
    assert _lengths(a.sequence_path) == [300.0]


def test_dispatch_history_observe_records_only_auto_applies(tmp_path, monkeypatch):
    a = _armer(tmp_path, monkeypatch, [_heart()])            # observe (default)
    _check(a.config, "2026-10-05", False, filt="OIII")
    assert a._dispatch() is True
    assert _lengths(a.sequence_path) == [600.0]              # nothing switched
    dec = gb.tonight_decisions(a.config, NIGHT)[("Heart Nebula", "OIII")]
    assert (dec["source"], dec["acted"], dec["exposure_s"]) == ("history", False, 300.0)
    assert len(a._block_alerts) == 1 and "would be planned unguided" in a._block_alerts[0][1]
    a._block_alerts = []
    a._dispatch()                                            # recorded once per night
    assert a._block_alerts == []
    b = _armer(tmp_path / "b", monkeypatch, [_heart()], guide_block_mode="auto")
    _check(b.config, "2026-10-05", False, filt="OIII")
    assert b._dispatch() is True
    assert _lengths(b.sequence_path) == [300.0, 600.0]


def test_dispatch_off_and_unguided_nights_untouched(tmp_path, monkeypatch):
    a = _armer(tmp_path, monkeypatch, [_heart()], guide_block_mode="off")
    _check(a.config, "2026-10-05", False)
    a.block_decisions = {"night": NIGHT, "blocks": {"Heart Nebula|Ha": {}}}
    assert a._dispatch() is True
    assert _lengths(a.sequence_path) == [600.0]
    u = _armer(tmp_path / "u", monkeypatch, [_heart()], guide_block_mode="auto")
    u.guiding_override = "unguided"
    assert u._dispatch() is True
    assert _lengths(u.sequence_path) == [300.0]              # PS-66 cap only


# ---- armer.block_unguided -------------------------------------------------------------------

def _running(tmp_path, monkeypatch, **cfg):
    a = _armer(tmp_path, monkeypatch, [_heart()], **cfg)
    a.state = "RUNNING"
    calls = []

    async def _nina(key, *x, **k):
        calls.append(key)
        return {"Success": True}
    monkeypatch.setattr(a, "_nina", _nina)
    monkeypatch.setattr(armer_mod, "notify", lambda *x, **k: _none())
    return a, calls


async def _none():
    return None


async def test_block_unguided_observe_records_and_pushes_once_per_target(tmp_path, monkeypatch, _quiet):
    a, calls = _running(tmp_path, monkeypatch)
    assert await a.block_unguided("Heart Nebula", "Ha", "SNR 22 under 30") is False
    assert await a.block_unguided("Heart Nebula", "OIII", "SNR 20 under 30") is False
    assert calls == [] and not a.block_decisions
    decs = gb.tonight_decisions(a.config, NIGHT)
    assert {k[1] for k in decs} == {"Ha", "OIII"}
    assert all(d["acted"] is False and "observe" in d["note"] for d in decs.values())
    assert len(_quiet) == 1 and "Would run it unguided at 300 s" in _quiet[0]


async def test_block_unguided_auto_redispatches_with_the_block_unguided(tmp_path, monkeypatch, _quiet):
    a, calls = _running(tmp_path, monkeypatch, guide_block_mode="auto",
                        guide_block_max_redispatch=1)
    assert await a.block_unguided("Heart Nebula", "Ha", "SNR 22 under 30") is True
    assert calls[:2] == ["sequence_stop", "guider_stop"]
    assert _lengths(a.sequence_path) == [300.0, 600.0]
    assert a.block_decisions["blocks"].keys() == {"Heart Nebula|Ha"}
    assert json.loads((tmp_path / "data" / "armer_state.json").read_text())[
        "block_decisions"]["redispatches"] == 1
    d = gb.tonight_decisions(a.config, NIGHT)[("Heart Nebula", "Ha")]
    assert d["acted"] is True and "re-dispatch ok" in d["note"]
    assert len(_quiet) == 1 and "re-dispatched with Ha unguided at 300 s" in _quiet[0]
    n = len(calls)
    assert await a.block_unguided("Heart Nebula", "Ha", "again") is False   # done
    assert await a.block_unguided("Heart Nebula", "OIII", "x") is False      # cap 1
    assert len(calls) == n
    assert "limit" in gb.tonight_decisions(a.config, NIGHT)[("Heart Nebula", "OIII")]["note"]


async def test_block_unguided_not_running_or_unguided_night(tmp_path, monkeypatch):
    a, calls = _running(tmp_path, monkeypatch, guide_block_mode="auto")
    a.state = "ARMED"
    assert await a.block_unguided("Heart Nebula", "Ha", "x") is False
    a.state, a.guiding_override = "RUNNING", "unguided"
    assert await a.block_unguided("Heart Nebula", "OIII", "x") is False
    assert calls == []


async def test_request_block_unguided_without_an_armer_records(tmp_path, monkeypatch, _quiet):
    import sys
    monkeypatch.setattr(sys.modules["photonscript.scheduler.app"], "_armer", None,
                        raising=False)
    cfg = _cfg(tmp_path)
    assert await armer_mod.request_block_unguided(cfg, "Heart Nebula", "SII", "SNR 21") is False
    night = store.night_of(cfg)
    d = gb.tonight_decisions(cfg, night)[("Heart Nebula", "SII")]
    assert d["note"] == "no armer in this process" and len(_quiet) == 1


def test_block_decisions_survive_a_restart_same_night_only(tmp_path, monkeypatch):
    a = Armer(_cfg(tmp_path, connect_all_on_arm=False))
    a.state, a.plan = "RUNNING", {"night_of": NIGHT, "dawn_utc": "2099-01-01T11:00:00Z"}
    a.block_decisions = {"night": NIGHT, "blocks": {"Heart Nebula|Ha": {}}}
    a._persist()
    b = Armer(a.config)
    monkeypatch.setattr(armer_mod.asyncio, "create_task",
                        lambda coro: (coro.close(), None)[1])
    assert b.restore() is True
    assert b._live_block_decisions() == {("Heart Nebula", "Ha"): {}}
    b.plan["night_of"] = "2026-10-07"
    assert b._live_block_decisions() == {}


# ---- NB tuner band and binning advice -------------------------------------------------------------

def test_nb_tuner_band(tmp_path):
    from photonscript.telescope_agent.guide_tuner import tune_cfg
    cfg = _cfg(tmp_path)
    assert tune_cfg(cfg, "Ha").exp_hi == 8000 and tune_cfg(cfg, "OIII").exp_hi == 8000
    assert tune_cfg(cfg, "L").exp_hi == 4000 and tune_cfg(cfg).exp_hi == 4000
    assert tune_cfg(_cfg(tmp_path, phd2_tune_exp_ms_nb=""), "Ha").exp_hi == 4000


def test_recommend_uses_the_nb_band_and_advises_binning(tmp_path):
    from photonscript.scheduler import phd2_tuning as tn
    cfg = _cfg(tmp_path)
    base = {"profile": "RC16 OAG", "binning": 2, "target": "Heart Nebula",
            "snr": 25.0, "hfd_px": 6.0, "clipped": False, "in_band": False}
    tn.record(cfg, dict(base, gain=40, filter="OIII", exposure_ms=4000,
                        peak_frac=0.208, amp_frac=0.20))
    rec = tn.recommend(cfg)
    row = rec["by_filter"]["OIII"]
    assert row["exp_band_ms"] == [1000, 8000]
    assert rec["gain"] == 70                    # 1.7x at 8 s, not 3.5x at 4 s
    assert "binning_advice" not in rec
    tn.record(cfg, dict(base, gain=100, filter="Ha", exposure_ms=4000,
                        peak_frac=0.033, amp_frac=0.025))
    rec = tn.recommend(cfg)
    adv = rec["binning_advice"]
    assert adv["from"] == 2 and adv["binning"] == 3 and "advice only" in adv["note"]
    assert tn.nb_binning_advice(cfg, 3, 100, 20.0) is None   # at the bound


# ---- API, Guiding tab, config ----------------------------------------------------------------------

def test_api_blocks_and_guiding_tab(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    night = store.night_of(cfg)
    _check(cfg, night, False, snr=24.5, hfd_px=5.9, profile="jagged profile")
    gb.append(cfg, night, {"event": "decision", "target": "Heart Nebula", "filter": "Ha",
                           "decision": "unguided", "source": "live", "acted": False,
                           "reason": "no real guide star", "exposure_s": 300.0})
    c = TestClient(app.app)
    r = c.get("/api/phd2/blocks").json()
    assert r["mode"] == "observe" and r["date"] == night and r["checks"] == 1
    b = r["blocks"][0]
    assert (b["target"], b["filter"], b["decision"], b["snr"]) == (
        "Heart Nebula", "Ha", "unguided", 24.5)
    assert r["fallback"]["Ha"] == {"exposure_s": 300.0, "source": "config"}
    html = c.get("/guiding").text
    for i in ("blockSec", "blockInfo", "blockRows", "blockRefresh"):
        assert f'id="{i}"' in html
    assert 'href="#blockSec"' in html


def test_config_defaults_and_system_fields():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    c = PhotonScriptConfig(_env_file=None)
    assert c.guide_block_mode == "observe" and c.guide_viable_snr_min == 30.0
    assert c.guide_lowsnr_frames == 10 and c.guide_viable_frames == 5
    assert gb.parse_lengths(c.guide_fallback_exposure_s)["Ha"] == 300.0
    assert gb.parse_lengths(c.guide_fallback_exposure_s)["L"] == 60.0
    assert c.guide_fallback_test_since_utc == "2026-10-06T04:12:00Z"
    # the earlier switches stay as they were (Jeremy flips them)
    assert (c.guard_on_fail, c.selftest_on_fail, c.phd2_cal_fail_action) == (
        "alert", "alert", "keep")
    names = {f[0]: f for f in _CONFIG_FIELDS}
    for k in ("guide_block_mode", "guide_viable_snr_min", "guide_viable_hfd_px",
              "guide_viable_frames", "guide_lowsnr_frames", "guide_fallback_exposure_s",
              "guide_fallback_test_date", "guide_fallback_test_since_utc",
              "guide_block_history_days", "guide_block_max_redispatch",
              "phd2_tune_exp_ms_nb", "phd2_tune_bin_max_nb"):
        assert k in names and names[k][1] == "PS_" + k.upper()
        assert hasattr(c, k)
    assert "PS-85)" not in names["guard_on_fail"][2]


async def test_block_unguided_ignores_a_filter_that_is_not_a_planned_block(tmp_path, monkeypatch, _quiet):
    a, calls = _running(tmp_path, monkeypatch, guide_block_mode="auto")
    assert a._dispatch() is True                     # snapshot: Ha + OIII guided
    assert gb.planned_blocks(a.config, NIGHT) == {("Heart Nebula", "Ha"),
                                                  ("Heart Nebula", "OIII")}
    # the AF filter between narrowband blocks is checked but is no block
    assert await a.block_unguided("Heart Nebula", "L", "SNR 12") is False
    assert calls == [] and not a.block_decisions and _quiet == []
    assert gb.planned_blocks(a.config, "2026-01-01") is None
