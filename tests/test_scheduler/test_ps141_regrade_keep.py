"""PS-141: a re-grade keeps manual verdicts; the pointing pass keeps the
Library in step with a verdict given during the pass.

Re-grade (POST /api/runs/{date}/regrade, re-grade all, CLI `regrade`)
re-measures and re-judges every sub but replaces each record in place
(runs.replace_graded, under the night lock): a sub a person decided keeps
every MANUAL_FIELDS field, and a target assigned by hand stays. The old wipe
is `discard_manual`. The pointing pass (unlocked, merged) decides its
Library moves from the merged log under the lock, so a sub a person accepts
during the pass keeps its Library link.
"""
import asyncio
import threading
import time
from datetime import datetime

import pytest

from photonscript.scheduler import runs
from photonscript.scheduler.qa_remeasure import MANUAL_FIELDS
from tests.test_scheduler.test_ps140_subs_lock_all import (JOIN_S, Verdict,
                                                           _by_file, _run)
from tests.test_scheduler.test_ps21_grading import NIGHT, _cfg, _rec, _seed


@pytest.fixture
def quiet(monkeypatch):
    """No goal-store, Library or post-pass side effects."""
    monkeypatch.setattr(runs, "sync_goal_progress", lambda cfg: [])
    monkeypatch.setattr(runs, "build_library", lambda cfg, d=None: {})
    monkeypatch.setattr(runs, "attribute_night", lambda *a, **k: {})
    monkeypatch.setattr(runs, "flag_hfr_outliers", lambda *a, **k: 0)
    from photonscript.scheduler import (flexure, focus_model, focus_reports,
                                        focus_seeds, optics_report,
                                        piggy_attribution, pointing_record,
                                        slew_gate, trends)
    monkeypatch.setattr(pointing_record, "night_pass", lambda *a, **k: {})
    monkeypatch.setattr(slew_gate, "night_pass", lambda *a, **k: {})
    monkeypatch.setattr(focus_seeds, "harvest_night", lambda *a, **k: 0)
    monkeypatch.setattr(optics_report, "night_optics", lambda *a, **k: {
        "overall": {"verdict": "-", "n_measured": 0}})
    monkeypatch.setattr(trends, "check_and_alert", lambda *a, **k: {})
    monkeypatch.setattr(focus_reports, "check_and_alert", lambda *a, **k: {})
    monkeypatch.setattr(focus_model, "ingest_af_reports", lambda *a, **k: {})
    monkeypatch.setattr(flexure, "build_report", lambda *a, **k: {})
    monkeypatch.setattr(piggy_attribution, "attribute_piggy_night",
                        lambda *a, **k: {})
    yield
    runs.flush_goal_sync()
    runs._backfill_state.pop(NIGHT, None)


def _lights(tmp_path, *names):
    d = tmp_path / "fits" / NIGHT / "LIGHT"
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / f"{n}.fits").write_bytes(b"x")


def _fresh(name, **kw):
    """What the grader says now (runs._fast_grade without the file / path)."""
    r = {"rig": "rc16", "target": "T", "filter": "Ha", "exp_s": 300.0,
         "hfr": 3.1, "stars": 900, "ecc": 0.3, "measure_v": 9,
         "graded_by": "backfill", "passed_qa": False,
         "reason": "new rules reject", "auto_verdict": "rejected",
         "auto_reason": "new rules reject", "drivers": ["hfr"],
         "scorecard": {"verdict": "rejected", "rows": [["hfr", 3.1]]}}
    r.update(kw)
    return r


def _fake_grader(hook=None, **per_file):
    def grade(path, config, plan_names=None, prewarm=None, stars_to=None):
        name = path.stem
        if hook:
            hook(name)
        return _fresh(name, **per_file.get(name, {}))
    return grade


def _wait_backfill():
    t0 = time.monotonic()
    while runs._backfill_state.get(NIGHT, {}).get("running"):
        assert time.monotonic() - t0 < JOIN_S, "backfill did not finish"
        time.sleep(0.02)


