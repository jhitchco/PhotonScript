"""PS-91 shared pieces: the PHD2 client's guide-frame ring buffer, lock
tracking, raw event hook, call()-based commands and RPC wrappers (against the
shared fake PHD2 server), and the phd2_ops one-actor lock."""
import asyncio

import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import phd2_ops
from photonscript.telescope_agent.phd2_client import (
    PHD2Client, PHD2RPCError, frame_from_event)
from tests.fakes.fake_phd2 import FakePHD2, SimField


def _cfg(**kw):
    return PhotonScriptConfig(_env_file=None, **kw)


@pytest.fixture
async def fake(tmp_path):
    f = FakePHD2(tmp_path / "phd2", field=SimField.default())
    port = await f.start()
    c = PHD2Client("127.0.0.1", port, config=_cfg())
    assert await c.connect()
    task = asyncio.create_task(c.run_event_loop())
    yield f, c
    await c.disconnect()
    task.cancel()
    await f.close()


def test_frame_from_guidestep_uses_guide_log_letters():
    ev = {"Event": "GuideStep", "Frame": 12, "RADistanceRaw": 1.5,
          "DECDistanceRaw": -0.5, "RADuration": 300, "RADirection": "West",
          "DECDuration": 120, "DECDirection": "South", "HFD": 3.2,
          "SNR": 25.0, "StarMass": 9000, "ErrorCode": 0}
    f = frame_from_event(ev, t=100.0, settling=False, epoch=3)
    assert (f["ra"], f["dec"], f["ra_dir"], f["dec_dir"]) == (1.5, -0.5, "W", "S")
    assert (f["ra_ms"], f["dec_ms"], f["hfd"], f["epoch"], f["drop"]) == \
        (300.0, 120.0, 3.2, 3, False)
    lost = frame_from_event({"Event": "StarLost", "Status": "Star lost - low SNR",
                             "ErrorCode": 2}, t=101.0, settling=True, epoch=3)
    assert lost["drop"] and lost["ra"] is None and lost["reason"].startswith("Star lost")


async def test_ring_buffer_lock_epoch_and_event_hook():
    c = PHD2Client(config=_cfg())
    seen = []

    async def hook(ev):
        seen.append(ev.get("Event"))
    c.on_event(hook)
    await c._handle_event({"Event": "LockPositionSet", "X": 40.0, "Y": 30.0})
    assert c.lock_position == (40.0, 30.0)
    e0 = c.lock_epoch
    for i in range(3):
        await c._handle_event({"Event": "GuideStep", "Frame": i, "Timestamp": 1000 + i,
                               "RADistanceRaw": 0.1, "DECDistanceRaw": 0.2,
                               "RADuration": 0, "RADirection": "East",
                               "DECDuration": 0, "DECDirection": "North",
                               "HFD": 4.0})
    await c._handle_event({"Event": "GuidingDithered", "dx": 3, "dy": 1})
    await c._handle_event({"Event": "SettleBegin"})
    await c._handle_event({"Event": "GuideStep", "Frame": 9, "Timestamp": 1010,
                           "RADistanceRaw": 2.0, "DECDistanceRaw": 0.0})
    frames = c.recent_frames()
    assert len(frames) == 4 and frames[-1]["settling"] is True
    assert frames[0]["epoch"] == e0 and frames[-1]["epoch"] == e0 + 1
    assert c.app_state == "Guiding" and c.frame_count == 4
    assert [f["t"] for f in c.recent_frames(5, now=1010)] == [1010.0]
    assert seen.count("GuideStep") == 4 and "GuidingDithered" in seen
    # metrics still flow (PS-70 behavior unchanged)
    assert c.metrics.samples == 4


async def test_wrappers_round_trip_against_the_fake(fake):
    f, c = fake
    assert await c.get_app_state() == "Stopped"
    await c.loop()
    assert await c.wait_frames(2, timeout=5)
    assert c.app_state == "Looping"
    xy = await c.find_star([60, 40, 60, 50])
    assert isinstance(xy, list) and len(xy) == 2
    await asyncio.sleep(0.1)
    assert c.lock_position == pytest.approx(tuple(xy))
    path = await c.save_image()
    assert path.endswith(".fits")
    img = await c.get_star_image()
    assert img["width"] >= 15 and img["pixels"]
    cal = await c.get_calibration_data()
    assert cal["calibrated"] is True
    assert await c.get_exposure() == 20
    assert await c.get_camera_binning() == 2
    await c.guide_pulse(500, "West")
    assert f.pulses == [(500, "W")]
    with pytest.raises(ValueError):
        await c.guide_pulse(500, "up")
    await c.stop_capture()
    assert await c.get_app_state() == "Stopped"


async def test_start_stop_dither_raise_on_refusal(fake):
    f, c = fake
    f.refuse = {"guide", "dither"}
    with pytest.raises(PHD2RPCError):
        await c.start_guiding()
    with pytest.raises(PHD2RPCError):
        await c.dither()
    assert await c.stop_guiding() == 0
    assert "stop_capture" in f.methods()


async def test_guide_pulse_refused_while_guiding(fake):
    f, c = fake
    await c.loop()
    await c.start_guiding()
    with pytest.raises(PHD2RPCError):
        await c.guide_pulse(300, "N")


async def test_get_app_state_falls_back_when_phd2_is_gone():
    c = PHD2Client("127.0.0.1", 1, config=_cfg())
    await c._handle_event({"Event": "AppState", "State": "Looping"})
    assert await c.get_app_state() == "Looping"


async def test_phd2_ops_one_actor_at_a_time():
    phd2_ops._reset_for_tests()
    async with phd2_ops.hold("selftest"):
        assert phd2_ops.owner() == "selftest" and phd2_ops.busy()
        with pytest.raises(phd2_ops.PHD2Busy) as e:
            async with phd2_ops.hold("guard"):
                pass
        assert e.value.owner == "selftest"
    assert phd2_ops.owner() is None

    async def holder():
        async with phd2_ops.hold("hotpix"):
            await asyncio.sleep(0.15)
    t = asyncio.create_task(holder())
    await asyncio.sleep(0.02)
    async with phd2_ops.hold("guard", wait_s=2, poll_s=0.02):
        assert phd2_ops.owner() == "guard"
    await t
    # released on error too
    with pytest.raises(RuntimeError):
        async with phd2_ops.hold("x"):
            raise RuntimeError("boom")
    assert not phd2_ops.busy()
