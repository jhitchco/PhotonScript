"""PS-154: cooling never gates the night (2026-10-06 incident).

NINA #1's generated full-session sequence sat on its Start-area Cool Camera
item (Temperature -10, PS_CAMERA_SETPOINT_C 0) from 19:45 scope clock: NINA's
CoolCamera waits until the sensor is within 1 C of its target with no timeout
of its own, the armer's cooler nanny held the driver at the configured 0 C
every 30 s, the sensor sat at 0.9 C, so the item never finished: no unpark,
no tracking, no RC16 lights all night. The -10 came from the
NinaSequenceTarget.camera_temp_c model default: the PS-144 dusk focus
calibration target (armer._focus_calibration_target) is built without
camera_temp_c, is inserted FIRST, and the Start area cooled to targets[0].

  1. setpoint source of truth: every generated CoolCamera (full session,
     tracking / optics tests, focus calibration, darks, dusk flats, Piggy
     companion, legacy XML) uses the rig's configured setpoint; a per-target
     value is ignored with a log line; the model default is None;
  2. bounded: every CoolCamera sits in a run-once container with a
     TimeSpanCondition of cooler_gate_timeout_min (+ ramp); a NINA simulator
     with a cooler that never reaches the target replays the night: the old
     shape images nothing, the new one images;
  3. lint rule cooling: setpoint mismatch and an unbounded cool fail (WARN
     for a hand-built sideload), and every generator lints clean;
  4. NINA watch: "stuck" (one non-long item too long) and "parked" (RC16
     not tracked yet past dusk + 15) page once, priority 1, with the item;
     the cooler nanny pages instead of fighting a sequence CoolCamera;
  5. no PHD2 guider restart while the mount is parked / not tracking.
"""
import asyncio
import copy
import json
import logging
import math
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import nina_watchdog as nw
from photonscript.scheduler import sideload as sd
from photonscript.scheduler.nina_sequence import (build_sequence_for_night,
                                                  generate_nina_xml)
from photonscript.scheduler.sequence_lint import check_cooling, lint, LintResult
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget

from tests.test_scheduler.test_ps77_safety_stop import (
    HORIZON, NinaSim, _Interrupt, _short, _vals, _walk)

T_DUSK = datetime(2026, 10, 7, 2, 30, 0)   # astro dusk 19:30 MST, as UTC


@pytest.fixture
def cfg0(monkeypatch):
    """The live config of 2026-10-06: RC16 setpoint 0 C, Piggy 0 C."""
    monkeypatch.setenv("PS_CAMERA_SETPOINT_C", "0.0")
    monkeypatch.setenv("PS_PIGGYBACK_SETPOINT_C", "0.0")
    cfg = PhotonScriptConfig(_env_file=None)
    monkeypatch.setattr(nsj, "_gen_cfg_cache", cfg)
    return cfg


def _heart(**kw):
    return NinaSequenceTarget(
        name="Heart Nebula", ra_hours=2.55, dec_degrees=61.5, **kw,
        exposures=[ExposurePlan(filter_type=FilterType.HA, exposure_seconds=300,
                                count=20, gain=200, offset=256)])


def _focus_cal_first():
    """Exactly what armer._focus_calibration_target builds (no camera_temp_c)."""
    return NinaSequenceTarget(
        name=f"{nsj.FOCUS_CAL_PREFIX}NGC 7789", ra_hours=23.957,
        dec_degrees=56.72, focus_calibration=True, focus_calibration_rounds=1,
        focus_calibration_filters=["L", "Ha", "L", "OIII", "L", "SII", "L"])


def _night(targets):
    s = build_sequence_for_night("PhotonScript_20261006", targets)
    s.wait_until_local = "00:00:00"
    return json.loads(nsj.generate_nina_json(s))


def _cools(seq):
    return [d for d in _walk(seq) if _short(d.get("$type", "")) == "CoolCamera"]


def _boxes(seq):
    return [d for d in _walk(seq)
            if str(d.get("Name") or "").startswith(nsj.COOL_BOUNDED_PREFIX)]


def _span_min(box):
    span = [c for c in _vals(box, "Conditions")
            if _short(c["$type"]) == "TimeSpanCondition"]
    assert len(span) == 1
    return span[0]["Hours"] * 60 + span[0]["Minutes"] + span[0]["Seconds"] / 60


