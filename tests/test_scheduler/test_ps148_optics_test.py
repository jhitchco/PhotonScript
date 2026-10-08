"""PS-148: through-focus optics test (astigmatism / collimation check).

The generator (steps named "Optics test <field> <filter> <offset>", focuser
moves that return to best focus, no AF triggers, no guiding), its lint rule,
the sideload recipe optics_through_focus, and the report's verdicts from
synthetic star sidecars: an axis that flips across focus (astigmatism, on
axis or only off axis), a constant axis (tracking / mechanical), round at
best focus (defocus only), and a soft side that swaps across focus (tilt).
"""
import asyncio
import json
import math
from pathlib import Path

import numpy as np
import pytest

from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import optics_test as ot
from photonscript.scheduler import sideload as sd
from photonscript.scheduler.sequence_lint import lint
from photonscript.shared import star_table
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.target_names import canonical_target

ROOT = Path(__file__).resolve().parents[2]
NIGHT = "2026-10-06"
W, H = 6224, 4168
SCALE = 0.236


def _cfg(tmp_path, **kw):
    kw.setdefault("quality_eccentricity_max", 0.60)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


@pytest.fixture(autouse=True)
def _pinned(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(nsj, "_gen_cfg_cache", cfg)
    monkeypatch.setattr(nsj, "_filter_names_cache", cfg.filter_name_map())
    monkeypatch.chdir(tmp_path)
    return cfg


def _walk(n):
    if isinstance(n, dict):
        yield n
        for v in n.values():
            yield from _walk(v)
    elif isinstance(n, list):
        for x in n:
            yield from _walk(x)


def _seq(**kw):
    return json.loads(nsj.generate_optics_test_json("M 2", 21.56, -0.82, **kw))


def _named(seq, suffix):
    return [d for d in _walk(seq) if str(d.get("Name") or "").endswith(suffix)]


# ------------------------------------------------------------- generator

class TestGenerator:
    def test_steps_named_and_ordered(self):
        seq = _seq()
        steps = _named(seq, nsj.OPTICS_STEP_SUFFIX)
        names = [s["Target"]["TargetName"] for s in steps]
        assert names == ["Optics test M 2 L 0", "Optics test M 2 L -300",
                         "Optics test M 2 L -150", "Optics test M 2 L +150",
                         "Optics test M 2 L +300"]
        for s in steps:
            assert "DeepSkyObjectContainer" in s["$type"]
            exps = [d for d in _walk(s) if "TakeExposure" in d.get("$type", "")]
            assert exps and all(e["ExposureTime"] == 45.0 for e in exps)
            loop = s["Items"]["$values"][0]["Conditions"]["$values"][0]
            assert loop["Iterations"] == 2

    def test_focuser_moves_return_to_best_focus(self):
        sweep = _named(_seq(), nsj.TARGET_OPTICS_SWEEP_SUFFIX)[0]
        moves = [d["RelativePosition"] for d in _walk(sweep)
                 if "MoveFocuserRelative" in d.get("$type", "")]
        assert moves == [-300, 150, 300, 150, -300] and sum(moves) == 0
        # only outward moves between the first and the last
        assert all(m > 0 for m in moves[1:-1])

    def test_ha_pass_applies_and_undoes_the_filter_offset(self, _pinned,
                                                          monkeypatch):
        monkeypatch.setattr(_pinned.__class__, "focus_offset_map",
                            lambda self: {"L": 0, "Ha": -187})
        seq = json.loads(nsj.generate_optics_test_json(
            "M 2", 21.56, -0.82, filters=["L", "Ha"], offsets=[-200, 200],
            exposure_s=30, nb_exposure_s=90))
        sweep = _named(seq, nsj.TARGET_OPTICS_SWEEP_SUFFIX)[0]
        moves = [d["RelativePosition"] for d in _walk(sweep)
                 if "MoveFocuserRelative" in d.get("$type", "")]
        assert sum(moves) == 0
        ha = [s for s in _named(seq, nsj.OPTICS_STEP_SUFFIX)
              if " Ha " in s["Target"]["TargetName"]]
        assert [s["Target"]["TargetName"] for s in ha] == [
            "Optics test M 2 Ha 0", "Optics test M 2 Ha -200",
            "Optics test M 2 Ha +200"]
        exps = [d["ExposureTime"] for s in ha for d in _walk(s)
                if "TakeExposure" in d.get("$type", "")]
        assert set(exps) == {90.0}

    def test_no_guiding_no_af_triggers_and_lint_passes(self):
        seq = _seq()
        test = [d for d in _walk(seq) if d.get("Name") == "Optics test M 2"][0]
        types = [d.get("$type", "") for d in _walk(test)]
        assert not any("StartGuiding" in t for t in types)
        assert any("StopGuiding" in t for t in types)
        sweep = _named(seq, nsj.TARGET_OPTICS_SWEEP_SUFFIX)[0]
        assert not any("Autofocus" in d.get("$type", "") and "Trigger" in
                       d.get("$type", "") for d in _walk(sweep))
        assert not any(d.get("AfterExposures", 0) > 0 for d in _walk(test)
                       if "DitherAfterExposures" in d.get("$type", ""))
        r = lint(seq, guided=False)
        assert r.ok, [f.detail for f in r.findings]
        assert any(f.rule == "optics-test" and f.level == "WARN"
                   for f in r.findings)

    def test_offsets_parse_and_duration(self):
        assert nsj._optics_test_offsets(None) == [-300, -150, 150, 300]
        assert nsj._optics_test_offsets(["300", 0, -300, "x", 300]) == [-300, 300]
        d = nsj.optics_test_duration_s(["L"], [-300, -150, 150, 300], 45, 120, 2)
        assert 10 * 60 < d < 25 * 60

    def test_names_strip_and_parse(self):
        assert nsj.optics_step_name("M 2", "L", 150) == "Optics test M 2 L +150"
        assert nsj.optics_step_name("Optics test M 2", "L", 0) == \
            "Optics test M 2 L 0"
        assert ot.parse_name("Optics test M 2 L -300") == {
            "field": "M 2", "filter": "L", "offset": -300}
        assert ot.parse_name("Optics test NGC 7789 Ha +150 through-focus "
                             "step_Container")["offset"] == 150
        assert ot.parse_name("M 2") is None
        assert canonical_target("Optics test M 2 L +150 through-focus "
                                "step_Container") == "Optics test M 2 L +150"
        assert ot.is_optics_test("Optics test M 2 L 0")
        assert not ot.is_optics_test("Tracking test M 2")


class TestLint:
    def _test(self, seq):
        return [d for d in _walk(seq) if d.get("Name") == "Optics test M 2"][0]

    def test_unbalanced_moves_fail(self):
        seq = _seq()
        sweep = _named(seq, nsj.TARGET_OPTICS_SWEEP_SUFFIX)[0]
        last = [d for d in _walk(sweep)
                if "MoveFocuserRelative" in d.get("$type", "")][-1]
        last["RelativePosition"] = -100
        r = lint(seq, guided=False)
        assert not r.ok and any(f.rule == "optics-test" and "off focus"
                                in f.detail for f in r.findings)

    def test_af_trigger_and_guiding_fail(self):
        seq = _seq()
        step = _named(seq, nsj.OPTICS_STEP_SUFFIX)[0]
        step["Triggers"]["$values"].append(
            {"$type": "NINA.Sequencer.Trigger.Autofocus."
                      "AutofocusAfterHFRIncreaseTrigger, NINA.Sequencer"})
        step["Items"]["$values"].append(
            {"$type": "NINA.Sequencer.SequenceItem.Guider.StartGuiding, "
                      "NINA.Sequencer"})
        r = lint(seq, guided=False)
        details = [f.detail for f in r.findings if f.rule == "optics-test"
                   and f.level == "ERROR"]
        assert any("autofocus trigger" in d for d in details)
        assert any("StartGuiding" in d for d in details)

    def test_steps_are_not_linted_as_targets(self):
        r = lint(_seq(), guided=False)
        assert not any("through-focus step" in f.detail
                       and f.rule in ("platesolve", "autofocus", "altitude")
                       for f in r.findings)


# ------------------------------------------------------------- sideload

def _night():
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget
    targets = [NinaSequenceTarget(
        name=n, ra_hours=ra, dec_degrees=dec,
        exposures=[ExposurePlan(filter_type=FilterType.HA, exposure_seconds=300,
                                count=10, gain=200, offset=256)])
        for n, ra, dec in (("Heart Nebula", 2.55, 61.5),
                           ("Pacman Nebula", 0.88, 56.6))]
    s = build_sequence_for_night("PhotonScript_20261006", targets)
    return json.loads(nsj.generate_nina_json(s))


class TestSideload:
    def test_splice_puts_the_test_before_the_night_loop(self):
        seq = sd.splice_optics_test(_night(), _seq(), exclude=["Pacman Nebula"],
                                    name="X")
        assert sd.target_names(seq) == ["Optics test M 2", "Heart Nebula"]
        text = json.dumps(seq)
        assert sd.SPLICED_OT_NOTE in text and nsj.OPTICS_TEST_PARK_NOTE not in text
        r = lint(seq, guided=False)
        assert r.ok, [f.detail for f in r.findings if f.level == "ERROR"]
        warn = [f.detail for f in r.findings if f.rule == "optics-test"]
        assert warn and "Tonight's targets follow" in warn[0]

    def test_splice_name_tag(self):
        assert sd.splice_name("2026-10-06", "M 2", [], "OT") == \
            "PhotonScript_20261006_OT_M_2_then_nothing"

    def test_preview_recipe(self, tmp_path, monkeypatch):
        from photonscript.scheduler import app
        from photonscript.scheduler.routers import sideload as rt
        cfg = _cfg(tmp_path, piggyback_enabled=True)

        class _Armer:
            state = "DISARMED"
        monkeypatch.setattr(app, "get_config", lambda: cfg)
        monkeypatch.setattr(app, "get_armer", lambda: _Armer())
        monkeypatch.setattr(app, "_stored_projects", lambda: {})
        monkeypatch.setattr(app, "_tonight_sequence", lambda now_mode=False: (
            "PhotonScript_20261006", json.dumps(_night()), False, False))
        monkeypatch.setattr(ot, "pick_field", lambda *a, **k: {
            "name": "M 2", "ra_hours": 21.56, "dec_degrees": -0.82,
            "alt_deg": 60.0, "est_minutes": 16, "source": "test"})
        assert sd.RECIPE_OPTICS_THROUGH_FOCUS in sd.RECIPES
        b = asyncio.run(rt.api_sideload_preview(
            recipe="optics_through_focus", exclude=["Pacman Nebula"], at=""))
        rc = b["rigs"]["rc16"]
        assert rc["lint"]["ok"], rc["lint"]["text"]
        assert rc["targets"] == ["Optics test M 2", "Heart Nebula"]
        assert rc["name"] == "PhotonScript_20261006_OT_M_2_then_Heart_Nebula"
        assert rc["field"]["name"] == "M 2"
        assert b["rigs"]["piggyback"]["lint"]["ok"]


# ------------------------------------------------------------- report

def _table(theta_fn, ecc_fn, fwhm_fn=lambda u, v: 3.0, seed=1):
    """Synthetic sidecar over the full frame, rows shuffled like a
    flux-sorted table. theta_fn / ecc_fn / fwhm_fn take (u, v, rng)."""
    rng = np.random.default_rng(seed)
    rows = []
    for j in range(14):
        for i in range(20):
            x, y = (i + 0.5) * W / 20, (j + 0.5) * H / 14
            u, v = (x - W / 2) / (W / 2), (y - H / 2) / (H / 2)
            rows.append((round(x, 1), round(y, 1),
                         round(fwhm_fn(u, v) / (2 * SCALE), 3),
                         ecc_fn(u, v), theta_fn(u, v, rng)))
    rng.shuffle(rows)
    return {"v": 1, "rig": "rc16", "grader": "test",
            "ecc_def": "sqrt(1-(b/a)^2)", "w": W, "h": H, "n": len(rows),
            "x": [r[0] for r in rows], "y": [r[1] for r in rows],
            "hfr": [r[2] for r in rows], "ecc": [r[3] for r in rows],
            "theta": [r[4] for r in rows]}


def _rand(u, v, rng):
    return float(rng.uniform(-math.pi / 2, math.pi / 2))


def _fixed(deg):
    return lambda u, v, rng: math.radians(deg)


def _write_night(cfg, spec, flt="L", field="M 2"):
    """spec: {offset: (theta_fn, ecc_fn[, fwhm_fn])}, two subs per step."""
    recs = []
    k = 0
    for off, fns in spec.items():
        for rep in range(2):
            k += 1
            f = f"LIGHT/opt_{flt}_{off}_{rep}.fits"
            star_table.write(cfg, NIGHT, f, _table(*fns, seed=k))
            recs.append({"file": f, "rig": "rc16", "filter": flt,
                         "exp_s": 45.0, "hfr": 3.0, "ecc": 0.5,
                         "target": nsj.optics_step_name(field, flt, off)})
    return recs


def _const(e):
    return lambda u, v: e


def _round_best():
    return (_rand, _const(0.25))


class TestReport:
    def test_axis_flip_through_the_center_is_astigmatism(self, tmp_path):
        cfg = _cfg(tmp_path)
        recs = _write_night(cfg, {
            0: _round_best(),
            -300: (_fixed(33), _const(0.75)), -150: (_fixed(33), _const(0.6)),
            150: (_fixed(123), _const(0.6)), 300: (_fixed(123), _const(0.75))})
        rep = ot.build_report(cfg, NIGHT, records=recs)
        f = rep["filters"][0]
        assert f["verdict"] == ot.ASTIGMATISM, f
        assert f["flip_deg"] == pytest.approx(90, abs=2)
        assert f["axis_inside"] == pytest.approx(33, abs=1)
        assert any("center flips too" in d for d in f["detail"])
        steps = {s["offset"]: s for s in f["steps"]}
        assert sorted(steps) == [-300, -150, 0, 150, 300]
        assert steps[-300]["ecc_median"] == pytest.approx(0.75)
        assert steps[-300]["zones"]["TL"]["axis_deg"] == pytest.approx(33, abs=1)
        assert steps[0]["hfr_median"] == pytest.approx(3.0 / (2 * SCALE), abs=0.01)
        assert "L: Astigmatism" in rep["headline"]
        assert "astigmatism" in ot.format_report(rep)

    def test_only_corners_flip_is_field_astigmatism(self, tmp_path):
        cfg = _cfg(tmp_path)

        def zoned(deg):
            def fn(u, v, rng):
                if abs(u) < 1 / 3 and abs(v) < 1 / 3:
                    return _rand(u, v, rng)
                return math.radians(deg)
            return fn
        recs = _write_night(cfg, {
            0: _round_best(),
            -300: (zoned(40), _const(0.7)), 300: (zoned(130), _const(0.7))})
        f = ot.build_report(cfg, NIGHT, records=recs)["filters"][0]
        assert f["verdict"] == ot.ASTIGMATISM
        assert any("Only off-axis zones flip" in d for d in f["detail"])

    def test_same_axis_everywhere_is_mechanical(self, tmp_path):
        cfg = _cfg(tmp_path)
        recs = _write_night(cfg, {
            0: (_fixed(33), _const(0.65)),
            -300: (_fixed(35), _const(0.5)), 300: (_fixed(31), _const(0.5))})
        f = ot.build_report(cfg, NIGHT, records=recs)["filters"][0]
        assert f["verdict"] == ot.CONSTANT, f
        assert "tracking" in f["headline"].lower()
        assert any("fixed-length smear" in d for d in f["detail"])

    def test_round_at_focus_is_defocus_only(self, tmp_path):
        cfg = _cfg(tmp_path)
        recs = _write_night(cfg, {0: _round_best(),
                                  -300: (_rand, _const(0.3)),
                                  300: (_rand, _const(0.3))})
        f = ot.build_report(cfg, NIGHT, records=recs)["filters"][0]
        assert f["verdict"] == ot.DEFOCUS_ONLY, f

    def test_soft_side_swapping_across_focus_is_tilt(self, tmp_path):
        cfg = _cfg(tmp_path)
        recs = _write_night(cfg, {
            0: (_rand, _const(0.25), lambda u, v: 3.0 * (1 - 0.12 * (u + v))),
            -300: (_rand, _const(0.3), lambda u, v: 6.0 * (1 + 0.2 * u)),
            300: (_rand, _const(0.3), lambda u, v: 6.0 * (1 - 0.2 * u))})
        f = ot.build_report(cfg, NIGHT, records=recs)["filters"][0]
        assert f["tilt"]["verdict"] == "tilt", f["tilt"]
        assert f["tilt"]["best_soft_corner"] == "TL"
        assert any(d.startswith("Tilt: the soft side swaps") for d in f["detail"])

    def test_soft_corner_without_swap_is_not_plain_tilt(self, tmp_path):
        cfg = _cfg(tmp_path)
        soft_tl = lambda u, v: 3.0 * (1 - 0.15 * (u + v))   # noqa: E731
        recs = _write_night(cfg, {0: (_rand, _const(0.25), soft_tl),
                                  -300: (_rand, _const(0.3), soft_tl),
                                  300: (_rand, _const(0.3), soft_tl)})
        f = ot.build_report(cfg, NIGHT, records=recs)["filters"][0]
        assert f["tilt"]["verdict"] == "soft-corner"

    def test_no_subs_and_no_sidecars(self, tmp_path):
        cfg = _cfg(tmp_path)
        rep = ot.build_report(cfg, NIGHT, records=[
            {"target": "Heart Nebula", "filter": "L", "exp_s": 300}])
        assert rep["n_subs"] == 0 and "No subs named" in rep["headline"]
        rep = ot.build_report(cfg, NIGHT, records=[
            {"file": "a.fits", "rig": "rc16", "filter": "L", "exp_s": 45,
             "ecc": 0.3, "hfr": 3.0, "target": "Optics test M 2 L 0"}])
        f = rep["filters"][0]
        assert f["steps"][0]["ecc_median"] == 0.3 and f["verdict"] == ot.UNCLEAR

    def test_filters_kept_apart(self, tmp_path):
        cfg = _cfg(tmp_path)
        recs = (_write_night(cfg, {0: _round_best(), -300: (_rand, _const(0.3)),
                                   300: (_rand, _const(0.3))})
                + _write_night(cfg, {0: _round_best(),
                                     -300: (_fixed(33), _const(0.7)),
                                     300: (_fixed(123), _const(0.7))},
                               flt="Ha"))
        rep = ot.build_report(cfg, NIGHT, records=recs)
        assert [f["filter"] for f in rep["filters"]] == ["L", "Ha"]
        assert [f["verdict"] for f in rep["filters"]] == [ot.DEFOCUS_ONLY,
                                                          ot.ASTIGMATISM]
        assert "Ha: astigmatism" in rep["headline"]


# ------------------------------------------------------------- api / config

def test_api_report_target_and_sequence(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import optics_test as rt
    from photonscript.scheduler.runs import runs_dir
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(app, "_stored_projects", lambda: {})
    recs = _write_night(cfg, {0: _round_best(), -300: (_rand, _const(0.3)),
                              300: (_rand, _const(0.3))})
    p = runs_dir(cfg) / f"{NIGHT}_subs.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    rep = rt.api_optics_test_report(date=NIGHT)
    assert rep["n_subs"] == 6 and rep["filters"][0]["verdict"] == ot.DEFOCUS_ONLY
    tgt = rt.api_optics_test_target(at="2026-10-07T04:00:00Z")
    assert tgt["name"] and tgt["params"]["offsets"] == [-300, -150, 150, 300]
    res = rt.api_optics_test_sequence(name="M 2", ra=21.56, dec=-0.82)
    assert res.status_code == 200
    body = json.loads(res.body)
    assert "Optics test M 2" in json.dumps(body)
    paths = {r.path for r in rt.router.routes}
    assert {"/api/optics-test/report", "/api/optics-test/target",
            "/api/optics-test/sequence"} <= paths
    src = (ROOT / "photonscript/scheduler/app.py").read_text(encoding="utf-8")
    assert "app.include_router(_optics_test_router.router)" in src


def test_config_keys_defaults_and_system_fields():
    from photonscript.scheduler import app
    c = PhotonScriptConfig(_env_file=None)
    assert c.optics_test_offsets == "-300,-150,150,300"
    assert c.optics_test_filters == "L,Ha"
    assert c.optics_test_exposure_s == 45.0
    assert c.optics_test_nb_exposure_s == 120.0
    assert c.optics_test_repeats == 2
    keys = {row[0] for row in app._CONFIG_FIELDS}
    assert {"optics_test_offsets", "optics_test_filters",
            "optics_test_exposure_s", "optics_test_nb_exposure_s",
            "optics_test_repeats"} <= keys
    p = ot.params(_cfg(Path("."), optics_test_offsets="200,-200,0",
                       optics_test_filters="L, Ha", optics_test_repeats=3))
    assert p["offsets"] == [-200, 200] and p["filters"] == ["L", "Ha"]
    assert p["repeats"] == 3


def test_agent_skips_optics_test_subs_in_the_reject_streak():
    src = (ROOT / "photonscript/telescope_agent/agent.py").read_text(
        encoding="utf-8")
    i = src.index("if is_test_target(target_name)")   # PS-152
    assert src.index("self._consecutive_rejects += 1") > i


def test_ui_and_ascii():
    dash = (ROOT / "photonscript/scheduler/templates/dashboard.html").read_text(
        encoding="utf-8")
    assert 'value="optics_through_focus"' in dash
    runs = (ROOT / "photonscript/scheduler/templates/runs.html").read_text(
        encoding="utf-8")
    assert 'id="opticsTest"' in runs and "/api/optics-test/report" in runs
    for p in ("photonscript/scheduler/optics_test.py",
              "photonscript/scheduler/routers/optics_test.py",
              "photonscript/scheduler/sideload.py",
              "photonscript/scheduler/static/js/sideload_panel.js",
              "tests/test_scheduler/test_ps148_optics_test.py"):
        b = (ROOT / p).read_bytes()
        assert b.isascii(), p
    hb = (ROOT / "docs/HANDBOOK.md").read_text(encoding="utf-8")
    assert "Through-focus optics test (PS-148)" in hb
