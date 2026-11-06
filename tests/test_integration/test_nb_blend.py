"""Narrowband two-rig blend (`photonscript blend --mode nb|auto`): the RC16
Ha / OIII / SII masters into the Piggy-600 color image (Heart Nebula)."""
import json
import re
import shutil
import subprocess
from collections import Counter

import pytest
from typer.testing import CliRunner

from photonscript import cli
from photonscript.integration import autointegrate as ai
from photonscript.integration import blend as bl
from photonscript.integration import mono_color as mc
from photonscript.integration import pjsr
from photonscript.shared import ledger as L

NODE = shutil.which("node")
HEART = "Heart Nebula"


def _touch(p, n=16):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * n)
    return p


def _run(root, name, rig, version, filters, *, linear=(), campaign=HEART, prefix="Heart_Nebula",
         created="2026-10-12T10:00:00Z"):
    d = root / name
    for f in filters:
        _touch(d / "out" / "master" / f"master_{f}.xisf")
    for f in linear:
        _touch(d / "out" / "final" / f"{prefix}_{f}_linear.xisf")
    L.save(d / "ledger.json", L.Ledger(campaign=campaign, rig=rig, run=name, version=version,
                                       kind="integration", created_at=created))
    return d


@pytest.fixture
def heart(tmp_path):
    root = tmp_path / "Staging"
    _run(root, "Heart_piggyback_1", "piggyback", 1, ["OSC"], linear=["OSC"])
    # PS-177 / hdr.py: Ha in two lengths -> master_Ha_hdr wins, exposure variants dropped
    _run(root, "Heart_rc16_1", "rc16", 1, ["Ha", "Ha_300s", "Ha_hdr", "OIII", "SII"])
    return root


def _ascii_js(name):
    return (pjsr.DEPLOY / name).read_bytes().decode("ascii")


def _rendered(heart, **kw):
    o = bl.Options(target=HEART, staging_root=heart, **kw)
    inp = bl.discover(o)
    cfg = bl.config_for(o, inp, heart / "Blend" / "x")
    return o, inp, cfg, bl.render(cfg)


def _functions(src):
    out = {}
    for m in re.finditer(r"^function (\w+)\s*\([^)]*\)\s*\{", src, re.M):
        depth, i = 1, m.end()
        while depth and i < len(src):
            depth += {"{": 1, "}": -1}.get(src[i], 0)
            i += 1
        out[m.group(1)] = src[m.start():i]
    return out


# ----------------------------------------------------------------- PJSR

def test_template_is_ascii_and_rule_clean():
    t = _ascii_js(bl.TEMPLATE_NB)
    assert pjsr.check(t) == ["CONFIG not filled"]
    assert t.index("#include <pjsr/SectionBar.jsh>") < t.index(pjsr.SOLVER_MARK) < t.index(pjsr.CONFIG_MARK)
    for mark in (bl.FINISH_LIB_MARK, bl.MONO_LIB_MARK, bl.BLEND_LIB_MARK):
        assert t.count(mark) == 1
    # the library regions exist in their sources
    assert bl.BLEND_LIB_BEGIN in _ascii_js(bl.TEMPLATE) and bl.BLEND_LIB_END in _ascii_js(bl.TEMPLATE)
    assert bl.MONO_LIB_BEGIN in _ascii_js(mc.TEMPLATE) and bl.MONO_LIB_END in _ascii_js(mc.TEMPLATE)


