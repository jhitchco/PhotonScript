"""PS-178: RC16 calibration reaches the Library (and the desktop).

2026-10-08: the scope's watch dir held RC16 300 s darks from a
calibration-only folder (2026-09-27, no run record: NINA restarted after
midnight) and a whole 2026-09-30 calibration set, and none of it was ever
filed: build_library(date) files only the night's own folder and
build_library() walked only nights with a subs file. The dawn sweep files
every recent calibration folder; the library report says, per frame, where
it is and why."""
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from photonscript.scheduler import calibration_library as cl
from photonscript.scheduler import calibration_qa as cq
from photonscript.scheduler import dawn_autofile as da
from photonscript.scheduler import runs
from photonscript.shared.config import PhotonScriptConfig


def _cfg(tmp_path, **kw):
    base = dict(_env_file=None, data_dir=str(tmp_path / "data"),
                image_watch_dir=str(tmp_path / "fits"),
                library_dir=str(tmp_path / "lib"),
                nina_logs_dir=str(tmp_path / "logs"),
                stamp_fits_object=False, observatory_tz="UTC",
                calibration_qa_mode="off", piggyback_enabled=False)
    base.update(kw)
    (tmp_path / "data").mkdir(exist_ok=True)
    return PhotonScriptConfig(**base)


def _day(n):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def _fits(path, kind, exp, settemp=0.0, ro="High Conversion Gain"):
    from astropy.io import fits
    h = fits.Header()
    h["IMAGETYP"] = kind
    h["EXPTIME"] = exp
    h["GAIN"] = 200
    h["OFFSET"] = 256
    h["SET-TEMP"] = settemp
    h["CCD-TEMP"] = settemp
    h["READOUTM"] = ro
    h["INSTRUME"] = "AP26MC"
    h["XBINNING"] = 1
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(np.zeros((4, 4), np.uint16), header=h).writeto(path)


def _cal(cfg, night, typ, n, exp, **kw):
    out = []
    for i in range(n):
        p = Path(cfg.image_watch_dir) / night / typ / f"{night}_{typ}_{exp:g}_{i:03d}.fits"
        _fits(p, typ, exp, **kw)
        out.append(p)
    return out


def _subs_file(cfg, night):
    p = runs.runs_dir(cfg) / f"{night}_subs.jsonl"
    p.write_text("", encoding="utf-8")


# ----------------------------------------------------------- finding the folders

def test_calibration_nights_lists_only_folders_with_calibration(tmp_path):
    cfg = _cfg(tmp_path)
    w = Path(cfg.image_watch_dir)
    _cal(cfg, _day(3), "DARK", 2, 300.0)
    (w / _day(2) / "LIGHT").mkdir(parents=True)
    _fits(w / _day(2) / "LIGHT" / "l.fits", "LIGHT", 300.0)
    _cal(cfg, _day(200), "BIAS", 1, 0.001)
    assert runs.calibration_nights(w) == [_day(200), _day(3)]
    assert runs.calibration_nights(w, 30) == [_day(3)]
    assert runs.calibration_nights(tmp_path / "missing") == []


def test_sweep_files_a_calibration_only_folder(tmp_path):
    cfg = _cfg(tmp_path)
    night, after_midnight = _day(5), _day(4)
    _subs_file(cfg, night)
    _cal(cfg, night, "DARK", 3, 180.0)
    _cal(cfg, after_midnight, "DARK", 4, 300.0)     # no run record for this folder
    runs.build_library(cfg, night)
    lib = Path(cfg.library_dir) / "Calibration" / "DARK"
    assert len(list((lib / night).glob("*.fits"))) == 3
    assert not (lib / after_midnight).exists()     # the 2026-09-27 gap
    res = runs.link_recent_calibration(cfg)
    assert res["rc16"]["nights"] == [night, after_midnight]
    assert res["rc16"]["linked"] == 4 and res["rc16"]["already_there"] == 3
    assert len(list((lib / after_midnight).glob("*.fits"))) == 4
    again = runs.link_recent_calibration(cfg)        # idempotent
    assert again["rc16"]["linked"] == 0 and again["rc16"]["already_there"] == 7


