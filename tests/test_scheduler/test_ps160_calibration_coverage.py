"""PS-160: calibration coverage drives the night / dawn plan.

Evidence (2026-10-07, /api/calibration/health): Piggy FLAT latest
2026-09-21; Piggy darks only 120 s / 400 s while its lights are 300 s; RC16
flats per filter of mixed age. Covers: lights-driven dark lengths (owed view
and the companion / RC16 quota agree), darks behind the cooler gate, flats
"as used" with an optics-change reset and a per-morning cap, the dark age
and temperature columns, the plan block and the morning line.
"""
import json

import pytest

from photonscript.scheduler import calibration
from photonscript.scheduler import calibration_owed as co
from photonscript.scheduler import calibration_plan as cp
from photonscript.scheduler import calibration_qa as cq
from tests.test_scheduler.test_ps122_calibration_owed import (
    _ago, _by_exp, _cfg, _m31, _piggy_setup, osc, store_frames, subs_log)


def test_lights_driven_lengths_fill_automatically(tmp_path):
    """Piggy darks 120 s only, lights 400 s (x3) and 300 s (x1): both
    lengths join the night quota, no config fix is needed."""
    cfg, n1, n2 = _piggy_setup(tmp_path)
    r = co.owed_report(cfg, "piggyback", projects=[_m31()])["rigs"][0]
    d = _by_exp(r)
    assert d[400.0]["auto_fill"] and d[400.0]["from_lights"]
    assert d[300.0]["auto_fill"] and d[300.0]["from_lights"]
    assert not d[120.0]["from_lights"]
    assert r["config_fixes"] == []
    assert any("Filled at night from the lights" in t for t in r["items"])
    assert [x["exp_s"] for x in r["plan"]["dark_sets"]] == [120.0, 300.0, 400.0]


def test_lights_driven_lengths_capped_most_used_first(tmp_path):
    cfg, *_ = _piggy_setup(tmp_path, calibration_darks_follow_lights_max=1)
    lights = co.collect_lights(cfg, "piggyback", [_m31()], days=60,
                               now=__import__("datetime").datetime.now())
    assert co.light_dark_lengths(cfg, "piggyback", lights) == [400.0]
    r = co.owed_report(cfg, "piggyback", projects=[_m31()])["rigs"][0]
    d = _by_exp(r)
    assert d[400.0]["auto_fill"] and not d[300.0]["auto_fill"]
    assert r["config_fixes"] and r["config_fixes"][0]["add"] == [300.0]


def test_off_epoch_lights_never_drive_darks(tmp_path):
    cfg, *_ = _piggy_setup(tmp_path)
    lights = [dict(exp_s=500.0, gain=0, offset=256, settemp=0.0, xbin=1,
                   readout=None)]
    assert co.light_dark_lengths(cfg, "piggyback", lights) == []


def test_companion_blocks_follow_the_lights(tmp_path, monkeypatch):
    cfg, *_ = _piggy_setup(tmp_path)
    monkeypatch.setattr(cp, "load_projects", lambda c: [_m31()])
    r = co.owed_report(cfg, "piggyback", projects=[_m31()])["rigs"][0]
    d = _by_exp(r)
    names = sorted(b["Name"] for b in
                   calibration._osc_dark_blocks(cq.rig_view(cfg, "piggyback")))
    assert names == sorted(f"OSC DARKS_{e:.0f}s (need {d[e]['owed']} of 30)"
                           for e in (120.0, 300.0, 400.0))


def test_night_dark_exposures_rc16_adds_used_length(tmp_path, monkeypatch):
    from photonscript.shared.models import (CelestialTarget, ExposurePlan,
                                            FilterType, ImagingProject)
    cfg = _cfg(tmp_path)
    heart = ImagingProject(
        id="heart", active=True,
        target=CelestialTarget(name="Heart Nebula", ra_hours=2.56,
                               dec_degrees=61.45),
        exposure_plans=[ExposurePlan(filter_type=FilterType("Ha"),
                                     exposure_seconds=900, count=30)])
    monkeypatch.setattr(cp, "load_projects", lambda c: [heart])
    subs_log(cfg, _ago(2), [{"rig": "rc16", "target": "Heart Nebula",
                             "filter": "Ha", "exp_s": 900.0, "gain": 200,
                             "offset": 256, "set_temp": 0.0, "xbin": 1}] * 2)
    assert calibration.night_dark_exposures(cfg, "rc16") == \
        calibration.quota_exposures(cfg, "rc16") + [900.0]
    off = _cfg(tmp_path, calibration_darks_follow_lights=False)
    assert calibration.night_dark_exposures(off, "rc16") == \
        calibration.quota_exposures(off, "rc16")


# ---- darks only at the setpoint (cooler gate) -----------------------------

