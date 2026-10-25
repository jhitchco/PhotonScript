"""Focuser position-loss guard (2026-10-25, scheduler/focuser_guard.py).

  1. pure pieces: connected readings (the 2026-10-24 dawn false alarm),
     zero / jump classification, explanations;
  2. detection over ticks: a drop to 0, the run event and the act push;
  3. no false positive: an autofocus, a commanded move, an AF in progress,
     a disconnected focuser, a reconnect at the same position;
  4. act vs advise: move back to the last good AF of the filter + AF, or
     report only; a sharp first sub = counter only, AF in place;
  5. the exposure guard: never move while the camera exposes or an AF runs;
  6. pre-config: detection and the push only;
  7. the sub doctor cites the reset, the morning report has it, the night
     pilot's focuser push is folded into the guard's, config fields.
"""
import asyncio
import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.scheduler import focuser_guard as fg
from photonscript.scheduler import sub_doctor as sd
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.night_events import events_path
from photonscript.shared.phd2_store import read_jsonl

NIGHT = "2026-10-24"
FIX = Path(__file__).parent / "fixtures" / "sub_doctor"


def T(hm: str, day: int = 25) -> datetime:
    h, m = hm.split(":")
    return datetime(2026, 10, day, int(h), int(m), 0)


def Z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _cfg(tmp_path, **kw):
    kw.setdefault("observatory_tz", "America/Denver")
    kw.setdefault("piggyback_enabled", False)
    kw.setdefault("focuser_guard_mode", "act")
    kw.setdefault("night_pilot_mode", "act")
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"), **kw)


class FakeArmer:
    def __init__(self, state="RUNNING"):
        self.state = state
        self.plan = {"night_of": NIGHT}

    def _recipe_tonight(self):
        return None


def foc(pos, connected=True, temp=12.5, moving=False):
    d = {"Position": pos, "Temperature": temp, "IsMoving": moving,
         "DisplayName": "EAF(MainRC16)", "Name": "EAF(MainRC16)"}
    if connected is not None:
        d["Connected"] = connected
    return d


class FakeIO(fg.GuardIO):
    """Scripted NINA: focuser readings in order (the last repeats), camera
    states in order, the event history, running items, subs, AF points."""

    def __init__(self, readings, cameras=None, history=None, running=None):
        self.readings = list(readings)
        self.cameras = list(cameras or [{"CameraState": "Idle", "IsExposing": False}])
        self.hist = list(history or [])
        self.names = list(running or ["M33", "Smart Exposure"])
        self.filter = "L"
        self.sub_list = []
        self.af_points = []
        self.pushes = []
        self.calls = []
        self.moved_to = None
        self.af_result = (True, "AUTOFOCUS-FINISHED, focuser 5690 -> 5702")
        self.claimed = []

    async def focuser(self, cfg, rig):
        if self.moved_to is not None:
            return foc(self.moved_to)
        r = self.readings.pop(0) if len(self.readings) > 1 else self.readings[0]
        return r

    async def camera(self, cfg, rig):
        c = self.cameras.pop(0) if len(self.cameras) > 1 else self.cameras[0]
        self.calls.append(("camera", c.get("CameraState")))
        return c

    async def history(self, cfg, rig):
        return list(self.hist)

    async def filter_name(self, cfg, rig):
        return self.filter

    async def running(self, cfg, rig):
        return list(self.names), "M33"

    async def move(self, cfg, rig, position):
        self.calls.append(("move", int(position)))
        self.moved_to = int(position)
        return {"ok": True}

    async def autofocus(self, cfg, rig, armer):
        self.calls.append(("autofocus", rig))
        return self.af_result

    def subs(self, cfg, night):
        return list(self.sub_list)

    def points(self, cfg, rig):
        return list(self.af_points)

    def calibration_busy(self):
        return None

    def claim_zero_alert(self, cfg, rig, night):
        self.claimed.append(rig)

    async def notify(self, cfg, msg, *, title=fg.TITLE, priority=1, kind="act", fact=None):
        self.pushes.append({"msg": msg, "title": title, "kind": kind, "fact": fact,
                            "priority": priority})

    async def sleep(self, s):
        return None


def run(cfg, io, now, armer=None, phase="night"):
    return asyncio.run(fg.check(cfg, armer or FakeArmer(), io, now=now, phase=phase))


