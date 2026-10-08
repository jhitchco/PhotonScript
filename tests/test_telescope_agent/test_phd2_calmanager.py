"""PS-93 calibration manager, live half: the shared fake PHD2 runs simulated
calibrations (Calibrating steps, then CalibrationComplete with the next
calibration data, or CalibrationFailed); a fake NINA mount sits on the
planned field. Grade, retry once in the hold, give up with one alert, the
flip check and the mid-night invalidation."""
import asyncio
import math

import pytest

from photonscript.scheduler import phd2_calibration as pc
from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import phd2_calmanager as cm
from photonscript.telescope_agent import phd2_ops
from photonscript.telescope_agent.phd2_client import PHD2Client
from tests.fakes.fake_phd2 import FakePHD2

SCALE = 0.255
DEC = 6.57
X_RATE = 7.5 * math.cos(math.radians(DEC)) / SCALE
Y_RATE = 7.5 / SCALE
GOOD = {"xAngle": 0.0, "xRate": X_RATE, "xParity": "+", "yAngle": 90.5,
        "yRate": Y_RATE, "yParity": "+", "declination": DEC}
BAD = dict(GOOD, yAngle=75.0)     # 15 deg from perpendicular
FIELD = {"name": "NGC 6633", "ra_hours": 18.455, "dec_degrees": DEC}


class FakeNina:
    def __init__(self, **kw):
        self.mount = {"Connected": True, "Tracking": True, "RightAscension": 18.455,
                      "Declination": DEC, "SiderealTime": 19.0,
                      "SideOfPier": "pierWest", "Altitude": 62.0,
                      "GuideRateRightAscensionArcsecPerSec": 7.5,
                      "GuideRateDeclinationArcsecPerSec": 7.5}
        self.mount.update(kw)

    async def get_mount_info(self):
        return dict(self.mount)


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    phd2_ops._reset_for_tests()
    monkeypatch.setattr(cm, "STOPPED_STABLE_S", 0.05)
    monkeypatch.setattr(cm, "POLL_S", 0.01)
    monkeypatch.setattr(cm, "RETRY_MIN_LEFT_S", 1.0)


@pytest.fixture
def pushes(monkeypatch):
    import photonscript.shared.pushover as po
    out = {"notify": [], "record": []}

    async def _notify(cfg, msg, **kw):
        out["notify"].append(msg)

    def _record(cfg, msg, **kw):
        out["record"].append(msg)
    monkeypatch.setattr(po, "notify", _notify)
    monkeypatch.setattr(po, "record", _record)
    return out


async def _setup(tmp_path, outcomes, plan=True, nina_kw=None, **cfg_kw):
    fake = FakePHD2(tmp_path / "phd2tmp", pixel_scale=SCALE, cal_outcomes=outcomes,
                    calibration=dict(GOOD, calibrated=True))
    port = await fake.start()
    cfg = PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                             phd2_host="127.0.0.1", phd2_port=port,
                             phd2_cal_hold_s=20, **cfg_kw)
    client = PHD2Client("127.0.0.1", port, config=cfg)
    assert await client.connect()
    task = await client.start_event_loop()
    mgr = cm.CalManager(cfg, client, FakeNina(**(nina_kw or {})))
    mgr.attach()
    if plan:
        pc.save_plan(cfg, {"night": store.night_of(cfg), "status": "pending",
                           "created_utc": store.iso_z(__import__("datetime").datetime.utcnow()),
                           "field": FIELD, "hold_s": 20, "attempts": 0})
    return fake, client, task, mgr, cfg


async def _nina_slot(client, fake):
    """What NINA's slot does: StartGuiding(ForceCalibration), then (after
    the calibration and settle) StopGuiding."""
    await client.start_guiding(recalibrate=True)
    for _ in range(300):
        if fake.app_state != "Calibrating":
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.05)
    await client.stop_capture()


