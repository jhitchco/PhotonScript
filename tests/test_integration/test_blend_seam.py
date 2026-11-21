"""Two-rig blend seam + blown core fix (2026-10-16 M31): the RC16 mask is the
valid registered footprint, eroded, heavily feathered with round corners; the
RC16 is matched to the OSC background with a smooth residual map before the
blend; the stretched looks get a guarded HDRMultiscaleTransform core."""
import json
import re
import shutil
import subprocess

import pytest
from typer.testing import CliRunner

from photonscript import cli
from photonscript.integration import blend as bl
from photonscript.integration import pjsr
from photonscript.shared import ledger as L

NODE = shutil.which("node")


def _touch(p, n=16):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * n)
    return p


@pytest.fixture
def staging(tmp_path):
    root = tmp_path / "Staging"
    for name, rig, filt in (("M31_piggyback_1", "piggyback", "OSC"), ("M31_rc16_1", "rc16", "L")):
        d = root / name
        _touch(d / "out" / "master" / f"master_{filt}.xisf")
        _touch(d / "out" / "final" / f"M31_{filt}_linear.xisf")
        L.save(d / "ledger.json", L.Ledger(campaign="Andromeda Galaxy", rig=rig, run=name, version=1,
                                           kind="integration", created_at="2026-10-05T10:00:00Z"))
    return root


def _cfg(staging, **kw):
    o = bl.Options(target="M31", staging_root=staging, **kw)
    inp = bl.discover(o)
    return bl.blend_config(o, inp, staging / "Blend" / "x")


def _functions(src):
    out = {}
    for m in re.finditer(r"^function (\w+)\s*\([^)]*\)\s*\{", src, re.M):
        depth, i = 1, m.end()
        while depth and i < len(src):
            depth += {"{": 1, "}": -1}.get(src[i], 0)
            i += 1
        out[m.group(1)] = src[m.start():i]
    return out


