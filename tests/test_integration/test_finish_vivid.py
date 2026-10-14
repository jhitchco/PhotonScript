"""Vivid finish variant (<name>_vivid.{xisf,tif,jpg}: more saturation and a
pink / warm / blue chroma lift through a luminance-range mask, natural stars)
and the gold palette only for emission targets (finish_palette_kinds)."""
import json
import re
import shutil
import subprocess

import pytest
from typer.testing import CliRunner

from photonscript import cli
from photonscript.integration import autointegrate as ai
from photonscript.integration import finish as fn
from photonscript.integration import pipeline as pl
from photonscript.integration import pjsr
from photonscript.shared.config import PhotonScriptConfig

NODE = shutil.which("node")
FCFG = {"out": "D:/X/out", "masters": [{"name": "M31_OSC", "path": "D:/X/out/master/master_OSC.xisf"}],
        "ra_deg": 10.68, "dec_deg": 41.27, "pi_library": "C:/PI/library"}


def _js() -> str:
    return pjsr.template(pjsr.FINISH_TEMPLATE)


def _var(js: str, name: str) -> str:
    return re.search(rf"^var {name}\s*=\s*(.+?);", js, re.M).group(1)


def _func(s: str, name: str) -> str:
    i = s.index(f"function {name}(")
    j = s.find("\nfunction ", i + 1)
    return s[i:j if j > 0 else len(s)]


def _math() -> str:
    s = _js()
    return s[s.index("// ==== PS_VIVID_MATH_BEGIN"):s.index("// ==== PS_VIVID_MATH_END")]