def _errors(res, rule="cooling"):
    return [f.detail for f in res.findings if f.level == "ERROR" and f.rule == rule]


# ------------------------------------------------------- 1. setpoint from config

def test_model_default_is_none_not_minus_10():
    assert NinaSequenceTarget(name="x", ra_hours=1, dec_degrees=2).camera_temp_c is None


def test_regression_2026_10_06_focus_cal_first_cools_to_config(cfg0):
    """targets[0] without camera_temp_c (the PS-144 dusk focus calibration)
    -> the Start-area CoolCamera is the configured 0.0 C, bounded, lint clean."""
    seq = _night([_focus_cal_first(), _heart()])
    start = [d for d in _walk(seq) if "StartAreaContainer" in d.get("$type", "")][0]
    cools = _cools(start)
    assert len(cools) == 1 and cools[0]["Temperature"] == 0.0
    assert all(c["Temperature"] == 0.0 for c in _cools(seq))
    assert "-10" not in json.dumps([c["Temperature"] for c in _cools(seq)])
    res = lint(seq)
    assert res.ok, [f"{f.rule}: {f.detail}" for f in res.findings if f.level == "ERROR"]
    assert not [f for f in res.findings if f.rule == "cooling"]


def test_per_target_override_is_ignored_with_a_log_line(cfg0, caplog):
    caplog.set_level(logging.WARNING, logger=nsj.__name__)
    seq = _night([_heart(camera_temp_c=-10.0)])
    assert [c["Temperature"] for c in _cools(seq)] == [0.0]
    assert any("camera_temp_c -10 C ignored" in r.getMessage() for r in caplog.records)
    caplog.clear()
    _night([_heart(camera_temp_c=0.0)])   # equal to config: no noise
    assert not [r for r in caplog.records if "ignored" in r.getMessage()]


def test_config_setpoint_flows_to_cool_and_gate(monkeypatch, tmp_path):
    script = tmp_path / "cooler-gate.cmd"
    script.write_text("@echo off\n", encoding="ascii")
    monkeypatch.setenv("PS_COOLER_GATE_MODE", "skip")
    monkeypatch.setenv("PS_COOLER_GATE_SCRIPT", str(script))
    monkeypatch.setenv("PS_CAMERA_SETPOINT_C", "-5")
    monkeypatch.setattr(nsj, "_gen_cfg_cache", PhotonScriptConfig(_env_file=None))
    seq = _night([_focus_cal_first(), _heart(camera_temp_c=-10.0)])
    assert {c["Temperature"] for c in _cools(seq)} == {-5.0}
    gates = [d for d in _walk(seq) if "cooler-gate" in str(d.get("Script", ""))]
    assert gates and all("--setpoint=-5 " in g["Script"] for g in gates)
    assert lint(seq).ok


def test_legacy_xml_uses_config(cfg0):
    s = build_sequence_for_night("x", [_heart()])
    assert "<Temperature>0.0</Temperature>" in generate_nina_xml(s)


# ------------------------------------------------------- 2. every cool bounded

def test_start_area_cool_is_bounded_by_the_gate_timeout(cfg0):
    seq = _night([_heart()])
    boxes = _boxes(seq)
    assert len(boxes) == len(_cools(seq)) == 1
    box = boxes[0]
    assert _span_min(box) == 20                          # cooler_gate_timeout_min
    kinds = [_short(c["$type"]) for c in _vals(box, "Conditions")]
    assert kinds == ["LoopCondition", "TimeSpanCondition"]
    span = _vals(box, "Conditions")[1]
    assert span["Parent"] == {"$ref": box["$id"]}         # PS-77: the watchdog needs it
    # the bounded cool still comes before the unpark, but cannot hold it
    order = [_short(d.get("$type", "")) for d in _walk(seq)]
    assert order.index("CoolCamera") < order.index("UnparkScope")


def test_bound_follows_config_and_adds_the_ramp(monkeypatch):
    monkeypatch.setenv("PS_COOLER_GATE_TIMEOUT_MIN", "7")
    monkeypatch.setenv("PS_COOL_RAMP_MINUTES", "2.5")
    seq = _night([_heart()])
    assert _span_min(_boxes(seq)[0]) == 7 + 3
    assert lint(seq).ok
    assert nsj.cool_bound_minutes(SimpleNamespace(cooler_gate_timeout_min=0)) == 1
    assert nsj.cool_bound_minutes(SimpleNamespace(cooler_gate_timeout_min="x")) == 20


