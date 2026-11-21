"""Fast scratch: integrate / blend build on integration_scratch_root and
archive the keepers to the staging root (scratch.py)."""
import json
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from photonscript.integration import autointegrate as ai
from photonscript.integration import blend as bl
from photonscript.integration import ledger as writer
from photonscript.integration import pipeline as pl
from photonscript.integration import report, scratch, watch
from photonscript.integration.runner import PixInsightBusy
from photonscript.shared import ledger as L
from tests.test_integration.test_pipeline import _opts, lib  # noqa: F401 - fixture

QUIET = dict(echo=lambda s: None)


def _touch(p, n=16):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * n)
    return p


def _snapshot(root: Path):
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")}


def _sopts(lib, tmp_path, **kw):
    o = _opts(lib, tmp_path, run_pixinsight=True, scratch_root=tmp_path / "Scratch", **kw)
    return o


def _fake_pi(make_master=True, ok=True, calls=None):
    """runner.run_script stand-in: the integration writes intermediates
    (cal, cc, debayer, reg, ln) and, with make_master, master_OSC; the
    finish writes a final JPG."""
    def run(script, log_path, **kw):
        script, log_path = Path(script), Path(log_path)
        if calls is not None:
            calls.append((script, log_path))
        out = log_path.parent
        out.mkdir(parents=True, exist_ok=True)
        if script.name == "integrate_run.js":
            for d in ("cal", "cc", "debayer", "reg", "ln"):
                _touch(out / "OSC" / d / f"x_{d}.xisf", 1000)
            _touch(out / "OSC" / "weights.csv")
            _touch(out / "flatcal" / "OSC" / "f_c.xisf", 500)
            _touch(out / "master" / "masterBias.xisf")
            if make_master:
                _touch(out / "master" / "master_OSC.xisf", 200)
            _touch(out / "timing_pi.csv")
            log_path.write_text("t  EXIT OK\n" if ok else "t  ERROR: boom\n")
        else:
            _touch(out / "final" / "M31_OSC_final.jpg")
            _touch(out / "final" / "M31_OSC_linear.xisf")
            log_path.write_text("t  EXIT OK\n")
        return {"ok": ok, "exit_code": 0 if ok else 1, "minutes": 0.1,
                "last_line": "EXIT OK" if ok else "ERROR", "timed_out": False}
    return run


# ----------------------------------------------------------------- config + paths

def test_configured_root_blank_is_off():
    assert scratch.configured_root(SimpleNamespace(integration_scratch_root="")) is None
    assert scratch.configured_root(SimpleNamespace()) is None
    assert scratch.configured_root(None) is None
    assert scratch.configured_root(SimpleNamespace(integration_scratch_root="C:/Astro/Scratch")) \
        == Path("C:/Astro/Scratch")


def test_config_defaults_keep_todays_behavior():
    from photonscript.shared.config import PhotonScriptConfig
    c = PhotonScriptConfig.model_construct()
    assert c.integration_scratch_root == "" and c.integration_keep_staged_frames is False
    kw = pl.config_options(c, "piggyback")
    assert kw["scratch_root"] is None and kw["keep_staged_frames"] is False


def test_usable_and_run_roots(tmp_path):
    st, sc = tmp_path / "Staging", tmp_path / "Scratch"
    assert scratch.usable(sc, st)
    assert not scratch.usable(None, st)
    assert not scratch.usable(st, st)
    assert not scratch.usable(st / "inner", st)          # inside the staging root
    assert not scratch.usable(tmp_path, st)              # holds the staging root
    assert scratch.run_roots(st, sc) == [st, sc]
    assert scratch.run_roots(st, None) == [st]
    assert scratch.run_roots(st, st) == [st]


def test_scratch_path_selection(lib, tmp_path):
    o = _sopts(lib, tmp_path)
    ws = pl.workspace(o, **QUIET)
    assert ws is not None
    assert ws.work_dir.parent == tmp_path / "Scratch"
    assert ws.archive_dir.parent == tmp_path / "Staging"
    assert ws.work_dir.name == ws.archive_dir.name and ws.work_dir.name.startswith("M31_piggyback_")
    assert ws.run_dir == ws.work_dir
    # off: --out, --dry-run, --no-pixinsight, no root, a root that is the staging root
    assert pl.workspace(_sopts(lib, tmp_path, out=tmp_path / "x"), **QUIET) is None
    assert pl.workspace(_sopts(lib, tmp_path, dry_run=True), **QUIET) is None
    o2 = _sopts(lib, tmp_path)
    o2.run_pixinsight = False
    assert pl.workspace(o2, **QUIET) is None
    assert pl.workspace(_opts(lib, tmp_path, run_pixinsight=True), **QUIET) is None
    o3 = _sopts(lib, tmp_path)
    o3.scratch_root = tmp_path / "Staging"
    assert pl.workspace(o3, **QUIET) is None


