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
