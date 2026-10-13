"""Legacy fix 2026-10-13: integrate_stack.js registration ladder and the star
QA run-reference tie-break.

The Nov 2025 RC16 legacy reprocess (gain 100, 3248 mm, 9..13 px stars at
17..37 deg altitude): M 27 lost every B sub and half the G and R subs inside
StarAlignment at the strict detector setting although star QA had aligned
them all, so no LRGB; NGC 6535 picked an R sub with 56 stars as the run
reference (G, R and L all 60 s; the tie went to R) and StarAlignment failed
R and then L against it, so no master at all.

The ladder (registerStack): pass 1 strict onto the shared reference (the
old behavior), pass 2 the lost frames again with the relaxed detector, pass
3 the stack's own star QA reference with its master aligned onto the
shared geometry afterwards (alignMaster). The stubbed runs need node."""
import json
import re
import shutil
import subprocess

import numpy as np
import pytest

from photonscript.integration import pjsr
from photonscript.integration import star_qa as q

NODE = shutil.which("node")
W, H = 3000, 2000


def _js() -> str:
    return pjsr.template(pjsr.INTEGRATE_TEMPLATE)


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


def _settings(src: str) -> str:
    """The SA_STRICT / SA_RELAXED var statements."""
    out = []
    for name in ("SA_STRICT", "SA_RELAXED"):
        i = src.index(f"var {name} = ")
        out.append(src[i:src.index("};", i) + 2])
    return "\n".join(out)


# ------------------------------------------------------------ static checks

def test_template_is_still_pjsr_clean():
    t = _js()
    assert pjsr.check(pjsr.render(t, {"staging": "C:/s", "out": "C:/s/out", "stacks": []})) == []
    t.encode("ascii")
    for n, ln in enumerate(t.splitlines(), 1):
        if "//" in ln:
            assert "/*" not in ln.split("//", 1)[1], n


def test_pass_one_keeps_the_long_standing_strict_setting():
    t = _js()
    assert "sensitivity: CONFIG.cfa ? 0.60 : 0.30, peakResponse: 0.50" in t
    assert "structureLayers: 5" in t
    relaxed = t[t.index("var SA_RELAXED"):t.index("};", t.index("var SA_RELAXED"))]
    assert "sensitivity: 0.85" in relaxed and "peakResponse: 0.40" in relaxed
    assert "structureLayers: 6" in relaxed and "noiseReduction: 2" in relaxed
    rs = _functions(t)["registerStack"]
    assert rs.index("SA_STRICT") < rs.index("SA_RELAXED")
    # the run stack uses the ladder, and a stack that registered nothing still says so
    run = _functions(t)["runStack"]
    assert "registerStack(S, rgbFiles, reg.file, ownRef, SOUT)" in run
    assert 'if (!regFiles.length) throw new Error("registration failed");' in run
    assert "new StarAlignment" not in run
    # drops are logged once, after every pass (the packet reads DROPPED lines)
    assert run.count('logDrops("registration"') == 1
    assert run.index("registerStack(") < run.index('logDrops("registration"')
    assert '"reg_self"' in run                      # cleared with the other intermediates


def test_pass_three_master_is_aligned_before_the_review_jpg():
    run = _functions(_js())["runStack"]
    assert "if (R.selfRef)" in run and "alignMaster(S, masterPath, reg)" in run
    assert run.index("alignMaster(") < run.index('TSTART(S, "review jpg")')
    assert "if (reg.owner === S) reg.master = masterPath;" in run


def test_a_failed_reference_stack_releases_the_reference():
    main = _functions(_js())["main"]
    assert "var reg = { file: null, owner: null, master: null };" in main
    loop = main[main.index("for (var s = 0; s < CONFIG.stacks.length; ++s)"):main.index("if (!made)")]
    c = loop[loop.index("catch (e)"):]
    assert c.index("skipStack(SN,") < c.index("if (reg.owner === SN && !made)")
    assert "reg.file = null; reg.owner = null; reg.master = null;" in c
    run = _functions(_js())["runStack"]
    # a stack without the run pick uses its own star QA reference
    assert "findByBase(rgbFiles, stack.reference)" in run and "reg.owner = S;" in run


# ------------------------------------------------------------ stubbed runs (node)

