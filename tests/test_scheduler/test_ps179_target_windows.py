"""PS-179: the first repeating target held the RC16 all night.

2026-10-07: tonight's plan had three targets (Andromeda Galaxy LRGB, NGC 604
and the Heart Nebula Ha/OIII/SII). The planner had scaled M31's sets down to
L x3, R/G/B x1 per pass, but "Andromeda Galaxy imaging (repeats while safe
and up)" loops while the target is up, so M31 took all 281 RC16 subs and
NGC 604 and the Heart never ran.

Now the planner gives every target a time window (visibility, priority,
goal hours still owed, moonrise for broadband-only) and the generator ends
the target's loop at its window end (a TimeCondition on the imaging loop and
the DSO container), so the sequence moves on. Leftover time at the end of
the night goes back to the highest-priority target still up.

Pinned: the allocation rule (allocate_windows), the planner on the 10-07
plan shape, the generated JSON (handoff conditions, fill container, lint
clean incl. PS-149 loop-spin), the PS-77 NINA simulator replaying the night
(old shape: every light is M31; new: all three targets image), and the
night plan / dashboard rows.
"""

import json
from datetime import datetime, timedelta

import pytest

from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import target_planner
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.sequence_lint import lint
from photonscript.scheduler.target_planner import (allocate_windows,
                                                   plan_night_sequence)
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.localtime import utc_offset_hours
from photonscript.shared.models import (CelestialTarget, ExposurePlan,
                                        FilterType, ImagingProject,
                                        NinaSequenceTarget)
from tests.test_scheduler.test_ps77_safety_stop import (DUSK_LOCAL, NinaSim,
                                                        _short, _walk)

F = FilterType
NIGHT = datetime(2026, 10, 7, 20)     # 14:00 MDT on 2026-10-07
_REAL_PLANNER = plan_night_sequence
H = timedelta(hours=1)
T0 = datetime(2026, 10, 8, 2, 10)      # astro dusk (UTC)
T1 = T0 + 9.75 * H                     # astro dawn


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _plan(f, exp, count, acquired=0):
    return ExposurePlan(filter_type=f, exposure_seconds=exp, count=count,
                        acquired=acquired, gain=200, offset=256)


def _projects_1007():
    """The goal store on 2026-10-07/08 (GET /api/projects, RC16 plans)."""
    return [
        ImagingProject(id="m31", priority=50, target=CelestialTarget(
            name="Andromeda Galaxy", ra_hours=0.712, dec_degrees=41.27,
            object_type="galaxy"), exposure_plans=[
            _plan(F.LUMINANCE, 300, 46, 19), _plan(F.RED, 300, 15, 2),
            _plan(F.GREEN, 300, 15, 2), _plan(F.BLUE, 300, 15, 3)]),
        ImagingProject(id="heart", priority=60, target=CelestialTarget(
            name="Heart Nebula", ra_hours=2.555, dec_degrees=61.47,
            object_type="emission nebula"), exposure_plans=[
            _plan(F.HA, 600, 54), _plan(F.OIII, 600, 63, 2),
            _plan(F.SII, 600, 63)]),
        ImagingProject(id="n604", priority=50, target=CelestialTarget(
            name="NGC 604", ra_hours=1.5758, dec_degrees=30.783,
            object_type="emission nebula"), exposure_plans=[
            _plan(F.HA, 600, 30), _plan(F.OIII, 600, 24),
            _plan(F.SII, 600, 6)]),
    ]


def _entry(name, prio=50, need_h=20.0, up_from=None, up_until=None):
    return {"name": name, "priority": prio, "need_s": need_h * 3600,
            "up_from": up_from, "up_until": up_until}


def _check_tiling(windows, start, end, names):
    """Windows in run order, back to back, inside [start, end]."""
    spans = [windows[n] for n in names]
    assert spans[0][0] == start
    for (a0, a1), (b0, b1) in zip(spans, spans[1:]):
        assert a0 < a1 <= b0 < b1
        assert a1 == b0                 # no idle gap, no overlap
    assert spans[-1][1] <= end


# --- 1. the allocation rule ---------------------------------------------------

