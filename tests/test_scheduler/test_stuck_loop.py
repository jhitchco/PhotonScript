"""Stuck-loop detector (2026-10-15, scheduler/stuck_loop.py).

2026-10-14 22:21-22:41 MDT: NINA #1 repeated AF -> center -> park -> unpark
three times with no light saved and nothing paged.

  1. backtests from read-only fixtures: 10-14 alerts once at 22:35 naming
     the AF times; 10-13 (a normal night, AF every ~40 min) never alerts;
  2. the signals and the legit gaps (unsafe, AF / flip in progress, the
     window start, a light ends the episode);
  3. the action: advise by default, act = one restart per rig per night,
     then alert only; guards; the Piggy-600 path; mode caps;
  4. the armer hook gating, morning lines, config and settings rows.
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.scheduler import stuck_loop as sl
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.night_events import events_path
from photonscript.shared.phd2_store import read_jsonl

FIX = Path(__file__).parent / "fixtures"
NIGHT = "2026-10-14"


def _cfg(tmp_path, **kw):
    kw.setdefault("observatory_tz", "America/Denver")
    kw.setdefault("piggyback_enabled", True)
    kw.setdefault("piggyback_exposure_s", 450.0)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                              image_watch_dir=str(tmp_path / "fits"),
                              library_dir=str(tmp_path / "lib"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def U(hm: str) -> datetime:
    """22:21 MDT on 10-14 -> naive UTC (04:21 on 10-15)."""
    h, m = (int(x) for x in hm.split(":"))
    return datetime(2026, 10, 14, h, m) + timedelta(hours=6)


def ev(hm: str, name: str, **kw) -> dict:
    t = U(hm)
    return {"Time": (t - timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S") + "-06:00",
            "Event": name, **kw}


def light(hm: str, rig="rc16", exp=120.0) -> dict:
    end = U(hm)
    st = end - timedelta(seconds=exp)
    return {"rig": rig, "file": f"LIGHT/{rig}_{hm.replace(':', '')}.fits",
            "exp_s": exp, "start_utc": st.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_utc": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "time": (end + timedelta(seconds=20)).strftime("%Y-%m-%dT%H:%M:%SZ")}


PLAN = {"night_of": NIGHT, "dusk_utc": "2026-10-15T02:05:43Z",
        "dawn_utc": "2026-10-15T12:00:54Z",
        "piggy_length": {"single_s": 450.0},
        "targets": [{"name": "NGC 604", "window_start_utc": "2026-10-15T04:19:00Z",
                     "window_end_utc": "2026-10-15T11:35:00Z", "guided": False,
                     "exposures": [{"filter": "L", "exp_s": 120.0}]}]}


class FakeArmer:
    def __init__(self, cfg, state="RUNNING"):
        self.config = cfg
        self.plan = json.loads(json.dumps(PLAN))
        self.state = state
        self.restarts = []

    def _dawn(self):
        return datetime(2026, 10, 15, 12, 0, 54)

    def _recipe_tonight(self):
        return None

    async def restart(self, when="after_exposure", now=None):
        self.restarts.append((when, now))
        return {"ok": True, "pending": True}


class FakeIO:
    def __init__(self, hist=None, subs=None, running=None):
        self.hist = {"rc16": hist or [], "piggyback": None}   # None: unreadable
        self.subs_ = subs or []
        self.running = running or []
        self.pushes = []
        self.piggy = []

    async def history(self, cfg, rig):
        return self.hist.get(rig)

    def subs(self, cfg, night):
        return self.subs_

    async def nina1_running(self, cfg):
        return self.running, "NGC 604"

    async def notify(self, cfg, msg, priority=1, **_kw):
        self.pushes.append((msg, priority))

    async def restart(self, armer, now):
        return await armer.restart(when="after_exposure", now=now)

    def piggy_restart(self, cfg, armer, done):
        self.piggy.append(True)
        done(True, "fake")


LOOP = [ev("22:19", "SEQUENCE-STARTING"), ev("22:21", "MOUNT-UNPARKED"),
        ev("22:21", "GUIDER-DISCONNECTED"),
        ev("22:22", "AUTOFOCUS-STARTING"), ev("22:25", "AUTOFOCUS-FINISHED"),
        ev("22:25", "MOUNT-CENTER"), ev("22:27", "MOUNT-PARKED"),
        ev("22:29", "MOUNT-UNPARKED"), ev("22:29", "AUTOFOCUS-STARTING"),
        ev("22:33", "AUTOFOCUS-FINISHED"), ev("22:33", "MOUNT-CENTER"),
        ev("22:34", "MOUNT-PARKED"), ev("22:35", "MOUNT-UNPARKED"),
        ev("22:36", "AUTOFOCUS-STARTING")]


def run(coro):
    return asyncio.run(coro)


def kinds(cfg):
    return [(r["value"], r["rig"]) for r in read_jsonl(events_path(cfg, NIGHT))
            if r.get("kind") == "stuck_loop"]


# --------------------------------------------------------------------------
# 1. backtests
# --------------------------------------------------------------------------

def test_backtest_2026_10_14_alerts_once_at_22_35(tmp_path):
    doc = json.loads((FIX / "stuck_loop" / "2026-10-14.json").read_text())
    cfg = _cfg(tmp_path)
    res = sl.backtest(doc, cfg)
    assert len(res) == 1
    a = res[0]
    assert a["rig"] == "rc16"
    assert U("22:34") <= a["t"] <= U("22:36")     # Jeremy restarted at 22:41
    assert set(a["signals"]) >= {"restarts", "park_cycle"}
    assert a["text"].startswith("RC16 looping: AF 22:21, 22:29; center x2; park/unpark x2")
    assert "guider was disconnected at 22:21" in a["text"]
    assert "would restart tonight from now" in a["text"]
    assert chr(0x2014) not in a["text"]   # no em dash in a push
    # the light at 22:46 ended the episode
    st = sl.load_state(cfg, NIGHT)
    assert st["open"] == {} and st["episodes"][0].get("cleared")
    assert [k for k, _r in kinds(cfg)] == ["advise", "alert", "clear"]
    lines = sl.morning_lines(cfg, NIGHT)
    assert lines and lines[0].startswith("Stuck loop 22:35: RC16 looping: AF 22:21, 22:29")


def test_backtest_2026_10_13_normal_night_no_alarm(tmp_path):
    doc = json.loads((FIX / "pilot_watch" / "2026-10-13.json").read_text())
    cfg = _cfg(tmp_path)
    assert sl.backtest(doc, cfg) == []
    # AF every ~40 min on both rigs: plenty of AF starts, never a loop
    starts = [e for e in doc["nina_events"]["rc16"] if e["Event"] == "AUTOFOCUS-STARTING"]
    assert len(starts) >= 15


def test_backtest_2026_10_14_act_mode_restarts_once(tmp_path):
    doc = json.loads((FIX / "stuck_loop" / "2026-10-14.json").read_text())
    cfg = _cfg(tmp_path, night_pilot_stuck_loop_mode="act", night_pilot_mode="act")
    res = sl.backtest(doc, cfg)
    assert len(res) == 1 and "Restart tonight from now (after the current sub)" in res[0]["text"]
    assert ("act", "rc16") in kinds(cfg)


# --------------------------------------------------------------------------
# 2. signals and legit gaps
# --------------------------------------------------------------------------

def _assess(cfg, rows, lights=(), now="22:36", floor="22:19", sub_s=120.0):
    return sl.assess(cfg, "rc16", sl.parse_events(rows, cfg), list(lights),
                     now=U(now), floor=U(floor), sub_s=sub_s)


def test_naive_mount_center_time_is_local(tmp_path):
    cfg = _cfg(tmp_path)
    t = sl.event_time(cfg, "2026-10-14T22:25:09.4002")
    assert t == datetime(2026, 10, 15, 4, 25, 9, 400200)
    assert sl.event_time(cfg, "2026-10-14T22:25:09-06:00") == datetime(2026, 10, 15, 4, 25, 9)


def test_gap_limit(tmp_path):
    cfg = _cfg(tmp_path)
    assert sl.gap_limit_min(cfg, 120) == 20.0
    assert sl.gap_limit_min(cfg, 300) == 25.0
    assert sl.gap_limit_min(cfg, 450) == 32.5
    assert sl.gap_limit_min(cfg, None) == 20.0


def test_loop_signals(tmp_path):
    v = _assess(_cfg(tmp_path), LOOP)
    assert v["state"] == "stuck"
    assert set(v["signals"]) == {"restarts", "park_cycle"}
    assert len(v["af"]) == 3 and len(v["centers"]) == 2


def test_one_target_start_is_not_a_loop(tmp_path):
    rows = LOOP[:6]        # AF + center, then waiting for the first light
    v = _assess(_cfg(tmp_path), rows, now="22:30")
    assert v["state"] == "ok"


def test_two_starts_then_light_is_fine(tmp_path):
    """Two AF + center cycles (a center that moved on to the next target),
    then a light within a sub + 5 min: not a loop."""
    rows = [ev("22:22", "AUTOFOCUS-STARTING"), ev("22:25", "AUTOFOCUS-FINISHED"),
            ev("22:25", "MOUNT-CENTER"), ev("22:26", "AUTOFOCUS-STARTING"),
            ev("22:29", "AUTOFOCUS-FINISHED"), ev("22:29", "MOUNT-CENTER")]
    assert _assess(_cfg(tmp_path), rows, now="22:33")["state"] == "ok"
    assert _assess(_cfg(tmp_path), rows, lights=[U("22:32")], now="22:40")["state"] == "ok"
    # no light 7 min after the second center: the container restarted
    assert _assess(_cfg(tmp_path), rows, now="22:36")["signals"] == ["restarts"]


def test_no_light_gap_and_its_holds(tmp_path):
    cfg = _cfg(tmp_path)
    assert _assess(cfg, [], now="22:38")["state"] == "ok"           # 19 min
    v = _assess(cfg, [], now="22:40")
    assert v["state"] == "stuck" and v["signals"] == ["no_light"]
    # a light resets the clock
    assert _assess(cfg, [], lights=[U("22:30")], now="22:40")["state"] == "ok"
    # any IMAGE-SAVE (a dark, a flat) counts as busy
    assert _assess(cfg, [ev("22:30", "IMAGE-SAVE")], now="22:40")["state"] == "ok"
    # an AF in progress within its normal duration holds it
    v = _assess(cfg, [ev("22:37", "AUTOFOCUS-STARTING")], now="22:40")
    assert v["state"] == "hold" and "autofocus" in v["why"]
    # ... but not one stuck for 15 min
    v = _assess(cfg, [ev("22:20", "AUTOFOCUS-STARTING")], now="22:40")
    assert v["state"] == "stuck"
    # a meridian flip in progress holds it
    v = _assess(cfg, [ev("22:35", "MOUNT-BEFORE-FLIP")], now="22:40")
    assert v["state"] == "hold" and "flip" in v["why"]


def test_unsafe_resets_and_holds(tmp_path):
    cfg = _cfg(tmp_path)
    rows = [ev("22:22", "SAFETY-CHANGED", IsSafe=False), ev("22:27", "MOUNT-PARKED"),
            ev("22:33", "SAFETY-CHANGED", IsSafe=True), ev("22:34", "MOUNT-UNPARKED")]
    v = _assess(cfg, rows, now="22:45")
    assert v["state"] == "ok" and v["since"] == U("22:33")
    v = _assess(cfg, rows[:2], now="22:45")
    assert v["state"] == "hold" and "unsafe" in v["why"]
    # a park -> unpark with an unsafe spell between is not a loop
    rows2 = [ev("22:20", "MOUNT-PARKED"), ev("22:21", "SAFETY-CHANGED", IsSafe=False),
             ev("22:24", "SAFETY-CHANGED", IsSafe=True), ev("22:25", "MOUNT-UNPARKED"),
             ev("22:26", "MOUNT-PARKED"), ev("22:27", "MOUNT-UNPARKED")]
    assert "park_cycle" not in _assess(cfg, rows2, now="22:30")["signals"]


def test_outside_a_target_window_or_after_dawn_skips(tmp_path):
    cfg = _cfg(tmp_path)
    armer = FakeArmer(cfg)
    io = FakeIO(LOOP)
    assert run(sl.check(cfg, armer, io, U("22:10")))["skip"]
    assert run(sl.check(cfg, armer, io, datetime(2026, 10, 15, 12, 30)))["skip"]
    assert io.pushes == []


# --------------------------------------------------------------------------
# 3. the action
# --------------------------------------------------------------------------

def _check(cfg, armer, io, hm):
    return run(sl.check(cfg, armer, io, U(hm)))


def _prime(cfg, armer, io):
    # the armer has been RUNNING since 22:19 (ticks every minute)
    for m in range(19, 34):
        _check(cfg, armer, io, f"22:{m:02d}")


def test_advise_default_pushes_once_per_episode(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(cfg), FakeIO(LOOP)
    _prime(cfg, armer, io)
    r = _check(cfg, armer, io, "22:34")
    assert r["rc16"]["episode"] == "new" and r["rc16"]["action"] == "advise"
    assert len(io.pushes) == 1 and io.pushes[0][1] == 1
    assert "nothing sent" in io.pushes[0][0]
    _check(cfg, armer, io, "22:37")
    assert len(io.pushes) == 1 and armer.restarts == []
    # a light ends it; the next loop is a new episode
    io.subs_ = [light("22:39")]
    assert _check(cfg, armer, io, "22:40")["rc16"]["episode"] == "cleared"


def test_act_restarts_once_then_alert_only(tmp_path):
    cfg = _cfg(tmp_path, night_pilot_stuck_loop_mode="act")
    armer, io = FakeArmer(cfg), FakeIO(LOOP)
    _prime(cfg, armer, io)
    r = _check(cfg, armer, io, "22:34")
    assert r["rc16"]["action"] == "act"
    assert armer.restarts == [("after_exposure", U("22:34"))]
    # after the restart it loops again: alert only
    again = [*LOOP, ev("22:40", "SEQUENCE-STARTING"),
             *[ev(f"22:{m}", n) for m, n in (
                 ("42", "AUTOFOCUS-STARTING"), ("45", "AUTOFOCUS-FINISHED"),
                 ("45", "MOUNT-CENTER"), ("46", "MOUNT-PARKED"), ("47", "MOUNT-UNPARKED"),
                 ("48", "AUTOFOCUS-STARTING"), ("51", "AUTOFOCUS-FINISHED"),
                 ("51", "MOUNT-CENTER"), ("52", "MOUNT-PARKED"), ("53", "MOUNT-UNPARKED"))]]
    io.hist["rc16"] = again
    for m in range(35, 52):
        _check(cfg, armer, io, f"22:{m}")
    r = _check(cfg, armer, io, "22:54")
    assert r["rc16"]["action"] == "repeat"
    assert len(armer.restarts) == 1
    assert "already restarted it at 22:34" in io.pushes[-1][0]
    assert [k for k, _ in kinds(cfg)] == ["act", "alert", "repeat", "alert"]


def test_calibration_slot_is_not_a_loop(tmp_path):
    cfg = _cfg(tmp_path, night_pilot_stuck_loop_mode="act")
    armer, io = FakeArmer(cfg), FakeIO(LOOP, running=["Take dark frames"])
    _prime(cfg, armer, io)
    r = _check(cfg, armer, io, "22:34")
    assert r["rc16"]["state"] == "hold" and "dark" in r["rc16"]["why"]
    assert io.pushes == [] and armer.restarts == []


def test_act_held_by_guards(tmp_path):
    cfg = _cfg(tmp_path, night_pilot_stuck_loop_mode="act")
    armer, io = FakeArmer(cfg), FakeIO(LOOP)
    armer._dawn = lambda: U("23:00")          # 26 min of dark left
    _prime(cfg, armer, io)
    r = _check(cfg, armer, io, "22:34")
    assert r["rc16"]["action"] == "held" and armer.restarts == []
    assert "held (only 26 min of dark left)" in io.pushes[-1][0]


def test_mode_caps(tmp_path):
    assert sl.action_mode(_cfg(tmp_path)) == "advise"
    assert sl.action_mode(_cfg(tmp_path, night_pilot_stuck_loop_mode="act",
                               night_pilot_mode="advise")) == "advise"
    assert sl.action_mode(_cfg(tmp_path, night_pilot_stuck_loop_mode="act",
                               night_pilot_mode="act")) == "act"
    assert sl.action_mode(_cfg(tmp_path, night_pilot_mode="off")) == "off"
    cfg = _cfg(tmp_path, night_pilot_stuck_loop_mode="off")
    armer, io = FakeArmer(cfg), FakeIO(LOOP)
    _prime(cfg, armer, io)
    _check(cfg, armer, io, "22:34")
    assert len(io.pushes) == 1 and "Night pilot" not in io.pushes[0][0]


def test_piggy_idle_light_loop(tmp_path):
    """NINA #2 idle while it should be imaging: 450 s subs -> 32.5 min."""
    cfg = _cfg(tmp_path, night_pilot_stuck_loop_mode="act")
    armer, io = FakeArmer(cfg), FakeIO([ev("22:19", "SEQUENCE-STARTING")],
                                       subs=[light(f"22:{m}") for m in range(21, 60, 2)])
    io.hist["piggyback"] = [ev("22:19", "SEQUENCE-STARTING")]
    _prime(cfg, armer, io)
    for m in range(30, 51):
        r = _check(cfg, armer, io, f"22:{m}")
        assert r["piggyback"]["state"] == "ok"
    r = _check(cfg, armer, io, "22:52")
    assert r["piggyback"]["episode"] == "new" and r["piggyback"]["action"] == "act"
    assert io.piggy == [True]
    assert io.pushes[-1][0].startswith("Piggy-600 not imaging: no light in 33 min")
    assert armer.restarts == []