def test_workspace_refuses_a_taken_archive_folder(tmp_path):
    st, sc = tmp_path / "Staging", tmp_path / "Scratch"
    _touch(st / "run1" / "ledger.json")
    with pytest.raises(ValueError, match="NEW folder"):
        scratch.Workspace.create(st / "run1", sc, st)
    ws = scratch.Workspace.create(st / "Blend" / "b1", sc, st, kind="blend")
    assert ws.work_dir == sc / "Blend" / "b1"


# ----------------------------------------------------------------- keep list

def _fake_run_folder(d: Path):
    for rel in ("ledger.json", "manifest.json", "manifest.csv", "reference.txt", "integrate_run.js",
                "finish_run.js", "qa/star_qa.csv", "astrobin/M31_packet.md", "astrobin/M31_acq.csv",
                "out/master/master_OSC.xisf", "out/master/masterBias.xisf",
                "out/final/M31_OSC_final.jpg", "out/final_hoo/M31_hoo.jpg",
                "out/pipeline.log", "out/finish.log", "out/timing.csv", "out/timing_pi.csv",
                "out/OSC/weights.csv", "out/OSC/dark_scaling.csv",
                "out/OSC/cal/a_c.xisf", "out/OSC/cc/a_cc.xisf", "out/OSC/debayer/a_d.xisf",
                "out/OSC/reg/a_r.xisf", "out/OSC/ln/a_n.xisf", "out/OSC/ln/a_n.xnml",
                "out/flatcal/OSC/f_c.xisf",
                "LIGHTS/OSC/120s/a.fits", "BIAS/b.fits", "DARKS/120s/d.fits", "FLATS/OSC/f.fits"):
        _touch(d / rel)


def test_archive_move_list(tmp_path):
    d = tmp_path / "run"
    _fake_run_folder(d)
    keep = {p.as_posix() for p in scratch.keep_files(d, scratch.keep_patterns("integrate"))}
    for want in ("ledger.json", "manifest.json", "manifest.csv", "reference.txt", "integrate_run.js",
                 "finish_run.js", "qa/star_qa.csv", "astrobin/M31_packet.md", "astrobin/M31_acq.csv",
                 "out/master/master_OSC.xisf", "out/master/masterBias.xisf",
                 "out/final/M31_OSC_final.jpg", "out/final_hoo/M31_hoo.jpg", "out/pipeline.log",
                 "out/finish.log", "out/timing.csv", "out/timing_pi.csv", "out/OSC/weights.csv",
                 "out/OSC/dark_scaling.csv"):
        assert want in keep, want
    for gone in ("cal", "cc", "debayer", "reg", "ln", "flatcal"):
        assert not any(f"/{gone}/" in k or k.startswith(f"out/{gone}/") for k in keep), gone
    assert not any(k.split("/")[0] in ("LIGHTS", "BIAS", "DARKS", "FLATS") for k in keep)
    keep2 = {p.as_posix() for p in scratch.keep_files(d, scratch.keep_patterns("integrate", True))}
    assert {"LIGHTS/OSC/120s/a.fits", "BIAS/b.fits", "DARKS/120s/d.fits", "FLATS/OSC/f.fits"} <= keep2
    b = tmp_path / "blend"
    for rel in ("blend_run.js", "manifest.json", "ledger.json", "out/blend.log", "out/timing.csv",
                "out/final/M31_blend.jpg", "out/work/osc_reg.xisf"):
        _touch(b / rel)
    kb = {p.as_posix() for p in scratch.keep_files(b, scratch.keep_patterns("blend"))}
    assert "out/final/M31_blend.jpg" in kb and "blend_run.js" in kb and "out/blend.log" in kb
    assert "out/work/osc_reg.xisf" not in kb
    assert "out/work/osc_reg.xisf" in {p.as_posix() for p in
                                       scratch.keep_files(b, scratch.keep_patterns("blend", True))}


