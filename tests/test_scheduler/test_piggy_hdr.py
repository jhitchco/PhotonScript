"""Piggy HDR short set (2026-10-28): the existing per-goal HDR mechanism
(hdr {"L": 30} on the RC16) extended to the Piggy-600 OSC plan.

Measured on the Piggy-600 (IMX571, gain 100): the read-noise penalty is
under 1% at 60 s, so longer subs add nothing, while star clipping grows
with length (2% of stars at 60 s, 9.8% at 180 s). Bright-core targets keep
120 s main subs and add about 10% of the time in 15 s shorts:

* hdr {"OSC": 15} + hdr_fraction 0.10 size the goal's OSC plan
  (project_store.osc_plan), the seed can declare a Piggy-only goal with
  osc_hours (M42);
* piggy_length.plan carries the short set per target and the companion's
  light loop shoots blocks of 10 x 120 s then 9 x 15 s;
* the 15 s darks are owed and filled with the others."""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from photonscript.scheduler import calibration as cal
from photonscript.scheduler import calibration_owed as co
from photonscript.scheduler import piggy_length as pl
from photonscript.scheduler.project_store import (ProjectStore, clean_hdr_fraction,
                                                  osc_plan, osc_plan_hours)
from photonscript.scheduler.sideload import lint_companion
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget, FilterType
from photonscript.shared.rigs import PIGGYBACK, rig_config

M42 = CelestialTarget(name="Orion Nebula", catalog_id="M 42", ra_hours=5.588056,
                      dec_degrees=-5.383333, object_type="emission nebula")
HANDOFF = datetime(2026, 10, 29, 9, 0)


def _cfg(tmp_path, **kw):
    (tmp_path / "data").mkdir(exist_ok=True)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"),
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _store(tmp_path, monkeypatch, **kw):
    monkeypatch.setattr(ProjectStore, "SEED_PATH", tmp_path / "no_seed.json")
    return ProjectStore(_cfg(tmp_path, **kw))


def _osc(p):
    return next(e for e in p.exposure_plans if e.rig == "piggyback")


# ----------------------------------------------------------------- the goal

def test_osc_plan_short_set_holds_the_fraction(tmp_path):
    cfg = _cfg(tmp_path)
    e = osc_plan(8.0, cfg, exp_s=120.0, short_s=15.0, fraction=0.10)
    assert (e.count, e.exposure_seconds) == (216, 120.0)
    assert (e.hdr_short_count, e.hdr_short_seconds) == (192, 15.0)
    assert osc_plan_hours(e) == pytest.approx(8.0)
    # the config default fraction; no short set without a short length
    e = osc_plan(8.0, _cfg(tmp_path, piggyback_hdr_fraction=0.2), exp_s=120.0, short_s=15.0)
    assert e.hdr_short_count == 384 and e.count == 192
    e = osc_plan(8.0, cfg, exp_s=120.0)
    assert e.hdr_short_count == 0 and e.hdr_short_seconds is None and e.count == 240
    # a "short" length at or over the main length is no short set
    assert osc_plan(8.0, cfg, exp_s=120.0, short_s=120.0).hdr_short_count == 0


def test_clean_hdr_fraction():
    assert clean_hdr_fraction(None) is None and clean_hdr_fraction("") is None
    assert clean_hdr_fraction("0.1") == 0.1
    for bad in (0, -0.1, 0.6, "x"):
        with pytest.raises(ValueError):
            clean_hdr_fraction(bad)


