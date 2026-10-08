"""PS-112: the planner keeps an HDR plan while either set is owed.

plan_night_sequence kept a plan only when long subs were still owed, so an
HDR target whose long set was complete never got its remaining short subs
scheduled, and _scale_group bumped a long count of 0 to 1. Now a plan stays
while its long set OR its short set is owed, tonight's copy carries only
what is owed (PS-105), and the night plan, plan snapshot and slewing text
show the shorts.
"""
import json
from datetime import datetime

from photonscript.scheduler import target_planner
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.nina_sequence_json import generate_nina_json
from photonscript.scheduler.target_planner import (
    _fit_by_moon, _scale_group, owed_seconds, plan_night_sequence,
    remaining_copy)
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


def _hdr(f=FilterType.HA, count=14, acquired=14, short_acq=5, short_n=12):
    return ExposurePlan(filter_type=f, exposure_seconds=600, count=count,
                        acquired=acquired, gain=200, offset=256,
                        hdr_short_seconds=60.0, hdr_short_count=short_n,
                        hdr_short_acquired=short_acq)


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
    seq = build_sequence_for_night("PS112", targets)
    seq.wait_until_local = "21:00:00"
    return json.loads(generate_nina_json(seq))


# ---- helpers ------------------------------------------------------------------

def test_scale_group_leaves_a_done_long_set_at_zero():
    done = remaining_copy(_hdr())                       # 0 long owed, 7 shorts
    owing = remaining_copy(_hdr(FilterType.OIII, acquired=0))   # 14 long owed
    _scale_group([done, owing], 600 * 4)
    assert done.count == 0
    assert owing.count == 4
    assert done.short_remaining() == 7                  # shorts untouched


def test_fit_by_moon_keeps_a_shorts_only_plan_at_zero_long():
    for tag in (None, "NB", "BB", "NB+OIII"):
        c = remaining_copy(_hdr())
        out = _fit_by_moon([c], 10.0, tag)
        assert [(e.count, e.short_remaining()) for e in out] == [(0, 7)]


def test_owed_seconds_counts_both_sets():
    assert owed_seconds(remaining_copy(_hdr())) == 7 * 60
    assert owed_seconds(remaining_copy(_hdr(acquired=4))) == 10 * 600 + 7 * 60


# ---- planner -> generator ----------------------------------------------------

def test_long_done_shorts_owed_schedules_only_the_shorts(tmp_path):
    # the ticket's case: 14/14 long done, 5 of 12 shorts done
    targets = plan_night_sequence([_project(_hdr())], _cfg(tmp_path), NIGHT)
    assert [t.name for t in targets] == ["Andromeda Galaxy"]
    e = targets[0].exposures[0]
    assert (e.count, e.acquired, e.short_remaining()) == (0, 0, 7)
    for t in targets:
        t.start_guiding = True
    data = _sequence(targets)
    assert _light_blocks(data) == [(60.0, 7)]           # 7 shorts, no long subs
    texts = [d.get("Message", "") for d in _walk(data) if isinstance(d, dict)]
    slew = next(m for m in texts if "Andromeda Galaxy: slewing" in m)
    assert f"Ha{X}7@60s" in slew and "@600s" not in slew


def _other():
    # an owing second goal, so an empty night does not fall back to the
    # seasonal catalog
    return ImagingProject(
        id="b", priority=10,
        target=CelestialTarget(name="Other", catalog_id="X", ra_hours=0.9,
                               dec_degrees=40.0, object_type="galaxy"),
        exposure_plans=[ExposurePlan(filter_type=FilterType.HA,
                                     exposure_seconds=60, count=5)])


def test_both_sets_done_drops_the_plan(tmp_path):
    p = _hdr(short_acq=12)
    targets = plan_night_sequence([_project(p), _other()], _cfg(tmp_path), NIGHT)
    assert [t.name for t in targets] == ["Other"]


def test_non_hdr_done_plan_still_dropped(tmp_path):
    p = ExposurePlan(filter_type=FilterType.HA, exposure_seconds=600, count=14,
                     acquired=14, gain=200, offset=256)
    targets = plan_night_sequence([_project(p), _other()], _cfg(tmp_path), NIGHT)
    assert [t.name for t in targets] == ["Other"]


