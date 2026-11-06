"""Mono color combine: an RC16-only run (M27, NGC 6535, NGC 2408, the RC16
Heart) made one finished mono image per filter and no color image. integrate
now combines the per-filter masters into <target>_LRGB / _RGB / _SHO / _HOO
(deploy/finish_mono_color.js, reusing the finish_osc.js functions), and
`photonscript finish --mono-run` does the same for an existing run."""
import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from photonscript import cli
from photonscript.integration import finish as fn
from photonscript.integration import hdr
from photonscript.integration import mono_color as mc
from photonscript.integration import pipeline as pl
from photonscript.integration import pjsr
from photonscript.shared import ledger as L
from tests.test_integration.test_ps177_178_exposure_groups import _fits, _opts

OUT = Path("D:/S/run/out")


def _m(*filters, hdr_l=False):
    out = {}
    for f in filters:
        out[f] = f"D:/S/run/out/master/master_{f}{'_hdr' if (hdr_l and f == 'L') else ''}.xisf"
    return out


def _cfg(js: str) -> dict:
    return json.loads(js.split("var CONFIG = ", 1)[1].split(";\n", 1)[0])


def _touch(p, n=16):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * n)
    return p


# ----------------------------------------------------------------- template + lint

def test_template_is_ascii_and_rule_clean():
    raw = (pjsr.DEPLOY / mc.TEMPLATE).read_bytes()
    t = raw.decode("ascii")
    assert not t.startswith("\ufeff")
    assert pjsr.check(t) == ["CONFIG not filled"]
    for n, ln in enumerate(t.splitlines(), 1):
        c = pjsr._line_comment(ln)
        assert c is None or "/*" not in c, n
    # pjsr headers before the solver include, CONFIG before the library
    assert t.index("#include <pjsr/SectionBar.jsh>") < t.index(pjsr.SOLVER_MARK)
    assert t.index(pjsr.CONFIG_MARK) < t.index(mc.LIB_MARK)


def test_template_has_the_guards_and_records():
    t = pjsr.template(mc.TEMPLATE)
    for g in ('typeof ChannelCombination === "function"', 'typeof LRGBCombination === "function"',
              'typeof LinearFit === "function"'):
        assert g in t
    # each guarded process has a JS fallback
    assert 'rec.combine = "JS channel copy"' in t
    assert 'step("lrgb", "JS lightness", "fallback"' in t
    assert "median / MAD match" in t
    # one registration is checked; a failed item is SKIPPED, not fatal
    assert "not one registration" in t
    assert 'log("SKIPPED [" + item.name + "]: "' in t
    assert 'log(NFAILED ? ("EXIT WITH " + NFAILED + " FAILED ITEM(S)") : "EXIT OK")' in t
    # outputs and records
    for s in ('".xisf"', '".tif"', '".jpg"', '"_steps.json"', '"_linear.xisf"', "/color.log",
              "timing_color.csv"):
        assert s in t
    # SHO look: SCNR green + hue curve; stars from the RGB layer, else white Ha stars
    assert "scnrGreen(view, SHO_SCNR)" in t and "CT.H = SHO_HUE" in t
    assert 'item.stars_from === "rgb" && STARS_FILE' in t
    assert 'pixelMath(stv, "$T[" + item.ha_channel + "]")' in t
    # the overrides that keep the finish library writing color.log
    assert t.count("function writeLog()") == 1 and t.count("function log(s)") == 1


def test_finish_osc_library_markers_and_contents():
    lib = mc.finish_library()
    assert lib.startswith(mc.LIB_BEGIN)
    for fname in ("cropEdges", "removeGradient", "plateSolve", "colorCalibrate", "spcc", "removeGreen",
                  "deconvolve", "denoise", "starSplit", "starRecombine", "stretch", "recoverCore",
                  "trySave", "labelOutput", "pixelMath", "applyHT"):
        assert f"function {fname}(" in lib, fname
    assert "function main(" not in lib and "var RUN = MASTERS" not in lib
    code = "\n".join(ln for ln in lib.splitlines() if not ln.lstrip().startswith("//"))
    assert not re.findall(r"__[A-Z][A-Z_]*__", code)
    # the finish itself still renders and checks clean with the markers in
    js = pjsr.render_finish({"out": "D:/x/out", "masters": [{"name": "a", "path": "D:/x/a.xisf"}]})
    assert pjsr.check(js) == []


