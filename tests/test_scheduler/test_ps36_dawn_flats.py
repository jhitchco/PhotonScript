"""PS-36: Piggy-600 dawn flats actually get shot.

2026-09-26 root cause: the roof closed for clouds at 11:39Z, and the companion
sat in the light loop's unbounded WaitUntilSafe until NINA #2 restarted, never
reaching its flats. Latent: the armer's dawn shutdown (astro dawn + 30) always
fired before the flat window (nautical dawn + 5; 28-37 min later at AARO), and
never stopped NINA #2. These tests pin the fixes: bounded waits, a gated flat
set of piggyback_flat_count at the OSC gain/offset, and a shutdown that waits
for the flat window unless the roof is closed.
"""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from photonscript.shared import rigs
from photonscript.shared.config import PhotonScriptConfig
from photonscript.scheduler import armer as armer_mod
from photonscript.scheduler.armer import Armer
from photonscript.scheduler.calibration import generate_piggyback_companion_json

_SKYFLAT = "NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat"


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _items(node):
    return node["Items"]["$values"]


def _conds(node):
    return node["Conditions"]["$values"]


def _short(t):
    return t.split(",")[0].split(".")[-1]


def _companion(has_safety=True, with_lights=True, **overrides):
    cfg = rigs.rig_config(PhotonScriptConfig(
        piggyback_enabled=True, piggyback_default_gain=100,
        piggyback_default_offset=256, piggyback_dark_exposures="120",
        piggyback_setpoint_c=0.0, piggyback_image_lights=True,
        piggyback_focus_seed=0, **overrides), "piggyback")
    return json.loads(generate_piggyback_companion_json(
        cfg, has_safety=has_safety, with_lights=with_lights))


def _targets(root):
    return next(n for n in _walk(root)
                if n.get("$type", "").startswith(
                    "NINA.Sequencer.Container.TargetAreaContainer"))


# ---- companion -------------------------------------------------------------

def test_osc_flats_default_25_at_osc_gain_offset():
    root = _companion()
    sf = [n for n in _walk(root) if n.get("$type", "").startswith(_SKYFLAT)]
    assert len(sf) == 1
    # PS-132: NINA's SkyFlat needs its SwitchFilter child (Filter null: no wheel)
    sw, loop = _items(sf[0])
    assert _short(sw["$type"]) == "SwitchFilter" and sw["Filter"] is None
    assert _conds(loop)[0]["Iterations"] == 25
    take = _items(loop)[0]
    assert (take["ImageType"], take["Gain"], take["Offset"]) == ("FLAT", 100, 256)


def test_osc_flat_count_is_configurable():
    root = _companion(piggyback_flat_count=30)
    sf = next(n for n in _walk(root) if n.get("$type", "").startswith(_SKYFLAT))
    assert _conds(_items(sf)[-1])[0]["Iterations"] == 30


def test_light_loop_waits_are_bounded_and_passes_gated():
    root = _companion()
    lights = next(n for n in _walk(root)
                  if n.get("Name") == "OSC_LIGHTS_UNTIL_DAWN")
    first, hold, confirm, image_pass = _items(lights)  # PS-25 resume hold
    # bounded wait: loops a short timespan while unsafe AND before naut. dawn
    assert first["Name"] == "WAIT_SAFE_OR_NAUTICAL_DAWN"
    kinds = [_short(c["$type"]) for c in _conds(first)]
    assert kinds == ["LoopWhileUnsafe", "TimeCondition"]
    assert "NauticalDawnProvider" in _conds(first)[1]["SelectedProvider"]["$type"]
    assert [_short(i["$type"]) for i in _items(first)] == ["WaitForTimeSpan"]
    assert confirm["Name"] == "WAIT_SAFE_CONFIRM_OR_NAUTICAL_DAWN"
    assert [_short(c["$type"]) for c in _conds(confirm)] == kinds
    assert hold["Name"].startswith("OSC_RESUME_HOLD")
    assert [_short(c["$type"]) for c in _conds(hold)] == [
        "LoopCondition", "TimeCondition"]
    assert "NauticalDawnProvider" in _conds(hold)[1]["SelectedProvider"]["$type"]
    # image pass: skipped when unsafe, runs once per outer pass, ends at dawn
    assert image_pass["Name"] == "OSC_IMAGE_PASS"
    assert [_short(c["$type"]) for c in _conds(image_pass)] == [
        "SafetyMonitorCondition", "LoopCondition", "TimeCondition"]
    inner = next(i for i in _items(image_pass) if i.get("Name") == "OSC_LIGHT_LOOP")
    assert [_short(c["$type"]) for c in _conds(inner)] == [
        "SafetyMonitorCondition", "TimeCondition"]
    # nowhere an unbounded WaitUntilSafe
    assert not any("WaitUntilSafe" in n.get("$type", "") for n in _walk(root))


