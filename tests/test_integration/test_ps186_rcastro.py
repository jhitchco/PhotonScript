"""PS-186: RC Astro BlurXTerminator / NoiseXTerminator / StarXTerminator in
the finish (deploy/finish_osc.js) and the RC16 L BXT pass in the blend
(deploy/blend_rc16_osc.js). PixInsight never runs here: these check the
generated scripts and config, and (when node is on PATH) run the finish's
step functions against stub PJSR processes to prove the fallbacks."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from photonscript.integration import blend as bl
from photonscript.integration import pjsr

DEPLOY = pjsr.DEPLOY
BASE = {"out": "D:/X/out", "ra_deg": 10.68, "dec_deg": 41.27,
        "masters": [{"name": "M31_OSC", "path": "D:/X/out/master/master_OSC.xisf"}]}


def _js(name="finish_osc.js") -> str:
    return (DEPLOY / name).read_bytes().decode("ascii")


def _functions(src: str) -> dict[str, str]:
    """name -> source of each top-level `function name(...) {...}`."""
    out = {}
    for m in re.finditer(r"^function (\w+)\s*\([^)]*\)\s*\{", src, re.M):
        depth, i = 1, m.end()
        while depth and i < len(src):
            depth += {"{": 1, "}": -1}.get(src[i], 0)
            i += 1
        out[m.group(1)] = src[m.start():i]
    return out


def _func(src: str, name: str) -> str:
    return _functions(src)[name]


# --------------------------------------------------------------- config

def test_render_finish_defaults_are_auto_with_the_ticket_strengths():
    out = pjsr.render_finish(BASE)
    assert pjsr.check(out) == []
    assert 'var RC_MODE        = "auto";' in out
    assert "var BXT_STARS      = 0.25;" in out
    assert "var BXT_NONSTELLAR = 0.5;" in out
    assert "var NXT_DENOISE    = 0.8;" in out
    assert "var NXT_DETAIL     = 0.15;" in out


def test_render_finish_mode_and_strength_flags():
    on = pjsr.render_finish({**BASE, "rc_mode": "on", "bxt_stars": 0.3, "bxt_nonstellar": 0.9,
                             "nxt_denoise": 0.6})
    assert 'var RC_MODE        = "on";' in on and "var BXT_NONSTELLAR = 0.9;" in on
    assert "var BXT_STARS      = 0.3;" in on and "var NXT_DENOISE    = 0.6;" in on
    # the older --no-rc (use_rc False) wins over any mode
    assert 'var RC_MODE        = "off";' in pjsr.render_finish({**BASE, "use_rc": False, "rc_mode": "on"})
    with pytest.raises(pjsr.PjsrError):
        pjsr.render_finish({**BASE, "rc_mode": "always"})
    with pytest.raises(pjsr.PjsrError):
        pjsr.render_finish({**BASE, "bxt_nonstellar": 1.5})


def test_rc_mode_normalization():
    assert pjsr.rc_mode("") == "auto" and pjsr.rc_mode(None) == "auto"
    assert pjsr.rc_mode(" ON ") == "on" and pjsr.rc_mode(True) == "on"
    assert pjsr.rc_mode("false") == "off" and pjsr.rc_mode(False) == "off"
    with pytest.raises(pjsr.PjsrError):
        pjsr.rc_mode("maybe")


def test_rc_settings_from_config_and_defaults():
    assert pjsr.rc_settings(SimpleNamespace()) == {"rc_mode": "auto", "bxt_stars": 0.25,
                                                   "bxt_nonstellar": 0.5, "nxt_denoise": 0.8}
    cfg = SimpleNamespace(finish_use_rcastro="off", bxt_sharpen_stars=0.2,
                          bxt_sharpen_nonstellar=0.7, nxt_denoise=0.9)
    assert pjsr.rc_settings(cfg) == {"rc_mode": "off", "bxt_stars": 0.2,
                                     "bxt_nonstellar": 0.7, "nxt_denoise": 0.9}
    with pytest.raises(pjsr.PjsrError):
        pjsr.rc_settings(SimpleNamespace(nxt_denoise=2))


def test_config_keys_defaults_and_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    from photonscript.shared.config import PhotonScriptConfig
    c = PhotonScriptConfig(_env_file=None)
    fields = {f[0]: f for f in _CONFIG_FIELDS}
    for k, v, typ in (("finish_use_rcastro", "auto", "str"), ("bxt_sharpen_stars", 0.25, "float"),
                      ("bxt_sharpen_nonstellar", 0.5, "float"), ("nxt_denoise", 0.8, "float"),
                      ("blend_rc16_bxt", True, "bool")):
        assert getattr(c, k) == v, k
        assert k in fields and fields[k][4] == typ and fields[k][1] == "PS_" + k.upper()


def test_config_options_carry_the_rc_settings():
    from photonscript.integration import pipeline as pl
    cfg = SimpleNamespace(integration_library_dir="X", desktop_library_dir="", observatory_bortle=2,
                          observatory_tz="America/Denver", observatory_name="AARO",
                          observatory_lat=31.9, observatory_lon=-109.0, observatory_elev=1300,
                          camera_readout_mode="HCG", piggyback_readout_mode="LCG",
                          finish_use_rcastro="on", bxt_sharpen_nonstellar=0.8)
    kw = pl.config_options(cfg, "rc16")
    assert kw["rc_mode"] == "on" and kw["bxt_nonstellar"] == 0.8 and kw["bxt_stars"] == 0.25
    o = pl.Options(target="M31", rig="rc16", **kw)
    assert o.rc_mode == "on" and o.nxt_denoise == 0.8


def test_integrate_cli_flags():
    from typer.testing import CliRunner

    from photonscript.cli import _rc_overrides, app
    r = CliRunner().invoke(app, ["integrate", "--help"])
    assert r.exit_code == 0
    for flag in ("--rcastro", "--bxt-stars", "--bxt-nonstellar", "--nxt-denoise", "--no-rc"):
        assert flag in r.output, flag
    assert _rc_overrides("", -1, -1, -1) == {}
    assert _rc_overrides("off", 0.3, -1, 0.5) == {"rc_mode": "off", "bxt_stars": 0.3, "nxt_denoise": 0.5}
    with pytest.raises(ValueError):
        _rc_overrides("sometimes", -1, -1, -1)
    with pytest.raises(ValueError):
        _rc_overrides("", 1.2, -1, -1)
    b = CliRunner().invoke(app, ["blend", "--help"])
    assert b.exit_code == 0 and "--rc16-bxt" in b.output


def test_ps1_rc_params():
    ps1 = (DEPLOY / "run-finish-osc.ps1").read_text(encoding="ascii")
    assert "[ValidateSet('auto','on','off')][string]$RcAstro = 'auto'" in ps1
    assert "[double]$BxtStars = 0.25" in ps1 and "[double]$BxtNonstellar = 0.5" in ps1
    assert "[double]$NxtDenoise = 0.8" in ps1
    assert "$rcMode = $(if ($NoRC) { 'off' } else { $RcAstro })" in ps1
    assert "@('BxtNonstellar', $BxtNonstellar, 0, 1)" in ps1
    d = pjsr.FINISH_DEFAULTS
    assert "[double]$BxtStars = %s" % d["bxt_stars"] in ps1
    assert "[double]$NxtDenoise = %s" % d["nxt_denoise"] in ps1
    assert "__USE_RC__" not in ps1 and "__USE_RC__" not in _js()


# --------------------------------------------------------------- finish script

def test_rc_tools_detected_at_run_time_never_assumed():
    s = _js()
    inst = _func(s, "rcInstalled")
    for tool in ("BlurXTerminator", "NoiseXTerminator", "StarXTerminator"):
        assert f'typeof {tool} === "function"' in inst, tool
        # every use of the tool goes through rcUse (which calls rcInstalled)
        assert f'rcUse("{tool}"' in s, tool
    use = _func(s, "rcUse")
    assert "if (!USE_RC)" in use and "finish_use_rcastro off" in use
    assert 'RC_MODE === "on" ? "WARNING: finish_use_rcastro on but "' in use


def test_bxt_runs_on_the_linear_image_after_spcc_and_gradient():
    main = _func(_js(), "main")
    order = [main.index(x) for x in ("removeGradient(v)", "colorCalibrate(v, solved)", "_linear.xisf",
                                     "deconvolve(v)", "denoise(v)", "starSplit(v)", "stretch(v)")]
    assert order == sorted(order)


def test_bxt_correct_only_pass_then_sharpen_pass():
    b = _func(_js(), "bxtDeconvolve")
    i1 = b.index('trySet(B1, "correct_only", true')
    i2 = b.index('trySet(B2, "correct_only", false')
    assert i1 < i2
    for prop in ('"sharpen_stars", BXT_STARS', '"sharpen_nonstellar", BXT_NONSTELLAR',
                 '"auto_nonstellar_psf", true', '"adjust_halos", 0.0'):
        assert prop in b[i2:], prop
    assert "rcInfo(B1, rec)" in b and "rcInfo(B2, rec)" in b


def test_fallback_order_per_step():
    s = _js()
    d = _func(s, "deconvolve")
    assert d.index('rcUse("BlurXTerminator", "bxt")') < d.index("if (GRAXPERT)") < d.index("no built-in fallback")
    n = _func(s, "denoise")
    assert n.index('rcUse("NoiseXTerminator", "nxt")') < n.index('graxpert(view, "denoising"') < n.index("mltDenoise(")
    sp = _func(s, "starSplit")
    assert sp.index('rcUse("StarXTerminator", "sxt")') < sp.index('typeof StarNet2 !== "undefined"')
    pre = _func(s, "starlessPre")
    assert 'trySet(SX, "unscreen", true' in pre and "new StarNet2" in pre


def test_steps_json_has_the_rcastro_record():
    s = _js()
    w = _func(s, "writeSteps")
    assert "rcastro: { mode: RC_MODE, bxt: RC.bxt || null, nxt: RC.nxt || null, sxt: RC.sxt || null }" in w
    assert "STEPS = []; RC = {};" in s           # reset per master
    info = _func(s, "rcInfo")
    assert "P.ai_file" in info and "processVersion" in info


def test_rendered_finish_passes_the_pjsr_rules_for_every_mode():
    for mode in pjsr.RC_MODES:
        out = pjsr.render_finish({**BASE, "rc_mode": mode})
        assert pjsr.check(out) == [], mode
        out.encode("ascii")
        for n, ln in enumerate(out.splitlines(), 1):
            if "//" in ln:
                assert "/*" not in ln.split("//", 1)[1], n


# --------------------------------------------------------------- blend

def _inputs(stage="linear"):
    return bl.Inputs(stage, Path("D:/S/osc_linear.xisf"), {"L": Path("D:/S/M31_L_linear.xisf")})


def test_blend_config_rc16_bxt_modes():
    o = bl.Options(target="M31")
    c = bl.blend_config(o, _inputs(), Path("D:/S/Blend/x"))
    assert c["rc16_bxt"] == {"mode": "auto", "why_off": "", "stars": 0.25, "nonstellar": 0.5}
    c = bl.blend_config(bl.Options(target="M31", rc16_bxt="off"), _inputs(), Path("D:/S/Blend/x"))
    assert c["rc16_bxt"]["mode"] == "off"
    c = bl.blend_config(bl.Options(target="M31", rc16_bxt="on"), _inputs("final"), Path("D:/S/Blend/x"))
    assert c["rc16_bxt"]["mode"] == "off" and "stage final" in c["rc16_bxt"]["why_off"]
    with pytest.raises(pjsr.PjsrError):
        bl.blend_config(bl.Options(target="M31", bxt_nonstellar=-0.1), _inputs(), Path("D:/S/Blend/x"))
    out = bl.render(c)
    assert pjsr.check(out) == []


def test_blend_rc16_bxt_mode_from_config():
    assert bl.rc16_bxt_mode(SimpleNamespace()) == "auto"
    assert bl.rc16_bxt_mode(SimpleNamespace(blend_rc16_bxt=False, finish_use_rcastro="on")) == "off"
    assert bl.rc16_bxt_mode(SimpleNamespace(blend_rc16_bxt=True, finish_use_rcastro="off")) == "off"
    assert bl.config_bxt(SimpleNamespace(finish_use_rcastro="on", bxt_sharpen_nonstellar=0.9)) == {
        "rc16_bxt": "on", "bxt_stars": 0.25, "bxt_nonstellar": 0.9}


def test_blend_script_runs_bxt_on_the_rc16_luminance_before_the_solve():
    s = _js("blend_rc16_osc.js")
    main = _func(s, "main")
    assert main.index("rc16Luminance()") < main.index("try { rc16Bxt(rc); }") < main.index('plateSolve(osc, "osc"')
    f = _func(s, "rc16Bxt")
    assert 'typeof BlurXTerminator !== "function"' in f
    assert f.index('cfg.mode === "off"') < f.index("new BlurXTerminator")
    assert f.index('trySet(B1, "correct_only", true') < f.index('trySet(B2, "sharpen_stars", cfg.stars')
    assert "rcastro: { rc16_bxt: RC16_BXT }" in _func(s, "writeSteps")


# --------------------------------------------------------------- stubbed run (node)

NODE = shutil.which("node")

PRELUDE = """
var LOGS = [], STEPS = [], CALLS = [], RC = {};
function log(s) { LOGS.push("" + s); }
function step(n, t, st, se) { STEPS.push({ step: n, tool: t, status: st, settings: se || {} }); }
var BXT_STARS = 0.25, BXT_NONSTELLAR = 0.5, NXT_DENOISE = 0.8, NXT_DETAIL = 0.15;
var DECONV = "on", DECONV_STRENGTH = 0.5, DENOISE = 0.5, GRAXPERT_VERSION = "1.0";
"""

DUMP = "\nconsole.log(JSON.stringify({ steps: STEPS, rc: RC, calls: CALLS, logs: LOGS }));\n"


def _run(scenario: str, call: str, funcs: tuple[str, ...]) -> dict:
    src = _functions(_js())
    prog = PRELUDE + scenario + "\n" + "\n".join(src[f] for f in funcs) + "\n" + call + DUMP
    r = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


RC_FUNCS = ("rcInstalled", "rcUse", "trySet", "rcInfo")


def _bxt_stub(fail_sharpen=False):
    return """