def test_render_embeds_config_and_passes_check():
    items, _ = mc.plan(_m("L", "R", "G", "B", "Ha", "OIII", "SII"), "M_27")
    cfg = mc.config(OUT, items, {"ra_deg": 298.5, "dec_deg": 22.72, "focal_mm": 3248.0, "pixel_um": 3.76,
                                 "rc_mode": "on", "graxpert": "C:\\GX\\GraXpert.exe"}, "M 27")
    js = mc.render(cfg)
    assert pjsr.check(js) == []
    assert mc.LIB_MARK not in js and pjsr.CONFIG_MARK not in js
    assert js.count("function colorCalibrate(") == 1
    c = _cfg(js)
    assert c["final"] == "D:/S/run/out/final" and c["out"] == "D:/S/run/out"
    assert c["steps_file"] == "D:/S/run/out/final/mono_color_steps.json"
    assert c["ra_deg"] == 298.5 and c["focal_mm"] == 3248.0 and c["rc_mode"] == "on"
    assert c["graxpert"] == "C:/GX/GraXpert.exe"
    # mono filters: never the OSC color-sensor SPCC curves
    assert "Sony Color Sensor" not in json.dumps(c)
    assert c["spcc_qe"] == "Sony IMX411/455/461/533/571"
    assert [i["name"] for i in c["items"]] == ["M_27_LRGB", "M_27_SHO", "M_27_HOO"]
    assert c["lrgb_lightness"] == mc.LRGB_LIGHTNESS and c["sho_scnr"] == mc.SHO_SCNR


def test_render_unknown_coordinates_and_rc_off():
    items, _ = mc.plan(_m("R", "G", "B"), "T")
    c = mc.config(OUT, items, {"ra_deg": None, "dec_deg": float("nan"), "use_rc": False}, "T")
    assert c["ra_deg"] is None and c["dec_deg"] is None and c["rc_mode"] == "off"
    js = mc.render(c)
    assert "var RA_DEG     = cfgNum(CONFIG.ra_deg, NaN);" in js
    assert pjsr.check(js) == []


def test_render_with_solver_block_keeps_headers_first():
    items, _ = mc.plan(_m("R", "G", "B"), "T")
    block = '#define USE_SOLVER_LIBRARY true\n#include "C:/PI/src/scripts/AdP/ImageSolver.js"'
    js = mc.render(mc.config(OUT, items, {}, "T"), block)
    assert pjsr.check(js) == []
    assert js.index("#include <pjsr/DataType.jsh>") < js.index('#include "C:/PI')


def test_render_refuses_no_items():
    with pytest.raises(pjsr.PjsrError):
        mc.render(mc.config(OUT, [], {}, "T"))


# ----------------------------------------------------------------- plan

def test_plan_lrgb_with_l():
    items, notes = mc.plan(_m("L", "R", "G", "B", hdr_l=True), "M_27")
    (it,) = items
    assert it["kind"] == "lrgb" and it["name"] == "M_27_LRGB" and notes == []
    assert it["L"] == "D:/S/run/out/master/master_L_hdr.xisf"
    assert it["channels"] == {"R": _m("R")["R"], "G": _m("G")["G"], "B": _m("B")["B"]}
    assert it["keep_stars"] is False                      # no narrowband item needs them


def test_plan_rgb_only_without_l():
    (it,), notes = mc.plan(_m("R", "G", "B"), "NGC_6535")
    assert it["kind"] == "rgb" and it["name"] == "NGC_6535_RGB" and it["L"] is None
    # asked for LRGB explicitly: RGB with a note
    (it2,), notes2 = mc.plan(_m("R", "G", "B"), "T", ["lrgb"])
    assert it2["kind"] == "rgb" and notes2 == ["LRGB: no L master, making RGB instead"]
    # rgb asked with an L present: RGB only, L ignored
    (it3,), _ = mc.plan(_m("L", "R", "G", "B"), "T", ["rgb"])
    assert it3["kind"] == "rgb" and it3["L"] is None


def test_plan_sho_and_hoo_with_rgb_stars():
    items, notes = mc.plan(_m("L", "R", "G", "B", "Ha", "OIII", "SII"), "T")
    assert [i["kind"] for i in items] == ["lrgb", "sho", "hoo"] and notes == []
    lrgb, sho, hoo = items
    assert lrgb["keep_stars"] is True
    assert sho["channels"] == {"R": _m("SII")["SII"], "G": _m("Ha")["Ha"], "B": _m("OIII")["OIII"]}
    assert sho["ha_channel"] == 1 and sho["stars_from"] == "rgb"
    assert hoo["channels"] == {"R": _m("Ha")["Ha"], "G": _m("OIII")["OIII"], "B": _m("OIII")["OIII"]}
    assert hoo["ha_channel"] == 0 and hoo["stars_from"] == "rgb"