def test_sweep_is_bounded_by_its_window_and_library_cal_days(tmp_path):
    cfg = _cfg(tmp_path, library_cal_days=10)
    _cal(cfg, _day(20), "DARK", 2, 300.0)
    _cal(cfg, _day(8), "DARK", 2, 300.0)
    res = runs.link_recent_calibration(cfg, days=30)
    assert res["rc16"]["nights"] == [_day(8)]


def test_build_library_without_a_date_files_calibration_only_folders(tmp_path):
    cfg = _cfg(tmp_path)
    _cal(cfg, _day(6), "BIAS", 5, 0.001)
    res = runs.build_library(cfg)
    assert res["linked"] == 5
    assert len(list((Path(cfg.library_dir) / "Calibration" / "BIAS" / _day(6)).glob("*.fits"))) == 5


def test_sweep_quarantines_failing_frames(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, calibration_qa_mode="quarantine")
    night = _day(8)
    frames = _cal(cfg, night, "BIAS", 3, 0.001, settemp=20.0)

    def fake_qa(config, rig, fr, **kw):
        out = {}
        for typ, d, p in fr:
            out[cq.frame_key(typ, d, Path(p).name)] = {
                "type": typ, "date": d, "name": Path(p).name, "verdict": "fail",
                "reasons": ["temp: CCD-TEMP 29 C vs set 20 C (tol 1)"]}
        return out
    monkeypatch.setattr(cq, "qa_frames", fake_qa)
    res = runs.link_recent_calibration(cfg)
    assert res["rc16"]["linked"] == 0
    q = Path(cfg.library_dir) / "Calibration" / "_quarantine" / "BIAS" / night
    assert sorted(p.name for p in q.glob("*.fits")) == sorted(p.name for p in frames)
    assert "CCD-TEMP 29 C" in (q / "reasons.json").read_text()


