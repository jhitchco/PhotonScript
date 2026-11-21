"""PS-140: every subs-log writer goes through runs.edit_subs.

A manual verdict given while a writer runs must survive it. Metadata passes
hold the per-night lock from load to rewrite (the verdict waits, then lands
on the written log); writers with slow work (identify, Piggy attribution,
the pointing pass) run unlocked and merge only the fields they changed under
the lock (merge_subs: a field changed meanwhile keeps its new value, the
verdict fields move as one group). Every thread is joined with a timeout,
so a deadlock fails the test instead of hanging the suite.
"""
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest

from photonscript.scheduler import runs
from tests.test_scheduler.test_ps21_grading import NIGHT, _cfg, _rec, _seed

JOIN_S = 10.0     # a deadlock fails here instead of hanging the suite
HOLD_PEEK_S = 0.3  # how long a locked writer is given to (wrongly) let it in


@pytest.fixture
def quiet(monkeypatch):
    """No goal-store or Library side effects from the writers / verdicts."""
    monkeypatch.setattr(runs, "sync_goal_progress", lambda cfg: [])
    monkeypatch.setattr(runs, "build_library", lambda cfg, d=None: {})
    yield
    runs.flush_goal_sync()


class Verdict:
    """A person's verdict, given from another thread while a writer runs.

    fire() is called from inside the writer (once). With a locked writer the
    verdict must still be waiting when fire() returns (the lock is held);
    with a merging writer it completes during the run."""

    def __init__(self, cfg, file, state="rejected"):
        self.cfg, self.file, self.state = cfg, file, state
        self.t = None
        self.done_during_run = None

    def _give(self):
        runs.set_verdicts(self.cfg, NIGHT, [self.file], self.state,
                          why="ps140", defer_goal_sync=True)

    def fire(self, wait=HOLD_PEEK_S):
        if self.t is not None:
            return
        self.t = threading.Thread(target=self._give, daemon=True)
        self.t.start()
        self.t.join(timeout=wait)
        self.done_during_run = not self.t.is_alive()

    def finish(self):
        assert self.t is not None, "the writer never reached the hook"
        self.t.join(timeout=JOIN_S)
        assert not self.t.is_alive(), "verdict thread deadlocked"

    def assert_kept(self, rec):
        assert rec["manual_qa"] is (self.state != "review")
        assert rec["review_source"] == "manual"
        if self.state == "rejected":
            assert rec["passed_qa"] is False
            assert rec["reason"] == "rejected manually: ps140"
        elif self.state == "accepted":
            assert rec["passed_qa"] is True and rec["reason"] == ""


def _by_file(cfg):
    return {r["file"]: r for r in runs._load_subs(cfg, NIGHT)}


def _run(fn, *a, **kw):
    """Run a writer in a thread with a timeout (deadlock guard)."""
    box = {}

    def go():
        try:
            box["out"] = fn(*a, **kw)
        except BaseException as e:  # noqa: BLE001 - re-raised below
            box["err"] = e
    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(timeout=JOIN_S)
    assert not t.is_alive(), f"{getattr(fn, '__name__', fn)} deadlocked"
    if "err" in box:
        raise box["err"]
    return box.get("out")


# ------------------------------------------------------------ edit_subs

def test_edit_subs_holds_the_lock_and_writes_once(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("a"), _rec("b")])
    appended = threading.Event()

    def appender():
        runs.append_sub_record(cfg, NIGHT, _rec("late"))
        appended.set()

    with runs.edit_subs(cfg, NIGHT) as subs:
        subs[0]["target"] = "M31"
        t = threading.Thread(target=appender, daemon=True)
        t.start()
        t.join(timeout=HOLD_PEEK_S)
        assert not appended.is_set()        # the live grader waits ...
    t.join(timeout=JOIN_S)
    assert appended.is_set()                # ... and lands after the rewrite
    by = _by_file(cfg)
    assert set(by) == {"LIGHT/a.fits", "LIGHT/b.fits", "LIGHT/late.fits"}
    assert by["LIGHT/a.fits"]["target"] == "M31"


