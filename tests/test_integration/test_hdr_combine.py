"""HDR combine of mono exposure-group masters (after PS-177): RC16 M31 made
master_L (300 s) + master_L_30s and master_B (30 s) + master_B_300s and left
"HDR combine them later" to a human. integrate now runs deploy/hdr_combine.js
after the integration; the finish and the PS-153 blend prefer master_<F>_hdr."""
import json
import re
from pathlib import Path

import pytest

from photonscript.integration import blend as bl
from photonscript.integration import hdr
from photonscript.integration import pipeline as pl
from photonscript.integration import pjsr
from tests.test_integration.test_ps177_178_exposure_groups import _opts, mono_lib  # noqa: F401


def _st(name, filt, exp, mixed=False, exps=None):
    return {"name": name, "filter": filt, "mixed_exposures": mixed,
            "groups": [{"exp": e} for e in (exps or [exp])]}


M31 = [_st("L", "L", 300.0), _st("L_30s", "L", 30.0), _st("B", "B", 30.0), _st("B_300s", "B", 300.0),
       _st("R", "R", 30.0)]


# ----------------------------------------------------------------- PJSR

def test_template_is_ascii_and_rule_clean():
    raw = (pjsr.DEPLOY / hdr.TEMPLATE).read_bytes()
    t = raw.decode("ascii")
    assert pjsr.check(t) == ["CONFIG not filled"]
    assert not t.startswith("\ufeff")
    for n, ln in enumerate(t.splitlines(), 1):
        c = pjsr._line_comment(ln)
        assert c is None or "/*" not in c, n
    assert t.index("#include <pjsr/DataType.jsh>") < t.index(pjsr.CONFIG_MARK)


def test_template_has_the_guards_and_records():
    t = pjsr.template(hdr.TEMPLATE)
    # HDRComposition only when it exists; a failure falls back to the fit
    assert 'typeof HDRComposition !== "function"' in t
    assert 'METHOD === "auto" ? "; using the fitted replacement"' in t
    assert "function viaFit(" in t and "function fitShort(" in t and "smoothstep(" in t
    # the fitted slope must stay near the exposure ratio, else the ratio itself
    assert "f.a > ratio / 3 && f.a < ratio * 3" in t
    # sizes are checked (one registration), a failed filter is SKIPPED, not fatal
    assert "not one registration" in t
    assert 'log("SKIPPED [" + S + "]: "' in t
    assert "hdr_steps.json" in t and "timing_hdr.csv" in t and "hdr.log" in t
    assert 'log("EXIT OK")' in t and "PSHDR" in t
    # the optional mask blur is guarded
    blur = t[t.index("function blurMask("):t.index("function viaFit(")]
    assert "catch (e)" in blur and "Convolution" in blur


def test_render_embeds_config_and_passes_check():
    items = hdr.plan(M31, Path("D:/S/run/out"))
    js = hdr.render(Path("D:/S/run/out"), items)
    assert pjsr.check(js) == []
    cfg = json.loads(js.split("var CONFIG = ", 1)[1].split(";\n", 1)[0])
    assert cfg["method"] == "auto" and cfg["threshold"] == hdr.THRESHOLD
    assert cfg["out"] == "D:/S/run/out"
    assert [i["name"] for i in cfg["items"]] == ["B_hdr", "L_hdr"]


# ----------------------------------------------------------------- plan

def test_plan_pairs_long_and_short_longest_first():
    items = {i["filter"]: i for i in hdr.plan(M31, Path("D:/S/run/out"))}
    assert set(items) == {"L", "B"}                              # R has one length only
    assert [x["stack"] for x in items["L"]["inputs"]] == ["L", "L_30s"]
    # B: the 30 s group was the primary (most time), the 300 s one is still the base
    assert [x["stack"] for x in items["B"]["inputs"]] == ["B_300s", "B"]
    assert [x["exp"] for x in items["B"]["inputs"]] == [300.0, 30.0]
    assert items["L"]["output"] == "D:/S/run/out/master/master_L_hdr.xisf"
    assert items["L"]["inputs"][1]["path"] == "D:/S/run/out/master/master_L_30s.xisf"


def test_plan_skips_single_length_osc_and_missing_masters():
    assert hdr.plan([_st("L", "L", 300.0), _st("R", "R", 300.0)], Path("o")) == []
    osc = [_st("OSC", "OSC", 0, mixed=True, exps=[120.0, 400.0])]
    assert hdr.plan(osc, Path("o")) == []
    # L_30s made no master inside PixInsight: nothing to combine for L
    assert [i["filter"] for i in hdr.plan(M31, Path("o"), made={"L", "B", "B_300s", "R"})] == ["B"]


