"""Tests for the FITS OBJECT-stamping helper."""

import numpy as np
import pytest

fits = pytest.importorskip("astropy.io.fits")

from photonscript.shared.fits_object import header_object, stamp_object


def _write(path, obj=None):
    hdu = fits.PrimaryHDU(np.zeros((4, 4), dtype=np.uint16))
    if obj is not None:
        hdu.header["OBJECT"] = obj
    hdu.writeto(path)
    return str(path)


def test_stamp_fills_blank_object(tmp_path):
    p = _write(tmp_path / "a.fits")
    assert header_object(p) == ""
    assert stamp_object(p, "NGC 7331 P11") is True
    assert header_object(p) == "NGC 7331 P11"


def test_stamp_does_not_overwrite_existing(tmp_path):
    p = _write(tmp_path / "b.fits", obj="M51")
    assert stamp_object(p, "Crescent Nebula") is False
    assert header_object(p) == "M51"


def test_overwrite_flag(tmp_path):
    p = _write(tmp_path / "c.fits", obj="M51")
    assert stamp_object(p, "Crescent Nebula", overwrite=True) is True
    assert header_object(p) == "Crescent Nebula"


@pytest.mark.parametrize("name", ["", "?", "   "])
def test_blank_or_unknown_name_is_noop(tmp_path, name):
    p = _write(tmp_path / "d.fits")
    assert stamp_object(p, name) is False
    assert header_object(p) == ""


def test_missing_file_is_safe(tmp_path):
    assert stamp_object(tmp_path / "nope.fits", "X") is False