def _slash(f):
    return str(f).replace("\\", "/")


def _manual(name, state="accepted"):
    r = _rec(name)
    runs._apply_verdict(r, state, "visual" if state != "review" else None,
                        "2026-09-27T10:00:00Z")
    return r


# ------------------------------------------------------------ replace_graded

def test_replace_graded_keeps_a_person_verdict_and_replaces_the_rest(tmp_path):
    cfg = _cfg(tmp_path)
    human = _manual("h", "accepted")
    human.update(target="M42", target_src="manual", hfr=9.9, old_field=1)
    _seed(cfg, [human, _rec("auto", stale=True), _rec("other")])
    out = runs.replace_graded(cfg, NIGHT, {
        "LIGHT/h.fits": {**_fresh("h"), "file": "LIGHT/h.fits"},
        "LIGHT/auto.fits": {**_fresh("auto"), "file": "LIGHT/auto.fits"}})
    assert out == {"replaced": 2, "manual_kept": 1}
    recs = runs._load_subs(cfg, NIGHT)
    assert [r["file"] for r in recs] == ["LIGHT/h.fits", "LIGHT/auto.fits",
                                         "LIGHT/other.fits"]   # order kept
    h, a, o = recs
    for k in MANUAL_FIELDS:                    # the person's verdict group
        assert h.get(k) == human.get(k), k
    assert h["passed_qa"] is True and h["manual_qa"] is True
    assert h["review_source"] == "manual" and h["manual_reason"] == "visual"
    assert h["hfr"] == 3.1 and h["measure_v"] == 9     # re-measured
    assert h["scorecard"]["verdict"] == "rejected"     # the new card shows
    assert h["target"] == "M42" and h["target_src"] == "manual"
    assert "old_field" not in h                # nothing else survives
    assert a["passed_qa"] is False and a["reason"] == "new rules reject"
    assert a["target"] == "T" and "stale" not in a     # auto: fully replaced
    assert o == _rec("other")                  # not in `fresh`: untouched


def test_replace_graded_review_state_kept_auto_approval_not(tmp_path):
    cfg = _cfg(tmp_path)
    review = _manual("r", "review")            # sent back to review by hand
    approved = _rec("ap", reviewed=True)       # Approve night (reviewed)
    auto = _rec("aa", reviewed=True, review_source="auto")  # all green
    _seed(cfg, [review, approved, auto])
    fresh = {f"LIGHT/{n}.fits": {**_fresh(n), "file": f"LIGHT/{n}.fits"}
             for n in ("r", "ap", "aa")}
    out = runs.replace_graded(cfg, NIGHT, fresh)
    assert out == {"replaced": 3, "manual_kept": 2}
    by = _by_file(cfg)
    r = by["LIGHT/r.fits"]
    assert r["review_source"] == "manual" and r["reviewed"] is False
    assert r["passed_qa"] is True and "manual_reason" not in r
    assert by["LIGHT/ap.fits"]["reviewed"] is True
    assert by["LIGHT/ap.fits"]["passed_qa"] is True
    aa = by["LIGHT/aa.fits"]
    assert aa["passed_qa"] is False and "review_source" not in aa
    assert "reviewed" not in aa


def test_replace_graded_holds_the_lock(tmp_path, monkeypatch):
    """A verdict given while the re-graded records are written waits for the
    rewrite and then lands on the new record."""
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("a")])
    v = Verdict(cfg, "LIGHT/a.fits", "rejected")
    real = runs._human_verdict
    monkeypatch.setattr(runs, "_human_verdict",
                        lambda rec: (v.fire(), real(rec))[1])
    out = _run(runs.replace_graded, cfg, NIGHT,
               {"LIGHT/a.fits": {**_fresh("a", passed_qa=True, reason=""),
                                 "file": "LIGHT/a.fits"}})
    monkeypatch.setattr(runs, "_human_verdict", real)
    v.finish()
    assert v.done_during_run is False and out["replaced"] == 1
    rec = _by_file(cfg)["LIGHT/a.fits"]
    assert rec["measure_v"] == 9
    v.assert_kept(rec)


