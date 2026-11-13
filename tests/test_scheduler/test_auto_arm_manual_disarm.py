"""2026-10-16: a manual disarm survives a restart; auto-arm inherits guiding.

Scheduler log, 2026-10-16 MDT: 20:26:16 manual disarm (POST /api/arm
armed=false), a deploy restarted the service, 20:27:05 the fresh auto-arm
loop armed guided (PHD2) and dispatched at 20:27:15. The operator had armed
unguided all week, and GET /api/arm showed no armed_by / armed_at.

Now: the manual disarm is persisted for tonight's night key (arm_memory.json),
every automatic arm (evening window and noon re-arm) skips with one logged
decision while it is on record, a manual Arm clears it, and the next night
arms normally. Auto-arms use the last manual Arm's guiding mode (config
default otherwise) and the armer records armed_by / armed_at.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from photonscript.scheduler import arm_memory as am
from photonscript.scheduler import auto_armer as aa
from photonscript.scheduler import sideload as sd
from photonscript.scheduler.armer import Armer
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.night_events import events_path
from photonscript.shared.phd2_store import night_of, read_jsonl

# 2026-10-16 20:27 MDT (UTC-6) = 2026-10-17 02:27Z: the restart's first tick
EVE = datetime(2026, 10, 17, 2, 27, 0)
DISARM_AT = datetime(2026, 10, 17, 2, 26, 16)
# 2026-10-17 12:30 MDT: the noon re-arm (night 2026-10-17)
NOON = datetime(2026, 10, 17, 18, 30, 0)

IDLE = ([{"Name": "Start", "Status": "FINISHED"},
         {"Name": "Targets", "Status": "CREATED"}], None)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _cfg(tmp_path, **kw):
    kw.setdefault("quality_eccentricity_max", 0.60)
    kw.setdefault("evening_forecast_enabled", False)
    kw.setdefault("auto_dusk_flats", False)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


class _FrozenDT(datetime):
    at = EVE

    @classmethod
    def utcnow(cls):
        return cls.at


class _Armer:
    def __init__(self):
        self.state = "DISARMED"
        self.armed = []
        self.armed_by = None

    async def arm(self, guiding=None, armed_by=None):
        self.armed.append(guiding)
        self.armed_by = armed_by
        self.state = "ARMED"
        return {"state": self.state}

    async def cooler_dew_off(self, skip_rigs=None):
        return {}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Ticks of run_auto_arm_loop at a frozen time; arm_memory sees the same
    clock (so a disarm "now" files under the frozen night)."""
    from photonscript.scheduler import night_plan, preflight
    from photonscript.shared import pushover
    _FrozenDT.at = EVE
    monkeypatch.setattr(aa, "datetime", _FrozenDT)
    monkeypatch.setattr(am, "datetime", _FrozenDT)

    def plan(c):
        now = _FrozenDT.at
        return {"night_of": night_of(c, now), "dusk_utc": None,
                "preconfig_utc": (now + timedelta(hours=1)).isoformat() + "Z"}
    monkeypatch.setattr(night_plan, "build_night_plan", plan)

    async def pf(c, **_kw):
        return {"go": True, "checks": []}
    monkeypatch.setattr(preflight, "run_preflight", pf)
    pushes = []

    async def notify(c, msg, title="", priority=0, sound="none", **_kw):
        pushes.append((title, msg))
        return True
    monkeypatch.setattr(pushover, "notify", notify)

    async def read(base, client=None):
        return IDLE
    monkeypatch.setattr(sd, "read_sequence_state", read)

    async def stop(_s):
        raise asyncio.CancelledError
    monkeypatch.setattr(aa.asyncio, "sleep", stop)

    def tick(cfg, armer):
        with pytest.raises(asyncio.CancelledError):
            _run(aa.run_auto_arm_loop(cfg, lambda: armer))
    return {"tick": tick, "pushes": pushes, "tmp": tmp_path}


def _decisions(cfg, now):
    return [r for r in read_jsonl(events_path(cfg, night_of(cfg, now)))
            if r.get("kind") == "auto_arm"]


# ------------------------------------------------ the 2026-10-16 restart

def test_restart_after_manual_disarm_does_not_auto_arm(env):
    cfg = _cfg(env["tmp"], auto_arm_enabled=True, noon_arm_enabled=False)
    first = Armer(cfg)
    first.state = "ARMED"                       # disarm path without make_safe
    _FrozenDT.at = DISARM_AT
    _run(first.disarm(manual=True))
    assert am.manual_disarm_tonight(cfg, EVE)["night"] == "2026-10-16"
    # deploy restart: a brand-new armer and a brand-new loop
    _FrozenDT.at = EVE
    second = Armer(cfg)
    assert second.restore() is False and second.state == "DISARMED"
    calls = []

    async def spy(**kw):
        calls.append(kw)
    second.arm = spy
    env["tick"](cfg, second)
    _FrozenDT.at = EVE + timedelta(minutes=5)
    env["tick"](cfg, second)                     # next tick: still skipped
    assert calls == [] and second.state == "DISARMED"
    ev = _decisions(cfg, EVE)
    assert [(e["value"], e["skip"]) for e in ev] == [("skipped", "manual_disarm")]
    assert "manual disarm" in aa.decision_chip(ev[-1])
    assert env["pushes"] == []                   # the operator chose this


