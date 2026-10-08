"""PS-105: the planner is the one owner of "remaining".

plan_night_sequence used to copy each plan with count = count - acquired but
keep `acquired`, and the sequence generator (plus the plan snapshot and the
night plan text) subtracted `acquired` again: 30 planned with 10 accepted
shot 10, and a project at least half done dropped out of the night. The
copy now carries only what is owed, with every acquired field at 0.
"""
import json
from datetime import datetime

import pytest

from photonscript.scheduler import target_planner
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.nina_sequence_json import generate_nina_json
from photonscript.scheduler.target_planner import (
    cap_unguided, plan_night_sequence, remaining_copy)
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (
    CelestialTarget, ExposurePlan, FilterType, ImagingProject)

M31 = CelestialTarget(name="Andromeda Galaxy", catalog_id="M 31",
                      ra_hours=0.712, dec_degrees=41.27, object_type="galaxy")
NIGHT = datetime(2026, 10, 2, 20)   # M31 up most of the night from AARO
_REAL_PLANNER = plan_night_sequence
X = chr(0xD7)   # the multiplication sign the plan text uses


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _plan(f=FilterType.HA, exp=60, count=30, acquired=0, **kw):
    return ExposurePlan(filter_type=f, exposure_seconds=exp, count=count,
                        acquired=acquired, gain=200, offset=256, **kw)


def _project(*plans):
    return ImagingProject(id="m31", target=M31, priority=90,
                          exposure_plans=list(plans))


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _light_blocks(data):
    """[(exposure s, LoopCondition iterations)] of every light SmartExposure."""
    out = []
    for sm in _walk(data):
        if not (isinstance(sm, dict) and "SmartExposure" in sm.get("$type", "")):
            continue
        te = next(d for d in _walk(sm) if isinstance(d, dict)
                  and "TakeExposure" in d.get("$type", ""))
        if te.get("ImageType") != "LIGHT":
            continue
        loop = next(d for d in _walk(sm) if isinstance(d, dict)
                    and "LoopCondition" in d.get("$type", ""))
        out.append((te["ExposureTime"], loop["Iterations"]))
    return out


def _sequence(targets):
    seq = build_sequence_for_night("PS105", targets)
    seq.wait_until_local = "21:00:00"
    return json.loads(generate_nina_json(seq))


# ---- the copy ---------------------------------------------------------------

@pytest.mark.parametrize("acquired,owed", [(0, 30), (10, 20), (15, 15),
                                           (29, 1), (30, 0)])
def test_remaining_copy_carries_only_what_is_owed(acquired, owed):
    p = _plan(count=30, acquired=acquired)
    c = remaining_copy(p)
    assert (c.count, c.acquired, c.acquired_s) == (owed, 0, 0.0)
    assert c.count - c.acquired == owed          # every consumer's arithmetic
    assert (p.count, p.acquired) == (30, acquired)   # the project is untouched


def test_remaining_copy_hdr_short_set_partly_done():
    p = _plan(exp=600, count=14, acquired=4, hdr_short_seconds=60.0,
              hdr_short_count=12, hdr_short_acquired=5)
    c = remaining_copy(p)
    assert (c.count, c.acquired) == (10, 0)
    assert (c.hdr_short_count, c.hdr_short_acquired, c.short_remaining()) == (7, 0, 7)
    assert c.hdr_short_seconds == 60.0
    assert p.short_remaining() == 7              # unchanged on the project


# ---- planner -> generator ---------------------------------------------------

@pytest.mark.parametrize("acquired,owed", [(10, 20), (15, 15), (29, 1)])
def test_partly_done_project_schedules_its_full_remainder(tmp_path, acquired, owed):
    targets = plan_night_sequence([_project(_plan(count=30, acquired=acquired))],
                                  _cfg(tmp_path), NIGHT)
    assert [t.name for t in targets] == ["Andromeda Galaxy"]
    e = targets[0].exposures[0]
    assert (e.count, e.acquired) == (owed, 0)
    data = _sequence(targets)
    assert _light_blocks(data) == [(60.0, owed)]
    # plan_desc in the slewing notification shows the owed count too
    texts = [d.get("Message", "") for d in _walk(data) if isinstance(d, dict)]
    assert any(f"Ha{X}{owed}@60s" in m for m in texts)