def test_plan_hoo_when_sii_missing_and_ha_stars():
    items, notes = mc.plan(_m("Ha", "OIII"), "Heart")
    assert [i["kind"] for i in items] == ["hoo"] and items[0]["stars_from"] == "ha"
    # SHO asked without SII: skipped with what is missing, HOO still made
    items2, notes2 = mc.plan(_m("Ha", "OIII"), "Heart", ["sho", "hoo"])
    assert [i["kind"] for i in items2] == ["hoo"]
    assert notes2 == ["SHO left out: needs SII, Ha and OIII (missing SII)"]


def test_plan_missing_filters_skip():
    items, notes = mc.plan(_m("L", "R", "G"), "T")
    assert items == []
    assert "RGB left out: needs R, G and B (missing B)" in notes
    assert any(n.startswith("no color image: the run has G, L, R") for n in notes)
    items, notes = mc.plan(_m("L"), "T")
    assert items == [] and notes == ["no color image: the run has L (LRGB needs R, G, B; HOO needs Ha, OIII)"]
    items, notes = mc.plan(_m("Ha"), "T")
    assert items == [] and notes[0] == "HOO left out: needs Ha and OIII (missing OIII)"
    items, notes = mc.plan({}, "T", ["sho"])
    assert items == [] and notes == ["SHO left out: needs SII, Ha and OIII (missing SII, Ha, OIII)"]


def test_plan_filter_aliases_and_variants():
    m = {"H": "h.xisf", "O": "o.xisf", "S": "s.xisf", "L_30s": "l30.xisf", "OSC": "x.xisf"}
    items, _ = mc.plan(m, "T")
    assert [i["kind"] for i in items] == ["sho", "hoo"]
    assert items[0]["channels"] == {"R": "s.xisf", "G": "h.xisf", "B": "o.xisf"}
    assert mc.canonical("Halpha") == "Ha" and mc.canonical("o3") == "OIII" and mc.canonical("L_30s") is None


@pytest.mark.parametrize("spec,want", [("", None), ("auto", None), (None, None), ("all", list(mc.COLORS)),
                                       ("LRGB", ["lrgb"]), ("sho, hoo,sho", ["sho", "hoo"])])
def test_parse_colors(spec, want):
    assert mc.parse_colors(spec) == want


def test_parse_colors_refuses_unknown():
    with pytest.raises(mc.MonoColorError, match="osc"):
        mc.parse_colors("lrgb,osc")


def _st(name, filt, exp):
    return {"name": name, "filter": filt, "mixed_exposures": False, "groups": [{"exp": exp}]}


def test_expected_masters_prefer_hdr():
    stacks = [_st("L", "L", 300.0), _st("L_30s", "L", 30.0), _st("R", "R", 300.0), _st("G", "G", 300.0)]
    items = hdr.plan(stacks, OUT)
    got = mc.expected_masters(stacks, OUT, items)
    assert got == {"L": "D:/S/run/out/master/master_L_hdr.xisf", "R": "D:/S/run/out/master/master_R.xisf",
                   "G": "D:/S/run/out/master/master_G.xisf"}
    assert mc.expected_masters(stacks, OUT, items, hdr_made=set())["L"].endswith("master_L.xisf")


# ----------------------------------------------------------------- pipeline

@pytest.fixture
def lrgb_lib(tmp_path):
    lib = tmp_path / "Library"
    k = 0
    for f in ("L", "R", "G", "B"):
        for i in range(3):
            _fits(lib / "M 27" / f / f"2026-10-06_03-{k:02d}-00__{f}_30.00s_{i:04d}.fits", "LIGHT", 30.0,
                  filt=f, minute=k)
            k += 1
    for i in range(12):
        _fits(lib / "Calibration" / "DARK" / "2026-10-06" / f"d30_{i:02d}.fits", "DARK", 30.0, filt="")
    return lib


