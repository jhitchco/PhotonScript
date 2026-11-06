"""PS-197: every planner mode inherits the arm's guiding mode, and the
dispatch gate lints each NINA #1 night against the ARM before it is loaded.

2026-10-14: armed UNGUIDED, deep night on NGC 604. The NGC 604 container
held a StartGuiding; with the guider disconnected (unguided guard) it failed
3 attempts, SkipInstructionSetOnError skipped the container and the night
loop re-acquired NGC 604 for 20 min with no lights. The armer's lint was
told the guiding mode the targets carried, so a guided plan linted clean.

Fixture: the 10-14 goal store (tests/test_scheduler/fixtures/deep_night),
planned at 2026-10-15T02:10Z (20:10 MDT) as in test_deep_night.
"""
import asyncio
import json
from datetime import datetime
from pathlib import Path

import pytest

from photonscript.scheduler import armer as armer_mod
from photonscript.scheduler import dispatch_gate as dg
from photonscript.scheduler import moon
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import target_planner as tp
from photonscript.scheduler import unguided_guard as ug
from photonscript.scheduler.armer import Armer
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ImagingProject

FIX = Path(__file__).parent / "fixtures" / "deep_night" / "projects2_2026-10-14.json"
NOW = datetime(2026, 10, 15, 2, 10)
NIGHT = "2026-10-14"
ALL = ["Andromeda Galaxy", "NGC 604", "Heart Nebula"]


@pytest.fixture(autouse=True)
def _det(monkeypatch):
    # a dark evening for the generator (broadband allowed), as the moon
    # rule allows tonight (moon.sets_soon_after_dusk)
    monkeypatch.setattr(nsj, "_moon_window",
                        lambda: {"available": True, "down_at_dusk": True})
    moon._cache.clear()
    yield
    moon._cache.clear()


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _goals():
    return [ImagingProject(**p) for p in json.loads(FIX.read_text())]


def _goal_id(name):
    return next(p.id for p in _goals() if p.target.name == name)


class _FrozenDT(datetime):
    @classmethod
    def utcnow(cls):
        return NOW


def _armer(tmp_path, monkeypatch, guiding, **cfg):
    from photonscript.scheduler import app as app_mod
    goals = _goals()

    class _Store:
        projects = {p.id: p for p in goals}
    monkeypatch.setattr(app_mod, "_store", _Store())
    monkeypatch.setattr(armer_mod, "datetime", _FrozenDT)
    monkeypatch.chdir(tmp_path)
    a = Armer(_cfg(tmp_path, **cfg))
    a.plan = {"night_of": NIGHT, "dusk_utc": "2026-10-15T01:30:00Z",
              "dawn_utc": "2026-10-15T11:30:00Z"}
    a.guiding_override = guiding
    monkeypatch.setattr(a, "_calibration_slot", lambda targets, now: None)
    return a


def _seq(a):
    return json.loads(a.sequence_path.read_text(encoding="utf-8"))


def _gate_events(tmp_path):
    p = tmp_path / "data" / "runs" / f"{NIGHT}_events.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines()
            if '"dispatch_gate"' in x]


# --- the 10-14 case ---------------------------------------------------------------

def test_1014_deep_pick_ngc604_unguided_has_no_start_guiding(tmp_path, monkeypatch):
    a = _armer(tmp_path, monkeypatch, "unguided", plan_night_mode="deep")
    assert a._dispatch() is True, a.detail
    assert a.plan["night_mode"]["deep_pick"]["target"] == "NGC 604"
    assert a.last_dispatch_targets == ["NGC 604"]
    seq = _seq(a)
    assert dg.start_guiding_items(seq) == []
    assert dg.guider_connects(seq) == []
    assert "Guider NOT connected" in json.dumps(seq)
    assert a.last_gate["ok"] and a.last_gate["action"] == "ok"
    assert a.status()["dispatch_gate"]["line"].startswith("OK (unguided, deep)")
    assert [e["value"] for e in _gate_events(tmp_path)] == ["ok"]


def test_planner_carries_the_arm_mode_in_every_mode(tmp_path):
    for kw in ({}, {"night_mode": "deep"},
               {"night_mode": "deep", "deep_target": _goal_id("Heart Nebula")}):
        for guided in (True, False):
            g = _goals()
            ts = tp.plan_night_sequence(g, _cfg(tmp_path), NOW, guided=guided,
                                        **kw)
            assert ts and all(t.start_guiding is guided for t in ts), (kw, guided)


def test_apply_guiding_clears_per_block_flags_when_unguided():
    from photonscript.shared.models import NinaSequenceTarget
    t = NinaSequenceTarget(name="X", ra_hours=1.0, dec_degrees=30.0,
                           exposures=[], start_guiding=True,
                           unguided_filters=["Ha"])
    tp.apply_guiding([t], False)
    assert t.start_guiding is False and t.unguided_filters == []


# --- planner mode x guiding mode matrix (through the armer) -----------------------