def _generated(cfg0):
    """Every generator that emits a CoolCamera, with the lint that applies."""
    from photonscript.scheduler.calibration import (
        generate_darks_json, generate_dusk_flats_json,
        generate_piggyback_companion_json)
    from photonscript.shared.rigs import PIGGYBACK, rig_config
    pig = rig_config(cfg0, PIGGYBACK)
    out = {
        "night": (_night([_focus_cal_first(), _heart()]), "rc16"),
        "focus_cal": (json.loads(nsj.generate_focus_calibration_json()), "rc16"),
        "tracking_test": (json.loads(nsj.generate_tracking_test_json()), "rc16"),
        "optics_test": (json.loads(nsj.generate_optics_test_json()), "rc16"),
        "darks": (json.loads(generate_darks_json(cfg0, [(300.0, 5)])[0]), "rc16"),
        "dusk_flats": (json.loads(generate_dusk_flats_json(cfg0)[0]), "rc16"),
        "piggy_flats": (json.loads(generate_dusk_flats_json(
            pig, osc=True, owns_mount=False)[0]), PIGGYBACK),
        "companion": (json.loads(generate_piggyback_companion_json(
            pig, has_safety=True, with_lights=True)), PIGGYBACK),
    }
    return out


def test_every_generated_cool_is_config_and_bounded(cfg0):
    for name, (seq, rig) in _generated(cfg0).items():
        cools = _cools(seq)
        assert cools, name
        assert all(c["Temperature"] == 0.0 for c in cools), name
        assert len(_boxes(seq)) == len(cools), name   # each in its own box
        r = LintResult()
        check_cooling(seq, r, rig=rig)
        assert not [f for f in r.findings if f.level == "ERROR"], (name, r.findings)


def test_generators_lint_clean(cfg0):
    g = _generated(cfg0)
    for name in ("night", "focus_cal", "tracking_test", "optics_test"):
        res = lint(g[name][0])
        assert res.ok, (name, [f.detail for f in res.findings if f.level == "ERROR"])
    res = sd.lint_companion(g["companion"][0])
    assert res.ok, [f.detail for f in res.findings if f.level == "ERROR"]


def test_sideload_splice_lints_clean(cfg0):
    night = _night([_heart(), NinaSequenceTarget(
        name="Cat's Eye Nebula", ra_hours=17.98, dec_degrees=66.63,
        exposures=[ExposurePlan(filter_type=FilterType.HA, exposure_seconds=300,
                                count=10, gain=200, offset=256)])])
    tt = json.loads(nsj.generate_tracking_test_json("M 2", 21.56, -0.82))
    out = sd.splice_tracking_test(night, tt, ["Cat's Eye Nebula"])
    res = lint(out)
    assert not _errors(res), _errors(res)
    assert all(c["Temperature"] == 0.0 for c in _cools(out))


# ------------------------------------------- 2b. replay the night in a NINA sim

class CoolSim(NinaSim):
    """NinaSim plus NINA's CoolCamera (waits until the sensor is within 1 C
    of Temperature; `reachable` False = a cooler that never gets there, as
    on 2026-10-06) and TimeSpanCondition (isbeorn/nina master
    TimeSpanCondition.cs: Check = remaining minus the next item's estimate,
    a 1 s ConditionWatchdog that interrupts the Parent when time is up,
    the span starting when the block starts)."""

    def __init__(self, seq, reachable=False, **kw):
        super().__init__(seq, **kw)
        self.reachable = reachable
        self.span_start = {}
        self.cool_done = []

    @staticmethod
    def _span(cond):
        d = cond.d
        return 3600 * d.get("Hours", 0) + 60 * d.get("Minutes", 0) + d.get("Seconds", 0)

    def run_container(self, c):
        for cond in c.conds:
            if cond.type == "TimeSpanCondition":
                self.span_start[id(cond)] = self.t
        return super().run_container(c)

    def check(self, c, nxt):
        if c.type == "TimeSpanCondition":
            start = self.span_start.get(id(c), self.t)
            return self.t + self.duration(nxt) < start + self._span(c)
        return super().check(c, nxt)

    def _interrupt_at(self, t0, t1):
        best = super()._interrupt_at(t0, t1)
        for c in self.stack:
            for cond in c.conds:
                if cond.type != "TimeSpanCondition":
                    continue
                p = cond.parent
                if p is None or p not in self.stack or not self._in_root(p):
                    continue
                f = max(t0, self.span_start[id(cond)] + self._span(cond)) + 1
                if f <= t1 and (best is None or f < best[0]):
                    best = (f, p)
        return best

    def run_instruction(self, n):
        if n.type == "CoolCamera":
            if self.reachable:
                self.advance(300)
                self.cool_done.append(self.t)
            else:
                self.advance(HORIZON - self.t)   # waits forever
            return
        super().run_instruction(n)


