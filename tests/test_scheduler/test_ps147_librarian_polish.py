"""PS-147: Librarian polish.

1. One path separator for a sub's `file`: writers store "/", every reader
   normalizes (shared.sub_file), so a log holding both forms (backslash
   records from before PS-147, "/" ones after) works everywhere.
2. Piggy attribution (reattribute) decides its Library moves from the merged
   log under the night lock, so a target a person assigned during the
   unlocked pass keeps its links.
3. A sub accepted after a move put its link in Library/_rejected loses that
   stale _rejected copy (only the same file as its original or its live
   link; never in the desktop mirror).
"""
import json
import os
import threading

import pytest

from photonscript.scheduler import piggy_attribution as pa
from photonscript.scheduler import runs
from photonscript.shared.sub_file import norm_file, norm_record
from tests.test_scheduler.test_ps21_grading import NIGHT, _cfg, _rec, _seed


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(runs, "sync_goal_progress", lambda cfg: [])
    yield
    runs.flush_goal_sync()


def _log(cfg):
    return runs.runs_dir(cfg) / f"{NIGHT}_subs.jsonl"


def _raw_lines(cfg, recs):
    """Write records verbatim (no normalization), as an old log holds them."""
    p = _log(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    runs._invalidate_subs_cache(p)


def _files_on_disk(cfg):
    return [json.loads(x)["file"] for x in
            _log(cfg).read_text(encoding="utf-8").splitlines()]


# ------------------------------------------------------------ 1. separator

def test_norm_helpers():
    assert norm_file("LIGHT\\M31\\a.fits") == "LIGHT/M31/a.fits"
    assert norm_file("LIGHT/a.fits") == "LIGHT/a.fits"
    assert norm_file(None) == "" and norm_file("") == ""
    r = {"file": "A\\b.fits"}
    assert norm_record(r) is r and r["file"] == "A/b.fits"
    assert norm_record({"time": "t"}) == {"time": "t"}


def test_mixed_log_reads_writes_and_matches_in_one_form(tmp_path, quiet):
    cfg = _cfg(tmp_path)
    _raw_lines(cfg, [_rec("a", file="LIGHT\\a.fits"),
                     _rec("b", file="LIGHT/b.fits")])
    assert [r["file"] for r in runs._load_subs(cfg, NIGHT)] == [
        "LIGHT/a.fits", "LIGHT/b.fits"]
    # a verdict by either spelling finds either record
    res = runs.set_verdicts(cfg, NIGHT, ["LIGHT/a.fits", "LIGHT\\b.fits"],
                            "rejected")
    assert len(res["subs"]) == 2 and res["missing"] == []
    by = {r["file"]: r for r in runs._load_subs(cfg, NIGHT)}
    assert by["LIGHT/a.fits"]["manual_qa"] and by["LIGHT/b.fits"]["manual_qa"]
    # the rewrite migrated the old line to the canonical form
    assert _files_on_disk(cfg) == ["LIGHT/a.fits", "LIGHT/b.fits"]
    # a new record with the OS separator is stored with "/"
    runs.append_sub_record(cfg, NIGHT, _rec("c", file="LIGHT\\c.fits"))
    assert _files_on_disk(cfg)[-1] == "LIGHT/c.fits"
    assert runs.set_manual_qa(cfg, NIGHT, "LIGHT\\c.fits",
                              state="accepted")["file"] == "LIGHT/c.fits"


def test_approve_and_record_keys_match_either_separator(tmp_path, quiet,
                                                       monkeypatch):
    monkeypatch.setattr(runs, "build_library", lambda cfg, d=None: {})
    cfg = _cfg(tmp_path)
    _raw_lines(cfg, [_rec("a", file="LIGHT\\a.fits"), _rec("b")])
    out = runs.approve_night(cfg, NIGHT, files=["LIGHT/a.fits"])
    assert out["approved"] == 1
    assert runs._record_key({"rig": "rc16", "file": "LIGHT\\a.fits"}) == \
        runs._record_key({"rig": "rc16", "file": "LIGHT/a.fits"})


def test_rewrite_keeps_an_appended_backslash_record_once(tmp_path, quiet):
    """A record appended (old form) while a writer held its copy is kept by
    the rewrite, and the writer's own records are not duplicated."""
    cfg = _cfg(tmp_path)
    _raw_lines(cfg, [_rec("a", file="LIGHT\\a.fits")])
    with runs.edit_subs(cfg, NIGHT) as subs:
        subs[0]["note"] = "x"
        with open(_log(cfg), "a", encoding="utf-8") as f:   # live appender
            f.write(json.dumps(_rec("b", file="LIGHT\\b.fits")) + "\n")
    assert _files_on_disk(cfg) == ["LIGHT/a.fits", "LIGHT/b.fits"]


def test_merge_subs_matches_a_backslash_log(tmp_path, quiet):
    cfg = _cfg(tmp_path)
    _raw_lines(cfg, [_rec("a", file="LIGHT\\a.fits")])
    with runs.edit_subs(cfg, NIGHT, hold_lock=False) as subs:
        subs[0]["target"] = "M31"
    recs = runs._load_subs(cfg, NIGHT)
    assert len(recs) == 1 and recs[0]["target"] == "M31"


def test_pointing_and_solve_stores_key_on_the_canonical_form(tmp_path):
    from photonscript.scheduler import solve_store
    from photonscript.shared import pointing
    cfg = _cfg(tmp_path)
    side = pointing.sidecar_path(cfg, NIGHT)
    side.parent.mkdir(parents=True, exist_ok=True)
    side.write_text(json.dumps({"rig": "rc16", "file": "LIGHT\\a.fits",
                                "src": "header"}) + "\n", encoding="utf-8")
    line = pointing.append_record(cfg, NIGHT, {"rig": "rc16",
                                               "file": "LIGHT\\b.fits"})
    assert line["file"] == "LIGHT/b.fits"
    pts = pointing.load(cfg, NIGHT)
    assert set(pts) == {("rc16", "LIGHT/a.fits"), ("rc16", "LIGHT/b.fits")}
    sp = solve_store.store_path(cfg, NIGHT, "piggyback")
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"file": "LIGHT\\a.fits", "solved": True}) + "\n",
                  encoding="utf-8")
    assert set(solve_store.lookup(cfg, NIGHT, "piggyback")) == {"LIGHT/a.fits"}
    solve_store.solve(cfg, tmp_path / "LIGHT" / "c.fits", rig="piggyback",
                      night=NIGHT, rel_file="LIGHT\\c.fits",
                      runner=lambda *a: None)
    assert "LIGHT/c.fits" in solve_store.lookup(cfg, NIGHT, "piggyback")


