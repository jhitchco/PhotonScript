"""2026-10-21: dusk sky flats on both rigs, independent of auto-arm.

Why they never ran: the dispatcher lived inside the auto-arm block (off on
TPoint nights and on the scope since 2026-10-20), needed an idle armer (a
noon or manual arm is ARMED from the afternoon on) and anchored sunset on the
UTC date (the window closed about 40 min before sunset). These tests pin the
rework (scheduler/dusk_flats.py): every evening, a logged decision, roof
open only, finished before nautical dusk, the Piggy-600 OSC set on NINA #2,
flats_when dawn | dusk | both, and (part B) the always-wanted RC16 darks.
"""
import asyncio
import json
from datetime import datetime, timedelta

import pytest

from photonscript.scheduler import auto_armer as aa
from photonscript.scheduler import calibration as cal
from photonscript.scheduler import dusk_flats as df
from photonscript.scheduler import night_plan
from photonscript.scheduler import sideload as sd
from photonscript.shared import rigs
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.night_events import events_path
from photonscript.shared.phd2_store import read_jsonl
from tests.test_scheduler import fake_sky

IDLE = ([{"Name": "Start", "Status": "FINISHED"},
         {"Name": "Targets", "Status": "CREATED"}], None)
RUNNING = ([{"Name": "Targets", "Status": "RUNNING",
             "Items": [{"Name": "Heart Nebula", "Status": "RUNNING"}]}], None)
_SKYFLAT = "NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _cfg(tmp_path, **kw):
    kw.setdefault("evening_forecast_enabled", False)
    kw.setdefault("quality_eccentricity_max", 0.60)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _short(t):
    return t.split(",")[0].split(".")[-1]


def _conds(node):
    return [_short(c["$type"]) for c in node.get("Conditions", {}).get("$values", [])]


def _named(root, prefix):
    return [n for n in _walk(root) if str(n.get("Name", "")).startswith(prefix)]


class _Armer:
    def __init__(self, state="DISARMED"):
        self.state = state
        self.raw = []
        self.armed = []
        self.detail = ""

    async def dispatch_raw(self, seq, label):
        self.raw.append((label, seq))
        return True

    async def arm(self, guiding=None, armed_by=None):
        self.armed.append(armed_by)
        self.state = "ARMED"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A tick 30 min before sunset; RC16 B and G stale; roof open; NINA idle."""
    now = datetime.utcnow().replace(microsecond=0)
    sunset = now + timedelta(minutes=30)
    tw = {"sunset": sunset, "naut_dusk": sunset + timedelta(minutes=54),
          "astro_dusk": sunset + timedelta(minutes=83)}
    monkeypatch.setattr(night_plan, "compute_night_times", lambda obs, base: tw)
    fake_sky.install(monkeypatch, sunset)   # 2026-10-24: windows by sun altitude
    st = {"stale": ["B", "G"], "pb": {"owed": False, "reasons": [],
                                      "shot_today": False},
          "safe": True, "tree": IDLE, "gen": [], "nina2": []}
    monkeypatch.setattr(cal, "stale_flat_filters", lambda c: list(st["stale"]))
    monkeypatch.setattr(cal, "piggyback_flat_status", lambda c: dict(st["pb"]))

    real_gen = cal.generate_dusk_flats_json

    def gen(c, **kw):
        st["gen"].append(kw)
        return real_gen(c, **kw)
    monkeypatch.setattr(cal, "generate_dusk_flats_json", gen)

    async def safe(c):
        return st["safe"]
    monkeypatch.setattr(df, "read_roof_safe", safe)

    async def piggy_safety(pcfg):
        return True
    monkeypatch.setattr(df, "_piggy_safety", piggy_safety)

    async def read(base, client=None):
        return st["tree"]
    monkeypatch.setattr(sd, "read_sequence_state", read)

    async def nina_dispatch(base, seq, config=None, rig=None):
        st["nina2"].append((base, seq, rig))
        return {"ok": True, "detail": "started"}
    monkeypatch.setattr(rigs, "nina_dispatch", nina_dispatch)
    pushes = []

    async def notify(c, msg, title="", priority=0, sound="none"):
        pushes.append((title, msg))
    return {"now": now, "tw": tw, "st": st, "pushes": pushes, "notify": notify,
            "tmp": tmp_path}


def _tick(cfg, armer, state, env, now=None):
    return _run(df.maybe_dispatch_dusk_flats(cfg, armer, state, env["notify"],
                                             now=now or env["now"]))


def _rows(cfg, now):
    from photonscript.scheduler.auto_armer import _night_key
    return [r for r in read_jsonl(events_path(cfg, _night_key(cfg, now)))
            if r.get("action") == "dusk_flats"]


# ---- the timeline -------------------------------------------------------------

def test_times_anchor_on_the_local_evening_date():
    """18:10 local MDT is 00:10Z the next UTC day: the old UTC anchor found
    tomorrow's sunset and the window was shut 40 min before sunset."""
    cfg = PhotonScriptConfig(_env_file=None)
    now = datetime(2026, 10, 22, 0, 10)          # 2026-10-21 18:10 local
    t = cal.dusk_flat_times(cfg, now)
    assert t["night"] == "2026-10-21"
    assert datetime(2026, 10, 22, 0, 30) <= t["sunset"] <= datetime(2026, 10, 22, 0, 45)
    assert t["open"] <= now <= t["last_dispatch"]
    # done before nautical dusk and before the night's pre-config
    assert t["stop"] <= t["naut_dusk"] - timedelta(minutes=8)
    assert t["stop"] <= t["preconfig"] - timedelta(minutes=8)
    assert t["start"] == t["sunset"] + timedelta(minutes=15)
    assert t["osc_start"] == t["sunset"] + timedelta(minutes=20)


