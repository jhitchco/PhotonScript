"""PS-153: `photonscript blend` (RC16 L core into the Piggy-600 color image)."""
import json
import re

import pytest
from typer.testing import CliRunner

from photonscript import cli
from photonscript.integration import blend as bl
from photonscript.integration import pjsr
from photonscript.integration import report as rp
from photonscript.shared import ledger as L


def _touch(p, n=16):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * n)
    return p


def _run(root, name, rig, version, filters, *, linear=(), final=(), campaign="Andromeda Galaxy",
         kind="integration", created="2026-10-05T10:00:00Z"):
    d = root / name
    for f in filters:
        _touch(d / "out" / "master" / f"master_{f}.xisf")
    _touch(d / "out" / "master" / "masterBias.xisf")
    if rig == "piggyback":
        _touch(d / "out" / "master" / "masterOSC.xisf")
    for f in linear:
        _touch(d / "out" / "final" / f"M31_{f}_linear.xisf")
    for f in final:
        _touch(d / "out" / "final" / f"M31_{f}_final.xisf")
    led = L.Ledger(campaign=campaign, rig=rig, run=name, version=version, kind=kind,
                   created_at=created)
    L.save(d / "ledger.json", led)
    return d


@pytest.fixture
def staging(tmp_path):
    root = tmp_path / "Staging"
    _run(root, "M31_piggyback_1", "piggyback", 1, ["OSC"], linear=["OSC"], final=["OSC"])
    _run(root, "M31_piggyback_2", "piggyback", 2, ["OSC"])                 # newest, no finish yet
    _run(root, "M31_rc16_1", "rc16", 1, ["L", "R", "G", "B"], linear=["L", "R", "G", "B"],
         final=["L"])
    _run(root, "M42_rc16_1", "rc16", 3, ["L"], linear=["L"], campaign="Orion Nebula")
    return root


# ----------------------------------------------------------------- PJSR

def test_template_is_ascii_and_rule_clean():
    raw = (pjsr.DEPLOY / bl.TEMPLATE).read_bytes()
    t = raw.decode("ascii")
    assert pjsr.check(t) == ["CONFIG not filled"]
    # pjsr headers come before the solver include mark, the CONFIG after it
    assert t.index("#include <pjsr/DataType.jsh>") < t.index(pjsr.SOLVER_MARK) < t.index(pjsr.CONFIG_MARK)
    # every Crop sits in a function that clears the astrometric solution first
    for m in re.finditer(r"new\s+Crop\b", t):
        body = t[t.rfind("function ", 0, m.start()):m.start()]
        assert "clearWcs(" in body


def test_template_has_the_steps_and_guards():
    t = pjsr.template(bl.TEMPLATE)
    for s in ("ImageSolver", "StarAlignment", "Resample", "ChannelExtraction", "ChannelCombination",
              "CIELab", "CIEXYZ", "wcsAffine", "footprintMask", "linearFit", "_blend_steps.json",
              "timing_pi.csv", "blend.log", "EXIT OK", "_core_linear.xisf", "_blend_linear.xisf",
              "_osc_ab", "typeof ImageSolver === \"undefined\""):
        assert s in t, s


def test_render_fills_config_and_solver(staging):
    o = bl.Options(target="M31", staging_root=staging)
    inp = bl.discover(o)
    cfg = bl.blend_config(o, inp, staging / "Blend" / "x")
    block = '#define USE_SOLVER_LIBRARY true\n#include "C:/PI/src/scripts/AdP/ImageSolver.js"'
    out = bl.render(cfg, block)
    assert pjsr.check(out) == []
    line = next(x for x in out.splitlines() if x.startswith("var CONFIG = "))
    got = json.loads(line[len("var CONFIG = "):-1])
    assert got["weight"] == 0.7 and got["stage"] == "linear" and got["core"] is True
    assert got["rc16"] == [{"filter": "L", "path": pjsr.fwd(inp.rc16["L"])}]
    assert got["osc"]["scale"] == 1.29 and got["rc16_scale"] == 0.236
    assert got["ra_deg"] is not None and abs(got["ra_deg"] - 10.68) < 0.1
    assert out.index("#include <pjsr/SectionBar.jsh>") < out.index(block) < out.index("var CONFIG = ")
    assert "\\" not in got["out"]


def test_render_non_ascii_path_is_escaped(staging):
    o = bl.Options(target="M31", staging_root=staging)
    inp = bl.discover(o)
    cfg = bl.blend_config(o, inp, staging / ("Caf" + chr(0xE9)))
    out = bl.render(cfg)
    assert pjsr.check(out) == [] and "Caf\\u00e9" in out


