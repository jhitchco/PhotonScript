"""Legacy import: --flat-temp-tol lets short sky flats shot uncooled (the
Nov 2025 RC16 dawn flats at +10..15 C for -10 C lights) calibrate the
lights; the default 5 C keeps excluding them, as before."""
import re

import numpy as np
import pytest

from photonscript.integration import pipeline as pl


def _fits(path, kind, exp, filt="L", temp=-10.0, minute=0):
    from astropy.io import fits
    h = fits.Header()
    h["IMAGETYP"] = kind
    h["EXPTIME"] = exp
    h["GAIN"] = 100
    h["OFFSET"] = 256
    h["SET-TEMP"] = temp
    h["READOUTM"] = "Low Conversion Gain"
    h["INSTRUME"] = "AP26MC"
    h["XBINNING"] = 1
    if filt:
        h["FILTER"] = filt
    h["DATE-LOC"] = f"2025-11-05T21:{minute:02d}:00"
    h["DATE-OBS"] = f"2025-11-06T04:{minute:02d}:00"
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(np.zeros((8, 8), np.uint16), header=h).writeto(path)


@pytest.fixture
def legacy_lib(tmp_path):
    L = tmp_path / "Library"
    for i in range(4):
        _fits(L / "M 27" / "L" / f"2025-11-05_21-{i:02d}-00_L_-10.00_45.00s_{i:04d}.fits", "LIGHT", 45.0,
              minute=i)
    for i in range(6):
        _fits(L / "Calibration" / "FLAT" / "2025-11-05" / f"flat_L_{i:02d}.fits", "FLAT", 0.04,
              temp=11.6, minute=30 + i)
    for i in range(5):
        _fits(L / "Calibration" / "BIAS" / "2025-11-05" / f"bias_{i:02d}.fits", "BIAS", 0.0, filt="",
              minute=40 + i)
    return L


def _opts(lib, tmp_path, **kw):
    o = pl.Options(target="M27", rig="rc16", library=lib, staging_root=tmp_path / "Staging",
                   qa="off", run_pixinsight=False, flats=True, default_readout="HCG",
                   allow_uncalibrated=True, dry_run=True,
                   pixinsight=str(tmp_path / "PI" / "bin" / "PixInsight.exe"))
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def test_default_tolerance_still_excludes_uncooled_flats(legacy_lib, tmp_path):
    res = pl.run(_opts(legacy_lib, tmp_path), echo=lambda s: None)
    fl = {f["filter"]: f for f in res["calibration"]["flats"]}
    assert fl["L"]["n"] == 0 and "uncooled flats" in fl["L"]["note"]
    assert res["calibration"]["bias"] == 5


def test_wider_tolerance_matches_the_dawn_flats(legacy_lib, tmp_path):
    res = pl.run(_opts(legacy_lib, tmp_path, flat_temp_tol=30.0), echo=lambda s: None)
    fl = {f["filter"]: f for f in res["calibration"]["flats"]}
    assert fl["L"]["n"] == 6 and fl["L"]["session"] == "2025-11-05"
    assert res["lights_staged"] == 4


def test_cli_integrate_has_flat_temp_tol():
    from typer.testing import CliRunner
    from photonscript.cli import app
    r = CliRunner().invoke(app, ["integrate", "--help"])
    assert r.exit_code == 0 and "--flat-temp-tol" in re.sub(r"\x1b\[[0-9;]*m", "", r.output)
