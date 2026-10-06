"""PS-150: alert on a NINA that is not running, API down, or up but silent.

  1. log helpers: pid from the NINA log name, the "Application shutting
     down" tail, per-rig log selection by API port;
  2. classify(): every state, wait / long-exposure exemptions;
  3. the monitor: quiet clock, 3-tick debounce, once per rig + state per
     night, recovery, night roll;
  4. tick() with fake reads replaying the 2026-10-04 blind night (both NINAs
     closed, armer ARMED): one push per rig, events, panel / off modes,
     outside the window;
  5. API, config fields, dashboard chip, observe-only static check.
"""
import asyncio
import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import nina_watchdog as nw
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.night_events import events_path
from photonscript.shared.phd2_store import night_of, read_jsonl

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 5, 1, 50, 0)   # 19:50 local on the blind night


def _cfg(tmp_path, **kw):
    kw.setdefault("nina_logs_dir", str(tmp_path / "logs"))
    kw.setdefault("nina_base_url", "http://localhost:1888/v2/api")
    kw.setdefault("piggyback_nina_base_url", "http://localhost:1889/v2/api")
    kw.setdefault("piggyback_enabled", True)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _log(d: Path, name: str, port: int, tail: str = "", mtime=None) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("2026-10-04T19:43:06.1|INFO|API.cs|APITask|134|starting web "
                 f"server, listening at 192.168.23.100:{port}\n" + tail,
                 encoding="utf-8")
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(p, (ts, ts))
    return p


# ------------------------------------------------------------- 1. log helpers

def test_pid_from_nina_log_name():
    assert nw.pid_of("20261005-061046-3.2.0.9001.13416-202610.log") == 13416
    assert nw.pid_of(r"C:\x\20261004-194306-3.2.0.9001.23716-202610.log") == 23716
    assert nw.pid_of("PHD2_GuideLog_2026-10-04_193012.txt") is None


def test_closed_at_reads_the_shutdown_line(tmp_path):
    p = _log(tmp_path, "20261004-194306-3.2.0.9001.23716-202610.log", 1888,
             "2026-10-04T19:45:52.5254|INFO|ApplicationVM.cs|Closing|289|"
             "Application shutting down\n"
             "2026-10-04T19:45:53.6011|INFO|API.cs|StopWatchers|86|Stopping\n")
    assert nw.closed_at(p) == "2026-10-04T19:45:52.5254"
    q = _log(tmp_path, "20261005-061046-3.2.0.9001.13416-202610.log", 1888,
             "2026-10-05T06:10:50.1|INFO|x|y|1|running\n")
    assert nw.closed_at(q) is None
    f = nw.log_facts(p)
    assert f["pid"] == 23716 and f["size"] > 0 and f["closed_at"]
    assert nw.log_facts(None)["file"] is None


def test_rig_log_picks_each_rigs_newest_by_port(tmp_path):
    cfg = _cfg(tmp_path)
    d = tmp_path / "logs"
    old = datetime.now() - timedelta(hours=2)
    _log(d, "20261004-190752-3.2.0.9001.9452-202610.log", 1889, mtime=old)
    _log(d, "20261004-194306-3.2.0.9001.23716-202610.log", 1888,
         mtime=old + timedelta(minutes=30))
    _log(d, "20261005-085006-3.2.0.9001.11332-202610.log", 0)   # no API port
    assert nw.rig_log(cfg, "rc16").name.endswith(".23716-202610.log")
    assert nw.rig_log(cfg, "piggyback").name.endswith(".9452-202610.log")


def test_process_alive_unknown_and_not_nina():
    assert nw.process_alive(None) is None
    assert nw.process_alive(os.getpid()) is False   # pytest is not NINA
    assert nw.process_alive(2 ** 22 + 12345) is False


def test_api_version_unreachable():
    ok, err = asyncio.run(nw.api_version("http://127.0.0.1:9/v2/api", timeout=2))
    assert ok is False and err
    assert asyncio.run(nw.api_version("")) == (False, "no API URL configured")


# ------------------------------------------------------------- 2. classify

