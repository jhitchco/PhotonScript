"""PS-127 follow-up (2026-11-06): the gated tracking-test splice.

2026-11-05: the test sat in the Targets area (runs once). The roof closed
during its opening AF, the night loop resumed tonight's targets at 23:07
and the test never ran (/api/tracking-test/report: 0 subs). With the
re-entry gate script on the machine the splice now puts the test FIRST in
TARGETS_CONTAINER, inside the night loop, led by a tracking_test gate that
answers SKIP once tonight's runs log has the whole ladder (or the test has
started GATE_MAX_RUNS times). The PS-77 simulator below models the gate
from the lights it saw, the way the service counts the runs log.
"""
import json
import re
from datetime import datetime

import pytest

from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import sideload as sd
from photonscript.scheduler import tracking_test as tt
from photonscript.scheduler.sequence_lint import lint
from photonscript.shared.config import PhotonScriptConfig
from tests.test_scheduler.test_ps77_safety_stop import HORIZON, _Interrupt
from tests.test_scheduler.test_ps123_sideload import _night, _tt
from tests.test_scheduler.test_ps127_tracking_test_once import TT, _Trace

SCRIPT = r"C:\astro\PhotonScript\deploy\reentry-gate.cmd"


@pytest.fixture(autouse=True)
def _pinned(tmp_path, monkeypatch):
    cfg = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                             quality_eccentricity_max=0.60)
    monkeypatch.setattr(nsj, "_gen_cfg_cache", cfg)
    monkeypatch.setattr(nsj, "_filter_names_cache", cfg.filter_name_map())
    monkeypatch.chdir(tmp_path)
    return cfg


def _gated():
    _, night = _night()
    return sd.splice_tracking_test(night, _tt(), [], gate_script=SCRIPT)


class _Gated(_Trace):
    """_Trace plus the tracking_test gate: SKIP (SkipInstructionSetOnError
    -> Parent.Interrupt(), the test container) per tt.gate_decision on the
    finished test lights so far."""

    def __init__(self, seq, unsafe=()):
        super().__init__(seq, unsafe)
        self.events, self.verdicts = [], []

    def run_instruction(self, n):
        script = str(n.d.get("Script") or "")
        if n.type == "ExternalScript" and " tracking_test " in script:
            label = re.search(r'--label="([^"]*)"', script).group(1)
            planned = int(re.search(r"--subs=(\d+)", script).group(1))
            recs = [{"target": TT} for x in self.lights_of(TT) if x["done"]]
            dec = tt.gate_decision(label, planned, recs, self.events)
            self.events.append({"value": dec["verdict"].lower(),
                                "label": label})
            self.verdicts.append(dec["verdict"])
            if dec["verdict"] == "SKIP":
                assert n.d.get("ErrorBehavior") == 1
                raise _Interrupt(n.parent)
            return None
        return super().run_instruction(n)

    def starts(self):
        return self.verdicts.count("RUN")


def _planned(seq):
    return sd.planned_subs(sd.find_container(seq, TT, sd.DSO))


# --------------------------------------------------------------- structure

def test_gated_splice_puts_the_test_first_in_the_night_loop():
    seq = _gated()
    area = sd.find_container(seq, "Targets", "TargetAreaContainer")
    assert [c["Name"] for c in sd._items(area)][0] == nsj.NIGHT_LOOP_NAME
    tc = sd._targets_container(seq)
    assert [c.get("Name") for c in sd._items(tc)][0] == TT
    assert sd.target_names(seq)[0] == TT
    gate = sd._items(sd.find_container(seq, TT, sd.DSO))[0]
    assert "ExternalScript" in gate["$type"]
    assert gate["ErrorBehavior"] == 1 and gate["Attempts"] == 1
    want = f'tracking_test --label="{TT}" --subs={_planned(seq)}'
    assert want in gate["Script"]


def test_planned_subs_counts_the_whole_ladder():
    test = sd.find_container(_tt(), TT, sd.DSO)
    lights = sum(1 for x in _Trace(_tt()).run().lights_of(TT) if x["done"])
    assert sd.planned_subs(test) == lights > 0


def test_gated_splice_lints_clean_and_says_so():
    res = lint(_gated(), guided=False)
    errs = [f.detail for f in res.findings if f.rule == "reentry-gate"]
    assert not errs, errs
    notes = [f.detail for f in res.findings if f.rule == "tracking-test"]
    assert notes and "runs it again after a pause" in notes[0]
    blob = json.dumps(sd.find_container(_gated(), TT, sd.DSO))
    assert "parks and holds" not in blob


def test_gate_elsewhere_is_a_lint_error():
    seq = _gated()
    gate = sd._items(sd.find_container(seq, TT, sd.DSO))[0]
    heart = sd.find_container(seq, "Heart Nebula", sd.DSO)
    heart["Items"]["$values"].insert(0, dict(gate))
    errs = [f.detail for f in lint(seq, guided=False).findings
            if f.rule == "reentry-gate"]
    assert any("tracking_test gate belongs" in d for d in errs)


def test_no_script_keeps_the_targets_area_placement():
    _, night = _night()
    seq = sd.splice_tracking_test(night, _tt(), [], gate_script=None)
    area = sd.find_container(seq, "Targets", "TargetAreaContainer")
    assert [c["Name"] for c in sd._items(area)][0] == TT
    assert "tracking_test" not in json.dumps(seq)