def test_edit_subs_without_a_change_or_on_an_error_writes_nothing(
        tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("a")])
    writes = []
    real = runs._rewrite_subs
    monkeypatch.setattr(runs, "_rewrite_subs",
                        lambda *a: writes.append(1) or real(*a))
    with runs.edit_subs(cfg, NIGHT) as subs:
        assert len(subs) == 1
    with pytest.raises(RuntimeError):
        with runs.edit_subs(cfg, NIGHT) as subs:
            subs[0]["target"] = "half done"
            raise RuntimeError("boom")
    with runs.edit_subs(cfg, NIGHT, write=False) as subs:
        subs[0]["target"] = "dry run"
    with runs.edit_subs(cfg, NIGHT, hold_lock=False, write=False) as subs:
        subs[0]["target"] = "dry run"
    assert writes == []
    assert _by_file(cfg)["LIGHT/a.fits"]["target"] == "T"


def test_edit_subs_is_reentrant_in_one_thread(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("a")])

    def nested():
        with runs.edit_subs(cfg, NIGHT) as subs:
            subs[0]["target"] = "outer"
            runs.append_sub_record(cfg, NIGHT, _rec("inner"))
    _run(nested)
    by = _by_file(cfg)
    assert by["LIGHT/a.fits"]["target"] == "outer" and "LIGHT/inner.fits" in by


def test_busy_file_retry_is_kept_through_edit_subs(tmp_path, monkeypatch):
    import os
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("a")])
    real, tries = os.replace, []

    def flaky(src, dst):
        tries.append(1)
        if len(tries) < 3:
            raise PermissionError("[WinError 5] Access is denied")
        return real(src, dst)
    monkeypatch.setattr(os, "replace", flaky)
    with runs.edit_subs(cfg, NIGHT) as subs:
        subs[0]["reviewed"] = True
    assert len(tries) == 3 and _by_file(cfg)["LIGHT/a.fits"]["reviewed"] is True


# ------------------------------------------------------------ merge_subs

def test_merge_writes_only_changed_fields_and_protects_a_verdict(tmp_path,
                                                                quiet):
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("a"), _rec("b"), _rec("c")])
    with runs.edit_subs(cfg, NIGHT, hold_lock=False) as subs:
        # slow work happens here; meanwhile a person and the live grader act
        runs.set_verdicts(cfg, NIGHT, ["LIGHT/a.fits"], "accepted",
                          defer_goal_sync=True)
        runs.append_sub_record(cfg, NIGHT, _rec("late"))
        with runs.edit_subs(cfg, NIGHT) as other:   # another writer
            other[2]["target"] = "M33"
        a, b, c = subs
        a.update(passed_qa=False, reason="stale auto reject", score=12)
        b["target"] = "M31"
        b.pop("ecc")
        c["target"] = "stale"
    by = _by_file(cfg)
    a = by["LIGHT/a.fits"]
    assert a["passed_qa"] is True and a["manual_qa"] is True    # verdict kept
    assert a["reason"] == "" and a["score"] == 12               # rest merged
    assert by["LIGHT/b.fits"]["target"] == "M31"
    assert "ecc" not in by["LIGHT/b.fits"]                      # removal too
    assert by["LIGHT/c.fits"]["target"] == "M33"     # changed meanwhile: kept
    assert "LIGHT/late.fits" in by                   # appended meanwhile


def test_merge_does_not_bring_back_a_wiped_record(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("a")])
    with runs.edit_subs(cfg, NIGHT, hold_lock=False) as subs:
        (runs.runs_dir(cfg) / f"{NIGHT}_subs.jsonl").unlink()   # re-grade
        runs.append_sub_record(cfg, NIGHT, _rec("b"))
        subs[0]["target"] = "M31"
    assert set(_by_file(cfg)) == {"LIGHT/b.fits"}


def test_merge_matches_duplicate_keys_in_order(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("a", hfr=1.0), _rec("a", hfr=2.0)])
    with runs.edit_subs(cfg, NIGHT, hold_lock=False) as subs:
        subs[1]["target"] = "second"
    recs = runs._load_subs(cfg, NIGHT)
    assert [(r["hfr"], r["target"]) for r in recs] == [(1.0, "T"),
                                                       (2.0, "second")]


# ------------------------------------------- each writer vs a verdict

