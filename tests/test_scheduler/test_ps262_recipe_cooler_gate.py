"""PS-262: every recipe's light loops sit behind the PS-61 cooler gate.

2026-11-15 the armer refused the guider_commission recipe at pre-config:
"[cooler-gate] 7 light loop(s) start without the cooler gate before them"
(the narrowband focus test frames). The main plan puts the gate before
every light block; the recipe containers must too, or the recipe falls
back to the normal plan and the test never runs (PS-246 waits on it).

Here the gate is ON (as on the scope PC, where deploy\\cooler-gate.cmd
exists) and every recipe is built offline for a sample night through the
same builder the sideload preview and the armer use
(routers/sideload.build_rc16_from), then linted: no lint errors at all.
"""
import json

import pytest

from photonscript.scheduler import guider_night as gn
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import optics_test as ot
from photonscript.scheduler import sideload as sd
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.sequence_lint import lint
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget

AT = "2026-11-16T02:30:00Z"
CAL_FIELD = {"name": "Cal field", "ra_hours": 22.0, "dec_degrees": 5.0,
             "ha_hours": 0.6}
FIELD = {"name": "M 2", "ra_hours": 21.56, "dec_degrees": -0.82,
         "alt_deg": 60.0, "est_minutes": 16, "source": "test"}


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


def _is_gate(n):
    return _short(n) == "ExternalScript" and "cooler-gate" in str(n.get("Script"))


@pytest.fixture
def gate_on(tmp_path, monkeypatch):
    """The cooler gate on in skip mode with its script present, for both the
    generator config and the lint (which reads PhotonScriptConfig())."""
    script = tmp_path / "cooler-gate.cmd"
    script.write_text("@echo off\n", encoding="ascii")
    gcs = tmp_path / "guider-commission.cmd"
    gcs.write_text("@echo off\n", encoding="ascii")
    monkeypatch.setenv("PS_COOLER_GATE_MODE", "skip")
    monkeypatch.setenv("PS_COOLER_GATE_SCRIPT", str(script))
    cfg = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"),
                             observatory_tz="America/Denver",
                             guider_commission_script=str(gcs))
    assert cfg.cooler_gate_script == str(script)
    monkeypatch.setattr(nsj, "_gen_cfg_cache", cfg)
    monkeypatch.setattr(nsj, "_filter_names_cache", cfg.filter_name_map())
    monkeypatch.chdir(tmp_path)
    return cfg


def _night(guided: bool) -> dict:
    t = NinaSequenceTarget(
        name="Heart Nebula", ra_hours=2.55, dec_degrees=61.5,
        start_guiding=guided,
        exposures=[ExposurePlan(filter_type=FilterType.HA, exposure_seconds=300,
                                count=10, gain=200, offset=256)])
    seq = build_sequence_for_night("PhotonScript_20261115", [t])
    seq.wait_until_local = "19:30:00"
    return json.loads(nsj.generate_nina_json(
        seq, cal_field=CAL_FIELD if guided else None))


@pytest.fixture
def router(gate_on, monkeypatch):
    """routers/sideload with the app's config, projects and field picks
    pinned (no service, no catalog lookups)."""
    from photonscript.scheduler import app
    from photonscript.scheduler import tracking_test as tt
    from photonscript.scheduler.routers import sideload as rt
    monkeypatch.setattr(app, "get_config", lambda: gate_on)
    monkeypatch.setattr(app, "_stored_projects", lambda: {})
    monkeypatch.setattr(ot, "pick_field", lambda *a, **k: dict(FIELD))
    monkeypatch.setattr(tt, "pick_target", lambda *a, **k: dict(FIELD))
    return rt


def _build(rt, recipe, guided):
    return rt.build_rc16_from(_night(guided), "PhotonScript_20261115", guided,
                              False, recipe, (), AT, {})


def _errors(res):
    return [f"{f.rule}: {f.detail}" for f in res.findings if f.level == "ERROR"]


# ------------------------------------------------- every recipe lints clean