def test_backfill_stores_posix_files(tmp_path, monkeypatch):
    """The backfill writes "/" and does not re-grade an old backslash record."""
    cfg = _cfg(tmp_path)
    d = tmp_path / "fits" / NIGHT / "LIGHT"
    d.mkdir(parents=True)
    for n in ("a", "b"):
        (d / f"{n}.fits").write_bytes(b"x")
    _raw_lines(cfg, [_rec("a", file="LIGHT\\a.fits")])
    graded = []

    def grade(path, config, plan_names=None, prewarm=None, stars_to=None):
        graded.append(path.stem)
        return {"rig": "rc16", "target": "T", "filter": "Ha",
                "passed_qa": True}
    monkeypatch.setattr(runs, "_fast_grade", grade)
    # stop after the grading loop (the post passes are not under test)
    monkeypatch.setattr(runs, "attribute_night",
                        lambda *a, **k: (_ for _ in ()).throw(SystemExit))
    done = threading.Event()
    real_logger = runs.logger.info

    def info(msg, *a, **k):
        if str(msg).startswith("Backfill finished"):
            done.set()
        return real_logger(msg, *a, **k)
    monkeypatch.setattr(runs.logger, "info", info)
    runs.start_backfill(cfg, NIGHT)
    assert done.wait(10)
    assert graded == ["b"]
    assert _files_on_disk(cfg) == ["LIGHT\\a.fits", "LIGHT/b.fits"]
    for _ in range(100):
        if not runs._backfill_state.get(NIGHT, {}).get("running"):
            break
        threading.Event().wait(0.05)
    runs._backfill_state.pop(NIGHT, None)


# ------------------------------------------------- 2. Piggy library moves

def _piggy(name, target, **kw):
    return _rec(name, rig="piggyback", filter="OSC", target=target, **kw)


