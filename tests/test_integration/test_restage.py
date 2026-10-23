"""`photonscript restage`: rebuild an archived run's staged frame copies from
manifest.json frames[].source (a fake Library mirror, read only) into a NEW
folder, sizes verified, scripts rewritten, then the rerun lines."""
import json
import os
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from photonscript import cli
from photonscript.integration import restage as rs
from photonscript.shared.config import PhotonScriptConfig


def _touch(p, n=16):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * n)
    return p


def _snapshot(root: Path):
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")}


@pytest.fixture
def archived(tmp_path, monkeypatch):
    """A run built in scratch and archived to staging (staged copies gone)."""
    lib = tmp_path / "D" / "ninashare" / "Library"
    scratch = tmp_path / "Scratch" / "M31_rc16_20261011-2200"
    run = tmp_path / "D" / "Staging" / "M31_rc16_20261011-2200"
    srcs = {
        "LIGHTS/L/120s/L1.fits": _touch(lib / "M31" / "2026-10-11" / "LIGHT" / "L1.fits", 100),
        "LIGHTS/L/120s/L2.fits": _touch(lib / "M31" / "2026-10-11" / "LIGHT" / "L2.fits", 110),
        "BIAS/B1.fits": _touch(lib / "_calibration" / "BIAS" / "B1.fits", 50),
        "DARKS/120s/D1.fits": _touch(lib / "_calibration" / "DARK" / "D1.fits", 60),
        "FLATS/L/F1.fits": _touch(lib / "_calibration" / "FLAT" / "F1.fits", 70),
    }
    for p in srcs.values():
        os.chmod(p, stat.S_IREAD)         # the mirror is read only
    rej = _touch(lib / "M31" / "2026-10-11" / "LIGHT" / "L3.fits", 120)

    def fe(rel, role):
        src = srcs[rel]
        return {"role": role, "file": src.name, "source": str(src), "bytes": src.stat().st_size,
                "staged": str(run / rel.replace("/", os.sep)), "qa_action": "keep"}
    frames = [fe("LIGHTS/L/120s/L1.fits", "light"), fe("LIGHTS/L/120s/L2.fits", "light"),
              {"role": "light", "file": "L3.fits", "source": str(rej), "bytes": 120, "staged": "",
               "qa_action": "reject"},
              fe("BIAS/B1.fits", "bias"), fe("DARKS/120s/D1.fits", "dark"),
              fe("FLATS/L/F1.fits", "flat")]
    man = {"version": 1, "target": "Andromeda Galaxy", "rig": "rc16", "run_name": run.name,
           "run_dir": str(run), "options": {"library": str(lib)}, "frames": frames,
           "scratch": {"work_dir": str(scratch), "archived_to": str(run)}}
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(json.dumps(man, indent=1), encoding="ascii")
    fwd = str(run).replace("\\", "/")
    (run / "integrate_run.js").write_text(f'var STAGING = "{fwd}";\nvar OUT = "{fwd}/out";\n',
                                          encoding="ascii")
    (run / "finish_run.js").write_text(f'var OUT = "{fwd}/out";\n', encoding="ascii")
    (run / "reference.txt").write_text(str(run / "LIGHTS" / "L" / "120s" / "L1.fits") + "\n",
                                       encoding="ascii")
    (run / "ledger.json").write_text("{}", encoding="ascii")
    _touch(run / "out" / "master" / "master_L.xisf")
    cfg = PhotonScriptConfig(_env_file=None, integration_scratch_root=str(tmp_path / "Scratch"),
                             integration_staging_root=str(run.parent),
                             integration_library_dir=str(lib))
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)
    yield {"run": run, "lib": lib, "srcs": srcs, "scratch": scratch, "tmp": tmp_path}
    for p in lib.rglob("*"):
        if p.is_file():
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)


def test_restage_rebuilds_the_staged_frames(archived):
    run, lib = archived["run"], archived["lib"]
    before = _snapshot(lib)
    new = archived["tmp"] / "Scratch" / "redo"
    p = rs.plan(run, new)
    assert [i.rel for i in p.items] == ["LIGHTS/L/120s/L1.fits", "LIGHTS/L/120s/L2.fits",
                                        "BIAS/B1.fits", "DARKS/120s/D1.fits", "FLATS/L/F1.fits"]
    assert p.total == 390 and p.errors == []
    res = rs.restage(p, echo=lambda s: None, library=lib)
    assert res["copied"] == 5 and res["bytes"] == 390 and res["errors"] == []
    for rel, src in archived["srcs"].items():
        assert (new / rel).stat().st_size == src.stat().st_size
    assert not (new / "LIGHTS" / "L" / "120s" / "L3.fits").exists()    # QA reject: never staged
    assert _snapshot(lib) == before                                     # Library untouched
    # scripts and manifest point at the new folder; no ledger copy
    js = (new / "integrate_run.js").read_text()
    assert str(new).replace("\\", "/") in js and str(run).replace("\\", "/") not in js
    assert str(new) in (new / "reference.txt").read_text()
    m = json.loads((new / "manifest.json").read_text())
    assert m["run_dir"] == str(new) and m["restaged"]["from"] == str(run)
    assert m["frames"][0]["source"] == str(archived["srcs"]["LIGHTS/L/120s/L1.fits"])
    assert m["frames"][0]["staged"].startswith(str(new))
    assert not (new / "ledger.json").exists()
    assert json.loads((new / "restage.json").read_text())["copied"] == 5
    # the archived run is unchanged
    assert not (run / "LIGHTS").exists()
    lines = rs.rerun_commands(res, r"C:\PI\PixInsight.exe", run.name)
    assert lines[0] == (f'& "C:\\PI\\PixInsight.exe" -n --automation-mode '
                        f'"--run={new / "integrate_run.js"}" --force-exit')
    assert "finish_run.js" in lines[1] and lines[2].startswith("photonscript finish --master")