@pytest.mark.parametrize("recipe,guided", [
    (sd.RECIPE_GUIDER_COMMISSION, True),
    (sd.RECIPE_GUIDER_COMMISSION, False),
    (sd.RECIPE_OPTICS_THROUGH_FOCUS, False),
    (sd.RECIPE_OPTICS_THROUGH_FOCUS, True),
    (sd.RECIPE_TT_THEN_TONIGHT, False),
    (sd.RECIPE_TPOINT_MAPPING, False),
])
def test_recipe_lints_clean_with_the_gate_on(router, recipe, guided):
    built = _build(router, recipe, guided)
    res = built["lint"]
    assert res.ok, _errors(res)
    assert not [f for f in res.findings if f.rule == "cooler-gate"], \
        [f.detail for f in res.findings if f.rule == "cooler-gate"]
    # the gate is really there (not a lint that skipped the rule)
    test = next(n for n in _walk(built["seq"])
                if n.get("Name") == built["test_container"])
    gates = [n for n in _walk(test) if _is_gate(n)]
    assert gates and all(g["ErrorBehavior"] == 1 for g in gates)


@pytest.mark.parametrize("recipe", [sd.RECIPE_GUIDER_COMMISSION,
                                    sd.RECIPE_OPTICS_THROUGH_FOCUS])
def test_cooler_gate_rule_is_enforced_on_the_recipe(router, recipe):
    """Drop the recipe container's gate(s): the rule must fire on exactly
    those lights (so the clean result above is the gate's doing)."""
    built = _build(router, recipe, recipe == sd.RECIPE_GUIDER_COMMISSION)
    seq = built["seq"]
    test = next(n for n in _walk(seq)
                if n.get("Name") == built["test_container"])
    for n in _walk(test):
        vals = (n.get("Items") or {}).get("$values") \
            if isinstance(n.get("Items"), dict) else None
        if vals:
            vals[:] = [v for v in vals if not (isinstance(v, dict) and _is_gate(v))]
    res = lint(seq, guided=built["guided"], unguided_dither=False)
    hits = [f.detail for f in res.findings if f.rule == "cooler-gate"]
    assert not res.ok and hits and built["test_container"] in hits[0]


# --------------------------------------- the guider_commission container

def test_nb_focus_test_starts_with_the_gate_before_slew_and_af(gate_on):
    spec = gn.build_spec(gate_on, guided=True, opts={},
                         nb_field={"name": "Bubble Nebula", "ra_hours": 23.35,
                                   "dec_degrees": 61.2},
                         star_field=None,
                         script=gate_on.guider_commission_script)
    c = nsj._build_guider_commission_container(spec)
    nb = next(n for n in _walk(c)
              if str(n.get("Name", "")).endswith(nsj.GUIDER_NB_SUFFIX))
    items = nb["Items"]["$values"]
    kinds = [_short(i) for i in items]
    at = next(i for i, it in enumerate(items) if _is_gate(it))
    gate = items[at]
    assert gate["Script"].startswith(f'"{gate_on.cooler_gate_script}" rc16 '
                                     "--setpoint=")
    assert '--label="Bubble Nebula NB focus test"' in gate["Script"]
    assert gate["ErrorBehavior"] == 1 and gate["Attempts"] == 1
    # after the bounded cool, before the slew, the first AF and every frame
    assert kinds[at - 1] == "SequentialContainer"     # _cool_camera_bounded
    assert at < kinds.index("SlewScopeToRaDec") < kinds.index("RunAutofocus")
    first_frames = next(i for i, it in enumerate(items)
                        if str(it.get("Name", "")).endswith(nsj.OPTICS_STEP_SUFFIX))
    assert at < first_frames
    # exactly one gate in the whole recipe container (the guider steps take
    # no lights)
    assert len([n for n in _walk(c) if _is_gate(n)]) == 1


def test_gate_off_leaves_the_container_as_before(gate_on, monkeypatch):
    monkeypatch.setattr(gate_on, "cooler_gate_mode", "off")
    spec = gn.build_spec(gate_on, guided=False, opts={},
                         nb_field={"name": "M 2", "ra_hours": 21.56,
                                   "dec_degrees": -0.82},
                         star_field=None, script="")
    c = nsj._build_guider_commission_container(spec)
    assert not [n for n in _walk(c) if _is_gate(n)]


# ---------------------------------------- pre-arm: report a failing recipe