def _as_2026_10_06(seq):
    """The deployed shape: the Start-area CoolCamera inline (no bound) at -10."""
    out = copy.deepcopy(seq)
    for d in _walk(out):
        items = _vals(d, "Items")
        for i, it in enumerate(items):
            if str(it.get("Name") or "").startswith(nsj.COOL_BOUNDED_PREFIX):
                inner = dict(_vals(it, "Items")[0])
                inner["Temperature"] = -10.0
                inner["Parent"] = {"$ref": d["$id"]}
                d["Items"]["$values"][d["Items"]["$values"].index(it)] = inner
    return out


def test_sim_old_shape_never_images_new_shape_does(cfg0):
    seq = _night([_focus_cal_first(), _heart()])
    old = CoolSim(_as_2026_10_06(seq)).run()
    assert old.lights == []                       # the lost night
    assert not lint(_as_2026_10_06(seq)).ok       # and lint now refuses it
    new = CoolSim(seq).run()
    assert new.lights, "the bounded cool still blocked the night"
    # the cool started at dusk - 30 and gave up 20 min later, before dark
    assert min(x["start"] for x in new.lights) >= 0
    ok = CoolSim(seq, reachable=True).run()
    assert ok.cool_done and ok.lights


# ------------------------------------------------------------- 3. lint rule

def _cool_seq(temp=0.0, bounded=True, span_min=20, duration=0.0):
    cool = nsj._cool_camera(temp, duration)
    if bounded:
        item = nsj._cool_camera_bounded(temp, duration, bound_min=span_min)
    else:
        item = cool
    root = nsj._seq_container("Root", [nsj._seq_container("Start", [item])],
                              container_type="NINA.Sequencer.Container."
                              "SequenceRootContainer, NINA.Sequencer")
    return nsj.link_parents(root)


def test_lint_cooling_rules(cfg0):
    def run(seq, **kw):
        r = LintResult()
        check_cooling(seq, r, **kw)
        return r
    assert not _errors(run(_cool_seq()))
    e = _errors(run(_cool_seq(-10.0)))
    assert len(e) == 1 and "-10 C but the rc16 setpoint is 0 C" in e[0]
    assert not _errors(run(_cool_seq(-0.8)))                  # inside 1 C
    assert _errors(run(_cool_seq(5.0)))                       # warm sensor
    e = _errors(run(_cool_seq(bounded=False)))
    assert len(e) == 1 and "no TimeSpanCondition bound" in e[0]
    assert _errors(run(_cool_seq(span_min=45)))               # too long a bound
    assert not _errors(run(_cool_seq(span_min=20, duration=2.0)))  # 20 + ramp
    hb = run(_cool_seq(bounded=False), strict=False)
    assert not _errors(hb) and any("no TimeSpanCondition" in f.detail
                                   for f in hb.findings if f.level == "WARN")
    assert not _errors(run(_cool_seq(-5.0), setpoint=-5.0))
    # the full lint and the companion lint carry the rule
    assert not lint(_cool_seq(-10.0)).ok
    assert any("hand" not in f.detail for f in lint(
        _cool_seq(bounded=False), hand_built=True).findings if f.rule == "cooling")
    assert not [f for f in lint(_cool_seq(bounded=False), hand_built=True).findings
                if f.rule == "cooling" and f.level == "ERROR"]
    assert _errors(sd.lint_companion(_cool_seq(-10.0)))
    assert _errors(sd.lint_companion(_cool_seq(bounded=False)))
    assert not _errors(sd.lint_companion(_cool_seq(bounded=False), hand_built=True))


