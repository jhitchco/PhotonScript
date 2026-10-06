"""PS-24: review verdicts without a page reload.

Server: the subs log is rewritten atomically under a per-night lock shared
with the live appender (no record lost, an injected write failure leaves the
old file intact, a sub appended meanwhile survives a rewrite); a verdict
links / unlinks only that sub in the Library by the build_library path rule;
goal progress is debounced; /qa answers {ok, sub, counts}; /qa-batch writes
N subs in one rewrite; /approve returns counts; /review-summary repaints the
plan-vs-actual rows; the lightbox preview is a JPEG.
Client: static checks on runs.html, target.html, review.js and sub_tiles.js
(optimistic verdicts with rollback, no reload on close, keys, batch, all subs
reachable, ASCII sources).
"""
import json
import re
import threading
import time
from pathlib import Path

import pytest

from photonscript.scheduler import runs
from tests.test_scheduler.test_ps21_grading import (NIGHT, _cfg, _rec, _seed,
                                                    _write_light)
from tests.test_scheduler.test_ps115_review_panel import _func, _page, _script

ROOT = Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
RUNS = ROOT / "templates" / "runs.html"
TARGET = ROOT / "templates" / "target.html"
REVIEW_JS = ROOT / "static" / "js" / "review.js"
TILES_JS = ROOT / "static" / "js" / "sub_tiles.js"


@pytest.fixture
def goal_calls(monkeypatch):
    """Count goal syncs instead of touching the app's project store; cancel
    any debounced one when the test ends."""
    calls = []
    monkeypatch.setattr(runs, "sync_goal_progress",
                        lambda cfg: calls.append(cfg) or [])
    yield calls
    runs.flush_goal_sync()


def _log(cfg) -> Path:
    return runs.runs_dir(cfg) / f"{NIGHT}_subs.jsonl"


# ------------------------------------------------------------ the subs log

def test_verdicts_under_a_concurrent_appender_lose_no_line(tmp_path, goal_calls):
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec(f"s{i}") for i in range(20)])
    stop = threading.Event()
    appended = []

    def appender():
        i = 0
        while not stop.is_set() and i < 300:
            runs.append_sub_record(cfg, NIGHT, _rec(f"live{i}"))
            appended.append(i)
            i += 1

    t = threading.Thread(target=appender)
    t.start()
    for k in range(60):
        st = ("accepted", "rejected", "review")[k % 3]
        assert runs.set_manual_qa(cfg, NIGHT, f"LIGHT/s{k % 20}.fits", state=st,
                                  defer_goal_sync=True) is not None
    stop.set()
    t.join()
    recs = runs._load_subs(cfg, NIGHT)
    files = [r["file"] for r in recs]
    assert len(files) == 20 + len(appended) == len(set(files))
    by = {r["file"]: r for r in recs}
    assert by["LIGHT/s19.fits"]["passed_qa"] is True           # k=59: review
    assert by["LIGHT/s18.fits"]["passed_qa"] is False          # k=58: rejected
    assert not list(_log(cfg).parent.glob("*.tmp"))


def test_rewrite_keeps_a_record_appended_after_the_load(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("a"), _rec("b")])
    subs = runs._load_subs(cfg, NIGHT)              # a caller loads ...
    runs.append_sub_record(cfg, NIGHT, _rec("late"))  # ... a sub lands ...
    subs[0]["reviewed"] = True
    runs._rewrite_subs(cfg, NIGHT, subs)            # ... and it rewrites
    by = {r["file"]: r for r in runs._load_subs(cfg, NIGHT)}
    assert set(by) == {"LIGHT/a.fits", "LIGHT/b.fits", "LIGHT/late.fits"}
    assert by["LIGHT/a.fits"]["reviewed"] is True


def test_injected_write_failure_leaves_the_old_log_intact(tmp_path, monkeypatch):
    import os
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("a")])
    before = _log(cfg).read_text(encoding="utf-8")

    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        runs._rewrite_subs(cfg, NIGHT, [dict(_rec("a"), reviewed=True)])
    assert _log(cfg).read_text(encoding="utf-8") == before
    assert not list(_log(cfg).parent.glob("*.tmp"))