def _node(body: str) -> dict:
    r = subprocess.run([NODE, "-e", body], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


# ----------------------------------------------------------------- render

def test_render_defaults_and_overrides():
    out = pjsr.render_finish(FCFG)
    assert pjsr.check(out) == []
    assert _var(out, "VIVID") == '"on"' and _var(out, "VIVID_SAT_MID") == "0.72"
    assert _var(out, "VIVID_HUE_BOOST") == "0.25" and _var(out, "VIVID_LHE") == "0.0"
    assert _var(out, "PALETTE_WHY") == '""' and _var(out, "SAT_MID") == "0.64"
    off = pjsr.render_finish({**FCFG, "vivid": "off", "vivid_sat_mid": 0.8, "vivid_hue_boost": 0.4,
                              "vivid_lhe": 0.3, "palette_why": "gold palette skipped: M31 is a galaxy"})
    assert _var(off, "VIVID") == '"off"' and _var(off, "VIVID_SAT_MID") == "0.8"
    assert _var(off, "VIVID_LHE") == "0.3" and "M31 is a galaxy" in _var(off, "PALETTE_WHY")
    assert pjsr.check(off) == []
    for bad in ({"vivid_sat_mid": 0.3}, {"vivid_hue_boost": 1.5}, {"vivid_lhe": -0.1}):
        with pytest.raises(pjsr.PjsrError):
            pjsr.render_finish({**FCFG, **bad})


def test_rendered_script_is_ascii_and_parses(tmp_path):
    for cfg in (FCFG, {**FCFG, "vivid": False, "vivid_lhe": 0.4}):
        out = pjsr.render_finish(cfg)
        out.encode("ascii")
        assert pjsr.check(out) == []
        if NODE:
            p = tmp_path / "f.js"
            p.write_text("\n".join(x for x in out.splitlines() if not x.startswith("#")), encoding="ascii")
            r = subprocess.run([NODE, "--check", str(p)], capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, r.stderr


def test_ps1_fills_the_vivid_placeholders():
    ps1 = (pjsr.DEPLOY / "run-finish-osc.ps1").read_text(encoding="ascii")
    d = pjsr.FINISH_DEFAULTS
    assert "[ValidateSet('on','off')][string]$Vivid = 'on'" in ps1
    assert "[double]$VividSatMid = %s" % d["vivid_sat_mid"] in ps1
    assert "[double]$VividHueBoost = %s" % d["vivid_hue_boost"] in ps1
    for ph in ("__VIVID__", "__VIVID_SAT_MID__", "__VIVID_HUE_BOOST__", "__VIVID_LHE__", "__PALETTE_WHY__"):
        assert f"Replace('{ph}'" in ps1, ph


# ----------------------------------------------------------------- script structure

def test_vivid_runs_after_the_natural_is_saved_and_the_natural_chain_is_unchanged():
    s = _js()
    main = _func(s, "main")
    assert main.index('"_final.jpg", "jpg")') < main.index("vividFinish(finalKw, to16)") \
        < main.index("if (hooWin) hooFinish(hooWin);") < main.index("if (palWin) paletteFinish(palWin);")
    # the natural flow is the same: star split, recombine or the plain stretch + core HDR
    assert "if (!split || !starRecombine(v, split)) {" in main
    assert main.index("vividCapture(v, null)") < main.index("frameCrop(v, EDGE_FRAC)")
    rec = _func(s, "starRecombine")
    # copies taken after the star stretch, before the screen back into the natural
    assert rec.index("applyHT(stv, cs, p.m)") < rec.index('typeof vividCapture === "function"') \
        < rec.index('pixelMath(slv, "~(~$T * ~("')
    # the stretch keeps the natural saturation
    assert "CT.S = [[0, 0], [0.5, SAT_MID], [1, 1]];" in _func(s, "stretch")


def test_vivid_lives_outside_the_shared_library():
    s = _js()
    lib = s[s.index("// ==== PS_FINISH_LIB_BEGIN"):s.index("// ==== PS_FINISH_LIB_END")]
    assert "function vivid" not in lib and "VIVID" not in lib
    assert s.index("// ==== PS_FINISH_LIB_END") < s.index("function vividFinish(")


def test_vivid_is_guarded_and_names_its_outputs():
    s = _js()
    f = _func(s, "vividFinish")
    assert 'VIVID !== "on"' in f and '"-Vivid off"' in f and '"mono master"' in f
    for out in ('"_vivid.xisf"', '"_vivid.tif"', '"_vivid.jpg"'):
        assert out in f
    assert 'log("vivid SKIPPED (" + e + "); the natural final is unaffected")' in f
    assert 'typeof LocalHistogramEqualization === "undefined"' in f
    assert 'log("vivid: local contrast SKIPPED ("' in f
    assert "if (VIVID_LHE > 0)" in f
    assert "try { win.forceClose(); }" in f and "src.stars.forceClose()" in f
    cap = _func(s, "vividCapture")
    assert "!view.image.isColor" in cap and "catch (e)" in cap


def test_palette_skip_reason_is_logged():
    s = _js()
    assert "PALETTE_SKIP_WHY = PALETTE_WHY;" in _func(s, "main")
    src = _func(s, "paletteSource")
    assert "if (PALETTE_SKIP_WHY) log(PALETTE_SKIP_WHY);" in src
    assert 'reason: PALETTE_SKIP_WHY || "-Palette off"' in src


# ----------------------------------------------------------------- math (node)

@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_sat_curve_steps_from_the_natural_to_the_vivid_midpoint():
    d = _node(_math() + """
var c = vividSatCurve(0.64, 0.72), same = vividSatCurve(0.64, 0.64);
var e = Math.log(0.72) / Math.log(0.64);
console.log(JSON.stringify({ c: c, same: same, at64: Math.pow(0.64, e) }));
""")
    c = d["c"]
    assert c[0] == [0, 0] and c[-1] == [1, 1]
    ys = [p[1] for p in c]
    assert ys == sorted(ys) and all(p[1] >= p[0] for p in c)          # monotonic, only adds saturation
    assert d["at64"] == pytest.approx(0.72, abs=1e-9)                  # natural 0.5 -> 0.64 -> vivid 0.72
    assert all(p[0] == pytest.approx(p[1], abs=1e-5) for p in d["same"])


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_mask_spares_background_and_core():
    d = _node(_math() + """
var bg = 0.12, out = {};
[0.05, 0.12, 0.14, 0.20, 0.24, 0.4, 0.6, 0.7, 0.8, 0.9, 0.97].forEach(function (L) { out[L] = vividMask(L, bg); });
console.log(JSON.stringify(out));
""")
    assert d["0.05"] == 0 and d["0.12"] == 0 and d["0.14"] == pytest.approx(0, abs=1e-12)
    assert 0 < d["0.2"] < 1 and d["0.24"] == 1 and d["0.4"] == 1 and d["0.6"] == 1
    assert 0 < d["0.8"] < 1 and d["0.9"] == 0 and d["0.97"] == 0


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_hue_lift_by_band_and_region():
    d = _node(_math() + """
console.log(JSON.stringify({
   red: vividHue(1, 0, 0), yellow: vividHue(1, 1, 0), blue: vividHue(0, 0, 1), gray: vividHue(0.3, 0.3, 0.3),
   pinkArm: vividHueWeight(vividHue(0.9, 0.5, 0.7), 0.2), pinkCore: vividHueWeight(vividHue(0.9, 0.5, 0.7), 0.6),
   warmArm: vividHueWeight(vividHue(0.9, 0.7, 0.4), 0.2), warmCore: vividHueWeight(vividHue(0.9, 0.7, 0.4), 0.6),
   blueArm: vividHueWeight(vividHue(0.4, 0.55, 0.9), 0.2), blueCore: vividHueWeight(vividHue(0.4, 0.55, 0.9), 0.6),
   green: vividHueWeight(vividHue(0.3, 0.8, 0.3), 0.4), grayW: vividHueWeight(-1, 0.4) }));
""")
    assert d["red"] == 0 and d["yellow"] == pytest.approx(1 / 6) and d["blue"] == pytest.approx(2 / 3)
    assert d["gray"] == -1 and d["grayW"] == 0 and d["green"] == 0
    assert d["pinkArm"] > 0.5 and d["pinkCore"] > 0.5                 # pink everywhere in the mask
    assert d["warmCore"] > 0.5 and d["warmArm"] < 0.1                  # warm yellow in the core region
    assert d["blueArm"] > 0.5 and d["blueCore"] < 0.1                  # blue in the outer arms


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_blend_keeps_background_core_and_gray_and_lifts_pink_without_clipping():
    d = _node(_math() + """
// pixels: background, bright core, gray arm, pink arm, saturated pink arm near clipping
var px = [[0.12, 0.12, 0.125], [0.93, 0.92, 0.90], [0.3, 0.3, 0.3], [0.42, 0.30, 0.36], [0.98, 0.25, 0.6]];
var N = px.length, c = [[], [], []], s = [[], [], []];
for (var i = 0; i < N; ++i) for (var ch = 0; ch < 3; ++ch) {
   c[ch].push(px[i][ch]);
   s[ch].push(px[i][ch]);
}
var r = vividBlend(c, s, N, 0.12, 0.25);
var outp = [];
for (var j = 0; j < N; ++j) outp.push([c[0][j], c[1][j], c[2][j]]);
console.log(JSON.stringify({ out: outp, mask: Array.prototype.slice.call(r.mask),
   hueIn: vividHue(0.42, 0.30, 0.36), hueOut: vividHue(outp[3][0], outp[3][1], outp[3][2]),
   before: r.chroma_before, after: r.chroma_after }));
""")
    out = d["out"]
    assert out[0] == pytest.approx([0.12, 0.12, 0.125])        # sky background untouched
    assert out[1] == pytest.approx([0.93, 0.92, 0.90])         # brightest core untouched
    assert out[2] == pytest.approx([0.3, 0.3, 0.3])            # neutral stays neutral
    chroma = lambda p: max(p) - min(p)                          # noqa: E731
    assert chroma(out[3]) > chroma([0.42, 0.30, 0.36]) * 1.15  # pink lifted
    assert d["hueOut"] == pytest.approx(d["hueIn"], abs=1e-6)   # hue kept
    assert all(0 <= v <= 1 for p in out for v in p)             # never clips
    assert d["mask"][0] == 0 and d["mask"][1] == 0 and d["mask"][3] == 1


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_vivid_finish_stubbed_run_saves_and_records():
    s = _js()
    funcs = "\n".join(_func(s, f) for f in ("vividRelease", "vividCapture", "vividFinish"))
    prog = _math() + """
var LOGS = [], STEPS = [], SAVED = [], PM = [];
function log(x) { LOGS.push("" + x); }
function step(n, t, st, se) { STEPS.push({ step: n, tool: t, status: st, settings: se || {} }); }
var VIVID = "on", VIVID_SAT_MID = 0.72, SAT_MID = 0.64, VIVID_HUE_BOOST = 0.25, VIVID_LHE = 0;
var BG_TARGET = 0.12, STAR_STRENGTH = 0.7, EDGE_FRAC = 0.015, NAME = "M31", FINAL = "F";
var VIVID_SRC = null, VIVID_ERR = "", UndoFlag_NoSwapFile = 0, CURVES = [];
function Rect(w, h) { this.w = w; this.h = h; }
function Img(color) {
   this.isColor = color; this.width = 2; this.height = 1; this.numberOfChannels = 3;
   this.d = [[0.4, 0.12], [0.3, 0.12], [0.36, 0.12]];
}
Img.prototype.getSamples = function (a, r, ch) { for (var i = 0; i < a.length; ++i) a[i] = this.d[ch][i]; };
Img.prototype.setSamples = function (a, r, ch) { for (var i = 0; i < a.length; ++i) this.d[ch][i] = a[i]; };
function Win(id, color) {
   var self = this; this.closed = false;
   this.mainView = { id: id, image: new Img(color), beginProcess: function () {}, endProcess: function () {},
                     window: self };
}
Win.prototype.forceClose = function () { this.closed = true; };
function dupWindow(view, id) { var w = new Win(id, view.image.isColor); w.mainView.image.d = JSON.parse(JSON.stringify(view.image.d)); return w; }
function CurvesTransformation() {}
CurvesTransformation.prototype.executeOn = function (v) { CURVES.push(this.S); return true; };
function SampleFormatConversion() {}
SampleFormatConversion.prototype.To16Bit = 1;
SampleFormatConversion.prototype.executeOn = function () { return true; };
function pixelMath(v, ex) { PM.push(ex); }
function frameCrop(v, f) { step("frame_crop", "Crop", "ran", {}); }
function trySave(w, p) { SAVED.push(p); return true; }
function trySet(P, k, v, set) { P[k] = v; set[k] = v; }
""" + funcs + """
var sl = new Win("starless", true), st = new Win("stars", true);
vividCapture(sl.mainView, st.mainView);
var src = VIVID_SRC;
vividFinish([], true);
console.log(JSON.stringify({ saved: SAVED, steps: STEPS, pm: PM, curves: CURVES, logs: LOGS,
   closed: [src.starless.closed, src.stars.closed], left: VIVID_SRC,
   px: src.starless.mainView.image.d }));
"""
    d = _node(prog)
    assert d["saved"] == ["F/M31_vivid.xisf", "F/M31_vivid.tif", "F/M31_vivid.jpg"]
    assert [x["step"] for x in d["steps"]] == ["vivid"]          # the frame crop is folded into the record
    rec = d["steps"][0]
    assert rec["status"] == "ran" and rec["settings"]["sub_steps"][0]["step"] == "frame_crop"
    assert rec["settings"]["sat_mid"] == 0.72 and rec["settings"]["local_contrast"] == "off"
    assert d["pm"] == ["~(~$T * ~(PS_vivid_stars * 0.700))"]
    assert d["curves"] and d["closed"] == [True, True] and d["left"] is None
    assert d["px"][0][1] == pytest.approx(0.12) and d["px"][1][1] == pytest.approx(0.12)   # background kept


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_vivid_finish_failure_is_logged_and_skipped():
    s = _js()
    funcs = "\n".join(_func(s, f) for f in ("vividRelease", "vividFinish"))
    d = _node(_math() + """
var LOGS = [], STEPS = [];
function log(x) { LOGS.push("" + x); }
function step(n, t, st, se) { STEPS.push({ step: n, tool: t, status: st, settings: se || {} }); }
var VIVID = "on", VIVID_SAT_MID = 0.72, SAT_MID = 0.64, VIVID_HUE_BOOST = 0.25, VIVID_LHE = 0;
var BG_TARGET = 0.12, STAR_STRENGTH = 0.7, NAME = "M31", VIVID_ERR = "";
var closed = 0;
var VIVID_SRC = { starless: { get mainView() { throw new Error("boom"); }, forceClose: function () { ++closed; } },
                  stars: null };
""" + funcs + """
vividFinish(null, true);
var a = { steps: STEPS, logs: LOGS, closed: closed };
VIVID = "off"; STEPS = []; vividFinish(null, true); a.off = STEPS;
VIVID = "on"; STEPS = []; vividFinish(null, true); a.mono = STEPS;
console.log(JSON.stringify(a));
""")
    assert d["steps"][0]["status"] == "failed" and "boom" in d["steps"][0]["settings"]["error"]
    assert any("vivid SKIPPED" in x for x in d["logs"]) and d["closed"] == 1
    assert d["off"][0]["status"] == "skipped" and d["off"][0]["settings"]["reason"] == "-Vivid off"
    assert d["mono"][0]["settings"]["reason"] == "mono master"


# ----------------------------------------------------------------- config + gold gating

def test_config_defaults_and_settings():
    c = PhotonScriptConfig(_env_file=None)
    assert c.finish_vivid is True and c.finish_vivid_sat_mid == 0.72
    assert c.finish_vivid_hue_boost == 0.25 and c.finish_vivid_lhe == 0.0
    assert c.finish_palette_kinds == "emission,nebula,remnant" == pjsr.PALETTE_KINDS
    assert pjsr.vivid_settings(c) == {"vivid": True, "vivid_sat_mid": 0.72, "vivid_hue_boost": 0.25,
                                      "vivid_lhe": 0.0}
    opts = pl.config_options(c, "piggyback")
    assert opts["vivid"] is True and opts["palette_kinds"] == "emission,nebula,remnant"
    c2 = PhotonScriptConfig(_env_file=None, finish_vivid=False, finish_vivid_sat_mid=0.8)
    assert pjsr.vivid_settings(c2)["vivid"] is False and pjsr.vivid_settings(c2)["vivid_sat_mid"] == 0.8
    with pytest.raises(pjsr.PjsrError):
        pjsr.vivid_settings(PhotonScriptConfig(_env_file=None, finish_vivid_sat_mid=0.2))
    from photonscript.scheduler.app import _CONFIG_FIELDS
    keys = {f[0] for f in _CONFIG_FIELDS}
    assert {"finish_vivid", "finish_vivid_sat_mid", "finish_vivid_hue_boost", "finish_vivid_lhe",
            "finish_palette_kinds"} <= keys


def test_gold_skipped_for_a_galaxy_kept_for_an_emission_nebula():
    k = pjsr.PALETTE_KINDS
    pal, why = pjsr.palette_for_target("gold", "M 31", k)
    assert pal == "off" and "galaxy" in why and "--palette gold" in why
    assert pjsr.palette_for_target("gold", "M13", k)[0] == "off"            # clusters too
    assert pjsr.palette_for_target("gold", "Heart Nebula", k) == ("gold", "")
    assert pjsr.palette_for_target("gold", "M42", k) == ("gold", "")
    assert pjsr.palette_for_target("gold", "M1", k) == ("gold", "")          # supernova remnant
    assert pjsr.palette_for_target("gold", "Not A Target 123", k) == ("gold", "")   # unknown: kept
    assert pjsr.palette_for_target("gold", "M31", "") == ("gold", "")       # explicit: no filter
    assert pjsr.palette_for_target("off", "M42", k) == ("off", "")


def _touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * 16)
    return p


def test_finish_dry_run_gates_the_palette_and_plumbs_vivid(tmp_path):
    master = _touch(tmp_path / "loose" / "masterOSC.xisf")
    base = dict(master=master, name="M31_v7", staging_root=tmp_path, dry_run=True, palette="gold",
                palette_kinds=pjsr.PALETTE_KINDS, pixinsight=str(tmp_path / "PI" / "bin" / "PixInsight.exe"))
    msgs = []
    res = fn.run(fn.Options(target="M 31", **base), echo=msgs.append)
    c = res["config"]
    assert c["palette"] == "off" and "galaxy" in c["palette_why"] and res["script_problems"] == []
    assert any("gold palette skipped" in m for m in msgs)
    assert c["vivid"] == "on" and c["vivid_sat_mid"] == 0.72
    js = pjsr.render_finish(c)
    assert _var(js, "PALETTE") == '"off"' and "galaxy" in _var(js, "PALETTE_WHY")
    res = fn.run(fn.Options(target="Heart Nebula", **base), echo=lambda s: None)
    assert res["config"]["palette"] == "gold" and res["config"]["palette_why"] == ""
    res = fn.run(fn.Options(target="M 31", **{**base, "palette_kinds": ""}, vivid=False), echo=lambda s: None)
    assert res["config"]["palette"] == "gold" and res["config"]["vivid"] == "off"


def test_cli_finish_dry_run_vivid_and_palette(tmp_path, monkeypatch):
    master = _touch(tmp_path / "loose" / "masterOSC.xisf")
    monkeypatch.setattr(cli, "_config_for_repo",
                        lambda repo: PhotonScriptConfig(_env_file=None, finish_palette="gold"))
    args = ["finish", "--master", str(master), "--name", "M31_v7", "--staging-root", str(tmp_path),
            "--target", "M 31", "--dry-run", "--json"]

    def run(*extra):
        r = CliRunner().invoke(cli.app, args + list(extra))
        assert r.exit_code == 0, r.output
        return json.loads(r.output[r.output.index("{"):])["config"]

    c = run()
    assert c["palette"] == "off" and "galaxy" in c["palette_why"]       # configured gold, galaxy target
    assert c["vivid"] == "on" and c["vivid_sat_mid"] == 0.72 and c["vivid_lhe"] == 0.0
    c = run("--palette", "gold", "--no-vivid")
    assert c["palette"] == "gold" and c["palette_why"] == "" and c["vivid"] == "off"
    c = run("--vivid-sat-mid", "0.8", "--vivid-hue-boost", "0.4", "--vivid-lhe", "0.2")
    assert (c["vivid_sat_mid"], c["vivid_hue_boost"], c["vivid_lhe"]) == (0.8, 0.4, 0.2)
    r = CliRunner().invoke(cli.app, args + ["--vivid-sat-mid", "0.3"])
    assert r.exit_code == 2
    assert not (tmp_path / "Finish").exists()


def test_cli_integrate_has_vivid():
    r = CliRunner().invoke(cli.app, ["integrate", "--help"])
    assert r.exit_code == 0 and "--vivid" in r.output and "--no-vivid" in r.output


def test_review_lists_vivid_next_to_final(tmp_path):
    from PIL import Image
    fin = tmp_path / "out" / "final"
    fin.mkdir(parents=True)
    for n in ("H_OSC_final.jpg", "H_OSC_gold.jpg", "H_OSC_hoo.jpg", "H_OSC_vivid.jpg"):
        Image.new("RGB", (8, 8)).save(fin / n)
    assert [p.name for p in ai.finals(tmp_path)] == ["H_OSC_final.jpg", "H_OSC_vivid.jpg", "H_OSC_hoo.jpg",
                                                     "H_OSC_gold.jpg"]
    b = tmp_path / "b"
    (b / "out" / "final").mkdir(parents=True)
    for n in ("X_blend.jpg", "X_core.jpg", "X_blend_vivid.jpg"):
        Image.new("RGB", (8, 8)).save(b / "out" / "final" / n)
    assert [p.name for p in ai.finals(b, blend=True)] == ["X_blend.jpg", "X_blend_vivid.jpg", "X_core.jpg"]
