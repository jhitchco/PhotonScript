"""PS-90 guide-star auto-tune, live half: star_stats on synthetic stars
(Gaussian, clipped, 8-bit, hot pixel), decide_exposure (step down at once on a
clip, cap at 4000 ms, hysteresis, snapping to PHD2's list, only exposures with
a dark), and GuideStarTuner over the shared fake PHD2: observe never sets,
exposure mode steps toward the band, nothing while Calibrating, while another
actor holds PHD2 or inside the PS-93 calibration slot, and a filter change
applies the remembered exposure."""
import base64
from datetime import datetime

import numpy as np
import pytest

from photonscript.scheduler import phd2_audit
from photonscript.scheduler import phd2_calibration as pc
from photonscript.scheduler import phd2_tuning as tn
from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import guide_tuner as gt
from photonscript.telescope_agent import phd2_ops
from photonscript.telescope_agent.phd2_client import PHD2Client
from tests.fakes.fake_phd2 import FakePHD2, SimField

CFG = gt.TuneCfg()
DURS = [500, 1000, 1500, 2000, 2500, 3000, 3500, 4000, 4500, 5000, 6000]
ALL_DARKS = [1000, 1500, 2000, 2500, 3000, 3500, 4000]


def _reply(peak, sigma=2.0, size=21, bkg=500.0, noise=8.0, hot=False, bit8=False):
    rng = np.random.default_rng(1)
    yy, xx = np.mgrid[0:size, 0:size]
    c = size // 2
    img = rng.normal(bkg, noise, (size, size))
    if hot:
        img[c, c] += peak
    else:
        img += peak * np.exp(-((xx - c) ** 2 + (yy - c) ** 2) / (2 * sigma * sigma))
    img = np.clip(img, 0, 65535)
    if bit8:
        img = img / 257.0
    px = img.astype("<u2")
    return {"frame": 1, "width": size, "height": size, "star_pos": [c, c],
            "pixels": base64.b64encode(px.tobytes()).decode()}


def _r(peak_frac, clipped=False, bkg=0.008, **kw):
    return dict({"peak_frac": peak_frac, "amp_frac": max(0.0, peak_frac - bkg),
                 "clipped": clipped, "bit8": False, "center_share": 0.1}, **kw)


# ------------------------------------------------------------------ pure

def test_star_stats_gaussian_star():
    s = gt.star_stats(_reply(40000, sigma=2.0), 65535)
    assert s["peak_frac"] == pytest.approx((500 + 40000) / 65535, abs=0.01)
    assert s["amp_frac"] == pytest.approx(40000 / 65535, abs=0.01)
    assert not s["clipped"] and not s["bit8"]
    assert s["hfd_px"] == pytest.approx(2.355 * 2.0, rel=0.2)
    assert s["snr_est"] > 100
    assert s["center_share"] < 0.3


def test_star_stats_clip_8bit_and_hot_pixel():
    assert gt.star_stats(_reply(90000), 65535)["clipped"]           # flat top
    assert gt.star_stats(_reply(20000), 65535, error_code=1)["clipped"]  # PHD2 says so
    s8 = gt.star_stats(_reply(40000, bit8=True), 65535)
    assert s8["bit8"] and s8["peak_frac"] < 0.01
    assert gt.star_stats(_reply(30000, hot=True), 65535)["center_share"] > 0.5
    assert gt.star_stats({"width": 3}, 65535) is None


def test_decide_steps_down_at_once_on_a_clip():
    d = gt.decide_exposure([_r(1.0, clipped=True)], 3000, DURS, CFG, ALL_DARKS)
    assert d["action"] == "set" and d["ms"] < 3000
    assert d["ms"] <= 2500 and "clipped" in d["reason"]


def test_decide_waits_for_three_out_of_band_readings():
    two = [_r(0.70), _r(0.30), _r(0.30)]
    assert gt.decide_exposure(two, 2000, DURS, CFG, ALL_DARKS)["action"] == "hold"
    three = [_r(0.30), _r(0.30), _r(0.30)]
    d = gt.decide_exposure(three, 2000, DURS, CFG, ALL_DARKS)
    # amplitude 0.292 -> 0.692 wanted: 2000 * 0.692 / 0.292 = 4740 -> capped 4000
    assert d["action"] == "set" and d["ms"] == 4000 and d["pinned"] == "faint"
    # inside the 55..85% hysteresis band but outside 60..80%: hold
    near = [_r(0.57), _r(0.57), _r(0.57)]
    assert gt.decide_exposure(near, 2000, DURS, CFG, ALL_DARKS)["action"] == "hold"
    # mixed sides never act
    mixed = [_r(0.30), _r(0.95), _r(0.30)]
    assert gt.decide_exposure(mixed, 2000, DURS, CFG, ALL_DARKS)["action"] == "hold"


