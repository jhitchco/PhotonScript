"""PS-91 recovery from a non-star lock, against the shared fake PHD2, and the
agent's episode handling (observe-only default, alert once, fallback only
when configured)."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import guard_recovery as gr
from photonscript.telescope_agent import phd2_ops
from photonscript.telescope_agent.guide_guard import NON_STAR, Verdict
from photonscript.telescope_agent.phd2_client import PHD2Client
from tests.fakes.fake_phd2 import FakePHD2, SimField

HOT = (20.0, 15.0)


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data", **kw)


def _hotpix(field):
    return {"binning": 2, "pixels": [[int(x), int(y), v] for x, y, v in field.hot]}


@pytest.fixture
async def locked_on_hot_pixel(tmp_path):
    """PHD2 guiding on the hot pixel at HOT (what auto-select picked)."""
    phd2_ops._reset_for_tests()
    field = SimField.default(n_stars=6)
    f = FakePHD2(tmp_path / "phd2tmp", field=field)
    port = await f.start()
    c = PHD2Client("127.0.0.1", port, config=_cfg(tmp_path))
    assert await c.connect()
    task = await c.start_event_loop()
    await c.find_star([HOT[0] - 4, HOT[1] - 4, 8, 8])     # the hot pixel
    await c.loop()
    await c.start_guiding()
    for _ in range(50):
        await asyncio.sleep(0.02)
        if c.app_state == "Guiding" and not c.settling:
            break
    yield f, c, field
    await c.disconnect()
    task.cancel()
    await f.close()


async def test_recovery_reselects_a_real_star_not_the_hot_pixel(tmp_path,
                                                                locked_on_hot_pixel):
    f, c, field = locked_on_hot_pixel
    assert c.lock_position == pytest.approx(HOT)
    res = await gr.recover(c, _cfg(tmp_path), _hotpix(field), target="Heart",
                           cap=gr.RecoveryCap(), settle_s=0.2)
    assert res["ok"], res
    lock = res["lock"]
    assert abs(lock[0] - HOT[0]) > 5 or abs(lock[1] - HOT[1]) > 5
    stars = [(x + field.offset[0], y + field.offset[1]) for x, y, _p, _s in field.stars]
    assert min(abs(lock[0] - x) + abs(lock[1] - y) for x, y in stars) < 2
    m = f.methods()
    i = len(m) - 1 - m[::-1].index("stop_capture")
    assert m[i:] [:2] == ["stop_capture", "loop"]
    roi = next(r["params"][0] for r in reversed(f.requests) if r["method"] == "find_star")
    assert roi[2] == roi[3] == 16                          # tight ROI
    assert m[-1] == "guide" and f.app_state == "Guiding"
    assert not phd2_ops.busy()


async def test_recovery_refused_while_settling_or_flipping(tmp_path, locked_on_hot_pixel):
    f, c, field = locked_on_hot_pixel
    cfg = _cfg(tmp_path)
    await f.push({"Event": "SettleBegin"})
    await asyncio.sleep(0.05)
    f.settling = False   # the fake would end it on its own; hold it here
    res = await gr.recover(c, cfg, _hotpix(field), target="T", cap=gr.RecoveryCap())
    assert res["skipped"] == "PHD2 is settling"
    await f.push({"Event": "SettleDone", "Status": 0})
    await asyncio.sleep(0.05)
    res = await gr.recover(c, cfg, _hotpix(field), target="T", cap=gr.RecoveryCap(),
                           flip_running=True)
    assert res["skipped"] == "a meridian flip is running"
    res = await gr.recover(c, cfg, _hotpix(field), target="T", cap=gr.RecoveryCap(),
                           slewing=True)
    assert res["skipped"] == "mount is slewing"
    async with phd2_ops.hold("selftest"):
        res = await gr.recover(c, cfg, _hotpix(field), target="T", cap=gr.RecoveryCap())
    assert "busy" in res["skipped"]
    assert "stop_capture" not in f.methods()


def test_recovery_cap_per_target_per_hour():
    cap = gr.RecoveryCap()
    for t in (0.0, 10.0):
        assert cap.allow("Heart", now=t)
        cap.note("Heart", now=t)
    assert not cap.allow("Heart", now=20.0)
    assert cap.allow("Cat's Eye", now=20.0)
    assert cap.allow("Heart", now=3601.0)


async def test_no_real_star_fails_cleanly(tmp_path):
    phd2_ops._reset_for_tests()
    field = SimField(stars=[], hot=[(20.0, 15.0, 30000.0), (100.0, 60.0, 20000.0)])
    f = FakePHD2(tmp_path / "t", field=field)
    port = await f.start()
    c = PHD2Client("127.0.0.1", port, config=_cfg(tmp_path))
    await c.connect()
    task = await c.start_event_loop()
    try:
        await c.loop()
        await c.start_guiding()
        await asyncio.sleep(0.3)
        res = await gr.recover(c, _cfg(tmp_path), _hotpix(field), target="T",
                               cap=gr.RecoveryCap())
        assert res["ok"] is False and "no real star" in res["detail"]
        assert "guide" not in f.methods()[2:]   # never resumed on a hot pixel
    finally:
        await c.disconnect()
        task.cancel()
        await f.close()


# ---- agent episode handling --------------------------------------------------

def _agent(tmp_path, **cfg):
    from photonscript.telescope_agent.agent import TelescopeAgent
    a = TelescopeAgent(_cfg(tmp_path, **cfg))
    a.phd2 = SimpleNamespace(app_state="Guiding", lock_position=(20.0, 15.0),
                             connected=True)
    a._guard_cap = gr.RecoveryCap()
    return a


def _v():
    return [Verdict("D5", NON_STAR, "lock on a hot pixel", {"distance_px": 0.4})]


async def test_observe_only_logs_an_episode_alerts_once_and_never_acts(tmp_path,
                                                                       monkeypatch):
    import photonscript.telescope_agent.agent as agent_mod
    a = _agent(tmp_path)
    pushes, recovered = [], []
    monkeypatch.setattr(agent_mod, "notify",
                        lambda cfg, msg, **kw: _async(pushes.append(msg)))
    monkeypatch.setattr(gr, "recover", lambda *x, **k: _async(recovered.append(1)))
    await a._guard_act(_v())
    await a._guard_act(_v())                  # same episode
    for _ in range(a.GUARD_CLOSE_TICKS):
        await a._guard_act([])               # clean ticks close it
    await a._guard_act(_v())                  # a second episode, same night
    night = store.night_of(a.config)
    eps = store.guard_episodes(a.config, night)
    assert len(eps) == 2 and eps[0]["end_utc"] and eps[1]["end_utc"] is None
    assert eps[0]["observe_only"] is True and eps[0]["codes"] == ["D5"]
    assert len(pushes) == 1 and "Observe-only" in pushes[0]
    assert recovered == []
    # the open episode marks a sub shot now as guided on a non-star
    from datetime import datetime, timedelta
    start = datetime.utcnow() - timedelta(seconds=30)
    assert a._guide_lock_for(start, 300, "guiding") == "non-star"
    assert a._guide_lock_for(start - timedelta(minutes=20), 60, "guiding") == "star"
    assert a._guide_lock_for(start - timedelta(minutes=20), 60, "stopped") is None


async def _async(v=None):
    return v


async def test_failed_recovery_alerts_once_and_falls_back_only_when_configured(
        tmp_path, monkeypatch):
    import photonscript.scheduler.armer as armer_mod
    import photonscript.telescope_agent.agent as agent_mod
    fallbacks, pushes = [], []

    async def _fail(*x, **k):
        return {"ok": False, "detail": "no real star", "steps": ["stop_capture"]}
    monkeypatch.setattr(gr, "recover", _fail)
    monkeypatch.setattr(agent_mod, "notify",
                        lambda cfg, msg, **kw: _async(pushes.append(msg)))
    monkeypatch.setattr(armer_mod, "request_fallback_unguided",
                        lambda cfg, reason: _async(fallbacks.append(reason)))
    a = _agent(tmp_path, guard_auto_recover=True)          # default on_fail=alert
    await a._guard_act(_v())
    await a._guard_act(_v())
    assert fallbacks == [] and len(pushes) == 1 and "recovery failed" in pushes[0]
    recs = store.guard_records(a.config, store.night_of(a.config))
    assert [r["ok"] for r in recs if r["event"] == "recovery"] == [False, False]
    b = _agent(tmp_path / "b", guard_auto_recover=True, guard_on_fail="unguided")
    await b._guard_act(_v())
    assert len(fallbacks) == 1 and "no real star" in fallbacks[0]


def test_defaults_are_observe_only_and_alert_only():
    c = PhotonScriptConfig(_env_file=None)
    assert c.guard_enabled is True and c.guard_auto_recover is False
    assert c.guard_on_fail == "alert" and c.qa_guide_lock_mode == "warn"


async def test_agent_tick_end_to_end_on_a_hot_pixel_lock(tmp_path, locked_on_hot_pixel):
    """The agent's own tick against the fake: D5 from the hot-pixel map, an
    observe-only episode, nothing sent to PHD2 beyond reads."""
    from photonscript.telescope_agent.agent import TelescopeAgent
    f, c, field = locked_on_hot_pixel
    cfg = _cfg(tmp_path)
    store.write_json(store.hotpix_path(cfg), _hotpix(field))
    a = TelescopeAgent(cfg)
    a.phd2 = c
    a._guard_setup()
    await c.refresh_pixel_scale()
    n = len(f.requests)
    await a._guard_tick()
    eps = store.guard_episodes(cfg, store.night_of(cfg))
    assert len(eps) == 1 and "D5" in eps[0]["codes"] and eps[0]["observe_only"]
    sent = {r["method"] for r in f.requests[n:]}
    assert sent <= {"get_calibration_data", "get_star_image", "get_app_state"}