# ---- the evening decision -----------------------------------------------------

@pytest.mark.parametrize("state", ["DISARMED", "COMPLETE", "ARMED"])
def test_dispatches_whatever_the_armer_idles_in(env, tmp_path, state):
    """A noon / manual arm (ARMED) no longer blocks the flats: the armer only
    touches NINA #1 at pre-config, after the flats stop."""
    cfg = _cfg(tmp_path, auto_arm_enabled=False, noon_arm_enabled=False)
    armer, fs = _Armer(state), {}
    assert _tick(cfg, armer, fs, env) == "dispatched"
    assert [r[0] for r in armer.raw] == ["auto dusk flats (G,B)"]
    assert fs["done"] and fs["until"] == cal.dusk_flat_times(cfg, env["now"])["stop"] \
        + timedelta(minutes=5)
    rows = _rows(cfg, env["now"])
    assert [(r["value"], r["skip"]) for r in rows] == [("dispatched", None)]
    assert "done by" in rows[0]["detail"]
    assert aa.decision_chip(rows[0]).startswith("Auto dusk flats dispatched at")
    assert len(env["pushes"]) == 1
    # one dispatch an evening
    assert _tick(cfg, armer, fs, env, env["now"] + timedelta(minutes=5)) is None
    assert len(armer.raw) == 1


def test_busy_armer_skips_and_retries(env, tmp_path):
    cfg = _cfg(tmp_path)
    armer, fs = _Armer("RUNNING"), {}
    assert _tick(cfg, armer, fs, env) == df.SKIP_ARMER_BUSY
    assert _tick(cfg, armer, fs, env, env["now"] + timedelta(minutes=5)) == \
        df.SKIP_ARMER_BUSY
    assert [r["skip"] for r in _rows(cfg, env["now"])] == [df.SKIP_ARMER_BUSY]
    armer.state = "DISARMED"
    assert _tick(cfg, armer, fs, env, env["now"] + timedelta(minutes=10)) == "dispatched"