def test_lint_reads_the_piggy_setpoint(monkeypatch):
    monkeypatch.setenv("PS_CAMERA_SETPOINT_C", "0")
    monkeypatch.setenv("PS_PIGGYBACK_SETPOINT_C", "-5")
    assert not _errors(sd.lint_companion(_cool_seq(-5.0)))
    assert _errors(sd.lint_companion(_cool_seq(0.0)))
    assert _errors(lint(_cool_seq(-5.0)))


# ------------------------------------------------------------- 4. NINA watch

def _r(**kw):
    base = {"window": True, "expected": True, "api_ok": True, "process": True,
            "log_quiet_min": 0.5, "seq_leaf": "Cool Camera",
            "seq_exposure_s": None, "leaf_age_min": 5.0, "night_running": True,
            "mount_parked": True, "mount_tracking": False,
            "tracked_tonight": False, "safe": True, "past_dusk_min": 0.0}
    base.update(kw)
    return base


def test_classify_stuck_and_parked():
    st, why = nw.classify(_r(leaf_age_min=26), 15, 25, 15)
    assert st == "stuck" and "'Cool Camera' for 26 min" in why and "PARKED" in why
    assert nw.classify(_r(leaf_age_min=24), 15, 25, 15)[0] == "ok"
    for leaf in ("Take Exposure", "Smart Exposure", "Run Autofocus",
                 "Wait for Time", "Wait until Safe", "Loop Condition"):
        assert nw.classify(_r(seq_leaf=leaf, leaf_age_min=90), 15, 25, 15)[0] == "ok"
    assert nw.classify(_r(night_running=False, leaf_age_min=90), 15, 25, 15)[0] == "ok"
    # parked past dusk + 15 with the roof safe
    st, why = nw.classify(_r(seq_leaf="Wait for Time", past_dusk_min=16), 15, 25, 15)
    assert st == "parked" and "still PARKED 16 min after astro dusk" in why
    assert "Wait for Time" in why
    assert nw.classify(_r(past_dusk_min=14), 15, 25, 15)[0] == "ok"
    assert nw.classify(_r(past_dusk_min=16, safe=False), 15, 25, 15)[0] == "ok"
    assert nw.classify(_r(past_dusk_min=16, safe=None), 15, 25, 15)[0] == "ok"
    assert nw.classify(_r(past_dusk_min=16, tracked_tonight=True), 15, 25, 15)[0] == "ok"
    st, why = nw.classify(_r(past_dusk_min=40, mount_parked=False), 15, 25, 15)
    assert st == "parked" and "not tracking" in why
    assert nw.classify(_r(past_dusk_min=40, mount_parked=False,
                          mount_tracking=True), 15, 25, 15)[0] == "ok"


def _incident_read(state):
    async def read(cfg, rig):
        if rig != "rc16":
            return {"api_ok": True, "process": True, "log_size": 1,
                    "log_mtime": None, "seq_leaf": "Take Exposure",
                    "seq_exposure_s": 300.0}
        return {"api_ok": True, "process": True, "pid": 1, "log_file": "x.log",
                "log_mtime": None, "log_size": state["size"],
                "seq_leaf": state["leaf"], "seq_exposure_s": None,
                "mount_parked": state["parked"], "mount_tracking": state["trk"],
                "safe": True}
    return read


def test_tick_replays_2026_10_06_one_push_with_the_item(tmp_path):
    cfg = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                             piggyback_enabled=True,
                             pushover_ratelimit_enabled=False)
    pushes = []

    async def notify(c, msg, title="", priority=0):
        pushes.append((msg, priority))
        return True

    mon = nw.NinaWatch()
    st = {"size": 100, "leaf": "Cool Camera", "parked": True, "trk": False}
    t0 = T_DUSK - timedelta(minutes=30)          # the cool starts
    states = []
    for i in range(0, 70):
        st["size"] += 50                         # nanny commands keep the log growing
        res = asyncio.run(nw.tick(cfg, "RUNNING", now=t0 + timedelta(minutes=i),
                                  read=_incident_read(st), notify=notify,
                                  window=True, monitor=mon,
                                  dusk_utc=T_DUSK.isoformat() + "Z"))
        states.append(res["rc16"]["state"])
    rc = [p for p in pushes if "NINA #1" in p[0]]
    assert len(rc) == 1                          # once, not every minute
    msg, prio = rc[0]
    assert "STUCK" in msg and "'Cool Camera'" in msg and prio == 1
    assert states.index("stuck") == 25           # at 25 min, no extra debounce
    assert "silent" not in states                # the old check never fired


