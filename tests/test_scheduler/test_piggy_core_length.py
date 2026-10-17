"""Piggy-600: shoot M31 at 120 s (nucleus clips at 300 s), 2026-10-14.

The core-check measure of the Library: Piggy-600 M31 300 s subs clip the
nucleus on 122 of 162 subs, 120 s only on 1 of 98 (p99.9 86% of
saturation). The NINA #2 companion used to shoot one length all night
(piggyback_exposure_s), so a goal's OSC length never reached it. Now:

* exposure_overrides {"OSC": 120} on a goal resizes its OSC plan (goal hours
  kept, accepted seconds carried) and leaves the RC16 plans alone;
* the armer turns tonight's targets into Piggy lengths (piggy_length.plan)
  and the companion gets one light loop per length, each until the RC16
  handoff of its last target;
* core_check says "Piggy M31 core clips at 300 s: set OSC to 120 s" in the
  morning report and on the target page (piggy_core_length advise), and
  in apply mode the planner shoots that length itself.
Nothing changes without an override and with the default mode."""
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from photonscript.scheduler import calibration as cal
from photonscript.scheduler import core_check as cc
from photonscript.scheduler import piggy_length as pl
from photonscript.scheduler.project_store import ProjectStore, osc_override_s
from photonscript.scheduler.sideload import lint_companion
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget, ExposurePlan, FilterType
from photonscript.shared.rigs import PIGGYBACK, rig_config

M31 = CelestialTarget(name="Andromeda Galaxy", catalog_id="M 31",
                      ra_hours=0.712, dec_degrees=41.27, object_type="galaxy")
HANDOFF = datetime(2026, 10, 14, 7, 30)        # 00:30 MST


def _cfg(tmp_path, **kw):
    (tmp_path / "data").mkdir(exist_ok=True)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"),
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _live_m31(store):
    """The live goal of 2026-10-13: RC16 LRGB at 180 s (+30 s HDR) and the
    Piggy OSC plan of 296 x 300 s with 295 accepted (88,560 s)."""
    p = store.add_from_target(M31, budget_hours=4.6)
    rc = [ExposurePlan(filter_type=FilterType(f), exposure_seconds=180.0, count=n,
                       gain=200, offset=256, acquired=a, acquired_s=a * 180.0,
                       hdr_short_seconds=30.0, hdr_short_count=12, hdr_short_acquired=12)
          for f, n, a in (("L", 44, 37), ("R", 13, 5), ("G", 13, 6), ("B", 13, 6))]
    osc = ExposurePlan(filter_type=FilterType.OSC, exposure_seconds=300.0, count=296,
                       gain=100, offset=256, acquired=295, acquired_s=88560.0, rig="piggyback")
    p.exposure_plans = rc + [osc]
    p.exposure_overrides = {"L": 180.0, "R": 180.0, "G": 180.0, "B": 180.0}
    p.hdr = {"L": 30.0, "R": 30.0, "G": 30.0, "B": 30.0}
    p.driving_rig = "piggyback"
    store.save()
    return p


def _by(p):
    return {(e.rig, e.filter_type.value): e for e in p.exposure_plans}


# ----------------------------------------------------------------- the goal

def test_osc_override_key():
    assert osc_override_s({"L": 180, "OSC": 120}) == 120.0
    assert osc_override_s({"osc": "90"}) == 90.0
    assert osc_override_s({"L": 180}) is None and osc_override_s(None) is None
    assert osc_override_s({"OSC": 0}) is None


def test_patch_osc_override_resizes_only_the_osc_plan(tmp_path):
    store = ProjectStore(_cfg(tmp_path))
    p = _live_m31(store)
    before = {k: (e.count, e.acquired, e.exposure_seconds) for k, e in _by(p).items()
              if k[0] == "rc16"}
    body = {"exposure_overrides": {"L": 180, "R": 180, "G": 180, "B": 180, "OSC": 120}}
    store.update(p.id, exposure_overrides=body["exposure_overrides"])
    osc = _by(p)[("piggyback", "OSC")]
    # same goal hours (296 x 300 s = 24.7 h) at 120 s, accepted seconds kept
    assert osc.exposure_seconds == 120.0 and osc.count == 740
    assert osc.acquired_s == 88560.0 and osc.acquired == 738
    # the RC16 plans are untouched (only the OSC key changed)
    assert {k: (e.count, e.acquired, e.exposure_seconds) for k, e in _by(p).items()
            if k[0] == "rc16"} == before
    assert p.exposure_overrides["OSC"] == 120.0