def test_rendered_script_is_clean_and_complete(heart):
    _, _, cfg, out = _rendered(heart, solve=True)
    assert pjsr.check(out) == []
    for mark in (bl.FINISH_LIB_MARK, bl.MONO_LIB_MARK, bl.BLEND_LIB_MARK, pjsr.CONFIG_MARK):
        assert mark not in out
    funcs = re.findall(r"^function (\w+)", out, re.M)
    # only the log overrides are declared twice (the template's win)
    assert sorted(n for n, c in Counter(funcs).items() if c > 1) == ["log", "writeLog"]
    assert out.rindex("function log(") > out.index("PS_BLEND_LIB_END")
    for s in ("solveSeeds", "starAlign", "wcsAffine", "footprintMask", "linearFit", "rc16Bxt",  # blend lib
              "paletteFinish", "starSplit", "deconvolve", "denoise",                            # finish lib
              "colorItem", "narrowbandStars", "fitTo",                                          # mono lib
              "fit2", "nbChannel", "productNbFull", "finishNatural", "coreStars", "coreItems"):
        assert s in funcs, s
    for s in ("_nb_linear.xisf", "_nb_mask.xisf", "_osc_ab.jpg", "_haRGB", "_blend_steps.json",
              "_core_stars_work.xisf", "timing_pi.csv", "blend.log", "EXIT OK",
              'typeof BlurXTerminator !== "function"', 'typeof ImageSolver === "undefined"'):
        assert s in out, s
    line = next(x for x in out.splitlines() if x.startswith("var CONFIG = "))
    got = json.loads(line[len("var CONFIG = "):-1])
    assert got["mode"] == "nb" and got["palette"] == "gold"
    assert got["nb"]["Ha"].endswith("master_Ha_hdr.xisf") and got["nb"]["SII"].endswith("master_SII.xisf")
    assert got["lum"] == {"filter": "Ha", "path": got["nb"]["Ha"], "weight": 0.3}
    assert (got["ha_weight"], got["oiii_weight"], got["ha_boost"], got["oiii_boost"]) == (0.8, 0.6, 0.15, 0.3)
    assert [i["name"] for i in got["items"]] == ["Heart_Nebula_core_SHO", "Heart_Nebula_core_HOO"]
    assert all(i["stars_from"] == "rgb" for i in got["items"])
    f = got["finish"]
    assert f["steps_file"].endswith("Heart_Nebula_core_color_steps.json") and f["gold_mix"] == 0.6
    assert f["rc_mode"] == "auto" and abs(f["ra_deg"] - 38.3) < 0.2


def test_hue_curves_match_their_sources():
    nb = _ascii_js(bl.TEMPLATE_NB)

    def curve(text, name):
        return re.search(rf"^var {name} = (\[.*\]);", text, re.M).group(1)

    assert curve(nb, "GOLD_HUE") == curve(_ascii_js(pjsr.FINISH_TEMPLATE), "GOLD_HUE")
    assert curve(nb, "SHO_HUE") == curve(_ascii_js(mc.TEMPLATE), "SHO_HUE")