def test_rewrite_handles_native_forward_and_json_escaped_paths():
    old, new = Path(r"C:\Astro\Scratch\run1"), Path(r"D:\Astrophotography\Staging\run1")
    txt = (r'a C:\Astro\Scratch\run1\out b C:/Astro/Scratch/run1/out c "C:\\Astro\\Scratch\\run1\\out"')
    got = scratch.rewrite_text(txt, old, new)
    assert "Scratch" not in got
    assert r"D:\Astrophotography\Staging\run1\out" in got
    assert "D:/Astrophotography/Staging/run1/out" in got
    assert r"D:\\Astrophotography\\Staging\\run1\\out" in got
    obj = scratch.rewrite_obj({"a": [str(old / "x")], "n": 3}, old, new)
    assert obj == {"a": [str(new / "x")], "n": 3}


# ----------------------------------------------------------------- cleanup scope

def test_cleanup_scope(tmp_path):
    sc = tmp_path / "Scratch"
    run = sc / "run1"
    _touch(run / "out" / "OSC" / "cal" / "a.xisf")
    sibling = _touch(sc / "run0" / "keep.xisf")
    outside = _touch(tmp_path / "Other" / "file.txt")
    for bad in (sc, tmp_path / "Other", tmp_path):
        with pytest.raises(ValueError):
            scratch.check_removable(bad, sc)
    with pytest.raises(ValueError, match="ninashare"):
        scratch.check_removable(sc / "ninashare" / "Library", sc)
    with pytest.raises(ValueError, match="overlaps"):
        scratch.check_removable(run, sc, protect=[run / "out"])
    ro = _touch(run / "LIGHTS" / "OSC" / "a.fits")
    os.chmod(ro, stat.S_IREAD)                            # a read-only staged copy
    scratch.remove_run(run, sc)
    assert not run.exists() and sibling.exists() and outside.exists() and sc.is_dir()


def test_cleanup_command_names_the_folder(tmp_path):
    cmd = scratch.cleanup_command(tmp_path / "Scratch" / "run1")
    assert cmd.startswith('Remove-Item -LiteralPath "') and cmd.endswith('" -Recurse -Force')
    assert "run1" in cmd


# ----------------------------------------------------------------- pipeline end to end

def test_success_archives_keepers_and_clears_scratch(lib, tmp_path, monkeypatch):
    monkeypatch.setattr(pl.runner, "run_script", _fake_pi())
    before = _snapshot(lib)
    sibling = _touch(tmp_path / "Scratch" / "older_run" / "keep.xisf")
    msgs = []
    res = pl.run(_sopts(lib, tmp_path), echo=msgs.append)
    assert _snapshot(lib) == before                                   # Library untouched
    arch = tmp_path / "Staging" / Path(res["run_dir"]).name
    assert Path(res["run_dir"]) == arch
    work = Path(res["scratch"]["work_dir"])
    assert work.parent == tmp_path / "Scratch" and not work.exists()  # scratch run folder gone
    assert sibling.exists()                                           # nothing else touched
    for rel in ("ledger.json", "manifest.json", "manifest.csv", "integrate_run.js", "finish_run.js",
                "out/master/master_OSC.xisf", "out/final/M31_OSC_final.jpg", "out/pipeline.log",
                "out/finish.log", "out/timing.csv", "out/timing_pi.csv", "out/OSC/weights.csv"):
        assert (arch / rel).is_file(), rel
    assert next((arch / "astrobin").glob("*_packet.md"))
    for gone in ("LIGHTS", "BIAS", "DARKS", "out/OSC/cal", "out/OSC/reg", "out/flatcal"):
        assert not (arch / gone).exists(), gone
    # every record now names the archive, not the scratch folder
    for rel in ("ledger.json", "manifest.json", "manifest.csv", "integrate_run.js", "finish_run.js"):
        txt = (arch / rel).read_text()
        if rel == "manifest.json":
            obj = json.loads(txt)
            obj.pop("scratch")                    # the one record of where it was built
            txt = json.dumps(obj)
        assert "Scratch" not in txt, rel
    led = L.load(arch / "ledger.json")
    assert led.machine["run_dir"] == str(arch)
    assert all(str(arch) in p for p in led.machine["outputs"])
    man = json.loads((arch / "manifest.json").read_text())
    assert man["scratch"]["work_dir"] == str(work) and man["scratch"]["staged_frames_kept"] is False
    assert Path(res["ledger"]) == arch / "ledger.json"
    assert res["scratch"]["product"] and res["scratch"].get("removed_bytes", 0) > 0
    assert any("scratch cleared" in m for m in msgs)
    # discovery: the run is found in the archive root
    assert [f.parent for f, _ in writer.local_ledgers(tmp_path / "Staging", tmp_path / "Scratch")] == [arch]