def test_patch_osc_override_with_osc_hours(tmp_path):
    store = ProjectStore(_cfg(tmp_path))
    p = _live_m31(store)
    store.update(p.id, exposure_overrides={"L": 180, "R": 180, "G": 180, "B": 180, "OSC": 120},
                 osc_hours=28.6)
    osc = _by(p)[("piggyback", "OSC")]
    assert osc.exposure_seconds == 120.0 and osc.count == 858 and osc.acquired == 738
    # a later resize keeps the override's length
    store.update(p.id, osc_hours=30)
    assert _by(p)[("piggyback", "OSC")].exposure_seconds == 120.0


def test_rc16_override_change_still_reallocates(tmp_path):
    store = ProjectStore(_cfg(tmp_path))
    p = _live_m31(store)
    store.update(p.id, exposure_overrides={"L": 300, "R": 180, "G": 180, "B": 180, "OSC": 120})
    assert _by(p)[("rc16", "L")].exposure_seconds == 300.0
    assert _by(p)[("piggyback", "OSC")].exposure_seconds == 120.0


def test_no_osc_key_keeps_the_osc_plan(tmp_path):
    store = ProjectStore(_cfg(tmp_path))
    p = _live_m31(store)
    store.update(p.id, exposure_overrides={"L": 180, "R": 180, "G": 180, "B": 180})
    osc = _by(p)[("piggyback", "OSC")]
    assert osc.exposure_seconds == 300.0 and osc.count == 296


# ----------------------------------------------------------------- the plan

def _t(name, handoff=None, **kw):
    return SimpleNamespace(name=name, handoff_utc=handoff, **kw)


def _goal(name, ov=None):
    return SimpleNamespace(target=SimpleNamespace(name=name), exposure_overrides=ov)


def test_plan_no_override_changes_nothing(tmp_path):
    cfg = _cfg(tmp_path, piggyback_exposure_s=450.0)
    rec = pl.plan(cfg, [_t("Andromeda Galaxy", HANDOFF), _t("Heart Nebula")],
                  [_goal("Andromeda Galaxy", {"L": 180}), _goal("Heart Nebula")])
    assert not rec["changed"] and rec["segments"] == [] and rec["single_s"] == 450.0
    assert pl.companion_kwargs(rec) == {} and pl.summary(rec) == ""


def test_plan_segments_follow_the_handoff(tmp_path):
    cfg = _cfg(tmp_path, piggyback_exposure_s=450.0)
    tg = [_t("focus", None, focus_calibration=True), _t("Andromeda Galaxy", HANDOFF),
          _t("Heart Nebula")]
    rec = pl.plan(cfg, tg, [_goal("Andromeda Galaxy", {"L": 180, "OSC": 120}),
                            _goal("Heart Nebula")])
    assert rec["changed"]
    assert [(s["exp_s"], s["until_utc"], s["targets"]) for s in rec["segments"]] == [
        (120.0, "2026-10-14T07:30:00Z", ["Andromeda Galaxy"]), (450.0, None, ["Heart Nebula"])]
    kw = pl.companion_kwargs(rec)
    assert kw["segments"][0]["until_utc"] == HANDOFF and kw["segments"][1]["until_utc"] is None
    assert "Andromeda Galaxy 120 s until 07:30Z" in pl.summary(rec)


def test_plan_single_target_and_unguided_cap(tmp_path):
    cfg = _cfg(tmp_path, piggyback_exposure_s=450.0)
    goals = [_goal("Andromeda Galaxy", {"OSC": 120})]
    rec = pl.plan(cfg, [_t("Andromeda Galaxy")], goals)
    assert rec["single_s"] == 120.0 and rec["changed"] and pl.companion_kwargs(rec) == {"exp_s": 120.0}
    # the applied unguided Piggy limit still caps a longer override
    rec = pl.plan(cfg, [_t("Andromeda Galaxy")], [_goal("Andromeda Galaxy", {"OSC": 600})], cap_s=300.0)
    assert rec["single_s"] == 300.0 and rec["targets"][0]["source"] == "override+unguided cap"
    # equal neighbors merge; a target without a handoff ends the list
    rec = pl.plan(cfg, [_t("A", HANDOFF), _t("B", HANDOFF), _t("C"), _t("D")],
                  [_goal("A", {"OSC": 120}), _goal("B", {"OSC": 120})])
    assert [s["targets"] for s in rec["segments"]] == [["A", "B"], ["C"]]


