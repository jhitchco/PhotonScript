"""NINA watch pages skip the global Pushover hourly cap (own budget instead).

2026-10-17 03:06:55Z: the NINA watchdog saw NINA #2 (Piggy-600) UP BUT
SILENT (log idle 18 min) but the push was held as hourly-cap, while the NOT
RUNNING pages got through. A not running / up but silent page now skips the
global hourly cap but keeps dedup, the monthly cap and its own per
(rig, state) budget, so it cannot spam.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

from photonscript.scheduler import nina_watchdog as nw
from photonscript.shared import pushover as po

from tests.test_scheduler.test_ps150_nina_watch import T0, _cfg, _blind_night_read

NOW = 1_792_206_415.0          # 2026-10-17T03:06:55Z
MONTH = "2026-10"
SILENT = ("NINA #2 (Piggy-600) is UP BUT SILENT: the process (pid 20568) is "
          "there but the log has not grown for 18 min")


def _full_hour(now=NOW, n=20):
    """A limiter state whose last hour already holds n sends (the 10-17 night)."""
    return {"month": MONTH, "month_count": 100,
            "hour_ts": [now - 60 * (i + 1) for i in range(n)]}


def _g(state, now, key, exempt_key=None, prio=1, exempt_max=2, monthly=9000):
    return po._gate(state, now, MONTH, key, prio, 300, 20, monthly,
                    exempt_key=exempt_key, exempt_max_per_hour=exempt_max)


# ------------------------------------------------------------ the limiter

def test_hour_full_holds_a_normal_page_but_not_a_nina_watch_page():
    st = _full_hour()
    assert _g(st, NOW, "PhotonScript NANNY\x1fx")[:2] == (False, "hourly-cap")
    allow, reason, _ = _g(st, NOW + 1, "PhotonScript NINA watch\x1f" + SILENT,
                          exempt_key="nina_watch:piggyback:silent")
    assert (allow, reason) == (True, "ok")
    # it still counts toward the hour the other pages see
    assert len([t for t in st["hour_ts"] if NOW + 1 - t < 3600]) == 21
    assert _g(st, NOW + 2, "PhotonScript NANNY\x1fy")[:2] == (False, "hourly-cap")


def test_exempt_page_has_its_own_budget_per_rig_and_state():
    st = _full_hour()
    k = "nina_watch:piggyback:silent"
    assert _g(st, NOW, "t\x1fa", k)[:2] == (True, "ok")
    assert _g(st, NOW + 400, "t\x1fb", k)[:2] == (True, "ok")
    # a third page for the same (rig, state) inside the hour: held
    assert _g(st, NOW + 800, "t\x1fc", k)[:2] == (False, "exempt-cap")
    # another (rig, state) has its own budget
    assert _g(st, NOW + 801, "t\x1fd", "nina_watch:rc16:not_running")[:2] == (True, "ok")
    # an hour after the first page the budget frees up again
    assert _g(st, NOW + 3601, "t\x1fe", k)[:2] == (True, "ok")
    # expired keys are pruned from the state file
    assert _g(st, NOW + 20000, "t\x1ff", "nina_watch:rc16:silent")[0]
    assert set(st["exempt_ts"]) == {"nina_watch:rc16:silent"}


def test_exempt_page_keeps_dedup_and_the_monthly_cap():
    st = _full_hour()
    k = "nina_watch:rc16:not_running"
    assert _g(st, NOW, "t\x1fsame", k)[0]
    assert _g(st, NOW + 60, "t\x1fsame", k)[:2] == (False, "dedup")
    st2 = {"month": MONTH, "month_count": 9000, "month_capped": True}
    assert _g(st2, NOW, "t\x1fz", k, monthly=9000)[:2] == (False, "monthly-capped")


def test_gate_without_exempt_key_is_unchanged():
    st = _full_hour(n=19)
    assert _g(st, NOW, "a\x1f1")[:2] == (True, "ok")
    assert _g(st, NOW + 1, "a\x1f2")[:2] == (False, "hourly-cap")
    assert "exempt_ts" not in st


# ------------------------------------------------------------ notify()

def _notify_env(tmp_path, monkeypatch, state):
    sent = []

    async def fake_send(config, message, title, priority, sound, **_kw):
        sent.append((title, message))
        return True

    monkeypatch.setattr(po, "_send_raw", fake_send)
    monkeypatch.setattr(po, "_is_daytime", lambda config, when=None: False)
    monkeypatch.setattr(po, "decide", lambda config, kind, safety, now=None: (True, "ok"))
    monkeypatch.setattr(po, "plan_window_open", lambda config: False)
    monkeypatch.setattr(po.time, "time", lambda: NOW)
    cfg = SimpleNamespace(data_dir=str(tmp_path))
    po._state_path(cfg).write_text(json.dumps(state))
    return cfg, sent


def _audit(cfg):
    return [json.loads(x) for x in po._audit_path(cfg).read_text().splitlines()]


def test_notify_sends_the_1017_silent_page_through_a_full_hour(tmp_path, monkeypatch):
    cfg, sent = _notify_env(tmp_path, monkeypatch, _full_hour())
    ok = asyncio.run(po.notify(cfg, SILENT, title="PhotonScript NINA watch",
                               priority=1, kind="act",
                               cap_exempt="nina_watch:piggyback:silent"))
    assert ok and sent and sent[0][0] == "PhotonScript NINA watch"
    held = asyncio.run(po.notify(cfg, "other", title="PhotonScript NANNY",
                                 priority=1, kind="act"))
    assert held is False
    assert [r["reason"] for r in _audit(cfg)] == ["sent", "hourly-cap"]


def test_notify_without_cap_exempt_is_still_hourly_capped(tmp_path, monkeypatch):
    cfg, sent = _notify_env(tmp_path, monkeypatch, _full_hour())
    ok = asyncio.run(po.notify(cfg, SILENT, title="PhotonScript NINA watch",
                               priority=1, kind="act"))
    assert ok is False and not sent
    assert _audit(cfg)[-1]["reason"] == "hourly-cap"


def test_notify_exempt_budget_follows_config(tmp_path, monkeypatch):
    cfg, sent = _notify_env(tmp_path, monkeypatch, _full_hour())
    cfg.pushover_cap_exempt_max_per_hour = 1
    k = "nina_watch:piggyback:not_running"
    assert asyncio.run(po.notify(cfg, "NINA #2 is NOT RUNNING (1)",
                                 title="PhotonScript NINA watch", priority=1,
                                 kind="act", cap_exempt=k, fact="a"))
    assert not asyncio.run(po.notify(cfg, "NINA #2 is NOT RUNNING (2)",
                                     title="PhotonScript NINA watch", priority=1,
                                     kind="act", cap_exempt=k, fact="b"))
    assert _audit(cfg)[-1]["reason"] == "exempt-cap"
    assert len(sent) == 1


# ------------------------------------------------------------ the watchdog

def test_cap_exempt_key_only_for_not_running_and_silent():
    assert nw.cap_exempt_key("piggyback", "silent") == "nina_watch:piggyback:silent"
    assert nw.cap_exempt_key("rc16", "not_running") == "nina_watch:rc16:not_running"
    for state in ("api_down", "stuck", "parked", "ok"):
        assert nw.cap_exempt_key("rc16", state) is None


def _ticks(cfg, n, read, armer_state="ARMED"):
    calls = []

    async def notify(c, msg, title="", priority=0, **kw):
        calls.append({"msg": msg, **kw})
        return True

    mon = nw.NinaWatch()
    for i in range(n):
        asyncio.run(nw.tick(cfg, armer_state, now=T0 + timedelta(minutes=i),
                            read=read, notify=notify, window=True, monitor=mon))
    return calls


def test_tick_marks_not_running_pages_cap_exempt(tmp_path):
    calls = _ticks(_cfg(tmp_path), 5, _blind_night_read())
    keys = sorted(c.get("cap_exempt") for c in calls)
    assert keys == ["nina_watch:piggyback:not_running", "nina_watch:rc16:not_running"]


def _silent_read():
    """10-17: NINA #2 process alive, API down, log idle 18+ min."""
    async def read(cfg, rig):
        if rig == "rc16":
            return {"api_ok": True, "process": True, "pid": 1, "log_file": "a.log",
                    "log_mtime": T0, "log_size": None, "closed_at": None,
                    "seq_leaf": "", "seq_exposure_s": None}
        return {"api_ok": False, "api_error": "ReadTimeout", "process": True,
                "pid": 20568, "log_file": "20261016-131704-3.2.0.9001.20568-202610.log",
                "log_mtime": T0 - timedelta(minutes=18), "log_size": 5000,
                "closed_at": None, "seq_leaf": None, "seq_exposure_s": None}
    return read


def test_tick_marks_silent_page_cap_exempt(tmp_path):
    calls = _ticks(_cfg(tmp_path), 5, _silent_read())
    silent = [c for c in calls if "UP BUT SILENT" in c["msg"]]
    assert len(silent) == 1                      # the (rig, state) latch still holds
    assert silent[0]["cap_exempt"] == "nina_watch:piggyback:silent"


def test_tick_api_down_page_stays_under_the_hourly_cap(tmp_path):
    async def read(cfg, rig):
        return {"api_ok": False, "api_error": "x", "process": True, "pid": 7,
                "log_file": "b.log", "log_mtime": T0, "log_size": None,
                "closed_at": None, "seq_leaf": None, "seq_exposure_s": None}
    calls = _ticks(_cfg(tmp_path), 4, read)
    assert calls and all("cap_exempt" not in c for c in calls)
