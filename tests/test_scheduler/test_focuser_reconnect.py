"""Stuck-focuser recovery (2026-10-26, scheduler/focuser_reconnect.py).

Jeremy: "If you get stuck with a focuser, then you can always reset it."

  1. detection (pure): AF runs with points but no fit at one position,
     IsMoving stuck, a commanded move that did not move it, a frozen
     temperature together with failed AF runs; NOT clouds (AF points at
     different positions, HFR 0), NOT a frozen temperature alone (the
     21.29 C EAF quirk);
  2. act: never while the camera exposes or an AF runs; disconnect,
     connect, read back against the focuser guard's baseline, then AF;
     once per rig per night; info push when it works, act push only when
     the reconnect or the AF fails;
  3. a counter reset after the reconnect goes to the focuser guard;
  4. advise / WATCHING: one info line, nothing sent; mode capping;
  5. morning report line, config fields, startup loop.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from photonscript.scheduler import focuser_reconnect as fr
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.night_events import events_path
from photonscript.shared.phd2_store import read_jsonl

NIGHT = "2026-10-25"


def T(hm: str, day: int = 26) -> datetime:
    h, m = hm.split(":")
    return datetime(2026, 10, day, int(h), int(m), 0)


def Z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _cfg(tmp_path, **kw):
    kw.setdefault("observatory_tz", "America/Denver")
    kw.setdefault("piggyback_enabled", False)
    kw.setdefault("night_pilot_mode", "act")
    kw.setdefault("focuser_guard_mode", "advise")
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"), **kw)


class FakeArmer:
    def __init__(self, state="RUNNING"):
        self.state = state
        self.plan = {"night_of": NIGHT}


def foc(pos=5674, temp=12.5, moving=False, connected=True):
    return {"Position": pos, "Temperature": temp, "IsMoving": moving,
            "Connected": connected, "Name": "EAF(MainRC16)"}


def af_events(starts, positions, hfr=2.5, points=5):
    """AF runs (AUTOFOCUS-STARTING + points, never FINISHED)."""
    ev = []
    for i, t0 in enumerate(starts):
        ev.append({"Event": "AUTOFOCUS-STARTING", "Time": Z(t0)})
        for k in range(points):
            pos = positions[i][k] if isinstance(positions[i], list) else positions[i]
            ev.append({"Event": "AUTOFOCUS-POINT-ADDED", "Time": Z(t0 + timedelta(minutes=k + 1)),
                       "Position": pos, "HFR": hfr})
    return ev


class FakeIO:
    def __init__(self, reading=None, after=None, history=None, moves=None,
                 cameras=None, names=None, base=5674.0):
        self.reading = reading or foc()
        self.after = after
        self.hist = list(history or [])
        self.move_list = list(moves or [])
        self.cameras = list(cameras or [{"CameraState": "Idle", "IsExposing": False}])
        self.names = list(names or ["Heart Nebula", "Smart Exposure"])
        self.base = base
        self.reconnected = False
        self.calls = []
        self.pushes = []
        self.af_result = (True, "AUTOFOCUS-FINISHED, focuser 5674 -> 5690")
        self.gmode = "advise"
        self.cal = None

    async def focuser(self, cfg, rig):
        if self.reconnected and self.after is not None:
            return self.after
        return self.reading

    async def last_af(self, cfg, rig):
        return None

    async def history(self, cfg, rig):
        return list(self.hist)

    def moves(self, cfg, rig, since):
        return list(self.move_list)

    async def running(self, cfg, rig):
        return list(self.names), "Heart Nebula"

    async def camera(self, cfg, rig):
        c = self.cameras.pop(0) if len(self.cameras) > 1 else self.cameras[0]
        return c

    def calibration_busy(self):
        return self.cal

    def recipe_busy(self, armer):
        return None

    async def disconnect(self, cfg, rig):
        self.calls.append(("disconnect", rig))
        return {"ok": True}

    async def connect(self, cfg, rig):
        self.calls.append(("connect", rig))
        self.reconnected = True
        return {"ok": True}

    async def sleep(self, s):
        self.calls.append(("sleep", s))

    async def autofocus(self, cfg, rig, armer):
        self.calls.append(("autofocus", rig))
        return self.af_result

    async def notify(self, cfg, msg, *, title=fr.TITLE, priority=0, kind="info", fact=None):
        self.pushes.append({"msg": msg, "priority": priority, "kind": kind, "fact": fact})

    async def guard_check(self, cfg, armer):
        self.calls.append(("guard_check",))
        return {}

    def guard_mode(self, cfg):
        return self.gmode

    def baseline(self, cfg, night, rig):
        return {"pos": self.base, "t": "2026-10-26T01:00:00Z"} if self.base is not None else None


def _run(cfg, io, now, armer=None):
    return asyncio.run(fr.check(cfg, armer or FakeArmer(), io=io, now=now))


STARTS = [T("03:00"), T("03:20"), T("03:40")]


def _stuck_io(**kw):
    return FakeIO(history=af_events(STARTS, [5674, 5674, 5674]), **kw)


def _prime(cfg, io):
    """The watch started early in the night (before the AF runs), as the
    loop does: older runs are judged by points alone otherwise."""
    hist, io.hist = io.hist, []
    _run(cfg, io, T("02:30"))
    io.hist = hist


# ------------------------------------------------------------------ 1. detection

def test_af_stuck_at_one_position_reconnects_and_autofocuses(tmp_path):
    cfg = _cfg(tmp_path)
    io = _stuck_io()
    _prime(cfg, io)
    out = _run(cfg, io, T("04:10"))
    r = out["rigs"]["rc16"]
    assert r["signal"] == "af_stuck" and r["action"] == "recovered"
    names = [c[0] for c in io.calls]
    assert names == ["disconnect", "sleep", "connect", "sleep", "autofocus"]
    assert io.calls[1] == ("sleep", 5.0)
    assert len(io.pushes) == 1 and io.pushes[0]["kind"] == "info"
    assert "never moved from 5674" in io.pushes[0]["msg"]
    assert "position reads 5674 (last good 5674)" in io.pushes[0]["msg"]
    ev = [e for e in read_jsonl(events_path(cfg, NIGHT)) if e.get("kind") == fr.KIND]
    assert [e["value"] for e in ev] == ["stuck", "recovered"]
    # once per rig per night
    io.calls.clear()
    out = _run(cfg, io, T("04:30"))
    assert out["rigs"]["rc16"] == {"skipped": "reconnected once tonight"} and io.calls == []


def test_clouds_do_not_trigger(tmp_path):
    """AF runs that fail on clouds move the focuser (different positions)."""
    cfg = _cfg(tmp_path)
    io = FakeIO(history=af_events(STARTS, [[5600, 5640, 5680, 5720, 5760]] * 3, hfr=0))
    _prime(cfg, io)
    assert _run(cfg, io, T("04:10"))["rigs"]["rc16"] == {"stuck": False}
    assert io.calls == [] and io.pushes == []


def test_frozen_temperature_alone_never_triggers(tmp_path):
    """The Piggy EAF serves 21.29 C all night on good nights."""
    cfg = _cfg(tmp_path)
    io = FakeIO(reading=foc(temp=21.29))
    for i in range(0, 120, 10):
        out = _run(cfg, io, T("02:00") + timedelta(minutes=i))
    assert out["rigs"]["rc16"] == {"stuck": False}
    assert io.calls == [] and io.pushes == []
    # nor with failed AF runs on clouds (HFR 0 everywhere)
    io.hist = af_events(STARTS, [[5600, 5640, 5680, 5720, 5760]] * 3, hfr=0)
    assert _run(cfg, io, T("04:10"))["rigs"]["rc16"] == {"stuck": False}


def test_frozen_temperature_with_failed_af_on_stars_triggers(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO(reading=foc(temp=21.29))
    _run(cfg, io, T("02:30"))
    io.hist = af_events(STARTS, [[5600, 5640, 5680, 5720, 5760]] * 3, hfr=3.1)
    r = _run(cfg, io, T("04:10"))["rigs"]["rc16"]
    assert r["signal"] == "temp_frozen_af" and r["action"] == "recovered"


def test_2026_10_25_piggy_frozen_temp_af_on_clouds_moving_focuser(tmp_path):
    """21.29 C all night, AF failed (some stars), but the focuser reading
    moved during the runs: the temperature quirk plus clouds, no reconnect."""
    cfg = _cfg(tmp_path)
    io = FakeIO(reading=foc(temp=21.29))
    _run(cfg, io, T("02:30"))
    io.hist = af_events(STARTS, [[5600, 5640, 5680, 5720, 5760]] * 3, hfr=1.2)
    for i, t in enumerate((T("03:02"), T("03:22"), T("03:42"))):
        io.reading = foc(pos=5600 + 40 * i, temp=21.29)
        _run(cfg, io, t)
    io.reading = foc(pos=5680, temp=21.29)
    assert _run(cfg, io, T("04:10"))["rigs"]["rc16"] == {"stuck": False}
    assert io.calls == [] and io.pushes == []


def test_is_moving_stuck(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO(reading=foc(moving=True))
    assert _run(cfg, io, T("02:00"))["rigs"]["rc16"] == {"stuck": False}
    assert _run(cfg, io, T("02:03"))["rigs"]["rc16"] == {"stuck": False}
    r = _run(cfg, io, T("02:06"))["rigs"]["rc16"]
    assert r["signal"] == "moving_stuck" and ("disconnect", "rc16") in io.calls


def test_moving_while_the_position_changes_is_normal(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO(reading=foc(moving=True))
    for i in range(8):
        io.reading = foc(pos=5600 + i * 10, moving=True)
        out = _run(cfg, io, T("02:00") + timedelta(minutes=i))
    assert out["rigs"]["rc16"] == {"stuck": False}


def test_a_move_that_did_not_move_it(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO()
    _run(cfg, io, T("02:00"))
    io.move_list = [{"t": Z(T("02:05")), "rig": "rc16", "position": 6000,
                     "source": "focus_model"}]
    assert _run(cfg, io, T("02:06"))["rigs"]["rc16"] == {"stuck": False}   # settling
    r = _run(cfg, io, T("02:08"))["rigs"]["rc16"]
    assert r["signal"] == "move_ignored" and r["action"] == "recovered"
    assert "a move to 6000 (focus_model" in io.pushes[0]["msg"]


def test_a_move_that_moved_it_is_fine(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO()
    _run(cfg, io, T("02:00"))
    io.move_list = [{"t": Z(T("02:05")), "rig": "rc16", "position": 6000}]
    io.reading = foc(pos=6000)
    _run(cfg, io, T("02:06"))
    assert _run(cfg, io, T("02:09"))["rigs"]["rc16"] == {"stuck": False}


def test_diagnose_is_pure_and_needs_points():
    s = {"runs": 3, "min_points": 4, "moving_s": 300, "frozen_s": 1800, "wait_s": 5}
    runs = [{"start": t, "end": t + timedelta(minutes=10), "end_by": "next_start",
             "points": 0, "hfr_zero": 0, "positions": [], "finished": None}
            for t in STARTS]
    rs = {"pos": 5674, "pos_since": Z(T("01:00"))}
    # no points at all (a focuser that dropped off): not "with points"
    assert fr.diagnose(rs, runs, [], s, T("04:30"), T("01:00")) is None


# ------------------------------------------------------------------ 2. act guards

def test_never_while_the_camera_exposes(tmp_path):
    cfg = _cfg(tmp_path)
    io = _stuck_io(cameras=[{"CameraState": "Exposing", "IsExposing": True}])
    _prime(cfg, io)
    r = _run(cfg, io, T("04:10"))["rigs"]["rc16"]
    assert r["deferred"].startswith("the camera is exposing")
    assert io.calls == [] and io.pushes == []
    io.cameras = [{"CameraState": "Idle", "IsExposing": False}]
    assert _run(cfg, io, T("04:12"))["rigs"]["rc16"]["action"] == "recovered"


def test_unreadable_camera_counts_as_busy(tmp_path):
    cfg = _cfg(tmp_path)
    io = _stuck_io(cameras=[{}])
    _prime(cfg, io)
    assert "deferred" in _run(cfg, io, T("04:10"))["rigs"]["rc16"]
    assert io.calls == []


def test_never_while_an_autofocus_runs(tmp_path):
    cfg = _cfg(tmp_path)
    io = _stuck_io()
    _prime(cfg, io)
    io.hist = io.hist + [{"Event": "AUTOFOCUS-STARTING", "Time": Z(T("04:08"))}]
    r = _run(cfg, io, T("04:10"))["rigs"]["rc16"]
    assert r.get("deferred") == "an autofocus is running" and io.calls == []


def test_never_during_a_focus_item_or_a_capture_job(tmp_path):
    cfg = _cfg(tmp_path)
    io = _stuck_io(names=["Heart Nebula", "Run Autofocus"])
    _prime(cfg, io)
    assert _run(cfg, io, T("04:10"))["rigs"]["rc16"]["deferred"] == "NINA runs 'Run Autofocus'"
    io.names = ["Heart Nebula", "Smart Exposure"]
    io.cal = "rc16"
    assert "calibration capture" in _run(cfg, io, T("04:11"))["rigs"]["rc16"]["deferred"]
    assert io.calls == []


def test_connect_that_reads_nothing_back_is_an_act(tmp_path):
    cfg = _cfg(tmp_path)
    io = _stuck_io(after=foc(pos=0, connected=False))
    _prime(cfg, io)
    r = _run(cfg, io, T("04:10"))["rigs"]["rc16"]
    assert r["action"] == "failed" and ("autofocus", "rc16") not in io.calls
    assert io.pushes[-1]["kind"] == "act" and io.pushes[-1]["priority"] == 1
    assert io.pushes[-1]["fact"] == f"focuser-reconnect:rc16:{NIGHT}"
    assert chr(0x2014) not in io.pushes[-1]["msg"]


def test_autofocus_failing_after_the_reconnect_is_an_act(tmp_path):
    cfg = _cfg(tmp_path)
    io = _stuck_io()
    io.af_result = (False, "no AUTOFOCUS-FINISHED in 8 min")
    _prime(cfg, io)
    r = _run(cfg, io, T("04:10"))["rigs"]["rc16"]
    assert r["action"] == "af_failed" and io.pushes[-1]["kind"] == "act"
    assert "autofocus failed: no AUTOFOCUS-FINISHED" in io.pushes[-1]["msg"]


# ------------------------------------------------------------------ 3. counter reset

def test_counter_reset_after_reconnect_goes_to_the_guard_in_act(tmp_path):
    cfg = _cfg(tmp_path, focuser_guard_mode="act")
    io = _stuck_io(after=foc(pos=0))
    io.gmode = "act"
    _prime(cfg, io)
    r = _run(cfg, io, T("04:10"))["rigs"]["rc16"]
    assert r["action"] == "guard"
    assert ("guard_check",) in io.calls and ("autofocus", "rc16") not in io.calls
    assert "counter came back changed (5674 -> 0)" in io.pushes[-1]["msg"]


def test_counter_reset_with_the_guard_in_advise_still_autofocuses(tmp_path):
    cfg = _cfg(tmp_path)
    io = _stuck_io(after=foc(pos=0))
    _prime(cfg, io)
    r = _run(cfg, io, T("04:10"))["rigs"]["rc16"]
    assert r["action"] == "recovered"
    names = [c[0] for c in io.calls]
    assert names.index("guard_check") < names.index("autofocus")
    assert "the counter changed (zero)" in io.pushes[-1]["msg"]


def test_baseline_comes_from_the_focuser_guard(tmp_path):
    from photonscript.scheduler import focuser_guard as fg
    cfg = _cfg(tmp_path)
    assert fr.baseline(cfg, NIGHT, "rc16") is None
    fg.save_last(cfg, {"rc16": {"pos": 5700, "t": "2026-10-24T10:00:00Z"}})
    assert fr.baseline(cfg, NIGHT, "rc16")["pos"] == 5700
    st = fg.load_state(cfg, NIGHT)
    st["rigs"]["rc16"] = {"last": {"pos": 5674, "t": "2026-10-26T03:00:00Z"}}
    fg.save_state(cfg, st)
    assert fr.baseline(cfg, NIGHT, "rc16")["pos"] == 5674


# ------------------------------------------------------------------ 4. modes

def test_advise_reports_once_and_sends_nothing(tmp_path):
    cfg = _cfg(tmp_path, night_pilot_mode="advise")
    assert fr.mode(cfg) == "advise"
    io = _stuck_io()
    _prime(cfg, io)
    r = _run(cfg, io, T("04:10"))["rigs"]["rc16"]
    assert r["advised"] is True and io.calls == []
    assert len(io.pushes) == 1 and io.pushes[0]["kind"] == "info"
    assert "nothing was sent" in io.pushes[0]["msg"]
    _run(cfg, io, T("04:20"))
    assert len(io.pushes) == 1


def test_watching_is_advise(tmp_path):
    cfg = _cfg(tmp_path)
    io = _stuck_io()
    _run(cfg, FakeIO(), T("02:30"), armer=FakeArmer("WATCHING"))
    r = _run(cfg, io, T("04:10"), armer=FakeArmer("WATCHING"))["rigs"]["rc16"]
    assert r["advised"] is True and io.calls == []
    assert "armer WATCHING" in io.pushes[0]["msg"]


@pytest.mark.parametrize("state", ["ARMED", "PAUSED_UNSAFE", "COMPLETE", "DISARMED"])
def test_idle_states_do_nothing(tmp_path, state):
    cfg = _cfg(tmp_path)
    io = _stuck_io()
    out = _run(cfg, io, T("04:10"), armer=FakeArmer(state))
    assert out["skipped"] == "not a live night" and io.calls == []


def test_mode_off_and_caps(tmp_path):
    assert fr.mode(_cfg(tmp_path, focuser_reconnect_mode="off")) == "off"
    assert fr.mode(_cfg(tmp_path, night_pilot_mode="off")) == "advise"
    assert fr.mode(_cfg(tmp_path, focuser_reconnect_mode="bogus")) == "act"
    assert asyncio.run(fr.check(_cfg(tmp_path, focuser_reconnect_mode="off"),
                                FakeArmer(), io=FakeIO())) == {"mode": "off"}


# ------------------------------------------------------------------ 5. reports, wiring

def test_morning_lines_and_report(tmp_path):
    from photonscript.scheduler import morning_report as mr
    cfg = _cfg(tmp_path)
    io = _stuck_io()
    _prime(cfg, io)
    _run(cfg, io, T("04:10"))
    lines = fr.morning_lines(cfg, NIGHT)
    assert lines == ["Focuser reconnect RC16: reconnected, autofocus ok (3 autofocus "
                     "runs in a row did not finish (points 5, 5, 5) and the focuser "
                     "never moved from 5674)."]
    assert fr.status(cfg, NIGHT)["lines"] == lines
    src = open(mr.__file__, encoding="utf-8").read()
    assert "focuser_reconnect import morning_lines" in src


def test_config_defaults_and_fields():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    c = PhotonScriptConfig(_env_file=None)
    assert c.focuser_reconnect_mode == "act"          # like cooler_stall_recovery
    assert c.cooler_stall_recovery is True
    assert c.focuser_reconnect_moving_min == 5.0 and c.focuser_reconnect_wait_s == 5.0
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    assert by_env["PS_FOCUSER_RECONNECT_MODE"][4] == "str"
    assert by_env["PS_FOCUSER_RECONNECT_MOVING_MIN"][4] == "float"
    assert by_env["PS_FOCUSER_RECONNECT_WAIT_S"][4] == "float"


def test_startup_loop_and_only_focuser_endpoints():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    app_src = (root / "photonscript/scheduler/app.py").read_text(encoding="utf-8")
    assert "from photonscript.scheduler.focuser_reconnect import run_loop" in app_src
    src = (root / "photonscript/scheduler/focuser_reconnect.py").read_text(encoding="utf-8")
    assert src.isascii()
    assert "/equipment/focuser/disconnect" in src and "/equipment/focuser/connect" in src
    for bad in ("camera/disconnect", "mount/", "safetymonitor", "/sequence/stop",
                "guider/"):
        assert bad not in src, bad