def test_tick_parked_after_dusk_and_not_after_a_tracked_night(tmp_path):
    cfg = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                             pushover_ratelimit_enabled=False)
    pushes = []

    async def notify(c, msg, title="", priority=0):
        pushes.append((msg, priority))
        return True

    st = {"size": 1, "leaf": "Wait until Safe", "parked": True, "trk": False}
    mon = nw.NinaWatch()
    for i in range(0, 20):
        st["size"] += 1
        asyncio.run(nw.tick(cfg, "RUNNING", now=T_DUSK + timedelta(minutes=i),
                            read=_incident_read(st), notify=notify, window=True,
                            monitor=mon, dusk_utc=T_DUSK))
    assert len(pushes) == 1 and "NOT IMAGING" in pushes[0][0]
    assert "still PARKED 15 min after astro dusk" in pushes[0][0]
    # a night that tracked and parked at the end: quiet
    pushes.clear()
    mon = nw.NinaWatch()
    st.update(parked=False, trk=True)
    asyncio.run(nw.tick(cfg, "RUNNING", now=T_DUSK, read=_incident_read(st),
                        notify=notify, window=True, monitor=mon, dusk_utc=T_DUSK))
    st.update(parked=True, trk=False, leaf="Wait for Time")   # parked, holding to dawn
    for i in range(1, 60):
        asyncio.run(nw.tick(cfg, "RUNNING", now=T_DUSK + timedelta(hours=8, minutes=i),
                            read=_incident_read(st), notify=notify, window=True,
                            monitor=mon, dusk_utc=T_DUSK))
    assert pushes == []


def test_read_mount_maps_the_v2_fields(monkeypatch):
    calls = []

    async def get(base, path, timeout=8.0):
        calls.append(path)
        if path.endswith("mount/info"):
            return {"Connected": True, "AtPark": True, "TrackingEnabled": False}
        return {"Connected": True, "IsSafe": True}
    monkeypatch.setattr(nw, "_api_get", get)
    cfg = PhotonScriptConfig(_env_file=None)
    got = asyncio.run(nw.read_mount(cfg, "rc16"))
    assert got == {"mount_parked": True, "mount_tracking": False, "safe": True}
    assert all(p.startswith("/equipment/") for p in calls)   # GETs only
    assert asyncio.run(nw.read_mount(cfg, "piggyback")) == {}


def test_new_config_keys_and_fields():
    from photonscript.scheduler.app import _CONFIG_FIELDS as CONFIG_FIELDS
    c = PhotonScriptConfig(_env_file=None)
    assert c.nina_watch_stuck_minutes == 25.0
    assert c.nina_watch_parked_after_dusk_min == 15.0
    keys = {row[0]: row[1] for row in CONFIG_FIELDS}
    assert keys["nina_watch_stuck_minutes"] == "PS_NINA_WATCH_STUCK_MINUTES"
    assert keys["nina_watch_parked_after_dusk_min"] == "PS_NINA_WATCH_PARKED_AFTER_DUSK_MIN"


# ------------------------------------------- 4b / 5. armer: nanny and guiding

def _armer():
    from photonscript.scheduler.armer import Armer
    a = Armer(PhotonScriptConfig(_env_file=None, cooling_tolerance_c=1.0))
    a.plan = {"dusk_utc": "2026-10-07T02:30:00Z", "dawn_utc": "2026-10-07T12:00:00Z"}
    return a


