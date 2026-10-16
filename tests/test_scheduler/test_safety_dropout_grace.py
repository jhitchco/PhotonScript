"""Safety dropout grace (2026-10-16).

An UNREADABLE safety read (ninaAPI error / timeout, monitor not Connected) is
a dropout: inside safety_dropout_grace_s (and before 2 unreadable reads in a
row) nothing reacts; past it the normal unsafe path runs (pause, PS-77 stop /
park, operator-pause park) with one push. A REPORTED unsafe (Connected,
IsSafe false) still acts on the first tick, grace or not.
"""
from datetime import datetime, timedelta

import pytest

import photonscript.scheduler.armer as armer_mod
from photonscript.scheduler.armer import PAUSE_STATE, Armer
from photonscript.shared.config import PhotonScriptConfig

TREE_IMAGING = [{"GlobalTriggers": []},
                {"Name": "Targets_Container", "Status": "RUNNING", "Items": [
                    {"Name": "LOOP_ALL_NIGHT_Container", "Status": "RUNNING",
                     "Items": [{"Name": "SAFE_LOOP_Container", "Status": "RUNNING",
                                "Items": []},
                               {"Name": "UNSAFE_Container", "Status": "CREATED",
                                "Items": []}]}]}]

T0 = datetime(2026, 9, 27, 6, 0, 0)   # mid-night, dawn 11:48Z


def _armer(tmp_path, monkeypatch, state="RUNNING", **cfg):
    a = Armer(PhotonScriptConfig(data_dir=str(tmp_path), **cfg))
    a.state = state
    a.plan = {"dawn_utc": "2026-09-27T11:48:14Z", "dusk_utc": "2026-09-27T02:26:50Z",
              "night_of": "2026-09-26"}
    calls, notes = [], []
    # safety: "safe" | "unsafe" | "error" (API failed) | "disconnected"
    env = {"safety": "safe", "tree": TREE_IMAGING}
    clock = {"now": T0}

    async def fake_nina(key, method="GET", json_body=None, **kw):
        calls.append(key)
        if key == "safety":
            s = env["safety"]
            if s == "error":
                return None
            if s == "disconnected":
                return {"Response": {"Connected": False, "IsSafe": False}}
            return {"Response": {"Connected": True, "IsSafe": s == "safe"}}
        if key == "sequence_json":
            return {"Response": env["tree"]}
        return {"Response": "ok"}

    async def fake_notify(cfg_, msg, **kw):
        notes.append((kw.get("title"), msg, kw.get("priority", 0)))

    async def noop(*a_, **k_):
        return None

    class _DT(datetime):
        @classmethod
        def utcnow(cls):
            return clock["now"]

    a._nina = fake_nina
    for name in ("_maybe_warn_not_guiding", "_unguided_guard", "_reconcile_cooler",
                 "_crosscheck_unsafe", "_close_piggy_cover", "_maybe_hotpix_map"):
        setattr(a, name, noop)
    a._ledger_hourly = lambda now: None
    from photonscript.scheduler import stuck_loop
    monkeypatch.setattr(stuck_loop, "armer_tick", lambda armer, now: None)
    monkeypatch.setattr(armer_mod, "notify", fake_notify)
    monkeypatch.setattr(armer_mod, "datetime", _DT)
    return a, calls, notes, env, clock


async def _ticks(a, clock, seconds):
    """One _tick at each offset (s) from T0."""
    for s in seconds:
        clock["now"] = T0 + timedelta(seconds=s)
        await a._tick()


# --- a real unsafe is never delayed ------------------------------------------

@pytest.mark.asyncio
async def test_reported_unsafe_pauses_on_the_first_tick(tmp_path, monkeypatch):
    a, calls, notes, env, clock = _armer(tmp_path, monkeypatch,
                                         safety_dropout_grace_s=600)
    env["safety"] = "unsafe"
    await _ticks(a, clock, [0])
    assert a.state == "PAUSED_UNSAFE"
    assert notes[-1][0] == "PhotonScript paused" and notes[-1][2] == 1
    assert notes[-1][1].startswith("PAUSED: unsafe.")


@pytest.mark.asyncio
async def test_reported_unsafe_during_a_dropout_is_not_delayed(tmp_path, monkeypatch):
    a, calls, notes, env, clock = _armer(tmp_path, monkeypatch,
                                         safety_dropout_grace_s=600)
    env["safety"] = "error"
    await _ticks(a, clock, [0, 30])
    assert a.state == "RUNNING"                    # dropout, inside the grace
    env["safety"] = "unsafe"
    await _ticks(a, clock, [60])
    assert a.state == "PAUSED_UNSAFE"              # the real unsafe acts at once
    assert a._dropout_since is None                # the dropout episode ended


@pytest.mark.asyncio
async def test_real_unsafe_still_stops_imaging_after_unsafe_stop_grace(tmp_path,
                                                                       monkeypatch):
    a, calls, notes, env, clock = _armer(tmp_path, monkeypatch)
    env["safety"] = "unsafe"
    await _ticks(a, clock, [0, 150])
    assert calls[-3:] == ["sequence_stop", "guider_stop", "mount_park"]
    assert a._unsafe_stopped


