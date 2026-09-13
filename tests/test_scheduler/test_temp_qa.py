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
