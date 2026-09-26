"""Starter coverage for the arm state machine's decision logic.

The armer is the single largest untested risk surface (it decides whether the
rig images, pauses, and shuts down). This covers the pure/guarded pieces that
don't need a live NINA — guiding-mode resolution and the guided-but-not-guiding
watchdog — as a foundation to build the transition tests on. Uses the same
FakeNina-style injection as the telescope-agent watchdog tests.
"""
from datetime import datetime

import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.scheduler.armer import Armer


def _armer(**cfg):
    return Armer(PhotonScriptConfig(**cfg))


def test_use_guiding_override_wins_over_default():
    a = _armer(guided_default=False)
    a.guiding_override = "guided"
    assert a._use_guiding() is True
    a.guiding_override = "encoders"
    assert a._use_guiding() is False


def test_use_guiding_falls_back_to_config_default():
    a = _armer(guided_default=True)
    a.guiding_override = None
    assert a._use_guiding() is True
    b = _armer(guided_default=False)
    b.guiding_override = None
    assert b._use_guiding() is False


def test_guiding_watchdog_flag_starts_clear():
    assert _armer()._guiding_alerted is False


@pytest.mark.asyncio
async def test_watchdog_noop_when_unguided():
    """Armed unguided: the watchdog must not fire, nor even query NINA."""
    a = _armer(guided_default=False)
    a.guiding_override = "encoders"
    a.plan = {"dusk_utc": "2026-09-25T02:00:00Z"}
    calls = {"n": 0}

    async def _fake_nina(key, **kw):
        calls["n"] += 1
        return {"Response": {"Connected": True, "State": "Stopped"}}

    a._nina = _fake_nina
    await a._maybe_warn_not_guiding(datetime(2026, 9, 25, 3, 0, 0))  # dusk+60m
    assert calls["n"] == 0
    assert a._guiding_alerted is False


@pytest.mark.asyncio
async def test_watchdog_fires_once_when_guided_but_stopped(monkeypatch):
    """Armed guided but PHD2 idle 60 min after dark → exactly one alert."""
    a = _armer(guided_default=True)
    a.guiding_override = "guided"
    a.plan = {"dusk_utc": "2026-09-25T02:00:00Z"}

    async def _fake_nina(key, **kw):
        return {"Response": {"Connected": True, "State": "Stopped"}}

    a._nina = _fake_nina
    notes = []
    import photonscript.scheduler.armer as armer_mod

    async def _fake_notify(cfg, msg, **kw):
        notes.append(msg)

    monkeypatch.setattr(armer_mod, "notify", _fake_notify)
    now = datetime(2026, 9, 25, 3, 0, 0)
    await a._maybe_warn_not_guiding(now)
    assert len(notes) == 1 and "not guiding" in notes[0].lower()
    await a._maybe_warn_not_guiding(now)  # deduped — fires at most once/night
    assert len(notes) == 1


@pytest.mark.asyncio
async def test_watchdog_quiet_when_actually_guiding():
    """State 'Guiding' → no alert."""
    a = _armer(guided_default=True)
    a.guiding_override = "guided"
    a.plan = {"dusk_utc": "2026-09-25T02:00:00Z"}

    async def _fake_nina(key, **kw):
        return {"Response": {"Connected": True, "State": "Guiding"}}

    a._nina = _fake_nina
    await a._maybe_warn_not_guiding(datetime(2026, 9, 25, 3, 0, 0))
    assert a._guiding_alerted is False


@pytest.mark.asyncio
async def test_watchdog_holds_fire_before_grace():
    """Only 5 min after dusk → too early, no query/alert yet."""
    a = _armer(guided_default=True)
    a.guiding_override = "guided"
    a.plan = {"dusk_utc": "2026-09-25T02:00:00Z"}
    calls = {"n": 0}

    async def _fake_nina(key, **kw):
        calls["n"] += 1
        return {"Response": {"Connected": True, "State": "Stopped"}}

    a._nina = _fake_nina
    await a._maybe_warn_not_guiding(datetime(2026, 9, 25, 2, 5, 0))  # dusk+5m
    assert calls["n"] == 0
    assert a._guiding_alerted is False


async def _noop_sleep(*_a, **_k):
    return None


def _guided_armer(**cfg):
    a = _armer(guided_default=True, **cfg)
    a.guiding_override = "guided"
    a.plan = {"dusk_utc": "2026-09-25T02:00:00Z"}
    return a