def test_plan_core_lengths_in_apply_only(tmp_path):
    cfg = _cfg(tmp_path, piggyback_exposure_s=450.0)
    core = {"Andromeda Galaxy": (120.0, "120 s: 1 of 98 subs clip")}
    rec = pl.plan(cfg, [_t("Andromeda Galaxy", HANDOFF), _t("Heart Nebula")],
                  [_goal("Andromeda Galaxy")], core=core)
    assert rec["targets"][0]["source"] == "core" and rec["segments"][0]["exp_s"] == 120.0
    assert pl.core_lengths(cfg, [], []) == {}            # advise (default): nothing


# ----------------------------------------------------------------- the companion

def _companion(cfg, **kw):
    return json.loads(cal.generate_piggyback_companion_json(
        rig_config(cfg, PIGGYBACK), has_safety=True, with_lights=True, **kw))


def _walk(n):
    if isinstance(n, dict):
        yield n
        for v in n.values():
            yield from _walk(v)
    elif isinstance(n, list):
        for v in n:
            yield from _walk(v)


def _loops(seq):
    return [d for d in _walk(seq) if d.get("Name") == cal.OSC_LIGHT_LOOP_NAME]


def _cond_types(loop):
    return [c["$type"].split(",")[0].rsplit(".", 1)[-1] for c in loop["Conditions"]["$values"]]


def test_companion_unchanged_without_segments(tmp_path):
    cfg = _cfg(tmp_path, piggyback_exposure_s=450.0)
    a = cal.generate_piggyback_companion_json(rig_config(cfg, PIGGYBACK), has_safety=True,
                                              with_lights=True)
    b = cal.generate_piggyback_companion_json(rig_config(cfg, PIGGYBACK), has_safety=True,
                                              with_lights=True, osc_segments=None)
    c = cal.generate_piggyback_companion_json(rig_config(cfg, PIGGYBACK), has_safety=True,
                                              with_lights=True,
                                              osc_segments=[{"exp_s": 120.0, "until_utc": None}])
    strip = lambda s: [d for d in _walk(json.loads(s)) if "ExposureTime" in d]  # noqa: E731
    assert strip(a) == strip(b) == strip(c)
    assert len(_loops(json.loads(a))) == 1


@pytest.mark.parametrize("gate", [False, True])
def test_companion_one_loop_per_length_until_the_handoff(tmp_path, gate):
    kw = {"piggyback_exposure_s": 450.0}
    if gate:
        script = tmp_path / "settle-gate.cmd"
        script.write_text("@echo off\n")
        kw.update(piggyback_settle_gate=True, piggyback_settle_script=str(script))
    else:
        kw.update(piggyback_settle_gate=False)
    cfg = _cfg(tmp_path, **kw)
    segs = [{"exp_s": 120.0, "until_utc": HANDOFF, "targets": ["Andromeda Galaxy"]},
            {"exp_s": 450.0, "until_utc": None, "targets": ["Heart Nebula"]}]
    seq = _companion(cfg, osc_segments=segs)
    loops = _loops(seq)
    assert len(loops) == 2
    exps = [[d["ExposureTime"] for d in _walk(lp) if "ExposureTime" in d and
             d.get("ImageType") == "LIGHT"] for lp in loops]
    assert exps == [[120.0], [450.0]]
    assert _cond_types(loops[0]) == ["SafetyMonitorCondition", "TimeCondition", "TimeCondition"]
    fixed = loops[0]["Conditions"]["$values"][1]
    assert "TimeProvider" in fixed["SelectedProvider"]["$type"]
    from photonscript.shared.localtime import to_local
    tl = to_local(cfg, HANDOFF)                                   # 07:30Z = 01:30 MDT
    assert (fixed["Hours"], fixed["Minutes"]) == (tl.hour, tl.minute) == (1, 30)
    assert _cond_types(loops[1]) == ["SafetyMonitorCondition", "TimeCondition"]
    assert all(lp["Triggers"]["$values"] for lp in loops)    # PS-68 refocus on each
    res = lint_companion(seq, settle_gate=gate)
    assert res.ok, [(f.rule, f.detail) for f in res.findings if f.level == "ERROR"]
    notice = [d for d in _walk(seq) if "Message" in d and "roof open" in str(d.get("Message"))]
    assert notice and "120/450s" in notice[0]["Message"]