def test_dawn_pass_runs_the_sweep(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(runs, "sync_goal_progress", lambda c: [])
    da._RUNNING.clear()
    seen = []
    monkeypatch.setattr(runs, "link_recent_calibration",
                        lambda c, days=runs.CAL_SWEEP_DAYS: seen.append(days) or
                        {"rc16": {"nights": ["a", "b"], "linked": 7, "already_there": 0}})
    night = _day(1)
    _subs_file(cfg, night)
    rec = da.file_night(cfg, night, push=False)
    assert seen == [runs.CAL_SWEEP_DAYS]
    assert rec["calibration_sweep"] == {"rc16": {"nights": 2, "linked": 7}}
    assert not [e for e in rec["errors"] if e.startswith("calibration sweep")]


# ----------------------------------------------------------- the report

def test_report_explains_each_frame(tmp_path):
    cfg = _cfg(tmp_path)
    lib = Path(cfg.library_dir)
    linked_night, cal_only, warm = _day(9), _day(8), _day(7)
    _subs_file(cfg, linked_night)
    _subs_file(cfg, warm)
    _cal(cfg, linked_night, "DARK", 3, 300.0)
    _cal(cfg, cal_only, "DARK", 2, 300.0)
    _cal(cfg, warm, "BIAS", 2, 0.001, settemp=20.0)
    _cal(cfg, warm, "DARK", 1, 600.0, ro="Low Conversion Gain")
    runs.build_library(cfg, linked_night)
    # one quarantined frame with reasons (as calibration_qa leaves it)
    qd = lib / "Calibration" / "_quarantine" / "DARK" / linked_night
    qd.mkdir(parents=True)
    src = lib / "Calibration" / "DARK" / linked_night
    first = sorted(src.glob("*.fits"))[0]
    first.replace(qd / first.name)
    store = {"version": 1, "rig": "rc16", "frames": {
        cq.frame_key("DARK", linked_night, first.name): {
            "type": "DARK", "date": linked_night, "name": first.name, "verdict": "fail",
            "reasons": ["leak: corner vs center 5.5 ADU (> 3)"], "exptime": 300.0,
            "gain": 200, "offset": 256, "settemp": 0.0, "readout": "HCG"}}}
    (Path(cfg.data_dir) / "calibration_qa").mkdir(parents=True, exist_ok=True)
    (Path(cfg.data_dir) / "calibration_qa" / "rc16.json").write_text(json.dumps(store))
    rep = cl.library_report(cfg, "rc16", frames_limit=50)
    by = {(s["type"], s["date"], s["status"]): s for s in rep["sets"]}
    assert by[("DARK", linked_night, "library")]["n"] == 2
    q = by[("DARK", linked_night, "quarantine")]
    assert q["n"] == 1 and q["reason"].startswith("leak")
    nf = by[("DARK", cal_only, "not_filed")]
    assert nf["n"] == 2 and "calibration-only folder" in nf["reason"]
    b = by[("BIAS", warm, "not_filed")]
    assert not b["usable"] and b["epoch_misses"] == ["SET-TEMP 20 C vs setpoint 0 C"]
    d = by[("DARK", warm, "not_filed")]
    assert d["epoch_misses"] == ["readout LCG vs HCG"]
    assert d["reason"] == "not filed yet (dawn filing / Approve night)"
    assert rep["totals"] == {"library": 2, "quarantine": 1, "not_filed": 5,
                             "usable_in_library": 2}
    assert rep["frames"][0]["status"] == "not_filed"
    txt = cl.format_report(rep)
    txt.encode("ascii")
    assert "not usable: SET-TEMP 20 C vs setpoint 0 C" in txt


def test_compare_desktop_counts_pending_frames(tmp_path):
    rep = {"rig": "rc16", "sets": [
        {"type": "DARK", "date": "2026-10-01", "status": "library", "names": ["a.fits", "b.fits"],
         "n": 2, "exptime": 300.0, "filter": None, "readout": "HCG", "settemp": 0.0,
         "reason": "", "usable": True, "epoch_misses": []},
        {"type": "DARK", "date": "2026-10-01", "status": "quarantine", "names": ["c.fits"],
         "n": 1, "exptime": 300.0, "filter": None, "readout": "HCG", "settemp": 0.0,
         "reason": "leak", "usable": True, "epoch_misses": []},
        {"type": "BIAS", "date": "2026-09-30", "status": "not_filed", "names": ["d.fits"],
         "n": 1, "exptime": 0.001, "filter": None, "readout": "HCG", "settemp": 20.0,
         "reason": "QA fail", "usable": False, "epoch_misses": ["SET-TEMP 20 C vs setpoint 0 C"]}]}
    mirror = tmp_path / "ninashare" / "Library"
    (mirror / "Calibration" / "DARK" / "2026-10-01").mkdir(parents=True)
    (mirror / "Calibration" / "DARK" / "2026-10-01" / "a.fits").write_bytes(b"x")
    (mirror / "Calibration" / "_quarantine" / "DARK" / "2026-10-01").mkdir(parents=True)
    (mirror / "Calibration" / "_quarantine" / "DARK" / "2026-10-01" / "c.fits").write_bytes(b"x")
    cl.compare_desktop(rep, mirror)
    assert rep["sets"][0]["desktop"] == {"present": 1, "pending": 1}
    assert rep["sets"][1]["desktop"] == {"present": 1, "pending": 0}
    assert rep["desktop"]["library_frames_present"] == 1
    assert rep["desktop"]["library_frames_pending"] == 1
    assert "1 still on their way (Syncthing)" in cl.format_report(rep)


def test_epoch_misses_assumes_the_rig_readout():
    ep = {"gain": 200, "offset": 256, "setpoint": 0.0, "readout": "HCG"}
    assert cl.epoch_misses({"gain": 200, "offset": 256, "settemp": 0.0, "readout": None}, ep) == []
    assert cl.epoch_misses({"gain": 200, "offset": 50, "settemp": -10.0, "readout": "LCG"}, ep) == [
        "offset 50 vs 256", "SET-TEMP -10 C vs setpoint 0 C", "readout LCG vs HCG"]


def test_library_report_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from photonscript.scheduler import app as app_mod
    cfg = _cfg(tmp_path)
    _cal(cfg, _day(3), "DARK", 2, 300.0)
    monkeypatch.setattr(app_mod, "_config", cfg)
    c = TestClient(app_mod.app)
    r = c.get("/api/calibration/library-report?rig=rc16")
    assert r.status_code == 200
    assert r.json()["totals"]["not_filed"] == 2
    assert c.get("/api/calibration/library-report?rig=nope").status_code == 404
