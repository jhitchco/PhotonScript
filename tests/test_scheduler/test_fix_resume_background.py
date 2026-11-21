"""Fix 2026-10-13: since the restart-race fix, Resume and Restart tonight
from now (from PAUSED_OPERATOR) waited for NINA #1 to wind down (up to
STOP_IDLE_MAX_WAIT_S, about 10 min) inside the HTTP request. The
re-dispatch now runs in the background like _finish_pause: the request
answers at once ("waiting for NINA #1"), pause.phase is "resuming" with
pause.resume.step / restart.step as progress, and the dashboard shows it."""
import asyncio

from photonscript.scheduler import armer as armer_mod
from photonscript.scheduler.armer import PAUSE_STATE, Armer
from tests.test_scheduler.test_1013_restart_race import race  # noqa: F401 - fixture
from tests.test_scheduler.test_ps181_arm_recipe import armer  # noqa: F401 - fixture
from tests.test_scheduler.test_ps64_pause_panel import (_call, _paused, api,  # noqa: F401
                                                         env)


def _gate_sleeps(a, monkeypatch):
    """NINA #1's wind-down polls block until the test releases them."""
    gate = asyncio.Event()

    async def _sleep(s):
        a.sleeps.append(s)
        await gate.wait()
    monkeypatch.setattr(armer_mod, "_armer_sleep", _sleep)
    return gate


async def _paused_race(a):
    a.state = "RUNNING"
    fake = a.use()
    assert (await a.pause())["ok"]
    await a._pause_task
    assert a.state == PAUSE_STATE
    return fake


async def test_resume_answers_before_nina1_has_stopped(race, monkeypatch):  # noqa: F811
    a = race
    fake = await _paused_race(a)
    fake.left = 50                              # NINA #1 still winding down
    gate = _gate_sleeps(a, monkeypatch)
    res = await asyncio.wait_for(a.resume(), 1)
    assert res["ok"] and res["pending"] and "waiting for NINA #1" in res["detail"]
    assert a.state == PAUSE_STATE and a.pause_info["phase"] == "resuming"
    await asyncio.sleep(0.01)                    # the task polls once, then waits
    step = a.status()["pause"]["resume"]["step"]
    assert step.startswith("waiting for NINA #1 to stop (") and "Targets" in step
    assert "sequence_load" not in a.calls
    # a second press while it runs is refused, and so is a Restart
    again = await a.resume()
    assert again["ok"] is False and "already re-dispatching" in again["detail"]
    rs = await a.restart()
    assert rs["ok"] is False and "already re-dispatching" in rs["detail"]
    fake.left = 0
    gate.set()
    await asyncio.wait_for(a._pause_task, 5)
    assert a.state == "RUNNING" and a.pause_info is None
    assert a.calls.count("sequence_load") == 1


async def test_restart_from_a_pause_answers_at_once(race, monkeypatch):  # noqa: F811
    a = race
    fake = await _paused_race(a)
    fake.left = 50
    gate = _gate_sleeps(a, monkeypatch)
    res = await asyncio.wait_for(a.restart(), 1)
    assert res["ok"] and res["pending"]
    await asyncio.sleep(0.01)
    rst = a.status()["restart"]
    assert rst["pending"] and rst["step"].startswith("waiting for NINA #1 to stop")
    fake.left = 0
    gate.set()
    await asyncio.wait_for(a._pause_task, 5)
    assert a.state == "RUNNING" and a.last_restart["ok"] is True


async def test_where_panel_shows_the_background_resume(api, monkeypatch):  # noqa: F811
    a = api.armer
    r = await _call(api.app, "POST", "/api/arm/pause", {"piggy": "keep"})
    await a._pause_task
    gate = asyncio.Event()

    async def _slow(companion=True, fail_state="ERROR"):
        a._dispatch_step("waiting for NINA #1 to stop (30 s, Targets_Container)")
        await gate.wait()
        return True
    monkeypatch.setattr(a, "_dispatch_and_start", _slow)
    r = await _call(api.app, "POST", "/api/arm/resume")
    assert r.status_code == 200 and r.json()["pending"] is True
    await asyncio.sleep(0)
    w = (await _call(api.app, "GET", "/api/night/where")).json()["armer"]
    assert w["state"] == PAUSE_STATE and w["pause"]["phase"] == "resuming"
    assert w["pause"]["resume"]["step"].startswith("waiting for NINA #1 to stop (30 s")
    assert w["can_resume"] is False and w["can_restart"] is False
    gate.set()
    await a._pause_task
    w = (await _call(api.app, "GET", "/api/night/where")).json()["armer"]
    assert w["state"] == "RUNNING"


async def test_restore_mid_resume_stays_paused(env):  # noqa: F811
    a = await _paused(env)
    a.pause_info["phase"] = "resuming"
    a.pause_info["resume"] = {"requested": "2026-10-13T04:00:00Z", "step": "x"}
    a._persist()
    b = Armer(a.config)
    assert b.restore() is True
    try:
        assert b.state == PAUSE_STATE and b.pause_info["phase"] == "paused"
        assert "resume" not in b.pause_info and b._pause_task is None
        assert "Resume again" in b.detail
    finally:
        b._task.cancel()


async def test_resume_failure_in_the_background_stays_paused(env):  # noqa: F811
    a = await _paused(env)
    a.fake["dispatch_ok"] = False
    res = await a.resume()
    assert res["ok"] and res["pending"]
    await a._pause_task
    assert a.state == PAUSE_STATE and a.pause_info["phase"] == "paused"
    assert "resume" not in a.pause_info and "Resume FAILED" in a.detail
    # Resume again works
    a.fake["dispatch_ok"] = True
    assert (await a.resume())["ok"]
    await a._pause_task
    assert a.state == "RUNNING"


def test_night_panel_shows_resuming():
    from pathlib import Path
    js = (Path(__file__).resolve().parents[2] / "photonscript/scheduler/static/js/"
          "night_panel.js").read_text(encoding="utf-8")
    assert "'resuming'" in js and "RESUMING" in js and "job.step" in js