@pytest.mark.asyncio
async def test_watchdog_escalates_and_auto_recovers(monkeypatch):
    """Sustained not-guiding climbs the ladder: one warn, one automatic guider
    restart, one priority escalation — the 2026-09-26 stuck-guiding failure."""
    import photonscript.scheduler.armer as armer_mod
    a = _guided_armer()
    keys = []

    async def _fake_nina(key, **kw):
        keys.append(key)
        if key == "guider":
            return {"Response": {"Connected": True, "State": "Stopped"}}
        return {"ok": True}  # guider_stop / guider_start

    a._nina = _fake_nina
    notes = []

    async def _fake_notify(cfg, msg, **kw):
        notes.append((msg, kw.get("priority")))

    monkeypatch.setattr(armer_mod, "notify", _fake_notify)
    monkeypatch.setattr(armer_mod.asyncio, "sleep", _noop_sleep)

    now = datetime(2026, 9, 25, 3, 0, 0)  # dusk+60m, well past grace
    for _ in range(armer_mod.GUIDING_ESCALATE_AFTER_TICKS):
        await a._maybe_warn_not_guiding(now)

    assert sum(1 for m, _ in notes if "likely trailing" in m) == 1  # warn once
    assert "guider_stop" in keys and "guider_start" in keys           # restarted
    assert sum(1 for m, _ in notes if "Auto-recovery" in m) == 1      # once
    assert any(p == 2 for _, p in notes)                              # escalated


@pytest.mark.asyncio
async def test_watchdog_no_autorecover_when_disabled(monkeypatch):
    """guiding_auto_recover=false → warn + escalate, but never touch the guider."""
    import photonscript.scheduler.armer as armer_mod
    a = _guided_armer(guiding_auto_recover=False)
    keys = []

    async def _fake_nina(key, **kw):
        keys.append(key)
        return {"Response": {"Connected": True, "State": "Stopped"}}

    a._nina = _fake_nina
    monkeypatch.setattr(armer_mod, "notify", lambda *a, **k: _noop_sleep())
    now = datetime(2026, 9, 25, 3, 0, 0)
    for _ in range(armer_mod.GUIDING_ESCALATE_AFTER_TICKS):
        await a._maybe_warn_not_guiding(now)
    assert "guider_start" not in keys and "guider_stop" not in keys
    assert a._guiding_escalated is True


@pytest.mark.asyncio
async def test_watchdog_tolerates_brief_working_state(monkeypatch):
    """A short calibrating blip must NOT warn (normal dither/settle); only a
    persistent stuck-calibration state trips."""
    import photonscript.scheduler.armer as armer_mod
    a = _guided_armer()

    async def _fake_nina(key, **kw):
        return {"Response": {"Connected": True, "State": "Calibrating"}}

    a._nina = _fake_nina
    notes = []

    async def _fake_notify(cfg, msg, **kw):
        notes.append(msg)

    monkeypatch.setattr(armer_mod, "notify", _fake_notify)
    now = datetime(2026, 9, 25, 3, 0, 0)
    for _ in range(armer_mod.GUIDING_WORKING_WARN_TICKS - 1):
        await a._maybe_warn_not_guiding(now)
    assert notes == []                       # tolerated so far
    await a._maybe_warn_not_guiding(now)     # crosses the working-warn threshold
    assert any("stuck" in m for m in notes)  # now it warns, with the cal hint


@pytest.mark.asyncio
async def test_watchdog_resets_and_renotifies_on_recovery(monkeypatch):
    """Not guiding (warn), then locked again → 'recovered' note + episode reset,
    so the watchdog re-arms for a later failure the same night."""
    import photonscript.scheduler.armer as armer_mod
    a = _guided_armer()
    state = {"v": "Stopped"}

    async def _fake_nina(key, **kw):
        return {"Response": {"Connected": True, "State": state["v"]}}

    a._nina = _fake_nina
    notes = []

    async def _fake_notify(cfg, msg, **kw):
        notes.append(msg)

    monkeypatch.setattr(armer_mod, "notify", _fake_notify)
    now = datetime(2026, 9, 25, 3, 0, 0)
    await a._maybe_warn_not_guiding(now)     # warns
    assert a._guiding_alerted is True
    state["v"] = "Guiding"
    await a._maybe_warn_not_guiding(now)     # recovers
    assert any("recovered" in m.lower() for m in notes)
    assert a._guiding_alerted is False and a._not_locked_ticks == 0