def test_busy_file_is_retried_then_replaced(tmp_path, monkeypatch):
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
    runs._rewrite_subs(cfg, NIGHT, [dict(_rec("a"), reviewed=True)])
    assert len(tries) == 3
    (r,) = runs._load_subs(cfg, NIGHT)
    assert r["reviewed"] is True


# --------------------------------------------------------------- verdicts

def test_library_follows_one_verdict_like_build_library(tmp_path, goal_calls):
    cfg = _cfg(tmp_path)
    f = _write_light(tmp_path / "fits" / NIGHT / "LIGHT" / "m_Ha_300s_0001.fits")
    _seed(cfg, [_rec("m_Ha_300s_0001", target="Test Nebula", abs_path=str(f))])
    lib = runs.library_root(cfg)
    want = lib / "Test Nebula" / "Ha" / f.name
    runs.set_manual_qa(cfg, NIGHT, "LIGHT/m_Ha_300s_0001.fits", state="accepted")
    assert want.is_file()
    # the same path build_library would give
    want.unlink()
    assert runs.build_library(cfg, NIGHT)["linked"] == 1 and want.is_file()
    runs.set_manual_qa(cfg, NIGHT, "LIGHT/m_Ha_300s_0001.fits", state="rejected")
    assert not want.exists()
    runs.set_manual_qa(cfg, NIGHT, "LIGHT/m_Ha_300s_0001.fits", state="accepted")
    runs.set_manual_qa(cfg, NIGHT, "LIGHT/m_Ha_300s_0001.fits", state="review")
    assert not want.exists()                     # back to review: out again


def test_verdict_is_fast_and_goal_sync_is_debounced(tmp_path, monkeypatch, goal_calls):
    """323-sub night with 70 nights of history: the verdict does not walk
    the history (goal sync deferred), and a burst syncs once."""
    cfg = _cfg(tmp_path)
    for d in range(70):
        night = f"2026-07-{1 + d % 30:02d}" if d < 30 else f"2026-08-{1 + d % 30:02d}"
        p = runs.runs_dir(cfg) / f"{night}x{d}_subs.jsonl"
        p.write_text("".join(json.dumps(_rec(f"h{d}_{i}")) + "\n" for i in range(150)),
                     encoding="utf-8")
    _seed(cfg, [_rec(f"n{i}") for i in range(323)])
    monkeypatch.setattr(runs, "GOAL_SYNC_DELAY_S", 30.0)
    runs._load_subs(cfg, NIGHT)
    t0 = time.perf_counter()
    for i in range(5):
        runs.set_verdicts(cfg, NIGHT, [f"LIGHT/n{i}.fits"], "accepted",
                          defer_goal_sync=True)
    per = (time.perf_counter() - t0) / 5
    assert per < 0.5, per          # target 0.2 s on the scope PC
    assert goal_calls == [] and runs.goal_sync_pending()
    runs.flush_goal_sync()
    assert len(goal_calls) == 1 and not runs.goal_sync_pending()


def test_bad_state_is_refused(tmp_path):
    cfg = _cfg(tmp_path)
    with pytest.raises(ValueError):
        runs.set_verdicts(cfg, NIGHT, ["LIGHT/a.fits"], "maybe")


# --------------------------------------------------------------------- API

def _client(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    from fastapi.testclient import TestClient
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)

    class _Store:
        projects = {}
    monkeypatch.setattr(app, "get_store", lambda: _Store())
    return cfg, TestClient(app.app)


