"""PS-139: no slew / Center / mount instruction in a NINA #2 (Piggy-600)
sequence, above all inside its exposure loop.

2026-10-03: a hand-built NINA #2 M31 sequence (NINA's stock deep-sky
template) ran a Center inside its per-sub loop; on a dual-rig night every
Piggy sub would pull the RC16 ~58' off target. These tests pin the type
list (sequence file and ninaAPI tree shapes), the loop detection, rule
piggy-mount in the companion lint (strict for PhotonScript's companion,
graded for a hand-built sideload), the nina_dispatch refusal, the
read-only NINA #2 check at arm / watch, and its chips.
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from photonscript.scheduler import nina2_mount_check as nm
from photonscript.scheduler import sequence_lint as sl
from photonscript.scheduler import sideload as sd
from photonscript.scheduler.nina_sequence_json import link_parents
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.phd2_store import night_of
from tests.test_scheduler.test_ps123_sideload import (  # noqa: F401
    _companion, _pinned, _post, wired)
from tests.test_scheduler.test_ps136_watch_sideload import env  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 6, 3, 0, 0)

SEQ = "NINA.Sequencer.Container.SequentialContainer, NINA.Sequencer"
DSO = "NINA.Sequencer.Container.DeepSkyObjectContainer, NINA.Sequencer"
CENTER = "NINA.Sequencer.SequenceItem.Platesolving.Center, NINA.Sequencer"
EXPOSE = "NINA.Sequencer.SequenceItem.Imaging.TakeExposure, NINA.Sequencer"
LOOP = "NINA.Sequencer.Conditions.LoopCondition, NINA.Sequencer"
ALT = "NINA.Sequencer.Conditions.AltitudeCondition, NINA.Sequencer"
FLIP = "NINA.Sequencer.Trigger.MeridianFlip.MeridianFlipTrigger, NINA.Sequencer"


def _t(cls, ns="NINA.Sequencer.SequenceItem"):
    return {"$type": f"{ns}.{cls}, NINA.Sequencer"}


def _cfg(tmp_path, **kw):
    kw.setdefault("piggyback_enabled", True)
    kw.setdefault("piggyback_nina_base_url", "http://nina2:1889/v2/api")
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _stock_template():
    """The 2026-10-03 shape: Center + exposure in a DSO container that loops
    (AltitudeCondition), a slew before it outside any loop."""
    return {"$type": SEQ, "Name": "M31 hand-built", "Items": {"$values": [
        {"$type": SEQ, "Name": "Start", "Items": {"$values": [
            {"$type": "NINA.Sequencer.SequenceItem.Telescope.SlewScopeToRaDec, "
                      "NINA.Sequencer"}]}},
        {"$type": DSO, "Name": "M31",
         "Conditions": {"$values": [{"$type": ALT}]},
         "Items": {"$values": [{"$type": CENTER}, {"$type": EXPOSE}]}}]}}


# -------------------------------------------------------------- type list

@pytest.mark.parametrize("cls,ns", [
    ("Telescope.SlewScopeToRaDec", "NINA.Sequencer.SequenceItem"),
    ("Telescope.SlewScopeToAltAz", "NINA.Sequencer.SequenceItem"),
    ("Platesolving.Center", "NINA.Sequencer.SequenceItem"),
    ("Platesolving.CenterAndRotate", "NINA.Sequencer.SequenceItem"),
    ("Platesolving.SolveAndSync", "NINA.Sequencer.SequenceItem"),
    ("Telescope.SetTracking", "NINA.Sequencer.SequenceItem"),
    ("Telescope.ParkScope", "NINA.Sequencer.SequenceItem"),
    ("Telescope.UnparkScope", "NINA.Sequencer.SequenceItem"),
    ("Telescope.FindHome", "NINA.Sequencer.SequenceItem"),
    ("MeridianFlip.MeridianFlipTrigger", "NINA.Sequencer.Trigger"),
    ("Platesolving.CenterAfterDriftTrigger", "NINA.Sequencer.Trigger"),
])
def test_mount_classes_match(cls, ns):
    assert sl.mount_label(_t(cls, ns)) == sl.MOUNT_CLASSES[cls]


def test_center_and_rotate_is_not_plain_center():
    assert sl.mount_label(_t("Platesolving.CenterAndRotate")) == "Center and rotate"
    assert sl.mount_label(_t("Platesolving.SolveAndRotate")) is None


@pytest.mark.parametrize("cls", ["Camera.CoolCamera", "Imaging.TakeExposure",
                                 "Guider.StartGuiding", "Telescope.SetTrackingRateX",
                                 "Connect.DisconnectAllEquipment"])
def test_non_mount_items_do_not_match(cls):
    assert sl.mount_label(_t(cls)) is None


def test_connect_matches_only_the_mount():
    con = _t("Connect.ConnectEquipment")
    assert sl.mount_label({**con, "SelectedDevice": "Mount"}) == sl.MOUNT_CONNECT_LABEL
    assert sl.mount_label({**con, "SelectedDevice": "Telescope"}) == sl.MOUNT_CONNECT_LABEL
    assert sl.mount_label({**con, "SelectedDevice": "Camera"}) is None


def test_types_the_generators_emit_are_in_the_list():
    """The class names the repo's own generators use for the mount."""
    src = (ROOT / "photonscript/scheduler/nina_sequence_json.py").read_text(encoding="utf-8")
    for cls in ("Telescope.SlewScopeToRaDec", "Telescope.SlewScopeToAltAz",
                "Telescope.ParkScope", "Telescope.UnparkScope",
                "Telescope.SetTracking", "Platesolving.Center",
                "MeridianFlip.MeridianFlipTrigger"):
        assert cls in src and cls in sl.MOUNT_CLASSES


