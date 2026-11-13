"""`photonscript scratch-status`: run folders left on the scratch disk and
the legacy --out root, and --clean of one named folder (scratch_status.py,
scratch.check_removable guards)."""
import json
import os
import time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from photonscript import cli
from photonscript.integration import scratch_status as ss
from photonscript.shared.config import PhotonScriptConfig


def _touch(p, n=16):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * n)
    return p


@pytest.fixture
def disks(tmp_path, monkeypatch):
    scratch = tmp_path / "Scratch"
    legacy = tmp_path / "AstroStaging"
    staging = tmp_path / "D" / "Staging"
    library = tmp_path / "D" / "ninashare" / "Library"
    _touch(library / "M31" / "a.fits")
    # a failed run kept for debugging: no master, no twin
    _touch(scratch / "M31_piggyback_20261011-2200" / "LIGHTS" / "OSC" / "a.fits", 4000)
    _touch(scratch / "M31_piggyback_20261011-2200" / "out" / "OSC" / "cal" / "a_c.xisf", 6000)
    # a run whose archive step did not finish: master here, no twin
    _touch(scratch / "M33_rc16_20261010-2100" / "out" / "master" / "master_L.xisf", 500)
    # a finished run whose cleanup failed: twin archived
    _touch(scratch / "NGC7000_rc16_20261009-2000" / "out" / "cal" / "x.xisf", 300)
    _touch(staging / "NGC7000_rc16_20261009-2000" / "ledger.json")
    # a blend under scratch/Blend
    _touch(scratch / "Blend" / "M31_blend_20261011-0900" / "out" / "final" / "M31_blend.jpg", 50)
    # a legacy --out run
    _touch(legacy / "Crescent_20260901" / "out" / "master" / "master_Ha.xisf", 800)
    old = time.time() - 3 * 86400
    for p in (legacy / "Crescent_20260901").rglob("*"):
        os.utime(p, (old, old))
    cfg = PhotonScriptConfig(_env_file=None, integration_scratch_root=str(scratch),
                             integration_staging_root=str(staging),
                             integration_library_dir=str(library))
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)
    return {"scratch": scratch, "legacy": legacy, "staging": staging, "library": library}


def _invoke(disks, *args, input=None):
    return CliRunner().invoke(cli.app, ["scratch-status", "--legacy-root", str(disks["legacy"]),
                                        *args], input=input)


def test_scan_lists_both_roots(disks):
    fs = {f.name: f for f in ss.scan(disks["scratch"], disks["staging"], disks["legacy"])}
    assert set(fs) == {"M31_piggyback_20261011-2200", "M33_rc16_20261010-2100",
                       "NGC7000_rc16_20261009-2000", "Blend/M31_blend_20261011-0900",
                       "Crescent_20260901"}
    f = fs["M31_piggyback_20261011-2200"]
    assert f.size == 10000 and f.product == "" and f.archive is None and f.kind == "scratch"
    assert fs["M33_rc16_20261010-2100"].product == "master"
    assert fs["Blend/M31_blend_20261011-0900"].product == "final"
    assert fs["NGC7000_rc16_20261009-2000"].archive == disks["staging"] / "NGC7000_rc16_20261009-2000"
    leg = fs["Crescent_20260901"]
    assert leg.kind == "legacy" and 2.9 <= leg.age_days <= 3.1
    assert leg.cleanup == f'Remove-Item -LiteralPath "{leg.path.resolve()}" -Recurse -Force'


def test_roots_skip_the_staging_root_and_ninashare(tmp_path, disks):
    st = disks["staging"]
    assert ss.roots(st, st, None) == []                       # scratch = staging root
    assert ss.roots(st / "sub", st, None) == []               # inside it
    assert ss.roots(disks["library"].parent, st, None) == []  # a ninashare path
    assert ss.roots(tmp_path / "missing", st, None) == []


def test_cli_lists_with_cleanup_lines(disks):
    r = _invoke(disks)
    assert r.exit_code == 0, r.output
    assert "M31_piggyback_20261011-2200" in r.output and "NOT archived" in r.output
    assert "archived: " in r.output and "Remove-Item -LiteralPath" in r.output
    assert "5 folder(s)" in r.output
    r = _invoke(disks, "--json")
    rows = json.loads(r.output)
    assert {x["name"] for x in rows} >= {"Crescent_20260901"}
    assert all(x["cleanup"].startswith("Remove-Item") for x in rows)


def test_clean_asks_and_deletes_only_that_folder(disks):
    target = disks["scratch"] / "M31_piggyback_20261011-2200"
    r = _invoke(disks, "--clean", "M31_piggyback_20261011-2200", input="n\n")
    assert r.exit_code == 1 and target.exists() and "not deleted" in r.output
    out = r.output.replace("\n", "")
    assert "0.0 GB" in out and "NOT archived" in out
    r = _invoke(disks, "--clean", "M31_piggyback_20261011-2200", input="\n")   # default N
    assert target.exists()
    r = _invoke(disks, "--clean", "M31_piggyback_20261011-2200", input="y\n")
    assert r.exit_code == 0, r.output
    assert not target.exists() and "freed" in r.output
    # everything else is untouched
    assert (disks["scratch"] / "M33_rc16_20261010-2100" / "out" / "master" / "master_L.xisf").exists()
    assert (disks["library"] / "M31" / "a.fits").exists()


def test_clean_yes_blend_and_legacy(disks):
    r = _invoke(disks, "--clean", "Blend/M31_blend_20261011-0900", "--yes")
    assert r.exit_code == 0, r.output
    assert not (disks["scratch"] / "Blend" / "M31_blend_20261011-0900").exists()
    assert (disks["scratch"] / "Blend").is_dir()
    r = _invoke(disks, "--clean", "Crescent_20260901", "--yes")
    assert r.exit_code == 0 and "WARNING" in r.output     # a master with no twin
    assert not (disks["legacy"] / "Crescent_20260901").exists()
    assert disks["legacy"].is_dir()


def test_clean_refuses_anything_not_listed(disks, tmp_path):
    for name in ("", "..", "../D", "Blend", "NoSuchRun", str(disks["library"]),
                 "../D/Staging/NGC7000_rc16_20261009-2000"):
        if not name:
            continue
        r = _invoke(disks, "--clean", name, "--yes")
        assert r.exit_code == 2, (name, r.output)
    assert (disks["staging"] / "NGC7000_rc16_20261009-2000" / "ledger.json").exists()
    assert (disks["library"] / "M31" / "a.fits").exists()
    assert (disks["scratch"] / "Blend").is_dir()


def test_check_guards(disks, tmp_path):
    fs = {f.name: f for f in ss.scan(disks["scratch"], disks["staging"], disks["legacy"])}
    f = fs["M33_rc16_20261010-2100"]
    # a folder that overlaps a protected root is refused
    with pytest.raises(ValueError):
        ss.check(f, disks["staging"], [f.path])
    # a forged entry outside its root is refused
    bad = ss.Folder(name="x", path=disks["staging"], root=disks["scratch"], kind="scratch",
                    size=0, age_days=0, product="", archive=None)
    with pytest.raises(ValueError):
        ss.check(bad, disks["staging"])
    # nested deeper than a run folder is refused
    deep = ss.Folder(name="x", path=f.path / "out", root=disks["scratch"], kind="scratch",
                     size=0, age_days=0, product="", archive=None)
    with pytest.raises(ValueError):
        ss.check(deep, disks["staging"])
