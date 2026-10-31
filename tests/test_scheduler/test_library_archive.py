"""Library archive: move old nights out of the Syncthing share, never re-link."""
import json
import os
import time
from datetime import datetime

import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.scheduler import library_archive as la
from photonscript.scheduler import runs


def _touch(p, date=None):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x" * 10)
    if date:
        t = time.mktime(datetime.strptime(date, "%Y-%m-%d").timetuple()) + 3600
        os.utime(p, (t, t))
    return p


@pytest.fixture
def world(tmp_path):
    share = tmp_path / "NINAShare"
    lib = share / "Library"
    watch = tmp_path / "NINA"
    cfg = PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                             library_dir=str(lib), image_watch_dir=str(watch),
                             review_gate=False, library_cal_days=100000)
    # lights: one old night (in the subs log), one new night, one unknown (mtime)
    old_src = _touch(watch / "2026-08-20" / "M31" / "LIGHT" / "m31_old.fits")
    new_src = _touch(watch / "2026-09-20" / "M31" / "LIGHT" / "m31_new.fits")
    for d, src in (("2026-08-20", old_src), ("2026-09-20", new_src)):
        (runs.runs_dir(cfg) / f"{d}_subs.jsonl").write_text(json.dumps({
            "file": f"M31/LIGHT/{src.name}", "abs_path": str(src),
            "target": "M31", "filter": "Ha", "passed_qa": True,
            "reviewed": True}) + "\n", encoding="utf-8")
    runs.build_library(cfg)
    _touch(lib / "M31" / "Ha" / "stray_old.fits", date="2026-07-01")
    # calibration already in the library
    _touch(lib / "Calibration" / "FLAT" / "2026-08-20" / "f1.fits")
    _touch(lib / "Calibration" / "DARK" / "2026-08-01" / "d1.fits")
    _touch(lib / "Calibration" / "FLAT" / "2026-09-20" / "f2.fits")
    _touch(lib / "_analysis" / "keepme.fits", date="2026-01-01")
    return cfg, share, lib, tmp_path


def test_dry_run_plans_old_lights_and_flats_only(world):
    cfg, share, lib, _ = world
    r = la.run_archive(cfg, "2026-09-01")
    assert r["applied"] is False
    rels = {m["rel"].replace("\\", "/") for m in r["moves"]}
    assert rels == {"M31/Ha/m31_old.fits", "M31/Ha/stray_old.fits",
                    "Calibration/FLAT/2026-08-20/f1.fits"}
    assert r["kept_calibration"] == [{"type": "DARK", "date": "2026-08-01", "files": 1}]
    assert (lib / "M31" / "Ha" / "m31_old.fits").exists()      # nothing moved


def test_apply_moves_outside_share_and_build_library_does_not_relink(world):
    cfg, share, lib, tmp = world
    r = la.run_archive(cfg, "2026-09-01", apply=True)
    assert r["moved"] == 3 and r["failed"] == 0
    arch = tmp / "NINAArchive" / "Library"
    assert (arch / "M31" / "Ha" / "m31_old.fits").exists()
    assert (arch / "Calibration" / "FLAT" / "2026-08-20" / "f1.fits").exists()
    assert not (lib / "M31" / "Ha" / "m31_old.fits").exists()
    assert not (lib / "Calibration" / "FLAT" / "2026-08-20").exists()  # pruned
    assert (lib / "M31" / "Ha" / "m31_new.fits").exists()               # kept
    assert (lib / "Calibration" / "DARK" / "2026-08-01" / "d1.fits").exists()
    assert (lib / "_analysis" / "keepme.fits").exists()
    assert la.archive_cutoff(cfg) == "2026-09-01"
    # a full rebuild ("Reset library") must not pull the old night back
    _touch(tmp / "NINA" / "2026-08-20" / "FLAT" / "f9.fits")
    _touch(tmp / "NINA" / "2026-08-20" / "DARK" / "d9.fits")
    res = runs.build_library(cfg)
    assert not (lib / "M31" / "Ha" / "m31_old.fits").exists()
    assert not (lib / "Calibration" / "FLAT" / "2026-08-20").exists()
    assert (lib / "Calibration" / "DARK" / "2026-08-20" / "d9.fits").exists()
    assert res["archived_nights_skipped"] == 1


def test_refuses_archive_inside_share(world):
    cfg, share, lib, _ = world
    with pytest.raises(ValueError):
        la.run_archive(cfg, "2026-09-01", apply=True, dest=str(share / "Archive"))


def test_calibration_all_and_bad_input(world):
    cfg, *_ = world
    r = la.plan_archive(cfg, "2026-09-01", calibration="all")
    assert {m["kind"] for m in r["moves"]} == {"lights", "FLAT", "DARK"}
    with pytest.raises(ValueError):
        la.plan_archive(cfg, "9/1/2026")
    with pytest.raises(ValueError):
        la.plan_archive(cfg, "2026-09-01", calibration="darks")


def test_cli_dry_run(world, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    cfg, *_ = world
    monkeypatch.setattr("photonscript.shared.config.PhotonScriptConfig",
                        lambda *a, **k: cfg)
    r = CliRunner().invoke(cli.app, ["archive-library", "--before", "2026-09-01"])
    assert r.exit_code == 0, r.output
    assert "DRY RUN" in r.output and "lights" in r.output
