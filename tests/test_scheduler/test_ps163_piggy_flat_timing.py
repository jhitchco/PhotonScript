"""PS-163 / flats v2: Piggy-600 OSC dawn flats start late enough to find a bright sky.

2026-10-07 (NINA #2 log): the companion's SkyFlat started at nautical dawn
+5 (+90 s) and NINA's exposure search climbed to its 30 s max while the sky
read 557 -> 8188 ADU, then failed "sky is too dim" after 14 min. PS-163
moved the start to nautical dawn + 20; 10-13 / 10-15 still read only
9581-11978 ADU at 30 s there. 2026-10-24 (flats v2): the start is the
Piggy-600's own sky model (sky_flat_model.py): the sun altitude at which a
flats_target_exposure_max_s (10 s) exposure fills the histogram.
"""
import json
from datetime import timedelta

from photonscript.scheduler import flat_plan as fp
from photonscript.scheduler import sky_flat_model as sfm
from photonscript.scheduler.calibration import (dawn_flat_plan,
                                                generate_piggyback_companion_json)
from photonscript.shared import rigs
from photonscript.shared.config import PhotonScriptConfig

NIGHT = "2026-10-14"            # the 10-15 dawn: nautical dawn 06:27 MDT


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _short(t):
    return t.split(",")[0].split(".")[-1]


def _cfg(tmp_path, **kw):
    return rigs.rig_config(PhotonScriptConfig(
        _env_file=None, data_dir=str(tmp_path), observatory_tz="America/Denver",
        piggyback_enabled=True, piggyback_default_gain=100,
        piggyback_default_offset=256, piggyback_dark_exposures="120",
        piggyback_setpoint_c=0.0, piggyback_image_lights=True,
        piggyback_focus_seed=0, **kw), "piggyback")


def test_osc_start_is_where_the_model_fills_10_s(tmp_path):
    cfg = _cfg(tmp_path)
    plan = dawn_flat_plan(cfg, [], True, night=NIGHT)
    tw = fp.twilight(cfg, NIGHT)
    slot = plan["rigs"]["piggyback"]["slots"][0]
    want = sfm.alt_for_exposure(sfm.DEFAULTS["piggyback"], "OSC", 10.0)
    assert abs(sfm.sun_alt(cfg, slot["start"]) - want) < 0.05
    # the 10-15 evidence: 9581 ADU at 30 s at ND + 23 (sun -6.8): far too
    # dim. The model starts the set well after that, and before sunrise
    assert slot["start"] > tw["naut_dawn"] + timedelta(minutes=30)
    assert slot["start"] < tw["sunrise"]


def test_osc_start_follows_the_exposure_range(tmp_path):
    a = dawn_flat_plan(_cfg(tmp_path / "a"), [], True, night=NIGHT)
    b = dawn_flat_plan(_cfg(tmp_path / "b", flats_target_exposure_max_s=20.0),
                       [], True, night=NIGHT)
    sa = a["rigs"]["piggyback"]["slots"][0]["start"]
    sb = b["rigs"]["piggyback"]["slots"][0]["start"]
    # a longer allowed exposure opens the dawn window earlier (dimmer sky)
    assert sb < sa


def test_companion_waits_for_the_planned_clock_time(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    from photonscript.scheduler import calibration as cal
    real = cal.dawn_flat_plan
    monkeypatch.setattr(cal, "dawn_flat_plan", lambda c, f, o, now=None, night=None:
                        real(c, f, o, night=NIGHT))
    plan = real(cfg, [], True, night=NIGHT)
    st = fp.to_local(cfg, plan["rigs"]["piggyback"]["slots"][0]["start"])
    for safety in (True, False):
        root = json.loads(generate_piggyback_companion_json(
            cfg, has_safety=safety, with_lights=True))
        waits = [n for n in _walk(root) if _short(n.get("$type", "")) == "WaitForTime"
                 and "TimeProvider" in (n.get("SelectedProvider") or {}).get("$type", "")
                 and (n.get("Hours"), n.get("Minutes")) == (st.hour, st.minute)]
        assert len(waits) == 1
        # no nautical-dawn offset wait for the flats any more
        assert not [n for n in _walk(root) if _short(n.get("$type", "")) == "WaitForTime"
                    and "NauticalDawnProvider" in
                    (n.get("SelectedProvider") or {}).get("$type", "")]


def test_flats_v2_settings_on_the_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS as CONFIG_FIELDS
    keys = [f[0] for f in CONFIG_FIELDS]
    for k in ("flats_target_exposure_min_s", "flats_target_exposure_max_s",
              "flats_dawn_end_after_sunrise_min", "flats_point_alt_deg"):
        assert k in keys
    assert "piggyback_flat_dawn_offset_min" not in keys