@pytest.mark.parametrize("name,label", [
    ("Slew to Ra/Dec", "Slew to Ra/Dec"), ("Slew and center", "Center (slew and center)"),
    ("Center", "Center (slew and center)"), ("Slew, center and rotate", "Center and rotate"),
    ("Solve and Sync", "Solve and sync"), ("Set Tracking", "Set tracking"),
    ("Park Scope", "Park scope"), ("Unpark Scope", "Unpark scope"),
    ("Find Home", "Find home"), ("Meridian Flip", "Meridian flip trigger"),
    ("Center After Drift", "Center after drift trigger")])
def test_ninaapi_display_names(name, label):
    assert sl.mount_label({"Name": name, "Status": "CREATED"}) == label


def test_ninaapi_containers_never_match_by_name():
    assert sl.mount_label({"Name": "Center_Container", "Items": []}) is None
    assert sl.mount_label({"Name": "Take Exposure"}) is None


# ---------------------------------------------------------------- loops

def test_stock_template_center_is_in_the_loop():
    items = sl.mount_items(_stock_template())
    by = {i["label"]: i for i in items}
    assert by["Center (slew and center)"]["in_loop"] is True
    assert by["Center (slew and center)"]["where"] == "M31 hand-built/M31"
    assert by["Slew to Ra/Dec"]["in_loop"] is False


def test_single_iteration_loop_is_not_a_loop():
    seq = {"$type": SEQ, "Name": "root", "Items": {"$values": [
        {"$type": SEQ, "Name": "once",
         "Conditions": {"$values": [{"$type": LOOP, "Iterations": 1}]},
         "Items": {"$values": [{"$type": CENTER}]}},
        {"$type": SEQ, "Name": "five",
         "Conditions": {"$values": [{"$type": LOOP, "Iterations": 5}]},
         "Items": {"$values": [{"$type": CENTER}]}}]}}
    got = [(i["where"], i["in_loop"]) for i in sl.mount_items(seq)]
    assert got == [("root/once", False), ("root/five", True)]


def test_nested_in_a_looping_ancestor_is_in_a_loop():
    seq = {"$type": SEQ, "Name": "root",
           "Conditions": {"$values": [{"$type": ALT}]},
           "Items": {"$values": [{"$type": SEQ, "Name": "inner", "Items": {
               "$values": [{"$type": CENTER}]}}]}}
    assert sl.mount_items(seq)[0]["in_loop"] is True


def test_trigger_counts_as_in_the_loop():
    seq = {"$type": SEQ, "Name": "root", "Items": {"$values": []},
           "Triggers": {"$values": [{"$type": FLIP}]}}
    it = sl.mount_items(seq)[0]
    assert it["trigger"] and it["in_loop"]


