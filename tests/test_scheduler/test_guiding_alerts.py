"""PS-66: collapse the guided-but-not-guiding Pushover flood.

Replays the 2026-09-26 pattern (PHD2 flapping LostLock <-> Guiding every
minute or two for hours) through the armer watchdog and checks that the phone
gets a handful of pushes while the audit still records every event.
"""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from photonscript.scheduler.armer import Armer
from photonscript.scheduler.guiding_alerts import GuidingAlertGate
from photonscript.shared import pushover as po
from photonscript.shared.config import PhotonScriptConfig

T0 = datetime(2026, 9, 27, 3, 0, 0)


def _gate(**kw):
    return GuidingAlertGate(SimpleNamespace(**kw))


# --- pure gate ---------------------------------------------------------------

def test_first_loss_pushes_then_repeats_are_held():
    g = _gate()
    assert g.on_lost(T0) == "push"
    assert g.on_lost(T0 + timedelta(minutes=2)) == "suppress"


def test_flap_summary_replaces_the_next_push():
    g = _gate(guiding_flap_count=3)
    assert g.on_lost(T0) == "push"
    for m in (2, 4, 6, 8, 20, 29):
        assert g.on_lost(T0 + timedelta(minutes=m)) == "suppress"
    # budget due again with 8 losses in the last hour: one summary, not a
    # plain "lost" push
    assert g.on_lost(T0 + timedelta(minutes=31)) == "flap"
    assert "8 losses" in g.flap_message(T0 + timedelta(minutes=31))
    assert g.on_lost(T0 + timedelta(minutes=35)) == "suppress"
    # quiet hour later: an isolated loss is a plain push again
    assert g.on_lost(T0 + timedelta(minutes=200)) == "push"


def test_flap_window_forgets_old_losses():
    g = _gate(guiding_flap_count=3, guiding_flap_window_min=60)
    g.on_lost(T0)
    g.on_lost(T0 + timedelta(minutes=10))
    assert g.losses_in_window(T0 + timedelta(minutes=65)) == 1


def test_recovered_pushes_only_after_a_long_loss():
    g = _gate(guiding_recovered_push_min=10)
    g.on_lost(T0, since=T0 - timedelta(minutes=1))
    push, mins = g.on_recovered(T0 + timedelta(minutes=2))
    assert push is False and 2.9 < mins < 3.1
    g.on_lost(T0 + timedelta(minutes=40))
    push, mins = g.on_recovered(T0 + timedelta(minutes=52))
    assert push is True and mins >= 10


def test_auto_recover_shares_the_budget():
    g = _gate(guiding_alert_repeat_min=30)
    assert g.on_lost(T0) == "push"
    # same continuous episode as a pushed loss: the ladder story goes out
    assert g.on_auto_recover(T0 + timedelta(minutes=3)) is True
    g.on_recovered(T0 + timedelta(minutes=4))
    assert g.on_lost(T0 + timedelta(minutes=5)) == "suppress"
    assert g.on_auto_recover(T0 + timedelta(minutes=8)) is False   # held
    # a FAILED restart still gets through, once per window
    assert g.on_auto_recover(T0 + timedelta(minutes=9), ok=False) is True
    assert g.on_auto_recover(T0 + timedelta(minutes=12), ok=False) is False
    assert g.on_auto_recover(T0 + timedelta(minutes=34)) is True


def test_escalation_once_per_window():
    g = _gate()
    assert g.on_escalate(T0) is True
    assert g.on_escalate(T0 + timedelta(minutes=16)) is False
    assert g.on_escalate(T0 + timedelta(minutes=30)) is True


def test_bad_config_values_fall_back_to_defaults():
    g = _gate(guiding_alert_repeat_min="junk", guiding_flap_count=None)
    assert g.repeat == timedelta(minutes=30) and g.flap_count == 3


# --- armer watchdog replay ------------------------------------------------------

def _armer():
    a = Armer(PhotonScriptConfig(guided_default=True))
    a.guiding_override = "guided"
    a.plan = {"dusk_utc": "2026-09-27T02:26:50Z"}
    return a


def _wire(monkeypatch, a, states):
    import photonscript.scheduler.armer as armer_mod
    it = iter(states)
    cur = {"v": "Guiding"}

    async def _fake_nina(key, **kw):
        if key == "guider":
            cur["v"] = next(it)
            return {"Response": {"Connected": True, "State": cur["v"]}}
        return {"ok": True}

    async def _noop(*_a, **_k):
        return None

    pushed, held = [], []

    async def _notify(cfg, msg, **kw):
        pushed.append((msg, kw.get("priority", 0)))

    a._nina = _fake_nina
    monkeypatch.setattr(armer_mod, "notify", _notify)
    monkeypatch.setattr(armer_mod, "record",
                        lambda cfg, msg, **kw: held.append((msg, kw)))
    monkeypatch.setattr(armer_mod.asyncio, "sleep", _noop)
    return pushed, held