def test_rescore_night(tmp_path, quiet, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("bad", ecc=0.66), _rec("ok")])
    v = Verdict(cfg, "LIGHT/bad.fits", "accepted")
    real = runs._night_cards
    monkeypatch.setattr(runs, "_night_cards",
                        lambda *a, **k: (v.fire(), real(*a, **k))[1])
    out = _run(runs.rescore_night, cfg, NIGHT, apply=True)
    v.finish()
    assert v.done_during_run is False          # held the lock to the rewrite
    assert out["counts"]["newly_rejected"] == 1
    v.assert_kept(_by_file(cfg)["LIGHT/bad.fits"])


def test_correlate_piggyback(tmp_path, quiet, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("r0", target="M31", time="2026-09-27T04:00:00"),
                _rec("p0", rig="piggyback", target="?",
                     time="2026-09-27T04:01:00")])
    v = Verdict(cfg, "LIGHT/p0.fits")
    real = runs.correlate_piggyback_records
    monkeypatch.setattr(runs, "correlate_piggyback_records",
                        lambda subs: (v.fire(), real(subs))[1])
    out = _run(runs.correlate_piggyback_targets, cfg, NIGHT)
    v.finish()
    assert v.done_during_run is False and out["attributed"] == 1
    rec = _by_file(cfg)["LIGHT/p0.fits"]
    assert rec["target"] == "M31"
    v.assert_kept(rec)


def test_assign_target_endpoint(tmp_path, quiet, monkeypatch):
    import asyncio

    from photonscript.scheduler import app as app_mod
    from photonscript.shared import target_names
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("a", target="?")])
    monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
    v = Verdict(cfg, "LIGHT/a.fits")
    real = target_names.canonical_target
    monkeypatch.setattr(target_names, "canonical_target",
                        lambda *a, **k: (v.fire(), real(*a, **k))[1])
    out = _run(asyncio.run, app_mod.api_run_assign_target(NIGHT, {"name": "M42"}))
    v.finish()
    assert v.done_during_run is False and out["updated"] == 1
    rec = _by_file(cfg)["LIGHT/a.fits"]
    assert rec["target"] == "M42" and rec["target_src"] == "manual"
    v.assert_kept(rec)


def test_identify_night(tmp_path, quiet, monkeypatch):
    from photonscript.scheduler import identify
    cfg = _cfg(tmp_path)
    f = tmp_path / "fits" / NIGHT / "u.fits"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    _seed(cfg, [_rec("u", target="?", abs_path=str(f),
                     time="2026-09-27T04:00:00")])
    v = Verdict(cfg, "LIGHT/u.fits")
    monkeypatch.setattr(identify, "_candidates", lambda c: [])
    monkeypatch.setattr(identify, "_maybe_stamp", lambda *a: None)
    monkeypatch.setattr(identify, "match_target", lambda ra, dec, c: "M31")
    monkeypatch.setattr(identify, "_header_radec",
                        lambda p: (v.fire(JOIN_S), (10.68, 41.27))[1])
    out = _run(identify.identify_night, cfg, NIGHT, solve=False)
    v.finish()
    assert v.done_during_run is True           # header reads run unlocked
    assert out["identified"] == 1
    rec = _by_file(cfg)["LIGHT/u.fits"]
    assert rec["target"] == "M31"
    v.assert_kept(rec)


def _fake_piggy(v):
    def attribute_records(config, date, subs, apply=False, solve=False,
                          runner=None, projects=None, max_solves=30):
        v.fire(JOIN_S)
        s = subs[0]
        s["target_attr"] = {"name": "NGC 7000", "applied": bool(apply)}
        changed = [{"file": s["file"], "filter": s.get("filter"),
                    "from": s.get("target"), "to": "NGC 7000",
                    "src": "solve", "abs_path": None, "passed_qa": True}]
        if apply:
            s["target"] = "NGC 7000"
            s["target_src"] = "piggy-frame"
        return {"piggy": 1, "placed": 1, "kept": 0, "no_goal": 0,
                "no_position": 0, "manual": 0, "changed": changed,
                "attr_written": 1}
    return attribute_records