def test_ninaapi_tree_shape():
    """/sequence/state: a list of areas, Items / Conditions / Triggers as
    bare lists, no $type, containers named <Name>_Container."""
    tree = {"Success": True, "Response": [
        {"Name": "Start_Container", "Status": "FINISHED", "Conditions": [],
         "Items": [{"Name": "Slew to Ra/Dec", "Status": "FINISHED"}]},
        {"Name": "Targets_Container", "Status": "RUNNING", "Conditions": [],
         "Items": [{"Name": "M31_Container", "Status": "RUNNING",
                    "Conditions": [{"Name": "Loop Until Time", "Status": "RUNNING"}],
                    "Triggers": [{"Name": "Center After Drift"}],
                    "Items": [{"Name": "Slew and center", "Status": "RUNNING"},
                              {"Name": "Take Exposure", "Status": "CREATED"}]}]}]}
    items = sl.mount_items(tree)
    got = [(i["label"], i["where"], i["in_loop"]) for i in items]
    assert got == [
        ("Slew to Ra/Dec", "Start", False),
        ("Center (slew and center)", "Targets/M31", True),
        ("Center after drift trigger", "Targets/M31", True)]
    assert items[1]["status"] == "RUNNING"


# ------------------------------------------------------- companion lint

def _with(seq, where, item):
    """Append item to the container named `where` (or the first top-level
    container), then rebuild $id / Parent links (PS-77)."""
    def find(n):
        if isinstance(n, dict):
            if n.get("Name") == where and isinstance(n.get("Items"), dict):
                return n
            for v in n.values():
                r = find(v)
                if r is not None:
                    return r
        elif isinstance(n, list):
            for v in n:
                r = find(v)
                if r is not None:
                    return r
        return None
    c = find(seq)
    assert c is not None, where
    c["Items"]["$values"].append(item)
    return link_parents(sd.strip_ids(seq))


def _rules(res, level):
    return {f.rule for f in res.findings if f.level == level}


def _first_container(seq):
    return seq["Items"]["$values"][0]["Name"]


def test_generated_companion_has_no_mount_items(tmp_path):
    seq = _companion(tmp_path)
    assert sl.mount_items(seq) == []
    res = sd.lint_companion(seq)
    assert res.ok and "piggy-mount" not in _rules(res, "WARN")


def test_osc_dusk_flats_have_no_mount_items():
    from photonscript.scheduler.calibration import generate_dusk_flats_json
    txt, _ = generate_dusk_flats_json(PhotonScriptConfig(_env_file=None),
                                      osc=True, owns_mount=False)
    assert sl.mount_items(json.loads(txt)) == []


def test_center_in_the_light_loop_is_an_error_hand_built_or_not(tmp_path):
    from photonscript.scheduler.calibration import OSC_LIGHT_LOOP_NAME
    seq = _with(_companion(tmp_path), OSC_LIGHT_LOOP_NAME, {"$type": CENTER})
    for hand in (False, True):
        res = sd.lint_companion(seq, hand_built=hand)
        assert "piggy-mount" in _rules(res, "ERROR") and not res.ok
        msg = next(f.detail for f in res.findings if f.rule == "piggy-mount")
        assert "inside a loop" in msg and OSC_LIGHT_LOOP_NAME in msg


def test_center_once_before_the_loop_warns_when_hand_built(tmp_path):
    base = _companion(tmp_path)
    seq = _with(base, _first_container(base), {"$type": CENTER})
    hand = sd.lint_companion(seq, hand_built=True)
    assert "piggy-mount" in _rules(hand, "WARN")
    assert "piggy-mount" not in _rules(hand, "ERROR")
    assert hand.ok, [f.detail for f in hand.findings if f.level == "ERROR"]
    # PhotonScript's own companion never carries one: strict
    own = sd.lint_companion(seq)
    assert {"piggy-mount", "companion-mount"} <= _rules(own, "ERROR")


def test_guiding_stays_an_error_when_hand_built(tmp_path):
    base = _companion(tmp_path)
    seq = _with(base, _first_container(base),
                _t("Guider.StartGuiding"))
    assert "companion-mount" in _rules(sd.lint_companion(seq, hand_built=True), "ERROR")


# --------------------------------------------------------- sideload body

def test_sideload_body_center_in_loop_refused(wired, tmp_path):
    from photonscript.scheduler.calibration import OSC_LIGHT_LOOP_NAME
    seq = _with(_companion(tmp_path), OSC_LIGHT_LOOP_NAME, {"$type": CENTER})
    code, b = _post(rig="piggyback", recipe="", payload=seq)
    assert code == 422 and not b["lint"]["ok"]
    assert any(f["rule"] == "piggy-mount" for f in b["lint"]["findings"])
    assert wired["nina"].loaded is None


def test_sideload_body_center_before_loop_loads_with_warning(wired, tmp_path):
    base = _companion(tmp_path)
    seq = _with(base, _first_container(base), {"$type": CENTER})
    code, b = _post(rig="piggyback", recipe="", payload=seq)
    assert code == 200 and b["ok"], b.get("detail")
    assert any(f["rule"] == "piggy-mount" and f["level"] == "WARN"
               for f in b["lint"]["findings"])


