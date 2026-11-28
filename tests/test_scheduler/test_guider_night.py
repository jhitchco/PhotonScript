"""Guider commissioning night (2026-10-28): the arm recipe guider_commission.

The first night with the ZWO ASI174MM Mini on the RC16 OAG. Fixture numbers
follow the live 2026-10-28 readiness (camera changed 2026-10-28 04:48 UTC,
GP678C dark library of 2026-09-28 at 1 to 6 s, PHD2 exposure 8 s, Max ADU
255) and the 2026-10-28 narrowband analysis (Ha / OIII stars ~0.73 ecc
along ~38 deg while L is ~0.55 with no common axis).
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from photonscript.scheduler import guided_commission as gc
from photonscript.scheduler import guider_night as gn
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import optics_test as ot
from photonscript.scheduler import sideload as sd
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget


NIGHT = "2026-10-28"
GC = sd.RECIPE_GUIDER_COMMISSION
CAL_FIELD = {"name": "Cal field", "ra_hours": 22.0, "dec_degrees": 5.0,
             "ha_hours": 0.6}
AT = "2026-10-29T01:52:00Z"


def _cfg(tmp_path, script=True, **kw):
    kw.setdefault("observatory_tz", "America/Denver")
    if script:
        p = tmp_path / "guider-commission.cmd"
        p.write_text("@echo off\n")
        kw.setdefault("guider_commission_script", str(p))
    else:
        kw.setdefault("guider_commission_script", str(tmp_path / "missing.cmd"))
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"), **kw)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    c = _cfg(tmp_path)
    monkeypatch.setattr(nsj, "_gen_cfg_cache", c)
    monkeypatch.setattr(nsj, "_filter_names_cache", c.filter_name_map())
    return c


def _night(guided=True, cal=True):
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    t = NinaSequenceTarget(
        name="Heart Nebula", ra_hours=2.55, dec_degrees=61.5, start_guiding=guided,
        exposures=[ExposurePlan(filter_type=FilterType.HA, exposure_seconds=300,
                                count=10, gain=200, offset=256)])
    seq = build_sequence_for_night("PhotonScript_20261028", [t])
    seq.wait_until_local = "19:30:00"
    return json.loads(nsj.generate_nina_json(
        seq, cal_field=CAL_FIELD if (guided and cal) else None))


def _walk(n, out=None):
    out = [] if out is None else out
    if isinstance(n, dict):
        out.append(n)
        for v in n.values():
            _walk(v, out)
    elif isinstance(n, list):
        for v in n:
            _walk(v, out)
    return out


def _short(n):
    return str(n.get("$type") or "").split(",")[0].split(".")[-1]


def _scripts(tree):
    return [(n["Script"].split()[-1], n["ErrorBehavior"]) for n in _walk(tree)
            if _short(n) == "ExternalScript" and "guider-commission" in n["Script"]]


def _container(tree, name):
    return next(n for n in _walk(tree) if n.get("Name") == name)


def _spec(cfg, guided=True, opts=None, star_field=None):
    return gn.build_spec(cfg, guided=guided, opts=opts or {},
                         nb_field={"name": "M 2", "ra_hours": 21.56,
                                   "dec_degrees": -0.82},
                         star_field=star_field, script=cfg.guider_commission_script)


# ---------------------------------------------------------------- the recipe

class TestArmRecipe:
    def test_plain(self):
        rec, err = sd.arm_recipe(GC)
        assert err is None
        assert rec == {"id": GC, "label": "guider commissioning", "opts": {}}
        assert GC in sd.RECIPES

    def test_options(self):
        rec, err = sd.arm_recipe(GC, {"skip": "ga, nb_focus", "rounds": 9,
                                      "star_tries": "5"})
        assert err is None
        assert rec["opts"] == {"skip": ["ga", "nb_focus"], "rounds": 3, "star_tries": 5}
        assert rec["label"] == "guider commissioning (skip ga, nb_focus)"

    def test_bad_options(self):
        assert "not a step" in sd.arm_recipe(GC, {"skip": ["coffee"]})[1]
        assert "not allowed" in sd.arm_recipe(GC, {"points": 10})[1]
        assert "whole number" in sd.arm_recipe(GC, {"rounds": "two"})[1]


def test_recipe_container_is_a_test_target():
    from photonscript.shared.target_names import is_test_target
    assert is_test_target("Guider commission M 2")
    assert is_test_target("Optics test NB M 2 Ha -220")
    p = ot.parse_name("Optics test NB M 2 Ha -220")
    assert p == {"field": "NB M 2", "filter": "Ha", "offset": -220}


# ---------------------------------------------------------------- generator

def test_guided_container_steps_in_order(cfg):
    seq = json.loads(nsj.generate_guider_commission_json(_spec(cfg)))
    assert sd.target_names(seq) == ["Guider commission M 2"]
    g = _container(seq, "Guider commission M 2 guider steps")
    kinds = [_short(i) + (":" + i["Script"].split()[-1] if "Script" in i else "")
             for i in g["Items"]["$values"]]
    assert kinds == ["ExternalScript:gate", "ExternalScript:selftest", "StartGuiding",
                     "ExternalScript:ga", "StopGuiding", "ExternalScript:ga-apply"]
    assert g["Items"]["$values"][0]["ErrorBehavior"] == nsj.GC_STOP_ERROR_BEHAVIOR
    sg = g["Items"]["$values"][2]
    assert sg["ForceCalibration"] is False and sg["ErrorBehavior"] == 0
    # every other step fails soft
    assert [eb for s, eb in _scripts(seq) if s != "gate"] == [0, 0, 0, 0]


def test_narrowband_test_order_and_sweep(cfg):
    seq = json.loads(nsj.generate_guider_commission_json(_spec(cfg)))
    nb = _container(seq, "Guider commission M 2 narrowband focus test")
    steps = [n["Target"]["TargetName"] for n in _walk(nb)
             if "DeepSkyObjectContainer" in str(n.get("$type"))]
    assert steps == ["Optics test NB M 2 L 0", "Optics test NB M 2 Ha 0",
                     "Optics test NB M 2 OIII 0", "Optics test NB M 2 L 0",
                     "Optics test NB M 2 OIII 0", "Optics test NB M 2 Ha 0",
                     "Optics test NB M 2 Ha -220", "Optics test NB M 2 Ha -110",
                     "Optics test NB M 2 Ha +110", "Optics test NB M 2 Ha +220"]
    moves = [n["RelativePosition"] for n in _walk(nb) if _short(n) == "MoveFocuserRelative"]
    assert moves == [-220, 110, 220, 110, -220] and sum(moves) == 0
    # one AF before every block (the first L AF comes before the center)
    assert sum(1 for n in _walk(nb) if _short(n) == "RunAutofocus") == 6
    # no autofocus trigger anywhere (it would refocus the sweep frames)
    assert not [n for n in _walk(seq) if "Autofocus" in _short(n) and "Trigger" in _short(n)]
    exps = [n["ExposureTime"] for n in _walk(nb) if _short(n) == "TakeExposure"]
    assert exps and set(exps) == {30.0}
    assert gn.expected_nb_frames() == len(steps)


def test_unguided_has_no_guider_items(cfg):
    seq = json.loads(nsj.generate_guider_commission_json(_spec(cfg, guided=False)))
    kinds = {_short(n) for n in _walk(seq)}
    assert "StartGuiding" not in kinds and "StopGuiding" not in kinds
    assert [s for s, _ in _scripts(seq)] == ["nb-report"]


def test_skip_options(cfg):
    seq = json.loads(nsj.generate_guider_commission_json(
        _spec(cfg, opts={"skip": ["ga", "nb_focus"]})))
    assert [s for s, _ in _scripts(seq)] == ["gate", "selftest"]
    assert not [n for n in _walk(seq) if "narrowband" in str(n.get("Name"))]
    assert gn.enabled_steps({"skip": ["ga_apply"]}, True) == {
        "star", "selftest", "ga", "nb_focus", "nb_report"}


def test_star_check_with_its_own_slew_when_no_slot(cfg):
    seq = json.loads(nsj.generate_guider_commission_json(
        _spec(cfg, star_field=CAL_FIELD)))
    g = _container(seq, "Guider commission M 2 guider steps")
    kinds = [_short(i) for i in g["Items"]["$values"]]
    assert kinds[:7] == ["ExternalScript", "StopGuiding", "SetTracking", "SwitchFilter",
                         "SlewScopeToRaDec", "Center", "ExternalScript"]
    star = g["Items"]["$values"][6]
    assert star["Script"].endswith(" star") and star["ErrorBehavior"] == 1


# ------------------------------------------------------------------- splice

@pytest.mark.parametrize("guided", [True, False])
def test_splice_lints_and_passes_the_dispatch_gate(cfg, guided):
    from photonscript.scheduler import dispatch_gate
    from photonscript.scheduler.sequence_lint import lint
    night = _night(guided)
    built = gn.build_test(cfg, night, guided, AT, {}, [])
    seq = sd.splice_guider_commission(night, built["test"], (), name="X",
                                      star_item=built["star_item"])
    res = lint(seq, guided=guided, unguided_dither=False)
    assert res.ok, [f.detail for f in res.findings if f.level == "ERROR"]
    v = dispatch_gate.check(seq, guided=guided,
                            guided_targets=["Heart Nebula"] if guided else [],
                            lint_errors=[])
    assert v["ok"], v["line"]
    names = sd.target_names(seq)
    assert names[0].startswith("Guider commission ") and names[1] == "Heart Nebula"
    assert built["field"]["est_minutes"] > 20


def test_star_check_rides_in_the_calibration_slot(cfg):
    night = _night(True)
    built = gn.build_test(cfg, night, True, AT, {}, [])
    assert built["spec"]["star_field"] is None and built["star_item"] is not None
    seq = sd.splice_guider_commission(night, built["test"], (), name="X",
                                      star_item=built["star_item"])
    slot = sd.calibration_slot(seq)
    kinds = [_short(i) for i in slot["Items"]["$values"]]
    at = kinds.index("Center")
    star = slot["Items"]["$values"][at + 1]
    assert star["Script"].endswith(" star") and star["ErrorBehavior"] == 1
    assert kinds[at + 2] == "StartGuiding"
    # Parent links rebuilt for the moved item
    assert star["Parent"] == {"$ref": slot["$id"]}


def test_no_slot_gives_the_star_check_its_own_field(cfg):
    night = _night(True, cal=False)
    built = gn.build_test(cfg, night, True, AT, {}, [])
    assert built["star_item"] is None and built["spec"]["star_field"]
    assert [s for s, _ in _scripts(built["test"])][:2] == ["gate", "star"]


def test_missing_script_refuses_the_guided_build_only(tmp_path, monkeypatch):
    c = _cfg(tmp_path, script=False)
    monkeypatch.setattr(nsj, "_gen_cfg_cache", c)
    with pytest.raises(ValueError, match="guider_commission_script"):
        gn.build_test(c, _night(True), True, AT, {}, [])
    built = gn.build_test(c, _night(False), False, AT, {}, [])
    assert not _scripts(built["test"])


# -------------------------------------------------------------- the armer

@pytest.fixture
def armer(tmp_path, monkeypatch, cfg):
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler import armer as armer_mod
    from photonscript.scheduler import target_planner

    class _Store:
        projects: dict = {}
    monkeypatch.setattr(app_mod, "_store", _Store())
    monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
    monkeypatch.setattr(target_planner, "plan_night_sequence",
                        lambda projects, config, now: [NinaSequenceTarget(
                            name="Heart Nebula", ra_hours=2.55, dec_degrees=61.5,
                            start_guiding=True,
                            exposures=[ExposurePlan(filter_type=FilterType.HA,
                                                    exposure_seconds=300, count=10,
                                                    gain=200, offset=256)])])
    monkeypatch.chdir(tmp_path)
    a = armer_mod.Armer(cfg)
    a.plan = {"night_of": NIGHT, "dusk_utc": "2026-10-29T01:52:00Z",
              "dawn_utc": "2026-10-29T12:07:00Z"}
    a.guiding_override = "guided"
    seen = {}

    def slot(targets, now):
        from photonscript.scheduler import phd2_calibration as pc
        seen["request"] = pc.pending_request(cfg)
        return dict(CAL_FIELD)
    monkeypatch.setattr(a, "_calibration_slot", slot)
    a.calls, a.pushes, a.seen = [], [], seen

    async def _nina(key, **kw):
        a.calls.append(key)
        return {"Success": True}

    async def _notify(config, msg, **k):
        a.pushes.append((msg, k.get("priority", 0)))
        return True

    async def _none(*_a, **_k):
        return None
    monkeypatch.setattr(a, "_nina", _nina)
    monkeypatch.setattr(a, "_send_block_alerts", _none)
    monkeypatch.setattr(a, "_dispatch_piggyback_companion", _none)
    monkeypatch.setattr(armer_mod, "notify", _notify)
    monkeypatch.setattr(app_mod, "get_armer", lambda: a)
    monkeypatch.setattr(app_mod, "_armer", a)
    return a


async def test_armer_dispatches_the_recipe_and_asks_for_a_calibration(armer):
    a = armer
    rec, _ = sd.arm_recipe(GC)
    a.recipe = {**rec, "night": NIGHT, "status": "armed"}
    assert await a._dispatch_and_start() is True
    seq = json.loads(a.sequence_path.read_text(encoding="utf-8"))
    names = sd.target_names(seq)
    assert names[0].startswith("Guider commission ") and names[-1] == "Heart Nebula"
    assert a.seen["request"]["mode"] == "guider_commission"
    assert a.recipe["status"] == "dispatched"
    assert a.recipe["container"] == names[0]
    slot = sd.calibration_slot(seq)
    assert any(str(i.get("Script", "")).endswith(" star") for i in slot["Items"]["$values"])
    # this night's guiding test runs in act
    assert gn.recipe_armed(a.config, NIGHT)
    assert gc.mode(a.config, NIGHT) == "act"
    assert gc.mode(a.config, "2026-10-29") == "advise"


def test_auto_recipe_on_the_trial_night(tmp_path):
    c = _cfg(tmp_path, guided_trial_night=NIGHT)
    assert gn.auto_recipe(c, NIGHT, True)["id"] == GC
    assert gn.auto_recipe(c, NIGHT, False) is None
    assert gn.auto_recipe(c, "2026-10-29", True) is None
    off = _cfg(tmp_path, guided_trial_night=NIGHT, guider_commission_on_trial_night=False)
    assert gn.auto_recipe(off, NIGHT, True) is None


def test_want_calibration(tmp_path):
    c = _cfg(tmp_path)
    r = {"id": GC, "status": "armed", "opts": {}}
    assert gn.want_calibration(c, r)
    assert not gn.want_calibration(c, {**r, "status": "dispatched"})
    assert not gn.want_calibration(c, {**r, "opts": {"skip": ["calibration"]}})
    assert not gn.want_calibration(_cfg(tmp_path, phd2_cal_mode="never"), r)


# ------------------------------------------------------------ step 1 and 2

def live_readiness():
    return {
        "night": NIGHT, "status": "blocked",
        "items": [
            {"order": 20, "id": "saturation_adu", "level": "blocker", "what": "Max ADU",
             "current": "255 (want 65535 (bit depth 16))",
             "fix": "Brain > Camera: saturation by Max ADU, value 65535 (16-bit)."},
            {"order": 30, "id": "exposure_ms", "level": "blocker",
             "what": "Guide exposure vs the dark library",
             "current": "8000 ms; the dark library has no dark at it"},
            {"order": 40, "id": "dark_library", "level": "blocker",
             "what": "PHD2 dark library",
             "current": "built 2026-09-28, before the guide camera changed 2026-10-28"},
            {"order": 45, "id": "darks_or_defects", "level": "advice",
             "what": "Dark library or defect map auto-loaded",
             "current": "darks off, defect map off"}],
        "fast_path": {"target_ms": 2000, "lib_s": [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0,
                                                   4.5, 5.0, 6.0]},
        "camera": {"id": "asi174mm_mini", "name": "ZWO ASI174MM Mini",
                   "detected": True, "changed_utc": "2026-10-28T04:48:36Z"}}


def test_darks_verdict_is_a_human_step():
    v = gn.darks_verdict(live_readiness(), [1000, 2000], 2000)
    assert v["status"] == gn.HUMAN and "before the guide camera changed" in v["detail"]
    assert "Max ADU" in v["fix"] and "GUI only" in v["fix"]
    ok = gn.darks_verdict({"items": [], "camera": {"name": "ZWO ASI174MM Mini"}},
                          [1000, 2000, 3000], 2000)
    assert ok["status"] == gn.OK
    assert gn.darks_verdict({"items": []}, [1000], 2000)["status"] == gn.HUMAN
    assert gn.darks_verdict({"items": []}, None, None)["status"] == gn.HUMAN


def _armer_stub(state="ARMED", recipe=None):
    a = SimpleNamespace(state=state, plan={"night_of": NIGHT,
                                           "dusk_utc": "2026-10-29T01:52:00Z"})
    r = recipe or {"id": GC, "night": NIGHT, "status": "armed", "opts": {}}
    a._recipe_tonight = lambda: r
    return a


async def test_pre_steps_push_the_darks_and_set_the_exposure(tmp_path):
    c = _cfg(tmp_path)
    pushes, applied = [], []

    async def notify(config, msg, priority=0, kind="story"):
        pushes.append((msg, priority, kind))

    async def apply_fn():
        applied.append(1)
        return {"ok": True, "results": [{"id": "exposure_ms", "ok": True, "to": 2000}]}

    async def audit_fn():
        return None
    now = datetime(2026, 10, 28, 23, 0)
    out = await gn.pre_steps(c, _armer_stub(), now, readiness=live_readiness(),
                             audit={"rows": []}, lib_ms=[1000, 2000], apply_fn=apply_fn,
                             audit_fn=audit_fn, notify=notify)
    assert out["did"] == ["darks", "exposure"] and applied == [1]
    st = gn.load_state(c, NIGHT)
    assert st["pre_done"] and st["steps"]["darks"]["status"] == gn.HUMAN
    assert st["steps"]["exposure"]["status"] == gn.OK
    act = [p for p in pushes if p[2] == "act"]
    assert len(act) == 1 and "If you act" in act[0][0] and "If not" in act[0][0]
    # once per night
    assert (await gn.pre_steps(c, _armer_stub(), now + timedelta(hours=1),
                               readiness=live_readiness(), notify=notify))["did"] == []


async def test_pre_steps_wait_for_phd2_until_late(tmp_path):
    c = _cfg(tmp_path)

    async def notify(*a, **k):
        return None

    async def refused():
        return {"ok": False, "results": [{"id": "exposure_ms", "ok": False,
                                          "note": "PHD2 not reachable"}]}

    async def audit_fn():
        return None
    kw = dict(readiness=live_readiness(), audit={"rows": []}, lib_ms=[2000],
              apply_fn=refused, audit_fn=audit_fn, notify=notify)
    early = datetime(2026, 10, 28, 22, 0)
    out = await gn.pre_steps(c, _armer_stub(), early, **kw)
    assert out["held"] == "PHD2 not reachable"
    assert "exposure" not in gn.load_state(c, NIGHT)["steps"]
    # throttled, then late: recorded as a soft WARN
    assert (await gn.pre_steps(c, _armer_stub(), early + timedelta(minutes=5), **kw))["did"] == []
    late = datetime(2026, 10, 29, 1, 30)
    await gn.pre_steps(c, _armer_stub(), late, **kw)
    assert gn.load_state(c, NIGHT)["steps"]["exposure"]["status"] == gn.WARN


async def test_pre_tick_only_while_armed_before_dusk(tmp_path):
    c = _cfg(tmp_path)
    assert (await gn.pre_tick(c, _armer_stub("DISARMED")))["did"] == []
    assert (await gn.pre_tick(c, _armer_stub("RUNNING"),
                              datetime(2026, 10, 29, 2, 0)))["did"] == []


def test_exposure_target():
    assert gn.exposure_target(live_readiness(), None)[0] == 2000
    audit = {"rows": [{"id": "exposure_ms", "status": "warn", "target": 3000}]}
    assert gn.exposure_target({}, audit)[0] == 3000
    t, why = gn.exposure_target({}, {"rows": [{"id": "exposure_ms", "status": "pass",
                                               "current": 2000}]})
    assert t is None and "already" in why


# ------------------------------------------------------------ step 3: star

def test_star_verdict():
    ok = gn.star_verdict([{"found": True, "snr": 25, "hfd_px": 3.1,
                           "center_share": 0.2}], 10, 8)
    assert ok["status"] == gn.OK
    soft = gn.star_verdict([{"found": True, "snr": 40, "hfd_px": 14.0}], 10, 8)
    assert soft["status"] == gn.WARN and "out of focus" in soft["why"]
    hot = gn.star_verdict([{"found": True, "snr": 90, "center_share": 0.8}], 10, 8)
    assert hot["status"] == gn.FAIL and "hot pixels" in hot["why"]
    none = gn.star_verdict([{"found": False}] * 3, 10, 8)
    assert none["status"] == gn.FAIL and "3 tries" in none["why"]


async def test_no_guide_star_stops_the_guider_steps(tmp_path, monkeypatch):
    c = _cfg(tmp_path)
    pushes = []

    async def notify(config, msg, priority=0, kind="story"):
        pushes.append((msg, priority, kind))

    async def star_fn():
        return {"readings": [{"try": i, "found": False} for i in (1, 2, 3)],
                "exposure_ms": 8000}

    async def safe(config, nina=None):
        return None
    monkeypatch.setattr(gn, "_unsafe", safe)
    now = datetime(2026, 10, 29, 1, 40)
    assert (await gn.run_step(c, "gate", now=now, night=NIGHT))["stop"] is False
    out = await gn.run_step(c, "star", now=now, night=NIGHT, notify=notify, star_fn=star_fn)
    assert out["stop"] is True and out["status"] == gn.FAIL
    assert "no guide star" in gn.stop_reason(c, NIGHT)
    msg, prio, kind = pushes[-1]
    assert kind == "act" and prio == 1 and "focusing by hand" in msg
    gate = await gn.run_step(c, "gate", now=now, night=NIGHT)
    assert gate["stop"] is True and gate["status"] == gn.SKIPPED
    ev = [json.loads(x) for x in Path(c.data_dir, "runs").glob("*_events.jsonl").__next__()
          .read_text().splitlines()]
    assert [e["value"] for e in ev if e["kind"] == gn.KIND] == ["star:fail"]


async def test_unsafe_roof_skips_the_sky_steps(tmp_path, monkeypatch):
    c = _cfg(tmp_path)

    async def unsafe(config, nina=None):
        return "NINA's safety monitor reads unsafe"

    async def notify(*a, **k):
        return None

    async def never():
        raise AssertionError("must not run")
    monkeypatch.setattr(gn, "_unsafe", unsafe)
    for step in ("star", "selftest", "ga"):
        out = await gn.run_step(c, step, night=NIGHT, notify=notify, star_fn=never,
                                selftest_fn=never, ga_fn=never)
        assert out["status"] == gn.SKIPPED and out["stop"] is False


async def test_unknown_step_never_stops(tmp_path):
    out = await gn.run_step(_cfg(tmp_path), "park", night=NIGHT)
    assert out["status"] == gn.FAIL and out["stop"] is False


async def test_guiding_test_fails_at_once_on_a_stop(tmp_path, monkeypatch):
    c = _cfg(tmp_path, guided_commission_mode="advise")
    st = gn.load_state(c, NIGHT)
    st["stopped"] = "no guide star on the OAG camera after 3 tries"
    gn.save_state(c, st)
    monkeypatch.setattr(gn, "recipe_armed", lambda config, night: night == NIGHT)
    calls, pushes = [], []

    class A:
        state = "RUNNING"
        plan = {"night_of": NIGHT}

        def _use_guiding(self):
            return True

        async def fallback_unguided(self, why):
            calls.append(why)
            return True

    async def notify(config, msg, priority=0, kind="story"):
        pushes.append((msg, priority))
    out = await gc.tick(c, A(), datetime(2026, 10, 29, 2, 0), subs=[], notify=notify)
    assert out["mode"] == "act" and out["step"] == "fail"
    assert calls and "guider commissioning stopped" in calls[0]
    assert gc.load_night(c, NIGHT)["verdict"] == "fail"


# ------------------------------------------------------------ steps 5 and 6

async def test_selftest_step(tmp_path, monkeypatch):
    c = _cfg(tmp_path)

    async def safe(config, nina=None):
        return None
    monkeypatch.setattr(gn, "_unsafe", safe)

    async def notify(*a, **k):
        return None

    async def st():
        return {"verdict": "PASS", "reasons": ["W/E/N/S moved 0.9 to 1.1 x"]}
    out = await gn.run_step(c, "selftest", night=NIGHT, notify=notify, selftest_fn=st)
    assert out["status"] == gn.OK and out["stop"] is False


def _frames(n=150, sd_ra=0.4, sd_dec=0.3, seed=7):
    import random
    rnd = random.Random(seed)
    return [{"ra": rnd.gauss(0, sd_ra), "dec": rnd.gauss(0, sd_dec), "drop": False,
             "settling": False} for _ in range(n)]


def test_ga_derive():
    d = gn.ga_derive(_frames(), 0.372)
    assert d["ok"] and d["n"] == 150
    assert 0.30 < d["rms_ra_px"] < 0.50 and 0.2 < d["rms_dec_px"] < 0.4
    # white noise about the lock: frame-to-frame / sqrt 2 recovers sigma
    assert abs(d["hf_ra_px"] - d["rms_ra_px"]) < 0.08
    assert d["ra_min_move"] == round(0.65 * d["hf_ra_px"], 2)
    assert d["dec_min_move"] == round(1.28 * d["hf_dec_px"], 2)
    assert d["rms_total_arcsec"] == round(d["rms_total_px"] * 0.372, 2)
    assert not gn.ga_derive(_frames(10), 0.372)["ok"]
    tiny = gn.ga_derive(_frames(100, 0.01, 0.01), 0.372)
    assert tiny["ra_min_move"] == gn.MINMOVE_RANGE[0]


def test_minmove_audit_rows():
    audit = {"rows": [{"id": "ra_min_move", "status": "pass", "current": "0.25",
                       "apply": "api"},
                      {"id": "dec_min_move", "status": "pass", "current": "1.5"}]}
    copy_, ids = gn.minmove_audit(audit, {"ra_min_move": 0.25, "dec_min_move": 0.4})
    assert ids == ["dec_min_move"]
    row = next(r for r in copy_["rows"] if r["id"] == "dec_min_move")
    assert row["target"] == 0.4 and row["apply"] == "api" and row["status"] == "warn"
    assert audit["rows"][1]["status"] == "pass"   # the stored audit is untouched


async def test_ga_then_apply(tmp_path, monkeypatch):
    c = _cfg(tmp_path)

    async def safe(config, nina=None):
        return None
    monkeypatch.setattr(gn, "_unsafe", safe)

    async def notify(*a, **k):
        return None

    async def ga_fn():
        return {"frames": _frames(), "scale": 0.372}
    out = await gn.run_step(c, "ga", night=NIGHT, notify=notify, ga_fn=ga_fn)
    assert out["status"] == gn.OK and "min-move" in out["detail"]
    from photonscript.scheduler import phd2_audit as pa
    monkeypatch.setattr(pa, "load_latest", lambda config: {"rows": []})
    seen = {}

    async def apply_fn():
        seen["ok"] = True
        return {"ok": True, "results": [{"id": "ra_min_move", "ok": True, "to": 0.26},
                                        {"id": "dec_min_move", "ok": True, "to": 0.4}]}

    async def audit_fn():
        return None
    out = await gn.run_step(c, "ga-apply", night=NIGHT, notify=notify, apply_fn=apply_fn,
                            audit_fn=audit_fn)
    assert seen and out["status"] == gn.OK
    assert gn.load_state(c, NIGHT)["steps"]["ga_apply"]["status"] == gn.OK


async def test_ga_apply_without_a_measurement_changes_nothing(tmp_path):
    async def notify(*a, **k):
        return None
    out = await gn.run_step(_cfg(tmp_path), "ga-apply", night=NIGHT, notify=notify)
    assert out["status"] == gn.SKIPPED


# ------------------------------------------------------------ step 7: report

def _step(off, ecc, axis, R=0.8):
    return {"offset": off, "ecc_median": ecc, "axis_deg": axis, "R": R,
            "hfr_median": 2.0 + abs(off) / 100.0, "zones": {}, "n": 2}


def test_nb_decision_astigmatism():
    ha_steps = [_step(-220, 0.7, 38.0), _step(-110, 0.6, 40.0), _step(0, 0.72, 38.0),
                _step(110, 0.6, 125.0), _step(220, 0.7, 128.0)]
    filters = [{"filter": "L", "steps": [_step(0, 0.4, None, 0.1)],
                **ot.filter_verdict([_step(0, 0.4, None, 0.1)])},
               {"filter": "Ha", "steps": ha_steps, **ot.filter_verdict(ha_steps)}]
    d = gn.nb_decision(filters, {"L": 5400, "Ha": 5520, "OIII": 5480}, 0.236)
    assert d["verdict"] == "astigmatism"
    assert d["nb_offsets"] == {"Ha": 120, "OIII": 80}
    assert "Ha +120" in d["headline"] and "UseFilterWheelOffsets" in d["headline"]
    assert d["best"]["Ha"]["fwhm_arcsec"] == round(2.0 * 2 * 0.236, 2)


def test_nb_decision_filter_tilt():
    ha_steps = [_step(-220, 0.74, 37.0), _step(-110, 0.73, 38.0), _step(0, 0.73, 38.0),
                _step(110, 0.72, 39.0), _step(220, 0.74, 38.0)]
    lsteps = [_step(0, 0.40, None, 0.1)]
    filters = [{"filter": "L", "steps": lsteps, **ot.filter_verdict(lsteps)},
               {"filter": "Ha", "steps": ha_steps, **ot.filter_verdict(ha_steps)}]
    d = gn.nb_decision(filters, {"L": 5400, "Ha": 5410}, 0.236)
    assert d["verdict"] == "filter_tilt" and d["l_round"]
    assert "seating" in d["headline"]


def test_nb_report_reads_only_the_nb_frames(tmp_path, monkeypatch):
    c = _cfg(tmp_path)
    recs = [{"rig": "rc16", "target": "Optics test NB M 2 Ha 0", "filter": "Ha",
             "focpos": 5520, "ecc": 0.7, "hfr": 2.1, "file": "a.fits"},
            {"rig": "rc16", "target": "Optics test NB M 2 L 0", "filter": "L",
             "focpos": 5400, "ecc": 0.4, "hfr": 1.9, "file": "b.fits"},
            {"rig": "rc16", "target": "Optics test M 2 L 0", "filter": "L",
             "focpos": 1, "file": "c.fits"},
            {"rig": "rc16", "target": "Heart Nebula", "filter": "Ha", "file": "d.fits"}]
    rep = gn.nb_report(c, NIGHT, records=recs)
    assert rep["n_frames"] == 2 and rep["af_positions"] == {"Ha": 5520, "L": 5400}
    assert (Path(c.data_dir) / "guided" / "guider_night" / f"{NIGHT}_nb_focus.json").is_file()
    assert "Narrowband focus test" in gn.format_nb_report(rep)


# --------------------------------------------------------------- the dry run

def test_plan_for_2026_10_28(tmp_path):
    c = _cfg(tmp_path)
    p = gn.plan(c, NIGHT, {}, guided=True, readiness=live_readiness(), projects=[])
    steps = [r["step"] for r in p["rows"]]
    assert steps == ["darks", "exposure", "preconfig", "twilight_af", "star",
                     "calibration", "imaging_gate", "selftest", "ga", "nb_focus",
                     "handover"]
    tw = p["twilight"]
    assert tw["naut_dusk"]["local"] == "19:24" and tw["astro_dusk"]["local"] == "19:52"
    by = {r["step"]: r for r in p["rows"]}
    assert by["darks"]["who"] == "human"
    assert by["selftest"]["time"]["local"] == "19:52"
    assert by["nb_focus"]["minutes"] > 30
    assert p["human_steps"][0]["needed_now"] and "Max ADU" in p["human_steps"][0]["what"]
    text = gn.format_plan(p)
    assert "Guider commissioning night 2026-10-28 (guided)" in text
    assert chr(0x2014) not in text and chr(0x2013) not in text


def test_plan_unguided_and_skips(tmp_path):
    c = _cfg(tmp_path)
    p = gn.plan(c, NIGHT, {"skip": ["ga"]}, guided=True, readiness=live_readiness(),
                projects=[])
    assert {r["step"]: r for r in p["rows"]}["ga"]["skipped"]
    u = gn.plan(c, NIGHT, {}, guided=False, readiness=live_readiness(), projects=[])
    assert "star" not in [r["step"] for r in u["rows"]]


def test_cli_step_exit_codes(monkeypatch):
    import io
    import urllib.request
    from typer.testing import CliRunner
    from photonscript.cli import app

    def fake(stop):
        class R(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def urlopen(req, timeout=0):
            return R(json.dumps({"status": "fail", "stop": stop, "detail": "x"}).encode())
        return urlopen
    monkeypatch.setattr(urllib.request, "urlopen", fake(True))
    assert CliRunner().invoke(app, ["guider-commission-step", "star"]).exit_code == gn.EXIT_STOP
    monkeypatch.setattr(urllib.request, "urlopen", fake(False))
    assert CliRunner().invoke(app, ["guider-commission-step", "ga"]).exit_code == 0

    def down(req, timeout=0):
        raise OSError("connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", down)
    assert CliRunner().invoke(app, ["guider-commission-step", "gate"]).exit_code == 0


def test_deploy_script_maps_stop_to_exit_1():
    text = (Path(__file__).resolve().parents[2] / "deploy" / "guider-commission.cmd").read_text()
    assert "guider-commission-step %1 --from-nina" in text
    assert "if %ERRORLEVEL% EQU 3 exit /b 1" in text and text.rstrip().endswith("exit /b 0")