def test_three_all_night_targets_share_the_night_by_priority():
    a = allocate_windows([_entry("M31", 50), _entry("NGC 604", 50),
                          _entry("Heart", 60)], T0, T1)
    names = ["M31", "NGC 604", "Heart"]
    _check_tiling(a["windows"], T0, T1, names)
    assert a["windows"]["Heart"][1] == T1
    hours = {n: (b - s) / H for n, (s, b) in a["windows"].items()}
    assert hours["M31"] == pytest.approx(9.75 * 50 / 160, abs=0.01)
    assert hours["NGC 604"] == pytest.approx(hours["M31"], abs=0.01)
    assert hours["Heart"] > hours["M31"]
    # the first two hand the mount on, the last keeps it to the loop end
    assert a["handoff"] == {"M31": a["windows"]["M31"][1],
                            "NGC 604": a["windows"]["NGC 604"][1]}
    assert a["fill"] is None and a["dropped"] == []


def test_a_nearly_done_goal_frees_its_time_for_the_others():
    a = allocate_windows([_entry("M31", 50, need_h=1.0),
                          _entry("NGC 604", 50), _entry("Heart", 60)], T0, T1)
    assert (a["windows"]["M31"][1] - T0) == H
    _check_tiling(a["windows"], T0, T1, ["M31", "NGC 604", "Heart"])


def test_leftover_time_goes_back_to_the_highest_priority_target_still_up():
    # the last target is nearly done: the rest of the night goes to the
    # Heart (priority 60), which ran earlier, in a final fill window
    a = allocate_windows([_entry("Heart", 60, need_h=2.0),
                          _entry("M31", 50, need_h=1.0)], T0, T1)
    assert a["fill"] == ("Heart", T0 + 3 * H, T1)
    assert a["handoff"] == {"Heart": T0 + 2 * H, "M31": T0 + 3 * H}


def test_leftover_on_the_last_target_just_extends_it():
    a = allocate_windows([_entry("M31", 50, need_h=1.0),
                          _entry("Heart", 60, need_h=1.0)], T0, T1)
    assert a["fill"] is None
    assert a["windows"]["Heart"] == (T0 + H, T1)
    assert a["handoff"] == {"M31": T0 + H}


def test_windows_respect_rise_and_set_and_never_leave_a_gap():
    a = allocate_windows([
        _entry("West", 50, up_until=T0 + 2 * H),           # sets early
        _entry("East", 50, up_from=T0 + 4 * H)], T0, T1)   # rises late
    w = a["windows"]
    assert w["West"] == (T0, T0 + 2 * H)
    assert w["East"] == (T0 + 4 * H, T1)
    # stretch to the next rise when the earlier target is still up
    a = allocate_windows([_entry("A", 50),
                          _entry("B", 50, up_from=T0 + 7 * H)], T0, T1)
    assert a["windows"]["A"] == (T0, T0 + 7 * H)


def test_a_target_with_too_little_time_left_gets_no_window():
    a = allocate_windows([_entry("A", 50),
                          _entry("Gone", 50, up_until=T0 + 0.2 * H)],
                         T0, T1, min_window_s=1800)
    assert a["dropped"] == ["Gone"]
    assert a["windows"]["A"] == (T0, T1) and a["handoff"] == {}


def test_redispatch_mid_night_allocates_from_now(tmp_path):
    now = T0 + 5 * H
    ts = [NinaSequenceTarget(name=n, ra_hours=0, dec_degrees=0,
                             exposures=[_plan(F.HA, 600, 30)])
          for n in ("A", "B")]
    kept = target_planner._apply_windows(
        ts, {}, {"A": 72000, "B": 72000}, {"A": 50, "B": 50}, _cfg(tmp_path),
        T0, T1, now, {})
    assert kept[0].window_start_utc == now
    assert kept[1].window_end_utc == T1