def test_piggy_without_a_running_sequence_is_not_judged(tmp_path):
    cfg = _cfg(tmp_path)
    for hist in ([], [ev("22:19", "SEQUENCE-STARTING"), ev("22:20", "SEQUENCE-FINISHED")]):
        v = sl.assess(cfg, "piggyback", sl.parse_events(hist, cfg), [],
                      now=U("23:30"), floor=U("22:19"), sub_s=450.0)
        assert v["state"] == "hold" and "not running" in v["why"]


# --------------------------------------------------------------------------
# 4. hook, reporting, config
# --------------------------------------------------------------------------

def test_armer_tick_gating(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setitem(sl._LIVE, "last_poll", None)
    monkeypatch.setitem(sl._LIVE, "task", None)
    io = FakeIO(LOOP)

    async def go():
        assert sl.armer_tick(FakeArmer(cfg, state="PAUSED_OPERATOR"), U("22:36"), io) is None
        t = sl.armer_tick(FakeArmer(cfg), U("22:36"), io)
        assert t is not None
        res = await t
        assert sl.armer_tick(FakeArmer(cfg), U("22:36") + timedelta(seconds=30), io) is None
        return res
    res = run(go())
    assert "rc16" in res
    off = _cfg(tmp_path, stuck_loop_mode="off")
    monkeypatch.setitem(sl._LIVE, "last_poll", None)

    async def go_off():
        return sl.armer_tick(FakeArmer(off), U("22:40"), io)
    assert run(go_off()) is None


def test_armer_hook_is_wired():
    src = (Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
           / "armer.py").read_text(encoding="utf-8")
    assert "stuck_loop.armer_tick(self, now)" in src


def test_config_and_settings_rows(tmp_path):
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    assert cfg.stuck_loop_mode == "alert"
    assert cfg.night_pilot_stuck_loop_mode == "advise"
    assert cfg.stuck_loop_gap_min == 20.0
    rows = {row[0]: row for row in app._CONFIG_FIELDS}
    for k in ("stuck_loop_mode", "stuck_loop_poll_s", "stuck_loop_gap_subs",
              "stuck_loop_gap_margin_min", "stuck_loop_gap_min", "stuck_loop_repeats",
              "stuck_loop_park_cycles", "night_pilot_stuck_loop_mode"):
        assert rows[k][1] == "PS_" + k.upper() and rows[k][3] == "Night pilot"


def test_morning_report_includes_stuck_loop(tmp_path):
    from photonscript.scheduler import morning_report as mr
    cfg = _cfg(tmp_path)
    sl.save_state(cfg, {"night": NIGHT, "open": {}, "restarts": {},
                        "episodes": [{"rig": "rc16", "t": "2026-10-15T04:35:00Z",
                                      "steps": "RC16 looping: AF 22:21, 22:29",
                                      "action": "advise"}]})
    lines = sl.morning_lines(cfg, NIGHT)
    assert lines == ["Stuck loop 22:35: RC16 looping: AF 22:21, 22:29 "
                     "(advise: the pilot would have restarted it)"]
    src = Path(mr.__file__).read_text(encoding="utf-8")
    assert "stuck_loop import morning_lines" in src
