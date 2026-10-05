"""PS-122: the Calibration owed view. Lights from fixture subs logs, the
calibration library as a fixture PS-113 QA store (or synthetic FITS for the
header-scan fallback), all in tmp dirs. Also checks that the view and the
night dark quota (RC16 armer, Piggy-600 companion) count the same."""

import json
from datetime import datetime, timedelta

import pytest

from photonscript.scheduler import calibration_owed as co
from photonscript.scheduler import calibration_qa as cq
from photonscript.scheduler import runs
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (CelestialTarget, ExposurePlan,
                                        FilterType, ImagingProject)
from tests.test_scheduler.test_ps113_calibration import write_frame


def _ago(days):
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _cfg(tmp_path, **kw):
    base = dict(_env_file=None, data_dir=tmp_path / "data",
                library_dir=str(tmp_path / "lib"),
                image_watch_dir=str(tmp_path / "watch"),
                nina_logs_dir=str(tmp_path / "logs"))
    base.update(kw)
    return PhotonScriptConfig(**base)


def _pb_cfg(tmp_path, **kw):
    kw.setdefault("piggyback_dark_exposures", "120")
    return _cfg(tmp_path, piggyback_enabled=True,
                piggyback_image_watch_dir=str(tmp_path / "pbwatch"), **kw)


def _m31():
    return ImagingProject(
        id="m31", active=True,
        target=CelestialTarget(name="M 31", ra_hours=0.71, dec_degrees=41.27),
        exposure_plans=[ExposurePlan(filter_type=FilterType.OSC, exposure_seconds=400,
                                     count=40, rig="piggyback")])


def _heart():
    return ImagingProject(
        id="heart", active=True,
        target=CelestialTarget(name="Heart Nebula", ra_hours=2.56, dec_degrees=61.45),
        exposure_plans=[ExposurePlan(filter_type=FilterType("Ha"), exposure_seconds=600,
                                     count=30)])


def store_frames(cfg, rig, frames):
    """frames: (type, date, n, extra fields) -> a QA store of passed frames."""
    recs = {}
    for typ, date, n, extra in frames:
        for i in range(n):
            name = f"{typ.lower()}{extra.get('exptime', 0):g}_{extra.get('filter', '')}_{i}.fits"
            rec = {"type": typ, "date": date, "name": name, "verdict": "pass",
                   "gain": 100 if rig == "piggyback" else 200, "offset": 256,
                   "settemp": 0.0}
            rec.update(extra)
            recs[cq.frame_key(typ, date, name)] = rec
    cq.save_store(cfg, rig, {"version": cq.QA_VERSION, "rig": rig, "frames": recs})


def subs_log(cfg, night, recs):
    p = runs.runs_dir(cfg) / f"{night}_subs.jsonl"
    with p.open("a", encoding="utf-8") as fh:
        for i, r in enumerate(recs):
            base = {"file": f"{night}_{i}_{len(r)}.fits", "passed_qa": True}
            base.update(r)
            fh.write(json.dumps(base) + "\n")


def osc(exp, **kw):
    r = {"rig": "piggyback", "target": "M 31", "filter": "OSC", "exp_s": exp,
         "gain": 100, "offset": 256, "set_temp": 0.0, "xbin": 1}
    r.update(kw)
    return r


def _piggy_setup(tmp_path, **kw):
    cfg = _pb_cfg(tmp_path, **kw)
    store_frames(cfg, "piggyback", [
        ("DARK", _ago(11), 11, {"exptime": 120.0}),
        ("FLAT", _ago(14), 10, {"exptime": 2.0}),
        ("BIAS", _ago(11), 50, {"exptime": 0.001}),
    ])
    n1, n2 = _ago(6), _ago(5)
    subs_log(cfg, n1, [osc(400.0), osc(400.0), osc(300.0),
                       osc(600.0, passed_qa=False),            # rejected: no darks owed
                       osc(400.0, target="Somewhere Else")])   # not an active goal
    subs_log(cfg, n2, [osc(400.0), osc(120.0)])
    subs_log(cfg, _ago(90), [osc(900.0)])                       # outside the lookback
    return cfg, n1, n2


def _by_exp(r):
    return {d["exp_s"]: d for d in r["darks"]}


