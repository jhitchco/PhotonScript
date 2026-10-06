"""PS-131: auto-arm guard follow-ups.

1. The noon re-arm's cooler-off (cooler belt #2) runs on every rig even when
   the PS-125 guard skips the noon arm: only the arm is skipped. Once per
   night, never at or after the pre-cool time, logged as "noon_cooler_off".
2. The automatic dusk sky flats (armer.dispatch_raw: stop, load, start) get
   the PS-125 guard: a sideload tonight, NINA #1 running or NINA #1
   unreadable skips them, one Pushover per night per reason, decisions in
   runs/<night>_events.jsonl with action "dusk_flats".
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from photonscript.scheduler import auto_armer as aa
from photonscript.scheduler import sideload as sd
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.night_events import events_path
from photonscript.shared.phd2_store import night_of, read_jsonl

# 2026-10-05 12:30 local (MDT, UTC-6) = 18:30Z
NOON = datetime(2026, 10, 5, 18, 30, 0)
DUSK = "2026-10-06T01:30:00Z"            # 19:30 local; pre-cool 19:00 local

IDLE = ([{"Name": "Start", "Status": "FINISHED"},
         {"Name": "Targets", "Status": "CREATED"}], None)
RUNNING = ([{"Name": "Targets", "Status": "RUNNING",
             "Items": [{"Name": "Heart Nebula", "Status": "RUNNING"}]}], None)
DOWN = (None, "ConnectError: refused")


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _cfg(tmp_path, **kw):
    kw.setdefault("quality_eccentricity_max", 0.60)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _events(cfg, now, kind=None):
    rows = read_jsonl(events_path(cfg, night_of(cfg, now)))
    return [r for r in rows if kind is None or r.get("kind") == kind]


def _sideload(cfg, when, ok=True):
    sd.record_event(cfg, "rc16", "PhotonScript_20261005_TT_M_2_then_tonight",
                    "x.json", "tracking_test_then_tonight", ok, "loaded", now=when)


class _Armer:
    def __init__(self):
        self.state = "DISARMED"
        self.armed = []
        self.cooler_off = []
        self.raw = []

    async def arm(self, guiding=None):
        self.armed.append(guiding)
        self.state = "ARMED"
        return {"state": self.state}

    async def cooler_dew_off(self, skip_rigs=None):
        self.cooler_off.append(dict(skip_rigs or {}))
        return {"rc16": "cooler off, dew off"}

    async def dispatch_raw(self, seq, label):
        self.raw.append(label)
        return True


# ------------------------------------------------ armer: the split step

def test_armer_cooler_dew_off_split_per_rig(tmp_path, monkeypatch):
    """The cooler-off step stands alone (no arm), covers both rigs, leaves a
    held rig alone, and _cooler_dew_off_all (arm's path) still uses it."""
    from photonscript.scheduler.armer import Armer
    from photonscript.shared import rigs
    calls = []

    async def warm(base, minutes=0.0):
        calls.append(("warm", base))
        return {"ok": "n2" not in base, "detail": "x"}

    async def dew(base, on):
        calls.append(("dew", base, on))
        return {"ok": True, "detail": "x"}
    monkeypatch.setattr(rigs, "nina_warm", warm)
    monkeypatch.setattr(rigs, "nina_dew_heater", dew)
    a = Armer(_cfg(tmp_path, piggyback_enabled=True,
                   nina_base_url="http://n1:1888/v2/api",
                   piggyback_nina_base_url="http://n2:1889/v2/api"))
    res = _run(a.cooler_dew_off())
    assert res == {"rc16": "cooler off, dew off",
                   "piggyback": "cooler FAILED, dew off"}
    assert ("dew", "http://n2:1889/v2/api", False) in calls
    calls.clear()
    res = _run(a.cooler_dew_off(skip_rigs={"rc16": "calibration capture running"}))
    assert res["rc16"] == "left alone (calibration capture running)"
    assert all("n1" not in c[1] for c in calls)
    calls.clear()
    _run(a._cooler_dew_off_all())
    assert {c[1] for c in calls} == {"http://n1:1888/v2/api", "http://n2:1889/v2/api"}


# ------------------------------------------- noon cooler-off on its own

def test_noon_cooler_off_once_per_night_and_logged(tmp_path):
    cfg = _cfg(tmp_path)
    armer = _Armer()
    line = _run(aa.noon_cooler_off(cfg, armer, {"dusk_utc": DUSK},
                                   aa.SKIP_NINA_UNREADABLE, NOON))
    assert line["kind"] == "noon_cooler_off" and line["skip"] == "nina_unreadable"
    assert "rc16: cooler off" in line["detail"]
    assert _run(aa.noon_cooler_off(cfg, armer, {"dusk_utc": DUSK}, "x",
                                   NOON + timedelta(minutes=5))) is None
    assert len(armer.cooler_off) == 1
    assert aa.noon_cooler_off_tonight(cfg, NOON)["value"] == "done"
    # it never shows up as an auto-arm decision (chip unaffected)
    assert aa.decisions_tonight(cfg, NOON) == []


def test_noon_cooler_off_never_at_or_after_precool(tmp_path):
    cfg = _cfg(tmp_path, cool_lead_minutes=30)
    armer = _Armer()
    precool = datetime(2026, 10, 6, 1, 0, 0)
    assert _run(aa.noon_cooler_off(cfg, armer, {"dusk_utc": DUSK},
                                   "x", precool)) is None
    assert armer.cooler_off == []


def test_noon_cooler_off_respects_switch_and_calibration_jobs(tmp_path, monkeypatch):
    from photonscript.scheduler import calibration_capture as cc
    armer = _Armer()
    off = _cfg(tmp_path, cooler_off_until_precool=False)
    assert _run(aa.noon_cooler_off(off, armer, {}, "x", NOON)) is None
    monkeypatch.setattr(cc, "busy", lambda rig: rig == "rc16")
    cfg = _cfg(tmp_path)
    _run(aa.noon_cooler_off(cfg, armer, {}, "x", NOON))
    assert armer.cooler_off == [{"rc16": "calibration capture running"}]


# ------------------------------------------------------ the loop at noon

class _FrozenDT(datetime):
    at = NOON

    @classmethod
    def utcnow(cls):
        return cls.at


@pytest.fixture
def noon_env(tmp_path, monkeypatch):
    """One tick of run_auto_arm_loop at 12:30 local with only the noon re-arm on."""
    from photonscript.scheduler import night_plan, preflight
    from photonscript.shared import pushover
    import photonscript.scheduler.auto_armer as mod
    cfg = _cfg(tmp_path, auto_arm_enabled=False, noon_arm_enabled=True,
               noon_arm_guided=True, evening_forecast_enabled=False,
               auto_dusk_flats=False)
    monkeypatch.setattr(mod, "datetime", _FrozenDT)
    monkeypatch.setattr(night_plan, "build_night_plan", lambda c: {
        "night_of": night_of(cfg, NOON), "dusk_utc": DUSK,
        "preconfig_utc": "2026-10-06T00:30:00Z"})
    pf = {"go": True, "checks": []}

    async def run_pf(c):
        return pf
    monkeypatch.setattr(preflight, "run_preflight", run_pf)
    pushes = []

    async def notify(c, msg, title="", priority=0, sound="none"):
        pushes.append((title, msg))
        return True
    monkeypatch.setattr(pushover, "notify", notify)
    state = {"tree": IDLE}

    async def read(base, client=None):
        return state["tree"]
    monkeypatch.setattr(sd, "read_sequence_state", read)

    async def stop(_s):
        raise asyncio.CancelledError
    monkeypatch.setattr(mod.asyncio, "sleep", stop)
    armer = _Armer()

    def tick():
        with pytest.raises(asyncio.CancelledError):
            _run(aa.run_auto_arm_loop(cfg, lambda: armer))
    return {"cfg": cfg, "armer": armer, "pushes": pushes, "state": state,
            "tick": tick, "pf": pf}


@pytest.mark.parametrize("tree,skip", [(RUNNING, "nina_running"),
                                       (DOWN, "nina_unreadable")])
def test_noon_skip_still_forces_coolers_off(noon_env, tree, skip):
    noon_env["state"]["tree"] = tree
    noon_env["tick"]()
    _FrozenDT.at = NOON + timedelta(minutes=5)
    try:
        noon_env["tick"]()                       # next tick: no second cooler-off
    finally:
        _FrozenDT.at = NOON
    a = noon_env["armer"]
    assert a.armed == [] and len(a.cooler_off) == 1
    assert len(noon_env["pushes"]) == 1          # the PS-125 skip push, once
    ev = _events(noon_env["cfg"], NOON, "noon_cooler_off")
    assert [e["skip"] for e in ev] == [skip]
    arm = _events(noon_env["cfg"], NOON, "auto_arm")
    assert [(e["value"], e["skip"]) for e in arm] == [("skipped", skip)]
    assert arm[0]["trigger"].startswith("noon")


def test_noon_skip_for_sideload_forces_coolers_off(noon_env):
    _sideload(noon_env["cfg"], NOON - timedelta(minutes=10))
    noon_env["tick"]()
    assert noon_env["armer"].armed == [] and len(noon_env["armer"].cooler_off) == 1


def test_noon_required_preflight_skip_forces_coolers_off(noon_env):
    noon_env["cfg"].auto_arm_require_preflight = True
    noon_env["pf"]["go"] = False
    noon_env["tick"]()
    assert noon_env["armer"].armed == [] and len(noon_env["armer"].cooler_off) == 1
    assert _events(noon_env["cfg"], NOON, "noon_cooler_off")[0]["skip"] == "preflight"


def test_noon_clear_arms_without_extra_cooler_off(noon_env):
    """Guard clear: the arm runs (arm() does its own cooler-off); no extra step."""
    noon_env["tick"]()
    assert noon_env["armer"].armed == ["guided"]
    assert noon_env["armer"].cooler_off == []
    assert _events(noon_env["cfg"], NOON, "noon_cooler_off") == []


def test_evening_window_skip_does_not_touch_coolers(noon_env):
    """Only the noon trigger carries the cooler-off; an evening-window skip
    (the sequence may be pre-cooling) never warms anything."""
    cfg = noon_env["cfg"]
    cfg.noon_arm_enabled = False
    cfg.auto_arm_enabled = True
    cfg.auto_arm_lead_hours = 10.0           # window already open at 12:30
    noon_env["state"]["tree"] = RUNNING
    noon_env["tick"]()
    assert noon_env["armer"].armed == [] and noon_env["armer"].cooler_off == []


# ---------------------------------------------------- dusk flats guarded

@pytest.fixture
def flats_env(tmp_path, monkeypatch):
    """One tick inside the dusk-flats window (sunset in 30 min), stale flats,
    evening auto-arm on but its window not yet open."""
    from photonscript.scheduler import calibration, night_plan
    from photonscript.shared import pushover
    import photonscript.scheduler.auto_armer as mod
    cfg = _cfg(tmp_path, auto_arm_enabled=True, noon_arm_enabled=False,
               auto_arm_lead_hours=1.0, evening_forecast_enabled=False,
               auto_dusk_flats=True)
    now = datetime.utcnow()
    monkeypatch.setattr(night_plan, "build_night_plan", lambda c: {
        "night_of": night_of(cfg, now),
        "preconfig_utc": (now + timedelta(hours=5)).isoformat() + "Z"})
    monkeypatch.setattr(night_plan, "compute_night_times",
                        lambda obs, base: {"sunset": now + timedelta(minutes=30)})
    monkeypatch.setattr(calibration, "stale_flat_filters", lambda c: ["Ha", "OIII"])
    monkeypatch.setattr(calibration, "generate_dusk_flats_json",
                        lambda c, only_filters=None: ('{"x": 1}', "18:45"))
    pushes = []

    async def notify(c, msg, title="", priority=0, sound="none"):
        pushes.append((title, msg))
        return True
    monkeypatch.setattr(pushover, "notify", notify)
    state = {"tree": IDLE}

    async def read(base, client=None):
        return state["tree"]
    monkeypatch.setattr(sd, "read_sequence_state", read)

    async def stop(_s):
        raise asyncio.CancelledError
    monkeypatch.setattr(mod.asyncio, "sleep", stop)
    armer = _Armer()

    def tick():
        with pytest.raises(asyncio.CancelledError):
            _run(aa.run_auto_arm_loop(cfg, lambda: armer))
    return {"cfg": cfg, "armer": armer, "pushes": pushes, "state": state,
            "tick": tick, "now": now}


def _flat_decisions(env):
    return [e for e in _events(env["cfg"], env["now"], "auto_arm")
            if e.get("action") == "dusk_flats"]


def test_flats_idle_nina_dispatches(flats_env):
    flats_env["tick"]()
    assert flats_env["armer"].raw == ["auto dusk flats (Ha,OIII)"]
    assert flats_env["armer"].armed == []
    assert _flat_decisions(flats_env) == []


def test_flats_sideload_skips_with_one_push(flats_env):
    _sideload(flats_env["cfg"], flats_env["now"] - timedelta(seconds=1))
    flats_env["tick"]()
    flats_env["tick"]()
    assert flats_env["armer"].raw == []
    assert len(flats_env["pushes"]) == 1
    title, msg = flats_env["pushes"][0]
    assert "flats skipped" in title and "sideloaded sequence is loaded" in msg
    ev = _flat_decisions(flats_env)
    assert [(e["value"], e["skip"]) for e in ev] == [("skipped", "sideload")]
    assert ev[0]["trigger"] == "auto dusk flats (Ha,OIII)"
    assert aa.decision_chip(ev[0]).startswith("Auto dusk flats skipped at")


@pytest.mark.parametrize("tree,skip", [(RUNNING, "nina_running"),
                                       (DOWN, "nina_unreadable")])
def test_flats_nina_running_or_unreadable_skips(flats_env, tree, skip):
    flats_env["state"]["tree"] = tree
    flats_env["tick"]()
    assert flats_env["armer"].raw == []
    assert [e["skip"] for e in _flat_decisions(flats_env)] == [skip]
    assert len(flats_env["pushes"]) == 1
    flats_env["state"]["tree"] = IDLE        # NINA finished: flats go next tick
    flats_env["tick"]()
    assert flats_env["armer"].raw == ["auto dusk flats (Ha,OIII)"]


def test_flats_and_arm_skips_push_separately(tmp_path):
    """One push per night per reason is counted per action: a flats skip does
    not swallow the later auto-arm skip push for the same reason."""
    cfg = _cfg(tmp_path)
    pushes = []

    async def notify(c, msg, title="", priority=0):
        pushes.append(title)
    now = datetime(2026, 10, 5, 23, 0, 0)
    _run(aa.skip_and_notify(cfg, aa.SKIP_SIDELOAD, "d", "auto dusk flats (Ha)",
                            notify, now, action=aa.ACTION_FLATS))
    _run(aa.skip_and_notify(cfg, aa.SKIP_SIDELOAD, "d", "within arm window",
                            notify, now + timedelta(minutes=5)))
    _run(aa.skip_and_notify(cfg, aa.SKIP_SIDELOAD, "d", "auto dusk flats (Ha)",
                            notify, now + timedelta(minutes=10),
                            action=aa.ACTION_FLATS))
    assert pushes == ["PhotonScript auto flats skipped",
                      "PhotonScript auto-arm skipped"]


def test_ps131_source_ascii():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    src = (root / "photonscript/scheduler/auto_armer.py").read_text(encoding="utf-8")
    new = src.split("# PS-131: the noon cooler-off", 1)[1].split("def decision_chip")[0]
    assert new.isascii()