# ---------------------------------------------------------- nina_dispatch

def test_nina_dispatch_refuses_looped_mount_on_piggyback(monkeypatch):
    from photonscript.shared import rigs

    def boom(*a, **k):
        raise AssertionError("no HTTP call expected")
    monkeypatch.setattr(rigs.httpx, "AsyncClient", boom)
    res = asyncio.run(rigs.nina_dispatch("http://nina2:1889/v2/api",
                                         _stock_template(), rig=rigs.PIGGYBACK))
    assert res["ok"] is False and "PS-139" in res["detail"]
    assert "Center" in res["detail"]


def test_nina_dispatch_rc16_not_affected(monkeypatch):
    from photonscript.shared import rigs
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"Success": True})
    real = httpx.AsyncClient
    monkeypatch.setattr(rigs.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, transport=httpx.MockTransport(handler), **k))
    res = asyncio.run(rigs.nina_dispatch("http://nina1:1888/v2/api",
                                         _stock_template(), rig=rigs.RC16))
    assert res["ok"] and calls[-1].endswith("/sequence/start")


# ----------------------------------------------------- NINA #2 live check

def _reader(tree, err=None):
    seen = []

    async def read(base):
        seen.append(base)
        return tree, err
    read.seen = seen
    return read


def _pusher():
    sent = []

    async def push(config, msg, title="", priority=0):
        sent.append((msg, title, priority))
    push.sent = sent
    return push


LIVE = [{"Name": "Targets_Container", "Status": "CREATED", "Items": [
    {"Name": "M31_Container", "Conditions": [{"Name": "Loop Until Time"}],
     "Items": [{"Name": "Slew and center"}, {"Name": "Take Exposure"}]}]}]


def test_check_finds_pushes_once_and_records(tmp_path):
    cfg = _cfg(tmp_path, piggyback_calibrate_on_arm=False)
    push, read = _pusher(), _reader(LIVE)
    rec = asyncio.run(nm.check(cfg, "watch", now=NOW, reader=read, push=push))
    assert read.seen == ["http://nina2:1889/v2/api"]
    assert rec["severity"] == "fail" and rec["in_loop"] == 1 and rec["pushed"]
    assert len(push.sent) == 1 and push.sent[0][2] == 1
    assert "Center" in push.sent[0][0] and "Remove them" in push.sent[0][0]
    assert nm.load_latest(cfg)["night"] == night_of(cfg, NOW)
    # same night, same findings: no second push
    asyncio.run(nm.check(cfg, "watch", now=NOW + timedelta(minutes=5),
                         reader=_reader(LIVE), push=push))
    assert len(push.sent) == 1
    assert nm.tonight(cfg, NOW)["items"][0]["label"] == "Center (slew and center)"


def test_check_at_arm_says_the_companion_replaces_it(tmp_path):
    cfg = _cfg(tmp_path)          # piggyback_calibrate_on_arm default True
    push = _pusher()
    rec = asyncio.run(nm.check(cfg, "arm", now=NOW, reader=_reader(LIVE), push=push))
    assert rec["replaced"] and push.sent[0][2] == 0
    assert "replaces it at pre-config" in push.sent[0][0]


def test_clean_nina2_clears_the_finding(tmp_path):
    cfg = _cfg(tmp_path)
    push = _pusher()
    asyncio.run(nm.check(cfg, "arm", now=NOW, reader=_reader(LIVE), push=push))
    rec = asyncio.run(nm.check(cfg, "watch", now=NOW, reader=_reader([]), push=push))
    assert rec["severity"] == "pass" and nm.tonight(cfg, NOW) is None
    assert len(push.sent) == 1


def test_unreadable_nina2_keeps_the_last_record(tmp_path):
    cfg = _cfg(tmp_path)
    push = _pusher()
    asyncio.run(nm.check(cfg, "arm", now=NOW, reader=_reader(LIVE), push=push))
    rec = asyncio.run(nm.check(cfg, "watch", now=NOW,
                               reader=_reader(None, "ConnectError: down"), push=push))
    assert rec["error"] and nm.tonight(cfg, NOW) is not None


@pytest.mark.parametrize("kw", [{"piggyback_enabled": False},
                                {"piggyback_mount_check": "off"}])
