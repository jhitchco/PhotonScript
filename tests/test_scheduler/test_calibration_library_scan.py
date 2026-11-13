"""Calibration discovery must see BOTH the NINA output dir and the
librarian tree.

Regression: once frames are pruned from image_watch_dir, the hardlinked
copies under Library/Calibration/<TYPE>/<DATE>/ were the only survivors,
but calibration_health() only walked the watch dir and reported
"none on disk" — which also mis-drove the bias gate and dark counting.
"""

from datetime import datetime, timedelta

import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.scheduler.calibration import (
    calibration_health, days_since_last_bias, iter_calibration_frames,
)


def _d(days_ago):
    return (datetime.now().date() - timedelta(days=days_ago)).strftime("%Y-%m-%d")


def _watch_frame(watch, date, typ, name="f_0001.fits"):
    """NINA layout: <watch>/<date>/<TYPE>/file.fits"""
    p = watch / date / typ
    p.mkdir(parents=True, exist_ok=True)
    (p / name).write_bytes(b"\x00")
    return p / name


def _lib_frame(lib, date, typ, name="f_0001.fits"):
    """Librarian layout: <lib>/Calibration/<TYPE>/<date>/file.fits"""
    p = lib / "Calibration" / typ / date
    p.mkdir(parents=True, exist_ok=True)
    (p / name).write_bytes(b"\x00")
    return p / name


@pytest.fixture
def cfg(tmp_path):
    watch = tmp_path / "nina"
    lib = tmp_path / "library"
    watch.mkdir()
    lib.mkdir()
    return PhotonScriptConfig(image_watch_dir=str(watch),
                              library_dir=str(lib)), watch, lib


def test_library_only_frames_are_found(cfg):
    """The exact production failure: watch dir pruned, library intact."""
    config, _watch, lib = cfg
    _lib_frame(lib, _d(10), "BIAS")
    _lib_frame(lib, _d(10), "DARK")
    _lib_frame(lib, _d(10), "FLAT")

    health = calibration_health(config)
    for typ in ("BIAS", "DARK", "FLAT"):
        assert health[typ]["total"] == 1, f"{typ} not seen in library"
        assert health[typ]["latest"] == _d(10)
        assert health[typ].get("note") != "none on disk"


def test_watch_dir_only_still_works(cfg):
    config, watch, _lib = cfg
    _watch_frame(watch, _d(3), "BIAS")
    health = calibration_health(config)
    assert health["BIAS"]["total"] == 1
    assert health["BIAS"]["latest"] == _d(3)


def test_hardlinked_frame_counted_once(cfg):
    """Library entries are hardlinks of watch files — don't double-count."""
    config, watch, lib = cfg
    _watch_frame(watch, _d(5), "BIAS", "bias_0001.fits")
    _lib_frame(lib, _d(5), "BIAS", "bias_0001.fits")
    assert calibration_health(config)["BIAS"]["total"] == 1


def test_distinct_frames_from_both_roots_are_summed(cfg):
    config, watch, lib = cfg
    _watch_frame(watch, _d(5), "DARK", "dark_0001.fits")
    _lib_frame(lib, _d(9), "DARK", "dark_0002.fits")
    health = calibration_health(config)
    assert health["DARK"]["total"] == 2
    assert health["DARK"]["latest"] == _d(5)  # newest of the two


def test_plural_type_dirs_normalise(cfg):
    config, _watch, lib = cfg
    _lib_frame(lib, _d(2), "DARKS")
    _lib_frame(lib, _d(2), "FLATS")
    health = calibration_health(config)
    assert health["DARK"]["total"] == 1
    assert health["FLAT"]["total"] == 1


def test_bias_not_mangled_to_bia(cfg):
    """rstrip('S') on 'BIAS' would yield 'BIA' — must stay 'BIAS'."""
    config, _watch, lib = cfg
    _lib_frame(lib, _d(1), "BIAS")
    assert {t for t, _, _ in iter_calibration_frames(config)} == {"BIAS"}


def test_days_since_last_bias_sees_library(cfg):
    config, _watch, lib = cfg
    _lib_frame(lib, _d(7), "BIAS")
    assert days_since_last_bias(config) == 7


def test_days_since_last_bias_prefers_newest_across_roots(cfg):
    config, watch, lib = cfg
    _lib_frame(lib, _d(40), "BIAS", "old.fits")
    _watch_frame(watch, _d(4), "BIAS", "new.fits")
    assert days_since_last_bias(config) == 4


def test_empty_everywhere_reports_none_on_disk(cfg):
    config, _watch, _lib = cfg
    health = calibration_health(config)
    for typ in ("BIAS", "DARK", "FLAT"):
        assert health[typ]["total"] == 0
        assert health[typ]["note"] == "none on disk"
        assert health[typ]["stale"] is True


def test_missing_library_dir_does_not_crash(tmp_path):
    watch = tmp_path / "nina"
    watch.mkdir()
    _watch_frame(watch, _d(2), "BIAS")
    config = PhotonScriptConfig(image_watch_dir=str(watch),
                                library_dir=str(tmp_path / "nope"))
    assert calibration_health(config)["BIAS"]["total"] == 1


def test_snapshot_dir_ignored(cfg):
    config, watch, _lib = cfg
    _watch_frame(watch, _d(2), "SNAPSHOT")
    health = calibration_health(config)
    assert all(health[t]["total"] == 0 for t in ("BIAS", "DARK", "FLAT"))