def test_closed_roof_dispatches_and_nina_waits(env, tmp_path):
    """2026-10-24: the routine goes out with the roof closed (cooled and
    ready, NINA polls the roof every 10 s and starts when it reads safe)."""
    cfg = _cfg(tmp_path)
    env["st"]["safe"] = False
    armer, fs = _Armer(), {}
    assert _tick(cfg, armer, fs, env) == "dispatched"
    row = _rows(cfg, env["now"])[0]
    assert "roof closed at dispatch" in row["detail"]
    wait = _named(armer.raw[0][1], "WAIT_SAFE_FOR_DUSK_FLATS")[0]
    assert wait["Items"]["$values"][0]["Time"] == cal.WAIT_SAFE_STEP_S == 10


def test_closed_roof_skips_then_dispatches_when_it_opens(env, tmp_path, caplog):
    cfg = _cfg(tmp_path, flats_dispatch_roof_closed=False)
    armer, fs = _Armer(), {}
    env["st"]["safe"] = False
    with caplog.at_level("INFO", logger="photonscript.scheduler.dusk_flats"):
        assert _tick(cfg, armer, fs, env) == df.SKIP_ROOF_CLOSED
    assert armer.raw == [] and env["pushes"] == []
    assert any("auto-flats" in m and "roof closed" in m for m in caplog.messages)
    env["st"]["safe"] = True
    assert _tick(cfg, armer, fs, env, env["now"] + timedelta(minutes=5)) == "dispatched"
    assert [r["skip"] for r in _rows(cfg, env["now"])] == [df.SKIP_ROOF_CLOSED, None]


def test_closed_roof_all_window_ends_with_a_line(env, tmp_path, caplog):
    cfg = _cfg(tmp_path, flats_dispatch_roof_closed=False)
    armer, fs = _Armer(), {}
    env["st"]["safe"] = False
    _tick(cfg, armer, fs, env)
    late = cal.dusk_flat_times(cfg, env["now"])["last_dispatch"] + timedelta(minutes=1)
    with caplog.at_level("INFO", logger="photonscript.scheduler.dusk_flats"):
        assert _tick(cfg, armer, fs, env, late) == df.SKIP_WINDOW_CLOSED
    assert armer.raw == [] and fs["done"]
    msg = [m for m in caplog.messages if "skipped for tonight" in m]
    assert msg and "roof closed" in msg[0]
    assert [r["skip"] for r in _rows(cfg, env["now"])] == [
        df.SKIP_ROOF_CLOSED, df.SKIP_WINDOW_CLOSED]


def test_unreadable_safety_monitor_never_dispatches(env, tmp_path):
    cfg = _cfg(tmp_path)
    env["st"]["safe"] = None
    armer = _Armer()
    assert _tick(cfg, armer, {}, env) == df.SKIP_SAFETY_UNREADABLE
    assert armer.raw == []


def test_flats_when_dawn_and_disabled_skip_with_a_line(env, tmp_path, caplog):
    armer = _Armer()
    cfg = _cfg(tmp_path, flats_when="dawn")
    assert _tick(cfg, armer, {}, env) == df.SKIP_FLATS_WHEN
    off = _cfg(tmp_path / "off", auto_dusk_flats=False)
    with caplog.at_level("INFO", logger="photonscript.scheduler.dusk_flats"):
        assert _tick(off, armer, {}, env) == df.SKIP_DISABLED
    assert any("auto_dusk_flats is off" in m for m in caplog.messages)
    assert armer.raw == []
    assert _rows(off, env["now"]) == []          # the line only, no event row


def test_nothing_stale_is_decided_once(env, tmp_path):
    """A dawn set shot this morning reads fresh, so the dusk run skips: the
    other half of flats_when=both."""
    cfg = _cfg(tmp_path)
    env["st"]["stale"] = []
    armer, fs = _Armer(), {}
    assert _tick(cfg, armer, fs, env) == df.SKIP_NOTHING_STALE
    assert _tick(cfg, armer, fs, env, env["now"] + timedelta(minutes=5)) is None
    assert armer.raw == []


