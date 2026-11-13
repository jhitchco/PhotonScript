"""PS-117 part (a): per-rig, per-readout-mode camera constants, and both
graders computing the swamp factor with the read noise of the frame's rig
and mode (RC16 HCG 5.66 ADU, RC16 LCG 4.27, Piggy-600 3.27; the old single
value was 4.1). Numbers from the PS-117 grooming measurements."""

import numpy as np
import pytest

from photonscript.shared import rigs
from photonscript.shared import star_measure as sm
from photonscript.shared.config import PhotonScriptConfig

from tests.test_scheduler.test_ps83_measure_parity import (NIGHT, _cfg,
                                                          _field, _need_sep,
                                                          _write)

HCG = {"READOUTM": "High Conversion Gain"}
LCG = {"READOUTM": "Low Conversion Gain"}


def test_config_defaults_are_the_measured_constants():
    c = PhotonScriptConfig(_env_file=None)
    assert c.camera_read_noise_adu == 5.66
    assert c.camera_gain_e_adu == 0.25
    assert c.camera_read_noise_lcg_adu == 4.27
    assert c.camera_gain_lcg_e_adu == 0.79
    assert c.piggyback_read_noise_adu == 3.27
    assert c.piggyback_gain_e_adu == 0.74


def test_new_keys_are_on_the_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    by_attr = {f[0]: f for f in _CONFIG_FIELDS}
    for attr, env in (("camera_read_noise_adu", "PS_CAMERA_READ_NOISE_ADU"),
                      ("camera_read_noise_lcg_adu",
                       "PS_CAMERA_READ_NOISE_LCG_ADU"),
                      ("camera_gain_e_adu", "PS_CAMERA_GAIN_E_ADU"),
                      ("camera_gain_lcg_e_adu", "PS_CAMERA_GAIN_LCG_E_ADU"),
                      ("piggyback_read_noise_adu",
                       "PS_PIGGYBACK_READ_NOISE_ADU"),
                      ("piggyback_gain_e_adu", "PS_PIGGYBACK_GAIN_E_ADU")):
        assert by_attr[attr][1] == env and by_attr[attr][4] == "float"


@pytest.mark.parametrize("ro,lcg", [
    ("Low Conversion Gain", True), ("low conversion gain ", True),
    ("High Conversion Gain", False), ("High Gain", False), ("", False),
    (None, False)])
def test_is_lcg(ro, lcg):
    assert rigs.is_lcg(ro) is lcg


def test_rc16_constants_follow_readoutm():
    c = PhotonScriptConfig(_env_file=None)
    assert rigs.camera_constants(c) == {"read_noise_adu": 5.66,
                                        "gain_e_adu": 0.25, "readout": "HCG"}
    assert rigs.camera_constants(c, HCG)["read_noise_adu"] == 5.66
    assert rigs.camera_constants(c, {})["read_noise_adu"] == 5.66
    assert rigs.camera_constants(c, LCG) == {"read_noise_adu": 4.27,
                                             "gain_e_adu": 0.79,
                                             "readout": "LCG"}


def test_piggyback_view_carries_its_own_constants_in_both_modes():
    c = PhotonScriptConfig(_env_file=None)
    pc = rigs.rig_config(c, rigs.PIGGYBACK)
    assert pc.camera_read_noise_adu == pc.camera_read_noise_lcg_adu == 3.27
    assert pc.camera_gain_e_adu == pc.camera_gain_lcg_e_adu == 0.74
    # the AP26CC shoots LCG; its header must not pick the RC16's LCG value
    assert rigs.camera_constants(pc, LCG)["read_noise_adu"] == 3.27
    assert rigs.camera_constants(pc)["gain_e_adu"] == 0.74
    # the RC16 view is untouched
    assert c.camera_read_noise_adu == 5.66
    # an env override of the piggyback key reaches the view
    pc2 = rigs.rig_config(PhotonScriptConfig(_env_file=None,
                                             piggyback_read_noise_adu=3.0),
                          rigs.PIGGYBACK)
    assert rigs.camera_constants(pc2, LCG)["read_noise_adu"] == 3.0