def test_decide_snaps_to_phd2_list_and_needs_a_dark():
    bright = [_r(0.95), _r(0.95), _r(0.95)]
    d = gt.decide_exposure(bright, 3000, DURS, CFG, ALL_DARKS)
    # 3000 * 0.692 / 0.942 = 2204 -> nearest listed 2000
    assert (d["action"], d["ms"]) == ("set", 2000)
    d = gt.decide_exposure(bright, 3000, DURS, CFG, [1000, 3000, 4000])
    assert (d["action"], d["ms"]) == ("set", 1000)    # 2000 has no dark: nearest with one
    d = gt.decide_exposure(bright, 3000, DURS, CFG, None)
    assert d["action"] == "hold" and "dark library unknown" in d["reason"]
    d = gt.decide_exposure(bright, 3000, DURS, CFG, [6000])
    assert d["action"] == "hold" and "no allowed exposure" in d["reason"]


def test_decide_never_for_8bit_or_a_hot_pixel():
    r8 = [_r(0.003, bit8=True)] * 3
    assert "8-bit" in gt.decide_exposure(r8, 2000, DURS, CFG, ALL_DARKS)["reason"]
    hot = [_r(0.3, center_share=0.8)] * 3
    assert gt.decide_exposure(hot, 2000, DURS, CFG, ALL_DARKS)["action"] == "hold"


def test_decide_pinned_bright_at_the_minimum():
    d = gt.decide_exposure([_r(1.0, clipped=True)], 1000, DURS, CFG, ALL_DARKS)
    assert d["action"] == "hold" and d["pinned"] == "bright"


def test_tune_cfg_from_config():
    c = gt.tune_cfg(PhotonScriptConfig(_env_file=None, phd2_tune_exp_ms="4000, 2000",
                                       phd2_tune_hfd_px="bad"))
    assert (c.exp_lo, c.exp_hi, c.hfd_lo, c.hfd_hi) == (2000, 4000, 2.0, 5.0)
    assert gt.tune_mode(PhotonScriptConfig(_env_file=None)) == "observe"
    assert gt.tune_mode(PhotonScriptConfig(_env_file=None, phd2_tune_mode="EXPOSURE")) == "exposure"
    assert gt.tune_mode(PhotonScriptConfig(_env_file=None, phd2_tune_mode="nonsense")) == "observe"


# ------------------------------------------------------------------ live

@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    phd2_ops._reset_for_tests()
    monkeypatch.setattr(gt, "FRAME_WAIT_S", 5.0)
    monkeypatch.setattr(phd2_audit, "dark_inventory", lambda cfg, pid=None: list(ALL_DARKS))
    yield
    phd2_ops._reset_for_tests()


def _field(peak):
    return SimField(width=120, height=100, stars=[(60.0, 50.0, peak, 1.6)], hot=[])


async def _setup(tmp_path, peak=10000.0, mode="observe", exp_ms=2000, ctx=None,
                 auto=False, **fake_kw):
    fake = FakePHD2(tmp_path / "phd2tmp", field=_field(peak), flux_ref_ms=2000,
                    guide_exposure_ms=exp_ms, app_state="Stopped", **fake_kw)
    port = await fake.start()
    cfg = PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                             phd2_host="127.0.0.1", phd2_port=port, phd2_tune_mode=mode)
    client = PHD2Client("127.0.0.1", port, config=cfg)
    assert await client.connect()
    task = await client.start_event_loop()
    await client.refresh_pixel_scale()
    context = {"target": "M27", "filter": "L"}
    context.update(ctx or {})
    tuner = gt.GuideStarTuner(cfg, client, context_fn=lambda: context)
    tuner.attach()
    if not auto:   # keep SettleDone's own measurement out of the way
        tuner._last_measure = __import__("time").time()
    return fake, client, task, tuner, cfg, context


async def _guide(client):
    await client.call("guide", [{"pixels": 1.5, "time": 1, "timeout": 10}, False])
    for _ in range(200):
        if client.app_state == "Guiding" and not client.settling:
            break
        await __import__("asyncio").sleep(0.02)


async def _close(fake, client, task):
    await client.disconnect()
    task.cancel()
    await fake.close()


async def test_observe_measures_and_records_but_never_sets(tmp_path):
    fake, client, task, tuner, cfg, _ = await _setup(tmp_path, peak=3000.0)
    try:
        await _guide(client)
        rec = await tuner.measure(force=True)
        assert rec is not None and rec["frames"] >= 3
        assert rec["peak_frac"] < 0.1 and rec["would"]["action"] == "set"
        assert "set_exposure" not in fake.methods()
        night = store.night_of(cfg)
        assert tn.night_records(cfg, night)[-1]["filter"] == "L"
        assert tn.summary(cfg, night)["by_filter"]["L"]["measurements"] == 1
    finally:
        await _close(fake, client, task)


async def test_exposure_mode_steps_a_clipped_star_down(tmp_path):
    fake, client, task, tuner, cfg, _ = await _setup(tmp_path, peak=90000.0,
                                                     mode="exposure", exp_ms=3000)
    try:
        await _guide(client)
        rec = await tuner.measure(force=True)
        assert rec["clipped"] and rec["decision"]["action"] == "set"
        assert ("set_exposure" in fake.methods()) and fake.guide_exposure_ms < 3000
        assert client.metrics.guide_camera_exposure == fake.guide_exposure_ms / 1000.0
        ch = [r for r in tn.night_records(cfg, store.night_of(cfg)) if r["kind"] == "change"]
        assert ch and ch[-1]["from"] == 3000 and ch[-1]["ok"]
        assert phd2_ops.owner() is None
    finally:
        await _close(fake, client, task)