def test_attribute_piggy_night(tmp_path, quiet, monkeypatch):
    from photonscript.scheduler import piggy_attribution as pa
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("p", rig="piggyback", target="M31")])
    v = Verdict(cfg, "LIGHT/p.fits")
    monkeypatch.setattr(pa, "attribute_records", _fake_piggy(v))
    out = _run(pa.attribute_piggy_night, cfg, NIGHT, apply=True)
    v.finish()
    assert v.done_during_run is True and out["renamed"] == 1
    rec = _by_file(cfg)["LIGHT/p.fits"]
    assert rec["target"] == "NGC 7000" and rec["target_attr"]["applied"]
    v.assert_kept(rec)


def test_piggy_reattribute_cli(tmp_path, quiet, monkeypatch):
    from photonscript.scheduler import piggy_attribution as pa
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("p", rig="piggyback", target="M31")])
    v = Verdict(cfg, "LIGHT/p.fits")
    monkeypatch.setattr(pa, "attribute_records", _fake_piggy(v))
    out = _run(pa.reattribute, cfg, [NIGHT], apply=True)
    v.finish()
    assert v.done_during_run is True and out["subs_changed"] == 1
    rec = _by_file(cfg)["LIGHT/p.fits"]
    assert rec["target"] == "NGC 7000"
    v.assert_kept(rec)


def test_pointing_night_pass(tmp_path, quiet, monkeypatch):
    from photonscript.scheduler import pointing_record as pr
    from photonscript.scheduler import solve_store
    from photonscript.shared import mount_log, pointing, qa_rules
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("a")])
    v = Verdict(cfg, "LIGHT/a.fits", "accepted")
    t0 = datetime(2026, 9, 27, 4, 0, 0)

    def frames(config, subs):
        v.fire(JOIN_S)          # the slow part (headers, solves) is unlocked
        return {"rc16": [{"rec": subs[0], "start": t0, "exp_s": 300.0,
                          "mid": t0}]}
    monkeypatch.setattr(pr, "_frames", frames)
    monkeypatch.setattr(pr, "_move_out_of_library", lambda c, r: [])
    monkeypatch.setattr(pointing, "load", lambda c, d: {})
    monkeypatch.setattr(pointing, "sub_pointing", lambda *a, **k: {})
    monkeypatch.setattr(pointing, "summarize", lambda pts: {})
    monkeypatch.setattr(mount_log, "load", lambda c, d: [])
    monkeypatch.setattr(solve_store, "lookup", lambda c, d, rig: {})
    monkeypatch.setattr(qa_rules, "regrade_pointing", lambda *a: {
        "passed_qa": False, "reason": "off target (stale)",
        "auto_verdict": "rejected", "drivers": ["pointing"],
        "pointing_src": "mount-log", "off_target_arcmin": 40.0})
    out = _run(pr.night_pass, cfg, NIGHT, solve=False)
    v.finish()
    assert v.done_during_run is True and out["newly_rejected"] == 1
    rec = _by_file(cfg)["LIGHT/a.fits"]
    v.assert_kept(rec)                          # the stale reject is dropped
    assert rec.get("drivers") != ["pointing"]   # verdict group moved as one
    assert rec["off_target_arcmin"] == 40.0     # the measurement is merged


def test_slew_gate_night_pass(tmp_path, quiet, monkeypatch):
    from photonscript.scheduler import flexure, pointing_record
    from photonscript.scheduler import slew_gate as sg
    from photonscript.shared import mount_log, qa_rules
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("p", rig="piggyback")])
    v = Verdict(cfg, "LIGHT/p.fits", "accepted")
    t0 = datetime(2026, 9, 27, 4, 0, 0)

    class NW:
        def __init__(self, *a, **k):
            pass

        def assess(self, start, end):
            return {"overlap_s": 5.0, "src": "mount-log", "note": None}
    monkeypatch.setattr(sg, "NightWindows", NW)
    monkeypatch.setattr(sg, "gated_rigs", lambda c: ["piggyback"])
    monkeypatch.setattr(mount_log, "load", lambda c, d: [])
    monkeypatch.setattr(pointing_record, "_move_out_of_library",
                        lambda c, r: [])
    monkeypatch.setattr(flexure, "frames_from_records", lambda subs, c, rig, **k: (
        v.fire(), [{"rec": subs[0], "start": t0, "end": t0}])[1])
    monkeypatch.setattr(qa_rules, "regrade_slew_straddle", lambda *a: {
        "passed_qa": False, "reason": "slew", "auto_verdict": "rejected",
        "drivers": ["slew_straddle"], "slew_overlap_s": 5.0})
    out = _run(sg.night_pass, cfg, NIGHT)
    v.finish()
    assert v.done_during_run is False and out["newly_rejected"] == 1
    rec = _by_file(cfg)["LIGHT/p.fits"]
    v.assert_kept(rec)
    assert rec["slew_overlap_s"] == 5.0


