"""/api/focus — per-rig autofocus seed data for the calibration Focus panel."""

import json

import pytest

from photonscript.shared.config import PhotonScriptConfig


@pytest.fixture
def app_cfg(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    from photonscript.scheduler import focus_seeds
    # Isolate the RC16 baseline: ignore the repo-tracked default seed table so
    # these assertions see only what the test writes into data_dir.
    monkeypatch.setattr(focus_seeds, "_MODULE_TABLE", tmp_path / "no_module.json")
    cfg = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                             piggyback_enabled=True, piggyback_focus_seed=11045)
    monkeypatch.setattr(app, "_config", cfg)
    return app, cfg, tmp_path


def test_focus_empty_stores(app_cfg):
    app, cfg, tmp_path = app_cfg
    out = app.api_focus()
    assert set(out) == {"rc16", "piggyback"}
    # RC16 has no records yet
    assert out["rc16"]["filters"] == {} and out["rc16"]["count"] == 0
    assert out["rc16"]["clamp"] == [4000, 7000]
    # OSC falls back to the static seed, sourced 'static'
    pb = out["piggyback"]
    assert pb["enabled"] is True
    assert pb["current_seed"] == 11045 and pb["source"] == "static"
    assert pb["points"] == []


def test_focus_reports_harvested_osc_and_rc16_filters(app_cfg):
    app, cfg, tmp_path = app_cfg
    (tmp_path / "piggyback_focus_seeds.json").write_text(json.dumps([
        {"focpos": 11040, "foctemp": 21.0, "n": 4, "date": "2026-09-21",
         "source": "harvest"},
        {"focpos": 11015, "foctemp": 12.0, "n": 6, "date": "2026-09-22",
         "source": "harvest"}]))
    (tmp_path / "focus_seeds.json").write_text(json.dumps([
        {"filter": "Ha", "focpos": 5600, "foctemp": 10.0, "date": "2026-09-20",
         "source": "harvest"},
        {"filter": "OIII", "focpos": 5620, "foctemp": 10.0, "date": "2026-09-20",
         "source": "harvest"}]))

    out = app.api_focus()
    # OSC now sourced from harvest, seed within the harvested range
    pb = out["piggyback"]
    assert pb["source"] == "harvested"
    assert len(pb["points"]) == 2
    assert 11000 <= pb["current_seed"] <= 11050
    # RC16 grouped per filter with a median seed each, rigs kept separate
    rc = out["rc16"]
    assert set(rc["filters"]) == {"Ha", "OIII"}
    assert rc["filters"]["Ha"]["seed"] == 5600
    assert rc["count"] == 2