def test_done_long_set_beside_owed_shorts_in_one_project(tmp_path):
    # the long set alone done on a project that is not 100 percent: the plan
    # stays for its shorts, never re-emits a long sub
    p = _hdr()
    proj = _project(p)
    proj.compute_completion()
    assert proj.completion_pct < 100
    targets = plan_night_sequence([proj, _other()], _cfg(tmp_path), NIGHT)
    andromeda = next(t for t in targets if t.name == "Andromeda Galaxy")
    assert [(e.count, e.short_remaining()) for e in andromeda.exposures] == [(0, 7)]


def test_mixed_filters_one_done_one_owing(tmp_path):
    proj = _project(_hdr(), _hdr(FilterType.OIII, acquired=10, short_acq=12))
    targets = plan_night_sequence([proj], _cfg(tmp_path), NIGHT)
    got = {e.filter_type.value: (e.count, e.short_remaining())
           for e in targets[0].exposures}
    assert got == {"Ha": (0, 7), "OIII": (4, 0)}
    for t in targets:
        t.start_guiding = True
    assert sorted(_light_blocks(_sequence(targets))) == [(60.0, 7), (600.0, 4)]


def test_short_time_is_charged_to_the_night(tmp_path, monkeypatch):
    # alloc_time includes the shorts, so the next target sees less night left
    real = target_planner._fit_by_moon
    other = ImagingProject(
        id="b", priority=10,
        target=CelestialTarget(name="Other", catalog_id="X", ra_hours=0.9,
                               dec_degrees=40.0, object_type="galaxy"),
        exposure_plans=[ExposurePlan(filter_type=FilterType.HA,
                                     exposure_seconds=60, count=5)])

    def second_budget(short_acq):
        calls = []

        def spy(exposures, available_seconds, moon_tag):
            calls.append(available_seconds)
            return real(exposures, available_seconds, moon_tag)

        monkeypatch.setattr(target_planner, "_fit_by_moon", spy)
        big = _hdr(acquired=4, short_n=200, short_acq=short_acq)
        plan_night_sequence([_project(big), other], _cfg(tmp_path), NIGHT)
        assert len(calls) == 2
        return calls[1]

    # 200 x 60 s shorts owed vs none owed: the other target's budget drops
    assert second_budget(0) < second_budget(200) - 3600


# ---- night plan text and plan snapshot -----------------------------------------

class _Store:
    def __init__(self, projects):
        self.projects = {p.id: p for p in projects}


def test_night_plan_shows_the_short_set(tmp_path, monkeypatch):
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler.night_plan import build_night_plan
    proj = _project(_hdr(), _hdr(FilterType.OIII, acquired=10, short_acq=12))
    monkeypatch.setattr(app_mod, "_store", _Store([proj]))
    monkeypatch.setattr(target_planner, "plan_night_sequence",
                        lambda projects, config, now: _REAL_PLANNER(
                            projects, config, NIGHT))
    plan = build_night_plan(_cfg(tmp_path))
    card = plan["schedule"][0]
    assert card["filters"] == [
        {"f": "Ha", "exp": 60, "n": 7, "short": True},
        {"f": "OIII", "exp": 600, "n": 4}]
    assert plan["stats"]["planned_subs"] == 11
    assert plan["stats"]["est_integration_h"] == round((7 * 60 + 4 * 600) / 3600, 1)
    ev = next(e for e in plan["events"] if e["event"] == "Target: Andromeda Galaxy")
    assert f"Ha{X}7@60s HDR short" in ev["detail"]
    assert f"OIII{X}4@600s" in ev["detail"]
    assert f"Ha{X}0" not in ev["detail"]


def test_plan_snapshot_carries_the_short_row(tmp_path):
    from photonscript.scheduler.runs import save_plan_snapshot
    from photonscript.shared.models import NinaSequenceTarget
    t = NinaSequenceTarget(name="Andromeda Galaxy", ra_hours=0.712,
                           dec_degrees=41.27,
                           exposures=[remaining_copy(_hdr(acquired=10))])
    cfg = _cfg(tmp_path)
    save_plan_snapshot(cfg, "2026-10-03", {}, [t])
    snap = json.loads((tmp_path / "data" / "runs" / "2026-10-03_plan.json")
                      .read_text())
    assert snap["targets"][0]["exposures"] == [
        {"filter": "Ha", "exp_s": 600, "planned": 4},
        {"filter": "Ha", "exp_s": 60.0, "planned": 7, "hdr_short": True}]