def _r(**kw):
    base = {"window": True, "expected": True, "api_ok": True, "process": True,
            "log_quiet_min": 1.0, "closed_at": None, "seq_leaf": "Take Exposure",
            "seq_exposure_s": 300, "leaf_age_min": 2.0}
    base.update(kw)
    return base


@pytest.mark.parametrize("reads,state", [
    (_r(window=False), "idle"),
    (_r(expected=False), "idle"),
    (_r(), "ok"),
    (_r(api_ok=False, log_quiet_min=1.0), "api_down"),
    (_r(api_ok=False, log_quiet_min=20.0), "silent"),
    (_r(api_ok=False, process=False, log_quiet_min=600.0), "not_running"),
    (_r(api_ok=False, process=None, closed_at="2026-10-04T19:45:52"), "not_running"),
    (_r(api_ok=False, process=None, log_quiet_min=30.0), "silent"),
    (_r(api_ok=False, process=None, log_quiet_min=2.0), "api_down"),
    # API up, one instruction "running" 40 min with no log line: stuck
    (_r(log_quiet_min=40.0, leaf_age_min=40.0, seq_exposure_s=None,
        seq_leaf="Center After Drift"), "silent"),
    # ... but a wait instruction may log nothing for hours
    (_r(log_quiet_min=90.0, leaf_age_min=90.0, seq_leaf="Wait for Time"), "ok"),
    # ... and a 1200 s exposure gets its length + 5 min
    (_r(log_quiet_min=20.0, leaf_age_min=20.0, seq_exposure_s=1200), "ok"),
    (_r(log_quiet_min=26.0, leaf_age_min=26.0, seq_exposure_s=1200), "silent"),
    # idle NINA (nothing running) with a quiet log is fine
    (_r(log_quiet_min=120.0, seq_leaf=""), "ok"),
])
def test_classify(reads, state):
    assert nw.classify(reads, 15.0)[0] == state


# ------------------------------------------------------------- 3. monitor

def test_quiet_clock_survives_first_sighting_and_resets_on_growth():
    m = nw.NinaWatch()
    mt = T0 - timedelta(minutes=30)
    q, _ = m.track("rc16", T0, "", 1000, mt)
    assert q == pytest.approx(30.0)            # first sighting: mtime rules
    q, _ = m.track("rc16", T0 + timedelta(minutes=1), "", 1000, mt)
    assert q == pytest.approx(31.0)
    q, _ = m.track("rc16", T0 + timedelta(minutes=2), "", 1200, mt)
    assert q == pytest.approx(0.0)             # grew (mtime lagging)
    _, age = m.track("rc16", T0 + timedelta(minutes=3), "Take Exposure", 1200, mt)
    assert age == pytest.approx(0.0)
    _, age = m.track("rc16", T0 + timedelta(minutes=8), "Take Exposure", 1200, mt)
    assert age == pytest.approx(5.0)


def test_debounce_once_per_night_recovery_and_roll():
    m = nw.NinaWatch()
    n1 = "2026-10-04"
    t = T0
    res = [m.update("rc16", "not_running", "x", {}, n1, t + timedelta(minutes=i))
           for i in range(3)]
    assert [r["alert"] for r in res] == [False, False, True]
    assert res[2]["state"] == "not_running"
    # NINA back: one ok read clears it at once (only bad states debounce)
    r = m.update("rc16", "ok", "", {}, n1, t + timedelta(minutes=4))
    assert r["recovered"] and r["prev"] == "not_running"
    for i in range(3):
        r = m.update("rc16", "not_running", "x", {}, n1, t + timedelta(minutes=5 + i))
    assert r["changed"] and not r["alert"]       # same state, same night: no 2nd push
    r = m.update("rc16", "ok", "", {}, n1, t + timedelta(minutes=9))
    assert not r["recovered"]                    # nothing pushed, nothing to clear
    for i in range(3):
        r = m.update("rc16", "not_running", "x", {}, "2026-10-05",
                     t + timedelta(days=1, minutes=i))
    assert r["alert"]                            # next night alerts again


