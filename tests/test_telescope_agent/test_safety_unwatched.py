"""PS-151: no hourly 'Safety monitor DISCONNECTED' pushes for a rig that is not
part of tonight.

2026-10-02/03 the nanny pushed DISCONNECTED every hour while NINA #2 was not
running at all. A rig whose NINA is unreachable, or that is up but idle with
nothing planned for it, gets ONE informational push per night; the hourly
SEVERE reminder stays for a rig that is expected to be imaging (sideload
tonight, RC16 armed/watching, or its NINA running a sequence).
"""

import asyncio
import json

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import TelescopeState


class FakeNina:
    def __init__(self, up=True, seq_state="IDLE", seq_error=False):
        self.up = up
        self.seq_state = seq_state
        self.seq_error = seq_error
        self.calls = []

    async def get_safety_info(self):
        if not self.up:
            raise ConnectionError("connection refused")
        return {"Connected": False}

    async def read_sequence_state(self, base_url, client=None):
        """Stands in for scheduler.sideload.read_sequence_state."""
        self.calls.append(f"seq:{base_url}")
        if not self.up or self.seq_error:
            return None, "ConnectError: refused"
        if self.seq_state == "RUNNING":
            return [{"Name": "Lights", "Status": "RUNNING"}], None
        return [{"Name": "Lights", "Status": "FINISHED"}], None

    async def connect_safety(self, device_id=None):
        self.calls.append("connect")
        if not self.up:
            raise ConnectionError("connection refused")

    async def disconnect_safety(self):
        self.calls.append("disconnect")

    async def stop_sequence(self):
        self.calls.append("stop")