# ------------------------------------------------------------ regrade_night

def test_regrade_night_keeps_verdicts_and_appends_new_subs(tmp_path, quiet,
                                                           monkeypatch):
    cfg = _cfg(tmp_path)
    _lights(tmp_path, "a", "b", "c")
    _seed(cfg, [_manual("a", "rejected"), _rec("b")])   # c is new
    monkeypatch.setattr(runs, "_fast_grade", _fake_grader(
        c={"passed_qa": True, "reason": ""}))
    monkeypatch.setattr(runs, "REGRADE_FLUSH_N", 1)     # several flushes
    ann = tmp_path / "data" / "thumbs" / NIGHT / "x.ann.png"
    ann.parent.mkdir(parents=True)
    ann.write_bytes(b"x")
    res = runs.regrade_night(cfg, NIGHT)
    assert res == {"date": NIGHT, "records": 2, "mode": "keep_manual",
                   "started": True}
    _wait_backfill()
    assert not ann.exists()                    # star overlays dropped
    recs = runs._load_subs(cfg, NIGHT)
    # the backfill writes the OS separator; a replaced record keeps its own
    assert [r["file"] for r in recs][:2] == ["LIGHT/a.fits", "LIGHT/b.fits"]
    assert _slash(recs[2]["file"]) == "LIGHT/c.fits" and len(recs) == 3
    a, b, c = recs
    assert a["passed_qa"] is False and a["manual_qa"] is True
    assert a["reason"] == "rejected manually: visual"
    assert a["measure_v"] == 9 and a["abs_path"].endswith("a.fits")
    assert b["passed_qa"] is False and b["measure_v"] == 9
    assert c["passed_qa"] is True
    st = runs._backfill_state[NIGHT]
    assert st["regrade_kept"] == {"replaced": 2, "manual_kept": 1}
    assert st["regrade_done"] == st["regrade_total"] == 3


def test_regrade_night_verdict_given_while_measuring_is_kept(tmp_path, quiet,
                                                            monkeypatch):
    """Measuring runs unlocked: a verdict given then completes at once and
    the replace (under the lock) keeps it."""
    cfg = _cfg(tmp_path)
    _lights(tmp_path, "a", "b")
    _seed(cfg, [_rec("a"), _rec("b")])
    v = Verdict(cfg, "LIGHT/a.fits", "accepted")
    monkeypatch.setattr(runs, "_fast_grade", _fake_grader(
        hook=lambda name: v.fire(JOIN_S) if name == "b" else None))
    runs.regrade_night(cfg, NIGHT)
    _wait_backfill()
    v.finish()
    assert v.done_during_run is True
    a = _by_file(cfg)["LIGHT/a.fits"]
    v.assert_kept(a)                           # not the fresh reject
    assert a["measure_v"] == 9


def test_regrade_status_reports_regrade_progress(tmp_path, quiet,
                                                 monkeypatch):
    cfg = _cfg(tmp_path)
    _lights(tmp_path, "a", "b")
    _seed(cfg, [_rec("a"), _rec("b")])
    gate, seen = threading.Event(), {}

    def hook(name):
        if name == "b":
            seen.update(runs.backfill_status(cfg, NIGHT))
            gate.set()
    monkeypatch.setattr(runs, "_fast_grade", _fake_grader(hook=hook))
    runs.regrade_night(cfg, NIGHT)
    assert gate.wait(JOIN_S)
    _wait_backfill()
    assert seen["running"] is True and seen["total_files"] == 2
    assert seen["graded"] == 1 and seen["pending"] == 1


def test_regrade_night_discard_manual_is_the_old_wipe(tmp_path, quiet,
                                                      monkeypatch):
    cfg = _cfg(tmp_path)
    _lights(tmp_path, "a")
    _seed(cfg, [_manual("a", "accepted"), _rec("gone")])
    monkeypatch.setattr(runs, "_fast_grade", _fake_grader())
    res = runs.regrade_night(cfg, NIGHT, discard_manual=True)
    assert res["mode"] == "discard_manual" and res["started"] is True
    _wait_backfill()
    recs = runs._load_subs(cfg, NIGHT)
    assert [_slash(r["file"]) for r in recs] == ["LIGHT/a.fits"]
    assert recs[0]["passed_qa"] is False and "manual_qa" not in recs[0]


