"""PS-36: Piggy-600 calibration is filed into the piggyback library subtree."""

from datetime import datetime, timedelta
from types import SimpleNamespace

from photonscript.scheduler import runs


def _cfg(tmp_path, enabled=True, watch=True):
    main_watch = tmp_path / "NINA"
    pb_watch = tmp_path / "NINA-Piggyback"
    lib = tmp_path / "Library"
    for p in (main_watch, pb_watch, lib):
        p.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        image_watch_dir=str(main_watch), library_dir=str(lib),
        data_dir=str(tmp_path), library_cal_days=120,
        piggyback_enabled=enabled,
        piggyback_image_watch_dir=str(pb_watch) if watch else "",
        piggyback_library_dir="")


def _night(root, d, typ, n):
    folder = root / d / typ
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (folder / f"{d}_{typ}_{i:03d}.fits").write_bytes(b"SIMPLE")


def test_piggyback_calibration_filed_into_its_subtree(tmp_path):
    cfg = _cfg(tmp_path)
    recent = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
    old = (datetime.now() - timedelta(days=400)).strftime("%Y-%m-%d")
    pb = tmp_path / "NINA-Piggyback"
    _night(pb, recent, "FLAT", 10)
    _night(pb, recent, "DARK", 3)
    _night(pb, recent, "BIAS", 2)
    _night(pb, old, "DARK", 4)  # beyond library_cal_days: ignored
    res = runs._build_piggyback_calibration(cfg)
    assert res["linked"] == 15
    base = tmp_path / "Library" / "piggyback" / "Calibration"
    assert len(list((base / "FLAT" / recent).glob("*.fits"))) == 10
    assert len(list((base / "DARK" / recent).glob("*.fits"))) == 3
    assert len(list((base / "BIAS" / recent).glob("*.fits"))) == 2
    assert not (base / "DARK" / old).exists()
    # idempotent
    assert runs._build_piggyback_calibration(cfg)["already_there"] == 15


def test_piggyback_calibration_skipped_when_disabled_or_unset(tmp_path):
    assert runs._build_piggyback_calibration(_cfg(tmp_path, enabled=False)) is None
    assert runs._build_piggyback_calibration(_cfg(tmp_path, watch=False)) is None


def test_rc16_calibration_still_goes_to_main_library(tmp_path):
    cfg = _cfg(tmp_path)
    recent = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")
    _night(tmp_path / "NINA", recent, "DARK", 2)
    n_l, _ = runs._link_calibration_night(cfg, tmp_path / "NINA",
                                          tmp_path / "Library", recent)
    assert n_l == 2
    assert len(list((tmp_path / "Library" / "Calibration" / "DARK" / recent)
                    .glob("*.fits"))) == 2