def test_plan_three_lengths():
    st = [_st("L", "L", 300.0), _st("L_30s", "L", 30.0), _st("L_120s", "L", 120.0)]
    (it,) = hdr.plan(st, Path("o"))
    assert [x["stack"] for x in it["inputs"]] == ["L", "L_120s", "L_30s"]


# ----------------------------------------------------------------- preference: finish

def _masters(names, out="D:/S/run/out"):
    return [{"name": f"M31_{n}", "path": f"{out}/master/master_{n}.xisf"} for n in names]


def test_finish_prefers_the_hdr_master():
    items = hdr.plan(M31, Path("D:/S/run/out"))
    got = hdr.finish_masters(_masters(["L", "L_30s", "B", "B_300s", "R"]), items, {"L_hdr", "B_hdr"},
                             Path("D:/S/run/out"))
    assert got == [{"name": "M31_L", "path": "D:/S/run/out/master/master_L_hdr.xisf"},
                   {"name": "M31_B", "path": "D:/S/run/out/master/master_B_hdr.xisf"},
                   {"name": "M31_R", "path": "D:/S/run/out/master/master_R.xisf"}]


def test_finish_keeps_single_masters_when_the_hdr_was_not_made():
    items = hdr.plan(M31, Path("D:/S/run/out"))
    ms = _masters(["L", "L_30s", "B", "B_300s", "R"])
    got = hdr.finish_masters(ms, items, {"B_hdr"}, Path("D:/S/run/out"))
    assert [m["name"] for m in got] == ["M31_L", "M31_L_30s", "M31_B", "M31_R"]
    assert got[2]["path"].endswith("master_B_hdr.xisf")
    assert hdr.finish_masters(ms, items, set(), Path("D:/S/run/out")) == ms


# ----------------------------------------------------------------- preference: blend

def _touch(p):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def test_blend_discovery_prefers_hdr_and_drops_its_exposure_masters(tmp_path):
    m = tmp_path / "run" / "out" / "master"
    for n in ("L", "L_30s", "L_hdr", "B", "B_300s", "R", "R_300s"):
        _touch(m / f"master_{n}.xisf")
    _touch(m / "masterBias.xisf")
    got = bl.masters(tmp_path / "run")
    assert got["L"].name == "master_L_hdr.xisf"
    assert "L_30s" not in got and "L_hdr" not in got
    # no HDR for B / R: unchanged behavior
    assert got["B"].name == "master_B.xisf" and "B_300s" in got and "R_300s" in got
    # the finish of an HDR run names it <target>_L, so the linear file still matches
    _touch(tmp_path / "run" / "out" / "final" / "M31_L_linear.xisf")
    lin, _ = bl.files_for(tmp_path / "run", "linear")
    assert lin["L"].name == "M31_L_linear.xisf"


def test_blend_without_hdr_is_unchanged(tmp_path):
    m = tmp_path / "run" / "out" / "master"
    for n in ("L", "L_30s"):
        _touch(m / f"master_{n}.xisf")
    assert sorted(bl.masters(tmp_path / "run")) == ["L", "L_30s"]


@pytest.mark.parametrize("name,filt", [("master_B_hdr.xisf", "B"), ("master_L_hdr.xisf", "L"),
                                       ("master_R.xisf", "R")])
def test_filter_of_hdr(name, filt):
    assert bl.filter_of(Path(name)) == filt


# ----------------------------------------------------------------- pipeline

def _fake_runner(calls, make_hdr=True):
    def fake_run(script, log_path, **kw):
        script, log_path = Path(script), Path(log_path)
        calls.append(script.name)
        out = log_path.parent
        out.mkdir(parents=True, exist_ok=True)
        if script.name == "integrate_run.js":
            cfg = json.loads(script.read_text().split("var CONFIG = ", 1)[1].split(";\n", 1)[0])
            for s in cfg["stacks"]:
                _touch(out / "master" / f"master_{s['name']}.xisf")
        elif script.name == "hdr_run.js" and make_hdr:
            cfg = json.loads(script.read_text().split("var CONFIG = ", 1)[1].split(";\n", 1)[0])
            for it in cfg["items"]:
                _touch(Path(it["output"]))
            (out / "master" / "hdr_steps.json").write_text(json.dumps(
                {"result": "ok", "items": [{"name": it["name"], "method": "HDRComposition", "status": "ok"}
                                           for it in cfg["items"]]}))
        log_path.write_text("t  EXIT OK\n")
        return {"ok": True, "exit_code": 0, "minutes": 0.1, "last_line": "EXIT OK", "timed_out": False}
    return fake_run


def _finish_masters(out):
    fin = (out / "finish_run.js").read_text()
    return json.loads(fin.split("var MASTERS = ", 1)[1].split(";\n", 1)[0])