def test_every_global_the_libraries_read_is_declared(heart):
    _, _, _, out = _rendered(heart)
    code = "\n".join(pjsr._strip_strings(x.split("//")[0]) for x in out.splitlines())
    declared = set(re.findall(r"\bvar\s+(\w+)", code)) | set(re.findall(r"function\s+(\w+)", code))
    used = set(re.findall(r"\b([A-Z][A-Z0-9_]{2,})\b", code))
    # BIG is a comma-declared local; the rest are properties / PJSR names
    assert used - declared <= {"BIG", "CIEXYZ", "JSON", "MAD", "OIII", "RGB", "SCNR", "SII"}


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_rendered_script_parses(heart, tmp_path):
    _, _, _, out = _rendered(heart)
    js = tmp_path / "nb.js"
    js.write_text("\n".join(x for x in out.splitlines() if not x.startswith("#")), encoding="ascii")
    r = subprocess.run([NODE, "--check", str(js)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr


def test_l_template_still_renders_unchanged_flow(tmp_path):
    root = tmp_path / "S"
    _run(root, "p", "piggyback", 1, ["OSC"], campaign="M31")
    _run(root, "r", "rc16", 1, ["L", "R", "G", "B"], campaign="M31")
    o = bl.Options(target="M31", staging_root=root)
    inp = bl.discover(o)
    assert inp.mode == "l" and "auto: no RC16 Ha master" in inp.mode_why
    out = bl.render(bl.config_for(o, inp, root / "Blend" / "x"))
    assert pjsr.check(out) == [] and "productNbFull" not in out and "solveSeeds(osc" in out


# ----------------------------------------------------------------- math (node)

def _node(prog):
    r = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def _math_prog(heart, body):
    _, _, _, out = _rendered(heart)
    f = _functions(out)
    lib = "\n".join(f[n] for n in ("solve3", "quantile", "subBox", "minOf3", "fit2", "nbChannel"))
    synth = """
var N = 4000, R = new Float32Array(N), G = new Float32Array(N), B = new Float32Array(N);
var HA = new Float32Array(N), M = new Float32Array(N), seed = 7;
function rnd() { seed = (seed * 16807) % 2147483647; return seed / 2147483647; }
for (var i = 0; i < N; ++i) {
   var cont = 0.02 + 0.1 * rnd() * rnd(), line = 0.3 * rnd();
   HA[i] = 0.01 + line + 0.05 * cont;
   R[i] = 0.005 + 0.4 * line + 1.2 * cont; G[i] = 0.004 + cont; B[i] = 0.006 + 1.1 * cont;
   M[i] = i < 3000 ? 1 : 0;
}
"""
    return lib + synth + body


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_channel_fit_recovers_line_and_continuum_and_keeps_outside(heart):
    d = _node(_math_prog(heart, """
var C = minOf3(R, G, B, N);
var f = fit2(R, HA, C, function (i) { return M[i] > 0.5; }, N, true);
var R0 = Array.prototype.slice.call(R);
var rec = nbChannel(R, HA, C, M, N, 0.8, 0.0, "R", "Ha");
var outside = 0;
for (var i = 3000; i < N; ++i) if (R[i] !== R0[i]) ++outside;
var err = 0;
for (var j = 0; j < 3000; ++j) err = Math.max(err, Math.abs(R[j] - R0[j]));
console.log(JSON.stringify({ f: f, rec: rec, outside: outside, err: err }));
"""))
    f = d["f"]
    # R = 0.005 + 0.4 line + 1.2 cont with HA = 0.01 + line + 0.05 cont and C = G = cont + 0.004:
    # the model is exact (b 0.4, c 1.18), so the blend changes nothing
    assert abs(f["b"] - 0.4) < 0.01 and abs(f["c"] - 1.18) < 0.02 and f["rms"] < 1e-4
    assert d["rec"]["status"] == "ran" and d["rec"]["continuum"].startswith("C = min")
    assert d["outside"] == 0 and d["err"] < 1e-3


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_channel_boost_adds_line_and_guards_skip(heart):
    d = _node(_math_prog(heart, """
var C = minOf3(R, G, B, N);
var R0 = Array.prototype.slice.call(R);
var boost = nbChannel(R, HA, C, M, N, 0.8, 0.5, "R", "Ha");
var up = 0; for (var i = 0; i < 3000; ++i) up += R[i] - R0[i];
var X = new Float32Array(N); for (var k = 0; k < N; ++k) X[k] = 0.4 - 0.5 * HA[k];
var X0 = Array.prototype.slice.call(X);
var neg = nbChannel(X, HA, C, M, N, 0.6, 0.3, "G", "OIII");
var same = 0; for (var j = 0; j < N; ++j) if (X[j] === X0[j]) ++same;
var none = nbChannel(B, HA, C, new Float32Array(N), N, 0.6, 0.3, "B", "OIII");
var off = nbChannel(B, HA, C, M, N, 0, 0, "B", "OIII");
console.log(JSON.stringify({ boost: boost, up: up / 3000, neg: neg, same: same, none: none, off: off }));
"""))
    assert d["boost"]["status"] == "ran" and d["up"] > 0.01          # extra Ha on top of R
    assert d["neg"]["status"] == "skipped" and "no positive OIII correlation" in d["neg"]["reason"]
    assert d["same"] == 4000                                          # a skipped channel is untouched
    assert d["none"]["status"] == "skipped" and "overlap samples" in d["none"]["reason"]
    assert d["off"]["reason"] == "weight and boost 0"


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_negative_continuum_refits_without_c(heart):
    d = _node(_math_prog(heart, """
for (var i = 0; i < N; ++i) R[i] = 0.01 + 0.5 * (HA[i] - 0.01) + 0.2 * (0.3 - G[i]);
var rec = nbChannel(R, HA, minOf3(R, G, B, N), M, N, 0.8, 0, "R", "Ha");
var sb = subBox(new Float32Array([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]), 4, [1, 1, 3, 3]);
console.log(JSON.stringify({ rec: rec, sb: Array.prototype.slice.call(sb) }));
"""))
    assert d["rec"]["fit"]["c"] == 0 and "refit without C" in d["rec"]["continuum"]
    assert d["sb"] == [5, 6, 9, 10]


# ----------------------------------------------------------------- discovery + mode

def test_discover_nb_masters_prefers_hdr(heart):
    inp = bl.discover(bl.Options(target=HEART, staging_root=heart))
    assert inp.mode == "nb" and inp.mode_why == "auto: RC16 Ha, OIII, SII"
    nb = inp.nb()
    assert nb["Ha"].name == "master_Ha_hdr.xisf" and set(nb) == {"Ha", "OIII", "SII"}
    assert "Ha_300s" not in inp.rc16 and inp.lum_filters() == ["Ha"]
    assert any("Ha -> R, OIII -> G and B" in n for n in inp.notes)


def test_discover_nb_uses_finished_linear_and_aliases(tmp_path):
    root = tmp_path / "S"
    _run(root, "p", "piggyback", 1, ["OSC"], linear=["OSC"])
    _run(root, "r", "rc16", 1, ["H", "O3"], linear=["H", "O3"])
    inp = bl.discover(bl.Options(target="IC 1805", staging_root=root))
    assert inp.mode == "nb"
    assert inp.nb() == {"Ha": root / "r" / "out" / "final" / "Heart_Nebula_H_linear.xisf",
                        "OIII": root / "r" / "out" / "final" / "Heart_Nebula_O3_linear.xisf"}


@pytest.mark.parametrize("filters,mode,stage,want", [
    (["Ha", "OIII", "SII"], "auto", "linear", "nb"),
    (["Ha"], "auto", "linear", "nb"),
    (["L", "R", "G", "B"], "auto", "linear", "l"),
    (["L", "Ha"], "auto", "linear", "nb"),
    (["Ha", "OIII"], "auto", "final", "l"),
    (["Ha", "OIII"], "l", "linear", "l"),
    (["Ha"], "nb", "linear", "nb"),
    (["Halpha"], "narrowband", "linear", "nb"),
])
def test_select_mode(filters, mode, stage, want):
    assert bl.select_mode({f: f"m_{f}" for f in filters}, mode, stage)[0] == want


def test_select_mode_errors():
    with pytest.raises(bl.BlendError, match="needs an RC16 Ha"):
        bl.select_mode({"L": "x", "OIII": "y"}, "nb")
    with pytest.raises(bl.BlendError, match="--stage linear"):
        bl.select_mode({"Ha": "x"}, "nb", "final")
    with pytest.raises(bl.BlendError, match="--mode must be"):
        bl.parse_mode("hdr")


def test_mode_nb_picks_the_newest_run_with_ha(tmp_path):
    root = tmp_path / "S"
    _run(root, "p", "piggyback", 1, ["OSC"])
    _run(root, "r1", "rc16", 1, ["Ha", "OIII"])
    _run(root, "r2", "rc16", 2, ["L"])                     # newer, no narrowband
    auto = bl.discover(bl.Options(target=HEART, staging_root=root))
    assert auto.rc16_run.run_dir.name == "r2" and auto.mode == "l"
    nb = bl.discover(bl.Options(target=HEART, staging_root=root, mode="nb"))
    assert nb.rc16_run.run_dir.name == "r1" and nb.mode == "nb"
    l_only = bl.discover(bl.Options(target=HEART, staging_root=root, mode="l"))
    assert l_only.mode == "l" and l_only.rc16_run.run_dir.name == "r2"


def test_l_master_carries_the_luminance_in_nb(tmp_path):
    root = tmp_path / "S"
    _run(root, "p", "piggyback", 1, ["OSC"])
    _run(root, "r", "rc16", 1, ["L", "Ha"])
    o = bl.Options(target=HEART, staging_root=root, weight=0.6)
    inp = bl.discover(o)
    cfg = bl.config_for(o, inp, root / "Blend" / "x")
    assert cfg["lum"]["filter"] == "L" and cfg["lum"]["weight"] == 0.6
    assert cfg["items"] == [] and any("no SHO / HOO core" in n for n in inp.notes)
    assert bl.channel_plan(inp, cfg["items"]) == {"R": ["Ha"], "G": [], "B": [], "L": ["L"], "core": []}


def test_weights_are_checked(heart):
    o = bl.Options(target=HEART, staging_root=heart, oiii_boost=1.5)
    inp = bl.discover(o)
    with pytest.raises(bl.BlendError, match="--oiii-boost"):
        bl.config_for(o, inp, heart / "Blend" / "x")


def test_core_items_without_sii_is_hoo_only(tmp_path):
    root = tmp_path / "S"
    _run(root, "p", "piggyback", 1, ["OSC"])
    _run(root, "r", "rc16", 1, ["Ha", "OIII"])
    o = bl.Options(target=HEART, staging_root=root)
    items, _ = bl.core_items(o, bl.discover(o))
    assert [i["kind"] for i in items] == ["hoo"] and items[0]["channels"]["G"] == items[0]["channels"]["B"]
    assert bl.core_items(bl.Options(target=HEART, staging_root=root, core=False), bl.discover(o))[0] == []


# ----------------------------------------------------------------- run (faked PixInsight)

NB_OUTPUTS = ("_nb_linear.xisf", "_nb_mask.xisf", "_nb_blend.jpg", "_osc_ab.jpg", "_haRGB.xisf",
              "_haRGB.tif", "_haRGB.jpg", "_haRGB_gold.xisf", "_haRGB_gold.jpg", "_haRGB_final_steps.json",
              "_core_SHO.xisf", "_core_SHO.tif", "_core_SHO.jpg", "_core_HOO.xisf", "_core_HOO.tif",
              "_core_HOO.jpg", "_core_color_steps.json")


def test_run_nb_with_faked_pixinsight(heart, monkeypatch):
    calls = []

    def fake_run(script, log_path, **k):
        calls.append(script)
        text = script.read_text("ascii")
        assert "function productNbFull(" in text and pjsr.check(text) == []
        fin = log_path.parent / "final"
        for sfx in NB_OUTPUTS:
            _touch(fin / f"Heart_Nebula{sfx}")
        (fin / "Heart_Nebula_blend_steps.json").write_text(json.dumps({
            "mode": "nb", "result": "ok",
            "channels": {"R": ["Ha"], "G": ["OIII"], "B": ["OIII"], "L": ["Ha"]},
            "products": {"full": {"ok": True, "registration": "StarAlignment"},
                         "natural": {"ok": True, "gold": True},
                         "core": {"ok": True, "made": ["Heart_Nebula_core_SHO", "Heart_Nebula_core_HOO"]}}}))
        log_path.write_text("STAGE START x\nEXIT OK\n")
        return {"ok": True, "exit_code": 0, "minutes": 9.5, "last_line": "EXIT OK", "timed_out": False}

    monkeypatch.setattr(bl.runner, "run_script", fake_run)
    res = bl.run(bl.Options(target=HEART, staging_root=heart), echo=lambda s: None)
    assert len(calls) == 1 and res["mode"] == "nb"
    rd = calls[0].parent
    assert rd.parent == heart / "Blend" and rd.name.startswith("Heart_Nebula_blend_nb_")
    man = json.loads((rd / "manifest.json").read_text("ascii"))
    assert man["mode"] == "nb" and man["config"]["mode"] == "nb"
    assert man["channels"] == {"R": ["Ha"], "G": ["OIII"], "B": ["OIII"], "L": ["Ha"], "core": ["SHO", "HOO"]}
    led = L.load(rd / "ledger.json")
    assert led.kind == "blend" and led.rig == "piggyback" and led.campaign == HEART
    m = led.machine
    assert m["mode"] == "nb" and m["channels"]["R"] == ["Ha"] and m["channels"]["core"] == ["SHO", "HOO"]
    assert m["blend"]["ok"] is True and m["blend"]["script"].endswith("(deploy/blend_nb_osc.js)")
    assert m["blend"]["channels_applied"] == {"R": ["Ha"], "G": ["OIII"], "B": ["OIII"], "L": ["Ha"]}
    assert m["blend"]["products"]["natural"]["gold"] is True
    assert m["options"]["ha_weight"] == 0.8 and m["options"]["palette"] == "gold"
    assert m["options"]["lum"]["filter"] == "Ha"
    for sfx in NB_OUTPUTS:
        assert any(p.endswith(f"Heart_Nebula{sfx}") for p in res["outputs"]), sfx
    # the autointegrate review shows the narrowband products first
    names = [p.name for p in ai.finals(rd, blend=True)]
    assert names[:4] == ["Heart_Nebula_haRGB.jpg", "Heart_Nebula_haRGB_gold.jpg",
                         "Heart_Nebula_core_SHO.jpg", "Heart_Nebula_core_HOO.jpg"]
    # blend idempotence: the same inputs are not blended again
    inp = bl.discover(bl.Options(target=HEART, staging_root=heart))
    assert ai.already_blended(heart, HEART, ai.blend_inputs_key(inp)) == rd.name


def test_cli_dry_run_modes(heart):
    r = CliRunner().invoke(cli.app, ["blend", "--target", HEART, "--staging-root", str(heart),
                                     "--dry-run", "--json", "--ha-weight", "0.5", "--palette", "off"])
    assert r.exit_code == 0, r.output
    res = json.loads(r.output[r.output.index("{"):])
    assert res["mode"] == "nb" and res["script_problems"] == []
    assert res["config"]["ha_weight"] == 0.5 and res["config"]["palette"] == "off"
    assert not (heart / "Blend").exists()
    r = CliRunner().invoke(cli.app, ["blend", "--target", HEART, "--staging-root", str(heart),
                                     "--dry-run", "--json", "--mode", "l"])
    assert r.exit_code == 0, r.output
    res = json.loads(r.output[r.output.index("{"):])
    assert res["mode"] == "l" and res["config"]["mode"] == "l"
    r = CliRunner().invoke(cli.app, ["blend", "--target", HEART, "--staging-root", str(heart),
                                     "--dry-run", "--mode", "hdr"])
    assert r.exit_code == 1 and "--mode must be" in r.output
