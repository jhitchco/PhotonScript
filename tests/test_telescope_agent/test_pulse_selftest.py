"""PS-92 pulse-path self-test against the shared fake PHD2 (a simulated star
field that moves by rate x ms on each guide pulse) and a fake NINA mount."""
import math
from pathlib import Path

import numpy as np
import pytest

from photonscript.scheduler import phd2_logs as pl
from photonscript.shared import guide_motion as gm
from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import phd2_ops
from photonscript.telescope_agent import pulse_selftest as ps
from photonscript.telescope_agent.phd2_client import guide_scale_from_config
from tests.fakes.fake_phd2 import FakePHD2, SimField

SPEED = 7.5   # "/s, what NINA reports for the Paramount guide rate


class FakeNina:
    def __init__(self, **kw):
        self.mount = {"Connected": True, "Tracking": True, "AtPark": False,
                      "Slewing": False, "Declination": 0.0, "SideOfPier": "pierWest",
                      "Altitude": 70.0, "GuideRateRightAscensionArcsecPerSec": SPEED,
                      "GuideRateDeclinationArcsecPerSec": SPEED}
        self.mount.update(kw)

    async def get_mount_info(self):
        return dict(self.mount)


def _cfg(tmp_path, port, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                              phd2_host="127.0.0.1", phd2_port=port, **kw)


async def _run(tmp_path, *, dec=0.0, ra_response=1.0, dec_response=1.0,
               field=None, nina_kw=None, context="manual", slot="manual",
               fake_kw=None, cfg_kw=None):
    phd2_ops._reset_for_tests()
    scale = guide_scale_from_config(PhotonScriptConfig(_env_file=None), 2)
    f = FakePHD2(tmp_path / "phd2tmp", field=field or SimField.default(),
                 ra_px_s=SPEED * math.cos(math.radians(dec)) / scale,
                 dec_px_s=SPEED / scale, ra_response=ra_response,
                 dec_response=dec_response, **(fake_kw or {}))
    port = await f.start()
    try:
        cfg = _cfg(tmp_path, port, **(cfg_kw or {}))
        nina = FakeNina(Declination=dec, **(nina_kw or {}))
        res = await ps.run_selftest(cfg, context=context, slot=slot, nina=nina,
                                    clock_scale=0.0)
    finally:
        await f.close()
    return res, f, cfg


def _ratios(res):
    return {d: v["ratio"] for d, v in res["directions"].items()}


@pytest.mark.parametrize("dec", [0.0, 66.0])
async def test_pass_at_dec_0_and_66(tmp_path, dec):
    res, f, cfg = await _run(tmp_path, dec=dec)
    assert res["verdict"] == "PASS", res["reasons"]
    for d, r in _ratios(res).items():
        assert 0.85 < r < 1.15, (d, r)
    assert res["pairs"]["WE"]["oppose"] and res["pairs"]["NS"]["oppose"]
    assert abs(res["pairs"]["axes_angle_deg"] - 90) < 10
    # RA pulses are longer at high Dec (cos Dec)
    assert res["pulse_ms"]["ra"] > res["pulse_ms"]["dec"] * (1.5 if dec > 60 else 0.5)
    assert [p[1] for p in f.pulses] == list("WWWEEENNNSSS")
    assert res["speeds"]["source"] == "nina"
    rows = store.selftest_results(cfg, night=res["night"])
    assert rows and rows[-1]["verdict"] == "PASS" and rows[-1]["kind"] == "active"
    assert not phd2_ops.busy()


async def test_dead_mount_fails_and_alerts_once(tmp_path, monkeypatch):
    import photonscript.shared.pushover as po
    pushes, fallbacks = [], []

    async def _notify(cfg, msg, **kw):
        pushes.append(msg)
    monkeypatch.setattr(po, "notify", _notify)
    import photonscript.scheduler.armer as armer_mod

    async def _fb(cfg, reason):
        fallbacks.append(reason)
    monkeypatch.setattr(armer_mod, "request_fallback_unguided", _fb)
    res, f, cfg = await _run(tmp_path, ra_response=0.0, dec_response=0.0)
    assert res["verdict"] == "FAIL"
    assert all(r < 0.2 for r in _ratios(res).values())
    assert len(pushes) == 1 and "DirectGuide" in pushes[0]
    assert pushes[0].index("DirectGuide") < pushes[0].index("Can Get Pointing")
    assert fallbacks == []                         # selftest_on_fail=alert
    res2, _, _ = await _run(tmp_path, ra_response=0.0, dec_response=0.0,
                            cfg_kw={"selftest_on_fail": "unguided"})
    assert res2["verdict"] == "FAIL" and len(pushes) == 1   # once per night
    assert fallbacks == ["pulse self-test FAIL"]


