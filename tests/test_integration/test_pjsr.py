"""PS-22: the generated PixInsight scripts obey the PJSR rules."""
import pytest

from photonscript.integration import pjsr

ICFG = {"staging": "D:/Astrophotography/Staging/X", "out": "D:/Astrophotography/Staging/X/out",
        "target": "Andromeda Galaxy", "rig": "piggyback", "cfa": "RGGB", "pedestal": 1000,
        "bias": {"dir": "BIAS"}, "darks": [{"exp": 120.0, "label": "120s", "dir": "DARKS/120s"}],
        "flats": [], "reference": "2026-10-03_21-49-08__0.00_400.00s_0014",
        "stacks": [{"name": "OSC", "filter": "OSC", "reference": "",
                    "groups": [{"name": "120s", "exp": 120.0, "dir": "LIGHTS/OSC/120s",
                                "tag": "_120.00s_", "dark": 120.0, "optimize": False},
                               {"name": "400s", "exp": 400.0, "dir": "LIGHTS/OSC/400s",
                                "tag": "_400.00s_", "dark": 120.0, "optimize": True}]}]}
FCFG = {"out": "D:/X/out", "ra_deg": 10.68, "dec_deg": 41.27, "focal_mm": 600.0, "pixel_um": 3.76,
        "gradient": "auto", "use_rc": True, "bg_target": 0.12, "shadow_sigma": 2.0, "scnr": 0.6,
        "sat_mid": 0.64, "hdr_layers": 7,
        "masters": [{"name": "M31_OSC", "path": "D:/X/out/master/master_OSC.xisf"}]}


@pytest.mark.parametrize("name", [pjsr.INTEGRATE_TEMPLATE, pjsr.FINISH_TEMPLATE])
def test_templates_are_ascii_and_rule_clean(name):
    raw = (pjsr.DEPLOY / name).read_bytes()
    raw.decode("ascii")
    t = raw.decode("ascii")
    probs = pjsr.check(t)
    if name == pjsr.INTEGRATE_TEMPLATE:
        assert probs == ["CONFIG not filled"]          # only the fill-in mark remains
    else:                                              # only the launcher placeholders remain
        assert len(probs) == 1 and probs[0].startswith("unfilled placeholders:")


def test_finish_is_the_one_osc_finish():
    # PS-22/PS-46: integrate renders deploy/finish_osc.js (the run-finish-osc.ps1
    # script), not a second copy of the finish.
    assert pjsr.FINISH_TEMPLATE == "finish_osc.js"
    assert not (pjsr.DEPLOY / "finish_stack.js").exists()


def test_render_finish_fills_what_the_ps1_fills():
    import re
    js = pjsr.template(pjsr.FINISH_TEMPLATE)
    code = "\n".join(l for l in js.splitlines() if not l.lstrip().startswith("//"))
    used = set(re.findall(r"__[A-Z][A-Z0-9_]*__", code))
    ps1 = (pjsr.DEPLOY / "run-finish-osc.ps1").read_text(encoding="ascii")
    filled = set(re.findall(r"Replace\('(__[A-Z][A-Z0-9_]*__)'", ps1))
    assert used <= filled
    out = pjsr.render_finish(FCFG)
    assert pjsr.check(out) == []
    assert 'var MASTERS = [{"name": "M31_OSC", "path": "D:/X/out/master/master_OSC.xisf"}];' in out
    assert 'var FINAL      = "D:/X/out/final";' in out and 'var LOGDIR     = "D:/X/out";' in out
    assert "var RA_DEG    = 10.68;" in out and 'var COLOR       = "auto";' in out


def test_render_finish_unknown_coords_are_nan_and_ascii():
    cfg = dict(FCFG, ra_deg=None, dec_deg=None, out="D:\\X\\Caf" + chr(0xE9))
    out = pjsr.render_finish(cfg)
    assert pjsr.check(out) == []
    assert "var RA_DEG    = NaN;" in out and "D:/X/Caf\\u00e9/final" in out