def test_keep_staged_frames_archives_the_copies(lib, tmp_path, monkeypatch):
    monkeypatch.setattr(pl.runner, "run_script", _fake_pi())
    res = pl.run(_sopts(lib, tmp_path, keep_staged_frames=True), **QUIET)
    arch = Path(res["run_dir"])
    assert len(list((arch / "LIGHTS").rglob("*.fits"))) == 5
    assert len(list((arch / "BIAS").glob("*.fits"))) == 12
    assert not (arch / "out" / "OSC" / "cal").exists()
    man = json.loads((arch / "manifest.json").read_text())
    assert man["scratch"]["staged_frames_kept"] is True
    staged = [f["staged"] for f in man["frames"] if f["staged"]]
    assert staged and all(Path(s).is_file() for s in staged)          # rewritten to the archive


def test_failure_without_master_keeps_scratch(lib, tmp_path, monkeypatch):
    monkeypatch.setattr(pl.runner, "run_script", _fake_pi(make_master=False, ok=False))
    msgs = []
    res = pl.run(_sopts(lib, tmp_path), echo=msgs.append)
    info = res["scratch"]
    work, arch = Path(info["work_dir"]), Path(info["archive_dir"])
    assert work.is_dir() and (work / "LIGHTS").is_dir() and (work / "out" / "OSC" / "cal").is_dir()
    assert not (work / "ledger.json").exists()                        # one ledger, in the archive
    assert (arch / "ledger.json").is_file() and (arch / "out" / "pipeline.log").is_file()
    assert (work / "out" / "pipeline.log").is_file()                  # copied, not moved
    assert info["kept_bytes"] > 0 and info["cleanup"] == scratch.cleanup_command(work)
    assert any(info["cleanup"] in m for m in msgs)
    assert any("KEPT" in m and "GB" in m for m in msgs)
    assert Path(res["ledger"]) == arch / "ledger.json"
    assert L.load(arch / "ledger.json").machine["run_dir"] == str(arch)


def test_error_after_staging_keeps_scratch_and_copies_records(lib, tmp_path, monkeypatch):
    def busy(*a, **k):
        raise PixInsightBusy("PixInsight is already running")
    monkeypatch.setattr(pl.runner, "run_script", busy)
    msgs = []
    with pytest.raises(PixInsightBusy):
        pl.run(_sopts(lib, tmp_path), echo=msgs.append)
    work = next((tmp_path / "Scratch").iterdir())
    arch = tmp_path / "Staging" / work.name
    assert (work / "LIGHTS").is_dir()                                 # kept for a rerun / debugging
    assert (arch / "manifest.json").is_file() and (arch / "integrate_run.js").is_file()
    assert any("Remove-Item" in m for m in msgs)


def test_error_before_staging_leaves_no_scratch_folder(lib, tmp_path):
    with pytest.raises(pl.PipelineError):
        pl.run(_sopts(lib, tmp_path, target="Crescent Nebula"), **QUIET)
    sc = tmp_path / "Scratch"
    assert not sc.exists() or not any(sc.iterdir())


def test_low_space_falls_back_to_the_staging_root(lib, tmp_path, monkeypatch):
    monkeypatch.setattr(pl.runner, "run_script", _fake_pi())
    monkeypatch.setattr(scratch, "free_bytes", lambda p: 1000)
    msgs = []
    res = pl.run(_sopts(lib, tmp_path), echo=msgs.append)
    run = Path(res["run_dir"])
    assert run.parent == tmp_path / "Staging"
    assert res["scratch_preflight"]["fell_back"] is True and "scratch" not in res
    assert (run / "LIGHTS").is_dir() and (run / "out" / "OSC" / "cal").is_dir()   # today's layout
    assert (run / "ledger.json").is_file() and (run / "out" / "timing.csv").is_file()
    sc = tmp_path / "Scratch"
    assert not sc.exists() or not any(sc.iterdir())
    assert any("WARNING: scratch too small" in m for m in msgs)


def test_preflight_estimate():
    assert scratch.estimate_bytes(100, osc=True) == 660
    assert scratch.estimate_bytes(100, osc=False) == 440


# ----------------------------------------------------------------- discovery across roots