async def test_guide_rate_mismatch_warns_above_1_5(tmp_path):
    # 09-26's 1.41x is recorded but stays under the approved 1.5x WARN line
    res, _, _ = await _run(tmp_path, ra_response=1.41, dec_response=1.41)
    assert res["verdict"] == "PASS"
    assert all(1.35 < r < 1.47 for r in _ratios(res).values())
    res, _, _ = await _run(tmp_path / "w", ra_response=1.65, dec_response=1.65)
    assert res["verdict"] == "WARN"
    assert all(1.55 < r < 1.75 for r in _ratios(res).values())
    assert any("guide rate mismatch" in r for r in res["reasons"])


async def test_hot_pixel_only_field_is_inconclusive(tmp_path):
    field = SimField(stars=[], hot=[(20.0, 15.0, 30000.0), (130.0, 100.0, 25000.0),
                                    (80.0, 60.0, 20000.0)])
    res, f, _ = await _run(tmp_path, field=field)
    assert res["verdict"] == "INCONCLUSIVE" and "star" in res["reasons"][0]
    assert f.pulses == []


async def test_drift_does_not_bias_the_ratios(tmp_path):
    res, _, _ = await _run(tmp_path, fake_kw={"drift_px_frame": (0.05, -0.03),
                                              "ra_axis_deg": 30.0})
    assert res["verdict"] == "PASS", res["reasons"]
    assert all(0.85 < r < 1.15 for r in _ratios(res).values())


async def test_600mm_profile_uses_the_config_scale(tmp_path):
    res, _, _ = await _run(tmp_path, fake_kw={"pixel_scale": 1.29})
    assert res["verdict"] == "PASS"
    assert res["profile_scale"] == pytest.approx(1.29)
    assert res["scale_arcsec_px"] < 0.3
    assert any("using config" in n for n in res["notes"])


async def test_guiding_refused_outside_the_nina_slot(tmp_path, monkeypatch):
    res, f, _ = await _run(tmp_path, fake_kw={"app_state": "Guiding"})
    assert res["verdict"] == "INCONCLUSIVE" and "refused" in res["reasons"][0]
    assert f.pulses == [] and "stop_capture" not in f.methods()
    res, f, _ = await _run(tmp_path / "n", fake_kw={"app_state": "Guiding"},
                           context="nina", slot="target",
                           cfg_kw={"phd2_selftest_enabled": True})
    assert res["verdict"] == "PASS"
    assert f.methods().index("stop_capture") < f.methods().index("guide_pulse")
    assert any("stopped PHD2" in n for n in res["notes"])


async def test_timeout_is_inconclusive(tmp_path):
    res, _, _ = await _run(tmp_path, fake_kw={"exposure_s": 0.4},
                           cfg_kw={"selftest_timeout_s": 1})
    assert res["verdict"] == "INCONCLUSIVE" and "timed out" in res["reasons"][0]
    assert not phd2_ops.busy()


async def test_mount_preconditions(tmp_path):
    res, f, _ = await _run(tmp_path, nina_kw={"Tracking": False})
    assert res["verdict"] == "INCONCLUSIVE" and "not tracking" in res["reasons"][0]
    res, _, _ = await _run(tmp_path / "p", nina_kw={"AtPark": True})
    assert "parked" in res["reasons"][0]


async def test_nina_slot_skips_when_disabled_or_already_passed(tmp_path):
    res, f, cfg = await _run(tmp_path, context="nina", slot="twilight")
    assert res["verdict"] == "SKIPPED" and f.pulses == []
    on = {"phd2_selftest_enabled": True}
    res, f, cfg = await _run(tmp_path, context="nina", slot="twilight", cfg_kw=on)
    assert res["verdict"] == "PASS"
    assert f.methods()[-1] == "stop_capture"         # twilight exit: stopped
    res, f, _ = await _run(tmp_path, context="nina", slot="target", cfg_kw=on)
    assert res["cached"] is True and f.pulses == []
    # the other pier side is tested again; the target exit selects a star
    res, f, _ = await _run(tmp_path, context="nina", slot="target", cfg_kw=on,
                           nina_kw={"SideOfPier": "pierEast"})
    assert res["verdict"] == "PASS" and not res.get("cached")
    assert "find_star" in f.methods() and f.app_state == "Looping"


async def test_zero_guide_rate_is_a_fail_cause(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "speed_from_log", lambda cfg: (None, None))
    res, _, _ = await _run(tmp_path, nina_kw={
        "GuideRateRightAscensionArcsecPerSec": 0.0,
        "GuideRateDeclinationArcsecPerSec": 0.0})
    assert res["verdict"] == "FAIL" and "zero" in res["reasons"][0]
    assert res["speeds"]["source"].startswith("config")


# ---- shared math --------------------------------------------------------------

def test_register_shift_recovers_a_known_shift_and_ignores_hot_pixels():
    f = SimField.default()
    a = f.render(1).astype(float)
    f.offset = [3.4, -2.2]
    b = f.render(2).astype(float)
    dx, dy, _ = gm.register_shift(a, b)
    assert dx == pytest.approx(3.4, abs=0.25) and dy == pytest.approx(-2.2, abs=0.25)
    # the fixed hot pixels never pin the answer to zero; masking them agrees
    mask = np.zeros(a.shape, dtype=bool)
    for x, y, _v in f.hot:
        mask[int(y), int(x)] = True
    mx, my, _ = gm.register_shift(a, b, mask)
    assert mx == pytest.approx(dx, abs=0.1) and my == pytest.approx(dy, abs=0.1)