def test_broadband_only_target_ends_at_moonrise(tmp_path):
    rise = T0 + 4 * H
    ts = [NinaSequenceTarget(name="M31", ra_hours=0.7, dec_degrees=41,
                             exposures=[_plan(F.LUMINANCE, 300, 200)]),
          NinaSequenceTarget(name="Heart", ra_hours=2.5, dec_degrees=61,
                             exposures=[_plan(F.HA, 600, 200)])]
    moon = {"available": True, "down_at_dusk": True, "rise_utc": rise}
    kept = target_planner._apply_windows(
        ts, {}, {"M31": 1e6, "Heart": 1e6}, {"M31": 90, "Heart": 10},
        _cfg(tmp_path), T0, T1, T0, moon)
    assert kept[0].window_end_utc == rise       # broadband before moonrise
    assert kept[0].handoff_utc == rise
    assert kept[1].window_start_utc == rise and kept[1].handoff_utc is None


def test_per_pass_counts_fit_the_window_for_filter_balance(tmp_path):
    t = NinaSequenceTarget(name="Heart", ra_hours=2.5, dec_degrees=61,
                           exposures=[_plan(F.HA, 600, 13),
                                      _plan(F.OIII, 600, 15),
                                      _plan(F.SII, 600, 16)])
    other = NinaSequenceTarget(name="M31", ra_hours=0.7, dec_degrees=41,
                               exposures=[_plan(F.HA, 600, 1)])
    target_planner._apply_windows(
        [other, t], {}, {"M31": 1e6, "Heart": 1e6}, {"M31": 50, "Heart": 50},
        _cfg(tmp_path), T0, T0 + 6 * H, T0, {})
    win_s = (t.window_end_utc - t.window_start_utc).total_seconds()
    assert sum(e.count * e.exposure_seconds for e in t.exposures) <= win_s * 0.85
    assert all(e.count >= 1 for e in t.exposures)   # every filter still shot


# --- 2. the 2026-10-07 plan shape through the planner and generator ---------------

@pytest.fixture
def night_1007(tmp_path):
    return plan_night_sequence(_projects_1007(), _cfg(tmp_path), NIGHT)


def test_replay_1007_three_targets_with_back_to_back_windows(night_1007):
    ts = night_1007
    assert [t.name for t in ts] == ["Andromeda Galaxy", "NGC 604",
                                    "Heart Nebula"]
    w = {t.name: (t.window_start_utc, t.window_end_utc) for t in ts}
    _check_tiling(w, ts[0].window_start_utc, ts[-1].window_end_utc,
                  [t.name for t in ts])
    dark = (ts[-1].window_end_utc - ts[0].window_start_utc) / H
    assert dark > 9.5                     # the windows cover the dark time
    hours = {n: (b - a) / H for n, (a, b) in w.items()}
    assert hours["NGC 604"] >= 2.5 and hours["Heart Nebula"] >= 2.5
    assert hours["Heart Nebula"] > hours["Andromeda Galaxy"]   # priority 60
    assert [t.handoff_utc is not None for t in ts] == [True, True, False]
    # the small per-pass counts for filter balance stay
    m31 = {e.filter_type.value: e.count for e in ts[0].exposures}
    assert m31["L"] <= 6 and m31["R"] >= 1


def test_replay_1007_sequence_hands_off_and_lints_clean(night_1007):
    for t in night_1007:
        t.start_guiding = True
    seq = json.loads(nsj.generate_nina_json(
        build_sequence_for_night("PS179_1007", night_1007)))
    r = lint(seq, guided=True)
    assert r.ok, [f.detail for f in r.findings if f.level == "ERROR"]
    assert not [f for f in r.findings if f.rule == "loop-spin"]
    dso = [d for d in _walk(seq) if _short(d.get("$type", ""))
           == "DeepSkyObjectContainer"]
    assert [d["Name"] for d in dso] == ["Andromeda Galaxy", "NGC 604",
                                        "Heart Nebula"]

    def times(d):
        return [(c["Hours"], c["Minutes"]) for c in d["Conditions"]["$values"]
                if _short(c["$type"]) == "TimeCondition"
                and ".DateTimeProvider.TimeProvider," in
                c["SelectedProvider"]["$type"]]

    cfg = PhotonScriptConfig()
    for d, t in zip(dso, night_1007):
        loop = next(x for x in _walk(d) if str(x.get("Name", "")).endswith(
            nsj.TARGET_IMAGING_SUFFIX))
        if t.handoff_utc is None:
            assert times(d) == [] and times(loop) == []
        else:
            loc = t.handoff_utc + timedelta(
                hours=utc_offset_hours(cfg, t.handoff_utc))
            assert times(d) == [(loc.hour, loc.minute)]
            assert (loc.hour, loc.minute) in times(loop)