def test_dawn_flat_step_order_and_gating():
    root = _companion(piggyback_flat_wait_min=20)
    names = [(_short(i["$type"]), i.get("Name")) for i in _items(_targets(root))]
    kinds = [k for k, _ in names]
    lights_i = [n for _, n in names].index("OSC_LIGHTS_UNTIL_DAWN")
    wait_i = kinds.index("WaitForTime", lights_i)
    items = _items(_targets(root))
    assert "NauticalDawnProvider" in items[wait_i]["SelectedProvider"]["$type"]
    assert items[wait_i]["MinutesOffset"] == 5
    # let the RC16's dawn slew land before the first flat
    assert kinds[wait_i + 1] == "WaitForTimeSpan"
    assert items[wait_i + 1]["Time"] == 90
    wait_safe = items[wait_i + 2]
    assert wait_safe["Name"] == "WAIT_SAFE_FOR_OSC_FLATS"
    tc = _conds(wait_safe)[1]
    assert _short(tc["$type"]) == "TimeCondition" and tc["MinutesOffset"] == 20
    flats = items[wait_i + 3]
    assert flats["Name"].startswith("DAWN_SKY_FLATS_OSC")
    assert [_short(c["$type"]) for c in _conds(flats)] == [
        "SafetyMonitorCondition", "LoopCondition"]
    assert any(i["$type"].startswith(_SKYFLAT) for i in _items(flats))


def test_no_safety_monitor_flats_stay_time_gated_only():
    root = _companion(has_safety=False, with_lights=False)
    items = _items(_targets(root))
    assert any(i["$type"].startswith(_SKYFLAT) for i in items)
    types = {n.get("$type", "") for n in _walk(root)}
    assert not any("LoopWhileUnsafe" in t or "WaitUntilSafe" in t
                   or "SafetyMonitorCondition" in t for t in types)


# ---- armer: shutdown timing -------------------------------------------------

def _armer(tmp_path, **cfg):
    return Armer(PhotonScriptConfig(data_dir=tmp_path, piggyback_enabled=True,
                                    **cfg))


_PLAN_0926 = {"night_of": "2026-09-26",
              "dawn_utc": "2026-09-27T11:48:14Z",
              "naut_dawn_utc": "2026-09-27T12:15:00Z",
              "sunrise_utc": "2026-09-27T13:09:00Z"}


def test_shutdown_waits_for_flat_window(tmp_path):
    a = _armer(tmp_path)
    a.plan = dict(_PLAN_0926)
    # nautical dawn + 5 + 40, not astro dawn + 30 (12:18Z, before any flat)
    assert a._shutdown_due_at() == datetime(2026, 9, 27, 13, 0, 0)


def test_shutdown_capped_at_sunrise(tmp_path):
    a = _armer(tmp_path, dawn_flats_window_min=120)
    a.plan = dict(_PLAN_0926)
    assert a._shutdown_due_at() == datetime(2026, 9, 27, 13, 9, 0)


def test_shutdown_unchanged_when_no_flats_expected(tmp_path):
    base = datetime(2026, 9, 27, 12, 18, 14)
    a = Armer(PhotonScriptConfig(data_dir=tmp_path, dawn_flats_enabled=False,
                                 piggyback_enabled=False))
    a.plan = dict(_PLAN_0926)
    assert a._shutdown_due_at() == base
    b = _armer(tmp_path, dawn_flats_window_min=0)
    b.plan = dict(_PLAN_0926)
    assert b._shutdown_due_at() == base


def test_shutdown_computes_nautical_dawn_for_old_plans(tmp_path):
    # plans persisted before PS-36 carry only dawn_utc
    a = _armer(tmp_path)
    a.plan = {"dawn_utc": "2026-09-27T11:48:14Z"}
    due = a._shutdown_due_at()
    # nautical dawn 12:15Z at AARO (+/- sampling) + 45 min
    assert datetime(2026, 9, 27, 12, 55) <= due <= datetime(2026, 9, 27, 13, 6)


def _live_plan(dawn_ago_min, naut_ago_min, sunrise_in_min=60):
    now = datetime.utcnow()
    iso = lambda d: d.isoformat() + "Z"  # noqa: E731
    return {"night_of": "x",
            "dawn_utc": iso(now - timedelta(minutes=dawn_ago_min)),
            "naut_dawn_utc": iso(now - timedelta(minutes=naut_ago_min)),
            "sunrise_utc": iso(now + timedelta(minutes=sunrise_in_min))}