def _lib_link(cfg, folder, name, root=""):
    lib = runs.library_root(cfg)
    p = (lib / root if root else lib) / folder / "OSC" / f"{name}.fits"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def _change(name, frm="Crescent Nebula", to="M31", **kw):
    c = {"file": f"LIGHT/{name}.fits", "rig": "piggyback", "filter": "OSC",
         "from": frm, "to": to, "date": NIGHT}
    c.update(kw)
    return c


def test_library_moves_follow_the_merged_log(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg, [_piggy("a", "M31"), _piggy("b", "Manual name",
                                            target_src="manual")])
    a = _lib_link(cfg, "Crescent Nebula", "a")
    b = _lib_link(cfg, "Crescent Nebula", "b")
    res = pa.library_moves(cfg, [_change("a"), _change("b")], apply=True)
    lib = runs.library_root(cfg)
    assert not a.exists() and (lib / "M31" / "OSC" / "a.fits").exists()
    assert b.exists() and not (lib / "M31" / "OSC" / "b.fits").exists()
    assert res["moved"] == 1 and len(res["skipped"]) == 1
    assert res["skipped"][0]["now"] == "Manual name"


def test_library_moves_skip_a_record_gone_from_the_log(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg, [_piggy("a", "M31")])
    link = _lib_link(cfg, "Crescent Nebula", "z")
    res = pa.library_moves(cfg, [_change("z")], apply=True)
    assert link.exists() and res["moved"] == 0
    assert res["skipped"][0]["now"] is None


def test_library_moves_dry_run_and_untagged_changes_unchanged(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg, [_piggy("a", "Crescent Nebula")])
    link = _lib_link(cfg, "Crescent Nebula", "a")
    res = pa.library_moves(cfg, [_change("a")], apply=False)
    assert link.exists() and len(res["moves"]) == 1 and not res["skipped"]
    res = pa.library_moves(cfg, [_change("a", date=None)], apply=True)
    assert not link.exists() and res["moved"] == 1


def test_library_moves_run_under_the_night_lock(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg, [_piggy("a", "M31")])
    _lib_link(cfg, "Crescent Nebula", "a")
    held = []
    real = pa._move_links

    def probe(*a, **k):
        box = {}
        t = threading.Thread(target=lambda: box.setdefault(
            "got", runs.subs_lock(cfg, NIGHT).acquire(timeout=0.2)))
        t.start()
        t.join(5)
        held.append(not box["got"])
        if box["got"]:
            runs.subs_lock(cfg, NIGHT).release()
        return real(*a, **k)
    monkeypatch.setattr(pa, "_move_links", probe)
    pa.library_moves(cfg, [_change("a")], apply=True)
    assert held == [True]


def test_reattribute_keeps_links_of_a_target_assigned_during_the_pass(
        tmp_path, monkeypatch):
    """End to end: a person assigns a target while the unlocked pass runs;
    the merge keeps the person's name and the links stay where they are."""
    from tests.test_scheduler import test_ps137_piggy_attribution as t137
    monkeypatch.setattr("photonscript.scheduler.identify._candidates",
                        lambda config: [])
    config = t137._config(tmp_path)
    where = t137._piggy_night(config, n=2, n_crescent=2)
    lib = runs.library_root(config)
    for name in ("OSC_0000.fits", "OSC_0001.fits"):
        p = lib / "Crescent Nebula" / "OSC" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    real = pa.attribute_records

    def slow_pass(config_, d, subs, **kw):
        out = real(config_, d, subs, **kw)
        with runs.edit_subs(config_, d) as live:   # the person, meanwhile
            for s in live:
                if s["file"].endswith("OSC_0001.fits"):
                    s["target"] = "Hand Picked"
                    s["target_src"] = "manual"
        return out
    monkeypatch.setattr(pa, "attribute_records", slow_pass)
    r = pa.reattribute(config, [t137.NIGHT], apply=True, solve=True,
                       runner=t137._runner_for(where))
    assert r["subs_changed"] == 2
    assert [m["to"] for m in r["library_moves"]] == [
        os.path.join("M31", "OSC", "OSC_0000.fits")]
    assert len(r["library_skipped"]) == 1
    assert (lib / "Crescent Nebula" / "OSC" / "OSC_0001.fits").exists()
    by = {s["file"]: s for s in runs._load_subs(config, t137.NIGHT)}
    assert by["LIGHT/OSC_0001.fits"]["target"] == "Hand Picked"


# ----------------------------------------------- 3. stale _rejected copies