def test_not_dispatched_before_the_cool_lead(env, tmp_path):
    """The dispatch goes out flats_dusk_cool_lead_min + 5 before the first
    window (or sunset): earlier ticks do nothing."""
    cfg = _cfg(tmp_path)
    early = env["tw"]["sunset"] - timedelta(minutes=80)
    armer = _Armer()
    assert _tick(cfg, armer, {}, env, early) is None
    assert armer.raw == [] and _rows(cfg, env["now"]) == []


def test_outside_the_window_does_nothing(env, tmp_path):
    cfg = _cfg(tmp_path)
    early = env["tw"]["sunset"] - timedelta(minutes=95)
    assert _tick(cfg, _Armer(), {}, env, early) is None
    assert _rows(cfg, env["now"]) == []


def test_guard_still_applies(env, tmp_path):
    """PS-125 / PS-131: a running NINA #1 skips (one push), retried later."""
    cfg = _cfg(tmp_path)
    env["st"]["tree"] = RUNNING
    armer, fs = _Armer(), {}
    assert _tick(cfg, armer, fs, env) == "nina_running"
    assert armer.raw == [] and len(env["pushes"]) == 1
    env["st"]["tree"] = IDLE
    assert _tick(cfg, armer, fs, env, env["now"] + timedelta(minutes=5)) == "dispatched"


# ---- both rigs ------------------------------------------------------------------

def test_piggyback_osc_set_goes_to_nina2_in_parallel(env, tmp_path):
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    env["st"]["pb"] = {"owed": True, "reasons": ["newest set has 10 of 25"],
                       "shot_today": False}
    armer, fs = _Armer("ARMED"), {}
    assert _tick(cfg, armer, fs, env) == "dispatched"
    label, rc = armer.raw[0]
    assert label == "auto dusk flats (G,B, Piggy-600)"
    # the RC16 holds its pointing for the riding OSC
    hold = _named(rc, "DUSK_SKY_FLATS")[0]["Items"]["$values"][-1]
    assert _short(hold["$type"]) == "WaitForTime"
    assert env["st"]["nina2"], _rows(cfg, env["now"])[0]["detail"]
    base, osc, rig = env["st"]["nina2"][0]
    assert rig == "piggyback" and base == rigs.rig_config(cfg, "piggyback").nina_base_url
    sf = [n for n in _walk(osc) if n.get("$type", "").startswith(_SKYFLAT)]
    assert len(sf) == 1
    loop = sf[0]["Items"]["$values"][1]
    assert loop["Conditions"]["$values"][0]["Iterations"] == 25
    temps = [n["Temperature"] for n in _walk(osc) if "Temperature" in n]
    assert temps and all(t == 0.0 for t in temps)   # piggyback_setpoint_c
    from photonscript.scheduler.sideload import lint_companion
    assert lint_companion(osc).ok
    assert "Piggy-600 OSC: dispatched" in _rows(cfg, env["now"])[0]["detail"]


def test_osc_only_evening_slews_and_holds_without_rc16_flats(env, tmp_path):
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    env["st"]["stale"] = []
    env["st"]["pb"] = {"owed": True, "reasons": ["no flats"], "shot_today": False}
    armer = _Armer()
    assert _tick(cfg, armer, {}, env) == "dispatched"
    label, rc = armer.raw[0]
    assert label == "auto dusk flats (OSC hold)"
    assert not [n for n in _walk(rc) if n.get("$type", "").startswith(_SKYFLAT)]
    assert [_short(n.get("$type", "")) for n in _walk(rc)].count(
        "SlewScopeToAltAz") == 1
    assert env["st"]["nina2"]