def test_patch_osc_hdr_rebuilds_only_the_osc_plan(tmp_path, monkeypatch):
    store = _store(tmp_path, monkeypatch)
    p = store.add_from_target(M42, budget_hours=6.0)      # RC16 Ha/OIII/SII
    store.update(p.id, osc_hours=8.0, exposure_overrides={"OSC": 120})
    rc_before = [(e.filter_type.value, e.count, e.exposure_seconds)
                 for e in p.exposure_plans if e.rig == "rc16"]
    osc = _osc(p)
    osc.acquired_s, osc.acquired = 1200.0, 10
    store.update(p.id, hdr={"OSC": 15}, hdr_fraction=0.10)
    osc = _osc(p)
    assert (osc.count, osc.hdr_short_seconds, osc.hdr_short_count) == (216, 15.0, 192)
    assert osc.acquired_s == 1200.0                       # accepted seconds carried
    assert [(e.filter_type.value, e.count, e.exposure_seconds)
            for e in p.exposure_plans if e.rig == "rc16"] == rc_before
    # accepted shorts kept on a fraction change at the same short length
    osc.hdr_short_acquired = 7
    store.update(p.id, hdr_fraction=0.2)
    osc = _osc(p)
    assert osc.hdr_short_count == 384 and osc.hdr_short_acquired == 7
    # credit: a 15 s sub counts as a short, a 120 s one as main
    assert store.record_accepted_sub("Orion Nebula", "OSC", 15.0, rig="piggyback")
    assert _osc(p).hdr_short_acquired == 8
    # removing the OSC key drops the short set, same hours
    store.update(p.id, hdr={})
    osc = _osc(p)
    assert osc.hdr_short_count == 0 and osc.count == 240
    with pytest.raises(ValueError):
        store.update(p.id, hdr_fraction=0.9)


def test_seed_adds_m42_piggy_hdr_goal(tmp_path):
    store = ProjectStore(_cfg(tmp_path))            # the committed seed file
    m42 = next(p for p in store.projects.values() if p.target.catalog_id == "M 42")
    assert m42.target.name == "Orion Nebula" and m42.driving_rig == "piggyback"
    assert m42.active and m42.hdr == {"OSC": 15} and m42.hdr_fraction == 0.10
    (e,) = m42.exposure_plans
    assert (e.rig, e.filter_type, e.exposure_seconds, e.count) == ("piggyback", FilterType.OSC, 120.0, 216)
    assert (e.hdr_short_seconds, e.hdr_short_count) == (15.0, 192)
    assert m42.budget_hours == 8.0
    assert abs(m42.target.ra_hours - (5 + 35 / 60 + 17 / 3600)) < 1e-4
    assert abs(m42.target.dec_degrees + (5 + 23 / 60)) < 1e-4


# ----------------------------------------------------------------- the plan

def _t(name, handoff=None):
    return SimpleNamespace(name=name, handoff_utc=handoff)


def _m42_goal(cfg, short_acquired=0):
    e = osc_plan(8.0, cfg, exp_s=120.0, short_s=15.0, fraction=0.10)
    e.hdr_short_acquired = short_acquired
    return SimpleNamespace(target=SimpleNamespace(name="Orion Nebula"),
                           exposure_overrides={"OSC": 120}, exposure_plans=[e],
                           hdr={"OSC": 15}, hdr_fraction=0.10)


def test_hdr_block():
    assert pl.hdr_block(120, 15, 0.10, 20) == (10, 9)
    n_main, n_short = pl.hdr_block(120, 10, 0.10, 20)
    assert (n_main, n_short) == (10, 13)
    assert pl.hdr_block(600, 15, 0.10, 5) == (1, 4)       # never below one each


def test_plan_carries_the_short_set(tmp_path):
    cfg = _cfg(tmp_path)
    rec = pl.plan(cfg, [_t("Andromeda Galaxy", HANDOFF), _t("Orion Nebula")],
                  [SimpleNamespace(target=SimpleNamespace(name="Andromeda Galaxy"),
                                   exposure_overrides=None), _m42_goal(cfg)])
    assert rec["changed"]
    assert [(s["exp_s"], s.get("hdr")) for s in rec["segments"]] == [
        (120.0, None), (120.0, {"short_s": 15.0, "fraction": 0.1})]
    kw = pl.companion_kwargs(rec)
    assert kw["segments"][1]["hdr"]["short_s"] == 15.0
    assert "Orion Nebula 120 s + HDR 15 s (10%)" in pl.summary(rec)
    # one target alone still goes as a segment (the HDR loop)
    rec = pl.plan(cfg, [_t("Orion Nebula")], [_m42_goal(cfg)])
    assert len(rec["segments"]) == 1 and pl.companion_kwargs(rec)["segments"][0]["hdr"]
    # shorts all accepted: plain 120 s, nothing changes
    rec = pl.plan(cfg, [_t("Orion Nebula")], [_m42_goal(cfg, short_acquired=192)])
    assert not rec["changed"] and pl.companion_kwargs(rec) == {}
    # the night pilot's ladder keeps the short set while above it
    rec = pl.plan(cfg, [_t("Orion Nebula")], [_m42_goal(cfg)])
    r2, s = pl.with_target_length(rec, "Orion Nebula", 60.0, 120.0)
    assert s == 60.0 and r2["segments"][0]["hdr"]["short_s"] == 15.0