@pytest.mark.parametrize("mode,target,names", [
    ("split", None, ALL),
    ("deep", None, ["NGC 604"]),
    ("deep", "Heart Nebula", None),
])
@pytest.mark.parametrize("guiding", ["guided", "unguided"])
def test_matrix(tmp_path, monkeypatch, mode, target, names, guiding):
    a = _armer(tmp_path, monkeypatch, guiding, plan_night_mode=mode)
    if target:
        from photonscript.scheduler import deep_night as dn
        dn.set_override(a.config, NIGHT, "deep", _goal_id(target))
    assert a._dispatch() is True, a.detail
    if names:
        assert a.last_dispatch_targets == names
    if target:
        assert a.plan["night_mode"]["deep_pick"]["target"] == target
    seq = _seq(a)
    tcs = [c for c in dg.target_containers(seq) if dg._has_lights(c)]
    assert tcs
    if guiding == "guided":
        assert all(dg.start_guiding_items(c) for c in tcs)
        assert dg.guider_connects(seq)
    else:
        assert dg.start_guiding_items(seq) == []
        assert dg.guider_connects(seq) == []
    assert a.last_gate["ok"] and a.last_gate["guided"] is (guiding == "guided")


# --- the gate ---------------------------------------------------------------------

def _night(tmp_path, guided):
    ts = tp.plan_night_sequence(_goals(), _cfg(tmp_path), NOW, guided=guided)
    return json.loads(nsj.generate_nina_json(build_sequence_for_night("t", ts)))


def test_gate_unguided_refuses_start_guiding_and_guider_connect(tmp_path):
    bad = _night(tmp_path, True)                 # a guided night ...
    v = dg.check(bad, guided=False, mode="deep")  # ... armed unguided
    assert not v["ok"] and v["level"] == "ERROR"
    assert any("StartGuiding" in e and "NGC 604" in e for e in v["errors"])
    assert any("connects the guider" in e for e in v["errors"])
    # Direct Guider dithers legitimately connect the guider
    v2 = dg.check(_night(tmp_path, False), guided=False, unguided_dither=True)
    assert v2["ok"]


def test_gate_guided_wants_start_guiding_on_guided_targets(tmp_path):
    seq = _night(tmp_path, False)
    v = dg.check(seq, guided=True)
    assert not v["ok"] and "no StartGuiding in" in v["errors"][0]
    # targets the per-block decisions turned unguided are not required to
    assert dg.check(seq, guided=True, guided_targets=[])["ok"]
    assert dg.check(_night(tmp_path, True), guided=True,
                    guided_targets=ALL)["ok"]


def test_gate_refuses_an_empty_night():
    assert dg.check({}, guided=False)["errors"] == ["empty night: no target container"]
    empty = {"$type": "NINA.Sequencer.Container.SequenceRootContainer, NINA.Sequencer",
             "Items": {"$values": [{
                 "$type": "NINA.Sequencer.Container.DeepSkyObjectContainer, NINA.Sequencer",
                 "Target": {"TargetName": "X"}, "Items": {"$values": []}}]}}
    v = dg.check(empty, guided=False)
    assert not v["ok"] and "no target container has exposure items" in v["errors"][0]


def _break_deep(monkeypatch, also_split=False):
    """A planner that hands the generator start_guiding on a deep plan even
    though the arm is unguided (what 10-14 loaded), with apply_guiding
    disabled so nothing downstream repairs it."""
    real = tp.plan_night_sequence

    def bad(projects, config, now, *, night_mode=None, info=None, guided=None,
            deep_target=None):
        ts = real(projects, config, now, night_mode=night_mode, info=info,
                  deep_target=deep_target)
        if also_split or (info or {}).get("mode") == "deep":
            for t in ts:
                t.start_guiding = True
        return ts
    monkeypatch.setattr(tp, "plan_night_sequence", bad)
    monkeypatch.setattr(tp, "apply_guiding", lambda ts, g: ts)


def test_gate_refuses_a_bad_deep_night_and_falls_back_to_split(tmp_path, monkeypatch):
    a = _armer(tmp_path, monkeypatch, "unguided", plan_night_mode="deep")
    _break_deep(monkeypatch)
    assert a._dispatch() is True, a.detail
    seq = _seq(a)
    assert dg.start_guiding_items(seq) == []
    assert a.last_dispatch_targets == ALL          # split, same goals
    assert a.plan["night_mode"]["mode"] == "split"
    assert a.last_gate["action"] == "fallback" and a.last_gate["ok"]
    assert a.last_gate["line"].startswith("fell back to split (deep refused")
    assert "StartGuiding" in a._gate_alert and "split plan" in a._gate_alert
    assert [e["value"] for e in _gate_events(tmp_path)] == ["refused", "fallback"]
    # the push goes out with the dispatch, priority 1
    sent = []

    async def _n(cfg, msg, **kw):
        sent.append((msg, kw.get("priority")))
    monkeypatch.setattr(armer_mod, "notify", _n)
    asyncio.run(a._send_gate_alert())
    assert len(sent) == 1 and sent[0][1] == 1 and "refused" in sent[0][0]