def test_hdr_short_set_partly_done_through_the_generator(tmp_path):
    p = _plan(exp=600, count=14, acquired=4, hdr_short_seconds=60.0,
              hdr_short_count=12, hdr_short_acquired=5)
    targets = plan_night_sequence([_project(p)], _cfg(tmp_path), NIGHT)
    for t in targets:
        t.start_guiding = True
    assert sorted(_light_blocks(_sequence(targets))) == [(60.0, 7), (600.0, 10)]


def test_unguided_capped_target_still_owes_the_same_integration(tmp_path):
    # PS-66 cap: 30 x 600 s planned, 10 accepted -> 20 x 600 s = 12000 s owed,
    # shot as 40 x 300 s. Before PS-105 the cap saw 20 - 10 = 10 owed (20 x 300 s)
    # and a half-done unguided target was not capped and then dropped.
    for acquired, n300 in ((10, 40), (15, 30)):
        targets = plan_night_sequence(
            [_project(_plan(exp=600, count=30, acquired=acquired))],
            _cfg(tmp_path), NIGHT)
        for t in targets:
            t.start_guiding = False
        cap_unguided(targets, 300)
        e = targets[0].exposures[0]
        assert (e.exposure_seconds, e.count, e.acquired, e.acquired_s) == (
            300, n300, 0, 0.0)
        assert _light_blocks(_sequence(targets)) == [(300.0, n300)]


# ---- re-dispatch, plan snapshot, night plan text -----------------------------

class _Store:
    def __init__(self, projects):
        self.projects = {p.id: p for p in projects}


def test_dispatch_and_plan_snapshot_show_the_remainder(tmp_path, monkeypatch):
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler.armer import Armer
    proj = _project(_plan(count=30, acquired=15),
                    _plan(FilterType.OIII, count=30, acquired=10))
    monkeypatch.setattr(app_mod, "_store", _Store([proj]))
    monkeypatch.setattr(target_planner, "plan_night_sequence",
                        lambda projects, config, now: _REAL_PLANNER(
                            projects, config, NIGHT))
    monkeypatch.chdir(tmp_path)
    a = Armer(_cfg(tmp_path, guided_default=True))
    monkeypatch.setattr(a, "_calibration_slot", lambda targets, now: None)
    a.plan = {"night_of": "2026-10-03", "dusk_utc": "2026-10-04T01:30:00Z",
              "dawn_utc": "2026-10-04T11:30:00Z"}
    assert a._dispatch() is True
    seq = json.loads(a.sequence_path.read_text(encoding="utf-8"))
    assert sorted(_light_blocks(seq)) == [(60.0, 15), (60.0, 20)]
    snap = json.loads((tmp_path / "data" / "runs" / "2026-10-03_plan.json")
                      .read_text())
    planned = {e["filter"]: e["planned"] for e in snap["targets"][0]["exposures"]}
    assert planned == {"Ha": 15, "OIII": 20}

    # a mid-night re-dispatch after 5 more Ha subs only re-runs what is left
    proj.exposure_plans[0].acquired = 20
    assert a._dispatch() is True
    seq = json.loads(a.sequence_path.read_text(encoding="utf-8"))
    assert sorted(_light_blocks(seq)) == [(60.0, 10), (60.0, 20)]


def test_night_plan_text_shows_the_remainder(tmp_path, monkeypatch):
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler.night_plan import build_night_plan
    proj = _project(_plan(count=30, acquired=15),
                    _plan(FilterType.OIII, count=30, acquired=10))
    monkeypatch.setattr(app_mod, "_store", _Store([proj]))
    monkeypatch.setattr(target_planner, "plan_night_sequence",
                        lambda projects, config, now: _REAL_PLANNER(
                            projects, config, NIGHT))
    plan = build_night_plan(_cfg(tmp_path))
    card = plan["schedule"][0]
    assert {f["f"]: f["n"] for f in card["filters"]} == {"Ha": 15, "OIII": 20}
    assert plan["stats"]["planned_subs"] == 35
    ev = next(e for e in plan["events"] if e["event"] == "Target: Andromeda Galaxy")
    assert f"Ha{X}15@60s" in ev["detail"] and f"OIII{X}20@60s" in ev["detail"]
