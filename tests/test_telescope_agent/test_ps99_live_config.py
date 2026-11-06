"""PS-99: a System page save reaches the telescope agent without a restart
(shared config object in full mode), and a key changed mid-recovery or
mid-self-test applies after that step finishes."""
import asyncio
from types import SimpleNamespace

from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import guard_recovery as gr
from photonscript.telescope_agent import phd2_ops
from photonscript.telescope_agent.guide_guard import NON_STAR, Verdict


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data", **kw)


def _agent(cfg):
    from photonscript.telescope_agent.agent import TelescopeAgent
    a = TelescopeAgent(cfg)
    a.phd2 = SimpleNamespace(app_state="Guiding", lock_position=(20.0, 15.0),
                             connected=True, on_event=lambda cb: None)
    a._guard_cap = gr.RecoveryCap()
    return a


def _v():
    return [Verdict("D5", NON_STAR, "lock on a hot pixel", {"distance_px": 0.4})]


async def _async(v=None):
    return v


class _Req:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


async def _save(app, body):
    return await app.api_update_config(_Req(body))


def _env(monkeypatch, tmp_path):
    from photonscript.shared import envfile
    env = tmp_path / ".env"
    monkeypatch.setattr(envfile, "env_path", lambda root=None: env)
    return env


# ---- web app: shared object, allowlist, response ------------------------------