def test_regrade_night_refused_while_grading(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg, [_manual("a")])
    monkeypatch.setitem(runs._backfill_state, NIGHT, {"running": True})
    res = runs.regrade_night(cfg, NIGHT, discard_manual=True)
    assert res["started"] is False
    assert len(runs._load_subs(cfg, NIGHT)) == 1   # nothing deleted


def test_regrade_all_keeps_by_default(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    for d in ("2026-09-25", NIGHT):
        (tmp_path / "fits" / d).mkdir(parents=True)
    calls = []
    monkeypatch.setattr(runs, "regrade_night",
                        lambda c, d, discard_manual=False: calls.append(
                            (d, discard_manual)))
    monkeypatch.setattr(runs, "sync_goal_progress", lambda cfg: [])
    for dm in (False, True):
        runs.start_regrade_all(cfg, since=NIGHT, discard_manual=dm)
        t0 = time.monotonic()
        while runs._regrade_all.get("running"):
            assert time.monotonic() - t0 < JOIN_S
            time.sleep(0.02)
    assert calls == [(NIGHT, False), (NIGHT, True)]
    assert runs.regrade_all_status()["discard_manual"] is True


def test_regrade_endpoints_pass_discard_manual(tmp_path, monkeypatch):
    from photonscript.scheduler import app as app_mod
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
    calls = []
    monkeypatch.setattr(runs, "regrade_night",
                        lambda c, d, discard_manual=False: calls.append(
                            ("night", d, discard_manual)) or {"started": True})
    monkeypatch.setattr(runs, "start_regrade_all",
                        lambda c, since="", discard_manual=False: calls.append(
                            ("all", since, discard_manual)) or {})
    out = asyncio.run(app_mod.api_run_regrade(NIGHT, {}))
    assert out == {"ok": True, "started": True}
    asyncio.run(app_mod.api_run_regrade(NIGHT, {"discard_manual": True}))
    asyncio.run(app_mod.api_run_regrade(NIGHT, {}, discard_manual=True))
    asyncio.run(app_mod.api_regrade_all({"since": "2026-09-01"}))
    asyncio.run(app_mod.api_regrade_all({"discard_manual": True}))
    assert calls == [("night", NIGHT, False), ("night", NIGHT, True),
                     ("night", NIGHT, True), ("all", "2026-09-01", False),
                     ("all", "", True)]


def test_regrade_cli(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from photonscript import cli
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)
    calls = []
    monkeypatch.setattr(runs, "regrade_night",
                        lambda c, d, discard_manual=False: calls.append(
                            (d, discard_manual)) or {
                            "date": d, "records": 3, "started": True,
                            "mode": "keep_manual"})
    runner = CliRunner()
    r = runner.invoke(cli.app, ["regrade", "--date", NIGHT])
    assert r.exit_code == 0, r.output
    assert "3 record(s) before" in r.output
    r = runner.invoke(cli.app, ["regrade", "--date", NIGHT,
                                "--discard-manual"], input="n\n")
    assert r.exit_code != 0                     # the confirm said no
    r = runner.invoke(cli.app, ["regrade", "--date", NIGHT,
                                "--discard-manual", "--yes"])
    assert r.exit_code == 0, r.output
    assert calls == [(NIGHT, False), (NIGHT, True)]
    r = runner.invoke(cli.app, ["regrade"])
    assert r.exit_code != 0                     # needs --date or --since


# ------------------------------------------------------------ pointing pass

def _pointing_setup(monkeypatch, cfg, fire=None):
    from photonscript.scheduler import pointing_record as pr
    from photonscript.scheduler import solve_store
    from photonscript.shared import mount_log, pointing, qa_rules
    t0 = datetime(2026, 9, 27, 4, 0, 0)

    def frames(config, subs):
        if fire:
            fire()              # the slow part (headers, solves) is unlocked
        return {"rc16": [{"rec": subs[0], "start": t0, "exp_s": 300.0,
                          "mid": t0}]}
    monkeypatch.setattr(pr, "_frames", frames)
    monkeypatch.setattr(pointing, "load", lambda c, d: {})
    monkeypatch.setattr(pointing, "sub_pointing", lambda *a, **k: {})
    monkeypatch.setattr(pointing, "summarize", lambda pts: {})
    monkeypatch.setattr(mount_log, "load", lambda c, d: [])
    monkeypatch.setattr(solve_store, "lookup", lambda c, d, rig: {})
    monkeypatch.setattr(qa_rules, "regrade_pointing", lambda *a: {
        "passed_qa": False, "reason": "off target", "auto_verdict": "rejected",
        "drivers": ["pointing"], "pointing_src": "solve",
        "off_target_arcmin": 40.0})
    return pr


def _linked(tmp_path):
    link = tmp_path / "lib" / "T" / "Ha" / "a.fits"
    link.parent.mkdir(parents=True)
    link.write_bytes(b"x")
    return link, tmp_path / "lib" / "_rejected" / "T" / "Ha" / "a.fits"


def test_pointing_accept_during_pass_keeps_the_library_link(tmp_path,
                                                            monkeypatch):
    monkeypatch.setattr(runs, "sync_goal_progress", lambda cfg: [])
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("a")])
    link, rejected = _linked(tmp_path)
    v = Verdict(cfg, "LIGHT/a.fits", "accepted")
    pr = _pointing_setup(monkeypatch, cfg, fire=lambda: v.fire(JOIN_S))
    out = _run(pr.night_pass, cfg, NIGHT, solve=False)
    v.finish()
    runs.flush_goal_sync()
    assert v.done_during_run is True and out["newly_rejected"] == 1
    v.assert_kept(_by_file(cfg)["LIGHT/a.fits"])
    assert link.exists() and not rejected.exists()   # in step with the accept
    assert out["library_moves"] == 0 and out["library_moves_skipped"] == 1


