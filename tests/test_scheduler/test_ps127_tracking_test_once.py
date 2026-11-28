"""PS-127: the sideloaded tracking test runs at most once per night.

PS-123 spliced the PS-84 tracking-test DeepSkyObjectContainer first into
TARGETS_CONTAINER, inside SAFE_LOOP / LOOP_ALL_NIGHT. Its LoopCondition(1)
counter is reset whenever a parent container loops, so after an unsafe pause
LOOP_ALL_NIGHT re-entered SAFE_LOOP, reset the test and ran the whole ladder
again (about 1 h). The PS-77 NINA simulator below shows that on the old
splice and pins the fix: the test now sits in the Targets area BEFORE
LOOP_ALL_NIGHT, which NINA runs once and never resets.

Design for an unsafe spell DURING the test: the remaining ladder is skipped
(the night loop parks in its UNSAFE branch and later resumes tonight's
targets); a partial ladder is still a valid report. If it is unsafe when
the Targets area starts, the test is skipped entirely.
"""
import json

import pytest

from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import sideload as sd
from photonscript.scheduler.sequence_lint import lint
from photonscript.shared.config import PhotonScriptConfig
from tests.test_scheduler.test_ps77_safety_stop import HORIZON, NinaSim
from tests.test_scheduler.test_ps123_sideload import _night, _tt

TT = "Tracking test M 2"


@pytest.fixture(autouse=True)
def _pinned(tmp_path, monkeypatch):
    cfg = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                             quality_eccentricity_max=0.60)
    monkeypatch.setattr(nsj, "_gen_cfg_cache", cfg)
    monkeypatch.setattr(nsj, "_filter_names_cache", cfg.filter_name_map())
    monkeypatch.chdir(tmp_path)
    return cfg


def _old_splice(night, tt):
    """The PS-123 splice (test first INSIDE TARGETS_CONTAINER), rebuilt here
    so the simulator can show what it did."""
    seq = sd.strip_ids(night)
    tests = [v for v in sd._items(sd._targets_container(sd.strip_ids(tt)))
             if sd.DSO in v.get("$type", "")]
    tc = sd._targets_container(seq)
    tc["Items"]["$values"] = tests + sd._items(tc)
    return nsj.link_parents(seq)


def _spliced():
    _, night = _night()
    return sd.splice_tracking_test(night, _tt(), [])


class _Trace(NinaSim):
    """NinaSim that records which DSO each LIGHT belongs to and every entry
    into a DSO container."""

    def __init__(self, seq, unsafe=()):
        super().__init__(seq, unsafe)
        self.entries, self.dso = [], []

    def run_container(self, c):
        is_dso = c.type == "DeepSkyObjectContainer"
        if is_dso:
            self.dso.append(c.d.get("Name"))
        try:
            return super().run_container(c)
        finally:
            if is_dso:
                self.dso.pop()

    def run_item(self, n):
        if n.type == "DeepSkyObjectContainer":
            self.entries.append((n.d.get("Name"), self.t))
        return super().run_item(n)

    def run_instruction(self, n):
        before = len(self.lights)
        try:
            return super().run_instruction(n)
        finally:
            for x in self.lights[before:]:
                x["target"] = self.dso[-1] if self.dso else None

    def lights_of(self, name):
        return [x for x in self.lights if x.get("target") == name]

    def tt_runs(self):
        """Distinct passes through the ladder (one per entry that shot)."""
        runs, last = 0, None
        for x in self.lights:
            tgt = x.get("target")
            if tgt == TT and last != TT:
                runs += 1
            last = tgt
        return runs


def _heart_window(seq):
    """(start, end) of Heart's lights on a clear night."""
    clear = _Trace(seq).run()
    h = clear.lights_of("Heart Nebula")
    assert h, "Heart never imaged on a clear night"
    return h[0]["start"], h[-1]["end"]


# ------------------------------------------------------------- the bug, pinned

def test_old_splice_reran_the_test_after_an_unsafe_pause():
    _, night = _night()
    seq = _old_splice(night, _tt())
    a, b = _heart_window(seq)
    mid = (a + b) / 2
    sim = _Trace(seq, unsafe=[(mid, mid + 1800)]).run()
    assert sim.tt_runs() == 2, "the PS-77 simulator no longer reproduces it"


# ----------------------------------------------------------------- the fix

