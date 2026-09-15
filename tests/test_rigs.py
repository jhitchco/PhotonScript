"""Tests for the rig registry (main + piggyback 2nd NINA)."""

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared import rigs


def test_rig_ids_main_only_by_default():
    cfg = PhotonScriptConfig()
    assert rigs.rig_ids(cfg) == ["rc16"]


def test_rig_ids_includes_piggyback_when_enabled():
    cfg = PhotonScriptConfig(piggyback_enabled=True)
    assert rigs.rig_ids(cfg) == ["rc16", "piggyback"]


def test_rig_config_main_is_identity():
    cfg = PhotonScriptConfig()
    assert rigs.rig_config(cfg, "rc16") is cfg


def test_rig_config_piggyback_overrides():
    cfg = PhotonScriptConfig(
        piggyback_enabled=True,
        piggyback_nina_base_url="http://localhost:1889/v2/api",
        piggyback_pixel_scale_arcsec=1.29,
        piggyback_default_gain=100,
        piggyback_image_watch_dir=r"C:\pb\images",
    )
    pc = rigs.rig_config(cfg, "piggyback")
    # main config untouched
    assert cfg.nina_base_url != pc.nina_base_url
    assert pc.nina_base_url == "http://localhost:1889/v2/api"
    assert pc.pixel_scale_arcsec == 1.29
    assert pc.default_gain == 100
    assert pc.image_watch_dir == r"C:\pb\images"
    # hfr gate swapped to the wide-field-appropriate value
    assert pc.quality_hfr_abs_max == cfg.piggyback_hfr_abs_max


def test_rig_devices_piggyback_is_camera_and_focuser():
    assert rigs.rig_devices("piggyback") == ("camera", "focuser")
    assert "safetymonitor" in rigs.rig_devices("rc16")


def test_rig_label():
    cfg = PhotonScriptConfig(piggyback_enabled=True, piggyback_name="Piggy-600")
    assert rigs.rig_label(cfg, "rc16") == "RC16"
    assert rigs.rig_label(cfg, "piggyback") == "Piggy-600"


def test_rig_setpoint_and_config_override():
    cfg = PhotonScriptConfig(piggyback_enabled=True, piggyback_setpoint_c=-5.0)
    assert rigs.rig_setpoint(cfg, "rc16") == cfg.camera_setpoint_c
    assert rigs.rig_setpoint(cfg, "piggyback") == -5.0
    assert rigs.rig_config(cfg, "piggyback").camera_setpoint_c == -5.0


def test_rig_config_piggyback_library_subtree_and_darks():
    # piggyback calibration + lights land in their OWN library subtree so they
    # never mix with the RC16's (calibration_health keys off library_dir), and
    # use the OSC dark-exposure set.
    cfg = PhotonScriptConfig(
        piggyback_enabled=True,
        library_dir="/data/Library",
        piggyback_dark_exposures="120,300",
    )
    pc = rigs.rig_config(cfg, "piggyback")
    assert pc.library_dir.replace("\\", "/").endswith("Library/piggyback")
    assert pc.dark_exposures == "120,300"
    # RC16's own library + darks are untouched
    assert cfg.library_dir == "/data/Library"
    assert rigs.rig_config(cfg, "rc16").library_dir == "/data/Library"


def test_rig_config_explicit_piggyback_library_wins():
    cfg = PhotonScriptConfig(piggyback_enabled=True,
                             piggyback_library_dir="/pb/lib")
    assert rigs.rig_config(cfg, "piggyback").library_dir == "/pb/lib"


def test_orchestrator_spawns_second_agent_only_with_own_dir():
    from photonscript import orchestrator as o
    # main only by default
    assert len(o._telescope_agents(PhotonScriptConfig())) == 1
    # enabled but no piggyback dir -> refuse (would double-grade RC16)
    assert len(o._telescope_agents(
        PhotonScriptConfig(piggyback_enabled=True))) == 1
    # enabled + distinct dir -> second agent, piggyback-scaled
    ag = o._telescope_agents(PhotonScriptConfig(
        piggyback_enabled=True, piggyback_image_watch_dir=r"C:\pb"))
    assert len(ag) == 2 and ag[1].rig == "piggyback"
    assert ag[1].config.pixel_scale_arcsec == 1.29
    # piggyback dir equal to RC16 dir -> refuse
    same = PhotonScriptConfig(
        piggyback_enabled=True,
        piggyback_image_watch_dir=PhotonScriptConfig().image_watch_dir)
    assert len(o._telescope_agents(same)) == 1
