"""2026-11-21: the RC16 idled from 09:36Z to dawn (12:26Z), 2.8 h, the same
on 11-20. plan_night_sequence walks the goals by priority and books the
night as one pool of hours, but the goals' visibility windows overlap:
M33 Core P1 (62), Pacman and Elephant Trunk (50) set early and used the pool
up, the walk stopped at remaining_hours <= 0.3, and Soul Nebula (45, up all
morning) was never looked at. The PS-180 windows then ended when the last
target set and nothing was up and planned after it.

Fix (target_planner.gap_fill): after the windows, every RC16 gap of 10 min
or more goes to the best goal up then that still owes RC16 time and passes
the walk's rules (moon, unguided narrowband, mosaic gate, paused tonight).

Live goals: GET /api/projects2 on 2026-11-21 (M33 Core P2 to P4 paused for
tonight, i.e. inactive). Moon: 92%, up at dusk, sets 11:42Z (night tag NB,
broadband not deferred: broadband-only targets only after moonset).
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import moon
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import target_planner as tp
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ImagingProject, NinaSequenceTarget

FIX = Path(__file__).parent / "fixtures" / "gap_fill" / "projects2_2026-11-21.json"
NOW = datetime(2026, 11, 21, 20, 0)            # 13:00 MST, before dusk
DUSK = datetime(2026, 11, 22, 1, 36)
DAWN = datetime(2026, 11, 22, 12, 26)
M33_SETS = datetime(2026, 11, 22, 9, 36)
MOONSET = datetime(2026, 11, 22, 11, 42, 9)
SOUL = "Soul Nebula"


def _cfg(tmp_path, **kw):
    # PS-270: the gap fill tests look at the goals alone; the drift test in
    # what they leave open is tested below with plan_gap_drift on
    kw.setdefault("plan_gap_drift", False)
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"),
                              observatory_elev=1250.0,
                              rc16_narrowband_requires_guided=False, **kw)


def _raw():
    return json.loads(FIX.read_text(encoding="utf-8"))


def _goals(raw=None, drop=(), only=None):
    return [ImagingProject(**p) for p in (raw or _raw())
            if p["target"]["name"] not in drop
            and (only is None or p["target"]["name"] in only)]


def _mw(**kw):
    mw = {"available": True, "down_at_dusk": False, "illum_pct": 92,
          "alt_at_dusk_deg": 44.0, "dusk_utc": DUSK, "dawn_utc": DAWN,
          "now_utc": NOW, "rise_local_hh": None, "rise_local_mm": None,
          "rise_utc": None, "set_utc": MOONSET, "set_local": "04:42"}
    mw.update(kw)
    return mw


def _night(monkeypatch, cfg, mw, tag="NB", free_h=0.7):
    monkeypatch.setattr(moon, "moon_window_tonight",
                        lambda config, now=None: dict(mw))
    monkeypatch.setattr(nsj, "_moon_window", lambda: dict(mw))
    monkeypatch.setattr(nsj, "_gen_cfg_cache", cfg)
    monkeypatch.setattr(moon, "night_moon", lambda *a, **k: {
        "illum_pct": mw["illum_pct"], "moon_free_h": free_h, "tag": tag})


@pytest.fixture(autouse=True)
def _clear_cache():
    moon._cache.clear()
    yield
    moon._cache.clear()


def _plan(cfg, goals):
    info = {}
    ts = tp.plan_night_sequence(goals, cfg, NOW, info=info, guided=False)
    return ts, info


def _near(a, b, minutes=5):
    return abs((a - b).total_seconds()) <= minutes * 60


def _no_overlap(ts):
    for a, b in zip(ts, ts[1:]):
        assert a.window_end_utc <= b.window_start_utc, (a.name, b.name)


# --- the live night ------------------------------------------------------------

def test_live_night_without_gap_fill_idles_after_m33(tmp_path, monkeypatch):
    """The bug as it ran: the last window ends when M33 Core sets."""
    cfg = _cfg(tmp_path, plan_gap_fill=False)
    _night(monkeypatch, cfg, _mw())
    ts, info = _plan(cfg, _goals())
    assert [t.name for t in ts] == ["Elephant Trunk vdB 142", "Pacman Nebula",
                                    "M33 Core P1"]
    last = ts[-1].window_end_utc
    assert _near(last, M33_SETS)
    assert (DAWN - last) > timedelta(hours=2, minutes=45)   # RC16 idle
    assert info["gap_fill"] == []


def test_live_night_soul_fills_the_tail(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _night(monkeypatch, cfg, _mw())
    ts, info = _plan(cfg, _goals())
    names = [t.name for t in ts]
    assert names == ["Elephant Trunk vdB 142", "Pacman Nebula", "M33 Core P1",
                     SOUL]
    _no_overlap(ts)
    m33, soul = ts[-2], ts[-1]
    # the earlier windows are untouched; M33 now hands the mount on
    assert _near(m33.window_end_utc, M33_SETS)
    assert m33.handoff_utc == m33.window_end_utc
    assert soul.window_start_utc == m33.window_end_utc
    # Soul until it drops under 30 deg (about 11:40Z); the last target keeps
    # the mount to the loop end
    assert soul.window_end_utc >= datetime(2026, 11, 22, 11, 30)
    assert soul.handoff_utc is None
    # narrowband only on the NB night, fitted to the window (15% overhead)
    assert soul.exposures
    assert {e.filter_type.value for e in soul.exposures} <= {"Ha", "OIII", "SII"}
    span = (soul.window_end_utc - soul.window_start_utc).total_seconds()
    assert sum(e.exposure_seconds * e.count for e in soul.exposures) <= span * 0.85
    rows = info["gap_fill"]
    assert rows[0]["target"] == SOUL
    assert rows[0]["gap_start_utc"].startswith("2026-11-22T09:36")
    assert rows[0]["gap_end_utc"].startswith("2026-11-22T12:26")
    assert rows[0]["priority"] == 45
    # what Soul leaves (it sets) has no goal that passes tonight's rules
    # (NGC 1514 is up for under 0.5 h of moonless time), and says so
    assert all(r["target"] is None and r["reason"] for r in rows[1:])
    open_h = sum(r["gap_h"] for r in rows[1:])
    assert open_h < 0.75
    assert info["gap_fill_note"].startswith("Gap fill: Soul Nebula 09:36-")


def test_live_night_sequence_runs_the_fill(tmp_path, monkeypatch):
    """The generator sees the fill: M33 Core P1 hands off at 09:36Z (02:36
    MST), the Soul Nebula container follows it."""
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    cfg = _cfg(tmp_path)
    _night(monkeypatch, cfg, _mw())
    ts, _ = _plan(cfg, _goals())
    doc = json.loads(nsj.generate_nina_json(build_sequence_for_night("t", ts)))
    found = []

    def walk(n):
        if isinstance(n, dict):
            if "DeepSkyObjectContainer" in str(n.get("$type", "")):
                conds = [c for c in (n.get("Conditions") or {}).get("$values", [])
                         if "TimeCondition" in str(c.get("$type", ""))]
                found.append((n.get("Name"), [(c.get("Hours"), c.get("Minutes"))
                                              for c in conds]))
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(doc)
    names = [n for n, _ in found]
    assert SOUL in names
    assert names.index(SOUL) == names.index("M33 Core P1") + 1
    m33 = dict(found)["M33 Core P1"]
    assert (2, 36) in m33


# --- the rules -----------------------------------------------------------------

def test_gap_without_candidate_stays_open(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _night(monkeypatch, cfg, _mw())
    ts, info = _plan(cfg, _goals(drop={SOUL}))
    assert [t.name for t in ts] == ["Elephant Trunk vdB 142", "Pacman Nebula",
                                    "M33 Core P1"]
    assert ts[-1].handoff_utc is None    # unchanged: keeps the mount
    rows = info["gap_fill"]
    assert len(rows) == 1 and rows[0]["target"] is None
    assert rows[0]["gap_start_utc"].startswith("2026-11-22T09:36")
    assert "no active goal" in rows[0]["reason"]
    assert "open" in info["gap_fill_note"]


def test_paused_tonight_goal_never_fills(tmp_path, monkeypatch):
    """A goal paused for tonight is inactive (tonight_pause); it is never
    the fill, even when it is the only goal up. M33 Core P2 to P4 are paused
    in the live goals too."""
    raw = _raw()
    for p in raw:
        if p["target"]["name"] == SOUL:
            p["active"], p["paused_tonight"] = False, True
    paused = {p["target"]["name"] for p in raw if p.get("paused_tonight")}
    assert {SOUL, "M33 Core P2", "M33 Core P3", "M33 Core P4"} <= paused
    cfg = _cfg(tmp_path)
    _night(monkeypatch, cfg, _mw())
    ts, info = _plan(cfg, _goals(raw))
    assert not paused & {t.name for t in ts}
    assert not paused & {r["target"] for r in info["gap_fill"]}
    assert info["gap_fill"] and info["gap_fill"][0]["target"] is None


def _soul_broadband(raw):
    for p in raw:
        if p["target"]["name"] == SOUL:
            p["exposure_plans"] = [
                {**p["exposure_plans"][0], "filter_type": f, "count": 30,
                 "acquired": 0, "acquired_s": 0.0, "exposure_seconds": 180.0}
                for f in ("L", "R", "G", "B")]
    return raw


def test_broadband_only_fill_waits_for_moonset(tmp_path, monkeypatch):
    """Soul as LRGB only: with the moon up until 11:42Z it is up for minutes
    of moonless time in the gap, so it is not the fill."""
    cfg = _cfg(tmp_path)
    _night(monkeypatch, cfg, _mw())
    ts, info = _plan(cfg, _goals(_soul_broadband(_raw())))
    assert SOUL not in {t.name for t in ts}
    assert all(r["target"] is None for r in info["gap_fill"])


def test_broadband_only_fill_moon_up_all_night(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _night(monkeypatch, cfg, _mw(set_utc=None, set_local=None), free_h=0.0)
    ts, info = _plan(cfg, _goals(_soul_broadband(_raw())))
    assert SOUL not in {t.name for t in ts}
    assert all(r["target"] is None for r in info["gap_fill"])


def test_broadband_only_fill_on_a_dark_night(tmp_path, monkeypatch):
    """The control: moon down all night, so LRGB Soul fills after M33 (the
    three narrowband goals still use the walk's pool up)."""
    cfg = _cfg(tmp_path)
    mw = _mw(illum_pct=3, down_at_dusk=True, set_utc=None, set_local=None,
             alt_at_dusk_deg=-30.0)
    _night(monkeypatch, cfg, mw, tag="BB", free_h=10.8)
    goals = _goals(_soul_broadband(_raw()),
                   only={"M33 Core P1", "Pacman Nebula",
                         "Elephant Trunk vdB 142", SOUL})
    ts, info = _plan(cfg, goals)
    by = {t.name: t for t in ts}
    assert SOUL in by, info
    assert ts[-1].name == SOUL
    assert {e.filter_type.value for e in by[SOUL].exposures} <= {"L", "R", "G", "B"}
    assert info["gap_fill"][0]["target"] == SOUL


# --- gap_fill unit ---------------------------------------------------------------

def _t(name, a, b):
    t = NinaSequenceTarget(name=name, ra_hours=1.0, dec_degrees=40.0,
                           exposures=[])
    t.window_start_utc, t.window_end_utc = a, b
    return t


def test_no_gap_leaves_the_plan_unchanged():
    a = _t("A", DUSK, datetime(2026, 11, 22, 6, 0))
    b = _t("B", datetime(2026, 11, 22, 6, 5), DAWN)   # 5 min apart: no gap
    b.handoff_utc = None
    a.handoff_utc = a.window_end_utc
    seq = [a, b]

    def consider(*_a, **_k):
        raise AssertionError("no gap, no candidate looked at")

    info = {"gap_fill": [], "gap_fill_note": None}
    out = tp.gap_fill(seq, [{"project": object(), "visibility": {}}], consider,
                      start=DUSK, end=DAWN, moon={}, promoted=False,
                      ha_first_by_name={}, skipped={}, info=info)
    assert out is seq
    assert [t.handoff_utc for t in out] == [a.window_end_utc, None]
    assert info == {"gap_fill": [], "gap_fill_note": None}


def test_no_gap_live_plan_unchanged(tmp_path, monkeypatch):
    """Integration: a night the windows already cover is the same with the
    gap fill on and off (Soul at a high priority is planned by the walk)."""
    raw = _raw()
    for p in raw:
        if p["target"]["name"] == SOUL:
            p["priority"] = 99
    plans = []
    for on in (True, False):
        moon._cache.clear()
        cfg = _cfg(tmp_path, plan_gap_fill=on)
        _night(monkeypatch, cfg, _mw())
        ts, info = _plan(cfg, _goals(raw))
        plans.append([(t.name, t.window_start_utc, t.window_end_utc,
                       t.handoff_utc, [(e.filter_type.value, e.count)
                                       for e in t.exposures]) for t in ts])
        if on:
            assert all(r["target"] is None for r in info["gap_fill"])
    assert plans[0] == plans[1]


# --- PS-270: a drift test in the gap the gap fill leaves open --------------------
#
# 2026-11-21: Soul Nebula fills 09:36-11:46Z, then 11:46-12:26Z (40 min) stays
# open and the RC16 idles to dawn. An open gap of plan_gap_drift_min_s (20
# min) or more now gets a short unguided L drift ladder: test data only.

DRIFT_GAP_START = datetime(2026, 11, 22, 11, 46)


def _drift_plan(tmp_path, monkeypatch, guided=False, **kw):
    cfg = _cfg(tmp_path, plan_gap_drift=True, **kw)
    _night(monkeypatch, cfg, _mw())
    info = {}
    ts = tp.plan_night_sequence(_goals(), cfg, NOW, info=info, guided=guided)
    return cfg, ts, info


def _drift_row(info):
    rows = [r for r in info["gap_fill"] if r.get("kind") == "drift_test"]
    assert len(rows) == 1, info["gap_fill"]
    return rows[0]


def test_drift_test_config_defaults():
    cfg = PhotonScriptConfig(_env_file=None)
    assert cfg.plan_gap_drift is True
    assert cfg.plan_gap_drift_min_s == 1200.0
    assert tp.gap_drift_exposures(cfg) == [30.0, 60.0, 120.0, 300.0]
    from photonscript.scheduler.app import _CONFIG_FIELDS as CONFIG_FIELDS
    keys = {row[0]: row[1] for row in CONFIG_FIELDS}
    assert keys["plan_gap_drift"] == "PS_PLAN_GAP_DRIFT"
    assert keys["plan_gap_drift_min_s"] == "PS_PLAN_GAP_DRIFT_MIN_S"
    assert keys["plan_gap_drift_exposures"] == "PS_PLAN_GAP_DRIFT_EXPOSURES"


def test_live_night_open_tail_becomes_a_drift_test(tmp_path, monkeypatch):
    from photonscript.scheduler.nina_sequence_json import (
        tracking_test_duration_s)
    from photonscript.scheduler.tracking_test import is_tracking_test
    from photonscript.shared.target_names import is_test_target
    _cfg_, ts, info = _drift_plan(tmp_path, monkeypatch)
    names = [t.name for t in ts]
    assert names[:4] == ["Elephant Trunk vdB 142", "Pacman Nebula",
                         "M33 Core P1", SOUL]
    assert len(ts) == 5
    _no_overlap(ts)
    soul, dt = ts[-2], ts[-1]
    assert dt.name.startswith("Tracking test Dawn (")
    assert is_tracking_test(dt.name) and is_test_target(dt.name)
    assert dt.tracking_test and not dt.start_guiding and dt.exposures == []
    assert dt.tracking_test_filters == ["L"]
    # the whole ladder fits 40 min with the dusk test's own overheads
    assert dt.tracking_test_exposures == [30.0, 60.0, 120.0, 300.0]
    assert dt.tracking_test_repeats == 3
    est = tracking_test_duration_s(1, dt.tracking_test_exposures, 3)
    gap_s = (dt.window_end_utc - dt.window_start_utc).total_seconds()
    assert _near(dt.window_start_utc, DRIFT_GAP_START, 1)
    assert _near(dt.window_end_utc, DAWN, 1)
    assert 39 * 60 < gap_s < 41 * 60
    assert est + tp.GAP_DRIFT_MARGIN_S <= gap_s
    # Soul now hands the mount on; the drift test is last and keeps it
    assert soul.handoff_utc == soul.window_end_utc == dt.window_start_utc
    assert dt.handoff_utc is None
    row = _drift_row(info)
    assert row["target"] == dt.name and "reason" not in row
    assert row["gap_start_utc"].startswith("2026-11-22T11:46")
    assert row["gap_end_utc"].startswith("2026-11-22T12:26")
    assert row["exposures"] == ["3x30s L", "3x60s L", "3x120s L", "3x300s L"]
    assert row["est_minutes"] == round(est / 60) <= 40
    # a field well up on the side the mount is already on (Soul is west of
    # the meridian at 11:46Z): no meridian flip inside the gap
    assert row["side"] == "west"
    assert row["ha_start_h"] > 0 and row["ha_end_h"] > 0
    assert min(row["alt_start_deg"], row["alt_end_deg"]) >= 35
    assert dt.name == f"Tracking test Dawn ({row['field']})"
    assert info["gap_fill_note"] == (
        "Gap fill: Soul Nebula 09:36-11:46Z; "
        f"11:46-12:26Z drift test ({row['field']})")
    assert [r["target"] for r in info["gap_fill"]][0] == SOUL


def test_drift_test_off_leaves_the_gap_open(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, plan_gap_drift=False)
    _night(monkeypatch, cfg, _mw())
    ts, info = _plan(cfg, _goals())
    assert ts[-1].name == SOUL and ts[-1].handoff_utc is None
    assert not any(t.tracking_test for t in ts)
    assert info["gap_fill"][-1]["target"] is None
    assert info["gap_fill_note"].endswith("11:46-12:26Z open")


def test_gap_shorter_than_min_stays_open(tmp_path, monkeypatch):
    _c, ts, info = _drift_plan(tmp_path, monkeypatch,
                               plan_gap_drift_min_s=3000)
    assert ts[-1].name == SOUL and ts[-1].handoff_utc is None
    assert not any(t.tracking_test for t in ts)
    row = info["gap_fill"][-1]
    assert row["target"] is None and row["reason"]
    assert "under 50 min" in row["drift_test"]
    assert info["gap_fill_note"].endswith("open")


def test_guided_night_gets_no_drift_test(tmp_path, monkeypatch):
    """The armed-guided watchdog would restart PHD2 inside the ladder."""
    _c, ts, info = _drift_plan(tmp_path, monkeypatch, guided=True)
    assert not any(t.tracking_test for t in ts)
    row = info["gap_fill"][-1]
    assert row["target"] is None and "guided" in row["drift_test"]


def test_drift_exposures_from_config(tmp_path, monkeypatch):
    _c, ts, info = _drift_plan(tmp_path, monkeypatch,
                               plan_gap_drift_exposures="120, 60,junk")
    dt = ts[-1]
    assert dt.tracking_test_exposures == [60.0, 120.0]
    assert dt.tracking_test_repeats == 3
    assert _drift_row(info)["exposures"] == ["3x60s L", "3x120s L"]


def test_fit_drift_ladder_drops_the_longest_first():
    full = [30.0, 60.0, 120.0, 300.0]
    assert tp.fit_drift_ladder(full, 2400)[:2] == (full, 3)
    assert tp.fit_drift_ladder(full, 1500)[:2] == (full, 1)
    # 1 each of the whole ladder (about 17 min) does not fit 15 min: the
    # 300 s length goes first
    assert tp.fit_drift_ladder(full, 900)[:2] == ([30.0, 60.0, 120.0], 1)
    assert tp.fit_drift_ladder(full, 300) is None
    for w in (600, 900, 1500, 2400):
        exps, rep, est = tp.fit_drift_ladder(full, w)
        assert 1 <= rep <= 3 and est + tp.GAP_DRIFT_MARGIN_S <= w


def test_drift_test_mid_night_gap_hands_off():
    """A gap between two targets: "Tracking test Gap HHMMZ", the target
    before it and the test both hand off at their window ends."""
    a = _t("A", DUSK, datetime(2026, 11, 22, 6, 0))
    b = _t("B", datetime(2026, 11, 22, 7, 0), DAWN)
    a.ra_hours, b.ra_hours = 3.0, 6.0
    a.handoff_utc, b.handoff_utc = a.window_end_utc, None
    info = {"gap_fill": [{
        "gap_start_utc": "2026-11-22T06:00:00Z",
        "gap_end_utc": "2026-11-22T07:00:00Z", "gap_h": 1.0,
        "target": None, "reason": "none"}]}
    cfg = PhotonScriptConfig(_env_file=None)
    out = tp.gap_drift([a, b], info, cfg, end=DAWN, guided=False)
    assert [t.name for t in out][0] == "A" and out[-1].name == "B"
    dt = out[1]
    assert dt.name.startswith("Tracking test Gap 0600Z")
    assert dt.handoff_utc == dt.window_end_utc == datetime(2026, 11, 22, 7, 0)
    assert a.handoff_utc == a.window_end_utc and b.handoff_utc is None
    assert info["gap_fill"][0]["kind"] == "drift_test"


def _walk(n):
    if isinstance(n, dict):
        yield n
        for k, v in n.items():
            if k != "Parent":
                yield from _walk(v)
    elif isinstance(n, list):
        for v in n:
            yield from _walk(v)


def _cls(d):
    return str(d.get("$type", "")).split(",")[0].split(".")[-1]


def test_drift_test_in_the_nina_sequence(tmp_path, monkeypatch):
    """The generated night runs the drift test as a DSO container after
    Soul (which now hands off at 04:46 MST): StopGuiding, L, the ladder
    30/60/120/300 s x 3, no StartGuiding; lint and the dispatch gate pass."""
    from photonscript.scheduler import dispatch_gate
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    from photonscript.scheduler.sequence_lint import lint
    _c, ts, _info = _drift_plan(tmp_path, monkeypatch)
    doc = json.loads(nsj.generate_nina_json(build_sequence_for_night("t", ts)))
    dsos = [d for d in _walk(doc) if _cls(d) == "DeepSkyObjectContainer"]
    names = [d["Name"] for d in dsos]
    drift = ts[-1].name
    assert names.index(drift) == names.index(SOUL) + 1
    box = dsos[names.index(drift)]
    soul = dsos[names.index(SOUL)]
    soul_times = [(c.get("Hours"), c.get("Minutes")) for c in _walk(soul)
                  if _cls(c) == "TimeCondition"]
    assert (4, 46) in soul_times
    assert any(_cls(d) == "StopGuiding" for d in _walk(box))
    assert not any(_cls(d) == "StartGuiding" for d in _walk(box))
    ladder = next(d for d in _walk(box)
                  if d.get("Name") == f"{drift} unguided ladder")
    smart = [d for d in _walk(ladder) if _cls(d) == "SmartExposure"]
    exps = [next(x["ExposureTime"] for x in _walk(s)
                 if _cls(x) == "TakeExposure") for s in smart]
    assert exps == [30.0, 60.0, 120.0, 300.0]
    for s in smart:
        loops = [c["Iterations"] for c in _walk(s) if _cls(c) == "LoopCondition"]
        assert loops[0] == 3
        flt = [x for x in _walk(s) if _cls(x) == "SwitchFilter"]
        assert flt and all(x["Filter"]["_name"] == "L" for x in flt)
        dith = [x for x in _walk(s) if _cls(x) == "DitherAfterExposures"]
        assert all(x.get("AfterExposures", 0) == 0 for x in dith)
    blob = json.dumps(box)
    assert "PS-270" in blob and "parks and holds until dawn" not in blob
    res = lint(doc, guided=False)
    assert not [f for f in res.findings if f.level == "ERROR"]
    warn = [f.detail for f in res.findings if f.rule == "tracking-test"]
    assert warn and "after tonight's targets" in warn[0]
    gate = dispatch_gate.check(doc, guided=False)
    assert gate["ok"], gate


def test_drift_container_honours_its_handoff(tmp_path, monkeypatch):
    """A mid-night drift test ends at its window end (outer container and
    ladder TimeCondition); the dusk test (no handoff) is unchanged."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(nsj, "_gen_cfg_cache", cfg)
    t = NinaSequenceTarget(name="Tracking test Gap 0600Z (M 36)", ra_hours=5.6,
                           dec_degrees=34.1, tracking_test=True,
                           tracking_test_filters=["L"],
                           tracking_test_exposures=[30, 60],
                           tracking_test_repeats=1,
                           tracking_test_note=tp.GAP_DRIFT_NOTE)
    plain = nsj._build_tracking_test_container(t, 30.0, None, None, None)
    assert not [c for c in _walk(plain) if _cls(c) == "TimeCondition"]
    t.handoff_utc = datetime(2026, 11, 22, 7, 0)   # 00:00 MST
    box = nsj._build_tracking_test_container(t, 30.0, None, None, None)
    outer = [c for c in box["Conditions"]["$values"] if _cls(c) == "TimeCondition"]
    assert [(c["Hours"], c["Minutes"]) for c in outer] == [(0, 0)]
    ladder = next(d for d in _walk(box)
                  if str(d.get("Name", "")).endswith(" unguided ladder"))
    assert any(_cls(c) == "TimeCondition"
               for c in ladder["Conditions"]["$values"])


def test_apply_guiding_keeps_a_tracking_test_unguided():
    a = NinaSequenceTarget(name="M 31", ra_hours=0.7, dec_degrees=41.3)
    d = NinaSequenceTarget(name="Tracking test Dawn (M 36)", ra_hours=5.6,
                           dec_degrees=34.1, tracking_test=True)
    tp.apply_guiding([a, d], True)
    assert a.start_guiding and not d.start_guiding


def _drift_recs(name):
    out = []
    for exp in (30, 60, 120, 300):
        for i in range(3):
            out.append({"rig": "rc16", "target": name, "filter": "L",
                        "exp_s": float(exp), "ecc": 0.40 + exp / 3000,
                        "hfr": 3.0, "fwhm_arcsec": 1.5, "stars": 180,
                        "passed_qa": True, "file": f"LIGHT\\L_{exp}_{i}.fits",
                        "time": f"2026-11-22T11:{50 + i:02d}:00Z"})
    return out


DRIFT_NAME = "Tracking test Dawn (Soul Nebula)"   # a goal's own field


def test_tracking_report_groups_the_drift_test(tmp_path):
    from photonscript.scheduler import tracking_test as tt
    cfg = _cfg(tmp_path)
    goal = [{"rig": "rc16", "target": SOUL, "filter": "Ha", "exp_s": 300.0,
             "ecc": 0.4, "passed_qa": True, "time": "2026-11-22T10:00:00Z"}]
    rep = tt.build_report(cfg, "2026-11-21", records=_drift_recs(DRIFT_NAME) + goal,
                          read_headers=False)
    assert rep["n_subs"] == 12 and rep["targets"] == [DRIFT_NAME]
    assert [(g["filter"], g["exp_s"], g["n"]) for g in rep["groups"]] == [
        ("L", 30.0, 3), ("L", 60.0, 3), ("L", 120.0, 3), ("L", 300.0, 3)]


def test_dusk_gate_does_not_count_the_drift_test():
    """The PS-127 gate keys on its own label: the dawn test's subs and
    starts never make the dusk test SKIP."""
    from photonscript.scheduler import tracking_test as tt
    recs = _drift_recs(DRIFT_NAME)
    ev = [{"value": "run", "label": DRIFT_NAME}] * 3
    for dusk in ("Tracking test Soul Nebula", "Tracking test East (Soul Nebula)"):
        d = tt.gate_decision(dusk, 8, recs, ev)
        assert d["verdict"] == "RUN" and d["n_subs"] == 0 and d["runs"] == 0


def test_drift_subs_are_test_data_never_filed(tmp_path):
    """Librarian / dawn filing: never approved into a goal, never counted
    for sync, never renamed to the goal whose field it used."""
    from photonscript.scheduler import dawn_autofile as da
    from photonscript.shared.qa_rules import is_test_record
    from photonscript.shared.target_names import canonical_target
    cfg = _cfg(tmp_path)
    rec = _drift_recs(DRIFT_NAME)[0]
    assert is_test_record(rec)
    assert not da.approvable(dict(rec))
    assert da.approvable(dict(rec, target=SOUL))
    assert da.filed_counts(cfg, [dict(rec, reviewed=True)]) == {}
    assert canonical_target(DRIFT_NAME, [SOUL]) == DRIFT_NAME
    segs = da.rc16_timeline(cfg, _drift_recs(DRIFT_NAME))
    assert segs and all(s["test"] for s in segs)


def test_piggy_lengths_skip_the_drift_test(tmp_path, monkeypatch):
    """The Piggy-600 companion's per-target lengths leave the test out; the
    last segment still runs to dawn."""
    from photonscript.scheduler import piggy_length as pl
    cfg, ts, _info = _drift_plan(tmp_path, monkeypatch)
    rec = pl.plan(cfg, ts, _goals())
    assert ts[-1].tracking_test
    assert ts[-1].name not in [r["target"] for r in rec["targets"]]
    assert rec["targets"][-1]["target"] == SOUL


def test_drift_test_stays_inside_an_early_gap():
    """A gap before the first target rises (e.g. a moonlit dusk): the test
    window is that gap only, never the targets' windows after it."""
    a = _t("A", datetime(2026, 11, 22, 4, 54), datetime(2026, 11, 22, 6, 30))
    b = _t("B", datetime(2026, 11, 22, 6, 30), DAWN)
    a.ra_hours, b.ra_hours = 3.0, 6.0
    a.handoff_utc, b.handoff_utc = a.window_end_utc, None
    info = {"gap_fill": [{
        "gap_start_utc": "2026-11-22T01:36:00Z",
        "gap_end_utc": "2026-11-22T04:54:00Z", "gap_h": 3.3,
        "target": None, "reason": "none"}]}
    cfg = PhotonScriptConfig(_env_file=None)
    out = tp.gap_drift([a, b], info, cfg, end=DAWN, guided=False)
    dt = out[0]
    assert dt.tracking_test and [t.name for t in out[1:]] == ["A", "B"]
    assert dt.window_start_utc == datetime(2026, 11, 22, 1, 36)
    assert dt.window_end_utc == dt.handoff_utc == a.window_start_utc
    assert dt.name.startswith("Tracking test Gap 0136Z")
    assert b.handoff_utc is None


def test_night_plan_shows_the_drift_test(tmp_path, monkeypatch):
    """The night plan view: a "Drift test" timeline event and the gap fill
    note; the schedule cards (goals) are unchanged."""
    from datetime import datetime as real_dt
    import photonscript.scheduler.night_plan as np_mod
    cfg, ts, info = _drift_plan(tmp_path, monkeypatch)

    class FakeDT(real_dt):
        @classmethod
        def utcnow(cls):
            return NOW

    monkeypatch.setattr(np_mod, "datetime", FakeDT)
    monkeypatch.setattr(tp, "plan_with_info", lambda *a, **k: (ts, info))
    plan = np_mod.build_night_plan(cfg)
    ev = [e for e in plan["events"] if e["event"].startswith("Drift test: ")]
    assert len(ev) == 1 and ts[-1].name in ev[0]["event"]
    assert "30/60/120/300 s x3" in ev[0]["detail"]
    assert ev[0]["time"]["utc"].startswith("2026-11-22T11:46")
    assert "drift test (" in plan["gap_fill_note"]
    assert ts[-1].name not in [s["name"] for s in plan["schedule"]]
    assert SOUL in [s["name"] for s in plan["schedule"]]