def test_gate_refuses_and_leaves_nina_idle_when_split_is_bad_too(tmp_path, monkeypatch):
    a = _armer(tmp_path, monkeypatch, "unguided", plan_night_mode="deep")
    _break_deep(monkeypatch, also_split=True)
    assert a._dispatch() is False
    assert a.sequence_path is None
    assert "dispatch gate refused" in a.detail and "NINA #1 not loaded" in a.detail
    assert a.last_gate["action"] == "refused" and not a.last_gate["ok"]
    assert [e["value"] for e in _gate_events(tmp_path)] == ["refused", "failed"]

    # through _dispatch_and_start: no stop, no load, a priority alert
    calls, sent = [], []

    async def _nina(key, **kw):
        calls.append(key)
        return {}

    async def _n(cfg, msg, **kw):
        sent.append((msg, kw.get("priority")))
    monkeypatch.setattr(armer_mod, "notify", _n)
    a._nina = _nina
    assert asyncio.run(a._dispatch_and_start(companion=False, fail_state=None)) is False
    assert "sequence_load" not in calls and "sequence_stop" not in calls
    assert sent and sent[-1][1] == 1 and "dispatch gate refused" in sent[-1][0]


def test_guided_night_unaffected(tmp_path, monkeypatch):
    a = _armer(tmp_path, monkeypatch, "guided")
    assert a._dispatch() is True, a.detail
    seq = _seq(a)
    assert len(dg.start_guiding_items(seq)) >= 3 and dg.guider_connects(seq)
    assert a.last_gate["action"] == "ok" and a._gate_alert is None
    snap = json.loads((tmp_path / "data" / "runs" / f"{NIGHT}_plan.json").read_text())
    assert all(t["guided"] for t in snap["targets"])


# --- the unguided guard reads the loaded sequence ----------------------------------

class _IO:
    def __init__(self, tree):
        self.tree, self.events, self.pushes = tree, [], []

    async def nina_guider(self):
        return {"Response": {"Connected": False}}

    async def nina_stop(self):
        return True

    async def nina_disconnect(self):
        return True

    async def nina_events(self):
        return []

    async def phd2_state(self):
        return "Stopped"

    async def phd2_stop(self):
        return True, ""

    async def nina_sequence(self):
        return self.tree

    async def notify(self, msg, priority=1, **_kw):
        self.pushes.append((msg, priority))

    async def event(self, value, detail, **kw):
        self.events.append(value)


def _api_tree(status="RUNNING"):
    # ninaAPI /sequence/state shape: no $type, display names, Status
    return {"Response": [{"Name": "Targets_Container", "Status": status, "Items": [
        {"Name": "NGC 604_Container", "Status": status, "Items": [
            {"Name": "Center", "Status": "FINISHED"},
            {"Name": "Start Guiding", "Status": "CREATED"}]}]}]}


def test_guard_at_arm_flags_start_guiding_in_the_running_sequence(tmp_path):
    io = _IO(_api_tree())
    st = ug.new_state(NIGHT)
    res = asyncio.run(ug.check(_cfg(tmp_path), io, st, phase="arm"))
    ls = res["loaded_sequence"]
    assert ls["pending"] == 1 and ls["running"] and ls["alerted"]
    assert io.events == ["loaded_start_guiding"]
    assert len(io.pushes) == 1 and "NGC 604" in io.pushes[0][0]
    asyncio.run(ug.check(_cfg(tmp_path), io, st, phase="arm"))
    assert len(io.pushes) == 1            # once per night


def test_guard_ignores_an_idle_leftover_and_finished_items(tmp_path):
    io = _IO(_api_tree(status="CREATED"))
    res = asyncio.run(ug.check(_cfg(tmp_path), io, ug.new_state(NIGHT), phase="arm"))
    assert res["loaded_sequence"]["pending"] == 1 and io.pushes == []
    assert io.events == ["loaded_start_guiding"]    # recorded, no push
    done = _api_tree()
    done["Response"][0]["Items"][0]["Items"][1]["Status"] = "FINISHED"
    io2 = _IO(done)
    res2 = asyncio.run(ug.check(_cfg(tmp_path), io2, ug.new_state(NIGHT), phase="arm"))
    assert res2["loaded_sequence"]["pending"] == 0 and io2.events == []
    # the watch phase never reads the sequence
    io3 = _IO(_api_tree())
    res3 = asyncio.run(ug.check(_cfg(tmp_path), io3, ug.new_state(NIGHT), phase="watch"))
    assert "loaded_sequence" not in res3


def test_guard_reads_a_generated_file_too(tmp_path):
    seq = _night(tmp_path, True)
    assert ug.loaded_start_guiding(seq)["pending"] >= 3
    assert ug.loaded_start_guiding(_night(tmp_path, False))["pending"] == 0


def test_new_sources_are_ascii():
    root = Path(__file__).resolve().parents[2]
    for f in ("photonscript/scheduler/dispatch_gate.py", __file__):
        txt = (root / f).read_text(encoding="utf-8")
        assert txt.isascii(), f