def test_piggy_owed_flags_400_not_in_the_quota_list(tmp_path):
    cfg, n1, n2 = _piggy_setup(tmp_path)
    rep = co.owed_report(cfg, "piggyback", projects=[_m31()])
    r = rep["rigs"][0]
    assert r["counted"] == "QA-passed"
    assert r["lights"] == 5 and r["lights_assumed_epoch"] == 0
    d = _by_exp(r)
    assert set(d) == {120.0, 300.0, 400.0}          # no 600 (rejected), no 900 (old)
    assert d[120.0]["have"] == 11 and d[120.0]["owed"] == 19
    assert d[120.0]["auto_fill"] and d[120.0]["fix"] is None
    assert d[400.0]["have"] == 0 and d[400.0]["owed"] == 30
    assert d[400.0]["lights"] == 3 and d[400.0]["nights"] == [n1, n2]
    assert not d[400.0]["in_quota_list"] and not d[400.0]["auto_fill"]
    assert "400 s not in piggyback_dark_exposures" in d[400.0]["fix"]
    fx = r["config_fixes"]
    assert len(fx) == 1 and fx[0]["set"] == "PS_PIGGYBACK_DARK_EXPOSURES=120,300,400"
    assert fx[0]["add"] == [300.0, 400.0]
    # flats: 10 of 25, lights shot since the last set
    f = r["flats"][0]
    assert f["filter"] == "OSC" and f["count"] == 10 and f["need"] == 25 and f["owed"]
    assert f["lights_since"] == [n1, n2] and not f["stale"]
    assert r["bias"]["owed"] is False and r["bias"]["count"] == 50
    # nights: n1 misses 300 s and 400 s darks; n2 misses 400 s
    nights = {n["night"]: n for n in r["nights"]}
    assert nights[n1]["uncalibrated"] and len(nights[n1]["missing"]) == 2
    assert any("120 s (11 of 30)" in x for x in nights[n2]["short"])
    s = r["summary"]
    assert s["dark_sets_owed"] == 3 and s["dark_frames_owed"] == 79
    assert s["config_fixes"] == 1 and s["uncalibrated_nights"] == 2
    assert s["flats_owed"] == 1 and s["bias_owed"] is False
    assert any("PS_PIGGYBACK_DARK_EXPOSURES=120,300,400" in t for t in r["items"])
    text = co.format_report(rep)
    assert "Piggy-600" in text and "uncalibrated" in text
    assert any("closed at night" in c for c in r["constraints"])
    assert any("light leak in the dome" in c for c in r["constraints"])
    assert any("safe (open) roof at dawn" in c for c in r["constraints"])


def test_owed_view_and_companion_quota_agree(tmp_path):
    from photonscript.scheduler.calibration import _osc_dark_blocks
    cfg, *_ = _piggy_setup(tmp_path, piggyback_dark_exposures="120,300,400")
    r = co.owed_report(cfg, "piggyback", projects=[_m31()])["rigs"][0]
    assert r["config_fixes"] == []
    d = _by_exp(r)
    assert all(d[e]["auto_fill"] for e in (120.0, 300.0, 400.0))
    names = sorted(b["Name"] for b in _osc_dark_blocks(cq.rig_view(cfg, "piggyback")))
    assert names == sorted(f"OSC DARKS_{e:.0f}s (need {d[e]['owed']} of 30)"
                           for e in (120.0, 300.0, 400.0))
    assert d[120.0]["owed"] == 19


def test_owed_view_and_rc16_armer_quota_agree(tmp_path, monkeypatch):
    from photonscript.scheduler import nina_sequence_json as nsj
    cfg = _cfg(tmp_path)
    store_frames(cfg, "rc16", [("DARK", _ago(1), 2, {"exptime": 600.0}),
                               ("DARK", _ago(1), 30, {"exptime": 180.0}),
                               # a failed frame never counts
                               ("DARK", _ago(1), 1, {"exptime": 300.0, "verdict": "fail"})])
    monkeypatch.setattr(nsj, "_gen_cfg", lambda: cfg)
    blocks = {b["Name"] for b in nsj._dark_quota_blocks("DawnProvider", 0)}
    d = _by_exp(co.owed_report(cfg, "rc16", projects=[])["rigs"][0])
    assert blocks == {f"DARKS_{e:.0f}s (need {d[e]['owed']} of 30)"
                      for e in d if d[e]["owed"]}
    assert d[600.0]["owed"] == 28 and d[180.0]["owed"] == 0 and d[300.0]["owed"] == 30