def _patch_nanny(monkeypatch, seq_cool, info):
    import photonscript.scheduler.armer as armer_mod
    import photonscript.shared.rigs as rigs_mod
    cools, notes = [], []

    async def _info(base):
        return info

    async def _cool(base, temp, minutes=0.0):
        cools.append(temp)
        return {"ok": True}

    async def _seq(self, base):
        return seq_cool

    async def _notify(cfg, msg, **kw):
        notes.append((msg, kw.get("priority")))
    monkeypatch.setattr(rigs_mod, "rig_ids", lambda cfg: ["rc16"])
    monkeypatch.setattr(rigs_mod, "rig_config",
                        lambda cfg, r: SimpleNamespace(nina_base_url="http://x"))
    monkeypatch.setattr(rigs_mod, "rig_setpoint", lambda cfg, r: 0.0)
    monkeypatch.setattr(rigs_mod, "nina_camera_info", _info)
    monkeypatch.setattr(rigs_mod, "nina_cool", _cool)
    monkeypatch.setattr(armer_mod.Armer, "_running_cool_target", _seq)
    monkeypatch.setattr(armer_mod, "notify", _notify)
    return cools, notes


def test_nanny_pages_instead_of_fighting_a_sequence_cool(monkeypatch):
    cools, notes = _patch_nanny(monkeypatch, -10.0,
                                {"Temperature": 2.5, "CoolerOn": True})
    a = _armer()
    now = T_DUSK + timedelta(minutes=10)
    for _ in range(5):
        asyncio.run(a._reconcile_cooler(now))
    assert cools == []                           # no 30 s fight
    assert len(notes) == 1 and notes[0][1] == 1
    assert "Cool Camera to -10 C" in notes[0][0] and "setpoint is 0 C" in notes[0][0]


def test_nanny_still_reasserts_when_the_sequence_agrees(monkeypatch):
    cools, notes = _patch_nanny(monkeypatch, 0.0,
                                {"Temperature": 2.5, "CoolerOn": True})
    a = _armer()
    asyncio.run(a._reconcile_cooler(T_DUSK + timedelta(minutes=10)))
    assert cools == [0.0] and notes == []


def test_running_cool_target_reads_the_running_leaf(monkeypatch):
    import photonscript.scheduler.sideload as side
    tree = {"Response": [{"Name": "Start", "Status": "RUNNING", "Items": [
        {"Name": "Cool Camera", "Status": "RUNNING", "Temperature": -10}]}]}

    async def state(base, client=None):
        return tree, None
    monkeypatch.setattr(side, "read_sequence_state", state)
    a = _armer()
    assert asyncio.run(a._running_cool_target("http://x")) == -10.0
    tree["Response"][0]["Items"][0]["Name"] = "Take Exposure"
    assert asyncio.run(a._running_cool_target("http://x")) is None


def _guider_armer(monkeypatch, mount):
    a = _armer()
    calls = []

    async def _nina(key, method="GET", json_body=None, **params):
        calls.append(key)
        if key == "mount_info":
            return {"Response": dict(mount)}
        return {"Success": True}
    monkeypatch.setattr(a, "_nina", _nina)

    async def _nosleep(s):
        return None
    import photonscript.scheduler.armer as armer_mod
    monkeypatch.setattr(armer_mod.asyncio, "sleep", _nosleep)
    return a, calls


def test_no_guider_start_while_parked(monkeypatch):
    mount = {"AtPark": True, "TrackingEnabled": False}
    a, calls = _guider_armer(monkeypatch, mount)
    a._guiding_recovered = True
    assert asyncio.run(a._restart_guiding()) is None
    assert "guider_start" not in calls and "guider_stop" not in calls
    assert a._guiding_recovered is False                 # retried on a later tick
    assert "parked" in a.detail
    mount.update(AtPark=False)                           # unparked, not tracking
    assert asyncio.run(a._restart_guiding()) is None
    assert "not tracking" in a.detail
    mount.update(TrackingEnabled=True)
    assert asyncio.run(a._restart_guiding()) is True
    assert calls[-2:] == ["guider_stop", "guider_start"]


def test_guider_start_fails_open_when_mount_unreadable(monkeypatch):
    a, calls = _guider_armer(monkeypatch, {})
    assert asyncio.run(a._restart_guiding()) is True
    assert "guider_start" in calls


def test_ascii_only_in_new_code():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for rel in ("tests/test_scheduler/test_ps154_cool_never_gates.py",):
        text = (root / rel).read_text(encoding="utf-8")
        assert all(ord(ch) < 128 for ch in text), rel
        assert chr(0x2014) not in text