def _events(cfg, kind=None):
    rows = read_jsonl(events_path(cfg, NIGHT))
    return [r for r in rows if kind is None or r.get("kind") == kind]


def _detect(cfg, io):
    run(cfg, io, T("05:30"))        # baseline 5674
    return run(cfg, io, T("05:41"))  # 0


# --------------------------------------------------------------------------
# 1. pure pieces
# --------------------------------------------------------------------------

def test_connected_position_dawn_disconnect_is_not_a_reading():
    assert fg.connected_position(foc(5674)) == 5674
    assert fg.connected_position(foc(0, connected=False)) is None
    # 2026-10-24 12:39Z: no Connected field, Position 0, Temperature NaN
    assert fg.connected_position(foc(0, connected=None, temp=float("nan"))) is None
    # the 2026-10-08 dead focuser said Connected=true: still a reading
    assert fg.connected_position(foc(0, connected=True, temp=21.29)) == 0
    assert fg.connected_position(None) is None
    assert fg.connected_position({"Position": "x"}) is None


def test_classify_zero_and_jump(tmp_path):
    th = fg.thresholds(_cfg(tmp_path), "rc16")
    assert th["jump_steps"] == 3000                  # travel unknown
    assert fg.classify(5674, 0, th) == "zero"
    assert fg.classify(5674, 5600, th) is None
    assert fg.classify(1500, 0, th) is None          # not a big enough drop
    assert fg.classify(5674, 9000, th) == "jump"
    th2 = fg.thresholds(_cfg(tmp_path, focuser_guard_travel_rc16=10000,
                             focuser_guard_jump_frac=0.2), "rc16")
    assert th2["jump_steps"] == 2000
    assert fg.classify(5674, 7800, th2) == "jump"


def test_explain_af_only_explains_a_jump():
    t0, t1 = T("05:30"), T("05:41")
    hist = [{"Event": "AUTOFOCUS-FINISHED", "Time": "2026-10-25T05:35:00Z"}]
    assert fg.explain("jump", 9000, t0, t1, hist, [])
    assert fg.explain("zero", 0, t0, t1, hist, []) is None
    moves = [{"t": "2026-10-25T05:36:00Z", "position": 20, "source": "x"}]
    assert "commanded move" in fg.explain("zero", 0, t0, t1, [], moves)
    old = [{"t": "2026-10-25T04:00:00Z", "position": 0, "source": "x"}]
    assert fg.explain("zero", 0, t0, t1, [], old) is None


# --------------------------------------------------------------------------
# 2. detection
# --------------------------------------------------------------------------