# ----------------------------------------------------------------- discovery

def test_discover_linear_prefers_finished_runs(staging):
    inp = bl.discover(bl.Options(target="M31", staging_root=staging))
    # v2 is newer but has only the raw master: the finished v1 linear wins
    assert inp.osc_run.version == 1 and inp.osc.name == "M31_OSC_linear.xisf"
    assert not any("raw master" in n for n in inp.notes)
    assert inp.rc16_run.run_dir.name == "M31_rc16_1"           # M42 run (v3) is another target
    assert inp.rc16["L"].name == "M31_L_linear.xisf" and set(inp.rc16) == {"L", "R", "G", "B"}
    assert inp.lum_filters() == ["L"]


def test_discover_final_stage_skips_runs_without_finals(staging):
    inp = bl.discover(bl.Options(target="Andromeda Galaxy", staging_root=staging, stage="final"))
    assert inp.osc_run.version == 1 and inp.osc.name == "M31_OSC_final.xisf"
    assert inp.rc16 == {"L": staging / "M31_rc16_1" / "out" / "final" / "M31_L_final.xisf"}


def test_discover_rgb_only_uses_the_mean(tmp_path):
    root = tmp_path / "S"
    _run(root, "p", "piggyback", 1, ["OSC"])
    _run(root, "r", "rc16", 1, ["B", "G", "R"])
    inp = bl.discover(bl.Options(target="M31", staging_root=root))
    assert inp.lum_filters() == ["R", "G", "B"]
    assert any("mean of R+G+B" in n for n in inp.notes)
    assert inp.rc16["R"].name == "master_R.xisf"               # no finish: raw masters
    assert any("raw master" in n for n in inp.notes)


def test_discover_manifest_only_run_and_ignores_blend_ledgers(tmp_path):
    root = tmp_path / "S"
    d = root / "PS22_smoke"
    _touch(d / "out" / "master" / "master_OSC.xisf")
    _touch(d / "out" / "final" / "M31_OSC_linear.xisf")
    (d / "manifest.json").write_text(json.dumps({"target": "M31", "rig": "piggyback"}), encoding="ascii")
    _run(root, "r", "rc16", 1, ["L"])
    _run(root, "b", "piggyback", 9, ["OSC"], linear=["OSC"], kind="blend")   # a blend is never an input
    _run(root / "Blend", "M31_blend_x", "piggyback", 1, ["OSC"], kind="blend")
    inp = bl.discover(bl.Options(target="M31", staging_root=root))
    assert inp.osc_run.run_dir.name == "PS22_smoke" and inp.osc.name == "M31_OSC_linear.xisf"
    assert bl.run_info(root / "b") is None


def test_discover_errors_name_the_fix(tmp_path):
    root = tmp_path / "S"
    _run(root, "p", "piggyback", 1, ["OSC"])
    with pytest.raises(bl.BlendError, match="--rig rc16"):
        bl.discover(bl.Options(target="M31", staging_root=root))
    with pytest.raises(bl.BlendError, match="--rig piggyback"):
        bl.discover(bl.Options(target="M42", staging_root=root))
    with pytest.raises(bl.BlendError, match="--stage"):
        bl.discover(bl.Options(target="M31", staging_root=root, stage="raw"))
    with pytest.raises(bl.BlendError, match="missing input"):
        bl.discover(bl.Options(target="M31", staging_root=root, rc16=[tmp_path / "nope.xisf"]))


def test_explicit_paths_win(tmp_path, staging):
    osc = _touch(tmp_path / "x" / "my_osc.xisf")
    rc = [_touch(tmp_path / "x" / "master_L.xisf"), _touch(tmp_path / "x" / "M31_Ha_linear.xisf")]
    inp = bl.discover(bl.Options(target="M31", staging_root=staging, osc=osc, rc16=rc))
    assert inp.osc == osc and inp.osc_run is None and inp.rc16_run is None
    assert inp.rc16 == {"L": rc[0], "Ha": rc[1]} and inp.lum_filters() == ["L"]


@pytest.mark.parametrize("name,filt", [("master_L.xisf", "L"), ("M31_R_linear.xisf", "R"),
                                       ("Andromeda_Galaxy_Ha_final.xisf", "Ha"), ("whatever.xisf", "L")])
def test_filter_of(name, filt):
    assert bl.filter_of(name) == filt


# ----------------------------------------------------------------- run