function BlurXTerminator() { this.ai_file = "BlurXTerminator.4.pb"; }
BlurXTerminator.prototype.processVersion = function () { return "2.0.1"; };
BlurXTerminator.prototype.executeOn = function (v) {
   CALLS.push({ tool: "bxt", correct_only: this.correct_only, stars: this.sharpen_stars,
                nonstellar: this.sharpen_nonstellar, psf_auto: this.auto_nonstellar_psf });
   if (%s && !this.correct_only) throw new Error("GPU out of memory");
   return true;
};
""" % ("true" if fail_sharpen else "false")


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_stub_bxt_two_passes_and_steps_record():
    d = _run(_bxt_stub() + 'var RC_MODE = "auto", USE_RC = true, GRAXPERT = "";',
             "deconvolve({});", RC_FUNCS + ("bxtDeconvolve", "deconvolve"))
    assert [c["correct_only"] for c in d["calls"]] == [True, False]
    assert d["calls"][1]["stars"] == 0.25 and d["calls"][1]["nonstellar"] == 0.5 and d["calls"][1]["psf_auto"]
    st = d["steps"][-1]
    assert st["step"] == "deconvolution" and st["tool"] == "BlurXTerminator" and st["status"] == "ran"
    assert st["settings"]["passes"] == ["correct_only", "sharpen"]
    assert d["rc"]["bxt"]["status"] == "used"
    assert d["rc"]["bxt"]["version"] == "2.0.1" and d["rc"]["bxt"]["ai_model"] == "BlurXTerminator.4.pb"


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_stub_bxt_sharpen_failure_falls_back_to_graxpert():
    gx = 'function graxpert(view, cmd, s) { CALLS.push("gx:" + cmd); return 3; }'
    d = _run(_bxt_stub(fail_sharpen=True) + gx + 'var RC_MODE = "auto", USE_RC = true, GRAXPERT = "gx.exe";',
             "deconvolve({});", RC_FUNCS + ("bxtDeconvolve", "deconvolve"))
    assert "gx:deconv-obj" in d["calls"] and "gx:deconv-stellar" in d["calls"]
    st = d["steps"][-1]
    assert st["tool"] == "GraXpert" and st["status"] == "ran" and "sharpen pass failed" in st["settings"]["rcastro"]
    assert d["rc"]["bxt"]["status"] == "partial" and "GPU out of memory" in d["rc"]["bxt"]["sharpen_error"]


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_stub_bxt_missing_or_off_is_recorded_with_why():
    d = _run('var RC_MODE = "on", USE_RC = true, GRAXPERT = "";', "deconvolve({});",
             RC_FUNCS + ("bxtDeconvolve", "deconvolve"))
    assert d["rc"]["bxt"]["status"] == "skipped" and "not installed" in d["rc"]["bxt"]["why"]
    assert any(line.startswith("WARNING: finish_use_rcastro on") for line in d["logs"])
    assert d["steps"][-1] == {"step": "deconvolution", "tool": "none", "status": "skipped",
                              "settings": {"reason": "no BlurXTerminator or GraXpert"}}
    off = _run(_bxt_stub() + 'var RC_MODE = "off", USE_RC = false, GRAXPERT = "";', "deconvolve({});",
               RC_FUNCS + ("bxtDeconvolve", "deconvolve"))
    assert off["calls"] == [] and off["rc"]["bxt"]["why"] == "finish_use_rcastro off"


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_stub_nxt_used_then_failure_falls_back_to_mlt():
    nxt = """