def test_rc16_off_epoch_stale_flats_and_tonight(tmp_path):
    cfg = _cfg(tmp_path)
    store_frames(cfg, "rc16", [
        ("DARK", _ago(1), 2, {"exptime": 600.0}),
        ("FLAT", _ago(28), 15, {"exptime": 3.0, "filter": "Ha"}),
        ("FLAT", _ago(93), 15, {"exptime": 1.0, "filter": "L"}),
    ])
    n1 = _ago(3)
    heart = {"rig": "rc16", "target": "Heart Nebula", "exp_s": 600.0, "set_temp": 0.0}
    subs_log(cfg, n1, [dict(heart, filter="Ha", gain=200, offset=256),
                       dict(heart, filter="Ha", gain=100, offset=256),   # off epoch
                       dict(heart, filter="L", exp_s=180.0),             # pre-PS-122 record
                       ])
    night = datetime.now().strftime("%Y-%m-%d")
    (runs.runs_dir(cfg) / f"{night}_plan.json").write_text(json.dumps({
        "night_of": night, "targets": [{"name": "Heart Nebula", "exposures": [
            {"filter": "OIII", "exp_s": 60.0, "planned": 10}]}]}))
    r = co.owed_report(cfg, "rc16", projects=[_heart()])["rigs"][0]
    assert r["lights_assumed_epoch"] == 1
    d = {(x["exp_s"], x["gain"]): x for x in r["darks"]}
    assert d[(600.0, 200)]["have"] == 2 and d[(600.0, 200)]["owed"] == 28
    off = d[(600.0, 100)]
    assert not off["on_epoch"] and "off-epoch" in off["fix"] and off["owed"] == 30
    assert d[(180.0, 200)]["assumed_epoch"]
    plan = d[(60.0, 200)]
    assert plan["planned"] and plan["lights"] == 0 and plan["owed"] == 30
    assert r["config_fixes"][0]["set"] == "PS_DARK_EXPOSURES=60,180,600"
    flats = {f["filter"]: f for f in r["flats"]}
    assert set(flats) == {"Ha", "L", "OIII"}
    assert not flats["Ha"]["owed"] and flats["Ha"]["age_days"] == 28
    assert flats["L"]["stale"] and flats["L"]["owed"] and flats["L"]["lights_since"] == [n1]
    assert flats["OIII"]["last"] is None and flats["OIII"]["owed"]
    assert r["bias"]["owed"] and r["bias"]["reasons"] == ["no bias"]
    assert r["nights"][0]["uncalibrated"] and "bias" in r["nights"][0]["missing"]


def test_header_scan_when_no_qa_store(tmp_path):
    cfg = _pb_cfg(tmp_path)
    lib = runs.library_root(cq.rig_view(cfg, "piggyback"))
    for i in range(2):
        write_frame(lib / "Calibration" / "DARK" / _ago(2) / f"d{i}.fits",
                    exp=120.0, seed=i)
    r = co.owed_report(cfg, "piggyback", projects=[_m31()])["rigs"][0]
    assert r["counted"].startswith("header scan")
    assert _by_exp(r)[120.0]["have"] == 2
    assert r["flats"][0]["last"] is None and r["bias"]["owed"]


def test_no_active_goals_only_quota_list(tmp_path):
    cfg, *_ = _piggy_setup(tmp_path)
    r = co.owed_report(cfg, "piggyback", projects=[])["rigs"][0]
    assert r["lights"] == 0 and r["nights"] == [] and r["config_fixes"] == []
    assert set(_by_exp(r)) == {120.0}


def test_api_endpoint(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.scheduler import calibration_plan as cp
    from photonscript.scheduler.routers import calibration as rc
    cfg, *_ = _piggy_setup(tmp_path)
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(cp, "load_projects", lambda c: [_m31()])
    body = rc.api_calibration_owed("piggyback")
    assert body["rigs"][0]["rig"] == "piggyback" and body["total_items"] >= 3
    assert rc.api_calibration_owed("nope").status_code == 404
    both = rc.api_calibration_owed("")
    assert [r["rig"] for r in both["rigs"]] == ["rc16", "piggyback"]


def test_light_epoch_fields():
    from photonscript.shared.rigs import light_epoch_fields
    assert light_epoch_fields({"GAIN": 100, "OFFSET": 256.0, "XBINNING": 1,
                               "READOUTM": "High Gain "}) == {
        "gain": 100, "offset": 256, "xbin": 1, "readout": "High Gain"}
    assert light_epoch_fields({}) == {"gain": None, "offset": None, "xbin": None,
                                      "readout": None}
    assert light_epoch_fields(None)["gain"] is None


def test_config_default_and_field():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    c = PhotonScriptConfig(_env_file=None)
    assert c.calibration_owed_lookback_days == 60
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    f = by_env["PS_CALIBRATION_OWED_LOOKBACK_DAYS"]
    assert f[0] == "calibration_owed_lookback_days" and f[4] == "int"


@pytest.mark.parametrize("days,expect", [(60, {120.0, 300.0, 400.0}),
                                         (120, {120.0, 300.0, 400.0, 900.0})])
def test_lookback_window(tmp_path, days, expect):
    cfg, *_ = _piggy_setup(tmp_path, calibration_owed_lookback_days=days)
    r = co.owed_report(cfg, "piggyback", projects=[_m31()])["rigs"][0]
    assert set(_by_exp(r)) == expect


def test_dashboard_owed_badge_wiring():
    """The main dashboard shows an owed count badge linking to /calibration,
    fed by /api/calibration/owed on a slow (5 min) refresh. ASCII only."""
    from pathlib import Path
    p = (Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
         / "templates" / "dashboard.html")
    s = p.read_text(encoding="utf-8")
    i = s.index("PS-122: \"Calibration owed\" badge")
    s[i:s.index("<!-- Equipment: one pane", i)].encode("ascii")   # markup
    k = s.index("async function refreshCalOwed")
    s[k:s.index("setInterval(refreshCalOwed", k)].encode("ascii")  # script
    assert 'id="calOwedBadge"' in s and 'href="/calibration"' in s
    assert "fetch('/api/calibration/owed')" in s
    assert "setInterval(refreshCalOwed, 300000)" in s