async def test_nothing_while_calibrating_held_or_in_the_slot(tmp_path):
    fake, client, task, tuner, cfg, ctx = await _setup(tmp_path, peak=90000.0,
                                                       mode="exposure", exp_ms=3000)
    try:
        client.app_state = "Calibrating"
        assert tuner.blocked() == "PHD2 Calibrating"
        assert await tuner.measure(force=True) is None
        await _guide(client)
        async with phd2_ops.hold("guard"):
            assert "guard" in tuner.blocked()
            assert await tuner.measure(force=True) is None
        pc.save_plan(cfg, {"night": store.night_of(cfg), "status": "retrying",
                           "created_utc": store.iso_z(datetime.utcnow()),
                           "field": {"ra_hours": 18.0, "dec_degrees": 5.0}})
        assert "calibration slot" in tuner.blocked()
        pc.save_plan(cfg, dict(pc.load_plan(cfg), status="pending"))
        assert tuner.blocked() is None                      # mount elsewhere
        ctx.update(mount_ra=18.01, mount_dec=5.2)
        assert "calibration slot" in tuner.blocked()
        pc.save_plan(cfg, None)
        ctx.update(guard_open=True)
        assert "guard" in tuner.blocked()
        assert "set_exposure" not in fake.methods()
    finally:
        await _close(fake, client, task)


async def test_settle_done_triggers_a_measurement(tmp_path):
    import asyncio
    fake, client, task, tuner, cfg, _ = await _setup(tmp_path, peak=30000.0, auto=True)
    try:
        await _guide(client)
        for _ in range(300):
            if tuner.last is not None:
                break
            await asyncio.sleep(0.02)
        assert tuner.last is not None and tuner.last["why"] == "settled"
        assert 0.4 < tuner.last["peak_frac"] < 0.6
    finally:
        await _close(fake, client, task)


async def test_filter_change_applies_the_remembered_exposure(tmp_path):
    fake, client, task, tuner, cfg, ctx = await _setup(tmp_path, peak=30000.0,
                                                       mode="exposure", exp_ms=2000)
    try:
        await _guide(client)
        info = await tuner._profile_info()
        tn.record(cfg, {**info, "target": "M27", "filter": "Ha", "exposure_ms": 4000,
                        "peak_frac": 0.7, "amp_frac": 0.69, "in_band": True})
        tuner.filter_changed("L")          # first sighting: no action
        assert not tuner.tasks
        ctx["filter"] = "Ha"
        fake.flux_scale = 0.5
        tuner._last_measure = 0.0
        tuner.filter_changed("Ha")
        import asyncio
        await asyncio.gather(*list(tuner.tasks))
        assert 4000 in [r.get("params", [None])[0] for r in fake.requests
                        if r["method"] == "set_exposure"]
        assert tuner.last["filter"] == "Ha"
    finally:
        await _close(fake, client, task)


def test_client_keeps_hfd_and_error_code():
    import asyncio
    c = PHD2Client(config=None)
    asyncio.run(c._handle_event({"Event": "GuideStep", "RADistanceRaw": 0.1,
                                 "DECDistanceRaw": 0.1, "SNR": 30, "StarMass": 900,
                                 "HFD": 6.8, "ErrorCode": 1}))
    m = c.metrics
    assert (m.hfd_px, m.error_code, m.saturated) == (6.8, 1, True)
    asyncio.run(c._handle_event({"Event": "LoopingExposures", "Frame": 3, "HFD": 5.0}))
    assert c.loop_star["HFD"] == 5.0


async def test_agent_wires_the_tuner_on_the_rc16_only(tmp_path):
    from photonscript.telescope_agent.agent import TelescopeAgent
    cfg = PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                             nina_filter_names="Ha:H,OIII:O")
    rc = TelescopeAgent(cfg, rig="rc16")
    rc._tuner_setup()
    assert isinstance(rc.tuner, gt.GuideStarTuner)
    pig = TelescopeAgent(cfg, rig="piggyback")
    pig._tuner_setup()
    assert pig.tuner is None
    off = TelescopeAgent(PhotonScriptConfig(_env_file=None, phd2_tune_mode="off"), rig="rc16")
    off._tuner_setup()
    assert off.tuner is None
    seen = []
    rc.tuner.filter_changed = seen.append

    class _Nina:
        name = "H"

        async def get_filter_wheel_info(self):
            return {"SelectedFilter": {"Name": self.name, "Id": 4}}
    rc.nina = _Nina()
    await rc._poll_filter()
    await rc._poll_filter()
    rc.nina.name = "O"
    await rc._poll_filter()
    assert seen == ["Ha", "OIII"]
    rc.state.current_target = "M27"
    ctx = rc._tuner_context()
    assert (ctx["target"], ctx["filter"], ctx["guard_open"]) == ("M27", "OIII", False)
