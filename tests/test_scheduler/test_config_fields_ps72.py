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


def test_ps72_rule_waived_when_the_ps93_slot_calibrates(monkeypatch):
    """PS-93: a PHD2_CALIBRATION slot replaces the forced first-target
    calibration, so the PS-72 lint rule only applies without one."""
    import json
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    from photonscript.scheduler.nina_sequence_json import generate_nina_json
    from photonscript.scheduler.sequence_lint import lint
    from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget
    monkeypatch.setenv("PS_GUIDING_FORCE_FIRST_CALIBRATION", "true")
    t = NinaSequenceTarget(name="M51", ra_hours=13.498, dec_degrees=47.2,
                           start_guiding=True, exposures=[ExposurePlan(
                               filter_type=FilterType.LUMINANCE, exposure_seconds=180,
                               count=30, gain=200, offset=50)])
    data = json.loads(generate_nina_json(build_sequence_for_night("x", [t]), cal_field={
        "name": "M67", "ra_hours": 8.855, "dec_degrees": 11.82}))
    assert lint(data, guided=True).ok
    assert PhotonScriptConfig().phd2_cal_mode == "auto"