@pytest.mark.asyncio
async def test_replay_2026_09_26_flap_collapses(monkeypatch):
    """Two hours of LostLock for one tick, then Guiding for 1-5 min (the
    2026-09-26 pattern). Old code: one lost + one recovered push per flap.
    New: a first push, one flap summary per 30 min, nothing else; every event
    is still audited."""
    a = _armer()
    pattern = []
    gaps = [2, 4, 6, 10, 3, 8]            # ticks locked between losses
    while len(pattern) < 240:              # 240 ticks x 30 s = 2 h
        for g in gaps:
            pattern += ["LostLock"] + ["Guiding"] * g
    pattern = pattern[:240]
    pushed, held = _wire(monkeypatch, a, pattern)
    now = T0
    for _ in pattern:
        await a._maybe_warn_not_guiding(now)
        now += timedelta(seconds=30)
    losses = pattern.count("LostLock")
    assert losses >= 30
    lost_pushes = [m for m, _ in pushed if "likely trailing" in m]
    flap_pushes = [m for m, _ in pushed if m.startswith("Guiding flapping")]
    assert len(lost_pushes) == 1           # the first loss only
    assert 1 <= len(flap_pushes) <= 4      # then one summary per 30 min
    assert not any(m.startswith("Guiding recovered") for m, _ in pushed)
    assert len(pushed) <= 5                # vs ~2 x losses before PS-66
    # nothing hidden: every loss and every recovery is pushed or audited
    audited_lost = [m for m, _ in held if "likely trailing" in m]
    audited_rec = [m for m, kw in held if "recovered" in m.lower()]
    assert len(lost_pushes) + len(audited_lost) == losses
    assert all(kw.get("priority") == -1 for m, kw in held if "recovered" in m.lower())
    assert len(audited_rec) >= losses - 1


@pytest.mark.asyncio
async def test_continuous_loss_still_escalates_and_recovery_pushes(monkeypatch):
    """Lost for 12 min straight: warn, auto-recovery, priority-2 escalation,
    then a pushed 'recovered' because the outage was long."""
    import photonscript.scheduler.armer as armer_mod
    a = _armer()
    n = armer_mod.GUIDING_ESCALATE_AFTER_TICKS + 14   # 24 ticks = 12 min
    pushed, held = _wire(monkeypatch, a, ["Stopped"] * n + ["Guiding"])
    now = T0
    for _ in range(n + 1):
        await a._maybe_warn_not_guiding(now)
        now += timedelta(seconds=30)
    msgs = [m for m, _ in pushed]
    assert sum("likely trailing" in m for m in msgs) == 1
    assert sum(m.startswith("Auto-recovery") for m in msgs) == 1
    assert any(p == 2 for _, p in pushed)
    assert any("recovered" in m.lower() for m in msgs)
    assert held == []


@pytest.mark.asyncio
async def test_rearm_resets_flap_history(monkeypatch):
    a = _armer()
    pushed, _ = _wire(monkeypatch, a, ["LostLock", "Guiding", "LostLock"])
    await a._maybe_warn_not_guiding(T0)
    await a._maybe_warn_not_guiding(T0 + timedelta(seconds=30))
    a._guiding_gate = GuidingAlertGate(a.config)   # what arm() does
    await a._maybe_warn_not_guiding(T0 + timedelta(seconds=60))
    assert sum("likely trailing" in m for m, _ in pushed) == 2


# --- pushover: emergency params + audit-only record ---------------------------

def test_emergency_payload_has_retry_and_expire():
    cfg = SimpleNamespace(pushover_api_token="t", pushover_user_key="u")
    p1 = po._payload(cfg, "m", "T", 1, "none")
    assert "retry" not in p1 and "expire" not in p1
    p2 = po._payload(cfg, "m", "T", 2, "none")
    assert p2["retry"] == 300 and p2["expire"] == 1800
    cfg2 = SimpleNamespace(pushover_api_token="t", pushover_user_key="u",
                           pushover_emergency_retry_s=5,
                           pushover_emergency_expire_s=99999)
    p3 = po._payload(cfg2, "m", "T", 2, "none")
    assert p3["retry"] == 30 and p3["expire"] == 10800


def test_record_audits_without_sending(tmp_path, monkeypatch):
    sent = []

    async def fake_send(*a, **k):
        sent.append(a)
        return True

    monkeypatch.setattr(po, "_send_raw", fake_send)
    cfg = SimpleNamespace(data_dir=str(tmp_path))
    po.record(cfg, "Guiding recovered (lost for 1 min).",
              title="PhotonScript guiding", priority=-1,
              reason="guiding-short-loss")
    rows = [json.loads(x) for x in
            (tmp_path / "notifications.jsonl").read_text().splitlines()]
    assert sent == []
    assert rows[-1]["sent"] is False and rows[-1]["priority"] == -1
    assert rows[-1]["reason"] == "guiding-short-loss"