def _gate_cfg(tmp_path, **kw):
    script = tmp_path / "cooler-gate.cmd"
    script.write_text("@echo off\n")
    return _cfg(tmp_path, cooler_gate_script=str(script), **kw)


@pytest.fixture
def gate_on(monkeypatch):
    """conftest pins the gate off suite-wide (env); turn it back on."""
    monkeypatch.setenv("PS_COOLER_GATE_MODE", "skip")


def test_dark_gate_items_modes(tmp_path, gate_on, monkeypatch):
    cfg = _gate_cfg(tmp_path)
    g = calibration.dark_gate_items(cfg, "rc16", 0.0)
    assert len(g) == 1 and "cooler-gate" in g[0]["Script"].lower()
    assert g[0]["ErrorBehavior"] == 1           # skip mode: skip the darks
    assert calibration.dark_gate_items(
        _gate_cfg(tmp_path, calibration_darks_gated=False), "rc16", 0.0) == []
    monkeypatch.setenv("PS_COOLER_GATE_MODE", "off")
    assert calibration.dark_gate_items(_gate_cfg(tmp_path), "rc16", 0.0) == []
    monkeypatch.setenv("PS_COOLER_GATE_MODE", "warn")
    warn = calibration.dark_gate_items(_gate_cfg(tmp_path), "rc16", 0.0)
    assert warn[0]["ErrorBehavior"] == 0


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_companion_osc_darks_are_gated(tmp_path, gate_on):
    cfg = _gate_cfg(tmp_path, piggyback_enabled=True,
                    piggyback_dark_exposures="120")
    root = json.loads(calibration.generate_piggyback_companion_json(
        cq.rig_view(cfg, "piggyback"), has_safety=True))
    darks = next(n for n in _walk(root)
                 if str(n.get("Name", "")).startswith("OSC_DARKS"))
    first = darks["Items"]["$values"][0]
    assert "cooler-gate" in str(first.get("Script", "")).lower()
    assert first["ErrorBehavior"] == 1


def test_rc16_unsafe_darks_wrapped_behind_gate(tmp_path, monkeypatch, gate_on):
    """The gate sits in its own DARKS_AT_SETPOINT container inside the
    unsafe branch, so a skip never skips the wait-for-safe after it."""
    from photonscript.scheduler import nina_sequence_json as nsj
    cfg = _gate_cfg(tmp_path)
    monkeypatch.setattr(nsj, "_gen_cfg", lambda: cfg)
    src = open(nsj.__file__, encoding="utf-8").read()
    i = src.index("night_dark_blocks = _dark_quota_blocks(")
    j = src.index("_wait_until_safe(),", i)
    assert "DARKS_AT_SETPOINT_NAME, dark_gate + night_dark_blocks" in src[i:j]
    assert nsj._dark_quota_blocks("DawnProvider", 0)   # 600 / 180 owed here


# ---- flats ------------------------------------------------------------------

def test_flats_reset_date_parsing(tmp_path):
    cfg = _cfg(tmp_path, calibration_flats_reset=
               "rc16:2026-10-01, piggy:2026-09-30,bogus")
    assert co.flats_reset_date(cfg, "rc16") == "2026-10-01"
    assert co.flats_reset_date(cfg, "piggyback") == "2026-09-30"
    both = _cfg(tmp_path, calibration_flats_reset="2026-09-15")
    assert co.flats_reset_date(both, "rc16") == "2026-09-15"
    assert co.flats_reset_date(_cfg(tmp_path), "rc16") is None


def test_flats_before_optics_change_are_owed(tmp_path):
    cfg, *_ = _piggy_setup(tmp_path, calibration_flats_reset=f"piggyback:{_ago(3)}")
    r = co.owed_report(cfg, "piggyback", projects=[_m31()])["rigs"][0]
    f = r["flats"][0]
    assert f["stale"] and f["optics_reset"] == _ago(3)
    assert any("optics change" in x for x in f["reasons"])


def _health(by_bucket):
    return {"FLAT": {"by_bucket": by_bucket}}