async def _settle(mgr, cfg, timeout=10.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        plan = pc.load_plan(cfg) or {}
        if plan.get("status") in ("done", "failed") and not mgr.tasks:
            return plan
        if mgr.last and not mgr.tasks and plan.get("status") in (None, "pending"):
            return plan     # graded outside the plan
        await asyncio.sleep(0.02)
    raise AssertionError(f"calmanager did not finish: {pc.load_plan(cfg)}")


async def _close(fake, client, task):
    await client.disconnect()
    task.cancel()
    await fake.close()


async def test_good_calibration_in_the_slot_passes_without_a_retry(tmp_path, pushes):
    fake, client, task, mgr, cfg = await _setup(tmp_path, [GOOD])
    try:
        await _nina_slot(client, fake)
        plan = await _settle(mgr, cfg)
    finally:
        await _close(fake, client, task)
    assert plan["status"] == "done" and plan["grade"] == pc.PASS
    assert fake.calibrations == 1 and not pushes["notify"]
    rec = pc.load_active(cfg, seed=False)
    assert rec["context"] == "plan" and rec["steps"]["West"] == 12
    assert rec["pier_side"] == "West" and rec["ha_hr"] == 0.55
    assert rec["profile"] == "Primary RC Profile (Guider)" and rec["binning"] == 2
    assert 60 <= rec["recommended_step_ms"] <= 80
    assert pc.load_live(cfg)["calibrated"] is True


async def test_failed_grade_is_retried_once_in_the_hold(tmp_path, pushes):
    fake, client, task, mgr, cfg = await _setup(tmp_path, [BAD, GOOD])
    try:
        await _nina_slot(client, fake)
        plan = await _settle(mgr, cfg)
    finally:
        await _close(fake, client, task)
    assert fake.calibrations == 2
    assert plan["status"] == "done" and plan["attempts"] == 1
    hist = pc.history(cfg)
    assert [(h["context"], h["grade"]) for h in hist] == [("plan", pc.FAIL),
                                                          ("retry", pc.PASS)]
    guides = [r for r in fake.requests if r["method"] == "guide"]
    assert len(guides) == 2 and guides[1]["params"][1] is True
    assert fake.app_state == "Stopped"          # never left guiding into the slew
    assert phd2_ops.owner() is None and not pushes["notify"]


async def test_second_failure_keeps_guiding_and_alerts_once(tmp_path, pushes, monkeypatch):
    import photonscript.scheduler.armer as armer_mod
    fallback = []

    async def _fb(config, reason):
        fallback.append(reason)
        return True
    monkeypatch.setattr(armer_mod, "request_fallback_unguided", _fb)
    fake, client, task, mgr, cfg = await _setup(tmp_path, [BAD, "Star did not move"])
    try:
        await _nina_slot(client, fake)
        plan = await _settle(mgr, cfg)
    finally:
        await _close(fake, client, task)
    assert plan["status"] == "failed" and fake.calibrations == 2
    assert len(pushes["notify"]) == 1 and "keep" in pushes["notify"][0]
    assert fallback == []
    assert [h["result"] for h in pc.history(cfg)] == ["complete", "failed"]


async def test_unguided_fail_action_requests_the_fallback(tmp_path, pushes, monkeypatch):
    import photonscript.scheduler.armer as armer_mod
    fallback = []

    async def _fb(config, reason):
        fallback.append(reason)
        return True
    monkeypatch.setattr(armer_mod, "request_fallback_unguided", _fb)
    fake, client, task, mgr, cfg = await _setup(tmp_path, [BAD, BAD],
                                                phd2_cal_fail_action="unguided")
    try:
        await _nina_slot(client, fake)
        await _settle(mgr, cfg)
    finally:
        await _close(fake, client, task)
    assert fallback == ["PHD2 calibration failed"]


async def test_calibration_outside_the_plan_is_graded_and_alerted_not_retried(
        tmp_path, pushes):
    fake, client, task, mgr, cfg = await _setup(tmp_path, [BAD], plan=False)
    try:
        await _nina_slot(client, fake)
        await _settle(mgr, cfg)
    finally:
        await _close(fake, client, task)
    assert fake.calibrations == 1
    assert pc.load_active(cfg, seed=False)["context"] == "other"
    assert len(pushes["notify"]) == 1 and "outside" in pushes["notify"][0]


async def test_mount_off_the_planned_field_is_not_the_plan(tmp_path, pushes):
    fake, client, task, mgr, cfg = await _setup(
        tmp_path, [BAD], nina_kw={"RightAscension": 2.55, "Declination": 61.5})
    try:
        await _nina_slot(client, fake)
        await _settle(mgr, cfg)
    finally:
        await _close(fake, client, task)
    assert fake.calibrations == 1 and pc.load_plan(cfg)["status"] == "pending"


async def test_no_time_left_in_the_hold_means_no_retry(tmp_path, pushes, monkeypatch):
    monkeypatch.setattr(cm, "RETRY_MIN_LEFT_S", 1000.0)
    fake, client, task, mgr, cfg = await _setup(tmp_path, [BAD, GOOD])
    try:
        await _nina_slot(client, fake)
        plan = await _settle(mgr, cfg)
    finally:
        await _close(fake, client, task)
    assert plan["status"] == "failed" and fake.calibrations == 1
    assert "hold" in pushes["notify"][0]


async def test_binning_change_mid_night_asks_for_a_recalibration(tmp_path, monkeypatch):
    import photonscript.scheduler.armer as armer_mod
    asked = []

    async def _recal(config, reason):
        asked.append(reason)
        return True
    monkeypatch.setattr(armer_mod, "request_recalibration", _recal)
    monkeypatch.setattr(armer_mod, "armer_guided_now", lambda cfg: True)
    fake, client, task, mgr, cfg = await _setup(tmp_path, [], plan=False)
    try:
        pc.save_record(cfg, {"t_utc": "2026-10-01T02:00:00Z", "grade": pc.PASS,
                             "binning": 1, "profile": "Primary RC Profile (Guider)"})
        assert await mgr.check_live("test") == "guide binning changed (1 -> 2)"
        pc.save_plan(cfg, {"status": "pending", "created_utc": store.iso_z(
            __import__("datetime").datetime.utcnow())})
        assert await mgr.check_live("test") is None      # a slot is already planned
        pc.save_plan(cfg, None)
        fake.calibration = {"calibrated": False}
        assert await mgr.check_live("test") == "PHD2 has no calibration"
        await fake.push({"Event": "ConfigurationChange"})
        await asyncio.sleep(0.2)
    finally:
        await _close(fake, client, task)
    assert asked[:2] == ["guide binning changed (1 -> 2)", "PHD2 has no calibration"]
    assert len(asked) == 3                                # the event re-checks too
    assert pc.load_live(cfg)["binning"] == 2


async def test_check_live_never_asks_for_a_recalibration_unguided(tmp_path, monkeypatch):
    """PS-66: PHD2 open and uncalibrated on an UNGUIDED night (or nothing
    armed): the invalidation is noted, the armer is never asked."""
    import json
    import photonscript.scheduler.armer as armer_mod
    asked = []

    async def _recal(config, reason):
        asked.append(reason)
        return True
    monkeypatch.setattr(armer_mod, "request_recalibration", _recal)
    import sys
    if "photonscript.scheduler.app" in sys.modules:       # read the state file
        monkeypatch.setattr(sys.modules["photonscript.scheduler.app"], "_armer", None)
    fake, client, task, mgr, cfg = await _setup(tmp_path, [], plan=False)
    try:
        st = tmp_path / "data" / "armer_state.json"
        st.parent.mkdir(parents=True, exist_ok=True)
        fake.calibration = {"calibrated": False}
        assert await mgr.check_live("test") == "PHD2 has no calibration"   # idle
        st.write_text(json.dumps({"state": "RUNNING", "guiding_override": "encoders"}))
        assert await mgr.check_live("test") == "PHD2 has no calibration"
        st.write_text(json.dumps({"state": "RUNNING", "guiding_override": "guided"}))
        assert await mgr.check_live("test") == "PHD2 has no calibration"
    finally:
        await _close(fake, client, task)
    assert asked == ["PHD2 has no calibration"]          # only the guided night


async def test_flip_check_runaway_alerts_and_a_clean_flip_is_verified(tmp_path, pushes):
    cfg = PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data")
    pc.save_record(cfg, {"t_utc": store.iso_z(__import__("datetime").datetime.utcnow()),
                         "grade": pc.PASS, "pier_side": "East"})
    mgr = cm.CalManager(cfg, None, FakeNina())

    def fr(errs, dirs, ms):
        return [{"t": 2.0 * i, "ra": 0.1, "dec": e, "ra_ms": 0, "ra_dir": "",
                 "dec_ms": ms if d else 0, "dec_dir": d, "drop": False,
                 "settling": False, "epoch": 0, "output": True}
                for i, (e, d) in enumerate(zip(errs, dirs))]
    run = fr([1 + 0.3 * i for i in range(90)], ["S"] * 90, 300)
    r = await mgr.flip_check(run, (20.0, 20.0), 0.25, "West", None)
    assert r["runaway"] is True
    assert pc.load_active(cfg, seed=False)["flip"]["West"]["ok"] is False
    assert len(pushes["notify"]) == 1 and "Reverse Dec" in pushes["notify"][0]
    await mgr.flip_check(run, (20.0, 20.0), 0.25, "West", None)
    assert len(pushes["notify"]) == 1                     # once per night
    calm = fr([0.3, -0.3] * 45, ["S", "N"] * 45, 60)
    await mgr.flip_check(calm, (20.0, 20.0), 0.25, "East", None)
    assert pc.load_active(cfg, seed=False)["flip"]["East"]["ok"] is True
    off = PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "d2",
                             phd2_flip_action="off")
    pc.save_record(off, {"t_utc": "2026-10-01T02:00:00Z", "grade": pc.PASS})
    await cm.CalManager(off, None, FakeNina()).flip_check(run, (20.0, 20.0), 0.25,
                                                          "West", None)
    assert len(pushes["notify"]) == 1
    assert pc.load_active(off, seed=False)["flip"]["West"]["ok"] is False