def _hardlinked_reject(tmp_path, cfg, name="a"):
    """An original FITS, its Library hardlink moved to _rejected (as the
    pointing pass / PS-71 do). Returns (original, live path, rejected)."""
    orig = tmp_path / "fits" / NIGHT / "LIGHT" / f"{name}.fits"
    orig.parent.mkdir(parents=True, exist_ok=True)
    orig.write_bytes(b"frame")
    lib = runs.library_root(cfg)
    live = lib / "T" / "Ha" / f"{name}.fits"
    rej = lib / "_rejected" / "T" / "Ha" / f"{name}.fits"
    rej.parent.mkdir(parents=True, exist_ok=True)
    os.link(orig, rej)
    return orig, live, rej


def test_accept_removes_the_stale_rejected_link(tmp_path, quiet):
    cfg = _cfg(tmp_path)
    orig, live, rej = _hardlinked_reject(tmp_path, cfg)
    _seed(cfg, [_rec("a", abs_path=str(orig), passed_qa=False)])
    runs.set_verdicts(cfg, NIGHT, ["LIGHT/a.fits"], "accepted")
    assert live.exists() and os.path.samefile(live, orig)
    assert not rej.exists() and orig.read_bytes() == b"frame"


def test_accept_with_a_live_link_already_there_also_cleans(tmp_path, quiet):
    cfg = _cfg(tmp_path)
    orig, live, rej = _hardlinked_reject(tmp_path, cfg)
    live.parent.mkdir(parents=True, exist_ok=True)
    os.link(orig, live)
    _seed(cfg, [_rec("a", abs_path=str(orig), passed_qa=False)])
    runs.set_verdicts(cfg, NIGHT, ["LIGHT/a.fits"], "accepted")
    assert live.exists() and not rej.exists() and orig.exists()


def test_a_different_file_in_rejected_is_kept(tmp_path, quiet):
    cfg = _cfg(tmp_path)
    orig, live, rej = _hardlinked_reject(tmp_path, cfg)
    rej.unlink()
    rej.write_bytes(b"another frame, same name")
    _seed(cfg, [_rec("a", abs_path=str(orig), passed_qa=False)])
    runs.set_verdicts(cfg, NIGHT, ["LIGHT/a.fits"], "accepted")
    assert live.exists() and rej.read_bytes() == b"another frame, same name"


def test_no_live_link_keeps_the_rejected_copy(tmp_path, quiet):
    """Original gone (no re-link possible): the _rejected copy stays."""
    cfg = _cfg(tmp_path)
    orig, live, rej = _hardlinked_reject(tmp_path, cfg)
    orig.unlink()
    _seed(cfg, [_rec("a", abs_path=str(orig), passed_qa=False)])
    runs.set_verdicts(cfg, NIGHT, ["LIGHT/a.fits"], "accepted")
    assert not live.exists() and rej.exists()


def test_reject_and_review_leave_rejected_alone(tmp_path, quiet):
    cfg = _cfg(tmp_path)
    orig, live, rej = _hardlinked_reject(tmp_path, cfg)
    _seed(cfg, [_rec("a", abs_path=str(orig))])
    for st in ("rejected", "review"):
        runs.set_verdicts(cfg, NIGHT, ["LIGHT/a.fits"], st)
        assert rej.exists()


def test_desktop_mirror_is_never_written(tmp_path, quiet):
    cfg = _cfg(tmp_path, desktop_library_dir=str(tmp_path / "lib"))
    orig, live, rej = _hardlinked_reject(tmp_path, cfg)
    live.parent.mkdir(parents=True, exist_ok=True)
    os.link(orig, live)
    rec = _rec("a", abs_path=str(orig))
    assert runs._is_desktop_mirror(cfg, runs.library_root(cfg))
    assert runs._drop_rejected_copies(cfg, rec) == [] and rej.exists()


def test_build_library_cleans_stale_rejected_copies(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "_build_piggyback_calibration",
                        lambda *a, **k: None)
    cfg = _cfg(tmp_path)
    orig, live, rej = _hardlinked_reject(tmp_path, cfg)
    _seed(cfg, [_rec("a", abs_path=str(orig), reviewed=True)])
    out = runs.build_library(cfg, NIGHT)
    assert out["linked"] == 1 and out["rejected_copies_removed"] == 1
    assert live.exists() and not rej.exists() and orig.exists()