PRELUDE = r"""
var LOGS = [], CALLS = [], FS = {};
var MIN_FRAMES = 3, OUT = "O";
var CONFIG = { cfa: "" };
function log(s) { LOGS.push("" + s); }
var File = {
   extractName: function (p) { var n = p.split("/").pop(); var d = n.lastIndexOf("."); return d > 0 ? n.substr(0, d) : n; },
   exists: function (p) { return !!FS[p]; },
   remove: function (p) { delete FS[p]; },
   move: function (a, b) { if (!FS[a]) throw new Error("move: no " + a); FS[b] = FS[a]; delete FS[a]; }
};
function ensureDir(d) {}
function listFits(dir) {
   var out = [];
   for (var p in FS) if (p.indexOf(dir + "/") === 0 && p.substr(dir.length + 1).indexOf("/") < 0) out.push(p);
   out.sort(); return out;
}
function clearDir(d) { var n = 0; listFits(d).forEach(function (p) { delete FS[p]; ++n; }); return n; }
function StarAlignment() {}
StarAlignment.prototype.executeGlobal = function () {
   var self = this, n = 0;
   CALLS.push({ ref: File.extractName(this.referenceImage), n: this.targets.length,
                sens: this.sensitivity, layers: this.structureLayers, out: this.outputDirectory,
                nr: this.noiseReductionFilterRadius || 0 });
   this.targets.forEach(function (t) {
      if (CAN(self.referenceImage, t[2], self.sensitivity)) {
         FS[self.outputDirectory + "/" + File.extractName(t[2]) + "_r.xisf"] = "reg:" + File.extractName(self.referenceImage);
         ++n;
      }
   });
   return n > 0;
};
function frames(stack, n) { var a = []; for (var i = 0; i < n; ++i) a.push("S/" + stack + "/cc/" + stack + "_" + (i < 10 ? "0" : "") + i + "_c_cc.xisf"); return a; }
"""

DUMP = "\nconsole.log(JSON.stringify({ calls: CALLS, logs: LOGS, fs: FS, res: RES }));\n"