def test_two_bad_reads_then_ok_never_alert():
    m = nw.NinaWatch()
    for i in range(2):
        r = m.update("rc16", "api_down", "x", {}, "n", T0 + timedelta(minutes=i))
        assert r["state"] == "ok" or r["state"] == "idle"
    r = m.update("rc16", "ok", "", {}, "n", T0 + timedelta(minutes=2))
    assert not r["alert"] and not r["recovered"]


def test_escalation_from_api_down_to_silent_alerts_each_once():
    m = nw.NinaWatch()
    seen = []
    for i in range(3):
        seen.append(m.update("rc16", "api_down", "x", {}, "n", T0 + timedelta(minutes=i)))
    for i in range(3, 6):
        seen.append(m.update("rc16", "silent", "x", {}, "n", T0 + timedelta(minutes=i)))
    assert [r["state"] for r in seen] == ["idle", "idle", "api_down",
                                          "api_down", "api_down", "silent"]
    assert sum(r["alert"] for r in seen) == 2


# ------------------------------------------------------------- 4. tick (10-04)

def _blind_night_read(closed=True):
    async def read(cfg, rig):
        port = 1889 if rig == "piggyback" else 1888
        return {"api_ok": False, "api_error": "ConnectError: All connection attempts failed",
                "process": False, "pid": 23716 if rig == "rc16" else 9452,
                "log_file": f"20261004-1943-{port}.log",
                "log_mtime": datetime(2026, 10, 5, 1, 45, 52),
                "log_size": 10525,
                "closed_at": "2026-10-04T19:45:52.5254" if closed else None,
                "seq_leaf": None, "seq_exposure_s": None}
    return read


def _run_ticks(cfg, n, armer_state="ARMED", read=None, window=True, mon=None):
    pushes = []

    async def notify(c, msg, title="", priority=0):
        pushes.append((title, msg, priority))
        return True

    mon = mon or nw.NinaWatch()
    last = None
    for i in range(n):
        last = asyncio.run(nw.tick(cfg, armer_state, now=T0 + timedelta(minutes=i),
                                   read=read or _blind_night_read(), notify=notify,
                                   window=window, monitor=mon))
    return pushes, last, mon


def test_blind_night_one_clear_push_per_rig(tmp_path):
    cfg = _cfg(tmp_path)
    pushes, last, mon = _run_ticks(cfg, 30)
    assert len(pushes) == 2                       # one per rig, not hourly
    titles = {p[0] for p in pushes}
    assert titles == {"PhotonScript NINA watch"}
    rc = next(p for p in pushes if "NINA #1" in p[1])
    assert "NOT RUNNING" in rc[1] and "start NINA #1" in rc[1]
    assert "19:45" in rc[1] and "never starts NINA" in rc[1]
    assert rc[2] == 1                             # armed: priority 1
    assert any("NINA #2 (Piggy-600)" in p[1] for p in pushes)
    assert last["rc16"]["state"] == "not_running"
    ev = [e for e in read_jsonl(events_path(cfg, night_of(cfg, T0)))
          if e.get("kind") == "nina_watch"]
    assert {(e["rig"], e["value"]) for e in ev} == {("rc16", "not_running"),
                                                    ("piggyback", "not_running")}
    v = mon.view()
    assert v["rigs"]["rc16"]["state"] == "not_running"
    assert v["rigs"]["rc16"]["alerted"] is True
    assert isinstance(v["rigs"]["rc16"]["reads"]["log_mtime"], str)


def test_recovery_push_when_nina_comes_back(tmp_path):
    cfg = _cfg(tmp_path, piggyback_enabled=False)
    pushes, _, mon = _run_ticks(cfg, 3)
    assert len(pushes) == 1

    async def ok_read(c, rig):
        return {"api_ok": True, "api_error": "", "process": True, "pid": 13416,
                "log_file": "x.log", "log_mtime": T0, "log_size": 5,
                "closed_at": None, "seq_leaf": "", "seq_exposure_s": None}
    more, last, _ = _run_ticks(cfg, 1, read=ok_read, mon=mon)
    assert last["rc16"]["recovered"]
    assert len(more) == 1 and "OK again" in more[0][1] and "not running" in more[0][1]