def test_qa_backfill_regrade_parked(tmp_path, quiet, monkeypatch):
    from photonscript.scheduler import qa_backfill
    from photonscript.shared import qa_signatures, safety_history
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("a", time="2026-09-27T04:00:00")])
    v = Verdict(cfg, "LIGHT/a.fits", "accepted")
    monkeypatch.setattr(safety_history, "unsafe_windows",
                        lambda *a, **k: ([], "none"))
    monkeypatch.setattr(qa_signatures, "parked_frame_verdict", lambda *a, **k: (
        v.fire(), SimpleNamespace(reject=True, reasons=["parked"],
                                  flag="parked"))[1])
    out = _run(qa_backfill.regrade_parked, cfg, NIGHT, apply=True)
    v.finish()
    assert v.done_during_run is False and len(out["rejects"]) == 1
    rec = _by_file(cfg)["LIGHT/a.fits"]
    v.assert_kept(rec)
    assert rec["regraded"] == "PS-71"


def test_qa_remeasure_apply(tmp_path, quiet, monkeypatch):
    from photonscript.scheduler import qa_remeasure
    cfg = _cfg(tmp_path)
    f = tmp_path / "fits" / NIGHT / "LIGHT" / "a.fits"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    _seed(cfg, [_rec("a", graded_by="backfill", hfr=13.0, abs_path=str(f)),
                _rec("b")])
    v = Verdict(cfg, "LIGHT/a.fits")

    def measure(config, path, rig):
        v.fire(JOIN_S)          # measuring runs unlocked
        return {"hfr": 6.5, "measure_v": 2, "graded_by": "backfill"}, None
    out = _run(qa_remeasure.remeasure_night, cfg, NIGHT, apply=True,
               measure=measure)
    v.finish()
    assert v.done_during_run is True and out["records_written"] == 1
    rec = _by_file(cfg)["LIGHT/a.fits"]
    assert rec["hfr"] == 6.5 and rec["remeasured"] == 2
    v.assert_kept(rec)


def test_target_rename_backfill(tmp_path, quiet, monkeypatch):
    from collections import Counter

    from photonscript.scheduler import target_backfill as tb
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("a", target="OSC_LIGHT_LOOP_Container")])
    v = Verdict(cfg, "LIGHT/a.fits")

    def rewrite(subs, known):
        v.fire()
        subs[0]["target_raw"] = subs[0]["target"]
        subs[0]["target"] = "M31"
        return Counter({("rc16", "OSC_LIGHT_LOOP_Container", "M31"): 1})
    monkeypatch.setattr(tb, "_rewrite_records", rewrite)
    _run(tb.rename_backfill, cfg, apply=True, dates=[NIGHT])
    v.finish()
    assert v.done_during_run is False
    rec = _by_file(cfg)["LIGHT/a.fits"]
    assert rec["target"] == "M31"
    v.assert_kept(rec)


def test_no_writer_bypasses_edit_subs():
    """Only edit_subs / merge_subs call _rewrite_subs (static check)."""
    from pathlib import Path
    root = Path(runs.__file__).resolve().parents[1]
    hits = []
    for p in root.rglob("*.py"):
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            if "_rewrite_subs(" in line and "def _rewrite_subs" not in line:
                hits.append((p.name, line.strip()))
    assert sorted(h[1] for h in hits) == sorted([
        "_rewrite_subs(config, date, subs)",
        "_rewrite_subs(config, date, fresh)"]), hits
    assert {h[0] for h in hits} == {"runs.py"}