def _run(scenario: str) -> dict:
    src = _js()
    f = _functions(src)
    body = "\n".join(f[n] for n in ("baseOf", "starAlignPass", "missingFrom", "registerStack", "alignMaster"))
    prog = PRELUDE + _settings(src) + "\n" + body + "\nvar RES = null;\n" + scenario + DUMP
    r = subprocess.run([NODE, "-e", prog], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_stub_m27_blue_fails_strict_and_registers_relaxed():
    # the M 27 B case: no B sub matches the L reference at sensitivity 0.30
    d = _run("""
function CAN(ref, t, sens) { return sens > 0.5; }
var B = frames("B", 28);
RES = registerStack("B", B, "S/L/cc/L_17_c_cc.xisf", B[14], "S/B");
RES.n = RES.files.length;
""")
    assert d["res"]["n"] == 28 and d["res"]["selfRef"] is None
    assert [(c["sens"], c["n"], c["layers"]) for c in d["calls"]] == [(0.3, 28, 5), (0.85, 28, 6)]
    assert d["calls"][1]["nr"] == 2
    assert all(c["ref"] == "L_17_c_cc" for c in d["calls"])        # shared geometry kept
    assert any("pass 1 (strict, shared reference): 0 of 28 (StarAlignment returned false)" in s for s in d["logs"])
    assert any("pass 2 (relaxed detector, shared reference): 28 of 28 recovered" in s for s in d["logs"])


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_stub_partial_strict_retries_only_the_lost_frames():
    # the M 27 G case: 13 of 30 at the strict setting
    d = _run("""
function CAN(ref, t, sens) { var i = parseInt(t.split("_")[1], 10); return sens > 0.5 || i < 13; }
var G = frames("G", 30);
RES = registerStack("G", G, "S/L/cc/L_17_c_cc.xisf", G[15], "S/G");
RES.n = RES.files.length;
""")
    assert d["res"]["n"] == 30 and d["res"]["selfRef"] is None
    assert [(c["sens"], c["n"]) for c in d["calls"]] == [(0.3, 30), (0.85, 17)]


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_stub_clean_stack_is_untouched_by_the_ladder():
    d = _run("""
function CAN(ref, t, sens) { return true; }
var R = frames("R", 20);
RES = registerStack("R", R, "S/L/cc/L_17_c_cc.xisf", R[3], "S/R");
RES.n = RES.files.length;
""")
    assert d["res"]["n"] == 20 and d["res"]["selfRef"] is None
    assert len(d["calls"]) == 1 and d["calls"][0]["sens"] == 0.3


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_stub_own_reference_when_the_shared_one_never_matches():
    d = _run("""
function CAN(ref, t, sens) { return ref.indexOf("/B/") >= 0; }
var B = frames("B", 10);
RES = registerStack("B", B, "S/L/cc/L_17_c_cc.xisf", B[4], "S/B");
RES.n = RES.files.length;
""")
    assert d["res"]["n"] == 10 and d["res"]["selfRef"] == "S/B/cc/B_04_c_cc.xisf"
    assert all(f.startswith("S/B/reg_self/") for f in d["res"]["files"])
    assert [c["ref"] for c in d["calls"]] == ["L_17_c_cc", "L_17_c_cc", "B_04_c_cc"]
    assert d["calls"][2]["sens"] == 0.85 and d["calls"][2]["out"] == "S/B/reg_self"


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_stub_no_own_pass_when_own_is_the_shared_reference_or_enough_registered():
    d = _run("""
function CAN(ref, t, sens) { return false; }
var R = frames("R", 15);
RES = registerStack("R", R, R[7], R[7], "S/R");
RES.n = RES.files.length;
""")
    assert d["res"]["n"] == 0 and d["res"]["selfRef"] is None and len(d["calls"]) == 2
    d = _run("""
function CAN(ref, t, sens) { return ref.indexOf("/G/") >= 0 || parseInt(t.split("_")[1], 10) < 16; }
var G = frames("G", 30);
RES = registerStack("G", G, "S/L/cc/L_17_c_cc.xisf", G[3], "S/G");
RES.n = RES.files.length;
""")
    assert d["res"]["n"] == 16 and d["res"]["selfRef"] is None and len(d["calls"]) == 2


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_stub_align_master_replaces_it_and_keeps_the_own_geometry():
    d = _run("""
function CAN(ref, t, sens) { return true; }
FS["O/master/master_L.xisf"] = "L"; FS["O/master/master_B.xisf"] = "B-own";
alignMaster("B", "O/master/master_B.xisf", { file: "S/L/cc/L_17_c_cc.xisf", master: "O/master/master_L.xisf" });
""")
    assert d["fs"]["O/master/master_B.xisf"] == "reg:master_L"          # aligned onto the L master
    assert d["fs"]["O/master/align_B/master_B_ownref.xisf"] == "B-own"
    assert d["calls"][0]["ref"] == "master_L" and d["calls"][0]["sens"] == 0.85


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_stub_align_master_failure_never_leaves_a_misaligned_master():
    d = _run("""
function CAN(ref, t, sens) { return false; }
FS["O/master/master_B.xisf"] = "B-own";
try { alignMaster("B", "O/master/master_B.xisf", { file: "S/L/cc/L_17_c_cc.xisf", master: null }); }
catch (e) { LOGS.push("THREW " + e.message); }
""")
    assert "O/master/master_B.xisf" not in d["fs"]                    # the color combine skips B
    assert not [p for p in d["fs"] if p.startswith("O/master/master_")]   # nothing blend.masters() would glob
    assert d["fs"]["O/master/align_B/master_B_ownref.xisf"] == "B-own"
    assert d["calls"][0]["ref"] == "L_17_c_cc"                        # no L master yet: the sub
    assert any(s.startswith("THREW own-reference master could not be aligned") for s in d["logs"])


# ------------------------------------------------------------ star QA run reference

def _field(n, seed):
    rng = np.random.default_rng(seed)
    return np.column_stack([rng.uniform(40, W - 40, n), rng.uniform(40, H - 40, n)])


def _row(i, xy, filt, exp):
    return {"file": f"{filt}_{exp:g}_{i:03d}.fits", "filter": filt, "exp": exp, "time": f"T{i:03d}",
            "stars": len(xy), "hfd_px": 11.0, "ecc": 0.45, "sky_adu": 600.0,
            "double_frac": 0.0, "double_vec": "", "theta_R": 0.1, "xbin": 1, "readout": "LCG",
            "_xy": np.asarray(xy, np.float32), "_shape": (W, H)}


def _group(filt, n, stars, seed, start):
    base = _field(stars, seed)
    return [_row(start + i, base + np.random.default_rng(seed * 100 + i).normal(0, 0.2, base.shape), filt, 60.0)
            for i in range(n)]


def test_run_reference_tie_goes_to_the_group_with_the_most_stars():
    # NGC 6535: R and G 20 subs, L 18 subs, all 60 s; L has three times the stars
    rows = _group("R", 20, 56, 1, 0) + _group("G", 20, 28, 2, 100) + _group("L", 18, 172, 3, 200)
    ref = q.assess(rows, q.Thresholds())
    assert rows[ref]["filter"] == "L" and rows[ref]["is_run_reference"]


def test_run_reference_tie_never_goes_to_a_group_too_small_to_stack():
    rows = _group("R", 6, 80, 1, 0) + _group("L", 2, 400, 3, 200)
    ref = q.assess(rows, q.Thresholds())
    assert rows[ref]["filter"] == "R"


def test_run_reference_longest_exposure_still_wins():
    rows = _group("L", 10, 400, 3, 0)
    for r in rows:
        r["exp"] = 30.0
    rows += _group("B", 4, 60, 4, 100)                               # 60 s, fewer stars
    ref = q.assess(rows, q.Thresholds())
    assert rows[ref]["filter"] == "B" and rows[ref]["exp"] == 60.0