def test_staged_rel_from_a_scratch_path_and_guards():
    roots = [r"D:\Staging\M31_x"]
    assert rs.staged_rel(r"C:\Astro\Scratch\M31_x\LIGHTS\L\120s\a.fits", roots) == \
        "LIGHTS/L/120s/a.fits"
    assert rs.staged_rel(r"d:\staging\m31_x\BIAS\b.fits", roots) == "BIAS/b.fits"
    assert rs.staged_rel(r"D:\Staging\M31_x\out\cal\a.xisf", roots) is None
    assert rs.staged_rel(r"D:\Staging\M31_x\LIGHTS\..\..\x.fits", roots) is None


def test_size_mismatch_and_missing_source_fail_but_copy_the_rest(archived):
    run, lib = archived["run"], archived["lib"]
    m = json.loads((run / "manifest.json").read_text())
    m["frames"][0]["bytes"] = 999                       # L1 changed size
    os.chmod(archived["srcs"]["BIAS/B1.fits"], stat.S_IWRITE | stat.S_IREAD)
    archived["srcs"]["BIAS/B1.fits"].unlink()           # gone from the mirror
    (run / "manifest.json").write_text(json.dumps(m), encoding="ascii")
    new = archived["tmp"] / "redo2"
    res = rs.restage(rs.plan(run, new), echo=lambda s: None, library=lib)
    assert res["copied"] == 3 and len(res["errors"]) == 2
    assert any("manifest says 999" in e for e in res["errors"])
    assert any("source missing" in e for e in res["errors"])
    assert not (new / "LIGHTS" / "L" / "120s" / "L1.fits").exists()


def test_library_remap(archived, tmp_path):
    run, lib = archived["run"], archived["lib"]
    moved = tmp_path / "NewMirror" / "Library"
    import shutil
    shutil.copytree(lib, moved)
    m = json.loads((run / "manifest.json").read_text())
    old = tmp_path / "OldMirror" / "Library"
    for e in m["frames"]:
        e["source"] = e["source"].replace(str(lib), str(old))
    m["options"]["library"] = str(old)
    (run / "manifest.json").write_text(json.dumps(m), encoding="ascii")
    p = rs.plan(run, tmp_path / "redo3", moved)
    assert all(str(i.source).startswith(str(moved)) for i in p.items)
    res = rs.restage(p, echo=lambda s: None, library=moved)
    assert res["copied"] == 5 and not res["errors"]
    for q in moved.rglob("*"):
        if q.is_file():
            os.chmod(q, stat.S_IWRITE | stat.S_IREAD)


def test_out_guards(archived, tmp_path):
    run, lib = archived["run"], archived["lib"]
    _touch(tmp_path / "busy" / "x.txt")
    for bad in (tmp_path / "busy", run, run / "redo", lib / "redo",
                tmp_path / "ninashare" / "redo"):
        with pytest.raises(rs.RestageError):
            rs.check_out(bad, run, lib)
    with pytest.raises(rs.RestageError):
        rs.restage(rs.plan(run, tmp_path / "small"), echo=lambda s: None, library=lib,
                   free=lambda p: 100)
    assert not (tmp_path / "small").exists()
    with pytest.raises(rs.RestageError):
        rs.plan(tmp_path / "busy", tmp_path / "x")        # no manifest


def test_cli_restage_and_rerun_lines(archived):
    run = archived["run"]
    new = archived["tmp"] / "Scratch" / "cli_redo"
    r = CliRunner().invoke(cli.app, ["restage", str(run), "--out", str(new), "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "5 frames" in r.output and not new.exists()
    r = CliRunner().invoke(cli.app, ["restage", str(run), "--out", str(new),
                                     "--pixinsight-exe", r"C:\PI\PixInsight.exe"])
    assert r.exit_code == 0, r.output
    out = r.output.replace("\n", "")
    assert "copied 5 of 5 frames" in out and "--automation-mode" in out
    assert "integrate_run.js" in out and "photonscript finish --master" in out
    # a second restage into the same folder is refused (not empty)
    r = CliRunner().invoke(cli.app, ["restage", str(run), "--out", str(new)])
    assert r.exit_code == 2
    # default --out: under the scratch root
    r = CliRunner().invoke(cli.app, ["restage", str(run), "--dry-run"])
    assert r.exit_code == 0 and "_restage_" in r.output.replace("\n", "")
