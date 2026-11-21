"""PS-187: the 2026-10-09 TPoint mapping run (--add auto) added no points.

GET /api/tpoint/samples?date= returns a night's tpoint-sample CSV rows and
a summary (solved / added / add errors), read only; the mapping recipe
passes --add off on every point unless a run asks for auto (the CSV is the
deliverable; TheSky's Automated Pointing Calibration adds points).
"""
import asyncio
import json
from pathlib import Path

import pytest

from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import tpoint_mapping as tm
from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import tpoint_sample as ts

ROOT = Path(__file__).resolve().parents[2]


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"), **kw)


def _rows(cfg, night="2026-10-09"):
    p = ts.csv_path(cfg, night)
    base = {"point": 1, "of": 45, "side": "east", "add_mode": "auto",
            "add_method": "action_execute_id_AddPointingSample"}
    ts.append_row(p, {**base, "solved": True, "added": True, "add_result": "0"})
    ts.append_row(p, {**base, "point": 2, "solved": False, "added": False,
                      "add_error": "not solved: not added",
                      "note": "Image Link did not solve"})
    ts.append_row(p, {**base, "point": 3, "solved": True, "added": False,
                      "add_error": "add method not available: not added"})
    return p


def test_read_rows_and_summary(tmp_path):
    cfg = _cfg(tmp_path)
    _rows(cfg)
    out = tm.samples(cfg, "2026-10-09")
    assert out["exists"] and out["file"] == "2026-10-09_tpoint.csv"
    assert [r["point"] for r in out["rows"]] == ["1", "2", "3"]
    s = out["summary"]
    assert (s["points"], s["solved"], s["added"]) == (3, 2, 1)
    assert s["add_errors"] == {"not solved: not added": 1,
                               "add method not available: not added": 1}
    assert s["add_results"] == {"0": 1} and s["add_modes"] == {"auto": 3}
    none = tm.samples(cfg, "2026-10-01")
    assert not none["exists"] and none["rows"] == [] and none["summary"]["points"] == 0


def test_route(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import thesky as rt
    cfg = _cfg(tmp_path)
    _rows(cfg)
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    out = asyncio.run(rt.api_tpoint_samples(date="2026-10-09"))
    assert out["summary"]["points"] == 3
    bad = asyncio.run(rt.api_tpoint_samples(date="../x"))
    assert bad.status_code == 400
    assert "/api/tpoint/samples" in {getattr(r, "path", None) for r in rt.router.routes}


def _plain(tmp_path, monkeypatch, **kw):
    script = tmp_path / "tpoint-sample.cmd"
    script.write_text("@echo off\n")
    cfg = _cfg(tmp_path, tpoint_sample_script=str(script), **kw)
    monkeypatch.setattr(nsj, "_gen_cfg_cache", cfg)
    monkeypatch.setattr(nsj, "_filter_names_cache", cfg.filter_name_map())
    monkeypatch.setattr(tm, "moon_track", lambda *a, **k: None)
    return cfg


def test_recipe_skips_the_add_by_default_even_with_config_auto(tmp_path, monkeypatch):
    cfg = _plain(tmp_path, monkeypatch, tpoint_sample_add="auto")
    run = tm.build(cfg, "2026-10-07T03:00:00Z", points=10)
    assert run["test_json"].count("--add off") == 10
    assert "--add auto" not in run["test_json"]
    assert "TPoint add off (default: CSV only, PS-187)" in run["field"]["summary"]
    # an explicit per-run auto (a supervised add test) still reaches it
    run = tm.build(cfg, "2026-10-07T03:00:00Z", points=10, add="auto")
    assert run["test_json"].count("--add auto") == 10


def test_sample_honours_the_run_override_over_config():
    cfg = PhotonScriptConfig(_env_file=None, tpoint_sample_add="auto")
    assert ts.add_mode(cfg, "off") == "off"
    assert ts.add_mode(cfg, None) == "auto"     # the CLI alone: config


def test_handbook_documents_the_finding():
    hb = (ROOT / "docs/HANDBOOK.md").read_text(encoding="utf-8")
    assert "PS-187 finding" in hb
    assert "GET /api/tpoint/samples?date=2026-10-09" in hb
    assert "Automated Pointing Calibration" in hb


@pytest.mark.parametrize("rel", [
    "tests/test_scheduler/test_ps187_tpoint_samples.py",
    "photonscript/telescope_agent/tpoint_sample.py",
    "photonscript/scheduler/tpoint_mapping.py"])
def test_ascii_no_em_dash(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert not [ln for ln in text.splitlines() if any(ord(c) > 127 for c in ln)]