def test_pointing_reject_without_a_verdict_still_moves_the_link(tmp_path,
                                                                monkeypatch):
    monkeypatch.setattr(runs, "sync_goal_progress", lambda cfg: [])
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("a")])
    link, rejected = _linked(tmp_path)
    pr = _pointing_setup(monkeypatch, cfg)
    out = _run(pr.night_pass, cfg, NIGHT, solve=False)
    assert _by_file(cfg)["LIGHT/a.fits"]["passed_qa"] is False
    assert not link.exists() and rejected.exists()
    assert out["library_moves"] == 1 and out["library_moves_skipped"] == 0


def test_pointing_moves_are_decided_under_the_lock(tmp_path, monkeypatch):
    """A verdict given while the moves are decided waits for them."""
    monkeypatch.setattr(runs, "sync_goal_progress", lambda cfg: [])
    cfg = _cfg(tmp_path)
    _lights(tmp_path, "a")
    _seed(cfg, [_rec("a", abs_path=str(
        tmp_path / "fits" / NIGHT / "LIGHT" / "a.fits"))])
    link, rejected = _linked(tmp_path)
    v = Verdict(cfg, "LIGHT/a.fits", "accepted")
    pr = _pointing_setup(monkeypatch, cfg)
    real = pr._move_out_of_library
    monkeypatch.setattr(pr, "_move_out_of_library",
                        lambda c, r: (v.fire(), real(c, r))[1])
    out = _run(pr.night_pass, cfg, NIGHT, solve=False)
    v.finish()
    runs.flush_goal_sync()
    assert v.done_during_run is False and out["library_moves"] == 1
    v.assert_kept(_by_file(cfg)["LIGHT/a.fits"])
    # the accept came after the move: its own Library follow re-links it
    assert rejected.exists() and link.exists()