# ------------------------------------------------------------- behaviour

def test_clear_night_runs_the_test_once_then_tonight():
    seq = _gated()
    sim = _Gated(seq).run()
    assert sim.tt_runs() == 1 and sim.starts() == 1
    assert len([x for x in sim.lights_of(TT) if x["done"]]) == _planned(seq)
    first_heart = sim.lights_of("Heart Nebula")[0]["start"]
    assert max(x["end"] for x in sim.lights_of(TT)) <= first_heart


def test_pause_after_the_ladder_never_reruns_it():
    seq = _gated()
    h = _Gated(seq).run().lights_of("Heart Nebula")
    mid = (h[0]["start"] + h[-1]["end"]) / 2
    sim = _Gated(seq, unsafe=[(mid, mid + 1800)]).run()
    assert sim.tt_runs() == 1 and sim.starts() == 1
    assert "SKIP" in sim.verdicts
    assert [x for x in sim.lights if x["start"] >= mid + 1800]


def test_2026_11_05_pause_in_the_opening_af_runs_it_on_resume():
    """Roof closed during the test's AF, before any sub: on resume the
    test runs (the old splice went straight to tonight's targets)."""
    seq = _gated()
    clear = _Gated(seq).run()
    t0 = next(t for n, t in clear.entries if n == TT)
    first = clear.lights_of(TT)[0]["start"]
    cut = (t0 + first) / 2
    sim = _Gated(seq, unsafe=[(cut, cut + 4200)]).run()
    done = [x for x in sim.lights_of(TT) if x["done"]]
    assert len(done) == _planned(seq)
    assert min(x["start"] for x in done) >= cut + 4200
    assert sim.lights_of("Heart Nebula")


def test_pause_mid_ladder_reruns_the_whole_ladder_once():
    seq = _gated()
    clear = _Gated(seq).run().lights_of(TT)
    cut = (clear[0]["start"] + clear[-1]["end"]) / 2
    sim = _Gated(seq, unsafe=[(cut, cut + 1800)]).run()
    after = [x for x in sim.lights_of(TT)
             if x["done"] and x["start"] >= cut + 1800]
    assert len(after) == _planned(seq)
    assert sim.starts() == 2
    assert sim.lights_of("Heart Nebula")


def test_two_pauses_in_the_ladder_stop_at_two_starts():
    seq = _gated()
    clear = _Gated(seq).run().lights_of(TT)
    a = clear[0]["start"] + 900
    sim = _Gated(seq, unsafe=[(a, a + 1200)]).run()
    rerun = [x for x in sim.lights_of(TT) if x["start"] >= a + 1200]
    b = rerun[0]["start"] + 600
    sim = _Gated(seq, unsafe=[(a, a + 1200), (b, b + 1200)]).run()
    assert sim.starts() == tt.GATE_MAX_RUNS
    assert not [x for x in sim.lights_of(TT) if x["start"] >= b + 1200]
    assert [x for x in sim.lights if x["start"] >= b + 1200]
    assert max(x["end"] for x in sim.lights) < HORIZON


# ------------------------------------------------------------ the service

def test_gate_decision_rules():
    recs = [{"target": TT}] * 5 + [{"target": "Heart Nebula"}] * 9
    assert tt.gate_decision(TT, 16, [], [])["verdict"] == "RUN"
    d = tt.gate_decision(TT, 16, recs, [{"value": "run", "label": TT}])
    assert d["verdict"] == "RUN" and d["n_subs"] == 5
    assert "rerun" in d["why"]
    d = tt.gate_decision(TT, 5, recs, [{"value": "run", "label": TT}])
    assert d["verdict"] == "SKIP" and "complete" in d["why"]
    runs = [{"value": "run", "label": TT.upper()}] * 2
    d = tt.gate_decision(TT, 16, recs, runs)
    assert d["verdict"] == "SKIP" and "started 2 times" in d["why"]
    # planned unknown: never complete, the run cap still holds
    assert tt.gate_decision(TT, 0, recs, [])["verdict"] == "RUN"


def test_run_gate_logs_events_and_counts_them(_pinned):
    now = datetime(2026, 11, 6, 5, 0)
    a = tt.run_gate(_pinned, TT, 16, now=now, records=[])
    b = tt.run_gate(_pinned, TT, 16, now=now, records=[{"target": TT}] * 3)
    c = tt.run_gate(_pinned, TT, 16, now=now, records=[{"target": TT}] * 3)
    assert [a["verdict"], b["verdict"], c["verdict"]] == ["RUN", "RUN", "SKIP"]
    ev = tt.gate_events(_pinned, "2026-11-05")
    assert [e["value"] for e in ev] == ["run", "run", "skip"]
    assert ev[1]["n_subs"] == 3 and ev[1]["planned"] == 16


def test_run_gate_fails_open(_pinned, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("runs log unreadable")
    monkeypatch.setattr(tt, "gate_decision", boom)
    out = tt.run_gate(_pinned, TT, 16, now=datetime(2026, 11, 6, 5, 0),
                      records=[])
    assert out["verdict"] == "RUN" and "error" in out["why"]


def test_gate_args_quote_safely():
    assert tt.gate_args('Tracking test "A&B"', 12) == \
        'tracking_test --label="Tracking test AB" --subs=12'
