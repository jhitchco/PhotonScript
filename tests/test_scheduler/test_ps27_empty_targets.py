"""PS-27: no empty target containers, and acquire once per entry.

2026-09-20/21: Andromeda was LRGB-only on a bright-moon night. The generator
deferred broadband, left a DSO container with zero exposures, and NINA looped
its Slew/AF/Center/StartGuiding every ~8 minutes until dawn, splitting 47
Piggy-600 subs. These tests pin the fix."""

import json

from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler.moon import broadband_deferred
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.sequence_lint import lint

BRIGHT = {"available": True, "down_at_dusk": False, "illum_pct": 99,
          "rise_local_hh": None, "rise_local_mm": None}


def _plan(f, s=180, c=10):
    return ExposurePlan(filter_type=f, exposure_seconds=s, count=c,
                        gain=200, offset=256)


def _target(name, exps, ra=0.7, dec=41.3):
    return NinaSequenceTarget(name=name, ra_hours=ra, dec_degrees=dec,
                              exposures=exps)


def _dsos(seq):
    return [d for _, d in _walk(seq) if "DeepSkyObjectContainer" in d.get("$type", "")]


def _walk(node, path=""):
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


def _count(node, fragment):
    return sum(1 for _, d in _walk(node) if fragment in d.get("$type", ""))


def _gen(targets):
    return json.loads(nsj.generate_nina_json(build_sequence_for_night("T", targets)))


# --- moon rule -------------------------------------------------------------

def test_broadband_deferred_rule():
    assert broadband_deferred(BRIGHT)
    assert not broadband_deferred({"available": True, "down_at_dusk": True,
                                   "illum_pct": 99})
    assert not broadband_deferred({"available": True, "down_at_dusk": False,
                                   "illum_pct": 10})
    assert broadband_deferred({"available": False})


def test_broadband_deferred_zero_illumination_is_not_unknown():
    """PS-106: a 0% (new) moon up at dusk is dark sky, not "unknown"."""
    up = {"available": True, "down_at_dusk": False}
    assert not broadband_deferred({**up, "illum_pct": 0})
    assert not broadband_deferred({**up, "illum_pct": 1})
    assert broadband_deferred({**up, "illum_pct": None})
    assert broadband_deferred(up)
    assert broadband_deferred({**up, "illum_pct": 20})


# --- generator -------------------------------------------------------------

def test_broadband_only_target_skipped_under_bright_moon(monkeypatch):
    monkeypatch.setattr(nsj, "_moon_window", lambda: BRIGHT)
    seq = _gen([
        _target("Andromeda", [_plan(FilterType.LUMINANCE), _plan(FilterType.RED)]),
        _target("Crescent", [_plan(FilterType.HA, 900, 6)], ra=20.2, dec=38.4),
    ])
    names = [(d.get("Target") or {}).get("TargetName") for d in _dsos(seq)]
    assert names == ["Crescent"]
    blob = json.dumps(seq)
    assert "Andromeda: skipped tonight" in blob


def test_mixed_target_keeps_only_narrowband_under_bright_moon(monkeypatch):
    monkeypatch.setattr(nsj, "_moon_window", lambda: BRIGHT)
    seq = _gen([_target("M8", [_plan(FilterType.LUMINANCE),
                               _plan(FilterType.HA, 600, 4)], ra=18.06, dec=-24.4)])
    (dso,) = _dsos(seq)
    blob = json.dumps(dso)
    assert '"Ha"' in blob or "Ha" in blob
    assert _count(dso, "SmartExposure") == 1


def test_target_acquires_once_and_images_in_inner_loop():
    seq = _gen([_target("Crescent", [_plan(FilterType.HA, 900, 6),
                                     _plan(FilterType.OIII, 900, 6)],
                        ra=20.2, dec=38.4)])
    (dso,) = _dsos(seq)
    # outer container runs once per entry
    outer = json.dumps(dso["Conditions"])
    assert "LoopCondition" in outer and '"Iterations": 1' in outer
    assert "SafetyMonitorCondition" in outer and "AltitudeCondition" in outer
    # acquisition happens exactly once in the target
    assert _count(dso, "SlewScopeToRaDec") + _count(dso, "SlewScopeAndCenter") >= 1
    assert _count(dso, "Platesolving.Center") == 1
    # exposures live in an inner container that loops while safe and up
    inner = [it for it in dso["Items"]["$values"]
             if "SequentialContainer" in it.get("$type", "")
             and "imaging" in (it.get("Name") or "")]
    assert len(inner) == 1
    ic = json.dumps(inner[0]["Conditions"])
    assert "SafetyMonitorCondition" in ic and "AltitudeCondition" in ic
    assert _count(inner[0], "SmartExposure") == 2
    assert _count(inner[0], "Platesolving.Center") == 0


def test_generated_sequence_lints_clean():
    seq = _gen([_target("Crescent", [_plan(FilterType.HA, 900, 6)],
                        ra=20.2, dec=38.4)])
    res = lint(seq, guided=None)
    assert res.ok, [f"{f.rule}: {f.detail}" for f in res.findings]
    assert not [f for f in res.findings if f.rule in ("empty-target", "reacquire")]


# --- lint --------------------------------------------------------------------

def test_lint_flags_empty_target():
    seq = _gen([_target("Crescent", [_plan(FilterType.HA, 900, 6)],
                        ra=20.2, dec=38.4)])
    (dso,) = _dsos(seq)
    # strip every exposure from the target to simulate the old empty container
    dso["Items"]["$values"] = [it for it in dso["Items"]["$values"]
                               if "SequentialContainer" not in it.get("$type", "")]
    res = lint(seq, guided=None)
    assert any(f.rule == "empty-target" for f in res.findings)