def test_qa_endpoint_answers_sub_and_counts(tmp_path, monkeypatch, goal_calls):
    cfg, client = _client(tmp_path, monkeypatch)
    _seed(cfg, [_rec("a"), _rec("b"), _rec("c", passed_qa=False, reason="ecc")])
    r = client.post(f"/api/runs/{NIGHT}/qa", json={"file": "LIGHT/a.fits",
                                                    "state": "accepted"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["sub"]["file"] == "LIGHT/a.fits"
    assert body["sub"]["reviewed"] is True and body["sub"]["manual_qa"] is True
    assert body["counts"] == {"total": 3, "approved": 1, "review": 1, "rejected": 1}
    # legacy {passed} body still works
    r = client.post(f"/api/runs/{NIGHT}/qa", json={"file": "LIGHT/b.fits",
                                                    "passed": False})
    assert r.status_code == 200 and r.json()["sub"]["passed_qa"] is False
    assert client.post(f"/api/runs/{NIGHT}/qa", json={
        "file": "LIGHT/nope.fits", "state": "accepted"}).status_code == 404
    assert client.post(f"/api/runs/{NIGHT}/qa", json={
        "file": "LIGHT/a.fits", "state": "maybe"}).status_code == 400
    assert goal_calls == []                      # debounced, not inline


def test_qa_batch_updates_n_subs_in_one_write(tmp_path, monkeypatch, goal_calls):
    cfg, client = _client(tmp_path, monkeypatch)
    _seed(cfg, [_rec(f"s{i}") for i in range(12)])
    writes = []
    real = runs._write_text_atomic
    monkeypatch.setattr(runs, "_write_text_atomic",
                        lambda p, t: writes.append(p) or real(p, t))
    files = [f"LIGHT/s{i}.fits" for i in range(8)] + ["LIGHT/missing.fits"]
    r = client.post(f"/api/runs/{NIGHT}/qa-batch",
                    json={"files": files, "state": "rejected", "why": "visual"})
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == 8 and body["missing"] == ["LIGHT/missing.fits"]
    assert body["counts"]["rejected"] == 8 and body["counts"]["review"] == 4
    assert len(writes) == 1
    by = {s["file"]: s for s in runs._load_subs(cfg, NIGHT)}
    assert by["LIGHT/s0.fits"]["reason"] == "rejected manually: visual"
    assert by["LIGHT/s9.fits"]["passed_qa"] is True
    assert client.post(f"/api/runs/{NIGHT}/qa-batch",
                       json={"files": "x", "state": "rejected"}).status_code == 400


def test_approve_and_review_summary(tmp_path, monkeypatch, goal_calls):
    cfg, client = _client(tmp_path, monkeypatch)
    _seed(cfg, [_rec("a"), _rec("b"), _rec("c", passed_qa=False, reason="ecc")])
    r = client.post(f"/api/runs/{NIGHT}/approve", json={"files": ["LIGHT/a.fits"]})
    assert r.status_code == 200
    assert r.json()["approved"] == 1 and r.json()["counts"]["approved"] == 1
    client.post(f"/api/runs/{NIGHT}/qa", json={"file": "LIGHT/b.fits",
                                               "state": "rejected"})
    assert runs.goal_sync_pending()
    s = client.get(f"/api/runs/{NIGHT}/review-summary")
    assert s.status_code == 200
    out = s.json()
    assert not runs.goal_sync_pending()          # flushed before the totals
    assert out["counts"] == {"total": 3, "approved": 1, "review": 0, "rejected": 2}
    (row,) = [t for t in out["table"] if t["target"] == "T"]
    assert row["attempted"] == 3 and row["accepted"] == 1
    assert "goal_total" in row and "done_total" in row


def test_verdict_routes_left_app_py():
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert '@app.post("/api/runs/{date}/qa")' not in src
    assert '@app.post("/api/runs/{date}/approve")' not in src
    rv = (ROOT / "routers" / "review.py").read_text(encoding="utf-8")
    for route in ('"/api/runs/{date}/qa"', '"/api/runs/{date}/qa-batch"',
                  '"/api/runs/{date}/approve"', '"/api/runs/{date}/review-summary"'):
        assert route in rv, route
    # plain def: the disk work runs in the threadpool, not on the event loop
    assert not re.search(r"async def api_run_(manual_qa|approve)", rv)


# ------------------------------------------------------------ JPEG preview

def test_lightbox_preview_is_a_jpeg(tmp_path, monkeypatch):
    cfg, client = _client(tmp_path, monkeypatch)
    f = _write_light(tmp_path / "fits" / NIGHT / "LIGHT" / "p_Ha_300s_0001.fits")
    _seed(cfg, [_rec("p_Ha_300s_0001", abs_path=str(f))])
    rel = "LIGHT/p_Ha_300s_0001.fits"
    assert runs._thumb_out_path(cfg, NIGHT, rel, 1400, False).suffix == ".jpg"
    assert runs._thumb_out_path(cfg, NIGHT, rel, 264, False).suffix == ".png"
    assert runs._thumb_out_path(cfg, NIGHT, rel, 1400, True).suffix == ".png"
    r = client.get(f"/api/runs/{NIGHT}/thumb", params={"file": rel, "w": 1400})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert r.content[:2] == b"\xff\xd8" and len(r.content) < 300_000
    r = client.get(f"/api/runs/{NIGHT}/thumb", params={"file": rel, "w": 264})
    assert r.headers["content-type"] == "image/png"


# ---------------------------------------------------------------- the pages

def test_runs_page_verdicts_are_optimistic_and_never_reload(tmp_path, monkeypatch):
    html = _page(tmp_path, monkeypatch)
    js = _script(html)
    assert "/static/js/review.js" in html
    st = _func(js, "setState")
    assert "Review.set(" in st and "revert:" in st and "patchSubs(" in st
    for f in ("qaSet", "toggleQa", "lbToggle"):
        body = _func(js, f)
        assert "await" not in body and "loadDetail" not in body, f
    # closing the lightbox (Esc or backdrop) does not refetch the night
    assert "loadDetail(currentDate, true)" not in js
    patch = _func(js, "patchSubs")
    assert "renderReviewBar(d)" in patch and "scheduleSummary()" in patch
    assert "renderSubs(" not in patch                  # only that tile / row
    assert "/review-summary" in _func(js, "scheduleSummary")
    # keys: A / R (then the next sub to review), U undo, X kept, arrows, Esc
    assert "k === 'a'" in js and "k === 'r'" in js and "Review.undo()" in js
    adv = _func(js, "lbVerdict")
    assert "subState(list[j]) === 'review'" in adv
    # every sub the chips select is reachable (no 1-in-N sample)
    rs = _func(js, "renderSubs")
    assert "i % step" not in rs and "const shown = subs;" in rs
    assert "SubTiles.lazy(strip, 3)" in rs
    # batch: shift / ctrl click, one qa-batch call through Review.setMany
    assert "ev.shiftKey" in _func(js, "selClick")
    assert "Review.setMany(" in _func(js, "selApply")
    # prefetch the next two previews; approve without alert() or reload
    assert "new Image().src" in _func(js, "openLb")
    rb = _func(js, "renderReviewBar")
    assert "alert(" not in rb and "loadDetail" not in rb and "reviewed ' +" in rb
    assert "Review.approve(" in _func(js, "approveInPlace")


def test_review_module_rolls_back_and_serializes(tmp_path):
    js = REVIEW_JS.read_text(encoding="utf-8")
    assert "window.Review = {" in js
    assert "opts.revert()" in js and "Rolled back" in js
    assert "if (!r.ok)" in js                       # a non-2xx is a failure
    assert "enqueue(key" in js                      # per-file order
    assert "'qa-batch'" in js and "'approve'" in js
    assert "seqs[key] === seq" in js                # stale answers ignored


def test_targets_page_gives_verdicts_in_place(tmp_path):
    t = TARGET.read_text(encoding="utf-8")
    assert "/static/js/review.js" in t
    assert "review: true" in t and "Review.set(" in t and "revert:" in t
    assert "Read-only here" not in t
    tiles = TILES_JS.read_text(encoding="utf-8")
    assert 'data-qa="accepted"' in tiles and 'data-qa="rejected"' in tiles
    assert "VERDICT_STYLE: VERDICT_STYLE" in tiles and 'data-key="' in tiles


def test_target_detail_flushes_a_pending_goal_sync(tmp_path, monkeypatch, goal_calls):
    cfg, client = _client(tmp_path, monkeypatch)
    _seed(cfg, [_rec("a", target="M 31")])
    monkeypatch.setattr(runs, "GOAL_SYNC_DELAY_S", 30.0)
    runs.set_verdicts(cfg, NIGHT, ["LIGHT/a.fits"], "accepted", defer_goal_sync=True)
    assert runs.goal_sync_pending()
    client.get("/api/targets/detail", params={"name": "M 31"})
    assert not runs.goal_sync_pending() and len(goal_calls) == 1


def test_new_sources_are_ascii_without_em_dashes():
    for p in (REVIEW_JS, TILES_JS, Path(__file__), ROOT / "routers" / "review.py"):
        b = p.read_bytes()
        assert all(c < 128 for c in b), p