def test_unarmed_and_closed_is_idle(tmp_path):
    cfg = _cfg(tmp_path)
    pushes, last, _ = _run_ticks(cfg, 5, armer_state="DISARMED")
    assert pushes == [] and last["rc16"]["state"] == "idle"


def test_panel_mode_logs_but_never_pushes(tmp_path):
    cfg = _cfg(tmp_path, nina_watch_mode="panel")
    pushes, last, _ = _run_ticks(cfg, 5)
    assert pushes == [] and last["rc16"]["state"] == "not_running"
    assert read_jsonl(events_path(cfg, night_of(cfg, T0)))


def test_off_mode_and_daytime_are_idle_without_reads(tmp_path):
    async def boom(c, rig):
        raise AssertionError("must not read")
    cfg = _cfg(tmp_path, nina_watch_mode="off")
    pushes, last, _ = _run_ticks(cfg, 4, read=boom)
    assert pushes == [] and last["rc16"]["state"] == "idle"
    cfg = _cfg(tmp_path)
    pushes, last, _ = _run_ticks(cfg, 4, read=boom, window=False)
    assert pushes == [] and last["piggyback"]["state"] == "idle"


def test_api_down_and_silent_texts(tmp_path):
    cfg = _cfg(tmp_path)
    reads = {"pid": 13416, "api_error": "ReadTimeout", "log_file": "a.log",
             "log_mtime": T0}
    t = nw.alert_text(cfg, "rc16", "api_down", "", reads, "RUNNING")
    assert "Advanced API" in t and "ReadTimeout" in t and "Nothing was changed" in t
    t = nw.alert_text(cfg, "piggyback", "silent", "the log has not grown for 20 min",
                      reads, "RUNNING")
    assert "UP BUT SILENT" in t and "pid 13416" in t and "restarts nothing" in t
    assert "The armer is RUNNING" in t


def test_in_window_uses_sun_altitude(tmp_path):
    cfg = _cfg(tmp_path)
    assert nw.in_window(cfg, datetime(2026, 10, 5, 6, 0)) is True     # midnight MDT
    assert nw.in_window(cfg, datetime(2026, 10, 5, 19, 0)) is False   # 13:00 MDT


# ------------------------------------------------------------- 5. API, config, static

def test_api_endpoint_reports_view(tmp_path, monkeypatch):
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler.routers.nina_watch import api_nina_watch
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
    mon = nw.NinaWatch()
    monkeypatch.setattr(nw, "MONITOR", mon)
    mon.update("rc16", "ok", "API answers", {"api_ok": True}, "n", T0)
    out = api_nina_watch()
    assert out["mode"] == "alert" and out["silent_minutes"] == 15.0
    assert out["rigs"]["rc16"]["state"] == "ok"
    assert out["names"]["rc16"] == "NINA #1 (RC16)"
    from fastapi.testclient import TestClient
    r = TestClient(app_mod.app).get("/api/nina/watch")    # mounted (no startup)
    assert r.status_code == 200 and r.json()["rigs"]["rc16"]["state"] == "ok"


def test_config_defaults_and_fields():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    c = PhotonScriptConfig(_env_file=None)
    assert c.nina_watch_mode == "alert"
    assert c.nina_watch_silent_minutes == 15.0
    assert c.nina_watch_sun_alt_deg == -6.0
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    for env, typ in (("PS_NINA_WATCH_MODE", "str"),
                     ("PS_NINA_WATCH_SILENT_MINUTES", "float"),
                     ("PS_NINA_WATCH_SUN_ALT_DEG", "float")):
        assert by_env[env][4] == typ and hasattr(c, by_env[env][0])
    assert nw.mode(PhotonScriptConfig(_env_file=None, nina_watch_mode="junk")) == "alert"


def test_dashboard_chip_and_observe_only():
    html = (ROOT / "photonscript/scheduler/templates/dashboard.html").read_text(
        encoding="utf-8")
    assert 'id="ninaWatchChip"' in html and "/api/nina/watch" in html
    src = (ROOT / "photonscript/scheduler/nina_watchdog.py").read_text(encoding="utf-8")
    assert ".post(" not in src and "sequence_stop" not in src and "start_sequence" not in src
    assert src.isascii()