function NoiseXTerminator() {}
NoiseXTerminator.prototype.executeOn = function () {
   CALLS.push({ tool: "nxt", denoise: this.denoise, detail: this.detail });
   if (FAIL_NXT) throw new Error("model missing"); return true; };
function MultiscaleLinearTransform() {}
function mltDenoise(view, amt) { CALLS.push("mlt:" + amt); }
var RC_MODE = "auto", USE_RC = true, GRAXPERT = "";
"""
    ok = _run(nxt + "var FAIL_NXT = false;", "denoise({});", RC_FUNCS + ("nxtDenoise", "denoise"))
    assert ok["calls"] == [{"tool": "nxt", "denoise": 0.8, "detail": 0.15}]
    assert ok["steps"][-1]["tool"] == "NoiseXTerminator" and ok["rc"]["nxt"]["status"] == "used"
    bad = _run(nxt + "var FAIL_NXT = true;", "denoise({});", RC_FUNCS + ("nxtDenoise", "denoise"))
    assert "mlt:0.5" in bad["calls"]                     # the built-in at DENOISE, not NXT_DENOISE
    assert bad["steps"][-1]["tool"] == "MLT" and bad["rc"]["nxt"]["status"] == "failed"


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_stub_sxt_failure_falls_back_to_starnet2():
    stubs = """