def test_finish_defaults_match_the_ps1():
    ps1 = (pjsr.DEPLOY / "run-finish-osc.ps1").read_text(encoding="ascii")
    d = pjsr.FINISH_DEFAULTS
    for line in ('[double]$BgTarget = %s' % d["bg_target"], '[double]$ShadowSigma = %s' % d["shadow_sigma"],
                 '[double]$Scnr = %.2f' % d["scnr"], '[double]$SatMid = %s' % d["sat_mid"],
                 '[int]$HdrLayers = %d' % d["hdr_layers"], '[double]$Denoise = %s' % d["denoise"],
                 "[string]$Deconv = '%s'" % d["deconv"], '[double]$DeconvStrength = %s' % d["deconv_strength"],
                 "[string]$StarReduction = '%s'" % d["stars"], '[double]$StarStrength = %s' % d["star_strength"],
                 "[string]$Color = '%s'" % d["color"], '[string]$SpccQE = "%s"' % d["spcc_qe"],
                 '[string]$SpccWhite = "%s"' % d["spcc_white"],
                 '[int]$GraXpertTimeoutMin = %d' % d["graxpert_timeout_min"]):
        assert line in ps1, line


def test_rendered_integrate_passes_and_embeds_config():
    out = pjsr.render(pjsr.template(pjsr.INTEGRATE_TEMPLATE), ICFG)
    assert pjsr.check(out) == []
    assert 'var CONFIG = {' in out and '"optimize": true' in out
    assert "PSFSignalWeight" in out and "WinsorizedSigmaClip" in out and "LocalNormalization" in out
    assert "distortionCorrection = true" in out


def test_rendered_finish_with_solver_block_keeps_include_order(tmp_path):
    exe = tmp_path / "PixInsight" / "bin" / "PixInsight.exe"
    adp = tmp_path / "PixInsight" / "src" / "scripts" / "AdP"
    adp.mkdir(parents=True)
    exe.parent.mkdir(parents=True)
    for d in pjsr.SOLVER_DEPS:
        (adp / d).write_text("x")
    block, missing = pjsr.solver_include(exe)
    assert not missing and "ImageSolver.js" in block
    out = pjsr.render_finish(FCFG, block)
    assert pjsr.check(out) == []
    assert out.index("#include <pjsr/SectionBar.jsh>") < out.index('#include "')


def test_solver_block_empty_when_files_missing(tmp_path):
    block, missing = pjsr.solver_include(tmp_path / "bin" / "PixInsight.exe")
    assert block == "" and "ImageSolver.js" in missing


def test_check_catches_each_rule():
    good = "#include <pjsr/DataType.jsh>\nvar a = 1; // fine\n"
    assert pjsr.check(good) == []
    assert any("non-ASCII" in p for p in pjsr.check(good + "// caf" + chr(0xE9) + "\n"))
    assert any("BOM" in p for p in pjsr.check(chr(0xFEFF) + good))
    assert any("slash-star" in p for p in pjsr.check(good + "x = 1; // glob *.fits or /* here\n"))
    # slash-star inside a string is fine
    assert pjsr.check(good + 'var g = "/*.fits"; // ok\n') == []
    assert any("after the quoted include" in p for p in
               pjsr.check('#include "C:/AdP/ImageSolver.js"\n#include <pjsr/DataType.jsh>\n'))
    bad_crop = "function f(v) {\n var CR = new Crop;\n}\n"
    assert any("Crop without clearing" in p for p in pjsr.check(bad_crop))
    ok_crop = "function f(v) {\n clearWcs(v);\n var CR = new Crop;\n}\n"
    assert pjsr.check(ok_crop) == []
    assert any("placeholders" in p for p in pjsr.check('var s = "__STAGING__";\n'))


def test_write_refuses_and_writes_without_bom(tmp_path):
    with pytest.raises(pjsr.PjsrError):
        pjsr.write(tmp_path / "bad.js", "x; // a /* b\n")
    p = pjsr.write(tmp_path / "ok.js", "var a = 1;\n")
    assert p.read_bytes() == b"var a = 1;\n"


def test_config_line_is_ascii_json():
    line = pjsr.config_line({"target": "Caf" + chr(0xE9)})
    line.encode("ascii")
    assert "\\u00e9" in line
