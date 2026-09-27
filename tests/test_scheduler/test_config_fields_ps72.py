"""PS-72: the forced-calibration switch and the NINA AutoFocus reports folder
are editable from the System config page (POST /api/config)."""
from photonscript.scheduler.app import _CONFIG_FIELDS
from photonscript.shared.config import PhotonScriptConfig


def test_switches_exposed():
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    assert by_env["PS_GUIDING_FORCE_FIRST_CALIBRATION"][4] == "bool"
    assert by_env["PS_NINA_AUTOFOCUS_REPORTS_DIR"][4] == "str"
    for env in ("PS_GUIDING_FORCE_FIRST_CALIBRATION", "PS_NINA_AUTOFOCUS_REPORTS_DIR"):
        assert hasattr(PhotonScriptConfig(), by_env[env][0])


def test_force_calibration_default_off():
    assert PhotonScriptConfig().guiding_force_first_calibration is False


def test_lint_requires_forced_cal_only_when_configured(monkeypatch):
    from photonscript.scheduler.sequence_lint import _force_cal_wanted
    monkeypatch.setenv("PS_GUIDING_FORCE_FIRST_CALIBRATION", "true")
    assert _force_cal_wanted() is True
    monkeypatch.setenv("PS_GUIDING_FORCE_FIRST_CALIBRATION", "false")
    assert _force_cal_wanted() is False