def test_clear_night_runs_the_test_once_then_tonight():
    sim = _Trace(_spliced()).run()
    assert sim.tt_runs() == 1
    order = [n for n, _ in sim.entries]
    assert order[0] == TT and order.count(TT) == 1
    first_heart = sim.lights_of("Heart Nebula")[0]["start"]
    assert max(x["end"] for x in sim.lights_of(TT)) <= first_heart


def test_unsafe_pause_mid_heart_does_not_rerun_the_test():
    seq = _spliced()
    a, b = _heart_window(seq)
    mid = (a + b) / 2
    sim = _Trace(seq, unsafe=[(mid, mid + 1800)]).run()
    assert sim.tt_runs() == 1
    assert [n for n, _ in sim.entries].count(TT) == 1
    # tonight's targets resumed after the pause
    assert [x for x in sim.lights if x["start"] >= mid + 1800]
    assert not [x for x in sim.lights if mid <= x["start"] < mid + 1800]


def test_two_unsafe_pauses_still_one_test():
    seq = _spliced()
    a, b = _heart_window(seq)
    sim = _Trace(seq, unsafe=[(a + 600, a + 1500),
                              (b + 3600, b + 5400)]).run()
    assert sim.tt_runs() == 1


def test_unsafe_during_the_test_skips_the_rest_of_the_ladder():
    """Documented design: an unsafe spell inside the ladder ends the test
    for tonight (no partial rerun); the night loop parks, waits and resumes
    tonight's targets, which then image normally."""
    seq = _spliced()
    clear = _Trace(seq).run()
    tt = clear.lights_of(TT)
    cut = (tt[0]["start"] + tt[-1]["end"]) / 2
    sim = _Trace(seq, unsafe=[(cut, cut + 1800)]).run()
    shot = sim.lights_of(TT)
    assert 0 < len(shot) < len(tt)
    assert max(x["start"] for x in shot) < cut
    assert sim.tt_runs() == 1
    after = [x for x in sim.lights if x["start"] >= cut + 1800]
    assert after and all(x["target"] != TT for x in after)
    assert sim.lights_of("Heart Nebula")


def test_unsafe_when_the_targets_area_starts_skips_the_test():
    seq = _spliced()
    clear = _Trace(seq).run()
    t0 = next(t for n, t in clear.entries if n == TT)
    sim = _Trace(seq, unsafe=[(t0 - 60, t0 + 3600)]).run()
    assert not sim.lights_of(TT)
    assert sim.lights_of("Heart Nebula")
    assert min(x["start"] for x in sim.lights) >= t0 + 3600


def test_no_light_after_horizon_or_runaway():
    sim = _Trace(_spliced()).run()
    assert sim.lights and max(x["end"] for x in sim.lights) < HORIZON


# --------------------------------------------------------------- structure

def test_test_sits_before_the_night_loop_in_the_targets_area():
    seq = _spliced()
    area = sd.find_container(seq, "Targets", "TargetAreaContainer")
    kids = [(sd._short(c["$type"]), c["Name"]) for c in sd._items(area)]
    assert kids[0] == ("DeepSkyObjectContainer", TT)
    assert kids[1][1] == nsj.NIGHT_LOOP_NAME
    tc = sd._targets_container(seq)
    assert TT not in [c.get("Name") for c in sd._items(tc)]
    assert sd.target_names(seq)[0] == TT


def test_spliced_test_does_not_promise_park_and_hold():
    seq = _spliced()
    blob = json.dumps(sd.find_container(seq, TT, sd.DSO))
    assert "parks and holds" not in blob
    assert "once" in blob
    notes = [f.detail for f in lint(seq, guided=False).findings
             if f.rule == "tracking-test"]
    assert notes and not any("parks and holds" in d for d in notes)


def test_standalone_download_keeps_park_and_hold():
    tt = _tt()
    blob = json.dumps(sd.find_container(tt, TT, sd.DSO))
    assert "parks and holds until dawn" in blob
    notes = [f.detail for f in lint(tt, guided=False).findings
             if f.rule == "tracking-test"]
    assert any("parks and holds" in d for d in notes)
    # standalone: the test stays inside the night loop
    tc = sd._targets_container(tt)
    assert sd.target_names(tt) == [TT]
    assert [c.get("Name") for c in sd._items(tc)] == [TT]
