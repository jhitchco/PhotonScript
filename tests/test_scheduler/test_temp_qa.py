"""Cooler-failure subs (CCD-TEMP >> SET-TEMP) must fail QA in _fast_grade."""

import numpy as np
from astropy.io import fits

from photonscript.shared.config import PhotonScriptConfig
from photonscript.scheduler.runs import _fast_grade


def _fake_light(tmp_path, ccd_temp, set_temp=0.0):
    data = np.random.normal(300, 10, (256, 256)).astype(np.uint16)
    hdu = fits.PrimaryHDU(data)
    hdu.header["EXPTIME"] = 300.0
    hdu.header["SET-TEMP"] = set_temp
    hdu.header["CCD-TEMP"] = ccd_temp
    hdu.header["FILTER"] = "H"
    hdu.header["OBJECT"] = "Crescent Nebula"
    hdu.header["DATE-OBS"] = "2026-07-04T08:00:00"
    p = tmp_path / f"t_{ccd_temp}.fits"
    hdu.writeto(p)
    return p


def test_hot_sensor_rejected(tmp_path):
    cfg = PhotonScriptConfig(_env_file=None)
    r = _fast_grade(_fake_light(tmp_path, 39.3), cfg)
    assert not r["passed_qa"]
    assert "cooler failure" in r["reason"]


def test_at_setpoint_not_temp_flagged(tmp_path):
    cfg = PhotonScriptConfig(_env_file=None)
    r = _fast_grade(_fake_light(tmp_path, 0.2), cfg)
    assert "cooler failure" not in r["reason"]


def test_wrong_setpoint_in_header_still_rejected(tmp_path):
    """2026-09-26: camera left at SET-TEMP=20, sensor 23-25°C. Judged against
    the header these looked 'at setpoint' and passed; judged against the
    configured setpoint (0°C) they are cooler-failure subs."""
    cfg = PhotonScriptConfig(_env_file=None)
    r = _fast_grade(_fake_light(tmp_path, 23.4, set_temp=20.0), cfg)
    assert not r["passed_qa"]
    assert "cooler failure" in r["reason"] and "set to 20C" in r["reason"]


def test_sensor_temp_reasons_rules():
    from photonscript.scheduler.runs import sensor_temp_reasons
    cfg = PhotonScriptConfig(_env_file=None)          # setpoint 0, +5, ceiling 10
    assert sensor_temp_reasons(4.9, 0.0, cfg) == []
    assert "cooler failure" in sensor_temp_reasons(5.5, 0.0, cfg)[0]
    assert sensor_temp_reasons(None, None, cfg) == []
    # ceiling applies even when the setpoint itself is warm
    warm = PhotonScriptConfig(_env_file=None, camera_setpoint_c=8.0)
    assert sensor_temp_reasons(9.0, 8.0, warm) == []
    assert "above 10C limit" in sensor_temp_reasons(11.0, 8.0, warm)[0]
    # explicit rig setpoint (piggyback) wins over camera_setpoint_c
    assert sensor_temp_reasons(3.0, None, cfg, setpoint=-10.0)