def _agent(monkeypatch, tmp_path, nina, rig="piggyback", sideload=None,
           armer=None, night="2026-10-02"):
    from photonscript.scheduler import auto_armer, sideload as sd
    from photonscript.shared import phd2_store
    from photonscript.telescope_agent import agent as agent_mod
    cfg = PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                             piggyback_enabled=True,
                             safety_disconnect_aborts=False,
                             auto_abort_on_severe=False)
    if armer:
        (tmp_path / "armer_state.json").write_text(json.dumps({"state": armer}))
    a = agent_mod.TelescopeAgent.__new__(agent_mod.TelescopeAgent)
    a.config = cfg
    a.rig = rig
    a.nina = nina
    a.state = TelescopeState()
    a._safety_bad_since = None
    a._safety_bad_reads = 0
    a._safety_fix_attempts = 0
    a._safety_last_attempt = 0.0
    a._safety_last_escalate = 0.0
    a._safety_aborted = False
    a._alerted = set()
    sent = []

    async def fake_notify(config, msg, **kw):
        sent.append((msg, kw.get("priority", 0)))
    monkeypatch.setattr(agent_mod, "notify", fake_notify)

    async def no_sleep(_):
        pass
    monkeypatch.setattr(agent_mod.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(agent_mod.TelescopeAgent, "_safety_watch_needed",
                        lambda self: True)
    monkeypatch.setattr(sd, "read_sequence_state", nina.read_sequence_state)
    seen_rigs = []

    def fake_sideload(config, now=None, rig=None):
        seen_rigs.append(rig)
        return sideload
    monkeypatch.setattr(auto_armer, "sideload_tonight", fake_sideload)
    a._night = [night]
    monkeypatch.setattr(phd2_store, "night_of", lambda config, when=None: a._night[0])
    a._seen_rigs = seen_rigs
    return a, sent


def _hour_passes(a):
    """Put the watchdog past debounce + grace with an escalation due."""
    a._safety_bad_since = -1e9
    a._safety_bad_reads = 2
    a._safety_last_escalate = -1e9
    a._safety_last_attempt = -1e9


def _run_hours(a, n):
    for _ in range(n):
        _hour_passes(a)
        asyncio.run(a._safety_monitor_watchdog())


def _severe(sent):
    return [m for m, p in sent if "Safety monitor DISCONNECTED" in m]


def test_nina2_not_running_pushes_once_per_night(monkeypatch, tmp_path):
    a, sent = _agent(monkeypatch, tmp_path, FakeNina(up=False))
    _run_hours(a, 6)
    assert _severe(sent) == []
    assert len(sent) == 1
    msg, prio = sent[0]
    assert msg.startswith("NINA #2 (Piggy-600) is not running; its safety "
                          "monitor is not watched tonight")
    assert prio == 0                                   # informational
    assert a._seen_rigs[0] == "piggyback"              # rig's own sideloads
    a._night[0] = "2026-10-03"                         # next night: one more
    _run_hours(a, 2)
    assert len(sent) == 2 and _severe(sent) == []


def test_unwatched_message_is_ascii_and_dashless(monkeypatch, tmp_path):
    a, sent = _agent(monkeypatch, tmp_path, FakeNina(up=False))
    _run_hours(a, 1)
    assert sent and sent[0][0].isascii()


def test_up_but_idle_rig_with_nothing_planned_pushes_once(monkeypatch, tmp_path):
    a, sent = _agent(monkeypatch, tmp_path, FakeNina(up=True, seq_state="IDLE"))
    _run_hours(a, 4)
    assert _severe(sent) == [] and len(sent) == 1
    assert "not running a sequence" in sent[0][0]


def test_running_sequence_keeps_hourly_reminder(monkeypatch, tmp_path):
    a, sent = _agent(monkeypatch, tmp_path, FakeNina(up=True, seq_state="RUNNING"))
    _run_hours(a, 3)
    assert len(_severe(sent)) == 3
    assert all(p == 1 for _, p in sent)


def test_sideload_tonight_keeps_hourly_even_if_nina_down(monkeypatch, tmp_path):
    a, sent = _agent(monkeypatch, tmp_path, FakeNina(up=False),
                     sideload={"ok": True, "rig": "piggyback"})
    _run_hours(a, 2)
    assert len(_severe(sent)) == 2


def test_rc16_armed_keeps_hourly_even_if_nina_down(monkeypatch, tmp_path):
    for state in ("ARMED", "RUNNING", "WATCHING"):
        a, sent = _agent(monkeypatch, tmp_path, FakeNina(up=False), rig="rc16",
                         armer=state)
        _run_hours(a, 2)
        assert len(_severe(sent)) == 2, state


def test_armer_does_not_make_piggyback_expected(monkeypatch, tmp_path):
    # the companion's own RUNNING sequence is what counts for NINA #2
    a, sent = _agent(monkeypatch, tmp_path, FakeNina(up=False), armer="RUNNING")
    _run_hours(a, 3)
    assert _severe(sent) == [] and len(sent) == 1


def test_rc16_unarmed_and_down_pushes_once(monkeypatch, tmp_path):
    a, sent = _agent(monkeypatch, tmp_path, FakeNina(up=False), rig="rc16",
                     armer="IDLE")
    _run_hours(a, 3)
    assert _severe(sent) == [] and len(sent) == 1
    assert sent[0][0].startswith("NINA #1 (RC16) is not running")


def test_unreadable_sequence_state_fails_safe(monkeypatch, tmp_path):
    a, sent = _agent(monkeypatch, tmp_path, FakeNina(up=True, seq_error=True))
    _run_hours(a, 2)
    assert len(_severe(sent)) == 2


def test_unreadable_events_fail_safe(monkeypatch, tmp_path):
    from photonscript.scheduler import auto_armer
    a, sent = _agent(monkeypatch, tmp_path, FakeNina(up=False))

    def boom(*a_, **k):
        raise OSError("disk")
    monkeypatch.setattr(auto_armer, "sideload_tonight", boom)
    _run_hours(a, 1)
    assert len(_severe(sent)) == 1


def test_rig_joining_the_night_gets_reminder_within_recheck(monkeypatch, tmp_path):
    n = FakeNina(up=True, seq_state="IDLE")
    a, sent = _agent(monkeypatch, tmp_path, n)
    _run_hours(a, 1)
    assert len(sent) == 1 and _severe(sent) == []
    # quiet: the next check is due SAFETY_QUIET_RECHECK_S later, not an hour
    repeat_s = 60 * a.config.safety_disconnect_repeat_min
    import time
    due_in = a._safety_last_escalate + repeat_s - time.monotonic()
    assert 0 < due_in <= a.SAFETY_QUIET_RECHECK_S + 1
    n.seq_state = "RUNNING"                             # someone starts imaging
    a._safety_last_escalate -= a.SAFETY_QUIET_RECHECK_S + 1
    asyncio.run(a._safety_monitor_watchdog())
    assert len(_severe(sent)) == 1


def test_reads_this_rigs_own_nina(monkeypatch, tmp_path):
    from photonscript.shared.rigs import PIGGYBACK, rig_config
    n = FakeNina(up=True, seq_state="IDLE")
    a, _ = _agent(monkeypatch, tmp_path, n)
    a.config = rig_config(a.config, PIGGYBACK)
    _run_hours(a, 1)
    assert f"seq:{a.config.piggyback_nina_base_url}" in n.calls


def test_reconnect_still_attempted_when_unwatched(monkeypatch, tmp_path):
    # scope: only the pushes change; the reconnect watchdog keeps working
    n = FakeNina(up=True, seq_state="IDLE")
    a, _ = _agent(monkeypatch, tmp_path, n)
    _run_hours(a, 1)
    assert "connect" in n.calls