def test_verdict_rules():
    def steps(r_w=1.0, r_e=1.0, r_n=1.0, r_s=1.0, e_dir=(-1, 0)):
        out = []
        for d, r, v in (("W", r_w, (1, 0)), ("E", r_e, e_dir),
                        ("N", r_n, (0, 1)), ("S", r_s, (0, -1))):
            out.append({"dir": d, "dx": 10 * r * v[0], "dy": 10 * r * v[1],
                        "expected_px": 10.0})
        return out
    assert gm.selftest_verdict(steps())["verdict"] == "PASS"
    assert gm.selftest_verdict(steps(r_w=1.6))["verdict"] == "WARN"
    assert gm.selftest_verdict(steps(r_n=0.3))["verdict"] == "FAIL"
    bad = gm.selftest_verdict(steps(e_dir=(1, 0)))         # E moves like W
    assert bad["verdict"] == "FAIL" and not bad["pairs"]["WE"]["oppose"]
    assert gm.selftest_verdict([])["verdict"] == "INCONCLUSIVE"
    assert gm.expected_px(7.5, 60.0, 1000, 0.25, "ra") == pytest.approx(15.0)
    assert gm.pulse_ms_for(10, 7.5, 0.0, 0.25, "dec") == 333
    assert gm.pulse_ms_for(10, 0.01, 0.0, 0.25, "dec") == 2000


# ---- passive post-flip check: replay ----------------------------------------------

FIX = Path(__file__).parents[1] / "test_scheduler" / "fixtures" / "phd2"


def _session(name, start):
    secs = pl.parse_guide_log((FIX / name).read_text(encoding="utf-8"), name)
    s = next(x for x in secs if x["kind"] == "guiding" and x["start"].endswith(start))
    scale, _ = pl._scale_for(s["header"], PhotonScriptConfig(_env_file=None))
    return s["frames"], gm.pulse_rates(s["header"], scale), scale


def test_passive_check_heart_fails_and_a_working_session_does_not():
    fr, rates, scale = _session("PHD2_GuideLog_2026-09-26_120453.txt", "21:54:09")
    t0 = fr[0]["t"]
    v, axes = ps.passive_verdict([f for f in fr if f["t"] - t0 <= 600], rates, scale)
    assert v == "FAIL" and axes["ra"]["response_verdict"] == "not moving"
    fr, rates, scale = _session("PHD2_GuideLog_2026-09-25_192414.txt", "01:36:51")
    v, _ = ps.passive_verdict(fr, rates, scale)
    assert v is None


async def test_passive_fail_recorded_once_per_source(tmp_path, monkeypatch):
    import photonscript.shared.pushover as po
    pushes = []

    async def _notify(cfg, msg, **kw):
        pushes.append(msg)
    monkeypatch.setattr(po, "notify", _notify)
    cfg = PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "d")
    r1 = await ps.record_passive_fail(cfg, "RA pulses did not move the star",
                                      source="post-flip", night="2026-10-02")
    r2 = await ps.record_passive_fail(cfg, "again", source="post-flip",
                                      night="2026-10-02")
    assert r1 and r2 is None and len(pushes) == 1
    rows = store.selftest_results(cfg, night="2026-10-02")
    assert [r["kind"] for r in rows] == ["passive"]


async def test_agent_flip_watch_records_a_post_flip_fail(tmp_path, monkeypatch):
    """Pier side changes while guiding; the next 3 min of guide frames (the
    09-26 Heart session: max pulses, no motion) become a passive FAIL."""
    from photonscript.telescope_agent.agent import TelescopeAgent
    from photonscript.telescope_agent.guide_guard import GuardContext
    import photonscript.shared.pushover as po
    pushes = []

    async def _notify(cfg, msg, **kw):
        pushes.append(msg)
    monkeypatch.setattr(po, "notify", _notify)
    a = TelescopeAgent(PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "d"))
    fr, rates, scale = _session("PHD2_GuideLog_2026-09-26_120453.txt", "21:54:09")
    a.state.mount_side_of_pier = "East"
    await a._flip_watch(GuardContext(app_state="Guiding", now=fr[0]["t"] - 5))
    a.state.mount_side_of_pier = "West"
    t0 = fr[0]["t"] - 1
    await a._flip_watch(GuardContext(app_state="Guiding", now=t0))
    assert a._flip_at == t0
    win = [f for f in fr if f["t"] - t0 <= 400]
    await a._flip_watch(GuardContext(frames=win, app_state="Guiding", scale=scale,
                                     rates_px_s=rates, now=win[-1]["t"]))
    assert a._flip_at is None
    rows = store.selftest_results(a.config, night=store.night_of(a.config))
    assert [(r["kind"], r["source"], r["verdict"]) for r in rows] == [
        ("passive", "post-flip", "FAIL")]
    assert rows[0]["pier_side"] == "West" and len(pushes) == 1