def test_prearm_recipe_row_pass_fail_and_unknown(router):
    from photonscript.scheduler import prearm as pa
    rec = {"id": sd.RECIPE_GUIDER_COMMISSION, "label": "guider commissioning",
           "status": "armed"}
    good = _build(router, sd.RECIPE_GUIDER_COMMISSION, True)
    r = pa.recipe_row(rec, good, None)
    assert r["status"] == "pass" and r["section"] == "Tonight"
    assert r["id"] == "tonight:recipe" and "lint PASS" in r["current"]
    # the 2026-11-15 failure: lights with no gate
    seq = good["seq"]
    for n in _walk(seq):
        vals = (n.get("Items") or {}).get("$values") \
            if isinstance(n.get("Items"), dict) else None
        if vals:
            vals[:] = [v for v in vals if not (isinstance(v, dict) and _is_gate(v))]
    bad = dict(good, lint=lint(seq, guided=True, unguided_dither=False))
    r = pa.recipe_row(rec, bad, None)
    assert r["status"] == "fail" and r["who"] == "claude"
    assert "[cooler-gate]" in r["current"] and "guider_commission" in r["fix"]
    assert r in pa.attention([r])
    r = pa.recipe_row(rec, None, "ValueError: no script")
    assert r["status"] == "unknown" and "no script" in r["current"]


def test_prearm_run_lints_the_armed_recipe(tmp_path, monkeypatch):
    import asyncio
    from datetime import datetime
    from types import SimpleNamespace
    from photonscript.scheduler import prearm as pa
    from photonscript.scheduler.routers import sideload as rt
    cfg = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"))
    calls = []
    lint_fail = SimpleNamespace(ok=False, findings=[SimpleNamespace(
        level="ERROR", rule="cooler-gate", detail="7 light loop(s) start without")])

    def build_rc16(recipe, exclude=(), at="", opts=None):
        calls.append((recipe, tuple(exclude), at, opts))
        return {"name": "X", "lint": lint_fail}
    monkeypatch.setattr(rt, "build_rc16", build_rc16)

    async def boom(*a, **k):
        raise RuntimeError("offline")
    import photonscript.scheduler.preflight as pre
    import photonscript.scheduler.frozen_sensor as fs
    import photonscript.scheduler.settings_ledger as sl
    monkeypatch.setattr(pre, "run_preflight", boom)
    monkeypatch.setattr(fs, "read_rig", boom)
    monkeypatch.setattr(sl, "take", boom)
    monkeypatch.setattr(pa, "read_windows", lambda: {"ok": False, "note": "test"})
    monkeypatch.setattr(pa, "read_pnp", lambda: ([], None))
    import photonscript.scheduler.mount_ledger as ml
    import photonscript.scheduler.protrack_recheck as pr
    import photonscript.scheduler.tpoint_preflight as tpp

    def offline(*a, **k):
        raise RuntimeError("offline")
    monkeypatch.setattr(ml, "read_thesky", offline)
    monkeypatch.setattr(pr, "recheck", offline)
    monkeypatch.setattr(tpp, "evaluate", offline)

    class _Armer:
        state = "ARMED"
        plan = {"dusk_utc": "2026-11-16T01:10:00Z"}
        recipe = {"id": sd.RECIPE_GUIDER_COMMISSION, "label": "guider commissioning",
                  "opts": {"skip": ["ga"]}, "status": "armed"}

        def _use_guiding(self):
            return True

    rep = asyncio.run(pa.run(cfg, _Armer(), trigger="manual", push=False,
                             connect=False, now=datetime(2026, 11, 15, 19, 0)))
    assert calls == [(sd.RECIPE_GUIDER_COMMISSION, (), "2026-11-16T01:10:00Z",
                      {"skip": ["ga"]})]
    row = next(r for r in rep["rows"] if r["id"] == "tonight:recipe")
    assert row["status"] == "fail" and "[cooler-gate]" in row["current"]
    assert "tonight:recipe" in rep["attention_ids"] and rep["go"] is False

    # a recipe already dispatched (or fallen back) is not rebuilt
    calls.clear()
    _Armer.recipe = dict(_Armer.recipe, status="dispatched")
    rep = asyncio.run(pa.run(cfg, _Armer(), trigger="manual", push=False,
                             connect=False, now=datetime(2026, 11, 15, 19, 0)))
    assert not calls and not [r for r in rep["rows"] if r["id"] == "tonight:recipe"]