def test_windows_off_keeps_the_old_shape(tmp_path):
    ts = plan_night_sequence(_projects_1007(),
                             _cfg(tmp_path, plan_target_windows=False), NIGHT)
    assert all(t.handoff_utc is None and t.window_start_utc is None
               for t in ts)


# --- 3. the PS-77 simulator replays the night ---------------------------------------

class _Sim(NinaSim):
    """NinaSim that names the DSO container every light was shot in."""

    def run_instruction(self, n):
        k = len(self.lights)
        try:
            super().run_instruction(n)
        finally:
            if len(self.lights) > k:
                dso = [c for c in self.stack
                       if c.type == "DeepSkyObjectContainer"]
                self.lights[-1]["target"] = (dso[-1].d.get("Name")
                                             if dso else None)


def _utc_at_local(hh, mm):
    """UTC of a local clock time on the night of 2026-10-07/08."""
    cfg = PhotonScriptConfig()
    day = 7 if hh >= 12 else 8
    loc = datetime(2026, 10, day, hh, mm)
    return loc - timedelta(hours=utc_offset_hours(cfg, loc + 6 * H))


def _sim_targets(handoffs):
    """The 10-07 per-pass plan (GET /api/tonight), handoff after 3 h and 6 h
    of the night (sim local dusk 20:18)."""
    def tgt(name, ra, dec, plans, handoff):
        t = NinaSequenceTarget(name=name, ra_hours=ra, dec_degrees=dec,
                               exposures=[_plan(f, e, n) for f, e, n in plans])
        t.start_guiding = True
        t.handoff_utc = handoff
        return t
    h1, h2 = (_utc_at_local(23, 18), _utc_at_local(2, 18)) if handoffs \
        else (None, None)
    return [
        tgt("Andromeda Galaxy", 0.712, 41.27,
            [(F.LUMINANCE, 300, 3), (F.RED, 300, 1), (F.GREEN, 300, 1),
             (F.BLUE, 300, 1)], h1),
        tgt("NGC 604", 1.5758, 30.783,
            [(F.HA, 600, 2), (F.OIII, 600, 1), (F.SII, 600, 1)], h2),
        tgt("Heart Nebula", 2.555, 61.47,
            [(F.HA, 600, 5), (F.OIII, 600, 6), (F.SII, 600, 6)], None),
    ]


def _run(handoffs):
    seq = json.loads(nsj.generate_nina_json(
        build_sequence_for_night("PS179_SIM", _sim_targets(handoffs))))
    return seq, _Sim(seq, tick=0.002, max_steps=3_000_000).run()


def _by_target(sim):
    out = {}
    for x in sim.lights:
        if x["done"]:
            out.setdefault(x["target"], []).append(x)
    return out


def test_sim_old_shape_m31_takes_every_sub():
    _, sim = _run(handoffs=False)
    got = _by_target(sim)
    assert set(got) == {"Andromeda Galaxy"}      # the 2026-10-07 night
    assert len(got["Andromeda Galaxy"]) > 60


def test_sim_windows_hand_the_mount_on_and_every_target_images():
    seq, sim = _run(handoffs=True)
    assert lint(seq, guided=True).ok
    assert sim.spins() == {}, sim.spins()
    got = _by_target(sim)
    assert set(got) == {"Andromeda Galaxy", "NGC 604", "Heart Nebula"}
    h1, h2 = 3 * 3600, 6 * 3600     # 23:18 and 02:18 local, from dusk
    assert DUSK_LOCAL == 20 * 3600 + 18 * 60
    m31, n604, heart = (got[k] for k in ("Andromeda Galaxy", "NGC 604",
                                         "Heart Nebula"))
    # no sub runs past its target's window end, and the next one starts soon
    # (a sub that would not fit before the window end is not started, so the
    # next target may start a few minutes early)
    assert max(x["end"] for x in m31) <= h1
    assert h1 - 600 < min(x["start"] for x in n604) < h1 + 900
    assert max(x["end"] for x in n604) <= h2
    assert h2 - 900 < min(x["start"] for x in heart) < h2 + 900
    assert max(x["end"] for x in m31) <= min(x["start"] for x in n604)
    assert max(x["end"] for x in n604) <= min(x["start"] for x in heart)
    # every filter of every target got time
    assert len(n604) >= 12 and len(heart) >= 12
    # the shutter stays busy: lights fill most of each window
    for subs, span in ((m31, h1), (n604, h2 - h1)):
        assert sum(x["end"] - x["start"] for x in subs) > 0.75 * span