var STARS = "on", STARNET_PRE_BG = 0.25, RC_MODE = "auto", USE_RC = true;
function mtfv(m, x) { return 0.01; }
function applyHT(v, c0, m) { CALLS.push("ht:" + m); }
function pixelMath(v, e) { CALLS.push("pm"); }
function Win(id) { this.mainView = { id: id, image: { median: function () { return 0.002; } } }; }
Win.prototype.forceClose = function () { CALLS.push("close:" + this.mainView.id); };
function dupWindow(view, id) { return new Win(id); }
var ImageWindow = { windowById: function () { return { isNull: true }; } };
function StarXTerminator() {}
StarXTerminator.prototype.executeOn = function () { CALLS.push("sxt:" + this.unscreen); throw new Error("no GPU"); };
function StarNet2() {}
StarNet2.prototype.executeOn = function () { CALLS.push("starnet2"); return true; };
"""
    call = "var r = starSplit({ image: { median: function () { return 0.002; } } }); RC.split_tool = r ? r.tool : null;"
    d = _run(stubs, call, RC_FUNCS + ("starlessPre", "starSplit"))
    assert d["calls"].index("sxt:true") < d["calls"].index("starnet2")
    assert d["rc"]["split_tool"] == "StarNet2"
    assert d["rc"]["sxt"]["status"] == "failed" and "no GPU" in d["rc"]["sxt"]["why"]
