"""The manual TPoint record against TPoint's own files (2026-10-12): the
record still said the 2026-10-04 model (250 points) while the files held
the 356-point model written 2026-10-10. The TPoint section flags the record
stale and the operator's "Use file values" fills model date, points, sky
RMS and IH / ID from the files, keeping every other field."""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.scheduler import thesky_audit as ta
from photonscript.scheduler import tpoint_files as tf
from tests.test_scheduler.test_ps171_tpoint_files import _cfg, _real_tree

ROOT = Path(__file__).resolve().parents[2]
RECORD = {"model_date": "2026-10-04", "points": 250, "rms_arcsec": 15.77,
          "polar_az_arcmin": -2.6, "polar_alt_arcmin": -1.0, "model_active": True,
          "protrack_on": True, "run_binning": 1, "ih_arcsec": -3484.63,
          "id_arcsec": -2250.28, "notes": "after the reinstall"}


def _stats(tmp_path):
    root = _real_tree(tmp_path, {"TPoint base run outmod.dat": 60,
                                 "TPoint base run in.dat": 60})
    cfg = _cfg(tmp_path, thesky_user_dir=str(root))
    return cfg, tf.stats(cfg)


def test_file_values_from_the_real_files(tmp_path):
    _cfg_, st = _stats(tmp_path)
    fv = ta.file_values(st)
    assert fv["points"] == 255 and fv["rms_arcsec"] == 15.77
    assert fv["model_date"] == st["model_date"]
    assert fv["ih_arcsec"] == st["terms"]["IH"]["value"]
    assert fv["id_arcsec"] == st["terms"]["ID"]["value"]
    assert ta.file_values({"ok": False}) == {}
    # an active recal model gives its own IH / ID
    rc = dict(st, active_model="recal",
              recal={"terms": {"IH": {"value": 12.5}, "ID": {"value": -3.0}}})
    assert ta.file_values(rc)["ih_arcsec"] == 12.5


def test_record_vs_files_stale_rules():
    st = {"ok": True, "model_date": "2026-10-10", "points": 356, "sky_rms_arcsec": 14.2,
          "terms": {"IH": {"value": -3500.0}, "ID": {"value": -2240.0}},
          "model_file": "TPoint/Super Model Outmod.dat",
          "active_model_mtime": "2026-10-10T13:10:24Z"}
    r = ta.record_vs_files(dict(RECORD, entered_at="2026-10-06T06:37:05Z"), st)
    assert r["stale"] and "newer model (2026-10-10)" in r["why"]
    assert r["files"]["points"] == 356 and r["record"]["points"] == 250
    assert set(r["differs"]) == {"model_date", "points", "rms_arcsec", "ih_arcsec", "id_arcsec"}
    # up to date
    same = {"model_date": "2026-10-10", "points": 356, "rms_arcsec": 14.2,
            "ih_arcsec": -3500.0, "id_arcsec": -2240.0, "entered_at": "2026-10-11T01:00:00Z"}
    r = ta.record_vs_files(same, st)
    assert not r["stale"] and r["differs"] == []
    # same date, rebuilt after the entry with other numbers
    r = ta.record_vs_files(dict(same, points=300, entered_at="2026-10-10T05:00:00Z"), st)
    assert r["stale"] and "after the record was entered" in r["why"]
    # same date, entered after the model: the operator's numbers stand
    r = ta.record_vs_files(dict(same, points=300), st)
    assert not r["stale"]
    # an older file model never flags; no record date does
    assert not ta.record_vs_files(dict(RECORD, model_date="2026-10-11"), st)["stale"]
    assert ta.record_vs_files(None, st)["stale"]
    assert not ta.record_vs_files(RECORD, {"ok": False, "note": "x"})["stale"]


def test_use_file_values_keeps_the_other_fields(tmp_path):
    cfg, st = _stats(tmp_path)
    assert ta.save_manual(cfg, RECORD)["ok"]
    res = ta.use_file_values(cfg, st)
    assert res["ok"] and res["applied"]["points"] == 255
    rec = ta.load_manual(cfg)
    assert rec["points"] == 255 and rec["rms_arcsec"] == 15.77
    assert rec["model_date"] == st["model_date"]
    assert rec["ih_arcsec"] == st["terms"]["IH"]["value"]
    for k in ("protrack_on", "run_binning", "polar_az_arcmin", "notes", "model_active"):
        assert rec[k] == RECORD[k], k
    assert not ta.record_vs_files(rec, st)["stale"]
    hist = (Path(cfg.data_dir) / "thesky" / "manual_history.jsonl").read_text().splitlines()
    assert len(hist) == 2
    bad = ta.use_file_values(cfg, {"ok": False, "note": "no TPoint file"})
    assert not bad["ok"] and "no TPoint model" in bad["problems"][0]


def test_routes_flag_and_apply(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import thesky as rt
    cfg, st = _stats(tmp_path)
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    old = dict(RECORD, model_date=(datetime.utcnow() - timedelta(days=8)).strftime("%Y-%m-%d"))
    assert ta.save_manual(cfg, old)["ok"]
    d = asyncio.run(rt.api_thesky_manual())
    assert d["record"]["points"] == 250
    assert d["files"]["stale"] and d["files"]["files"]["points"] == 255
    out = asyncio.run(rt.api_thesky_manual_use_files())
    assert out["ok"] and out["record"]["points"] == 255
    d = asyncio.run(rt.api_thesky_manual())
    assert not d["files"]["stale"]
    # no TPoint files: 400, and GET still answers
    cfg2 = _cfg(tmp_path / "none", thesky_user_dir=str(tmp_path / "nothing"))
    monkeypatch.setattr(app, "get_config", lambda: cfg2)
    assert asyncio.run(rt.api_thesky_manual_use_files()).status_code == 400
    assert asyncio.run(rt.api_thesky_manual())["files"]["stale"] is False


def test_page_has_the_stale_flag_and_button():
    js = (ROOT / "photonscript/scheduler/static/js/thesky_panels.js").read_text(encoding="utf-8")
    assert "/api/thesky/manual/use-files" in js and "on('tsManualUseFiles', useFiles)" in js
    assert "record stale" in js and js.isascii()
    html = (ROOT / "photonscript/scheduler/templates/guiding.html").read_text(encoding="utf-8")
    assert 'id="tsManualFiles"' in html and ">Use file values</button>" in html