def test_calmanager_on_the_rc16_agent_only(tmp_path):
    from photonscript.telescope_agent.agent import TelescopeAgent
    cfg = PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "d")
    rc = TelescopeAgent(cfg, rig="rc16")
    pb = TelescopeAgent(cfg, rig="piggyback")
    rc._calmgr_setup()
    pb._calmgr_setup()
    assert isinstance(rc.calmgr, cm.CalManager) and pb.calmgr is None
    assert rc.calmgr.on_event in rc.phd2._event_listeners


async def test_client_tracks_calibration_events():
    c = PHD2Client()
    await c._handle_event({"Event": "StartCalibration"})
    assert c.app_state == "Calibrating"
    await c._handle_event({"Event": "CalibrationComplete"})
    assert c.app_state == "Guiding"
    await c._handle_event({"Event": "StartCalibration"})
    await c._handle_event({"Event": "CalibrationFailed", "Reason": "x"})
    assert c.app_state == "Stopped"
    assert str(c.metrics.state.value).lower() == "stopped"


async def test_agent_flip_watch_hands_the_window_to_the_calmanager(tmp_path, monkeypatch):
    """The PS-92 post-flip watch passes its 3 min window to flip_check."""
    from photonscript.telescope_agent.agent import TelescopeAgent
    from photonscript.telescope_agent.guide_guard import GuardContext
    import photonscript.shared.pushover as po

    async def _notify(cfg, msg, **kw):
        pass
    monkeypatch.setattr(po, "notify", _notify)
    a = TelescopeAgent(PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "d"))
    a._calmgr_setup()
    seen = []

    async def _flip(frames, rates, scale, pier, passive):
        seen.append((len(frames), pier, passive))
        return {"runaway": False}
    monkeypatch.setattr(a.calmgr, "flip_check", _flip)
    fr = [{"t": 1000.0 + 2 * i, "ra": 0.1, "dec": 0.1, "ra_ms": 0, "ra_dir": "",
           "dec_ms": 0, "dec_dir": "", "drop": False, "settling": False,
           "epoch": 0, "output": True} for i in range(120)]
    a.state.mount_side_of_pier = "East"
    await a._flip_watch(GuardContext(app_state="Guiding", now=990.0))
    a.state.mount_side_of_pier = "West"
    await a._flip_watch(GuardContext(app_state="Guiding", now=999.0))
    await a._flip_watch(GuardContext(frames=fr, app_state="Guiding", scale=0.25,
                                     rates_px_s=(20.0, 20.0), now=fr[-1]["t"]))
    assert seen == [(120, "West", None)]