def test_stale_flat_filters_as_used_and_ordered(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    bb = {"Ha": {"date": _ago(50), "age_days": 50},
          "OIII": {"date": _ago(80), "age_days": 80},
          "L": {"date": _ago(10), "age_days": 10},
          "R": {"date": _ago(90), "age_days": 90}}
    monkeypatch.setattr(calibration, "calibration_health", lambda c: _health(bb))
    monkeypatch.setattr(co, "used_filters", lambda c, rig="rc16": {"Ha", "OIII", "SII", "L"})
    # SII has no flats at all: first; then oldest; R unused; L fresh
    assert calibration.stale_flat_filters(cfg) == ["SII", "OIII", "Ha"]
    monkeypatch.setattr(co, "used_filters", lambda c, rig="rc16": set())
    assert set(calibration.stale_flat_filters(cfg)) == {"SII", "OIII", "Ha",
                                                        "R", "G", "B"}
    reset = _cfg(tmp_path, calibration_flats_reset=f"rc16:{_ago(5)}",
                 calibration_flats_as_used=False)
    assert "L" in calibration.stale_flat_filters(reset)


def test_dawn_flats_capped_per_morning(monkeypatch):
    from datetime import datetime
    from unittest.mock import patch
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.shared.astronomy import get_seasonal_targets
    from photonscript.scheduler import nina_sequence_json as nsj
    from photonscript.scheduler.target_planner import (
        create_project_from_target, plan_night_sequence)
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    monkeypatch.setenv("PS_CALIBRATION_DAWN_FLAT_EXTRA_MAX", "1")
    config = PhotonScriptConfig()
    monkeypatch.setattr(nsj, "_gen_cfg", lambda: config)
    projects = [create_project_from_target(t) for t in get_seasonal_targets(7)]
    targets = plan_night_sequence(projects, config, datetime.utcnow())[:2]
    for t in targets:
        t.start_guiding = False
    seq = build_sequence_for_night("flats_cap", targets)
    tonight = {e.filter_type.value for t in targets for e in t.exposures}
    extra = [f for f in ("L", "R", "G", "B", "Ha", "OIII", "SII")
             if f not in tonight]
    with patch("photonscript.scheduler.calibration.stale_flat_filters",
               return_value=extra):
        txt = nsj.generate_nina_json(seq)
    assert txt.count('"NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat') \
        == len(tonight) + min(1, len(extra))


# ---- report columns, plan, morning line -------------------------------------

def test_dark_age_and_sensor_temperature(tmp_path):
    cfg = _cfg(tmp_path, piggyback_enabled=True,
               piggyback_dark_exposures="120")
    store_frames(cfg, "piggyback", [
        ("DARK", _ago(20), 3, {"exptime": 120.0, "ccdtemp": 0.4}),
        ("DARK", _ago(4), 2, {"exptime": 120.0, "ccdtemp": -0.2}),
    ])
    r = co.owed_report(cfg, "piggyback", projects=[])["rigs"][0]
    d = _by_exp(r)[120.0]
    assert d["newest"] == _ago(4) and d["age_days"] == 4
    assert d["ccd_temp_c"] == 0.4 and d["ccd_temp_max_off_c"] == 0.4
    assert d["at_setpoint"]


def test_plan_and_morning_note(tmp_path):
    cfg, *_ = _piggy_setup(tmp_path)
    rep = co.owed_report(cfg, "piggyback", projects=[_m31()])
    plan = rep["rigs"][0]["plan"]
    assert "300 s" in plan["text"][0] and "(from lights)" in plan["text"][0]
    assert "OSC set every safe dawn" in plan["text"][1]
    note = co.morning_note(cfg, rep)
    assert note.startswith("Calibration owed: Piggy-600:")
    assert "darks 400 s 0/30" in note and "flats OSC" in note
    note.encode("ascii")
    assert "plan:" in co.format_report(rep)


def test_rc16_plan_caps_owed_flats(tmp_path):
    cfg = _cfg(tmp_path, calibration_dawn_flat_extra_max=1)
    planned = [{"filter": "Ha"}]
    flats = [{"filter": f, "owed": True, "last": last, "age_days": age,
              "light_nights": ["n"]}
             for f, last, age in (("L", "x", 90), ("SII", None, None),
                                  ("Ha", "x", 50), ("R", "x", 60))]
    p = co._plan(cfg, "rc16", [], flats, planned)
    assert p["dawn_flats_tonight"] == ["Ha"] and p["dawn_flats_extra"] == ["SII"]
    assert p["flats_owed_later"] == ["L", "R"]


def test_new_config_keys_on_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    from photonscript.shared.config import PhotonScriptConfig
    c = PhotonScriptConfig(_env_file=None)
    names = {f[0] for f in _CONFIG_FIELDS}
    for k, v in (("calibration_darks_follow_lights", True),
                 ("calibration_darks_follow_lights_max", 2),
                 ("calibration_darks_gated", True),
                 ("calibration_flats_as_used", True),
                 ("calibration_dawn_flat_extra_max", 3),
                 ("calibration_flats_reset", "")):
        assert getattr(c, k) == v and k in names


def test_calibration_page_shows_plan_and_dark_age():
    from pathlib import Path
    p = (Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
         / "templates" / "calibration.html")
    s = p.read_text(encoding="utf-8")
    i = s.index("function owedCard")
    s[i:s.index("async function loadOwed", i)].encode("ascii")
    assert "(r.plan || {}).text" in s and "<th>newest</th><th>sensor</th>" in s