def _fake_runner(calls, make_color=True, skip=()):
    def fake_run(script, log_path, **kw):
        script, log_path = Path(script), Path(log_path)
        calls.append(script.name)
        out = log_path.parent
        out.mkdir(parents=True, exist_ok=True)
        cfg = _cfg(script.read_text()) if "var CONFIG = " in script.read_text() else {}
        if script.name == "integrate_run.js":
            for s in cfg["stacks"]:
                if s["name"] not in skip:
                    _touch(out / "master" / f"master_{s['name']}.xisf")
        elif script.name == "color_run.js" and make_color:
            for it in cfg["items"]:
                for sfx in (".xisf", ".tif", ".jpg", "_linear.xisf", "_steps.json"):
                    _touch(Path(cfg["final"]) / f"{it['name']}{sfx}")
            Path(cfg["steps_file"]).write_text(json.dumps(
                {"result": "ok", "items": [{"name": it["name"], "status": "ok"} for it in cfg["items"]]}))
        log_path.write_text("t  EXIT OK\n")
        return {"ok": True, "exit_code": 0, "minutes": 0.1, "last_line": "EXIT OK", "timed_out": False}
    return fake_run


def _m27_opts(lib, tmp_path, **kw):
    return _opts(lib, tmp_path, target="M 27", **kw)


def test_pipeline_makes_lrgb_after_the_finish(lrgb_lib, tmp_path, monkeypatch):
    out = tmp_path / "Staging" / "m27"
    calls = []
    monkeypatch.setattr(pl.runner, "run_script", _fake_runner(calls))
    msgs = []
    res = pl.run(_m27_opts(lrgb_lib, tmp_path, out=out, run_pixinsight=True), echo=msgs.append)
    assert calls == ["integrate_run.js", "finish_run.js", "color_run.js"]
    js = (out / "color_run.js").read_text("ascii")
    assert pjsr.check(js) == []
    c = _cfg(js)
    (it,) = c["items"]
    assert it["kind"] == "lrgb" and it["name"] == "M_27_LRGB"
    assert it["L"] == pjsr.fwd(out / "out" / "master" / "master_L.xisf")
    # the finish settings of the run (catalog coordinates, the lights' optics)
    fin = (out / "finish_run.js").read_text("ascii")
    assert c["focal_mm"] == float(re.search(r"^var FOCAL_MM\s*=\s*([-0-9.eE]+);", fin, re.M).group(1))
    mcr = res["mono_color"]
    assert mcr["ok"] and mcr["made"] == ["M_27_LRGB"]
    assert any(r[0] == "PixInsight color combine (launch to exit)" for r in res["timing"])
    led = json.loads(Path(res["ledger"]).read_text())
    col = led["machine"]["color"]
    assert col["made"] == ["M_27_LRGB"] and col["items"][0]["kind"] == "lrgb"
    assert col["steps"]["result"] == "ok"
    for sfx in (".jpg", ".tif", ".xisf"):
        assert any(p.endswith("M_27_LRGB" + sfx) for p in led["machine"]["outputs"]), sfx
    assert any("color_run.js (color: M_27_LRGB)" in m for m in msgs)


def test_pipeline_replans_from_the_masters_made(lrgb_lib, tmp_path, monkeypatch):
    out = tmp_path / "Staging" / "m27b"
    calls = []
    monkeypatch.setattr(pl.runner, "run_script", _fake_runner(calls, skip={"L"}))
    res = pl.run(_m27_opts(lrgb_lib, tmp_path, out=out, run_pixinsight=True), echo=lambda s: None)
    # L made no master inside PixInsight: RGB instead of LRGB
    assert calls[-1] == "color_run.js"
    assert [i["kind"] for i in res["mono_color"]["items"]] == ["rgb"]
    assert res["mono_color"]["made"] == ["M_27_RGB"]


def test_pipeline_missing_blue_makes_no_color(lrgb_lib, tmp_path, monkeypatch):
    for p in (lrgb_lib / "M 27" / "B").glob("*.fits"):
        p.unlink()
    out = tmp_path / "Staging" / "nob"
    calls = []
    monkeypatch.setattr(pl.runner, "run_script", _fake_runner(calls))
    msgs = []
    res = pl.run(_m27_opts(lrgb_lib, tmp_path, out=out, run_pixinsight=True), echo=msgs.append)
    assert calls == ["integrate_run.js", "finish_run.js"]
    assert "mono_color" not in res and not (out / "color_run.js").exists()
    assert any("color: RGB left out: needs R, G and B (missing B)" in m for m in msgs)
    assert "color" not in json.loads(Path(res["ledger"]).read_text())["machine"]