@pytest.mark.parametrize("graded_at_4_1,rn,expect", [
    (23.0, 3.27, 36.2),   # M31 400 s Piggy-600: grooming 23 -> 36
    (26.0, 3.27, 40.9),   # M31 300 s Piggy-600: 26 -> 38 (rounded grooming)
    (6.0, 3.27, 9.4),     # M31 120 s Piggy-600: 6.0 -> 8.6 (grooming)
    (8.8, 5.66, 4.6),     # Heart OIII 300 s RC16 HCG: over-reported 1.9x
    (25.2, 5.66, 13.2),   # Heart OIII 300 s full moon
])
def test_swamp_with_the_measured_read_noise(graded_at_4_1, rn, expect):
    noise = 4.1 * graded_at_4_1 ** 0.5
    data = np.full((40, 40), 1000.0, dtype=np.float32)
    cfg = PhotonScriptConfig(_env_file=None, camera_read_noise_adu=4.1)
    assert sm.exposure(data, [], [], noise, cfg)["swamp"] == \
        pytest.approx(graded_at_4_1, abs=0.1)
    ex = sm.exposure(data, [], [], noise, cfg, read_noise=rn)
    assert ex["swamp"] == pytest.approx(expect, abs=0.15)


def test_cats_eye_ha_60s_stays_under_on_hcg():
    noise = 4.1 * 2.7 ** 0.5     # graded 2.7 (flagged under) at RN 4.1
    data = np.full((40, 40), 1000.0, dtype=np.float32)
    ex = sm.exposure(data, [], [], noise, PhotonScriptConfig(_env_file=None))
    assert ex["swamp"] == pytest.approx(1.4, abs=0.05)
    assert ex["exposure"] == "under"


def _flat_noise(sigma=12.0, size=256, seed=5):
    rng = np.random.default_rng(seed)
    return rng.normal(1000, sigma, (size, size)).astype(np.float32)


def test_measure_frame_picks_read_noise_by_header():
    cfg = PhotonScriptConfig(_env_file=None)
    d = _flat_noise()
    m_none = sm.measure_frame(d, cfg)
    m_hcg = sm.measure_frame(d, cfg, header=HCG)
    m_lcg = sm.measure_frame(d, cfg, header=LCG)
    n = m_none["noise"]     # rounded to 0.01 in the record: compare approx
    assert m_none["swamp"] == m_hcg["swamp"]
    assert m_hcg["swamp"] == pytest.approx((n / 5.66) ** 2, abs=0.1)
    assert m_lcg["swamp"] == pytest.approx((n / 4.27) ** 2, abs=0.1)
    pm = sm.measure_frame(d, rigs.rig_config(cfg, rigs.PIGGYBACK),
                          "piggyback", header=LCG)
    assert pm["swamp"] == pytest.approx((pm["noise"] / 3.27) ** 2, abs=0.1)


def test_both_graders_use_the_lcg_read_noise_for_an_lcg_frame(tmp_path):
    """Live (validate_image) and backfill (_fast_grade) read READOUTM from
    the FITS and record the same swamp, computed with RN 4.27."""
    _need_sep()
    from photonscript.scheduler.runs import _fast_grade
    from photonscript.telescope_agent.image_validator import validate_image
    cfg = _cfg(tmp_path)
    f = _write(tmp_path / "fits" / NIGHT / "LIGHT" / "l_Ha_300s_0001.fits",
               _field(), READOUTM="Low Conversion Gain")
    live = validate_image(str(f), cfg, rig="rc16")
    back = _fast_grade(f, cfg, [])
    assert live.swamp_factor == back["swamp"]
    assert back["swamp"] == pytest.approx((back["noise"] / 4.27) ** 2,
                                          abs=0.1)
    g = _write(tmp_path / "fits" / NIGHT / "LIGHT" / "h_Ha_300s_0001.fits",
               _field(), READOUTM="High Conversion Gain")
    assert _fast_grade(g, cfg, [])["swamp"] == pytest.approx(
        (back["noise"] / 5.66) ** 2, abs=0.1)


def test_both_graders_use_the_piggyback_read_noise(tmp_path):
    _need_sep()
    from photonscript.scheduler.runs import _fast_grade
    from photonscript.telescope_agent.image_validator import validate_image
    cfg = _cfg(tmp_path)
    f = _write(tmp_path / "fits" / NIGHT / "LIGHT" / "o_300s_0001.fits",
               _field(q_ratio=1.0, fwhm=3.0, size=400, n=10, bayer=True,
                      hot=0),
               BAYERPAT="RGGB", READOUTM="Low Conversion Gain")
    live = validate_image(str(f), rigs.rig_config(cfg, "piggyback"),
                          rig="piggyback")
    back = _fast_grade(f, cfg, [], rig="piggyback")
    assert live.swamp_factor == back["swamp"]
    assert back["swamp"] == pytest.approx((back["noise"] / 3.27) ** 2,
                                          abs=0.1)
