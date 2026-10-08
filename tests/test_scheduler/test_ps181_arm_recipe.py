"""PS-181: arm with a recipe.

POST /api/arm {"recipe", "recipe_opts"} is validated against the sideload
recipes (TPoint options clamp to the mapping's limits); the armer keeps it
for that night only (armer_state.json, cleared on disarm, a new arm and the
dawn shutdown), splices it at dispatch with the same builder the sideload
preview uses (identical sequence and lint), starts it like a normal night,
falls back to the normal plan with one page when it does not build or lint,
and never repeats it on a re-dispatch (unsafe resume, PS-64 resume, PS-143
restart) once dispatched. The per-arm TPoint add mode reaches the
tpoint-sample script as --add.
"""
import asyncio
import json
from pathlib import Path

import pytest

from photonscript.scheduler import armer as armer_mod
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import sideload as sd
from photonscript.scheduler.routers import sideload as rt
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget

ROOT = Path(__file__).resolve().parents[2]
TT_FIELD = {"name": "M 2", "ra_hours": 21.56, "dec_degrees": -0.82,
            "source": "test"}
TT = sd.RECIPE_TT_THEN_TONIGHT
TPM = sd.RECIPE_TPOINT_MAPPING


def _cfg(tmp_path, **kw):
    kw.setdefault("data_dir", tmp_path / "data")
    kw.setdefault("quality_eccentricity_max", 0.60)
    return PhotonScriptConfig(_env_file=None, **kw)


def _tgt(name="Heart Nebula"):
    return NinaSequenceTarget(
        name=name, ra_hours=2.55, dec_degrees=61.5, start_guiding=False,
        exposures=[ExposurePlan(filter_type=FilterType.HA, exposure_seconds=300,
                                count=10, gain=200, offset=256)])


def _dsos(seq: dict) -> list:
    out = []

    def walk(d):
        if isinstance(d, dict):
            if "DeepSkyObjectContainer" in str(d.get("$type", "")):
                out.append(d.get("Name"))
            for v in d.values():
                walk(v)
        elif isinstance(d, list):
            for v in d:
                walk(v)
    walk(seq)
    return out


def _events(tmp_path, kind="arm_recipe"):
    out = []
    for p in (tmp_path / "data" / "runs").glob("*_events.jsonl"):
        out += [json.loads(x) for x in p.read_text().splitlines()]
    return [e for e in out if e.get("kind") == kind]


@pytest.fixture
def armer(tmp_path, monkeypatch):
    """An Armer for night 2026-10-06 (dusk past), tonight = Heart Nebula,
    the app pinned to its config, a fixed tracking-test field, fake ninaAPI
    (every call ok) and a notify recorder."""
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler import target_planner
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(nsj, "_gen_cfg_cache", cfg)
    monkeypatch.setattr(nsj, "_filter_names_cache", cfg.filter_name_map())

    class _Store:
        projects: dict = {}
    monkeypatch.setattr(app_mod, "_store", _Store())
    monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
    monkeypatch.setattr(app_mod, "_tracking_test_field",
                        lambda *a, **k: dict(TT_FIELD))
    monkeypatch.setattr(target_planner, "plan_night_sequence",
                        lambda projects, config, now: [_tgt()])
    monkeypatch.chdir(tmp_path)
    a = armer_mod.Armer(cfg)
    a.plan = {"night_of": "2026-10-06", "dusk_utc": "2026-10-07T01:30:00Z",
              "dawn_utc": "2026-10-07T11:30:00Z"}
    monkeypatch.setattr(a, "_calibration_slot", lambda targets, now: None)
    a.calls, a.pushes, a.tree = [], [], None

    async def _nina(key, **kw):
        a.calls.append(key)
        return {"Success": True}

    async def _read():
        a.calls.append("state")
        return a.tree, None

    async def _notify(config, msg, **k):
        a.pushes.append((msg, k.get("priority", 0)))
        return True

    async def _none(*_a, **_k):
        return None
    monkeypatch.setattr(a, "_nina", _nina)
    monkeypatch.setattr(a, "_watch_read_state", _read)
    monkeypatch.setattr(a, "_send_block_alerts", _none)
    monkeypatch.setattr(a, "_dispatch_piggyback_companion", _none)
    monkeypatch.setattr(armer_mod, "notify", _notify)
    return a