def test_drop_to_zero_is_detected_with_event_and_act_push(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO([foc(5674), foc(0)])
    io.hist = [{"Event": "FOCUSER-CONNECTED", "Time": "2026-10-25T05:40:10Z"}]
    out = _detect(cfg, io)
    r = out["rigs"]["rc16"]["reset"]
    assert r["from"] == 5674 and r["to"] == 0 and r["kind"] == "zero"
    ev = _events(cfg, fg.KIND_RESET)
    assert len(ev) == 1 and ev[0]["value"] == "5674 -> 0" and ev[0]["rig"] == "rc16"
    assert len(io.pushes) == 1
    p = io.pushes[0]
    assert p["kind"] == "act" and p["title"] == fg.TITLE and p["priority"] >= 1
    assert p["fact"] == f"focuser-reset:rc16:{NIGHT}"
    msg = p["msg"]
    for frag in ("5674 -> 0", "EAF(MainRC16)", "If you act:", "If not:",
                 "focus_counter_epoch", "focuser-connected", "Pilot (act)"):
        assert frag in msg, frag
    assert msg.isascii() and chr(0x2014) not in msg
    assert io.claimed == ["rc16"]          # the agent's "focuser at 0" is folded in
    assert fg.pushed_tonight(cfg, NIGHT, "rc16")


def test_second_tick_does_not_detect_again(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO([foc(5674), foc(0), foc(0)])
    _detect(cfg, io)
    run(cfg, io, T("05:42"))
    assert len(_events(cfg, fg.KIND_RESET)) == 1 and len(io.pushes) == 1


def test_baseline_survives_across_nights(tmp_path):
    cfg = _cfg(tmp_path)
    run(cfg, FakeIO([foc(5674)]), T("12:00", day=24))
    io = FakeIO([foc(0)])
    out = run(cfg, io, T("03:00"))
    assert out["rigs"]["rc16"]["reset"]["from"] == 5674


# --------------------------------------------------------------------------
# 3. no false positives
# --------------------------------------------------------------------------

def test_dawn_disconnect_then_reconnect_same_place_is_quiet(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO([foc(5674), foc(0, connected=None, temp=float("nan")),
                 foc(0, connected=False), foc(5674)])
    for hm in ("05:30", "05:31", "05:32", "05:33"):
        run(cfg, io, T(hm))
    assert not io.pushes and not _events(cfg, fg.KIND_RESET)


def test_reset_after_reconnect_is_judged_against_last_connected(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO([foc(5674), foc(0, connected=False), foc(3)])
    for hm in ("05:30", "05:31", "05:32"):
        out = run(cfg, io, T(hm))
    assert out["rigs"]["rc16"]["reset"]["from"] == 5674
    assert out["rigs"]["rc16"]["reset"]["to"] == 3


def test_jump_after_autofocus_is_explained(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO([foc(5674), foc(9100)])
    io.hist = [{"Event": "AUTOFOCUS-STARTING", "Time": "2026-10-25T05:32:00Z"},
               {"Event": "AUTOFOCUS-FINISHED", "Time": "2026-10-25T05:35:00Z"}]
    out = _detect(cfg, io)
    assert "autofocus finished" in out["rigs"]["rc16"]["explained"]
    assert not io.pushes and not _events(cfg, fg.KIND_RESET)
    assert [e["value"] for e in _events(cfg, fg.KIND)] == ["explained"]


def test_commanded_move_is_explained(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO([foc(5674), foc(400)])
    run(cfg, io, T("05:30"))
    fg.note_move(cfg, "rc16", 410, "test", T("05:35"))
    out = run(cfg, io, T("05:41"))
    assert "commanded move" in out["rigs"]["rc16"]["explained"]
    assert not io.pushes


def test_reading_during_autofocus_is_skipped_and_baseline_kept(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO([foc(5674), foc(400), foc(5700)])
    io.hist = [{"Event": "AUTOFOCUS-STARTING", "Time": "2026-10-25T05:40:00Z"}]
    run(cfg, io, T("05:30"))
    out = run(cfg, io, T("05:41"))
    assert out["rigs"]["rc16"]["skipped"].startswith("autofocus")
    io.hist.append({"Event": "AUTOFOCUS-FINISHED", "Time": "2026-10-25T05:43:00Z"})
    run(cfg, io, T("05:44"))
    assert not io.pushes
    assert fg.load_state(cfg, NIGHT)["rigs"]["rc16"]["last"]["pos"] == 5700


def test_moving_focuser_is_not_judged(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO([foc(5674), foc(100, moving=True)])
    run(cfg, io, T("05:30"))
    out = run(cfg, io, T("05:31"))
    assert out["rigs"]["rc16"]["skipped"] == "focuser moving" and not io.pushes


# --------------------------------------------------------------------------
# 4. act vs advise
# --------------------------------------------------------------------------

def _points():
    return [{"filter": "L", "position": 5690, "time": "2026-10-25T04:10:00Z"},
            {"filter": "Ha", "position": 5810, "time": "2026-10-25T05:00:00Z"},
            {"filter": "L", "position": 5600, "time": "2026-10-23T04:10:00Z"}]


def _sub(start: datetime, hfr: float, passed: bool, rig="rc16"):
    return {"rig": rig, "file": f"{rig}_{start:%H%M}.fits", "target": "M33",
            "filter": "L", "exp_s": 180, "start_utc": Z(start),
            "end_utc": Z(start + timedelta(seconds=180)), "hfr": hfr,
            "passed_qa": passed}


def _with_subs(io, after_hfr, after_pass):
    io.sub_list = [_sub(T("05:00"), 2.3, True), _sub(T("05:10"), 2.4, True),
                   _sub(T("05:45"), after_hfr, after_pass)]


def test_act_soft_sub_moves_back_to_last_good_af_of_the_filter_then_af(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO([foc(5674), foc(0)])
    io.af_points = _points()
    _detect(cfg, io)
    assert not [c for c in io.calls if c[0] == "move"]     # waits for a sub
    _with_subs(io, 8.3, False)
    run(cfg, io, T("05:49"))
    moves = [c for c in io.calls if c[0] in ("move", "autofocus")]
    assert moves == [("move", 5690), ("autofocus", "rc16")]
    r = fg.resets(cfg, NIGHT, "rc16")[-1]
    assert r["verdict"] == "soft" and "focuser at 5690" in r["outcome"]
    assert [e["value"] for e in _events(cfg, fg.KIND)] == ["restored"]
    assert any(m["kind"] == "info" and "moved" in m["msg"] for m in io.pushes)
    # the guard's own move is logged as commanded (no second alarm)
    assert fg.recent_moves(cfg, "rc16", T("05:40"))[-1]["position"] == 5690


def test_advise_reports_only(tmp_path):
    cfg = _cfg(tmp_path, focuser_guard_mode="advise")
    io = FakeIO([foc(5674), foc(0)])
    io.af_points = _points()
    _detect(cfg, io)
    assert "Pilot (advise, nothing is sent)" in io.pushes[0]["msg"]
    _with_subs(io, 8.3, False)
    run(cfg, io, T("05:49"))
    assert not [c for c in io.calls if c[0] in ("move", "autofocus")]
    assert "advise" in fg.resets(cfg, NIGHT, "rc16")[-1]["outcome"]
    assert [e["value"] for e in _events(cfg, fg.KIND)] == ["advise"]


def test_night_pilot_advise_caps_the_guard(tmp_path):
    assert fg.mode(_cfg(tmp_path, night_pilot_mode="advise")) == "advise"
    assert fg.mode(_cfg(tmp_path, night_pilot_mode="off")) == "advise"
    assert fg.mode(_cfg(tmp_path, focuser_guard_mode="off")) == "off"
    assert run(_cfg(tmp_path, focuser_guard_mode="off"), FakeIO([foc(0)]),
               T("05:30")) == {"mode": "off"}


def test_sharp_first_sub_means_counter_only_af_in_place(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO([foc(5674), foc(0)])
    io.af_points = _points()
    _detect(cfg, io)
    _with_subs(io, 2.5, True)
    run(cfg, io, T("05:49"))
    assert [c for c in io.calls if c[0] in ("move", "autofocus")] == [("autofocus", "rc16")]
    r = fg.resets(cfg, NIGHT, "rc16")[-1]
    assert r["verdict"] == "sharp"


def test_no_sub_within_the_wait_restores_as_if_moved(tmp_path):
    cfg = _cfg(tmp_path, focuser_guard_sub_wait_min=10)
    io = FakeIO([foc(5674), foc(0)])
    io.af_points = _points()
    _detect(cfg, io)
    run(cfg, io, T("05:45"))
    assert not [c for c in io.calls if c[0] == "move"]
    run(cfg, io, T("05:56"))
    assert ("move", 5690) in io.calls


def test_restore_target_falls_back(tmp_path):
    cfg = _cfg(tmp_path)
    pts = _points()
    assert fg.restore_target(cfg, "rc16", "H", T("05:41"), pts, 5674)[0] == 5810
    assert fg.restore_target(cfg, "rc16", "L", T("04:00"), pts, 5674)[0] == 5600
    assert fg.restore_target(cfg, "rc16", "L", None, [], 5674) == (5674, "last known position")


# --------------------------------------------------------------------------
# 5. the exposure guard
# --------------------------------------------------------------------------

def test_never_moves_while_the_camera_exposes(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO([foc(5674), foc(0)],
                cameras=[{"CameraState": "Exposing", "IsExposing": True}])
    io.af_points = _points()
    _detect(cfg, io)
    _with_subs(io, 8.3, False)
    run(cfg, io, T("05:49"))
    assert not [c for c in io.calls if c[0] in ("move", "autofocus")]
    assert "never went idle" in fg.resets(cfg, NIGHT, "rc16")[-1]["outcome"]
    fail = [m for m in io.pushes if m["title"] == fg.TITLE_FAIL]
    assert fail and fail[0]["kind"] == "act" and "If you act:" in fail[0]["msg"]


def test_waits_for_the_sub_to_end_then_moves(tmp_path):
    cfg = _cfg(tmp_path)
    exposing = {"CameraState": "Exposing", "IsExposing": True}
    idle = {"CameraState": "Idle", "IsExposing": False}
    io = FakeIO([foc(5674), foc(0)], cameras=[exposing, exposing, idle])
    io.af_points = _points()
    _detect(cfg, io)
    _with_subs(io, 8.3, False)
    run(cfg, io, T("05:49"))
    seq = [c for c in io.calls if c[0] in ("camera", "move")]
    assert seq[-1] == ("move", 5690) and seq[-2] == ("camera", "Idle")


def test_never_moves_during_an_autofocus_run(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO([foc(5674), foc(0)])
    io.af_points = _points()
    _detect(cfg, io)
    io.hist = [{"Event": "AUTOFOCUS-STARTING", "Time": "2026-10-25T05:48:30Z"}]
    _with_subs(io, 8.3, False)
    run(cfg, io, T("05:49"))
    assert not [c for c in io.calls if c[0] == "move"]
    assert "autofocus is running" in fg.resets(cfg, NIGHT, "rc16")[-1]["outcome"]


def test_never_moves_during_a_flat_or_focus_item(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO([foc(5674), foc(0)])
    io.af_points = _points()
    _detect(cfg, io)
    io.names = ["Dawn flats", "Take Exposure"]
    _with_subs(io, 8.3, False)
    run(cfg, io, T("05:49"))
    assert not [c for c in io.calls if c[0] == "move"]


def test_paused_night_detects_but_does_not_decide(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO([foc(5674), foc(0)])
    armer = FakeArmer("PAUSED_UNSAFE")
    run(cfg, io, T("05:30"), armer)
    run(cfg, io, T("05:41"), armer)
    assert len(io.pushes) == 1
    _with_subs(io, 8.3, False)
    run(cfg, io, T("05:49"), armer)
    assert not [c for c in io.calls if c[0] == "move"]
    assert run(cfg, io, T("05:50"), FakeArmer("COMPLETE"))["skipped"]


# --------------------------------------------------------------------------
# 6. pre-config
# --------------------------------------------------------------------------

def test_preconfig_detects_and_pushes_without_moving(tmp_path):
    cfg = _cfg(tmp_path)
    run(cfg, FakeIO([foc(5674)]), T("12:00", day=24))
    io = FakeIO([foc(0)])
    io.af_points = _points()
    out = run(cfg, io, T("00:30"), FakeArmer("ARMED"), phase="preconfig")
    assert out["rigs"]["rc16"]["reset"]["phase"] == "preconfig"
    assert "Found at pre-config" in io.pushes[0]["msg"]
    assert not [c for c in io.calls if c[0] in ("move", "autofocus")]
    # the night decides on the first sub
    assert fg.load_state(cfg, NIGHT)["rigs"]["rc16"]["pending"]


def test_piggy_rig_is_watched_when_enabled(tmp_path):
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    io = FakeIO([foc(11000), foc(11000), foc(0), foc(0)])
    run(cfg, io, T("05:30"))
    out = run(cfg, io, T("05:41"))
    assert set(out["rigs"]) == {"rc16", "piggyback"}
    assert any(e["rig"] == "piggyback" for e in _events(cfg, fg.KIND_RESET))


# --------------------------------------------------------------------------
# 7. surfaces
# --------------------------------------------------------------------------

def test_morning_lines(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO([foc(5674), foc(0)])
    io.af_points = _points()
    _detect(cfg, io)
    _with_subs(io, 8.3, False)
    run(cfg, io, T("05:49"))
    lines = fg.morning_lines(cfg, NIGHT)
    assert len(lines) == 1 and lines[0].startswith("Focuser reset: RC16 5674 -> 0 at ")
    assert "focuser at 5690" in lines[0]
    from photonscript.scheduler.morning_report import card_lines
    assert any(x.startswith("Focuser reset") for x in card_lines({"date": NIGHT, "night_pilot": lines}))


def _set_row(rec, check, status, value=None):
    rows = rec.setdefault("scorecard", {}).setdefault("rows", [])
    for r in rows:
        if r[0] == check:
            r[3] = status
            if value is not None:
                r[1] = value
            return
    rows.append([check, value, None, status])


def test_sub_doctor_focuser_verdict_cites_the_reset(tmp_path):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    cfg = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"),
                             observatory_tz="America/Denver", piggyback_enabled=True)
    d = json.loads((FIX / "2026-10-05_subs.json").read_text())
    subs = copy.deepcopy(d["subs"])
    cut = "2026-10-06T09:00"
    for r in subs:
        if (r.get("start_utc") or "") >= cut and r["rig"] == "rc16":
            r["hfr"] = (r.get("hfr") or 3) * 1.8
            _set_row(r, "hfr", "fail", r["hfr"])
            _set_row(r, "ecc", "pass")
    ev = [{"t": "2026-10-06T08:58:00Z", "rig": "rc16", "kind": "focuser_reset",
           "value": "5674 -> 0", "detail": "x"}]
    rep = sd.replay_night(cfg, d["night"], subs, plan=d["plan"], solves={}, events=ev)
    f = rep["rigs"]["rc16"]["fired"]
    assert f and f[0]["cause"] == "focuser" and f[0]["confidence"] == "high"
    assert "reset at 08:58Z" in f[0]["headline"] and "5674 -> 0" in f[0]["headline"]
    assert f[0]["evidence"]["focuser_reset_t"] == "2026-10-06T08:58:00Z"
    # without the event the same subs read as focus (the control)
    rep2 = sd.replay_night(cfg, d["night"], subs, plan=d["plan"], solves={})
    assert rep2["rigs"]["rc16"]["fired"][0]["cause"] == "focus"


def test_sub_doctor_ignores_a_reset_after_an_af(tmp_path):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    cfg = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"),
                             observatory_tz="America/Denver", piggyback_enabled=True)
    d = json.loads((FIX / "2026-10-05_subs.json").read_text())
    subs = copy.deepcopy(d["subs"])
    for r in subs:
        if (r.get("start_utc") or "") >= "2026-10-06T09:00" and r["rig"] == "rc16":
            r["hfr"] = (r.get("hfr") or 3) * 1.8
            _set_row(r, "hfr", "fail", r["hfr"])
            _set_row(r, "ecc", "pass")
    ev = [{"t": "2026-10-06T08:00:00Z", "rig": "rc16", "kind": "focuser_reset",
           "value": "5674 -> 0"}]
    nina = {"rc16": [{"Event": "AUTOFOCUS-FINISHED", "Time": "2026-10-06T08:20:00Z"}]}
    rep = sd.replay_night(cfg, d["night"], subs, plan=d["plan"], solves={}, events=ev,
                          nina_events=nina)
    assert rep["rigs"]["rc16"]["fired"][0]["cause"] != "focuser"


def test_night_pilot_focuser_push_folds_into_the_guards(tmp_path):
    from photonscript.scheduler import night_pilot as npil
    cfg = _cfg(tmp_path)
    io = FakeIO([foc(5674), foc(0)])
    _detect(cfg, io)

    class PIO(npil.PilotIO):
        def __init__(self):
            self.pushes = []

        async def notify(self, cfg, msg, priority=0, kind="info", fact=None):
            self.pushes.append(kind)
    pio = PIO()
    st = npil.load_state(cfg, NIGHT)
    d = {"rig": "rc16", "cause": "focuser", "family": "focuser", "confidence": "high",
         "headline": "RC16 focuser", "dt": T("05:50"), "evidence": {}}
    asyncio.run(npil._handle(cfg, FakeArmer(), pio, st, d, {"subs": []}, T("05:50"), "act"))
    assert pio.pushes == ["info"]
    st2 = npil.load_state(cfg, "2026-10-23")
    pio2 = PIO()
    asyncio.run(npil._handle(cfg, FakeArmer(), pio2, dict(st2, night="2026-10-23"), d,
                             {"subs": []}, T("05:50"), "act"))
    assert pio2.pushes == ["act"]


def test_config_fields_and_ascii(tmp_path):
    from photonscript.scheduler.app import _CONFIG_FIELDS
    by = {f[0]: f for f in _CONFIG_FIELDS}
    for k in ("focuser_guard_mode", "focuser_guard_reset_below", "focuser_guard_drop_steps",
              "focuser_guard_jump_frac", "focuser_guard_travel_rc16",
              "focuser_guard_travel_piggyback", "focuser_guard_sub_wait_min",
              "focuser_guard_af_after_move", "focuser_guard_poll_s"):
        assert k in by and by[k][3] == "Night pilot", k
        assert by[k][1] == "PS_" + k.upper()
    c = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path))
    assert c.focuser_guard_reset_below == 1000 and c.focuser_guard_drop_steps == 3000
    src = Path(fg.__file__).read_text(encoding="utf-8")
    assert src.isascii()