def _wire_tick(monkeypatch, a, safe):
    calls = []

    async def _safe():
        return safe

    async def _shutdown(reason="dawn"):
        calls.append(reason)
        return "stop ok"

    async def _notify(cfg, msg, **kw):
        calls.append(("notify", msg))

    monkeypatch.setattr(a, "_is_safe", _safe)
    monkeypatch.setattr(a, "dawn_shutdown", _shutdown)
    monkeypatch.setattr(armer_mod, "notify", _notify)
    return calls


@pytest.mark.asyncio
async def test_tick_holds_shutdown_in_flat_window_when_safe(tmp_path, monkeypatch):
    a = _armer(tmp_path)
    a.state = "RUNNING"
    a.plan = _live_plan(dawn_ago_min=35, naut_ago_min=8)
    calls = _wire_tick(monkeypatch, a, safe=True)
    await a._tick()
    assert calls == []
    assert a.state == "RUNNING"
    assert a.detail.startswith("Dawn flat window: shutdown held until")


@pytest.mark.asyncio
async def test_tick_shuts_down_at_once_when_roof_closed(tmp_path, monkeypatch):
    a = _armer(tmp_path)
    a.state = "RUNNING"
    a.plan = _live_plan(dawn_ago_min=35, naut_ago_min=8)
    calls = _wire_tick(monkeypatch, a, safe=False)
    await a._tick()
    assert calls[0] == "dawn: unsafe, no flats possible"
    assert a.state == "COMPLETE"


@pytest.mark.asyncio
async def test_tick_shuts_down_when_window_over(tmp_path, monkeypatch):
    a = _armer(tmp_path)
    a.state = "RUNNING"
    a.plan = _live_plan(dawn_ago_min=100, naut_ago_min=70)
    calls = _wire_tick(monkeypatch, a, safe=True)
    await a._tick()
    assert calls[0] == "dawn"
    assert a.state == "COMPLETE"


def test_status_surfaces_shutdown_due(tmp_path):
    a = _armer(tmp_path)
    a.plan = dict(_PLAN_0926)
    a.state = "RUNNING"
    assert a.status()["shutdown_due_utc"] == "2026-09-27T13:00:00Z"
    a.state = "COMPLETE"
    assert a.status()["shutdown_due_utc"] is None


@pytest.mark.asyncio
async def test_restore_reattaches_inside_flat_window(tmp_path, monkeypatch):
    a = _armer(tmp_path, connect_all_on_arm=False)
    (tmp_path / "armer_state.json").write_text(json.dumps({
        "state": "RUNNING", "detail": "",
        "plan": _live_plan(dawn_ago_min=35, naut_ago_min=8)}))

    async def _noop(*a_, **k):
        return None

    monkeypatch.setattr(a, "_run", _noop)
    monkeypatch.setattr(armer_mod, "notify", _noop)
    # astro dawn has passed, but the shutdown (after the flats) has not
    assert a.restore() is True
    b = _armer(tmp_path, connect_all_on_arm=False)
    (tmp_path / "armer_state.json").write_text(json.dumps({
        "state": "RUNNING", "detail": "",
        "plan": _live_plan(dawn_ago_min=100, naut_ago_min=70)}))
    assert b.restore() is False


# ---- armer: dawn_shutdown stops NINA #2 too ---------------------------------

@pytest.mark.asyncio
async def test_dawn_shutdown_stops_the_piggyback_sequence(tmp_path, monkeypatch):
    import photonscript.shared.rigs as rigs_mod
    import photonscript.scheduler.runs as runs_mod
    a = _armer(tmp_path)
    stopped, warmed = [], []

    async def _nina(key, *args, **kw):
        return {"Success": True}

    async def _ok(base, *args, **kw):
        warmed.append(base)
        return {"ok": True}

    async def _stop(base):
        stopped.append(base)
        return {"ok": True}

    async def _noop(*a_, **k):
        return None

    urls = {"rc16": "http://nina1", "piggyback": "http://nina2"}
    monkeypatch.setattr(a, "_nina", _nina)
    monkeypatch.setattr(a, "_verify_shutdown", _noop)
    monkeypatch.setattr(rigs_mod, "rig_ids", lambda cfg: ["rc16", "piggyback"])
    monkeypatch.setattr(rigs_mod, "rig_config", lambda cfg, r:
                        SimpleNamespace(nina_base_url=urls[r]))
    monkeypatch.setattr(rigs_mod, "nina_warm", _ok)
    monkeypatch.setattr(rigs_mod, "nina_dew_heater", _ok)
    monkeypatch.setattr(rigs_mod, "nina_sequence_stop", _stop)
    monkeypatch.setattr(runs_mod, "post_night_warm", lambda cfg: [])
    report = await a.dawn_shutdown(reason="dawn")
    assert stopped == ["http://nina2"]     # NINA #1 is stopped via _nina
    assert "piggyback stop ok" in report
    assert "stop ok" in report.split(" · ")[0]