def test_check_off(tmp_path, kw):
    cfg = _cfg(tmp_path, **kw)
    read = _reader(LIVE)
    assert asyncio.run(nm.check(cfg, "arm", now=NOW, reader=read,
                                push=_pusher())) is None
    assert read.seen == []


def test_mode_and_config_field():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    assert PhotonScriptConfig.model_fields["piggyback_mount_check"].default == "alert"
    assert nm.mode(PhotonScriptConfig(_env_file=None, piggyback_mount_check="OFF")) == "off"
    assert nm.mode(PhotonScriptConfig(_env_file=None, piggyback_mount_check="x")) == "alert"
    assert "PS_PIGGYBACK_MOUNT_CHECK" in {f[1] for f in _CONFIG_FIELDS}


def test_check_never_raises(tmp_path):
    async def bad(base):
        raise RuntimeError("boom")
    assert asyncio.run(nm.check(_cfg(tmp_path), "arm", now=NOW, reader=bad,
                                push=_pusher())) is None


# ------------------------------------------------------------- chips

def test_guiding_attention_item(tmp_path):
    from photonscript.scheduler import guiding_attention as ga
    cfg = _cfg(tmp_path)
    asyncio.run(nm.check(cfg, "watch", now=NOW, reader=_reader(LIVE), push=_pusher()))
    s = ga.build(cfg, NOW, phd2={}, thesky={})
    it = next(i for i in s["items"] if i["id"] == "nina2_mount")
    assert it["severity"] == "fail" and it["where"] == "NINA #2"
    assert it["priority"] == 0 and "Center" in it["current"]


def test_armer_status_carries_tonights_finding(tmp_path, monkeypatch):
    from photonscript.scheduler.armer import Armer
    cfg = _cfg(tmp_path)
    a = Armer(cfg)
    assert a.status()["nina2_mount"] is None
    asyncio.run(nm.check(cfg, "arm", now=datetime.utcnow(), reader=_reader(LIVE),
                         push=_pusher()))
    assert a.status()["nina2_mount"]["in_loop"] == 1


async def test_arm_starts_the_check(tmp_path, monkeypatch):
    from photonscript.scheduler import armer as armer_mod
    started = []
    monkeypatch.setattr(armer_mod.Armer, "_start_nina2_mount_check",
                        lambda self, reason: started.append(reason))

    async def _noop(*a, **k):
        return None
    a = armer_mod.Armer(_cfg(tmp_path, connect_all_on_arm=False,
                             cooler_off_until_precool=False))
    monkeypatch.setattr(armer_mod, "notify", _noop)
    monkeypatch.setattr("photonscript.scheduler.night_plan.build_night_plan",
                        lambda cfg: {"night_of": "2026-10-05", "targets": ["M31"],
                                     "preconfig_utc": "2026-10-06T00:30:00Z",
                                     "dark_hours": 8.0})
    monkeypatch.setattr(a, "_run", _noop)
    monkeypatch.setattr(a, "_nina", _noop)
    await a.arm("encoders")
    assert started == ["arm"]


async def test_watch_starts_the_check(env, monkeypatch):
    from photonscript.scheduler import armer as armer_mod
    from tests.test_scheduler import test_ps136_watch_sideload as w
    started = []
    monkeypatch.setattr(armer_mod.Armer, "_start_nina2_mount_check",
                        lambda self, reason: started.append(reason))
    b = w._armer(env)
    res = await b.start_watch("button", now=w.NOW)
    if b._task:
        b._task.cancel()
    assert res["ok"] and started == ["watch"]


async def test_start_check_spawns_only_when_enabled(tmp_path, monkeypatch):
    from photonscript.scheduler import armer as armer_mod
    calls = []

    async def fake_check(config, reason):
        calls.append(reason)
    monkeypatch.setattr(nm, "check", fake_check)
    off = armer_mod.Armer(_cfg(tmp_path, piggyback_enabled=False))
    off._start_nina2_mount_check("arm")
    assert off._nina2_task is None
    on = armer_mod.Armer(_cfg(tmp_path))
    on._start_nina2_mount_check("watch")
    await on._nina2_task
    assert calls == ["watch"]


def test_dashboard_chip_and_ascii():
    dash = (ROOT / "photonscript/scheduler/templates/dashboard.html").read_text(encoding="utf-8")
    assert "d.nina2_mount" in dash and "NINA #2 holds mount" in dash
    for p in ("photonscript/scheduler/nina2_mount_check.py",
              "photonscript/scheduler/sideload.py"):
        assert (ROOT / p).read_bytes().isascii()