# --- a dropout inside the grace does nothing -----------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["error", "disconnected"])
async def test_dropout_within_grace_does_not_pause_or_park(tmp_path, monkeypatch, kind):
    a, calls, notes, env, clock = _armer(tmp_path, monkeypatch)
    env["safety"] = kind
    await _ticks(a, clock, [0, 30])                # 30 s < 60 s grace
    assert a.state == "RUNNING"
    assert "mount_park" not in calls and "sequence_stop" not in calls
    assert not [n for n in notes if n[2] == 1]     # no priority push
    env["safety"] = "safe"
    await _ticks(a, clock, [60])
    assert a.state == "RUNNING" and a._dropout_since is None
    events = (tmp_path / "runs").glob("*_events.jsonl")
    text = "".join(p.read_text() for p in events)
    assert '"safety_dropout"' in text and '"start"' in text and '"end"' in text


@pytest.mark.asyncio
async def test_one_slow_read_alone_never_counts(tmp_path, monkeypatch):
    """Grace measured in time AND reads: a single unreadable read is never
    past the grace, however long the tick took."""
    a, calls, notes, env, clock = _armer(tmp_path, monkeypatch)
    env["safety"] = "error"
    await _ticks(a, clock, [0])
    assert a.state == "RUNNING"
    assert await a._effective_safe(None, T0 + timedelta(seconds=59)) is None


@pytest.mark.asyncio
async def test_dropout_within_grace_does_not_park_an_operator_pause(tmp_path,
                                                                    monkeypatch):
    a, calls, notes, env, clock = _armer(tmp_path, monkeypatch, state=PAUSE_STATE,
                                         unsafe_stop_grace_s=0)
    a.pause_info = {"phase": "paused", "parked": False}
    env["safety"] = "error"
    await _ticks(a, clock, [0, 30])
    assert "mount_park" not in calls and not a.pause_info.get("parked")


# --- past the grace it is the unsafe path ----------------------------------------

@pytest.mark.asyncio
async def test_dropout_beyond_grace_pauses_then_parks(tmp_path, monkeypatch):
    a, calls, notes, env, clock = _armer(tmp_path, monkeypatch)
    env["safety"] = "error"
    await _ticks(a, clock, [0, 30])
    assert a.state == "RUNNING"
    await _ticks(a, clock, [60])                   # 60 s, 3 reads: past the grace
    assert a.state == "PAUSED_UNSAFE"
    over = [n for n in notes if n[0] == "PhotonScript safety"]
    assert len(over) == 1 and "past the 60 s dropout grace" in over[0][1]
    assert over[0][2] == 1
    assert "dropout grace" in notes[-1][1]         # the pause push says why
    assert "sequence_stop" not in calls
    await _ticks(a, clock, [90, 150, 185])          # unsafe_stop_grace_s 120
    assert calls[-3:] == ["sequence_stop", "guider_stop", "mount_park"]
    assert a._unsafe_stopped
    assert len([n for n in notes if n[0] == "PhotonScript safety"]) == 1  # once
    # the monitor comes back safe: the episode ends, the resume path runs
    env["safety"] = "safe"
    await _ticks(a, clock, [215])
    assert a._dropout_since is None
    assert a.detail.startswith("Safe again, confirming")


@pytest.mark.asyncio
async def test_dropout_beyond_grace_parks_an_operator_pause(tmp_path, monkeypatch):
    a, calls, notes, env, clock = _armer(tmp_path, monkeypatch, state=PAUSE_STATE,
                                         unsafe_stop_grace_s=0)
    a.pause_info = {"phase": "paused", "parked": False}
    env["safety"] = "error"
    await _ticks(a, clock, [0, 30, 60])
    assert "mount_park" in calls and a.pause_info["parked"] is True


@pytest.mark.asyncio
async def test_dropout_action_alert_pushes_but_does_not_pause(tmp_path, monkeypatch):
    a, calls, notes, env, clock = _armer(tmp_path, monkeypatch,
                                         safety_dropout_action="alert")
    env["safety"] = "error"
    await _ticks(a, clock, [0, 30, 60, 90, 300])
    assert a.state == "RUNNING" and "mount_park" not in calls
    over = [n for n in notes if n[0] == "PhotonScript safety"]
    assert len(over) == 1 and "alert only" in over[0][1]


@pytest.mark.asyncio
async def test_zero_grace_counts_the_first_unreadable_read(tmp_path, monkeypatch):
    a, calls, notes, env, clock = _armer(tmp_path, monkeypatch,
                                         safety_dropout_grace_s=0)
    env["safety"] = "error"
    await _ticks(a, clock, [0])
    assert a.state == "PAUSED_UNSAFE"


@pytest.mark.asyncio
async def test_unreadable_reads_far_apart_are_separate_dropouts(tmp_path, monkeypatch):
    """No carry-over: a dropout left open (armer idle, last night) must not
    count as past the grace on the next unreadable read."""
    a, calls, notes, env, clock = _armer(tmp_path, monkeypatch)
    assert await a._effective_safe(None, T0) is None
    later = T0 + timedelta(hours=10)
    assert await a._effective_safe(None, later) is None
    assert a._dropout_since == later and a._dropout_reads == 1


@pytest.mark.asyncio
async def test_watch_mode_dropout_within_grace_does_not_alert(tmp_path, monkeypatch):
    a, calls, notes, env, clock = _armer(tmp_path, monkeypatch, state="WATCHING")
    a.watch = {}

    async def read_state():
        return None, "unreadable"

    a._watch_read_state = read_state
    a._shutdown_due_at = lambda plan=None: T0 + timedelta(hours=5)
    env["safety"] = "error"
    await _ticks(a, clock, [0, 30])
    assert not a.watch.get("paused") and not notes
    await _ticks(a, clock, [60])
    assert a.watch.get("paused")
