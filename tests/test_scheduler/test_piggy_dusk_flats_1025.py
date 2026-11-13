"""Piggy-600 dusk sky flats under a closed roof (2026-10-25).

10-24 and 10-25 the OSC dusk set was dispatched with the roof closed
(flats_dispatch_roof_closed) and saved nothing. NINA #2's log on 10-25: the
cool ended 18:05:03, DUSK_SKY_FLATS_OSC (SafetyMonitorCondition, the 19:19
cap, run once) read Unsafe on entry, skipped both items (the 18:44 wait and
the set) and the run finished; the roof read safe at 18:05:35. The RC16 run
waits for the roof first (WAIT_SAFE_FOR_DUSK_FLATS) and saved its sets.

Pins: the OSC dusk run has no SafetyMonitor condition on its outer container;
each set waits for its start, then for the roof (bounded by the set's stop),
then shoots under its own SafetyMonitor condition; without a safety monitor
there is no wait and no condition; the run lints clean as a companion.
"""
import json
from datetime import timedelta


from photonscript.scheduler import calibration as cal
from photonscript.scheduler import flat_plan as fp
from photonscript.scheduler.sideload import lint_companion
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.rigs import rig_config

NIGHT = "2026-10-23"
SAFETY = "NINA.Sequencer.Conditions.SafetyMonitorCondition"
UNSAFE = "NINA.Sequencer.Conditions.LoopWhileUnsafe"
TIME = "NINA.Sequencer.Conditions.TimeCondition"
WAIT_TIME = "NINA.Sequencer.SequenceItem.Utility.WaitForTime"
SKYFLAT = "NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat"


def _cfg(tmp_path, **kw):
    kw.setdefault("observatory_tz", "America/Denver")
    kw.setdefault("evening_forecast_enabled", False)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"),
                              image_watch_dir=str(tmp_path / "nina"),
                              nina_logs_dir=str(tmp_path / "logs"),
                              piggyback_enabled=True, **kw)


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _vals(node, key):
    v = node.get(key) or []
    return v.get("$values", []) if isinstance(v, dict) else v


def _types(node, key="Conditions"):
    return [c.get("$type", "") for c in _vals(node, key)]


def _osc_run(tmp_path, safety):
    cfg = _cfg(tmp_path)
    tw = fp.twilight(cfg, NIGHT)
    now = tw["sunset"] - timedelta(minutes=40)
    p = fp.plan(cfg, "dusk", NIGHT, ["L"], osc=True, ready=now)
    slots = p["rigs"]["piggyback"]["slots"]
    assert slots, p["rigs"]["piggyback"]
    seq = json.loads(cal.generate_dusk_flats_json(
        rig_config(cfg, "piggyback"), osc=True, owns_mount=False, safety=safety,
        now=now, plan=p)[0])
    outer = next(n for n in _walk(seq)
                 if str(n.get("Name", "")).startswith("DUSK_SKY_FLATS_OSC"))
    return cfg, slots, seq, outer


def test_closed_roof_does_not_skip_the_osc_set(tmp_path):
    cfg, slots, seq, outer = _osc_run(tmp_path, safety=True)
    # the outer container must not read the roof on entry
    assert not any(t.startswith(SAFETY) for t in _types(outer))
    assert any(t.startswith(TIME) for t in _types(outer))
    items = _vals(outer, "Items")
    kinds = [i.get("$type", "").split(",")[0] for i in items]
    names = [str(i.get("Name", "")) for i in items]
    # per set: wait for its start, wait for the roof, the set
    assert kinds[0] == WAIT_TIME
    assert names[1] == "WAIT_SAFE_FOR_OSC_FLATS"
    assert names[2].startswith(cal.FLAT_SLOT_PREFIX + "OSC")
    wait = items[1]
    assert any(t.startswith(UNSAFE) for t in _types(wait))
    stop = fp.to_local(cfg, slots[0]["stop"])
    tc = next(c for c in _vals(wait, "Conditions") if c["$type"].startswith(TIME))
    assert (tc["Hours"], tc["Minutes"]) == (stop.hour, stop.minute)
    # the set itself still stops on an unsafe roof
    assert any(t.startswith(SAFETY) for t in _types(items[2]))
    assert any(n.get("$type", "").startswith(SKYFLAT) for n in _walk(items[2]))
    assert lint_companion(seq).ok


def test_no_safety_monitor_no_wait(tmp_path):
    _cfg_, _slots, seq, outer = _osc_run(tmp_path, safety=False)
    assert not any(str(n.get("Name", "")) == "WAIT_SAFE_FOR_OSC_FLATS"
                   for n in _walk(seq))
    assert not any(t.startswith(SAFETY) for n in _walk(seq)
                   for t in _types(n))
    assert lint_companion(seq).ok
