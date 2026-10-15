"""OSC core HDR combine (2026-10-14, "Piggy-600: shoot M31 at 120 s").

The Piggy-600 M31 stack holds 300 s subs that burned the nucleus and 120 s
subs that did not; OSC integrate made one master and the core stayed
clipped. integrate now also stacks the short subs (<= 180 s) on their own
and, only when core_master's plateau check finds the full master clipped,
replaces the nucleus from them (deploy/hdr_core_osc.js -> master_OSC_hdr).
Every failure keeps today's single master."""
import json
import re
import shutil
import struct
import subprocess
from pathlib import Path

import numpy as np
import pytest

from photonscript.integration import osc_hdr, pjsr
from photonscript.integration import pipeline as pl
from tests.test_integration.test_pipeline import _opts, lib  # noqa: F401

NODE = shutil.which("node")
SIZE = 400


def _st(exps, name="OSC"):
    return {"name": name, "filter": "OSC", "mixed_exposures": len(exps) > 1,
            "groups": [{"name": f"{e:g}s", "exp": float(e), "tag": f"_{e:.2f}s_"} for e in exps]}


# ----------------------------------------------------------------- plan

def test_plan_subset_needs_both_sides_of_the_limit_and_enough_subs():
    sub, note = osc_hdr.plan_subset(_st([120, 300]), {120.0: 98, 300.0: 162})
    assert sub["name"] == "OSC_short" and sub["groups"] == ["120s"] and sub["frames"] == 98
    assert "98 subs of 120 s also make master_OSC_short" in note
    # 180 s is still short (the limit is inclusive); 181 is not
    assert osc_hdr.plan_subset(_st([180, 300]), {180.0: 5, 300.0: 5})[0]["groups"] == ["180s"]
    assert osc_hdr.plan_subset(_st([181, 300]), {181.0: 5, 300.0: 5}) == (None, "")
    assert osc_hdr.plan_subset(_st([300, 400]), {300.0: 5, 400.0: 5}) == (None, "")
    assert osc_hdr.plan_subset(_st([120]), {120.0: 50}) == (None, "")
    sub, note = osc_hdr.plan_subset(_st([120, 300]), {120.0: 2, 300.0: 50})
    assert sub is None and "only 2 short sub(s)" in note
    # two short lengths go in together
    sub, _ = osc_hdr.plan_subset(_st([60, 120, 300]), {60.0: 4, 120.0: 4, 300.0: 9})
    assert sub["groups"] == ["60s", "120s"] and sub["frames"] == 8


def test_item_names_inputs_and_disk():
    st = _st([120, 300])
    osc_hdr.attach([st], {"OSC": {120.0: 98, 300.0: 162}}, Path("D:/S/run/out"), max_s=180,
                   min_frames=3)
    assert st["short_subset"]["output"] == "D:/S/run/out/master/osc_hdr/master_OSC_short.xisf"
    it = osc_hdr.item_for(st, {120.0: 98, 300.0: 162}, Path("D:/S/run/out"),
                          {"x": 3000.0, "y": 2000.0, "clipped_px": 80}, 46.4)
    assert it["name"] == "OSC_hdr" and it["output"] == "D:/S/run/out/master/master_OSC_hdr.xisf"
    assert [x["stack"] for x in it["inputs"]] == ["OSC", "OSC_short"]
    assert it["ratio"] == 2.5 and it["core"]["region_px"] == pytest.approx(139.2)


# ----------------------------------------------------------------- PJSR

def test_templates_are_ascii_and_rule_clean():
    for name in (osc_hdr.TEMPLATE, pjsr.INTEGRATE_TEMPLATE):
        t = (pjsr.DEPLOY / name).read_bytes().decode("ascii")
        assert pjsr.check(t) == ["CONFIG not filled"], name
        for n, ln in enumerate(t.splitlines(), 1):
            c = pjsr._line_comment(ln)
            assert c is None or "/*" not in c, (name, n)
    t = pjsr.template(osc_hdr.TEMPLATE)
    assert t.index("#include <pjsr/DataType.jsh>") < t.index(pjsr.CONFIG_MARK)
    assert "new Crop" not in t
    # guarded: a failure is SKIPPED (no output file), the log still ends EXIT OK
    assert 'log("SKIPPED [" + item.name + "]: "' in t and "removeQuiet(item.output)" in t
    assert "not one registration" in t and 'log("EXIT OK")' in t and "'osc-core'" in t
    assert "f.a > ratio / 3 && f.a < ratio * 3" in t


