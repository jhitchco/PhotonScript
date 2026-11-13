"""Autofocus that keeps restarting without finishing (2026-10-08..11: the
RC16 focuser dropped off USB, NINA #1 logged 55 AUTOFOCUS-STARTING and 54
points with no finish, nothing paged for three nights).

Fixtures (tests/test_scheduler/fixtures/nina_events):
  2026-10-11_nina1_event_history.json  the real NINA #1 event history of
      2026-10-11 (GET :1888/v2/api/event-history): an AF at 21:47 cut by the
      roof going unsafe at 21:50:09, the next one started under the closed
      roof (21:50-21:53), a 2-point AF at 22:42 ended by SEQUENCE-FINISHED
  2026-10-11_last_af_failed.json  the real last-af report of 21:53 (NaN fit)
  2026-10-10_af_one_point.json  the 10-10 pattern rebuilt from the counts:
      every AF quits after one point at position 0
  2026-10-05_healthy_8pt.json  a healthy night: three 8-point AFs that finish
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.scheduler import af_stall
from photonscript.shared.config import PhotonScriptConfig

FIX = Path(__file__).parent / "fixtures" / "nina_events"


def _events(name):
    return json.loads((FIX / name).read_text())["Response"]


def _cfg(tmp_path, **kw):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"),
                              observatory_tz="America/Denver", **kw)


# ------------------------------------------------------------- parsing

def test_one_point_night_is_a_stall():
    runs = af_stall.af_runs(_events("2026-10-10_af_one_point.json"))
    assert len(runs) == 18 and all(r["points"] == 1 for r in runs)
    assert all(r["finished"] is None and not r["unsafe"] for r in runs)
    now = datetime(2026, 10, 11, 9, 0)             # 03:00 local
    streak = af_stall.stall(runs, 3, 4, now=now)
    assert streak is not None and len(streak) == 18
    assert [r["verdict"] for r in runs] == ["bad"] * 18
    # mid-night: the newest AF is still open (not judged) until it goes
    # quiet for OPEN_MAX_MIN, so three starts are not yet three failures
    ev = _events("2026-10-10_af_one_point.json")[:3 + 3 * 3]
    part = af_stall.af_runs(ev)
    assert part[-1]["end_by"] == "open"
    t = part[-1]["start"]
    assert af_stall.stall(part, 3, 4, now=t) is None
    assert af_stall.stall(part, 3, 4, now=t + timedelta(minutes=16)) is not None


def test_healthy_8_point_night_never_stalls():
    runs = af_stall.af_runs(_events("2026-10-05_healthy_8pt.json"))
    assert [r["points"] for r in runs] == [8, 8, 8]
    assert all(r["finished"] is True for r in runs)
    assert af_stall.stall(runs, 1, 4, now=datetime(2026, 10, 6, 12, 0),
                          watching_since=datetime(2026, 10, 5, 0, 0)) is None
    assert {r["verdict"] for r in runs} == {"ok"}


def test_roof_interrupted_afs_of_2026_10_11_do_not_count():
    rep = json.loads((FIX / "2026-10-11_last_af_failed.json").read_text())["Response"]
    assert af_stall.report_ok(rep) is False         # NaN fit
    runs = af_stall.af_runs(_events("2026-10-11_nina1_event_history.json"), [rep])
    assert [r["points"] for r in runs] == [8, 8, 2]
    first, second, third = runs
    assert first["unsafe"] is True                  # unsafe at 21:50:09
    assert second["unsafe"] is True                 # started under a closed roof
    assert second["finished"] is False              # the 21:53 NaN report
    assert third["end_by"] == "sequence"
    now = datetime(2026, 10, 12, 6, 0)
    assert af_stall.stall(runs, 1, 4, now=now,
                          watching_since=datetime(2026, 10, 11, 22, 0)) is None
    assert [r["verdict"] for r in runs] == ["cut", "cut", "cut"]


def test_report_marks_completion_and_failed_event_counts_bad():
    ev = [{"Event": "AUTOFOCUS-STARTING", "Time": "2026-10-05T20:00:00-06:00"}]
    ev += [{"Event": "AUTOFOCUS-POINT-ADDED", "Time": f"2026-10-05T20:0{i}:30-06:00",
            "Position": 5600 + 90 * i, "HFR": 3.5} for i in range(1, 7)]
    good = {"Timestamp": "2026-10-05T20:08:00-06:00",
            "CalculatedFocusPoint": {"Position": 5760, "Value": 3.4}}
    runs = af_stall.af_runs(ev, [good])
    assert runs[0]["finished"] is True and runs[0]["end_by"] == "report"
    assert af_stall.judge(runs[0], 4, datetime(2026, 10, 6, 4, 0)) == "ok"
    bad = ev + [{"Event": "AUTOFOCUS-FAILED", "Time": "2026-10-05T20:09:00-06:00"}]
    runs = af_stall.af_runs(bad)
    assert runs[0]["finished"] is False and runs[0]["failed_event"] == "AUTOFOCUS-FAILED"
    assert af_stall.judge(runs[0], 4) == "bad"


def test_no_completion_only_judged_while_watching():
    ev = [{"Event": "AUTOFOCUS-STARTING", "Time": "2026-10-05T20:00:00-06:00"}]
    ev += [{"Event": "AUTOFOCUS-POINT-ADDED", "Time": f"2026-10-05T20:0{i}:30-06:00",
            "Position": 5600 + 90 * i, "HFR": 3.5} for i in range(1, 7)]
    ev += [{"Event": "AUTOFOCUS-STARTING", "Time": "2026-10-05T21:00:00-06:00"}]
    run = af_stall.af_runs(ev)[0]
    now = datetime(2026, 10, 6, 6, 0)
    # ended before the agent watched: 6 points is enough
    assert af_stall.judge(run, 4, now, watching_since=datetime(2026, 10, 6, 5, 0)) == "ok"
    # ended while watching and no fit was reported: did not complete
    assert af_stall.judge(run, 4, now, watching_since=datetime(2026, 10, 6, 1, 0)) == "bad"


def _af(t0, points, finished):
    ev = [{"Event": "AUTOFOCUS-STARTING", "Time": f"2026-10-10T{t0}:00:00-06:00"}]
    ev += [{"Event": "AUTOFOCUS-POINT-ADDED", "Time": f"2026-10-10T{t0}:0{i}:30-06:00",
            "Position": 5600 + 90 * i, "HFR": 3.5} for i in range(points)]
    if finished:
        ev.append({"Event": "AUTOFOCUS-FINISHED", "Time": f"2026-10-10T{t0}:09:00-06:00"})
    return ev


def test_ok_run_resets_the_streak():
    now = datetime(2026, 10, 11, 12, 0)
    bad2_ok = _af(20, 1, False) + _af(21, 1, False) + _af(22, 8, True)
    assert af_stall.stall(af_stall.af_runs(bad2_ok), 2, 4, now=now) is None
    ok_bad2 = _af(20, 8, True) + _af(21, 1, False) + _af(22, 1, False)
    s = af_stall.stall(af_stall.af_runs(ok_bad2), 2, 4, now=now)
    assert s is not None and len(s) == 2


def test_tonight_filters_by_local_night(tmp_path):
    cfg = _cfg(tmp_path)
    runs = af_stall.af_runs(_events("2026-10-05_healthy_8pt.json"))
    assert len(af_stall.tonight(cfg, runs, "2026-10-05")) == 3   # 01:09 local too
    assert af_stall.tonight(cfg, runs, "2026-10-06") == []


def test_alert_text_names_focuser_counts_and_causes(tmp_path):
    cfg = _cfg(tmp_path)
    runs = af_stall.af_runs(_events("2026-10-10_af_one_point.json"))
    streak = af_stall.stall(runs, 3, 4, now=datetime(2026, 10, 11, 9, 0))[-3:]
    f = {"Connected": True, "Position": 0, "DisplayName": "EAF(MainRC16)"}
    msg = af_stall.alert_text(cfg, "rc16", streak, f, 4)
    assert msg.startswith("RC16: autofocus did not finish 3 times in a row "
                          "(focuser EAF(MainRC16))")
    assert "Points per run: 1, 1, 1 (a good run has 4+)" in msg
    assert "not moving" in msg and "position 0" in msg and "clouds" in msg
    assert msg.index("not moving") < msg.index("clouds")
    # moving focuser, no stars: clouds first
    cloudy = [{**r, "positions": [6107, 5997], "points": 2, "hfr_zero": 2}
              for r in streak]
    m2 = af_stall.alert_text(cfg, "piggyback", cloudy,
                             {"Position": 11032, "Name": "EAF"}, 4)
    assert m2.startswith("Piggy-600: autofocus") and "HFR 0 on every point" in m2
    assert m2.index("clouds") < m2.index("not moving")
    assert "\u2014" not in msg + m2


def test_defaults_and_system_page_fields():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    cfg = PhotonScriptConfig(_env_file=None)
    assert cfg.af_stall_alert is True
    assert cfg.af_stall_runs == 3 and cfg.af_stall_min_points == 4
    by = {f[0]: f for f in _CONFIG_FIELDS}
    assert by["af_stall_alert"][1:5:3] == ("PS_AF_STALL_ALERT", "bool")
    assert by["af_stall_runs"][4] == "int" and by["af_stall_min_points"][4] == "int"


# ------------------------------------------------------------- live watch

class _State:
    def __init__(self, imaging=True):
        from photonscript.shared.models import SessionState
        self.session_state = SessionState.IMAGING if imaging else SessionState.IDLE
        self.current_target = "M31"


class _Nina:
    def __init__(self, events, report=None):
        self.events, self.report, self.calls = events, report or {}, 0

    async def get_event_history(self):
        self.calls += 1
        return self.events

    async def get_last_af(self):
        return self.report


def _agent(monkeypatch, cfg, events, rig="rc16", imaging=True, report=None):
    from photonscript.telescope_agent import agent as agent_mod
    a = agent_mod.TelescopeAgent.__new__(agent_mod.TelescopeAgent)
    a.config, a.rig, a.state = cfg, rig, _State(imaging)
    a.nina = _Nina(events, report)
    sent, held = [], []

    async def fake_notify(config, msg, **kw):
        sent.append((msg, kw))
    monkeypatch.setattr(agent_mod, "notify", fake_notify)
    monkeypatch.setattr("photonscript.shared.pushover.record",
                        lambda c, m, **kw: held.append(m))
    return a, sent, held


def _now(monkeypatch, when):
    import photonscript.telescope_agent.agent as agent_mod

    class _DT(datetime):
        @classmethod
        def utcnow(cls):
            return when
    monkeypatch.setattr(agent_mod, "datetime", _DT)


def _tick(a):
    asyncio.run(a._af_stall_watch({"Connected": True, "Position": 0,
                                   "DisplayName": "EAF(MainRC16)"}))
    a._af_stall_polled = None          # next call polls again


def test_watch_pages_once_per_rig_per_night_and_writes_event(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _now(monkeypatch, datetime(2026, 10, 11, 7, 0))           # 01:00 local, 10-10
    ev = _events("2026-10-10_af_one_point.json")
    a, sent, held = _agent(monkeypatch, cfg, ev)
    for _ in range(4):
        _tick(a)
    assert len(sent) == 1
    msg, kw = sent[0]
    assert kw["priority"] == 1 and kw["title"] == af_stall.ALERT_TITLE
    assert "EAF(MainRC16)" in msg and "1, 1, 1" in msg
    # a restarted agent the same night: latched, recorded only
    b, sent_b, held_b = _agent(monkeypatch, cfg, ev)
    _tick(b)
    assert sent_b == [] and len(held_b) == 1
    # the Piggy has its own latch
    c, sent_c, _ = _agent(monkeypatch, cfg, ev, rig="piggyback")
    _tick(c)
    assert len(sent_c) == 1 and sent_c[0][0].startswith("Piggy-600:")
    from photonscript.shared.night_events import events_path
    from photonscript.shared.phd2_store import read_jsonl
    rows = [r for r in read_jsonl(events_path(cfg, "2026-10-10"))
            if r.get("kind") == "af_stall"]
    assert len(rows) == 3 and rows[0]["points"][:3] == [1, 1, 1]
    # and the morning report says so
    from photonscript.scheduler import morning_report as mr
    card = mr.report_card(cfg, "2026-10-10", projects=[], calibration=False,
                          completeness=False)
    line = next(x for x in card["lines"] if x.startswith("RC16"))
    assert "autofocus: 18 runs in a row did not finish (points 1, 1, 1" in line


def test_watch_quiet_when_not_imaging_healthy_roof_or_off(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _now(monkeypatch, datetime(2026, 10, 11, 7, 0))
    one = _events("2026-10-10_af_one_point.json")
    idle, sent, _ = _agent(monkeypatch, cfg, one, imaging=False)
    _tick(idle)
    assert sent == [] and idle.nina.calls == 0             # not even read
    off, sent, _ = _agent(monkeypatch, _cfg(tmp_path, af_stall_alert=False), one)
    _tick(off)
    assert sent == [] and off.nina.calls == 0
    _now(monkeypatch, datetime(2026, 10, 6, 12, 0))
    ok, sent, _ = _agent(monkeypatch, cfg, _events("2026-10-05_healthy_8pt.json"))
    _tick(ok)
    assert sent == [] and ok.nina.calls == 1
    _now(monkeypatch, datetime(2026, 10, 12, 6, 0))
    rep = json.loads((FIX / "2026-10-11_last_af_failed.json").read_text())["Response"]
    roof, sent, _ = _agent(monkeypatch, cfg,
                           _events("2026-10-11_nina1_event_history.json"),
                           report=rep)
    roof._af_watch_since = datetime(2026, 10, 11, 20, 0)
    _tick(roof)
    assert sent == []


def test_watch_rate_limited_and_never_raises(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _now(monkeypatch, datetime(2026, 10, 6, 12, 0))
    a, sent, _ = _agent(monkeypatch, cfg, _events("2026-10-05_healthy_8pt.json"))
    asyncio.run(a._af_stall_watch({}))
    asyncio.run(a._af_stall_watch({}))                     # inside AF_STALL_POLL_S
    assert a.nina.calls == 1

    class _Boom(_Nina):
        async def get_event_history(self):
            raise RuntimeError("API down")
    a.nina = _Boom([])
    a._af_stall_polled = None
    asyncio.run(a._af_stall_watch({}))                     # logs, no raise
    assert sent == []