def test_pipeline_combines_and_the_finish_uses_the_hdr(mono_lib, tmp_path, monkeypatch):  # noqa: F811
    out = tmp_path / "Staging" / "hdr"
    calls = []
    monkeypatch.setattr(pl.runner, "run_script", _fake_runner(calls))
    msgs = []
    res = pl.run(_opts(mono_lib, tmp_path, out=out, allow_uncalibrated=True, run_pixinsight=True),
                 echo=msgs.append)
    assert calls == ["integrate_run.js", "hdr_run.js", "finish_run.js"]
    assert pjsr.check((out / "hdr_run.js").read_text("ascii")) == []
    assert res["hdr"]["made"] == ["L_hdr"] and res["hdr"]["ok"]
    assert _finish_masters(out) == [{"name": "M31_L", "path": pjsr.fwd(out / "out" / "master" / "master_L_hdr.xisf")}]
    assert any(r[0] == "PixInsight HDR combine (launch to exit)" for r in res["timing"])
    led = json.loads(Path(res["ledger"]).read_text())
    h = led["machine"]["hdr"]
    assert h["made"] == ["L_hdr"] and h["steps"]["items"][0]["method"] == "HDRComposition"
    assert [x["stack"] for x in h["items"][0]["inputs"]] == ["L", "L_30s"]
    assert any(p.endswith("master_L_hdr.xisf") for p in led["machine"]["outputs"])
    assert any("HDR combined into master_L_hdr" in m for m in msgs)


def test_pipeline_hdr_not_made_keeps_both_masters(mono_lib, tmp_path, monkeypatch):  # noqa: F811
    out = tmp_path / "Staging" / "nohdr"
    calls = []
    monkeypatch.setattr(pl.runner, "run_script", _fake_runner(calls, make_hdr=False))
    res = pl.run(_opts(mono_lib, tmp_path, out=out, allow_uncalibrated=True, run_pixinsight=True),
                 echo=lambda s: None)
    assert calls == ["integrate_run.js", "hdr_run.js", "finish_run.js"]
    assert res["hdr"]["made"] == []
    assert [m["name"] for m in _finish_masters(out)] == ["M31_L", "M31_L_30s"]


def test_pipeline_skips_hdr_when_one_length_exists(mono_lib, tmp_path, monkeypatch):  # noqa: F811
    out = tmp_path / "Staging" / "one"
    calls = []
    monkeypatch.setattr(pl.runner, "run_script", _fake_runner(calls))
    res = pl.run(_opts(mono_lib, tmp_path, out=out, run_pixinsight=True), echo=lambda s: None)
    assert [s["name"] for s in res["stacks"]] == ["L"]          # 300 s refused: no dark
    assert calls == ["integrate_run.js", "finish_run.js"]
    assert "hdr" not in res and not (out / "hdr_run.js").exists()
    assert "hdr" not in json.loads(Path(res["ledger"]).read_text())["machine"]


def test_pipeline_hdr_off(mono_lib, tmp_path, monkeypatch):  # noqa: F811
    out = tmp_path / "Staging" / "off"
    calls = []
    monkeypatch.setattr(pl.runner, "run_script", _fake_runner(calls))
    pl.run(_opts(mono_lib, tmp_path, out=out, allow_uncalibrated=True, run_pixinsight=True,
                 hdr_combine=False), echo=lambda s: None)
    assert calls == ["integrate_run.js", "finish_run.js"]
    assert [m["name"] for m in _finish_masters(out)] == ["M31_L", "M31_L_30s"]


def test_pipeline_no_pixinsight_writes_the_hdr_script(mono_lib, tmp_path):  # noqa: F811
    out = tmp_path / "Staging" / "nopi"
    msgs = []
    res = pl.run(_opts(mono_lib, tmp_path, out=out, allow_uncalibrated=True), echo=msgs.append)
    assert any(s.endswith("hdr_run.js") for s in res["scripts"])
    assert any("--run=" in m and "hdr_run.js" in m for m in msgs)


# ----------------------------------------------------------------- config

def test_config_default_on_and_on_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    from photonscript.shared.config import PhotonScriptConfig
    c = PhotonScriptConfig(_env_file=None)
    assert c.integrate_hdr_combine is True
    f = {x[0]: x for x in _CONFIG_FIELDS}["integrate_hdr_combine"]
    assert f[1] == "PS_INTEGRATE_HDR_COMBINE" and f[3] == "Integration" and f[4] == "bool"
    assert pl.config_options(c, "rc16")["hdr_combine"] is True
    c2 = PhotonScriptConfig(_env_file=None, integrate_hdr_combine=False)
    assert pl.config_options(c2, "rc16")["hdr_combine"] is False


def test_cli_integrate_has_hdr_flag():
    from typer.testing import CliRunner
    from photonscript.cli import app
    r = CliRunner().invoke(app, ["integrate", "--help"])
    assert r.exit_code == 0 and "--no-hdr" in re.sub(r"\x1b\[[0-9;]*m", "", r.output)