def test_integrate_template_short_subset_is_optional_and_guarded():
    t = pjsr.template(pjsr.INTEGRATE_TEMPLATE)
    body = t[t.index("function integrateShort("):t.index("function runStack(")]
    assert "if (!sub) return null;" in body
    assert "catch (e)" in body and "stands as it is" in body
    assert "AdditiveWithScaling" in body and "LocalNormalization" not in body.split("var makeII")[1]
    assert "if (selfRef) throw" in body
    assert "if (stack.short_subset) integrateShort(stack, regFiles, R.selfRef);" in t


def _rendered_osc_js(tmp_path):
    st = _st([120, 300])
    osc_hdr.attach([st], {"OSC": {120.0: 9, 300.0: 9}}, tmp_path / "out", max_s=180, min_frames=3)
    it = osc_hdr.item_for(st, {120.0: 9, 300.0: 9}, tmp_path / "out", {"x": 200.0, "y": 200.0}, 46.4)
    return osc_hdr.render(tmp_path / "out", it)


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_rendered_scripts_parse(tmp_path, lib):  # noqa: F811
    js = _rendered_osc_js(tmp_path)
    assert pjsr.check(js) == []
    out = tmp_path / "Staging" / "parse"
    pl.run(_opts(lib, tmp_path, out=out), echo=lambda s: None)
    integ = (out / "integrate_run.js").read_text("ascii")
    assert pjsr.check(integ) == []
    cfg = json.loads(integ.split("var CONFIG = ", 1)[1].split(";\n", 1)[0])
    assert cfg["stacks"][0]["short_subset"]["groups"] == ["120s"]
    for name, text in (("osc.js", js), ("integ.js", integ)):
        p = tmp_path / name
        p.write_text("\n".join(x for x in text.splitlines() if not x.startswith("#")), encoding="ascii")
        r = subprocess.run([NODE, "--check", str(p)], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, (name, r.stderr)


# ----------------------------------------------------------------- math (node)

def _math():
    t = pjsr.template(osc_hdr.TEMPLATE)
    return t[t.index("// PS_OSC_HDR_MATH begin"):t.index("// PS_OSC_HDR_MATH end")]


def _node(body):
    r = subprocess.run([NODE, "-e", _math() + "\n" + body], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


SYNTH = """
var BW = 121, BH = 121, X0 = 1000, Y0 = 2000, CX = 1060, CY = 2060, N = BW * BH;
var COL = [1.0, 0.8, 0.55], PLATEAU = 0.62, RATIO = 2.5;
var L = [], S = [], T = [], seed = 3;
function rnd() { seed = (seed * 16807) % 2147483647; return seed / 2147483647; }
for (var c = 0; c < 3; ++c) { L.push(new Float32Array(N)); S.push(new Float32Array(N)); T.push(new Float32Array(N)); }
for (var y = 0; y < BH; ++y) for (var x = 0; x < BW; ++x) {
   var k = y * BW + x, dx = X0 + x - CX, dy = Y0 + y - CY, r2 = dx * dx + dy * dy;
   var g = 0.02 + 0.5 * Math.exp(-r2 / (2 * 9 * 9)) + 0.12 * Math.exp(-r2 / (2 * 40 * 40));
   for (var c2 = 0; c2 < 3; ++c2) {
      var truth = RATIO * g * COL[c2] + 0.01;
      T[c2][k] = truth;
      L[c2][k] = Math.min(truth, PLATEAU * COL[c2] + 0.01) + 1e-5 * (rnd() - 0.5);
      S[c2][k] = g * COL[c2] + 0.004 + 1e-5 * (rnd() - 0.5);
   }
}
function core(R, F, t0, t1) {
   var sat = [arrMax(L[0]), arrMax(L[1]), arrMax(L[2])], U = new Float32Array(N);
   for (var i = 0; i < N; ++i) { var u = 0; for (var c = 0; c < 3; ++c) u = Math.max(u, L[c][i] / sat[c]); U[i] = u; }
   return { sat: sat, M: maskFor(U, BW, BH, X0, Y0, CX, CY, R, F, t0, t1) };
}
"""


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_math_restores_the_clipped_nucleus_with_its_color():
    d = _node(SYNTH + """
var r = core(55, 15, 0.65, 0.80), out = [], fits = [];
for (var c = 0; c < 3; ++c) {
   var f = fitChannel(L[c], S[c], r.M.mask, r.sat[c], 0.65, RATIO);
   fits.push(f);
   out.push(blendChannel(L[c], S[c], r.M.mask, f.a, f.b));
}
var kc = 60 * BW + 60, err = 0, outside = 0, before = 0;
for (var i = 0; i < N; ++i) {
   var y = Math.floor(i / BW), x = i % BW, dx = X0 + x - CX, dy = Y0 + y - CY;
   if (Math.sqrt(dx * dx + dy * dy) >= 55) { for (var c3 = 0; c3 < 3; ++c3) if (out[c3][i] !== L[c3][i]) ++outside; }
   else if (r.M.mask[i] >= 1) for (var c4 = 0; c4 < 3; ++c4) err = Math.max(err, Math.abs(out[c4][i] - T[c4][i]) / T[c4][i]);
}
console.log(JSON.stringify({ fits: fits, n: r.M.n, err: err, outside: outside,
   core: [out[0][kc], out[1][kc], out[2][kc]], truth: [T[0][kc], T[1][kc], T[2][kc]],
   clipped: [L[0][kc], L[1][kc], L[2][kc]] }));
""")
    for f in d["fits"]:
        assert f["how"] == "least squares" and abs(f["a"] - 2.5) < 0.01 and f["n"] > 1000
    assert d["n"] > 100 and d["outside"] == 0 and d["err"] < 0.01
    # the nucleus is back above the plateau, and the color ratios are the truth's
    assert d["core"][0] > 1.3 * d["clipped"][0]
    assert abs(d["core"][1] / d["core"][0] - d["truth"][1] / d["truth"][0]) < 0.005
    assert abs(d["core"][2] / d["core"][0] - d["truth"][2] / d["truth"][0]) < 0.005


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_math_guards():
    d = _node(SYNTH + """
var r = core(55, 15, 0.65, 0.80);
// a short master with the wrong scale (slope far off the exposure ratio)
var bad = new Float32Array(N); for (var i = 0; i < N; ++i) bad[i] = 0.001 * S[0][i];
var f1 = fitChannel(L[0], bad, r.M.mask, r.sat[0], 0.65, RATIO);
// nothing above the threshold: no pixel to replace
var flat = new Float32Array(N); for (var j = 0; j < N; ++j) flat[j] = 0.3;
var m2 = maskFor(flat, BW, BH, X0, Y0, CX, CY, 55, 15, 1.01, 1.2);
// a disk off the box replaces nothing; the feather is a smooth edge
var m3 = maskFor(new Float32Array(N).fill(1), BW, BH, X0, Y0, CX + 500, CY, 55, 15, 0.65, 0.8);
console.log(JSON.stringify({ f1: f1, n2: m2.n, n3: m3.n,
   w: [regionWeight(0, 50, 10), regionWeight(45, 50, 10), regionWeight(50, 50, 10), regionWeight(60, 50, 10)] }));
""")
    assert d["f1"]["a"] == 2.5 and d["f1"]["how"].startswith("exposure ratio (fitted slope")
    assert d["n2"] == 0 and d["n3"] == 0
    assert d["w"][0] == 1 and 0 < d["w"][1] < 1 and d["w"][2] == 0 and d["w"][3] == 0


# ----------------------------------------------------------------- pipeline (fake PixInsight)

def _write_xisf(path: Path, img: np.ndarray):
    """A minimal monolithic XISF: Float32, planar, uncompressed, 3 channels."""
    c, h, w = img.shape
    data = img.astype("<f4").tobytes()
    pos = 4096
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><xisf version="1.0" '
           f'xmlns="http://www.pixinsight.com/xisf"><Image geometry="{w}:{h}:{c}" '
           f'sampleFormat="Float32" bounds="0:1" colorSpace="RGB" '
           f'location="attachment:{pos}:{len(data)}"/></xisf>').encode("utf-8")
    head = b"XISF0100" + struct.pack("<I", len(xml)) + b"\0\0\0\0" + xml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(head + b"\0" * (pos - len(head)) + data)
    return path


def _galaxy(clipped: bool):
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    r2 = (xx - 200) ** 2 + (yy - 200) ** 2
    g = 0.02 + 0.9 * np.exp(-r2 / (2 * 30.0 ** 2)) + 0.1 * np.exp(-r2 / (2 * 150.0 ** 2))
    if clipped:
        g = np.minimum(g, 0.62)
    rng = np.random.default_rng(2)
    img = np.stack([g * k for k in (1.0, 0.85, 0.6)]) + rng.normal(0, 1e-4, (3, SIZE, SIZE))
    return img.astype(np.float32)


def _cfg_of(script: Path) -> dict:
    return json.loads(script.read_text().split("var CONFIG = ", 1)[1].split(";\n", 1)[0])


def _fake_runner(calls, *, long_clipped=True, short=True, combine="ok"):
    def fake_run(script, log_path, **kw):
        script, log_path = Path(script), Path(log_path)
        calls.append(script.name)
        out = log_path.parent
        out.mkdir(parents=True, exist_ok=True)
        if script.name == "integrate_run.js":
            for s in _cfg_of(script)["stacks"]:
                _write_xisf(out / "master" / f"master_{s['name']}.xisf", _galaxy(long_clipped))
                if s.get("short_subset") and short:
                    _write_xisf(Path(s["short_subset"]["output"]), _galaxy(False) * 0.4)
        elif script.name == "osc_hdr_run.js":
            c = _cfg_of(script)
            it = c["item"]
            rec = {"name": it["name"], "status": combine, "replaced_px": 900,
                   "fits": [{"channel": ch, "a": 2.5, "how": "least squares"} for ch in "RGB"]}
            if combine == "ok":
                _write_xisf(Path(it["output"]), _galaxy(False))
            else:
                rec["error"] = "fit failed"
            Path(c["steps"]).parent.mkdir(parents=True, exist_ok=True)
            Path(c["steps"]).write_text(json.dumps({"result": combine, "item": rec}))
        log_path.write_text("t  EXIT OK\n")
        return {"ok": True, "exit_code": 0, "minutes": 0.1, "last_line": "EXIT OK", "timed_out": False}
    return fake_run


def _finish_masters(out):
    fin = (out / "finish_run.js").read_text()
    return json.loads(fin.split("var MASTERS = ", 1)[1].split(";\n", 1)[0])


def test_pipeline_combines_the_clipped_core(lib, tmp_path, monkeypatch):  # noqa: F811
    out = tmp_path / "Staging" / "hdr"
    calls, msgs = [], []
    monkeypatch.setattr(pl.runner, "run_script", _fake_runner(calls))
    res = pl.run(_opts(lib, tmp_path, out=out, run_pixinsight=True), echo=msgs.append)
    assert calls == ["integrate_run.js", "osc_hdr_run.js", "finish_run.js"]
    assert pjsr.check((out / "osc_hdr_run.js").read_text("ascii")) == []
    h = res["hdr"]
    assert h["made"] == ["OSC_hdr"] and h["status"] == "ok" and h["kind"] == "osc_core"
    assert h["long_check"]["still_clipped"]
    assert _finish_masters(out) == [{"name": "M31_OSC",
                                     "path": pjsr.fwd(out / "out" / "master" / "master_OSC_hdr.xisf")}]
    log = (out / "out" / "pipeline.log").read_text("ascii")
    assert "OSC core combine: master_OSC_hdr = master_OSC with its nucleus (900 px" in log
    # the core check reads the HDR master now
    assert [c["master"] for c in res["core_check"]] == ["master_OSC_hdr"]
    assert not res["core_check"][0]["still_clipped"]
    led = json.loads(Path(res["ledger"]).read_text())["machine"]
    assert led["hdr"]["kind"] == "osc_core" and led["hdr"]["made"] == ["OSC_hdr"]
    assert led["hdr"]["script"].startswith("osc_hdr_run.js") and led["hdr"]["log"].endswith("osc_hdr.log")
    assert any("OSC core combine planned: 3 subs of 120 s" in m for m in msgs)
    # the short master sits outside out/master itself: blend never sees it
    from photonscript.integration import blend as bl
    assert sorted(bl.masters(out)) == ["OSC"] and bl.masters(out)["OSC"].name == "master_OSC_hdr.xisf"


def test_pipeline_core_not_clipped_keeps_the_single_master(lib, tmp_path, monkeypatch):  # noqa: F811
    out = tmp_path / "Staging" / "clean"
    calls = []
    monkeypatch.setattr(pl.runner, "run_script", _fake_runner(calls, long_clipped=False))
    res = pl.run(_opts(lib, tmp_path, out=out, run_pixinsight=True), echo=lambda s: None)
    assert calls == ["integrate_run.js", "finish_run.js"]
    assert res["hdr"]["made"] == [] and res["hdr"]["status"] == "skipped"
    assert [m["name"] for m in _finish_masters(out)] == ["M31_OSC"]
    assert _finish_masters(out)[0]["path"].endswith("master_OSC.xisf")
    assert "OSC core combine: core not clipped in master_OSC" in (out / "out" / "pipeline.log").read_text("ascii")


@pytest.mark.parametrize("case", ["no_short", "combine_failed"])
def test_pipeline_failures_keep_the_single_master(lib, tmp_path, monkeypatch, case):  # noqa: F811
    out = tmp_path / "Staging" / case
    calls = []
    kw = {"short": False} if case == "no_short" else {"combine": "failed"}
    monkeypatch.setattr(pl.runner, "run_script", _fake_runner(calls, **kw))
    res = pl.run(_opts(lib, tmp_path, out=out, run_pixinsight=True), echo=lambda s: None)
    assert res["hdr"]["made"] == []
    assert _finish_masters(out)[0]["path"].endswith("master_OSC.xisf")
    assert not (out / "out" / "master" / "master_OSC_hdr.xisf").exists()
    log = (out / "out" / "pipeline.log").read_text("ascii")
    if case == "no_short":
        assert calls == ["integrate_run.js", "finish_run.js"]
        assert "no master_OSC_short" in log
    else:
        assert calls == ["integrate_run.js", "osc_hdr_run.js", "finish_run.js"]
        assert "not made (fit failed): master_OSC kept" in log
    led = json.loads(Path(res["ledger"]).read_text())["machine"]
    assert led["hdr"]["made"] == [] and led["hdr"]["kind"] == "osc_core"


def test_pipeline_off_is_todays_run(lib, tmp_path, monkeypatch):  # noqa: F811
    out = tmp_path / "Staging" / "off"
    calls = []
    monkeypatch.setattr(pl.runner, "run_script", _fake_runner(calls))
    res = pl.run(_opts(lib, tmp_path, out=out, run_pixinsight=True, osc_hdr_core=False),
                 echo=lambda s: None)
    assert calls == ["integrate_run.js", "finish_run.js"]
    assert "short_subset" not in _cfg_of(out / "integrate_run.js")["stacks"][0]
    assert "hdr" not in res
    assert _finish_masters(out)[0]["path"].endswith("master_OSC.xisf")


def test_run_never_raises(tmp_path):
    st = _st([120, 300])
    osc_hdr.attach([st], {"OSC": {120.0: 5, 300.0: 5}}, tmp_path / "out", max_s=180, min_frames=3)
    _write_xisf(tmp_path / "out" / "master" / "master_OSC.xisf", _galaxy(True))
    _write_xisf(Path(st["short_subset"]["output"]), _galaxy(False))
    lines = []
    r = osc_hdr.run(tmp_path, st, {120.0: 5, 300.0: 5}, r_core_px=46.4, warn_px=25, exe="x",
                    echo=lines.append, check=lambda *a: 1 / 0)
    assert r["status"] == "failed" and r["made"] == [] and "master_OSC kept" in lines[-1]


def test_config_keys_and_cli_flag():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    from photonscript.shared.config import PhotonScriptConfig
    c = PhotonScriptConfig(_env_file=None)
    assert c.integrate_osc_hdr_core is True and c.integrate_osc_hdr_short_max_s == 180.0
    f = {x[0]: x for x in _CONFIG_FIELDS}
    assert f["integrate_osc_hdr_core"][1] == "PS_INTEGRATE_OSC_HDR_CORE"
    assert f["integrate_osc_hdr_short_max_s"][1] == "PS_INTEGRATE_OSC_HDR_SHORT_MAX_S"
    o = pl.config_options(PhotonScriptConfig(_env_file=None, integrate_osc_hdr_core=False,
                                             integrate_osc_hdr_short_max_s=150), "piggyback")
    assert o["osc_hdr_core"] is False and o["osc_hdr_short_max_s"] == 150.0
    src = (Path(pl.__file__).parents[1] / "cli.py").read_text(encoding="utf-8")
    assert re.search(r'kw\["hdr_combine"\] = hdr\n\s+kw\["osc_hdr_core"\] = hdr', src)


def test_read_xisf_pixinsight_header_with_xsi_schema(tmp_path):
    # real PixInsight headers carry xmlns:xsi + xsi:schemaLocation; stripping
    # the xmlns left an unbound prefix (2026-10-14 M31 master)
    import struct
    import numpy as np
    from photonscript.integration import core_master as cm
    data = (np.arange(12, dtype=np.float32) / 12).reshape(1, 3, 4)
    head = ('<?xml version="1.0" encoding="UTF-8"?><xisf version="1.0" '
            'xmlns="http://www.pixinsight.com/xisf" '
            'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xsi:schemaLocation="http://www.pixinsight.com/xisf '
            'http://pixinsight.com/xisf/xisf-1.0.xsd">'
            '<Image geometry="4:3:1" sampleFormat="Float32" bounds="0:1" '
            'location="attachment:{off}:48"/></xisf>')
    off = 16 + len(head.format(off=0)) + 8
    xml = head.format(off=off).encode()
    off = 16 + len(xml)
    xml = head.format(off=off).encode()
    p = tmp_path / "m.xisf"
    p.write_bytes(b"XISF0100" + struct.pack("<I", len(xml)) + b"\0" * 4 + xml
                  + data.tobytes())
    out = cm.read_xisf(p)
    assert out is not None and out.shape == (1, 3, 4)
    assert np.allclose(out, data)


# ------------------------------------------------- 2026-10-15 core wording

def _heart_run(tmp_path, clipped_px, target=None):
    st = _st([120, 300])
    osc_hdr.attach([st], {"OSC": {120.0: 5, 300.0: 5}}, tmp_path / "out", max_s=180, min_frames=3)
    _write_xisf(tmp_path / "out" / "master" / "master_OSC.xisf", _galaxy(False))
    _write_xisf(Path(st["short_subset"]["output"]), _galaxy(False))
    lines = []

    def check(path, r_px, warn_px):
        from photonscript.integration.core_master import verdict
        return {"master": "master_OSC", "clipped_px": clipped_px,
                **verdict("master_OSC", clipped_px, warn_px)}
    r = osc_hdr.run(tmp_path, st, {120.0: 5, 300.0: 5}, r_core_px=46.4, warn_px=25, exe="x",
                    echo=lines.append, check=check, target=target)
    return r, lines


def test_combine_line_and_core_check_line_agree(tmp_path):
    """Heart 2026-10-15 printed "core not clipped in master_OSC (13 px)" and
    "core clipped in master_OSC: 13 px": one rule (over warn_px) and one
    wording now."""
    from photonscript.integration.core_master import verdict
    r, lines = _heart_run(tmp_path, 13)
    v = verdict("master_OSC", 13, 25)
    assert not v["still_clipped"] and v["line"] == "core not clipped in master_OSC: 13 px (limit 25)"
    assert r["status"] == "skipped"
    assert lines[-1] == "  OSC core combine: " + v["line"] + ": single master kept"
    hot = verdict("master_OSC", 26, 25)
    assert hot["still_clipped"] and hot["line"] == "core clipped in master_OSC: 26 px (over 25)"
    assert verdict("master_OSC", 25, 25)["still_clipped"] is False      # the limit itself


def test_no_compact_core_skips_the_check_with_one_line(tmp_path):
    from photonscript.integration import core_master as cm
    from photonscript.shared.core_sat import compact_core
    assert compact_core("M31") == (True, "galaxy")
    assert compact_core("M13")[0] and compact_core("NGC 6543")[0]       # globular, planetary
    assert compact_core("Heart Nebula") == (False, "emission nebula")
    assert compact_core("M45") == (False, "open cluster")
    assert compact_core("Not A Catalog Thing")[0]                       # unknown: checked
    assert compact_core("x", "Supernova Remnant") == (False, "supernova remnant")
    r, lines = _heart_run(tmp_path, 13, target="Heart Nebula")
    assert r["status"] == "skipped" and "long_check" not in r
    assert lines == ["  OSC core combine: core check skipped: Heart Nebula is an emission "
                     "nebula (no compact core): single master kept"]
    echo = []
    res = cm.check_run(tmp_path / "out", [_st([300])], [], r_px=46.4, warn_px=25,
                       echo=echo.append, target="Heart Nebula")
    assert res == [{"skipped": "no compact core",
                    "line": "core check skipped: Heart Nebula is an emission nebula "
                            "(no compact core)"}]
    assert echo == ["  " + res[0]["line"]]
    assert res[0]["line"] in (tmp_path / "out" / "pipeline.log").read_text("ascii")
    # a galaxy is still checked
    res = cm.check_run(tmp_path / "out", [_st([300])], [], r_px=46.4, warn_px=25,
                       echo=lambda s: None, target="M31")
    assert res and res[0].get("master") == "master_OSC"