def _node(tmp_path, cfg: dict, names, body: str):
    fn = _functions(pjsr.template(bl.TEMPLATE))
    js = ("var CONFIG = " + json.dumps(cfg) + ";\nvar LOGS = [], STEPS = [];\n"
          "function log(s) { LOGS.push(s); }\n"
          "function step(n, t, s, x) { STEPS.push({ step: n, status: s, settings: x }); }\n"
          + "\n".join(fn[n] for n in names) + "\n" + body)
    f = tmp_path / "t.js"
    f.write_text(js, encoding="ascii")
    r = subprocess.run([NODE, str(f)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


# ----------------------------------------------------------------- config

def test_defaults_are_the_seam_fix(staging):
    c = _cfg(staging)
    assert c["inset_frac"] == bl.INSET_FRAC == 0.04
    assert c["feather_frac"] == bl.FEATHER_FRAC == 0.18
    assert c["mask_round"] == bl.MASK_ROUND and c["bg_match"] is True
    assert c["bg_cell_frac"] == bl.BG_CELL_FRAC
    assert c["core_hdr"] == {"lo": 0.55, "span": 0.35, "layers": 0, "mode": "auto"}


def test_switches_turn_the_steps_off(staging):
    c = _cfg(staging, bg_match=False, core_hdr=False)
    assert c["bg_match"] is False and c["core_hdr"]["mode"] == "off"


def test_cli_flags_reach_the_options(staging, monkeypatch):
    seen = {}

    def fake_run(o, echo=print):
        seen.update(bg=o.bg_match, hdr=o.core_hdr, feather=o.feather_frac, inset=o.inset_frac)
        return {"ok": True, "run_dir": str(staging / "Blend" / "x"), "outputs": []}

    monkeypatch.setattr(bl, "run", fake_run)
    r = CliRunner().invoke(cli.app, ["blend", "--target", "M31", "--staging-root", str(staging),
                                     "--no-bg-match", "--no-core-hdr", "--no-pixinsight"])
    assert seen == {"bg": False, "hdr": False, "feather": 0.18, "inset": 0.04}, r.output


# ----------------------------------------------------------------- template

def test_template_has_the_guarded_steps():
    t = (pjsr.DEPLOY / bl.TEMPLATE).read_bytes().decode("ascii")
    assert pjsr.check(t) == ["CONFIG not filled"]
    for s in ("function bgMatch(", "function boxBlur3(", "function coreHdr(", "CONFIG.bg_match === false",
              'typeof HDRMultiscaleTransform === "undefined"', '"bg_match"', "core_hdr_",
              "background match skipped", "core HDR skipped"):
        assert s in t, s
    # the HDR runs only on stretched (LIN) looks, right after the stretch
    assert "applyHT(w.mainView, p || stretchParams(w.mainView)); coreHdr(w.mainView, label);" in t
    # bgMatch and boxBlur3 sit inside the shared blend library (the NB blend copies it)
    lib = bl.library(t, bl.BLEND_LIB_BEGIN, bl.BLEND_LIB_END, bl.TEMPLATE)
    assert "function bgMatch(" in lib and "function boxBlur3(" in lib
    assert "—" not in t


def test_rendered_nb_blend_still_checks(tmp_path):
    root = tmp_path / "Staging"
    for name, rig, filters in (("H_piggyback_1", "piggyback", ["OSC"]), ("H_rc16_1", "rc16", ["Ha", "OIII"])):
        d = root / name
        for f in filters:
            _touch(d / "out" / "master" / f"master_{f}.xisf")
        L.save(d / "ledger.json", L.Ledger(campaign="Heart Nebula", rig=rig, run=name, version=1,
                                           kind="integration", created_at="2026-10-05T10:00:00Z"))
    o = bl.Options(target="Heart Nebula", staging_root=root, mode="nb")
    inp = bl.discover(o)
    out = bl.render(bl.config_for(o, inp, root / "Blend" / "x"))
    assert pjsr.check(out) == []
    assert out.count("function bgMatch(") == 1 and out.count("function footprintMask(") == 1


# ----------------------------------------------------------------- node

MASK_JS = r"""
var W = 400, H = 300, reg = new Float32Array(W * H);
// valid RC16 field 300 x 200 at (50, 50); a 3 px black (near zero) border inside it
for (var y = 50; y < 250; ++y) for (var x = 50; x < 350; ++x) {
   var edge = (x < 53 || y < 53 || x > 346 || y > 246);
   reg[y * W + x] = edge ? 1e-6 : 0.01 + 0.001 * Math.sin(x / 7);
}
var fp = footprintMask(reg, W, H), m = fp.m;
function at(x, y) { return m[y * W + x]; }
var maxStep = 0;
for (var yy = 0; yy < H; ++yy) for (var xx = 1; xx < W; ++xx)
   maxStep = Math.max(maxStep, Math.abs(m[yy * W + xx] - m[yy * W + xx - 1]));
var d = fp.inset + 0.5 * fp.feather;
console.log(JSON.stringify({
   border: at(51, 150), black: at(54, 150), inset_edge: at(52 + Math.floor(fp.inset), 150),
   centre: at(200, 150), max: Math.max.apply(null, Array.prototype.slice.call(m)),
   max_step: maxStep, feather: fp.feather, inset: fp.inset,
   corner: at(53 + Math.round(d), 53 + Math.round(d)), side: at(53 + Math.round(d), 150),
   steps: STEPS }));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_footprint_mask_is_valid_eroded_feathered_and_round(tmp_path):
    cfg = {"inset_frac": bl.INSET_FRAC, "feather_frac": bl.FEATHER_FRAC, "mask_round": bl.MASK_ROUND}
    r = _node(tmp_path, cfg, ["boxBlur3", "footprintMask"], MASK_JS)
    assert r["border"] == 0 and r["black"] == 0 and r["inset_edge"] == 0   # borders + erosion
    assert r["centre"] > 0.99 and r["max"] <= 1.0
    assert r["feather"] > 30                                               # 0.18 * ~194 px
    assert r["max_step"] < 2.5 / r["feather"]                              # smooth, no hard edge
    assert r["corner"] < r["side"]                                         # rounded corners
    assert r["steps"][0]["settings"]["round_sigma_px"] > 1


SEAM_JS = r"""
var W = 400, H = 300, N = W * H, reg = new Float32Array(N), target = new Float32Array(N);
var seed = 1; function rnd() { seed = (seed * 16807) % 2147483647; return seed / 2147483647 - 0.5; }
for (var y = 0; y < H; ++y) for (var x = 0; x < W; ++x) {
   var i = y * W + x, gal = 0.002 + 0.05 * Math.exp(-((x - 200) * (x - 200) / 6000 + (y - 150) * (y - 150) / 2500));
   target[i] = gal + 0.00002 * rnd();
   // the RC16 after GradientCorrection: the galaxy plus a smooth tilted surface
   if (x >= 50 && x < 350 && y >= 50 && y < 250)
      reg[i] = (gal + 0.004 * ((x - 200) / 150) * ((y - 150) / 100)) * 10 + 0.0002 * rnd();
}
var fp = footprintMask(reg, W, H), m = fp.m;
var fit = { a: 0, b: 0.1 };
var fitted = new Float32Array(N);
for (var q = 0; q < N; ++q) if (m[q] > 0) fitted[q] = fit.a + fit.b * reg[q];
var bm = bgMatch(fitted, target, m, W, H, fp.box, 0.06 * 200);
function err(useOff) {
   var s = 0, n = 0, mx = 0;
   for (var i = 0; i < N; ++i) {
      if (m[i] <= 0) continue;
      var e = Math.abs(fitted[i] + (useOff ? bm.off[i] : 0) - target[i]);
      s += e; ++n; if (e > mx) mx = e;
   }
   return { mean: s / n, max: mx };
}
// what the blend adds near the mask edge (weight 0.7): the visible seam
function blendRowStep(useOff) {
   var s = 0, n = 0;
   for (var i = 0; i < N; ++i) {
      if (m[i] <= 0 || m[i] >= 0.5) continue;
      s += Math.abs(0.7 * m[i] * (fitted[i] + (useOff ? bm.off[i] : 0) - target[i])); ++n;
   }
   return s / n;
}
console.log(JSON.stringify({ before: err(false), after: err(true), step_before: blendRowStep(false),
                             step_after: blendRowStep(true), cells: bm.cells, sd0: bm.before, sd1: bm.after }));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_background_match_removes_the_gradient_mismatch(tmp_path):
    cfg = {"inset_frac": bl.INSET_FRAC, "feather_frac": bl.FEATHER_FRAC, "mask_round": bl.MASK_ROUND}
    r = _node(tmp_path, cfg, ["boxBlur3", "footprintMask", "bgMatch"], SEAM_JS)
    assert r["cells"] > 50
    assert r["after"]["mean"] < 0.15 * r["before"]["mean"], r
    assert r["sd1"] < r["sd0"]
    assert r["step_after"] < 0.2 * r["step_before"], r


BLUR_JS = r"""
var w = 50, h = 40, a = new Float32Array(w * h);
for (var i = 0; i < w * h; ++i) a[i] = 0.25;
boxBlur3(a, w, h, 4);
var lo = 1, hi = 0; for (i = 0; i < w * h; ++i) { lo = Math.min(lo, a[i]); hi = Math.max(hi, a[i]); }
var b = new Float32Array(w * h); b[20 * w + 25] = 1; boxBlur3(b, w, h, 3);
var s = 0; for (i = 0; i < w * h; ++i) s += b[i];
console.log(JSON.stringify({ lo: lo, hi: hi, sum: s, peak: b[20 * w + 25] }));
"""


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_box_blur_keeps_flat_fields_and_spreads_a_spike(tmp_path):
    r = _node(tmp_path, {}, ["boxBlur3"], BLUR_JS)
    assert abs(r["lo"] - 0.25) < 1e-6 and abs(r["hi"] - 0.25) < 1e-6
    assert abs(r["sum"] - 1) < 0.05 and r["peak"] < 0.1