def _ledger(root, name, rig="piggyback", version=1, reported=False, campaign="M31",
            created="2026-10-05T10:00:00Z"):
    led = L.Ledger(campaign=campaign, rig=rig, run=name, version=version, created_at=created,
                   reported=reported)
    return L.save(Path(root) / name / "ledger.json", led)


def test_discovery_across_roots(tmp_path):
    st, sc = tmp_path / "Staging", tmp_path / "Scratch"
    _ledger(st, "M31_piggyback_a", version=1, reported=True)
    _ledger(sc, "M31_piggyback_b", version=2, created="2026-10-06T10:00:00Z")   # archive did not finish
    _ledger(sc, "M31_piggyback_a", version=9)                                   # same name: archive wins
    found = writer.local_ledgers(st, sc)
    assert [f.parent.name for f, _ in found] == ["M31_piggyback_a", "M31_piggyback_b"]
    assert found[0][0].parent.parent == st
    assert len(writer.local_ledgers(st)) == 1
    assert writer.next_version(st, "M31", "piggyback", scratch_root=sc) == 3
    assert report.pending(st, sc) == [sc / "M31_piggyback_b" / "ledger.json"]
    assert report.pending(st) == []
    s = watch.local_state(st, "M31", "piggyback", sc)
    assert s["count"] == 2 and s["queued"] == ["M31_piggyback_b"]
    assert s["newest"] == "2026-10-06T10:00:00Z"


def test_blend_finds_runs_in_both_roots(tmp_path):
    st, sc = tmp_path / "Staging", tmp_path / "Scratch"
    _ledger(st, "M31_piggyback_1", version=1)
    _touch(st / "M31_piggyback_1" / "out" / "master" / "master_OSC.xisf")
    _ledger(sc, "M31_rc16_1", rig="rc16", version=1)
    _touch(sc / "M31_rc16_1" / "out" / "master" / "master_L.xisf")
    assert bl.find_runs(st, "M31", "rc16") == []
    assert [r.run_dir for r in bl.find_runs(st, "M31", "rc16", sc)] == [sc / "M31_rc16_1"]
    inp = bl.discover(bl.Options(target="M31", staging_root=st, scratch_root=sc))
    assert inp.osc.parent.parent.parent == st / "M31_piggyback_1"
    assert set(inp.rc16) == {"L"}


def test_already_blended_reads_both_roots(tmp_path):
    st, sc = tmp_path / "Staging", tmp_path / "Scratch"
    key = ["a.xisf", "b.xisf"]
    m = {"target": "M31", "inputs": [{"path": "a.xisf"}, {"path": "b.xisf"}], "pixinsight": {"ok": True}}
    _touch(sc / "Blend" / "M31_blend_1" / "manifest.json").write_text(json.dumps(m))
    assert ai.already_blended(st, "M31", key) is None
    assert ai.already_blended(st, "M31", key, sc) == "M31_blend_1"


# ----------------------------------------------------------------- blend

def test_blend_builds_in_scratch_and_archives(tmp_path, monkeypatch):
    st, sc = tmp_path / "Staging", tmp_path / "Scratch"
    _ledger(st, "M31_piggyback_1", version=1)
    _touch(st / "M31_piggyback_1" / "out" / "master" / "master_OSC.xisf")
    _ledger(st, "M31_rc16_1", rig="rc16", version=1)
    _touch(st / "M31_rc16_1" / "out" / "master" / "master_L.xisf")
    seen = []

    def fake_run(script, log_path, **k):
        seen.append(Path(script))
        fin = log_path.parent / "final"
        _touch(fin / "M31_blend.jpg")
        _touch(log_path.parent / "work" / "osc_reg.xisf", 1000)
        log_path.write_text("EXIT OK\n")
        return {"ok": True, "exit_code": 0, "minutes": 1.0, "last_line": "EXIT OK", "timed_out": False}

    monkeypatch.setattr(bl.runner, "run_script", fake_run)
    res = bl.run(bl.Options(target="M31", staging_root=st, scratch_root=sc), **QUIET)
    assert seen[0].parent.parent == sc / "Blend"                      # built on the scratch disk
    arch = Path(res["run_dir"])
    assert arch.parent == st / "Blend"
    assert (arch / "out" / "final" / "M31_blend.jpg").is_file() and (arch / "ledger.json").is_file()
    assert not (arch / "out" / "work").exists()
    assert not (sc / "Blend" / arch.name).exists()
    led = L.load(arch / "ledger.json")
    assert led.kind == "blend" and led.machine["run_dir"] == str(arch)
    assert all(str(arch) in p for p in res["outputs"])
