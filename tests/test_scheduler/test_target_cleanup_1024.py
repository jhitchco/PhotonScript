"""2026-10-24 target cleanup: the '?' Targets card and the TPoint mapping
cards.

* sub_index leaves out records filed under a calibration folder (110 bias /
  darks of 2026-04-06 sat in the '?' card) and makes no card for a
  calibration-run sub (PS-171 "TPoint point 01/45 alt 38.1 az 72.6 (east)").
* ingest marks TPoint mapping / autocal frames test + calibration_run.
* the dawn timeline names a Piggy-600 sub that rode along a TPoint run
  "TPoint mapping" (it stayed '?' on 2026-10-09) instead of leaving it '?'.
* build_library never files a calibration-run sub under a target folder.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import dawn_autofile as da
from photonscript.scheduler import runs, sub_index
from photonscript.shared import qa_rules
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.target_names import (calibration_run_name,
                                              is_calibration_run_target,
                                              is_test_target)

NIGHT = "2026-10-09"
P1 = "TPoint point 01/45 alt 38.1 az 72.6 (east)"


def _cfg(tmp_path, **kw):
    (tmp_path / "data").mkdir(exist_ok=True)
    base = dict(_env_file=None, data_dir=str(tmp_path / "data"),
                image_watch_dir=str(tmp_path / "fits"),
                library_dir=str(tmp_path / "lib"),
                nina_logs_dir=str(tmp_path / "logs"),
                stamp_fits_object=False, observatory_tz="UTC",
                piggyback_enabled=True)
    base.update(kw)
    return PhotonScriptConfig(**base)


def _write(cfg, rows, date=NIGHT):
    with open(runs.runs_dir(cfg) / f"{date}_subs.jsonl", "w",
              encoding="utf-8") as f:
        f.write("".join(json.dumps(r) + "\n" for r in rows))


def _row(target, file, *, rig="rc16", ok=True, reviewed=False, **kw):
    r = {"rig": rig, "file": file, "abs_path": f"C:/NINA/{NIGHT}/{file}",
         "time": f"{NIGHT}T03:00:00", "target": target, "filter": "L",
         "exp_s": 5, "passed_qa": ok, "reviewed": reviewed, "reason": ""}
    r.update(kw)
    return r


# ------------------------------------------------------------------ names

@pytest.mark.parametrize("name", [
    P1, "TPoint point 45/45 alt 70.0 az 300.0 (west)",
    "TPoint mapping 45 points", "TPoint mapping",
    "TPoint mapping 45 points_Container", "TPoint autocal 1"])
def test_calibration_run_names(name):
    assert is_calibration_run_target(name)
    assert calibration_run_name(name) == "TPoint mapping"
    assert qa_rules.is_test_record({"target": name})


@pytest.mark.parametrize("name", [
    "NGC 604", "Tracking test h Persei Cluster", "Focus calibration NGC 7789",
    "?", "", None, "Andromeda Galaxy"])
def test_not_calibration_runs(name):
    assert not is_calibration_run_target(name)
    assert calibration_run_name(name) is None


def test_tracking_tests_stay_tests_not_runs():
    assert is_test_target("Tracking test NGC 6934")
    assert qa_rules.calibration_run_fields("Tracking test NGC 6934") == {}


def test_ingest_fields():
    assert qa_rules.calibration_run_fields(P1) == {"test": True,
                                                   "calibration_run": True}
    assert qa_rules.calibration_run_fields("NGC 604") == {}
    assert qa_rules.is_calibration_run_record({"target": "?",
                                               "calibration_run": True})


# ------------------------------------------------------------------ Targets

def test_targets_skip_calibration_folders_and_tpoint_cards(tmp_path):
    cfg = _cfg(tmp_path)
    _write(cfg, [
        _row(P1, "LIGHT/tp1.fits"),
        _row("TPoint point 02/45 alt 32.4 az 52.5 (east)", "LIGHT/tp2.fits",
             ok=False),
        _row("?", "LIGHT/osc1.fits", rig="piggyback", calibration_run=True),
        _row("?", "BIAS/b1.fits", ok=False),
        _row("?", "DARK\\d1.fits", ok=False),
        _row("?", "LIGHT/osc2.fits", rig="piggyback"),
        _row("NGC 604", "LIGHT/n1.fits", reviewed=True),
    ])
    rows = sub_index.all_rows(cfg, [])
    files = {r["file"] for r in rows}
    assert "BIAS/b1.fits" not in files and "DARK/d1.fits" not in files
    assert {r["file"] for r in rows if r["calibration_run"]} == {
        "LIGHT/tp1.fits", "LIGHT/tp2.fits", "LIGHT/osc1.fits"}
    cards = {c["name"]: c for c in sub_index.targets(cfg, [])}
    assert not any(n.startswith("TPoint") for n in cards)
    assert cards["?"]["counts"]["total"] == 1         # osc2 only
    assert cards["NGC 604"]["counts"]["approved"] == 1
    # the night itself still lists every light (the runs page reviews them)
    assert len(runs._load_subs(cfg, NIGHT)) == 7


# ------------------------------------------------------------------ dawn

def _t(hhmm, sec=0):
    return datetime.fromisoformat(f"{NIGHT}T{hhmm}:00") + timedelta(seconds=sec)


def _tpoint_night(cfg):
    """45 s per point: a 25 s slew then a 5 s frame (2026-10-09 shape), and
    Piggy-600 450 s subs riding along."""
    recs, mount = [], []
    t = _t("02:47")
    for i in range(1, 21):
        mount.append((t, "slewing"))
        mount.append((t + timedelta(seconds=25), "tracking"))
        st = t + timedelta(seconds=30)
        recs.append({"rig": "rc16", "file": f"LIGHT/tp{i:02d}.fits",
                     "time": st.isoformat(), "target":
                     f"TPoint point {i:02d}/45 alt 40.0 az {i * 15}.0 (east)",
                     "filter": "L", "exp_s": 5, "passed_qa": True,
                     "reason": ""})
        t += timedelta(seconds=45)
    end_run = t
    mount.append((end_run, "parked"))
    for k in range(2):
        st = _t("02:48") + timedelta(seconds=k * 460)
        en = st + timedelta(seconds=450)
        recs.append({"rig": "piggyback", "file": f"LIGHT/osc{k}.fits",
                     "time": en.isoformat() + "Z", "target": "?",
                     "filter": "OSC", "exp_s": 450, "passed_qa": True,
                     "reason": ""})
    from photonscript.shared.mount_log import log_path
    p = log_path(cfg, NIGHT)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps({
        "t": tt.isoformat() + "Z", "rig": "rc16", "ra": 30.0, "dec": 40.0,
        "slewing": s == "slewing", "tracking": s == "tracking",
        "parked": s == "parked", "pier": "East", "why": "start"})
        for tt, s in mount) + "\n", encoding="utf-8")
    return recs


def test_piggy_sub_riding_a_tpoint_run_is_named_for_the_run(tmp_path):
    cfg = _cfg(tmp_path)
    subs = _tpoint_night(cfg)
    out = da.attribute_timeline_records(cfg, NIGHT, subs)
    pig = [s for s in subs if s["rig"] == "piggyback"]
    assert out["named"] == 2 and out["by_target"] == {"TPoint mapping": 2}
    for s in pig:
        assert s["target"] == "TPoint mapping"
        assert s["test"] and s["calibration_run"]
        assert s["target_raw"] == "?" and s["timeline"]["calibration_run"]
    # never auto-approved at dawn, never a Targets card
    assert all(qa_rules.is_test_record(s) for s in pig)


def test_a_real_target_still_wins_over_a_run(tmp_path):
    cfg = _cfg(tmp_path)
    subs = _tpoint_night(cfg)
    # the RC16 images NGC 604 over the second Piggy sub instead
    st = _t("02:48") + timedelta(seconds=460)
    subs = [s for s in subs if not (s["rig"] == "rc16"
                                    and s["time"] >= st.isoformat())]
    subs.append({"rig": "rc16", "file": "LIGHT/n604.fits",
                 "time": st.isoformat(), "target": "NGC 604", "filter": "L",
                 "exp_s": 450, "passed_qa": True, "reason": ""})
    da.attribute_timeline_records(cfg, NIGHT, subs)
    names = sorted(s["target"] for s in subs if s["rig"] == "piggyback")
    assert names == ["NGC 604", "TPoint mapping"]


# ------------------------------------------------------------------ Library

def test_build_library_never_files_a_calibration_run_sub(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, library_attribute=False)
    monkeypatch.setattr(runs, "sync_goal_progress", lambda c: [])
    night_dir = Path(cfg.image_watch_dir) / NIGHT / "LIGHT"
    night_dir.mkdir(parents=True)
    recs = []
    for name, tgt in (("tp.fits", P1), ("n.fits", "NGC 604")):
        f = night_dir / name
        f.write_bytes(b"SIMPLE  =                    T")
        recs.append(_row(tgt, f"LIGHT/{name}", reviewed=True,
                         abs_path=str(f)))
    _write(cfg, recs)
    runs.build_library(cfg, NIGHT)
    lib = runs.library_root(cfg)
    assert (lib / "NGC 604" / "L" / "n.fits").exists()
    assert not any("TPoint" in str(p) for p in lib.rglob("*.fits"))
