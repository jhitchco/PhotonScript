"""PS-164: focuser position and rotator angle on subs and flats, and the
calibration coverage (PS-160) judging flats against focus changes.

NINA writes FOCPOS on every frame. Subs records and the calibration QA
store's flat records now carry it (and a rotator angle when a rotator is
connected); flats whose lights moved more than calibration_flats_focus_steps
(or calibration_flats_rotator_deg) away are owed, next to the manual
calibration_flats_reset.
"""
from datetime import datetime

import numpy as np

from photonscript.scheduler import calibration
from photonscript.scheduler import calibration_owed as co
from photonscript.scheduler import calibration_qa as cq
from photonscript.shared.optics_state import angle_diff, optics_fields
from tests.test_scheduler.test_ps122_calibration_owed import (
    _ago, _cfg, _m31, _pb_cfg, osc, store_frames, subs_log)


def test_optics_fields_from_header():
    assert optics_fields({"FOCPOS": 11102, "ROTATOR": 12.345}) == {
        "focpos": 11102, "rotator_deg": 12.35}
    assert optics_fields({"FOCUSPOS": "5853.4"}) == {"focpos": 5853,
                                                     "rotator_deg": None}
    assert optics_fields({}) == {"focpos": None, "rotator_deg": None}
    assert optics_fields(None) == {"focpos": None, "rotator_deg": None}
    assert optics_fields({"FOCPOS": "", "ROTATANG": "bad"}) == {
        "focpos": None, "rotator_deg": None}


def test_angle_diff_wraps():
    assert angle_diff(359.0, 1.0) == 2.0
    assert angle_diff(10.0, 40.0) == 30.0


def test_header_fields_and_header_only_backfill(tmp_path):
    from astropy.io import fits
    p = tmp_path / "flat.fits"
    hdu = fits.PrimaryHDU(np.zeros((4, 4), dtype=np.uint16))
    hdu.header["IMAGETYP"] = "FLAT"
    hdu.header["FOCPOS"] = 6040
    hdu.writeto(p)
    hf = cq.header_fields(fits.getheader(p))
    assert hf["focpos"] == 6040 and hf["rotator_deg"] is None
    out = cq._read_readouts([("k", p)])["k"]
    assert out["focpos"] == 6040 and "readout" in out
    # an unreadable file still records the keys (no re-read every pass)
    bad = cq._read_readouts([("x", tmp_path / "missing.fits")])["x"]
    assert bad["focpos"] is None and "rotator_deg" in bad


def test_flat_session_optics_median(tmp_path):
    cfg = _pb_cfg(tmp_path)
    store_frames(cfg, "piggyback", [
        ("FLAT", _ago(14), 3, {"exptime": 2.0, "focpos": 11000}),
        ("FLAT", _ago(14), 2, {"exptime": 2.5, "focpos": 11010}),
        ("FLAT", _ago(30), 2, {"exptime": 2.0}),
    ])
    opt = cq.flat_session_optics(cq.rig_view(cfg, "piggyback"), "piggyback")
    assert opt["OSC"][_ago(14)]["focpos"] == 11000
    assert opt["OSC"][_ago(14)]["n"] == 5
    assert opt["OSC"][_ago(30)]["focpos"] is None


def _setup(tmp_path, light_fp, **kw):
    cfg = _pb_cfg(tmp_path, **kw)
    store_frames(cfg, "piggyback", [
        ("DARK", _ago(11), 30, {"exptime": 400.0}),
        ("FLAT", _ago(14), 25, {"exptime": 2.0, "focpos": 11000,
                                "rotator_deg": 10.0}),
        ("BIAS", _ago(11), 50, {"exptime": 0.001}),
    ])
    subs_log(cfg, _ago(5), [osc(400.0, focpos=fp, rotator_deg=10.0)
                            for fp in light_fp])
    return cfg


def _osc_flat(cfg):
    r = co.owed_report(cfg, "piggyback", projects=[_m31()])["rigs"][0]
    return r, r["flats"][0]


def test_focus_moved_past_threshold_owes_flats(tmp_path):
    cfg = _setup(tmp_path, [11050, 11400, 11420],
                 calibration_flats_focus_steps="piggyback:300")
    r, f = _osc_flat(cfg)
    assert f["owed"] and f["stale"]
    assert any("focus moved: 2 light(s) more than 300 steps" in x
               for x in f["reasons"])
    assert f["focus"]["flat_focpos"] == 11000
    assert f["focus"]["max_shift"] == 420
    assert f["focus"]["lights_beyond"] == 2
    assert "flats OSC" in (co.morning_note(cfg, {"rigs": [r]}) or "")


def test_focus_within_threshold_or_off_is_not_owed(tmp_path):
    cfg = _setup(tmp_path, [11050, 11100],
                 calibration_flats_focus_steps="piggyback:300")
    _, f = _osc_flat(cfg)
    assert not f["owed"] and f["focus"]["max_shift"] == 100
    # default: rc16 only, so the Piggy-600 is not judged (info still shown)
    off = _setup(tmp_path / "b", [12000])
    _, f = _osc_flat(off)
    assert not f["owed"] and f["focus"]["threshold"] is None
    assert f["focus"]["max_shift"] == 1000