def test_piggyback_skipped_when_nina2_busy(env, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    env["st"]["pb"] = {"owed": True, "reasons": ["x"], "shot_today": False}
    calls = {"n": 0}

    async def read(base, client=None):
        calls["n"] += 1
        return IDLE if calls["n"] <= 2 else RUNNING   # guard reads #1, #2
    monkeypatch.setattr(sd, "read_sequence_state", read)
    armer = _Armer()
    assert _tick(cfg, armer, {}, env) == "dispatched"
    assert env["st"]["nina2"] == []
    assert "NINA #2 running" in _rows(cfg, env["now"])[0]["detail"]


# ---- the sequences --------------------------------------------------------------

def test_rc16_sequence_capped_before_nautical_dusk_and_roof_gated(env, tmp_path):
    cfg = _cfg(tmp_path)
    t = cal.dusk_flat_times(cfg, env["now"])
    seq = json.loads(cal.generate_dusk_flats_json(cfg, only_filters=["B", "G"],
                                                  now=env["now"])[0])
    box = _named(seq, "DUSK_SKY_FLATS")[0]
    assert _conds(box) == ["SafetyMonitorCondition", "TimeCondition", "LoopCondition"]
    tc = box["Conditions"]["$values"][1]
    from photonscript.shared.localtime import utc_offset_hours
    stop = t["stop"] + timedelta(hours=utc_offset_hours(cfg, t["stop"]))
    assert (tc["Hours"], tc["Minutes"]) == (stop.hour, stop.minute)
    kinds = [_short(i["$type"]) for i in box["Items"]["$values"]]
    assert kinds[:3] == ["UnparkScope", "SetTracking", "SlewScopeToAltAz"]
    assert len([n for n in _walk(box) if n.get("$type", "").startswith(_SKYFLAT)]) == 2
    wait = _named(seq, "WAIT_SAFE_FOR_DUSK_FLATS")[0]
    assert _conds(wait) == ["LoopWhileUnsafe", "TimeCondition"]
    # no unbounded WaitUntilSafe any more
    assert "WaitUntilSafe" not in [_short(n.get("$type", "")) for n in _walk(seq)]
    from photonscript.scheduler.sequence_lint import lint
    bad = [f for f in lint(seq).findings if f.level == "ERROR" and f.rule in (
        "loop-spin", "parent-links", "safety")]
    assert not bad, bad


def test_api_style_all_filters_when_only_is_none(env, tmp_path):
    cfg = _cfg(tmp_path)
    seq = json.loads(cal.generate_dusk_flats_json(cfg, now=env["now"])[0])
    assert len([n for n in _walk(seq) if n.get("$type", "").startswith(_SKYFLAT)]) == 7


# ---- flats_when in the night sequences -------------------------------------------

def _night(tmp_path, monkeypatch, **kw):
    from photonscript.scheduler import nina_sequence_json as nsj
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    from photonscript.shared.models import (ExposurePlan, FilterType,
                                            NinaSequenceTarget)
    cfg = _cfg(tmp_path, observatory_tz="America/Denver", **kw)
    monkeypatch.setattr(nsj, "_gen_cfg", lambda: cfg)
    # generate_nina_json reads a fresh PhotonScriptConfig() (the env)
    if "flats_when" in kw:
        monkeypatch.setenv("PS_FLATS_WHEN", kw["flats_when"])
    else:
        monkeypatch.delenv("PS_FLATS_WHEN", raising=False)
    monkeypatch.setattr(cal, "stale_flat_filters", lambda c: [])
    t = NinaSequenceTarget(name="Heart Nebula", ra_hours=2.55, dec_degrees=61.5,
                           exposures=[ExposurePlan(filter_type=FilterType.HA,
                                                   exposure_seconds=300, count=40,
                                                   gain=200, offset=256),
                                      ExposurePlan(filter_type=FilterType("OIII"),
                                                   exposure_seconds=300, count=40,
                                                   gain=200, offset=256)],
                           camera_temp_c=0.0)
    return json.loads(nsj.generate_nina_json(build_sequence_for_night("FW", [t])))


def _dawn_filters(seq):
    box = _named(seq, "DAWN_SKY_FLATS")
    if not box:
        return None
    return [n["Filter"]["_name"] if isinstance(n.get("Filter"), dict) and "_name" in n["Filter"]
            else n.get("Name") for n in _walk(box[0])
            if n.get("$type", "").startswith(_SKYFLAT)]


def test_night_dawn_flats_follow_flats_when(tmp_path, monkeypatch):
    assert len(_dawn_filters(_night(tmp_path / "a", monkeypatch))) == 2
    assert _dawn_filters(_night(tmp_path / "b", monkeypatch, flats_when="dawn")) \
        is not None
    assert _dawn_filters(_night(tmp_path / "c", monkeypatch, flats_when="dusk")) is None
    # both: a filter the dusk flats shot today is not shot again at dawn
    monkeypatch.setattr(cal, "flats_shot_today", lambda c: {"Ha"})
    both = _dawn_filters(_night(tmp_path / "d", monkeypatch, flats_when="both"))
    assert len(both) == 1
    monkeypatch.setattr(cal, "flats_shot_today", lambda c: {"Ha", "OIII"})
    assert _dawn_filters(_night(tmp_path / "e", monkeypatch)) is None


def _companion(**kw):
    from photonscript.scheduler.calibration import generate_piggyback_companion_json
    over = kw.pop("over", {})
    pc = rigs.rig_config(PhotonScriptConfig(
        _env_file=None, piggyback_enabled=True, piggyback_focus_seed=0, **over),
        "piggyback")
    return json.loads(generate_piggyback_companion_json(pc, has_safety=True,
                                                        with_lights=True, **kw))


def test_companion_dawn_flats_follow_flats_when():
    from photonscript.scheduler.sideload import lint_companion
    assert _named(_companion(), "DAWN_SKY_FLATS_OSC")
    for seq in (_companion(over={"flats_when": "dusk"}), _companion(dawn_flats=False)):
        assert not _named(seq, "DAWN_SKY_FLATS_OSC")
        assert not [n for n in _walk(seq) if n.get("$type", "").startswith(_SKYFLAT)]
        assert lint_companion(seq).ok
        assert "WarmCamera" in [_short(n.get("$type", "")) for n in _walk(seq)]


def test_armer_dawn_hold_off_with_flats_when_dusk(tmp_path):
    from photonscript.scheduler.armer import Armer
    a = Armer(PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                                 piggyback_enabled=True, flats_when="dusk"))
    assert a._dawn_flats_expected() is False
    b = Armer(PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                                 piggyback_enabled=True))
    assert b._dawn_flats_expected() is True