def test_armer_records_and_passes_the_lengths(tmp_path, monkeypatch):
    from photonscript.scheduler.armer import Armer
    cfg = _cfg(tmp_path, piggyback_exposure_s=450.0)
    a = Armer.__new__(Armer)
    a.config, a.plan = cfg, {"night_of": "2026-10-13"}
    goals = [_goal("Andromeda Galaxy", {"OSC": 120}), _goal("Heart Nebula")]
    a._piggy_lengths([_t("Andromeda Galaxy", HANDOFF), _t("Heart Nebula")], goals)
    assert a.plan["piggy_length"]["changed"]
    pcfg, kw = a._piggy_length_kwargs(rig_config(cfg, PIGGYBACK))
    assert pcfg.piggyback_exposure_s == 450.0 and len(kw["osc_segments"]) == 2
    a._piggy_lengths([_t("Andromeda Galaxy")], goals)
    pcfg, kw = a._piggy_length_kwargs(rig_config(cfg, PIGGYBACK))
    assert pcfg.piggyback_exposure_s == 120.0 and kw == {}
    a._piggy_lengths([_t("Heart Nebula")], goals)                   # no override tonight
    pcfg, kw = a._piggy_length_kwargs(rig_config(cfg, PIGGYBACK))
    assert pcfg.piggyback_exposure_s == 450.0 and kw == {}
    # a failure plans as before
    monkeypatch.setattr(pl, "plan", lambda *a, **k: 1 / 0)
    a._piggy_lengths([_t("Andromeda Galaxy")], goals)
    assert a.plan["piggy_length"] is None
    pcfg, kw = a._piggy_length_kwargs(rig_config(cfg, PIGGYBACK))
    assert pcfg.piggyback_exposure_s == 450.0 and kw == {}
    src = (Path(pl.__file__).parent / "armer.py").read_text(encoding="utf-8")
    assert "self._piggy_lengths(targets, projects)" in src
    assert "pcfg, seg_kw = self._piggy_length_kwargs(pcfg)" in src


# ----------------------------------------------------------------- core advice

def _prow(exp, sat_px, p999):
    return {"date": "2026-10-12", "target": "Andromeda Galaxy", "rig": "piggyback",
            "filter": "OSC", "exp_s": exp, "verdict": "approved", "core_sat_px": sat_px,
            "core_peak_adu": 65535 if sat_px else int(p999), "core_p999": p999,
            "sat_adu": 65535.0, "offset": 256}


def _m31_rows():
    rows = [_prow(300, 10, 65535) for _ in range(122)] + [_prow(300, 0, 60000) for _ in range(40)]
    rows += [_prow(120, 0, 56400) for _ in range(97)] + [_prow(120, 6, 65535)]
    return rows


def test_core_advice_says_set_osc_to_120(tmp_path):
    cfg = _cfg(tmp_path, piggyback_exposure_s=450.0)
    (c,) = cc.coverage(cfg, _m31_rows(), _goal("Andromeda Galaxy", {"L": 180}))
    assert c["status"] == cc.OSC_LENGTH and c["osc_advice"]["suggest_s"] == 120.0
    assert c["message"].startswith("Piggy Andromeda Galaxy core clips at 300 s (core clipped on 122 of 162")
    assert 'set OSC to 120 s (exposure_overrides {"OSC": 120}; now 450 s from piggyback_exposure_s)' in c["message"]
    assert "120 s: 1 of 98 subs clip, core p99.9 86% of saturation" in c["message"]
    assert c["message"].isascii()


def test_core_advice_quiet_once_the_goal_shoots_120(tmp_path):
    cfg = _cfg(tmp_path, piggyback_exposure_s=450.0)
    (c,) = cc.coverage(cfg, _m31_rows(), _goal("Andromeda Galaxy", {"OSC": 120}))
    assert c["status"] == cc.COVERED and "osc_advice" not in c
    assert "master_OSC_hdr" in c["message"]


def test_core_advice_modes(tmp_path):
    off = _cfg(tmp_path, piggyback_exposure_s=450.0, piggy_core_length="off")
    (c,) = cc.coverage(off, _m31_rows(), None)
    assert c["status"] != cc.OSC_LENGTH and "osc_advice" not in c
    app = _cfg(tmp_path, piggyback_exposure_s=450.0, piggy_core_length="apply")
    (c,) = cc.coverage(app, _m31_rows(), None)
    assert c["status"] == cc.OSC_LENGTH and "the companion shoots 120 s on it" in c["message"]


def test_core_advice_without_a_proven_length_uses_the_suggestion(tmp_path):
    cfg = _cfg(tmp_path, piggyback_exposure_s=300.0)
    rows = [_prow(300, 10, 65535) for _ in range(10)]
    (c,) = cc.coverage(cfg, rows, None)
    assert c["status"] == cc.OSC_LENGTH and c["osc_advice"]["suggest_s"] == 30.0