def test_lights_without_focpos_are_not_judged(tmp_path):
    cfg = _setup(tmp_path, [None, None],
                 calibration_flats_focus_steps="piggyback:10")
    _, f = _osc_flat(cfg)
    assert not f["owed"] and f["focus"] is None


def test_rotator_change_owes_flats(tmp_path):
    cfg = _pb_cfg(tmp_path, calibration_flats_rotator_deg=1.0)
    store_frames(cfg, "piggyback", [
        ("FLAT", _ago(14), 25, {"exptime": 2.0, "rotator_deg": 359.5})])
    subs_log(cfg, _ago(5), [osc(400.0, rotator_deg=0.2),
                            osc(400.0, rotator_deg=3.0)])
    _, f = _osc_flat(cfg)
    assert f["rotator"]["lights_beyond"] == 1
    assert any("rotator moved" in x for x in f["reasons"])


def test_focus_steps_parsing(tmp_path):
    cfg = _cfg(tmp_path, calibration_flats_focus_steps="rc16:800, piggy:150")
    assert co.flats_focus_steps(cfg, "rc16") == 800
    assert co.flats_focus_steps(cfg, "piggyback") == 150
    assert co.flats_focus_steps(_cfg(tmp_path), "rc16") == 1000    # default
    assert co.flats_focus_steps(_cfg(tmp_path), "piggyback") is None
    assert co.flats_focus_steps(
        _cfg(tmp_path, calibration_flats_focus_steps="0"), "rc16") is None
    assert co.flats_focus_steps(
        _cfg(tmp_path, calibration_flats_focus_steps="250"), "piggyback") == 250
    assert co.flats_rotator_deg(_cfg(tmp_path, calibration_flats_rotator_deg=0)) is None


def test_optics_moved_filters_rc16(tmp_path, monkeypatch):
    from photonscript.scheduler import calibration_plan as cp
    from photonscript.shared.models import (CelestialTarget, ExposurePlan,
                                            FilterType, ImagingProject)
    heart = ImagingProject(
        id="heart", active=True,
        target=CelestialTarget(name="Heart Nebula", ra_hours=2.56,
                               dec_degrees=61.45),
        exposure_plans=[ExposurePlan(filter_type=FilterType("Ha"),
                                     exposure_seconds=600, count=30)])
    monkeypatch.setattr(cp, "load_projects", lambda c: [heart])
    cfg = _cfg(tmp_path, calibration_flats_focus_steps="rc16:200")
    store_frames(cfg, "rc16", [
        ("FLAT", _ago(10), 15, {"exptime": 3.0, "filter": "Ha", "focpos": 5800}),
        ("FLAT", _ago(10), 15, {"exptime": 1.0, "filter": "L", "focpos": 5900})])
    subs_log(cfg, _ago(3), [
        {"rig": "rc16", "target": "Heart Nebula", "filter": "Ha",
         "exp_s": 600.0, "gain": 200, "offset": 256, "set_temp": 0.0,
         "focpos": 6100},
        {"rig": "rc16", "target": "Heart Nebula", "filter": "L",
         "exp_s": 600.0, "gain": 200, "offset": 256, "set_temp": 0.0,
         "focpos": 5950}])
    assert co.optics_moved_filters(cfg, "rc16") == {"Ha"}
    off = _cfg(tmp_path, calibration_flats_focus_steps="0",
               calibration_flats_rotator_deg=0)
    assert co.optics_moved_filters(off, "rc16") == set()


def test_stale_flat_filters_adds_focus_moved(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, calibration_flats_as_used=False)
    bb = {f: {"date": _ago(5), "age_days": 5}
          for f in ("Ha", "OIII", "SII", "R", "G", "B", "L")}
    monkeypatch.setattr(calibration, "calibration_health",
                        lambda c: {"FLAT": {"by_bucket": bb}})
    monkeypatch.setattr(co, "optics_moved_filters", lambda c, rig="rc16": {"OIII"})
    assert calibration.stale_flat_filters(cfg) == ["OIII"]

    def boom(c, rig="rc16"):
        raise RuntimeError("store unreadable")
    monkeypatch.setattr(co, "optics_moved_filters", boom)
    assert calibration.stale_flat_filters(cfg) == []


def test_collect_lights_carries_optics(tmp_path):
    cfg = _setup(tmp_path, [11111])
    lights = co.collect_lights(cfg, "piggyback", [_m31()], days=60,
                               now=datetime.now())
    assert lights[0]["focpos"] == 11111 and lights[0]["rotator_deg"] == 10.0


def test_config_keys_on_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    keys = {f[0] for f in _CONFIG_FIELDS}
    assert {"calibration_flats_focus_steps",
            "calibration_flats_rotator_deg"} <= keys