# ---- the loop: independent of auto-arm, holds only the automatic arm ---------------

def _loop_tick(cfg, armer, monkeypatch):
    import photonscript.scheduler.auto_armer as mod
    from photonscript.shared import pushover

    async def notify(c, msg, title="", priority=0, sound="none"):
        return True
    monkeypatch.setattr(pushover, "notify", notify)

    async def stop(_s):
        raise asyncio.CancelledError
    monkeypatch.setattr(mod.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        _run(aa.run_auto_arm_loop(cfg, lambda: armer))


def test_loop_runs_dusk_flats_with_auto_arm_and_noon_arm_off(env, tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, auto_arm_enabled=False, noon_arm_enabled=False)
    armer = _Armer()
    _loop_tick(cfg, armer, monkeypatch)
    assert [r[0] for r in armer.raw] == ["auto dusk flats (G,B)"]


def test_loop_holds_the_automatic_arm_only(env, tmp_path, monkeypatch):
    """The auto-arm window is open, flats dispatch this tick: the automatic
    arm waits (it would skip on "NINA is running"). A manual Arm goes
    straight to armer.arm(), which never reads the hold."""
    from photonscript.scheduler import preflight
    from photonscript.shared.phd2_store import night_of
    cfg = _cfg(tmp_path, auto_arm_enabled=True, noon_arm_enabled=False,
               auto_arm_lead_hours=5.0)
    now = env["now"]
    monkeypatch.setattr(night_plan, "build_night_plan", lambda c: {
        "night_of": night_of(cfg, now),
        "preconfig_utc": (now + timedelta(hours=2)).isoformat() + "Z"})

    async def pf(c):
        return {"go": True, "checks": []}
    monkeypatch.setattr(preflight, "run_preflight", pf)
    armer = _Armer()
    _loop_tick(cfg, armer, monkeypatch)
    assert len(armer.raw) == 1 and armer.armed == []
    _run(armer.arm(armed_by="manual"))
    assert armer.armed == ["manual"]


# ---- part B: the always-wanted RC16 darks ------------------------------------------

def test_always_wanted_rc16_darks_in_quota_and_owed(tmp_path, monkeypatch):
    from photonscript.scheduler import calibration_owed as co
    from photonscript.scheduler import calibration_plan as cp
    cfg = _cfg(tmp_path, image_watch_dir=str(tmp_path / "watch"),
               library_dir=str(tmp_path / "lib"))
    monkeypatch.setattr(cp, "load_projects", lambda c: [])
    assert cal.quota_exposures(cfg, "rc16")[-2:] == [750.0, 900.0]
    assert {750.0, 900.0} <= set(cal.night_dark_exposures(cfg, "rc16"))
    assert 750.0 not in cal.quota_exposures(cfg, "piggyback")
    rep = co.owed_report(cfg, "rc16", projects=[])
    rc = rep["rigs"][0]
    assert rc["always_dark_exposures"] == [750.0, 900.0]
    long = {d["exp_s"]: d for d in rc["darks"] if d["exp_s"] in (750.0, 900.0)}
    assert set(long) == {750.0, 900.0}
    for d in long.values():
        assert d["always_wanted"] and d["in_quota_list"] and d["auto_fill"]
        assert d["owed"] == 30 and d["have"] == 0
    assert any("always wanted" in line for line in rc["items"])
    assert {750.0, 900.0} <= {x["exp_s"] for x in rc["plan"]["dark_sets"]}
    assert rep["flats_when"] == "both"
    needs = {s["exp_s"]: s for s in cp.rig_needs(cfg, "rc16", [])
             if s["type"] == "DARK"}
    assert {750.0, 900.0} <= set(needs)


def test_always_wanted_list_is_configurable(tmp_path):
    from photonscript.scheduler.nina_sequence_json import (always_dark_exposures,
                                                           dark_library_exposures)
    cfg = _cfg(tmp_path, rc16_always_dark_exposures="900, 1200,x,900")
    assert always_dark_exposures(cfg) == [900.0, 1200.0]
    assert dark_library_exposures(cfg)[-2:] == [900.0, 1200.0]
    assert always_dark_exposures(_cfg(tmp_path, rc16_always_dark_exposures="")) == []


def test_closed_roof_rc16_darks_carry_the_long_lengths(tmp_path, monkeypatch):
    from photonscript.scheduler import nina_sequence_json as nsj
    cfg = _cfg(tmp_path, image_watch_dir=str(tmp_path / "watch"))
    monkeypatch.setattr(nsj, "_gen_cfg", lambda: cfg)
    blocks = nsj._dark_quota_blocks("DawnProvider", 0)
    names = [b["Name"] for b in blocks]
    assert any(n.startswith("DARKS_750s") for n in names)
    assert any(n.startswith("DARKS_900s") for n in names)
    assert names.index(next(n for n in names if n.startswith("DARKS_600s"))) < \
        names.index(next(n for n in names if n.startswith("DARKS_900s")))


def test_new_sources_ascii_no_em_dash():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for rel in ("photonscript/scheduler/dusk_flats.py",
                "tests/test_scheduler/test_dusk_flats_both_rigs.py"):
        assert (root / rel).read_text(encoding="utf-8").isascii(), rel
