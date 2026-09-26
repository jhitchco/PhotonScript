"""Tests for stage_for_analysis — copying subs into the Syncthing dropbox."""

import json
from types import SimpleNamespace

import numpy as np
import pytest

fits = pytest.importorskip("astropy.io.fits")

from photonscript.scheduler.runs import stage_for_analysis, analysis_dropbox


def _make_night(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "runs").mkdir(parents=True)
    nina = tmp_path / "NINA" / "2026-09-11" / "LIGHT"
    nina.mkdir(parents=True)

    def mk(name):
        p = nina / name
        fits.PrimaryHDU(np.zeros((4, 4), dtype=np.uint16)).writeto(p)
        return str(p)

    a0 = mk("2026-09-12_04-01-14__L_180.00s_0000.fits")  # rejected
    a1 = mk("2026-09-12_04-04-15__L_180.00s_0001.fits")  # accepted
    subs = [
        {"file": "LIGHT/f0.fits", "abs_path": a0, "target": "?",
         "filter": "L", "passed_qa": False},
        {"file": "LIGHT/f1.fits", "abs_path": a1, "target": "?",
         "filter": "L", "passed_qa": True},
    ]
    (data_dir / "runs" / "2026-09-11_subs.jsonl").write_text(
        "\n".join(json.dumps(s) for s in subs) + "\n")

    cfg = SimpleNamespace(
        data_dir=data_dir,
        library_dir=str(tmp_path / "Library"),
        desktop_library_dir=r"C:\Users\sleep\ninashare\Library",
        analysis_dropbox_subdir="_analysis",
        reverse_filter_map=lambda: {},
    )
    return cfg


def test_stage_rejected_only(tmp_path):
    cfg = _make_night(tmp_path)
    r = stage_for_analysis(cfg, "2026-09-11", which="rejected")
    assert r["copied"] == 1 and r["requested"] == 1
    f = r["files"][0]
    assert f["ok"] and f["name"].endswith("0000.fits")
    assert (analysis_dropbox(cfg) / "2026-09-11" / f["name"]).is_file()


def test_desktop_path_is_windows_form(tmp_path):
    cfg = _make_night(tmp_path)
    r = stage_for_analysis(cfg, "2026-09-11", which="rejected")
    assert r["files"][0]["desktop_path"] == (
        r"C:\Users\sleep\ninashare\Library\_analysis"
        r"\2026-09-11\2026-09-12_04-01-14__L_180.00s_0000.fits")


def test_stage_all_and_explicit_files(tmp_path):
    cfg = _make_night(tmp_path)
    assert stage_for_analysis(cfg, "2026-09-11", which="all")["copied"] == 2
    r = stage_for_analysis(cfg, "2026-09-11", files=["LIGHT/f1.fits"])
    assert r["copied"] == 1 and r["files"][0]["name"].endswith("0001.fits")


def test_missing_source_reported_not_fatal(tmp_path):
    cfg = _make_night(tmp_path)
    # point one record at a nonexistent file
    p = cfg.data_dir / "runs" / "2026-09-11_subs.jsonl"
    rows = [json.loads(x) for x in p.read_text().splitlines()]
    rows.append({"file": "LIGHT/gone.fits",
                 "abs_path": str(tmp_path / "gone.fits"),
                 "target": "?", "filter": "L", "passed_qa": False})
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    r = stage_for_analysis(cfg, "2026-09-11", files=["LIGHT/gone.fits"])
    assert r["copied"] == 0 and r["files"][0]["ok"] is False


def test_idempotent(tmp_path):
    cfg = _make_night(tmp_path)
    stage_for_analysis(cfg, "2026-09-11", which="all")
    r = stage_for_analysis(cfg, "2026-09-11", which="all")
    assert r["copied"] == 2  # already-present files still report ok