def test_morning_line_and_target_page(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, piggyback_exposure_s=450.0)
    goals = [SimpleNamespace(target=SimpleNamespace(name="Andromeda Galaxy"),
                             exposure_overrides={"L": 180}, exposure_plans=[])]
    import photonscript.scheduler.sub_index as si
    from photonscript.shared.target_names import target_key
    k = target_key("Andromeda Galaxy")
    monkeypatch.setattr(si, "all_rows", lambda c, p: [dict(r, target_key=k) for r in _m31_rows()])
    line = cc.morning_line(cfg, "2026-10-12", _m31_rows()[:5], goals)
    assert line.startswith("Core clipped: Piggy Andromeda Galaxy core clips at 300 s")
    assert "set OSC to 120 s" in line
    blk = cc.target_core(cfg, _m31_rows(), goals[0])
    assert any("set OSC to 120 s" in ln for ln in blk["lines"])
    assert blk["coverage"][0]["osc_advice"]["suggest_s"] == 120.0


def test_core_lengths_apply_reads_the_index(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, piggyback_exposure_s=450.0, piggy_core_length="apply")
    import photonscript.scheduler.sub_index as si
    monkeypatch.setattr(si, "all_rows", lambda c, p: _m31_rows())
    got = pl.core_lengths(cfg, [_t("Andromeda Galaxy"), _t("Heart Nebula")],
                          [_goal("Andromeda Galaxy", {"L": 180})])
    assert got == {"Andromeda Galaxy": (120.0, "120 s: 1 of 98 subs clip, core p99.9 86% of saturation")}
    # a goal with its own OSC override is left to it
    assert pl.core_lengths(cfg, [_t("Andromeda Galaxy")],
                           [_goal("Andromeda Galaxy", {"OSC": 180})]) == {}
    monkeypatch.setattr(si, "all_rows", lambda c, p: 1 / 0)
    assert pl.core_lengths(cfg, [_t("Andromeda Galaxy")], []) == {}     # never raises


def test_config_keys_on_the_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    c = PhotonScriptConfig(_env_file=None)
    assert c.piggy_core_length == "advise"
    f = {x[0]: x for x in _CONFIG_FIELDS}
    assert f["piggy_core_length"][1] == "PS_PIGGY_CORE_LENGTH"
    assert pl.core_mode(SimpleNamespace(piggy_core_length="bogus")) == "advise"


@pytest.mark.parametrize("weather", ["clear", "unsafe_across_the_handoff"])
def test_sim_switches_length_at_the_handoff(tmp_path, weather):
    """NINA's SequentialStrategy (tests/test_scheduler/test_ps77 NinaSim):
    120 s lights until the RC16 handoff at 23:18 local, then 450 s to astro
    dawn; an unsafe spell across the handoff resumes straight at 450 s; no
    busy loop (PS-149)."""
    from tests.test_scheduler.test_ps77_safety_stop import TIMES, NinaSim
    cfg = _cfg(tmp_path, piggyback_exposure_s=450.0, piggyback_settle_gate=False)
    handoff_t = 3 * 3600.0                                   # dusk 20:18 + 3 h = 23:18 local
    until = datetime(2026, 10, 14, 5, 18)                    # 23:18 MDT
    segs = [{"exp_s": 120.0, "until_utc": until}, {"exp_s": 450.0, "until_utc": None}]
    seq = _companion(cfg, osc_segments=segs)
    unsafe = [] if weather == "clear" else [(handoff_t - 1500, handoff_t + 1500)]
    sim = NinaSim(seq, unsafe=unsafe, tick=0.002, max_steps=3_000_000).run()
    done = [x for x in sim.lights if x["done"]]
    assert done and sim.spins() == {}, sim.spins()
    short = [x for x in done if abs((x["end"] - x["start"]) - 120.0) < 1]
    long_ = [x for x in done if abs((x["end"] - x["start"]) - 450.0) < 1]
    assert short and long_ and len(short) + len(long_) == len(done)
    # the 120 s loop never runs past the handoff (NINA's TimeCondition counts
    # the next exposure), the 450 s loop takes over right after it
    assert max(x["end"] for x in short) <= handoff_t + 1
    assert min(x["start"] for x in long_) >= max(x["end"] for x in short)
    if weather == "clear":
        assert min(x["start"] for x in long_) > handoff_t - 121
    assert max(x["end"] for x in done) <= TIMES["DawnProvider"]
