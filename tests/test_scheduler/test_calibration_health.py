"""Camera-aware calibration health — the OSC's zero-calibration must be visible
instead of hidden behind the mono set."""
from pathlib import Path

import photonscript.scheduler.calibration as cal
from photonscript.shared.config import PhotonScriptConfig


def _mono_only_frames(config):
    # Only the mono AP26MC has calibration; the OSC AP26CC has none.
    yield ("BIAS", "2026-07-31", Path("bias_0.fits"))
    yield ("BIAS", "2026-07-31", Path("bias_1.fits"))
    yield ("DARK", "2026-09-08", Path("dark_0.fits"))
    yield ("FLAT", "2026-09-07", Path("flat_0.fits"))


def _fake_getheader(_path):
    # Every calibration frame in this fixture is the mono camera.
    return {"INSTRUME": "AP26MC", "FILTER": "H", "EXPTIME": 600.0}


def test_health_splits_by_camera_and_flags_missing_osc(monkeypatch):
    monkeypatch.setattr(cal, "iter_calibration_frames", _mono_only_frames)
    import astropy.io.fits as _f
    monkeypatch.setattr(_f, "getheader", _fake_getheader)

    out = cal.calibration_health(PhotonScriptConfig(piggyback_enabled=True))

    assert out["cameras"] == ["AP26MC"]              # only the mono has any
    assert set(out["by_camera"]["AP26MC"]) == {"BIAS", "DARK", "FLAT"}
    assert "AP26CC" not in out["by_camera"]          # OSC has none
    assert out["by_camera"]["AP26MC"]["BIAS"]["total"] == 2
    # dual-rig setup with a camera lacking calibration -> loud note
    assert "multi_camera_note" in out and "UNCALIBRATED" in out["multi_camera_note"]


def test_health_no_note_when_piggyback_disabled(monkeypatch):
    monkeypatch.setattr(cal, "iter_calibration_frames", _mono_only_frames)
    import astropy.io.fits as _f
    monkeypatch.setattr(_f, "getheader", _fake_getheader)
    out = cal.calibration_health(PhotonScriptConfig(piggyback_enabled=False))
    assert "multi_camera_note" not in out
    assert out["by_camera"]["AP26MC"]["DARK"]["latest"] == "2026-09-08"
