"""`photonscript finish`: finish one master again without integrating (the
same finish and defaults as integrate, one PixInsight at a time, a NEW
folder, a ledger of kind finish). Replaces run-finish-osc.ps1 re-finishes."""
import json
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from photonscript import cli
from photonscript.integration import finish as fn
from photonscript.integration import pjsr
from photonscript.integration import report as rp
from photonscript.integration.runner import PixInsightBusy
from photonscript.shared import ledger as L


def _touch(p, n=16):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * n)
    return p


@pytest.fixture
def staging(tmp_path):
    root = tmp_path / "Staging"
    run = root / "M31_rc16_20261012-0500"
    for f in ("L", "L_30s", "L_hdr", "R"):
        _touch(run / "out" / "master" / f"master_{f}.xisf")
    L.save(run / "ledger.json", L.Ledger(campaign="Andromeda Galaxy", rig="rc16", run=run.name, version=4))
    # what integrate rendered from the lights' headers and the catalog
    (run / "finish_run.js").write_text("var RA_DEG    = 10.68;\nvar DEC_DEG   = 41.27;\n"
                                       "var FOCAL_MM  = 3250.5;\nvar PIXEL_UM  = 3.76;\n", encoding="ascii")
    osc = root / "loose" / "masterOSC.xisf"
    _touch(osc)
    return root


def _masters_of(js: str):
    return json.loads(js.split("var MASTERS = ", 1)[1].split(";\n", 1)[0])


def _var(js: str, name: str) -> str:
    return re.search(rf"^var {name}\s*=\s*(.+?);", js, re.M).group(1)


def _fake(calls, ok=True, steps=True):
    def run_script(script, log_path, **kw):
        script, log_path = Path(script), Path(log_path)
        calls.append((script.name, log_path.name, kw.get("exe")))
        fin = log_path.parent / "final"
        _touch(fin / "M31_v5_final.jpg")
        if steps:
            (fin / "M31_v5_final_steps.json").write_text(json.dumps(
                {"name": "M31_v5", "result": "ok", "tag": "BXT+NXT", "finish": {"steps": []}}))
        log_path.write_text("t  EXIT OK\n" if ok else "t  ERROR: boom\n")
        return {"ok": ok, "exit_code": 0, "minutes": 2.0, "last_line": "EXIT OK" if ok else "ERROR: boom",
                "timed_out": False}
    return run_script


def _opts(staging, master, **kw):
    o = fn.Options(master=master, name="M31_v5", staging_root=staging,
                   pixinsight=str(staging / "PI" / "bin" / "PixInsight.exe"))
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def test_run_renders_the_finish_and_writes_a_finish_ledger(staging, monkeypatch):
    calls = []
    monkeypatch.setattr(fn.runner, "run_script", _fake(calls))
    master = staging / "M31_rc16_20261012-0500" / "out" / "master" / "master_L_hdr.xisf"
    out = staging / "Finish" / "f1"
    res = fn.run(_opts(staging, master, out=out), echo=lambda s: None)
    assert calls == [("finish_run.js", "finish.log", str(staging / "PI" / "bin" / "PixInsight.exe"))]
    js = (out / "finish_run.js").read_text("ascii")
    assert pjsr.check(js) == []
    assert _masters_of(js) == [{"name": "M31_v5", "path": pjsr.fwd(master)}]
    # the source run's optics and coordinates, the integrate defaults
    assert _var(js, "FOCAL_MM") == "3250.5" and _var(js, "RA_DEG") == "10.68"
    assert _var(js, "GRADIENT") == '"auto"' and _var(js, "RC_MODE") == '"auto"'
    assert _var(js, "FINAL") == '"' + pjsr.fwd(out / "out" / "final") + '"'
    led = L.load(out / "ledger.json")
    assert led.kind == "finish" and led.rig == "rc16" and led.campaign == "Andromeda Galaxy"
    assert led.version == 1
    assert led.machine["finish"]["ok"] is True
    assert led.machine["finish"]["steps"][0]["steps"]["tag"] == "BXT+NXT"
    assert led.machine["input"]["run_version"] == 4
    assert any(p.endswith("M31_v5_final.jpg") for p in res["outputs"])
    assert (out / "out" / "timing.csv").is_file()
    man = json.loads((out / "manifest.json").read_text("ascii"))
    assert man["kind"] == "finish" and man["master"] == str(master)
    # a finish ledger is never queued for the scheduler nor counted as an integration
    assert rp.pending(staging / "Finish") == []
    from photonscript.integration import ledger as writer
    assert writer.next_version(staging, "M31", "rc16") == 5


def test_every_finish_gets_a_new_folder(staging, monkeypatch):
    monkeypatch.setattr(fn.runner, "run_script", _fake([]))
    master = staging / "loose" / "masterOSC.xisf"
    out = staging / "Finish" / "f2"
    fn.run(_opts(staging, master, out=out), echo=lambda s: None)
    with pytest.raises(fn.FinishError, match="NEW folder"):
        fn.run(_opts(staging, master, out=out), echo=lambda s: None)
    # an empty folder is fine
    (staging / "Finish" / "empty").mkdir()
    fn.run(_opts(staging, master, out=staging / "Finish" / "empty"), echo=lambda s: None)
    assert L.load(staging / "Finish" / "empty" / "ledger.json").version == 2