def _arm_recipe(a, rid=TT, opts=None):
    rec, err = sd.arm_recipe(rid, opts)
    assert err is None
    a.recipe = {**rec, "night": a.plan["night_of"], "status": "armed"}
    return a.recipe


def _dispatched(a) -> dict:
    return json.loads(a.sequence_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- validation

class TestArmRecipe:
    def test_none(self):
        assert sd.arm_recipe(None) == (None, None)
        assert sd.arm_recipe("") == (None, None)
        assert sd.arm_recipe("none") == (None, None)

    def test_unknown_recipe(self):
        rec, err = sd.arm_recipe("make_coffee")
        assert rec is None and "unknown recipe" in err

    @pytest.mark.parametrize("n,want", [(10, 10), (1, 3), (999, 300), ("12", 12)])
    def test_points_clamp_to_the_mapping_limits(self, n, want):
        rec, err = sd.arm_recipe(TPM, {"points": n})
        assert err is None and rec["opts"] == {"points": want}
        assert rec["label"] == f"TPoint mapping ({want})"

    def test_add_mode(self):
        rec, _ = sd.arm_recipe(TPM, {"add": "AUTO"})
        assert rec["opts"] == {"add": "auto"} and rec["label"] == "TPoint mapping"
        assert "add must be" in sd.arm_recipe(TPM, {"add": "yes"})[1]
        assert "whole number" in sd.arm_recipe(TPM, {"points": "ten"})[1]

    def test_options_only_for_tpoint(self):
        rec, err = sd.arm_recipe(TT, {"points": 10})
        assert rec is None and "not allowed" in err
        assert "not allowed" in sd.arm_recipe(TPM, {"exposure_s": 3})[1]
        assert "object" in sd.arm_recipe(TT, [1])[1]
        rec, err = sd.arm_recipe(TT, {"points": None, "add": ""})   # blanks
        assert err is None and rec == {"id": TT, "label": "tracking test",
                                       "opts": {}}


class TestApi:
    def _post(self, monkeypatch, body):
        from photonscript.scheduler import app

        seen = {}

        class _A:
            async def arm(self, guiding=None, recipe=None):
                seen.update(guiding=guiding, recipe=recipe)
                return {"state": "ARMED", "recipe": recipe}

            async def disarm(self):
                seen["disarm"] = True
                return {"state": "DISARMED"}
        monkeypatch.setattr(app, "get_armer", lambda: _A())

        class _Req:
            async def json(self):
                return body
        res = asyncio.run(app.api_arm(_Req()))
        return res, seen

    def test_bad_recipe_is_400_and_never_arms(self, monkeypatch):
        res, seen = self._post(monkeypatch, {"armed": True, "recipe": "x"})
        assert res.status_code == 400 and not seen
        res, seen = self._post(monkeypatch, {"armed": True, "recipe": TT,
                                             "recipe_opts": {"points": 5}})
        assert res.status_code == 400 and not seen

    def test_recipe_reaches_the_armer(self, monkeypatch):
        res, seen = self._post(monkeypatch, {
            "armed": True, "guiding": "guided", "recipe": TPM,
            "recipe_opts": {"points": 10, "add": "auto"}})
        assert seen["guiding"] == "guided"
        assert seen["recipe"] == {"id": TPM, "label": "TPoint mapping (10)",
                                  "opts": {"points": 10, "add": "auto"}}

    def test_plain_arm_has_no_recipe(self, monkeypatch):
        _, seen = self._post(monkeypatch, {"armed": True})
        assert seen["recipe"] is None


# ------------------------------------------------------------------ dispatch

async def test_arm_with_recipe_dispatches_and_starts_the_recipe(armer, tmp_path):
    a = armer
    _arm_recipe(a)
    assert await a._dispatch_and_start() is True
    seq = _dispatched(a)
    assert seq["Name"] == "PhotonScript_20261006_TT_M_2_then_Heart_Nebula"
    assert _dsos(seq)[:2] == ["Tracking test M 2", "Heart Nebula"]
    # stopped, loaded and STARTED by the armer (no hand Start)
    assert a.calls == ["sequence_stop", "sequence_load", "sequence_start"]
    r = a.recipe
    assert r["status"] == "dispatched" and r["container"] == "Tracking test M 2"
    assert r["sequence"] == seq["Name"] and r["field"]["name"] == "M 2"
    saved = json.loads((tmp_path / "data" / "armer_state.json").read_text())
    assert saved["recipe"]["status"] == "dispatched"
    assert a.status()["recipe"]["status"] == "dispatched"
    assert [e["value"] for e in _events(tmp_path)] == ["dispatched"]
    assert any("dispatched and started" in m for m, _ in a.pushes)


async def test_lint_failure_falls_back_to_the_normal_plan_and_pages_once(
        armer, tmp_path, monkeypatch):
    from photonscript.scheduler import sequence_lint as sl
    real = sl.lint

    def lint(seq, **kw):
        res = real(seq, **kw)
        if "Tracking test M 2" in _dsos(seq):
            res.error("test-rule", "the recipe would not run")
        return res
    monkeypatch.setattr(sl, "lint", lint)
    a = armer
    _arm_recipe(a)
    assert await a._dispatch_and_start() is True
    seq = _dispatched(a)
    assert _dsos(seq) == ["Heart Nebula"]          # the normal plan
    assert seq["Name"] == "PhotonScript_20261006"
    assert a.calls[-1] == "sequence_start"
    assert a.recipe["status"] == "fallback"
    assert "the recipe would not run" in a.recipe["detail"]
    pages = [(m, p) for m, p in a.pushes if "NOT run tonight" in m]
    assert len(pages) == 1 and pages[0][1] == 1
    # a re-dispatch neither retries the recipe nor pages again
    a.pushes.clear()
    assert await a._dispatch_and_start(companion=False, fail_state=None)
    assert _dsos(_dispatched(a)) == ["Heart Nebula"]
    assert not [m for m, _ in a.pushes if "NOT run tonight" in m]
    assert [e["value"] for e in _events(tmp_path)] == ["fallback"]


async def test_build_failure_falls_back(armer, monkeypatch):
    from photonscript.scheduler import app as app_mod

    def boom(*a, **k):
        raise RuntimeError("no field")
    monkeypatch.setattr(app_mod, "_tracking_test_field", boom)
    a = armer
    _arm_recipe(a)
    assert await a._dispatch_and_start() is True
    assert _dsos(_dispatched(a)) == ["Heart Nebula"]
    assert a.recipe["status"] == "fallback"
    assert "could not build" in a.recipe["detail"]


def _tree(status):
    return {"Response": [{"Name": "Targets_Container", "Status": "RUNNING",
                          "Items": [{"Name": "Tracking test M 2_Container",
                                     "Status": status},
                                    {"Name": "Heart Nebula_Container",
                                     "Status": "RUNNING"}]}]}


@pytest.mark.parametrize("path", ["unsafe_resume", "operator_resume", "restart"])
async def test_redispatch_never_repeats_the_recipe(armer, tmp_path, path):
    """Every re-dispatch path goes through _dispatch_and_start(companion=
    False, fail_state=None): the one-shot test is left out once dispatched,
    finished or not."""
    a = armer
    _arm_recipe(a)
    assert await a._dispatch_and_start() is True
    a.tree = _tree("RUNNING")      # interrupted mid-test (e.g. unsafe)
    a.calls.clear()
    assert await a._dispatch_and_start(companion=False, fail_state=None)
    assert a.calls[:2] == ["state", "sequence_stop"]   # progress read first
    assert _dsos(_dispatched(a)) == ["Heart Nebula"]
    assert a.recipe["status"] == "dispatched"
    a.tree = _tree("FINISHED")
    assert await a._dispatch_and_start(companion=False, fail_state=None)
    assert _dsos(_dispatched(a)) == ["Heart Nebula"]
    assert a.recipe["status"] == "done" and a.recipe["done_utc"]
    assert [e["value"] for e in _events(tmp_path)] == ["dispatched", "done"]


async def test_resume_paths_use_the_shared_redispatch():
    """The re-dispatch paths named in the ticket all call
    _dispatch_and_start(companion=False, fail_state=None), the path the
    test above covers."""
    import inspect
    src = inspect.getsource(armer_mod.Armer)
    for fn in ("resume", "_restart_dispatch", "_resume_after_safety_stop"):
        body = inspect.getsource(getattr(armer_mod.Armer, fn))
        assert "_dispatch_and_start(companion=False, fail_state=None)" in body, fn
    assert src.count("await self._consume_recipe()") == 1


async def test_disarm_clears_the_recipe(armer, tmp_path):
    a = armer
    _arm_recipe(a)
    a.state = "ARMED"
    out = await a.disarm()
    assert a.recipe is None and out["recipe"] is None
    saved = json.loads((tmp_path / "data" / "armer_state.json").read_text())
    assert saved["recipe"] is None


async def test_arm_stores_recipe_for_tonight_and_a_plain_arm_clears_it(
        armer, tmp_path, monkeypatch):
    from photonscript.scheduler import night_plan
    a = armer
    a.config.connect_all_on_arm = False
    a.config.cooler_off_until_precool = False
    a.config.phd2_audit_enabled = False
    a.config.thesky_audit_enabled = False
    a.config.app_lifecycle_alert = False
    monkeypatch.setattr(night_plan, "build_night_plan", lambda cfg: {
        "night_of": "2026-10-06", "preconfig_utc": "2026-10-07T00:30:00Z",
        "dusk_utc": "2026-10-07T01:30:00Z", "dawn_utc": "2099-01-01T11:30:00Z",
        "targets": ["Heart Nebula"], "dark_hours": 9.5})

    async def _quiet():
        return None
    monkeypatch.setattr(a, "_check_guider_at_arm", _quiet)
    monkeypatch.setattr(a, "_run", _quiet)
    monkeypatch.setattr(a, "_start_nina2_mount_check", lambda reason: None)
    rec, _ = sd.arm_recipe(TPM, {"points": 10})
    out = await a.arm(guiding="unguided", recipe=rec)
    assert out["state"] == "ARMED"
    assert out["recipe"] == {"id": TPM, "label": "TPoint mapping (10)",
                             "opts": {"points": 10}, "night": "2026-10-06",
                             "status": "armed"}
    assert "with TPoint mapping (10) first" in a.detail
    saved = json.loads((tmp_path / "data" / "armer_state.json").read_text())
    assert saved["recipe"]["id"] == TPM
    # restored after a restart, still tonight's
    b = armer_mod.Armer(a.config)
    monkeypatch.setattr(armer_mod.asyncio, "create_task",
                        lambda c: (c.close(), None)[1])
    assert b.restore() is True and b.recipe == a.recipe
    # another night's record is not shown or run
    b.plan = {**b.plan, "night_of": "2026-10-07"}
    assert b._recipe_tonight() is None and b.status()["recipe"] is None
    # a plain re-arm drops it
    out = await a.arm(guiding="unguided")
    assert out["recipe"] is None and a.recipe is None


# ------------------------------------------------------------- preview parity

async def test_preview_builds_the_same_sequence_as_the_armed_dispatch(
        armer, monkeypatch):
    from photonscript.scheduler import app as app_mod
    a = armer
    assert a._dispatch() is True           # tonight's normal night
    normal = a.sequence_path.read_text(encoding="utf-8")
    name = json.loads(normal)["Name"]
    # the preview's night carries the ARMED guiding mode, as the dispatch
    monkeypatch.setattr(app_mod, "_tonight_sequence", lambda now_mode=False: (
        name, normal, a._use_guiding(), a._unguided_dither()))
    _arm_recipe(a)
    assert a._dispatch() is True
    armed = _dispatched(a)
    built = rt.build_rc16(TT)
    assert built["seq"] == armed
    assert built["lint"].ok
    # and the endpoint shows the same lint and container tree
    res = await rt.api_sideload_preview(recipe=TT, exclude=[], at="")
    rc = res["rigs"]["rc16"]
    assert rc["name"] == armed["Name"] and rc["lint"]["ok"]
    assert rc["tree"] == sd.container_tree(armed)


# ------------------------------------------------------- TPoint per-arm options

def test_tpoint_points_and_add_reach_the_sequence(tmp_path, monkeypatch):
    from photonscript.scheduler import tpoint_mapping as tm
    script = tmp_path / "tpoint-sample.cmd"
    script.write_text("@echo off\n")
    cfg = _cfg(tmp_path, tpoint_sample_script=str(script))
    monkeypatch.setattr(nsj, "_gen_cfg_cache", cfg)
    monkeypatch.setattr(nsj, "_filter_names_cache", cfg.filter_name_map())
    monkeypatch.setattr(tm, "moon_track", lambda *a, **k: None)
    run = tm.build(cfg, "2026-10-07T03:00:00Z", points=10, add="auto")
    assert run["field"]["points"] == 10 and run["params"]["points"] == 10
    assert "TPoint add auto (this arm)" in run["field"]["summary"]
    text = run["test_json"]
    assert text.count("--add auto") == 10
    plain = tm.build(cfg, "2026-10-07T03:00:00Z")
    assert "--add" not in plain["test_json"]
    assert plain["field"]["points"] == cfg.tpoint_mapping_points


def test_script_args_and_sample_mode_override():
    from photonscript.telescope_agent import tpoint_sample as ts
    assert nsj.tpoint_script_args(1, 10, 45, 120, "east").endswith("--side east")
    assert nsj.tpoint_script_args(1, 10, 45, 120, "east", "auto").endswith(
        "--side east --add auto")
    assert "--add" not in nsj.tpoint_script_args(1, 10, 45, 120, "east", "x")

    class C:
        tpoint_sample_add = "off"
    assert ts.add_mode(C()) == "off"
    assert ts.add_mode(C(), "auto") == "auto"
    assert ts.add_mode(C(), "bogus") == "off"
    C.tpoint_sample_add = "auto"
    assert ts.add_mode(C(), "off") == "off"


def test_cli_passes_add_through():
    src = (ROOT / "photonscript/cli.py").read_text(encoding="utf-8")
    assert '"--add"' in src and "add=add or None" in src


async def test_preview_validates_points(armer):
    res = await rt.api_sideload_preview(recipe=TT, exclude=[], at="", points=10)
    assert res.status_code == 400


# ------------------------------------------------------------------ dashboard

def test_dashboard_arm_select_and_card():
    html = (ROOT / "photonscript/scheduler/templates/dashboard.html").read_text(
        encoding="utf-8")
    assert 'id="armRecipe"' in html and 'id="armRecipePoints"' in html
    for rid in sd.RECIPES:
        assert f'value="{rid}"' in html
    assert "TPoint mapping (N)" in html and "body.recipe = rid" in html
    assert "With recipe: " in html


@pytest.mark.parametrize("rel", [
    "photonscript/scheduler/armer.py", "photonscript/scheduler/sideload.py",
    "photonscript/scheduler/routers/sideload.py",
    "photonscript/scheduler/tpoint_mapping.py",
    "tests/test_scheduler/test_ps181_arm_recipe.py"])
def test_new_text_is_ascii_without_em_dashes(rel):
    # armer.py predates the ASCII rule: check only the PS-181 lines
    text = (ROOT / rel).read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines()
             if rel != "photonscript/scheduler/armer.py" or "recipe" in ln.lower()]
    bad = [ln for ln in lines if any(ord(c) > 127 for c in ln)]
    assert not bad, bad[:3]