def test_dry_run_writes_nothing(staging):
    res = bl.run(bl.Options(target="M31", staging_root=staging, dry_run=True), echo=lambda s: None)
    assert res["script_problems"] == [] and not (staging / "Blend").exists()


def test_run_without_pixinsight_writes_script_manifest_ledger(staging):
    o = bl.Options(target="M31", staging_root=staging, run_pixinsight=False,
                   pixinsight=str(staging / "no" / "bin" / "PixInsight.exe"))
    res = bl.run(o, echo=lambda s: None)
    rd = staging / "Blend" / re.sub(r".*[\\/]", "", res["run_dir"])
    assert (rd / "blend_run.js").is_file() and pjsr.check((rd / "blend_run.js").read_text("ascii")) == []
    man = json.loads((rd / "manifest.json").read_text("ascii"))
    assert man["kind"] == "blend" and [i["role"] for i in man["inputs"]][:2] == ["osc", "rc16"]
    assert (rd / "out" / "timing.csv").is_file()
    led = L.load(rd / "ledger.json")
    assert led.kind == "blend" and led.rig == "piggyback" and led.version == 1
    assert led.machine["blend"]["ok"] is None and led.machine["luminance_from"] == ["L"]
    # a second blend gets a NEW folder and the next blend version; integration
    # versions and the scheduler queue never see blend ledgers
    o2 = bl.Options(target="M31", staging_root=staging, run_pixinsight=False,
                    out=staging / "Blend" / "second", pixinsight=o.pixinsight)
    res2 = bl.run(o2, echo=lambda s: None)
    assert res2["ledger_version"] == 2
    assert rp.pending(staging / "Blend") == []
    from photonscript.integration import ledger as writer
    assert writer.next_version(staging, "M31", "piggyback") == 3      # integration v2 + 1
    with pytest.raises(bl.BlendError, match="NEW folder"):
        bl.run(o2, echo=lambda s: None)


def test_run_with_pixinsight_records_steps(staging, monkeypatch):
    calls = []

    def fake_run(script, log_path, **k):
        calls.append((script, log_path))
        fin = log_path.parent / "final"
        _touch(fin / "M31_blend.jpg")
        (fin / "M31_blend_steps.json").write_text(json.dumps(
            {"result": "ok", "products": {"full": {"ok": True, "registration": "StarAlignment"}}}))
        log_path.write_text("STAGE START x\nEXIT OK\n")
        return {"ok": True, "exit_code": 0, "minutes": 1.5, "last_line": "EXIT OK", "timed_out": False}

    monkeypatch.setattr(bl.runner, "run_script", fake_run)
    res = bl.run(bl.Options(target="M31", staging_root=staging, out=staging / "Blend" / "b1"),
                 echo=lambda s: None)
    assert len(calls) == 1 and calls[0][1].name == "blend.log"
    led = L.load(staging / "Blend" / "b1" / "ledger.json")
    assert led.machine["blend"]["ok"] is True
    assert led.machine["blend"]["products"]["full"]["registration"] == "StarAlignment"
    assert any(p.endswith("M31_blend.jpg") for p in res["outputs"])


def test_weight_is_checked(staging):
    with pytest.raises(bl.BlendError, match="weight"):
        bl.run(bl.Options(target="M31", staging_root=staging, weight=1.5, dry_run=True), echo=lambda s: None)


def test_cli_dry_run(staging):
    r = CliRunner().invoke(cli.app, ["blend", "--target", "M31", "--staging-root", str(staging),
                                     "--dry-run", "--json", "--weight", "0.6", "--no-core"])
    assert r.exit_code == 0, r.output
    res = json.loads(r.output[r.output.index("{"):])
    assert res["config"]["weight"] == 0.6 and res["config"]["core"] is False
    assert res["script_problems"] == [] and not (staging / "Blend").exists()
    r = CliRunner().invoke(cli.app, ["blend", "--target", "M42", "--staging-root", str(staging), "--dry-run"])
    assert r.exit_code == 1


# ----------------------------------------------------------------- ledger kind

def test_ledger_kind_default_and_validation():
    led = L.parse({"schema": L.SCHEMA, "campaign": "M31", "run": "r", "machine": {}})
    assert led.kind == "integration" and led.dump()["kind"] == "integration"
    assert L.parse({"campaign": "M31", "run": "r", "kind": "Blend", "machine": {},
                    "schema": L.SCHEMA}).kind == "blend"
    with pytest.raises(Exception):
        L.parse({"campaign": "M31", "run": "r", "kind": "mosaic", "machine": {}, "schema": L.SCHEMA})