def _walk(n):
    if isinstance(n, dict):
        yield n
        for v in n.values():
            yield from _walk(v)
    elif isinstance(n, list):
        for v in n:
            yield from _walk(v)


@pytest.mark.parametrize("gate", [False, True])
def test_companion_hdr_light_loop(tmp_path, gate):
    kw = {}
    if gate:
        script = tmp_path / "settle-gate.cmd"
        script.write_text("@echo off\n")
        kw.update(piggyback_settle_gate=True, piggyback_settle_script=str(script))
    else:
        kw.update(piggyback_settle_gate=False)
    cfg = _cfg(tmp_path, **kw)
    segs = [{"exp_s": 120.0, "until_utc": None, "targets": ["Orion Nebula"],
             "hdr": {"short_s": 15.0, "fraction": 0.10}}]
    seq = json.loads(cal.generate_piggyback_companion_json(
        rig_config(cfg, PIGGYBACK), has_safety=True, with_lights=True, osc_segments=segs))
    loops = [d for d in _walk(seq) if d.get("Name") == cal.OSC_LIGHT_LOOP_NAME]
    assert len(loops) == 1
    lights = [d["ExposureTime"] for d in loops[0]["Items"]["$values"]
              if "TakeExposure" in d.get("$type", "")]
    assert lights == [120.0] * 10 + [15.0] * 9
    assert all(d["ImageType"] == "LIGHT" for d in loops[0]["Items"]["$values"]
               if "TakeExposure" in d.get("$type", ""))
    res = lint_companion(seq, settle_gate=gate)
    assert res.ok, [(f.rule, f.detail) for f in res.findings if f.level == "ERROR"]
    notice = [str(d.get("Message") or d.get("Text")) for d in _walk(seq)
              if "roof open" in str(d.get("Message") or d.get("Text") or "")]
    assert notice and "120+15s" in notice[0]


def test_light_lengths_of():
    segs = [{"exp_s": 120.0, "hdr": {"short_s": 15.0, "fraction": 0.1}}, {"exp_s": 450.0},
            {"exp_s": 120.0}]
    assert cal.light_lengths_of(segs) == [120.0, 15.0, 450.0]
    assert cal.segments_label(segs) == "120+15/450/120s"


# ----------------------------------------------------------------- darks owed

def test_15s_darks_owed_and_in_the_night_quota(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    goals = [SimpleNamespace(active=True, target=SimpleNamespace(name="Orion Nebula"),
                             exposure_plans=[osc_plan(8.0, cfg, exp_s=120.0, short_s=15.0,
                                                      fraction=0.1)])]
    monkeypatch.setattr(co, "_active_projects", lambda config: goals)
    g = co.goal_hdr_dark_lengths(cfg, "piggyback")
    assert g == [{"exp_s": 15.0, "label": "goal Orion Nebula OSC HDR short (15 s)"}]
    assert co.goal_hdr_dark_lengths(cfg, "rc16") == []
    pcfg = rig_config(cfg, PIGGYBACK)
    assert 15.0 in cal.night_dark_exposures(pcfg, "piggyback")
    rep = co.owed_report(cfg, "piggyback", projects=goals)
    (rig,) = [r for r in rep["rigs"] if r["rig"] == "piggyback"]
    (d15,) = [d for d in rig["darks"] if abs(d["exp_s"] - 15.0) < 0.5]
    assert d15["from_goal_hdr"] and d15["owed"] > 0 and d15["have"] == 0
    assert "goal Orion Nebula OSC HDR short (15 s)" in d15["planned"]