async def test_save_reaches_the_agent_and_says_applied_live(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", None)
    monkeypatch.setattr(app, "_agents_share_config", False)
    app.set_config(cfg)                     # what run_full does
    env = _env(monkeypatch, tmp_path)
    a = _agent(cfg)
    assert a._live_get("guard_auto_recover") is False
    res = await _save(app, {"PS_GUARD_AUTO_RECOVER": "true",
                            "PS_PHD2_TUNE_MODE": "exposure"})
    assert res["updated"] == 2 and res["restart_recommended"] is False
    assert sorted(res["applied_live"]) == ["PS_GUARD_AUTO_RECOVER",
                                           "PS_PHD2_TUNE_MODE"]
    assert a.config is cfg and cfg.guard_auto_recover is True
    a._live_sync()
    assert a._live_get("guard_auto_recover") is True
    assert a._live_get("phd2_tune_mode") == "exposure"
    assert "PS_GUARD_AUTO_RECOVER=true" in env.read_text(encoding="utf-8")
    # the System page no longer marks the four keys restart-only
    groups = await app.api_get_config()
    flags = {f["env"]: f["restart"] for g in groups for f in g["fields"]}
    for k in ("PS_GUARD_AUTO_RECOVER", "PS_GUARD_ON_FAIL",
              "PS_SELFTEST_ON_FAIL", "PS_PHD2_TUNE_MODE"):
        assert flags[k] is False


async def test_without_shared_agents_the_keys_still_say_restart(tmp_path, monkeypatch):
    """scheduler-only mode: the agents run elsewhere, so no live promise."""
    from photonscript.scheduler import app
    monkeypatch.setattr(app, "_config", _cfg(tmp_path))
    monkeypatch.setattr(app, "_agents_share_config", False)
    _env(monkeypatch, tmp_path)
    res = await _save(app, {"PS_GUARD_ON_FAIL": "unguided"})
    assert res["restart_recommended"] is True and res["applied_live"] == []
    groups = await app.api_get_config()
    flags = {f["env"]: f["restart"] for g in groups for f in g["fields"]}
    assert flags["PS_GUARD_ON_FAIL"] is True


async def test_off_allowlist_key_does_not_say_applied_live(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    monkeypatch.setattr(app, "_config", _cfg(tmp_path))
    monkeypatch.setattr(app, "_agents_share_config", True)
    _env(monkeypatch, tmp_path)
    res = await _save(app, {"PS_AF_STALL_ALERT": "false"})
    assert res["applied_live"] == [] and res["restart_recommended"] is True


# ---- agent latch ---------------------------------------------------------------

async def test_guard_on_fail_saved_mid_recovery_applies_to_the_next_one(
        tmp_path, monkeypatch):
    import photonscript.scheduler.armer as armer_mod
    import photonscript.telescope_agent.agent as agent_mod
    cfg = _cfg(tmp_path, guard_auto_recover=True)        # on_fail=alert
    a = _agent(cfg)
    fallbacks = []
    gate = asyncio.Event()

    async def _slow_fail(*x, **k):
        cfg.guard_on_fail = "unguided"       # the System page save lands now
        a._live_sync()                       # the poll loop ticks meanwhile
        await gate.wait()
        return {"ok": False, "detail": "no real star", "steps": []}
    monkeypatch.setattr(gr, "recover", _slow_fail)
    monkeypatch.setattr(agent_mod, "notify", lambda *x, **k: _async())
    monkeypatch.setattr(armer_mod, "request_fallback_unguided",
                        lambda c, reason: _async(fallbacks.append(reason)))
    t = asyncio.create_task(a._guard_act(_v()))
    await asyncio.sleep(0)
    assert a._live_busy() == "guard recovery"
    assert a._live_get("guard_on_fail") == "alert"
    gate.set()
    await t
    assert fallbacks == []                   # this recovery kept "alert"
    assert a._guard_recovering == 0 and a._live_busy() is None
    a._live_sync()
    assert a._live_get("guard_on_fail") == "unguided"

    async def _fail(*x, **k):
        return {"ok": False, "detail": "no real star", "steps": []}
    monkeypatch.setattr(gr, "recover", _fail)
    await a._guard_act(_v())
    assert len(fallbacks) == 1               # the next one falls back


async def test_latch_holds_while_a_self_test_holds_phd2(tmp_path):
    phd2_ops._reset_for_tests()
    cfg = _cfg(tmp_path)
    a = _agent(cfg)
    a._live_sync()
    async with phd2_ops.hold("selftest"):
        cfg.guard_auto_recover = True
        a._live_sync()
        assert a._live_get("guard_auto_recover") is False
    a._live_sync()
    assert a._live_get("guard_auto_recover") is True
    phd2_ops._reset_for_tests()


async def test_observe_only_turns_into_recovery_without_a_restart(tmp_path, monkeypatch):
    import photonscript.telescope_agent.agent as agent_mod
    cfg = _cfg(tmp_path)
    a = _agent(cfg)
    recovered = []
    monkeypatch.setattr(agent_mod, "notify", lambda *x, **k: _async())
    monkeypatch.setattr(gr, "recover", lambda *x, **k: _async(
        recovered.append(1) or {"ok": True, "detail": "reselected", "steps": []}))
    await a._guard_act(_v())
    assert recovered == []
    cfg.guard_auto_recover = True            # System page save
    await a._guard_act(_v())
    assert recovered == [1]
    eps = store.guard_episodes(cfg, store.night_of(cfg))
    assert eps and eps[0]["observe_only"] is True


async def test_tune_mode_off_to_observe_builds_the_tuner(tmp_path):
    cfg = _cfg(tmp_path, phd2_tune_mode="off")
    a = _agent(cfg)
    a._tuner_setup()
    a._live_sync()
    assert a.tuner is None
    a._running = True
    cfg.phd2_tune_mode = "observe"
    a._live_sync()
    assert a.tuner is not None and a.tuner.mode == "observe"


# ---- self-test latch -------------------------------------------------------------

async def test_selftest_on_fail_latched_at_the_start_of_the_run(tmp_path, monkeypatch):
    import photonscript.scheduler.armer as armer_mod
    from photonscript.telescope_agent import pulse_selftest as ps
    phd2_ops._reset_for_tests()
    cfg = _cfg(tmp_path, selftest_on_fail="unguided")
    fallbacks = []
    monkeypatch.setattr(armer_mod, "request_fallback_unguided",
                        lambda c, reason: _async(fallbacks.append(reason)))
    monkeypatch.setattr("photonscript.shared.pushover.notify",
                        lambda *x, **k: _async())

    class _Nina:
        async def get_mount_info(self):
            return {"Connected": True, "SideOfPier": "pierEast"}

    async def _run(*x, **k):
        cfg.selftest_on_fail = "alert"       # saved mid-test
        return ps._result("FAIL", ["RA pulses did not move the star"])
    monkeypatch.setattr(ps, "_run", _run)
    rec = await ps.run_selftest(cfg, context="manual", nina=_Nina())
    assert rec["verdict"] == "FAIL"
    assert fallbacks == ["pulse self-test FAIL"]   # the value at the start
    phd2_ops._reset_for_tests()