def test_loose_master_uses_the_rig_and_target(staging, monkeypatch):
    monkeypatch.setattr(fn.runner, "run_script", _fake([]))
    master = staging / "loose" / "masterOSC.xisf"
    msgs = []
    res = fn.run(_opts(staging, master, dry_run=True), echo=msgs.append)
    assert res["rig"] == "piggyback" and res["config"]["focal_mm"] == 600.0
    assert res["config"]["ra_deg"] is None
    assert any("coordinates unknown" in m for m in msgs)
    res = fn.run(_opts(staging, master, dry_run=True, target="M31", rig="rc16"), echo=lambda s: None)
    assert res["rig"] == "rc16" and res["config"]["focal_mm"] == 3248.0
    assert res["config"]["ra_deg"] == pytest.approx(10.68, abs=0.1)
    assert res["campaign"] == "M31"


def test_dry_run_writes_nothing(staging):
    master = staging / "loose" / "masterOSC.xisf"
    res = fn.run(_opts(staging, master, dry_run=True, palette="gold"), echo=lambda s: None)
    assert res["script_problems"] == [] and res["config"]["palette"] == "gold"
    assert not (staging / "Finish").exists()


def test_errors(staging):
    with pytest.raises(fn.FinishError, match="no master"):
        fn.run(_opts(staging, staging / "nope.xisf", dry_run=True), echo=lambda s: None)
    master = staging / "loose" / "masterOSC.xisf"
    with pytest.raises(fn.FinishError, match="inside the finish folder"):
        fn.run(_opts(staging, master, out=staging / "loose"), echo=lambda s: None)
    with pytest.raises(fn.FinishError, match="--rig"):
        fn.run(_opts(staging, master, rig="mosaic", dry_run=True), echo=lambda s: None)


def test_pixinsight_busy_starts_nothing(staging, monkeypatch):
    def busy(*a, **k):
        raise PixInsightBusy("PixInsight is already running (pid 42)")
    monkeypatch.setattr(fn.runner, "run_script", busy)
    with pytest.raises(PixInsightBusy):
        fn.run(_opts(staging, staging / "loose" / "masterOSC.xisf", out=staging / "Finish" / "b"),
               echo=lambda s: None)


def test_hdr_sibling_is_pointed_out(staging):
    master = staging / "M31_rc16_20261012-0500" / "out" / "master" / "master_L.xisf"
    msgs = []
    fn.run(_opts(staging, master, dry_run=True), echo=msgs.append)
    assert any("master_L_hdr.xisf exists beside it" in m for m in msgs)


def test_source_values_parse(staging):
    v = fn.source_finish_values(staging / "M31_rc16_20261012-0500")
    assert v == {"RA_DEG": 10.68, "DEC_DEG": 41.27, "FOCAL_MM": 3250.5, "PIXEL_UM": 3.76}
    assert fn.source_finish_values(staging / "nope") == {} and fn.source_finish_values(None) == {}


# ----------------------------------------------------------------- CLI

def test_cli_dry_run_json(staging):
    master = staging / "loose" / "masterOSC.xisf"
    r = CliRunner().invoke(cli.app, ["finish", "--master", str(master), "--name", "M31_v5",
                                     "--staging-root", str(staging), "--dry-run", "--json",
                                     "--palette", "gold", "--rcastro", "off", "--gradient", "abe"])
    assert r.exit_code == 0, r.output
    res = json.loads(r.output[r.output.index("{"):])
    c = res["config"]
    assert c["palette"] == "gold" and c["rc_mode"] == "off" and c["gradient"] == "abe"
    assert res["script_problems"] == [] and not (staging / "Finish").exists()


def test_cli_runs_with_faked_runner_and_exit_codes(staging, monkeypatch):
    master = staging / "loose" / "masterOSC.xisf"
    calls = []
    monkeypatch.setattr(fn.runner, "run_script", _fake(calls))
    out = staging / "Finish" / "cli"
    r = CliRunner().invoke(cli.app, ["finish", "--master", str(master), "--name", "M31_v5",
                                     "--out", str(out), "--pixinsight-exe", "X:/PI/bin/PixInsight.exe"])
    assert r.exit_code == 0, r.output
    assert calls == [("finish_run.js", "finish.log", "X:/PI/bin/PixInsight.exe")]
    assert L.load(out / "ledger.json").kind == "finish"
    monkeypatch.setattr(fn.runner, "run_script", _fake([], ok=False))
    r = CliRunner().invoke(cli.app, ["finish", "--master", str(master), "--name", "M31_v5",
                                     "--out", str(staging / "Finish" / "cli2")])
    assert r.exit_code == 1
    r = CliRunner().invoke(cli.app, ["finish", "--master", str(master), "--name", "M31_v5",
                                     "--out", str(out)])
    assert r.exit_code == 1 and "NEW folder" in r.output
    r = CliRunner().invoke(cli.app, ["finish", "--master", str(master), "--name", "x", "--dry-run",
                                     "--palette", "rainbow"])
    assert r.exit_code == 2


def test_ledger_kind_finish():
    assert L.parse({"campaign": "M31", "run": "r", "kind": "Finish", "machine": {},
                    "schema": L.SCHEMA}).kind == "finish"
