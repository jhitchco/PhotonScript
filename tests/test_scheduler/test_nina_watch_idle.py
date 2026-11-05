"""NINA watchdog: IDLE WHILE ARMED (2026-11-05).

2026-11-04 at 23:05:51 MST NINA #1 logged "Sequence run was cancelled"
during a slew (a manual Stop) and sat idle until the roof closed at 02:34.
The armer stayed RUNNING and nothing paged: about 3.5 h of RC16 time lost.

  1. idle_blocker() / classify(): every guard, the minutes, the Piggy-600;
  2. the monitor's idle clock and the per-outage latch;
  3. tick() replaying the 11-04 night: one priority push about 15 min after
     the Stop that says what to do, a clear when the roof closes;
  4. no page during a pause, WATCHING, unsafe, a re-dispatch, a calibration
     capture job, after dawn or with the sequence state unknown;
  5. the night pilot's opt-in restart (off by default, once per night);
  6. the armer's dispatch-in-flight marker, config, API, dashboard chip.
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from photonscript.scheduler import night_pilot as npil
from photonscript.scheduler import nina_watchdog as nw
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.night_events import events_path
from photonscript.shared.phd2_store import night_of, read_jsonl

ROOT = Path(__file__).resolve().parents[2]
# 2026-11-04 (MST, UTC-7): astro dusk 18:49, dawn 05:15 local
DUSK = datetime(2026, 11, 5, 1, 49)
DAWN = datetime(2026, 11, 5, 12, 15)
STOP = datetime(2026, 11, 5, 6, 5, 51)       # 23:05:51 local, the Stop
ROOF = datetime(2026, 11, 5, 9, 34)          # 02:34 local, roof closed


def _cfg(tmp_path, **kw):
    kw.setdefault("piggyback_enabled", True)
    kw.setdefault("pushover_ratelimit_enabled", False)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _idle_reads(**kw):
    r = {"window": True, "expected": True, "api_ok": True, "process": True,
         "log_quiet_min": 0.0, "seq_leaf": "", "leaf_age_min": None,
         "night_running": True, "mount_parked": False, "mount_tracking": True,
         "tracked_tonight": True, "safe": True, "past_dusk_min": 200.0,
         "idle_rig": True, "armer_running": True, "in_dark": True,
         "idle_hold": None, "idle_for_min": 16.0}
    r.update(kw)
    return r


# ------------------------------------------------------- 1. classify (pure)

def test_classify_idle_running_after_the_limit():
    st, why = nw.classify(_idle_reads(), 15.0)
    assert st == "idle_running"
    assert "no sequence has run for 16 min" in why and "RUNNING" in why
    st, why = nw.classify(_idle_reads(idle_for_min=14.0), 15.0)
    assert st == "ok" and "pages at 15" in why
    assert nw.classify(_idle_reads(idle_for_min=30.0), 15.0, idle_min=45.0)[0] == "ok"


@pytest.mark.parametrize("kw,blocker", [
    ({"armer_running": False}, "armer not RUNNING"),      # pause / unsafe / WATCHING
    ({"in_dark": False}, "outside astro dusk to dawn"),   # dusk wait, dawn flats
    ({"safe": False}, "safety monitor not SAFE"),
    ({"safe": None}, "safety monitor not SAFE"),          # unreadable: no page
    ({"seq_leaf": None}, "sequence state unknown"),
    ({"seq_leaf": "Take Exposure"}, "a sequence runs"),
    ({"idle_hold": "the armer is (re-)dispatching"}, "the armer is (re-)dispatching"),
    ({"idle_rig": False}, "rig not judged for idle"),     # Piggy-600
])
def test_every_guard_keeps_it_quiet(kw, blocker):
    r = _idle_reads(**kw)
    assert nw.idle_blocker(r) == blocker
    assert nw.classify(r, 15.0)[0] != "idle_running"


def test_api_down_is_not_idle_and_parked_still_wins():
    r = _idle_reads(api_ok=False, process=True, log_quiet_min=1.0)
    assert nw.classify(r, 15.0)[0] == "api_down"
    # never tracked tonight and parked: the PS-154 page, not this one
    r = _idle_reads(mount_parked=True, mount_tracking=False, tracked_tonight=False)
    assert nw.classify(r, 15.0)[0] == "parked"


# ------------------------------------------------------- 2. monitor

def test_idle_clock_restarts_on_any_tick_that_does_not_count():
    m = nw.NinaWatch()
    t = STOP
    assert m.idle_track("rc16", t, True) == 0.0
    assert m.idle_track("rc16", t + timedelta(minutes=10), True) == 10.0
    assert m.idle_track("rc16", t + timedelta(minutes=11), False) is None
    assert m.idle_track("rc16", t + timedelta(minutes=12), True) == 0.0


def test_latch_once_per_outage_and_rearms_after_ok():
    m = nw.NinaWatch(rearm_min=10.0)
    n = "2026-11-04"
    res = m.update("rc16", "idle_running", "x", {}, n, STOP)
    assert res["alert"] and res["state"] == "idle_running"   # no extra debounce
    assert not m.update("rc16", "idle_running", "x", {}, n,
                        STOP + timedelta(minutes=1))["alert"]
    rec = m.update("rc16", "ok", "API answers", {}, n, STOP + timedelta(minutes=2))
    assert rec["recovered"] and rec["prev"] == "idle_running"
    # a short flap is the same outage
    assert not m.update("rc16", "idle_running", "x", {}, n,
                        STOP + timedelta(minutes=3))["alert"]
    for i in range(4, 16):
        m.update("rc16", "ok", "API answers", {}, n, STOP + timedelta(minutes=i))
    assert m.update("rc16", "idle_running", "x", {}, n,
                    STOP + timedelta(minutes=17))["alert"]


# ------------------------------------------------------- 3. the 11-04 night

def _night_read(st):
    async def read(cfg, rig):
        return {"api_ok": True, "api_error": "", "process": True, "pid": 4242,
                "log_file": "n.log", "log_mtime": st["now"], "log_size": 1,
                "closed_at": None,
                "seq_leaf": st["leaf"] if rig == "rc16" else st["pig_leaf"],
                "seq_exposure_s": None,
                **({"mount_parked": False, "mount_tracking": True,
                    "safe": st["safe"]} if rig == "rc16" else {})}
    return read


def _replay(cfg, start, minutes, script, mon=None, restart=None, hold=None):
    """script(now, st) -> armer state; one tick a minute."""
    pushes, states = [], []

    async def notify(c, msg, title="", priority=0, **kw):
        pushes.append({"msg": msg, "priority": priority, **kw})
        return True
    mon = mon or nw.NinaWatch()
    st = {"leaf": "Take Exposure", "pig_leaf": "", "safe": True, "now": start}
    for i in range(minutes):
        now = start + timedelta(minutes=i)
        st["now"] = now
        armer = script(now, st)
        res = asyncio.run(nw.tick(
            cfg, armer, now=now, read=_night_read(st), notify=notify,
            window=True, monitor=mon, dusk_utc=DUSK.isoformat() + "Z",
            dawn_utc=DAWN.isoformat() + "Z",
            idle_hold=hold(now) if hold else None, restart=restart))
        states.append((now, res["rc16"]["state"]))
    return pushes, states, mon


def _nov4(now, st):
    if now >= ROOF:
        st["safe"] = False
        return "PAUSED_UNSAFE"
    st["leaf"] = "Take Exposure" if now < STOP else ""
    return "RUNNING"


def test_replay_2026_11_04_one_page_with_what_to_do(tmp_path):
    cfg = _cfg(tmp_path)
    start = STOP - timedelta(minutes=30)
    pushes, states, mon = _replay(cfg, start, 240, _nov4)
    rc = [p for p in pushes if "NINA #1" in p["msg"]]
    assert len(rc) == 2                          # the page and its clear
    page, clear = rc
    assert "IDLE WHILE ARMED" in page["msg"]
    assert "Restart tonight from now on the dashboard" in page["msg"]
    assert "Pause on the dashboard" in page["msg"]
    assert "idle since 23:05 local" in page["msg"]
    assert "dark until 05:15 local" in page["msg"]
    assert page["priority"] == 1 and page["kind"] == "act"
    assert page["safety"] is False               # PS-212: a plain act
    assert chr(0x2014) not in page["msg"]
    first = next(t for t, s in states if s == "idle_running")
    assert timedelta(minutes=14) <= first - STOP <= timedelta(minutes=16)
    assert "cleared" in clear["msg"] and "PAUSED_UNSAFE" in clear["msg"]
    assert clear["kind"] == "info"
    # the Piggy-600 (idle companion) never pages
    assert not any("NINA #2" in p["msg"] for p in pushes)
    ev = [e for e in read_jsonl(events_path(cfg, night_of(cfg, STOP)))
          if e.get("kind") == "nina_watch" and e.get("rig") == "rc16"]
    vals = [e["value"] for e in ev]
    i = vals.index("idle_running")
    assert vals[i:i + 2] == ["idle_running", "ok"]
    v = mon.view()["rigs"]["rc16"]["reads"]
    assert isinstance(v.get("dawn_utc"), str)


def test_the_running_sequence_coming_back_is_a_recovery(tmp_path):
    cfg = _cfg(tmp_path, piggyback_enabled=False)

    def script(now, st):
        st["leaf"] = "" if now < STOP + timedelta(minutes=20) else "Take Exposure"
        return "RUNNING"
    pushes, _, _ = _replay(cfg, STOP, 25, script)
    assert len(pushes) == 2
    assert "running a sequence again ('Take Exposure')" in pushes[1]["msg"]


# ------------------------------------------------------- 4. false alarms

@pytest.mark.parametrize("armer", ["PAUSED_OPERATOR", "PAUSED_UNSAFE",
                                   "WATCHING", "ARMED", "COMPLETE"])
def test_no_page_unless_the_armer_is_running(tmp_path, armer):
    cfg = _cfg(tmp_path, piggyback_enabled=False)

    def script(now, st):
        st["leaf"] = ""
        return armer
    pushes, states, _ = _replay(cfg, STOP, 60, script)
    assert not any(s == "idle_running" for _, s in states)
    assert not any("IDLE" in p["msg"] for p in pushes)


def test_unsafe_unreadable_and_unknown_never_page(tmp_path):
    cfg = _cfg(tmp_path, piggyback_enabled=False)
    for safe, leaf in ((False, ""), (None, ""), (True, None)):
        def script(now, st, safe=safe, leaf=leaf):
            st["leaf"], st["safe"] = leaf, safe
            return "RUNNING"
        pushes, _, _ = _replay(cfg, STOP, 40, script)
        assert pushes == [], (safe, leaf)


def test_a_redispatch_or_calibration_job_restarts_the_clock(tmp_path):
    cfg = _cfg(tmp_path, piggyback_enabled=False)

    def script(now, st):
        st["leaf"] = ""
        return "RUNNING"
    # NINA #1 stopped for 20 min by a re-dispatch, then idle 10 more: quiet
    hold_until = STOP + timedelta(minutes=20)
    pushes, states, _ = _replay(
        cfg, STOP, 30, script,
        hold=lambda now: "the armer is (re-)dispatching" if now < hold_until else None)
    assert pushes == []
    # ... and it pages 15 min after the hold ended
    pushes, states, _ = _replay(
        cfg, STOP, 40, script,
        hold=lambda now: "calibration capture running on the rc16" if now < hold_until else None)
    first = next(t for t, s in states if s == "idle_running")
    assert first - hold_until == timedelta(minutes=15)


def test_dusk_wait_and_dawn_flats_are_outside_the_dark_span(tmp_path):
    cfg = _cfg(tmp_path, piggyback_enabled=False)

    def script(now, st):
        st["leaf"] = ""
        return "RUNNING"
    pushes, _, _ = _replay(cfg, DUSK - timedelta(minutes=60), 59, script)
    assert pushes == []
    pushes, _, _ = _replay(cfg, DAWN, 60, script)   # flats done, shutdown held
    assert pushes == []
    # an idle spell that runs into dawn stops counting there
    pushes, states, _ = _replay(cfg, DAWN - timedelta(minutes=10), 30, script)
    assert pushes == []


def test_idle_hold_reason_reads_the_armer(monkeypatch):
    from photonscript.scheduler.armer import Armer
    monkeypatch.setattr(Armer, "_calibration_busy", staticmethod(lambda: None))
    a = SimpleNamespace(dispatch_in_flight=lambda: True)
    assert nw.idle_hold_reason(a) == "the armer is (re-)dispatching"
    a = SimpleNamespace(dispatch_in_flight=lambda: False)
    assert nw.idle_hold_reason(a) is None
    monkeypatch.setattr(Armer, "_calibration_busy", staticmethod(lambda: "rc16"))
    assert "calibration capture" in nw.idle_hold_reason(a)
    assert "calibration capture" in nw.idle_hold_reason(object())


# ------------------------------------------------------- 5. night pilot restart

class _FakeArmer:
    def __init__(self, state="RUNNING", dawn=DAWN, ok=True):
        self.state = state
        self.plan = {"night_of": "2026-11-04"}
        self._dawn_t = dawn
        self.ok = ok
        self.calls = []

    def _dawn(self):
        return self._dawn_t

    async def restart(self, when="after_exposure", now=None):
        self.calls.append(when)
        return {"ok": self.ok, "detail": "refused here" if not self.ok else "x",
                "pending": True}


def test_pilot_restart_is_off_by_default(tmp_path):
    cfg = _cfg(tmp_path)
    assert cfg.nina_watch_idle_restart is False
    assert not npil.idle_restart_on(cfg)
    assert nw._pilot_restart(cfg, _FakeArmer()) is None
    a = _FakeArmer()
    assert asyncio.run(npil.idle_restart(cfg, a, STOP)) == {"text": "", "acted": False}
    assert a.calls == []
    # night_pilot_mode advise caps it
    cfg = _cfg(tmp_path, nina_watch_idle_restart=True, night_pilot_mode="advise")
    assert not npil.idle_restart_on(cfg)


def test_pilot_restart_once_per_night(tmp_path, monkeypatch):
    monkeypatch.setattr(npil, "_calibration_busy", lambda: None)
    cfg = _cfg(tmp_path, nina_watch_idle_restart=True, night_pilot_mode="act")
    a = _FakeArmer()
    now = STOP + timedelta(minutes=15)
    r = asyncio.run(npil.idle_restart(cfg, a, now))
    assert r["acted"] and "Restart tonight from now requested" in r["text"]
    assert a.calls == ["after_exposure"]
    assert json.loads((tmp_path / "night_pilot" / "2026-11-04_idle_restart.json")
                      .read_text())["t"].startswith("2026-11-05T06:20")
    r = asyncio.run(npil.idle_restart(cfg, a, now + timedelta(hours=1)))
    assert not r["acted"] and "already restarted tonight" in r["text"]
    assert a.calls == ["after_exposure"]
    ev = [e for e in read_jsonl(events_path(cfg, "2026-11-04"))
          if e.get("kind") == "night_pilot"]
    assert [e["value"] for e in ev] == ["act", "held"]


def test_pilot_restart_guards(tmp_path, monkeypatch):
    monkeypatch.setattr(npil, "_calibration_busy", lambda: None)
    cfg = _cfg(tmp_path, nina_watch_idle_restart=True)
    a = _FakeArmer(state="PAUSED_OPERATOR")
    r = asyncio.run(npil.idle_restart(cfg, a, STOP))
    assert not r["acted"] and "held (armer is PAUSED_OPERATOR)" in r["text"]
    a = _FakeArmer(dawn=STOP + timedelta(minutes=30))
    r = asyncio.run(npil.idle_restart(cfg, a, STOP))
    assert not r["acted"] and "min of dark left" in r["text"] and a.calls == []
    a = _FakeArmer(ok=False)
    r = asyncio.run(npil.idle_restart(cfg, a, STOP))
    assert not r["acted"] and "refused (refused here)" in r["text"]
    # a refusal is not "restarted tonight": the next outage may try again
    assert not (tmp_path / "night_pilot" / "2026-11-04_idle_restart.json").exists()
    monkeypatch.setattr(npil, "_calibration_busy", lambda: "rc16")
    r = asyncio.run(npil.idle_restart(cfg, _FakeArmer(), STOP))
    assert "calibration capture job" in r["text"]


def test_tick_hands_the_page_to_the_pilot(tmp_path):
    cfg = _cfg(tmp_path, piggyback_enabled=False)
    calls = []

    async def restart(c, now):
        calls.append(now)
        return {"text": " Night pilot: Restart tonight from now requested.",
                "acted": True}

    def script(now, st):
        st["leaf"] = ""
        return "RUNNING"
    pushes, _, _ = _replay(cfg, STOP, 30, script, restart=restart)
    assert len(calls) == 1 and len(pushes) == 1
    msg = pushes[0]["msg"]
    assert msg.endswith("Night pilot: Restart tonight from now requested.")
    assert "PhotonScript changed nothing" not in msg
    assert pushes[0]["kind"] == "info"


# ------------------------------------------------------- 6. armer, config, UI

def test_armer_marks_a_dispatch_in_flight(monkeypatch):
    from photonscript.scheduler.armer import Armer
    a = Armer(PhotonScriptConfig(_env_file=None))
    assert a.dispatch_in_flight() is False
    seen = []

    async def body(self, companion, fail_state):
        seen.append((self.dispatch_in_flight(), companion, fail_state))
        return True
    monkeypatch.setattr(Armer, "_dispatch_and_start_body", body)
    assert asyncio.run(a._dispatch_and_start(companion=False, fail_state=None))
    assert seen == [(True, False, None)]
    assert a.dispatch_in_flight() is False

    async def boom(self, companion, fail_state):
        raise RuntimeError("x")
    monkeypatch.setattr(Armer, "_dispatch_and_start_body", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(a._dispatch_and_start())
    assert a.dispatch_in_flight() is False


def test_config_defaults_fields_api_and_chip(tmp_path, monkeypatch):
    from photonscript.scheduler.app import _CONFIG_FIELDS
    c = PhotonScriptConfig(_env_file=None)
    assert c.nina_watch_idle_minutes == 15.0
    assert c.nina_watch_idle_restart is False
    assert nw.idle_minutes(PhotonScriptConfig(_env_file=None,
                                              nina_watch_idle_minutes=0)) == 1.0
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    assert by_env["PS_NINA_WATCH_IDLE_MINUTES"][0] == "nina_watch_idle_minutes"
    assert by_env["PS_NINA_WATCH_IDLE_MINUTES"][4] == "float"
    assert by_env["PS_NINA_WATCH_IDLE_RESTART"][0] == "nina_watch_idle_restart"
    assert by_env["PS_NINA_WATCH_IDLE_RESTART"][4] == "bool"
    html = (ROOT / "photonscript/scheduler/templates/dashboard.html").read_text(
        encoding="utf-8")
    assert "idle_running: 'IDLE WHILE ARMED'" in html
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler.routers.nina_watch import api_nina_watch
    monkeypatch.setattr(app_mod, "get_config", lambda: c)
    assert api_nina_watch()["idle_minutes"] == 15.0
    src = (ROOT / "photonscript/scheduler/nina_watchdog.py").read_text(encoding="utf-8")
    assert ".post(" not in src and "sequence_stop" not in src
    for rel in ("photonscript/scheduler/nina_watchdog.py",
                "photonscript/scheduler/night_pilot.py",
                "tests/test_scheduler/test_nina_watch_idle.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert chr(0x2014) not in text, rel