def test_pipeline_mono_color_off_and_no_finish(lrgb_lib, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(pl.runner, "run_script", _fake_runner(calls))
    pl.run(_m27_opts(lrgb_lib, tmp_path, out=tmp_path / "S" / "off", run_pixinsight=True, mono_color=False),
           echo=lambda s: None)
    assert calls == ["integrate_run.js", "finish_run.js"]
    calls.clear()
    pl.run(_m27_opts(lrgb_lib, tmp_path, out=tmp_path / "S" / "nofin", run_pixinsight=True, finish=False),
           echo=lambda s: None)
    assert calls == ["integrate_run.js"]


def test_pipeline_color_failure_is_not_fatal(lrgb_lib, tmp_path, monkeypatch):
    calls = []
    fake = _fake_runner(calls)

    def boom(script, log_path, **kw):
        if Path(script).name == "color_run.js":
            raise RuntimeError("PixInsight is already running")
        return fake(script, log_path, **kw)
    monkeypatch.setattr(pl.runner, "run_script", boom)
    msgs = []
    res = pl.run(_m27_opts(lrgb_lib, tmp_path, out=tmp_path / "S" / "boom", run_pixinsight=True),
                 echo=msgs.append)
    assert res["mono_color"]["ok"] is False and res["finish"]["ok"]
    assert any(m.startswith("  color: not run (PixInsight is already running)") for m in msgs)
    assert Path(res["ledger"]).is_file()


def test_pipeline_no_pixinsight_writes_the_color_script(lrgb_lib, tmp_path):
    out = tmp_path / "Staging" / "nopi"
    msgs = []
    res = pl.run(_m27_opts(lrgb_lib, tmp_path, out=out), echo=msgs.append)
    assert any(s.endswith("color_run.js") for s in res["scripts"])
    assert any("--run=" in m and "color_run.js" in m for m in msgs)


# ----------------------------------------------------------------- config + CLI

def test_config_default_on_and_on_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    from photonscript.shared.config import PhotonScriptConfig
    c = PhotonScriptConfig(_env_file=None)
    assert c.integrate_mono_color is True
    f = {x[0]: x for x in _CONFIG_FIELDS}["integrate_mono_color"]
    assert f[1] == "PS_INTEGRATE_MONO_COLOR" and f[3] == "Integration" and f[4] == "bool"
    assert pl.config_options(c, "rc16")["mono_color"] is True
    c2 = PhotonScriptConfig(_env_file=None, integrate_mono_color=False)
    assert pl.config_options(c2, "rc16")["mono_color"] is False


def _plain(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


def test_cli_flags():
    r = CliRunner().invoke(cli.app, ["integrate", "--help"])
    assert r.exit_code == 0 and "--no-mono-color" in _plain(r.output)
    r = CliRunner().invoke(cli.app, ["finish", "--help"])
    assert r.exit_code == 0 and "--mono-run" in _plain(r.output) and "--color" in _plain(r.output)


# ----------------------------------------------------------------- finish --mono-run

@pytest.fixture
def mono_run(tmp_path):
    root = tmp_path / "Staging"
    run = root / "M_27_rc16_20261012-2300"
    for f in ("L", "L_30s", "L_hdr", "R", "G", "B", "Ha", "OIII"):
        _touch(run / "out" / "master" / f"master_{f}.xisf")
    _touch(run / "out" / "master" / "masterBias.xisf")
    L.save(run / "ledger.json", L.Ledger(campaign="M 27", rig="rc16", run=run.name, version=2))
    (run / "finish_run.js").write_text("var RA_DEG    = 299.9;\nvar DEC_DEG   = 22.72;\n"
                                       "var FOCAL_MM  = 3250.5;\nvar PIXEL_UM  = 3.76;\n", encoding="ascii")
    return run


def _fopts(run, **kw):
    o = fn.Options(master=None, name="", staging_root=run.parent, mono_run=run,
                   pixinsight=str(run.parent / "PI" / "bin" / "PixInsight.exe"))
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def test_mono_run_colors_an_existing_run(mono_run, monkeypatch):
    calls = []
    monkeypatch.setattr(fn.runner, "run_script", _fake_runner(calls))
    before = sorted(p.relative_to(mono_run) for p in mono_run.rglob("*"))
    res = fn.run_mono_color(_fopts(mono_run, colors="lrgb,hoo"), echo=lambda s: None)
    assert calls == ["color_run.js"]
    run_dir = Path(res["run_dir"])
    assert run_dir.parent == mono_run.parent / "Finish" and run_dir.name.startswith("M_27_color_")
    c = _cfg((run_dir / "color_run.js").read_text("ascii"))
    assert [i["name"] for i in c["items"]] == ["M_27_LRGB", "M_27_HOO"]
    assert c["items"][0]["L"].endswith("master_L_hdr.xisf")           # the HDR master preferred
    assert c["items"][1]["stars_from"] == "rgb"
    assert c["ra_deg"] == 299.9 and c["focal_mm"] == 3250.5          # from the run's finish_run.js
    assert res["color"]["made"] == ["M_27_LRGB", "M_27_HOO"]
    led = L.load(Path(res["ledger"]))
    assert led.kind == "finish" and led.campaign == "M 27" and led.rig == "rc16"
    assert led.machine["input"]["mono_run"] == str(mono_run)
    assert led.machine["color"]["made"] == ["M_27_LRGB", "M_27_HOO"]
    assert any(p.endswith("M_27_LRGB.jpg") for p in led.machine["outputs"])
    assert json.loads((run_dir / "manifest.json").read_text())["mono_run"] == str(mono_run)
    # the source run is only read
    assert sorted(p.relative_to(mono_run) for p in mono_run.rglob("*")) == before


def test_mono_run_accepts_the_master_folder_and_auto(mono_run, monkeypatch):
    monkeypatch.setattr(fn.runner, "run_script", _fake_runner([]))
    res = fn.run_mono_color(_fopts(mono_run / "out" / "master", name="M27_v3", staging_root=mono_run.parent),
                             echo=lambda s: None)
    assert res["mono_run"] == str(mono_run)
    assert [i["name"] for i in res["items"]] == ["M27_v3_LRGB", "M27_v3_HOO"]   # no SII: HOO only


def test_mono_run_dry_run_writes_nothing(mono_run):
    res = fn.run_mono_color(_fopts(mono_run, colors="sho,rgb", dry_run=True), echo=lambda s: None)
    assert res["script_problems"] == []
    assert [i["kind"] for i in res["items"]] == ["rgb"]                  # SHO needs SII
    assert "SHO left out: needs SII, Ha and OIII (missing SII)" in res["notes"]
    assert not (mono_run.parent / "Finish").exists()


def test_mono_run_errors(mono_run, tmp_path):
    with pytest.raises(fn.FinishError, match="no out/master"):
        fn.run_mono_color(_fopts(tmp_path / "nowhere"), echo=lambda s: None)
    with pytest.raises(fn.FinishError, match="--color"):
        fn.run_mono_color(_fopts(mono_run, colors="osc"), echo=lambda s: None)
    with pytest.raises(fn.FinishError, match="nothing to color"):
        fn.run_mono_color(_fopts(mono_run, colors="sho"), echo=lambda s: None)
    with pytest.raises(fn.FinishError, match="overlaps"):
        fn.run_mono_color(_fopts(mono_run, out=mono_run / "out" / "color"), echo=lambda s: None)
    osc = tmp_path / "osc_run"
    _touch(osc / "out" / "master" / "master_OSC.xisf")
    with pytest.raises(fn.FinishError, match="OSC run"):
        fn.run_mono_color(_fopts(osc), echo=lambda s: None)
    out = tmp_path / "busy"
    _touch(out / "x")
    with pytest.raises(fn.FinishError, match="not empty"):
        fn.run_mono_color(_fopts(mono_run, out=out), echo=lambda s: None)


def test_cli_mono_run(mono_run, monkeypatch):
    monkeypatch.setattr(fn.runner, "run_script", _fake_runner([]))
    r = CliRunner().invoke(cli.app, ["finish", "--mono-run", str(mono_run), "--color", "lrgb",
                                     "--staging-root", str(mono_run.parent), "--json"])
    assert r.exit_code == 0, r.output
    res = json.loads(r.output[r.output.index("{\n"):])
    assert res["color"]["made"] == ["M_27_LRGB"]
    r = CliRunner().invoke(cli.app, ["finish", "--mono-run", str(mono_run), "--master", "x.xisf"])
    assert r.exit_code == 2 and "exactly one of --master or --mono-run" in _plain(r.output)
    r = CliRunner().invoke(cli.app, ["finish"])
    assert r.exit_code == 2
    r = CliRunner().invoke(cli.app, ["finish", "--master", "x.xisf"])
    assert r.exit_code == 2 and "--master needs --name" in _plain(r.output)