# --- 4. fill container ------------------------------------------------------------

def test_fill_container_runs_last_without_a_handoff():
    ts = _sim_targets(handoffs=True)
    heart = ts[2]
    heart.handoff_utc = _utc_at_local(4, 0)
    ts[0].fill_from_utc = heart.handoff_utc         # M31 back for the rest
    ts[0].fill_end_utc = _utc_at_local(5, 50)
    seq = json.loads(nsj.generate_nina_json(
        build_sequence_for_night("PS179_FILL", ts)))
    r = lint(seq, guided=True)
    assert r.ok, [f.detail for f in r.findings if f.level == "ERROR"]
    dso = [d for d in _walk(seq) if _short(d.get("$type", ""))
           == "DeepSkyObjectContainer"]
    fill_name = "Andromeda Galaxy" + nsj.TARGET_FILL_SUFFIX
    assert [d["Name"] for d in dso][-1] == fill_name
    fill = dso[-1]
    assert fill["Target"]["TargetName"] == "Andromeda Galaxy"   # FITS OBJECT
    assert not [c for c in fill["Conditions"]["$values"]
                if _short(c["$type"]) == "TimeCondition"]
    from photonscript.shared.target_names import strip_container_name
    assert strip_container_name(
        f"{fill_name}{nsj.TARGET_IMAGING_SUFFIX}_Container") == \
        "Andromeda Galaxy"
    sim = _Sim(seq, tick=0.002, max_steps=3_000_000).run()
    assert sim.spins() == {}
    got = _by_target(sim)
    assert fill_name in got
    # the Heart hands off at 04:00 local (dusk 20:18 + 7 h 42 min)
    assert min(x["start"] for x in got[fill_name]) >= 7 * 3600 + 42 * 60 - 900
    assert max(x["end"] for x in got["Heart Nebula"]) <= 7 * 3600 + 42 * 60


# --- 5. night plan and dashboard rows -----------------------------------------------

def test_night_plan_shows_the_windows_and_the_fill(tmp_path, monkeypatch):
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler.night_plan import build_night_plan

    class _Store:
        projects = {p.id: p for p in _projects_1007()}

    def fake(projects, config, now):
        ts = _REAL_PLANNER(projects, config, NIGHT)
        ts[0].fill_from_utc = ts[-1].window_end_utc - H
        ts[0].fill_end_utc = ts[-1].window_end_utc
        ts[-1].window_end_utc = ts[-1].handoff_utc = ts[0].fill_from_utc
        return ts

    monkeypatch.setattr(app_mod, "_store", _Store())
    monkeypatch.setattr(target_planner, "plan_night_sequence", fake)
    plan = build_night_plan(_cfg(tmp_path))
    rows = plan["schedule"]
    assert [r["name"] for r in rows] == ["Andromeda Galaxy", "NGC 604",
                                         "Heart Nebula", "Andromeda Galaxy"]
    assert [r["fill"] for r in rows] == [False, False, False, True]
    assert rows[-1]["kind"].endswith("fill (rest of the night)")
    for a, b in zip(rows, rows[1:]):
        assert a["window_end_utc"] == b["window_start_utc"]
    assert rows[0]["handoff_utc"] == rows[0]["window_end_utc"]
    assert rows[-1]["handoff_utc"] is None
    assert plan["stats"]["targets"] == 3
    assert any(e["event"] == "Fill: Andromeda Galaxy" for e in plan["events"])
    assert any("repeats until the window ends" in e["detail"]
               for e in plan["events"] if e["event"].startswith("Target:"))


def test_new_sources_are_ascii():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[2]
    for p in ("tests/test_scheduler/test_ps179_target_windows.py",):
        (root / p).read_text(encoding="ascii")