def test_noon_rearm_skips_on_manual_disarm_same_night(env):
    cfg = _cfg(env["tmp"], auto_arm_enabled=False, noon_arm_enabled=True)
    _FrozenDT.at = NOON
    am.record_manual_disarm(cfg, NOON - timedelta(minutes=10))
    armer = _Armer()
    env["tick"](cfg, armer)
    assert armer.armed == []
    assert [e["skip"] for e in _decisions(cfg, NOON)] == ["manual_disarm"]


def test_manual_arm_clears_the_record(env, monkeypatch):
    from photonscript.scheduler import night_plan
    cfg = _cfg(env["tmp"], auto_arm_enabled=True, noon_arm_enabled=False)
    am.record_manual_disarm(cfg, DISARM_AT)
    a = Armer(cfg)
    # stop arm() at the plan step: the manual bookkeeping is done before it
    monkeypatch.setattr(night_plan, "build_night_plan",
                        lambda c: {"error": "test: no plan"})
    out = _run(a.arm(guiding="unguided"))
    assert out["state"] == "ERROR"
    assert am.manual_disarm_tonight(cfg, EVE) is None
    assert am.last_manual_arm(cfg)["guiding"] == "unguided"
    assert a.armed_by == "manual" and a.armed_at


def test_next_night_arms_normally(env):
    cfg = _cfg(env["tmp"], auto_arm_enabled=True, noon_arm_enabled=False)
    am.record_manual_disarm(cfg, DISARM_AT - timedelta(days=1))   # 10-15
    assert am.manual_disarm_tonight(cfg, EVE) is None
    armer = _Armer()
    env["tick"](cfg, armer)
    assert armer.armed == [None] and armer.armed_by == "auto-evening"
    # and a disarm in the small hours lapses at local noon
    am.record_manual_disarm(cfg, EVE + timedelta(hours=6))         # 02:27 MDT
    assert am.manual_disarm_tonight(cfg, EVE + timedelta(hours=9))  # 05:27 MDT
    assert am.manual_disarm_tonight(cfg, NOON) is None             # 12:30 MDT


# ------------------------------------------------ guiding + armed_by

@pytest.mark.parametrize("when,flags,trigger", [
    (EVE, dict(auto_arm_enabled=True, noon_arm_enabled=False), "auto-evening"),
    (NOON, dict(auto_arm_enabled=False, noon_arm_enabled=True), "auto-noon"),
])
def test_auto_arm_inherits_last_manual_guiding(env, when, flags, trigger):
    cfg = _cfg(env["tmp"], noon_arm_guided=True, guided_default=True, **flags)
    am.record_manual_arm(cfg, "unguided", when - timedelta(days=2))
    _FrozenDT.at = when
    armer = _Armer()
    env["tick"](cfg, armer)
    assert armer.armed == ["unguided"] and armer.armed_by == trigger
    ev = _decisions(cfg, when)
    assert ev[-1]["value"] == "armed"
    assert "guiding unguided (inherited from the last manual arm" in ev[-1]["detail"]


def test_auto_guiding_falls_back_to_config(tmp_path):
    cfg = _cfg(tmp_path, noon_arm_guided=False, guided_default=True)
    g, why = am.auto_guiding(cfg, "auto-noon")
    assert g == "unguided" and "noon_arm_guided" in why
    g, why = am.auto_guiding(cfg, "auto-evening")
    assert g is None and "guided_default -> guided" in why


def test_armed_by_persists_and_shows_in_status(tmp_path, monkeypatch):
    from photonscript.scheduler import night_plan
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(night_plan, "build_night_plan",
                        lambda c: {"error": "test: no plan"})
    a = Armer(cfg)
    _run(a.arm(guiding="guided", armed_by="auto-noon"))
    assert am.last_manual_arm(cfg) is None      # an auto arm is not "manual"
    b = Armer(cfg)
    b.restore()
    assert (b.armed_by, b.armed_at) == ("auto-noon", a.armed_at)
    st = b.status()
    assert st["armed_by"] == "auto-noon" and st["armed_at"] == a.armed_at
    assert st["manual_disarm"] is None
    am.record_manual_disarm(cfg)
    assert b.status()["manual_disarm"]["night"] == night_of(cfg, datetime.utcnow())


def test_api_disarm_is_manual(monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    seen = []

    class _A:
        async def disarm(self, manual=False):
            seen.append(manual)
            return {"state": "DISARMED"}
    monkeypatch.setattr(app, "_armer", _A())
    r = TestClient(app.app).post("/api/arm", json={"armed": False})
    assert r.status_code == 200 and seen == [True]
