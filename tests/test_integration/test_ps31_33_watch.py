"""PS-33 (desktop half) and PS-31: the integrate run writes ledger.json,
report.py posts it or leaves it queued and retries, and integrate-watch
decides from the scheduler's candidates and runs at most one integrate
per cycle (fakes only: no network, no PixInsight)."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from photonscript.integration import ledger as writer
from photonscript.integration import pipeline as pl
from photonscript.integration import report, watch
from photonscript.shared import ledger as L
from tests.test_integration.test_pipeline import _opts, lib  # noqa: F401 - fixture

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=timezone.utc)
URL = "http://scope.test:8100"


# --- the ledger the pipeline writes ----------------------------------------------

def test_integrate_run_writes_a_ledger(lib, tmp_path):  # noqa: F811
    staging = tmp_path / "Staging"
    res = pl.run(_opts(lib, tmp_path, out=staging / "run1",
                       trigger={"by": "integrate-watch", "reason": "goal met"}),
                 echo=lambda s: None)
    led = L.load(Path(res["ledger"]))
    assert res["ledger_version"] == 1 and led.version == 1 and not led.reported
    assert led.campaign == "M31" and led.rig == "piggyback" and led.run == "run1"
    acq = led.machine["acquisition"]
    assert acq["subs_by_filter"]["OSC"] == {"selected": 5, "staged": 5, "integrated": 5,
                                            "integrated_s": 1160.0}
    assert acq["hours"] == pytest.approx(1160 / 3600, abs=1e-3)
    assert acq["nights"] == ["2026-09-20", "2026-10-03"]
    assert len(led.machine["subs"]) == 5 and all(s["used"] for s in led.machine["subs"])
    assert led.machine["calibration"]["status"] == "partial"      # bias + darks, no flats
    assert led.machine["integration"]["ok"] is None                # PixInsight not run
    assert led.machine["astrobin"]["packet"].endswith("_astrobin_packet.md")
    assert Path(led.machine["astrobin"]["packet"]).exists() and not led.machine["astrobin"]["uploaded"]
    assert led.machine["trigger"]["reason"] == "goal met"
    assert led.machine["timing"]["stages"]
    assert led.review.is_empty()
    (Path(res["ledger"])).read_text(encoding="ascii")              # ASCII file


def test_second_run_is_v2_and_review_is_never_overwritten(lib, tmp_path):  # noqa: F811
    staging = tmp_path / "Staging"
    r1 = pl.run(_opts(lib, tmp_path, out=staging / "run1"), echo=lambda s: None)
    led = L.load(Path(r1["ledger"]))
    led.review = L.Review(verdict="keep", notes="first look")
    L.save(Path(r1["ledger"]), led)
    r2 = pl.run(_opts(lib, tmp_path, out=staging / "run2"), echo=lambda s: None)
    assert r2["ledger_version"] == 2
    # rewriting run1's ledger keeps its review and version
    again = writer.build(target="M31", rig="piggyback", run_dir=staging / "run1", options={},
                         selected=[], staged=[], integrated=[], dropped=set(), qa={},
                         calibration={}, integration=None, finish=None, timing=[], packet="",
                         csv="", final="")
    writer.write_for_run(again, staging / "run1", staging)
    kept = L.load(staging / "run1" / "ledger.json")
    assert kept.version == 1 and kept.review.verdict == "keep"


# --- report: post or queue ----------------------------------------------------------

def _write(tmp_path, run, version=1, reported=False, campaign="M31", created="2026-10-06T22:00:00Z"):
    led = L.Ledger(campaign=campaign, rig="piggyback", run=run, version=version,
                   created_at=created, reported=reported,
                   machine={"acquisition": {"hours": 2.0}, "subs": []})
    return L.save(tmp_path / "Staging" / run / "ledger.json", led)


def test_post_success_marks_reported_and_takes_the_stored_version(tmp_path):
    f = _write(tmp_path, "r1")
    calls = []

    def post(url, body, timeout):
        calls.append((url, body["run"]))
        return 200, {"ok": True, "version": 4, "headline": "Integrated 2.0 h", "project_id": "p"}
    r = report.post_ledger(f, URL, post=post)
    assert r["ok"] and calls == [(URL + "/api/integrations", "r1")]
    led = L.load(f)
    assert led.reported and led.reported_at and led.version == 4


def test_unreachable_scope_leaves_it_queued_and_the_next_sweep_posts_it(tmp_path):
    root = tmp_path / "Staging"
    f = _write(tmp_path, "r1")
    _write(tmp_path, "r2", version=2)

    def down(url, body, timeout):
        raise OSError("timed out")
    msgs = []
    res = report.sweep(root, URL, post=down, echo=msgs.append)
    assert res["pending"] == 2 and not res["posted"] and len(res["failed"]) == 1  # stops early
    assert not L.load(f).reported and any("WARNING" in m for m in msgs)
    res = report.sweep(root, URL, post=lambda u, b, t: (200, {"ok": True}), echo=lambda s: None)
    assert len(res["posted"]) == 2 and report.pending(root) == []


def test_http_error_is_reported_not_raised(tmp_path):
    f = _write(tmp_path, "r1")
    r = report.post_ledger(f, URL, post=lambda u, b, t: (404, {"detail": "no goal for 'M31'"}))
    assert not r["ok"] and r["status"] == 404 and "no goal" in r["detail"]
    assert report.post_ledger(f, "", post=None)["ok"] is False
    assert not L.load(f).reported


# --- decisions -------------------------------------------------------------------

THR = {"rigs": ["piggyback"], "new_data_h": 1.0, "first_h": 0.0, "min_interval_h": 12.0,
       "require_calibration": False}
LOCAL0 = {"newest": "", "queued": [], "count": 0}


def _cand(pct=100, last=None, new_h=0.0, approved=6.0, rig="piggyback", missing=()):
    return {"target": "Andromeda Galaxy", "rig": rig, "project_id": "p1",
            "goal": {"pct": pct, "hours_done": 6.0 * pct / 100, "hours_goal": 6.0},
            "approved_h": approved, "approved_subs": int(approved * 30), "last": last,
            "new_data_h": new_h, "readiness": {"calibration_missing": list(missing)},
            "calibration_owed": []}


def test_decide_goal_met_without_a_ledger_runs():
    d = watch.decide(_cand(pct=100), THR, LOCAL0, NOW)
    assert d.run and "goal met" in d.reason


def test_decide_waits_below_goal_unless_first_h():
    assert not watch.decide(_cand(pct=60, approved=3.6), THR, LOCAL0, NOW).run
    d = watch.decide(_cand(pct=60, approved=3.6), {**THR, "first_h": 3.0}, LOCAL0, NOW)
    assert d.run and "first integration" in d.reason


def test_decide_new_data_threshold():
    last = {"version": 2}
    assert not watch.decide(_cand(last=last, new_h=0.6), THR, LOCAL0, NOW).run
    d = watch.decide(_cand(last=last, new_h=1.2), THR, LOCAL0, NOW)
    assert d.run and "since v2" in d.reason


def test_decide_guards():
    assert "not watched" in watch.decide(_cand(rig="rc16"), THR, LOCAL0, NOW).reason
    assert "no approved" in watch.decide(_cand(approved=0), THR, LOCAL0, NOW).reason
    recent = {"newest": (NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ"),
              "queued": [], "count": 1}
    d = watch.decide(_cand(), THR, recent, NOW)
    assert not d.run and "3.0 h ago" in d.reason
    assert not watch.decide(_cand(), THR, {**LOCAL0, "queued": ["r9"]}, NOW).run
    cal = _cand(missing=["flats OSC"])
    assert watch.decide(cal, THR, LOCAL0, NOW).run                 # report-only default
    d = watch.decide(cal, {**THR, "require_calibration": True}, LOCAL0, NOW)
    assert not d.run and "flats OSC" in d.reason


def test_pick_prefers_a_met_goal_then_most_new_data():
    a = watch.decide({**_cand(last={"version": 1}, new_h=3.0), "target": "A"}, THR, LOCAL0, NOW)
    b = watch.decide({**_cand(), "target": "B"}, THR, LOCAL0, NOW)
    c = watch.decide({**_cand(last={"version": 1}, new_h=5.0), "target": "C"}, THR, LOCAL0, NOW)
    assert watch.pick([a, b, c]).target == "B"
    assert watch.pick([a, c]).target == "C"
    assert watch.pick([watch.decide(_cand(pct=10), THR, LOCAL0, NOW)]) is None


def test_thresholds_merge_command_line_over_scope():
    o = watch.WatchOptions(base_url=URL, staging_root=Path("."), new_data_h=2.5)
    t = watch.merged_thresholds({**THR, "rigs": ["piggyback", "rc16"]}, o)
    assert t["new_data_h"] == 2.5 and t["rigs"] == ["piggyback", "rc16"]


def test_local_state_matches_aliases_and_queued(tmp_path):
    _write(tmp_path, "r1", campaign="M 31", created="2026-10-07T08:00:00Z", reported=True)
    _write(tmp_path, "r2", campaign="Andromeda Galaxy", created="2026-10-06T08:00:00Z")
    s = watch.local_state(tmp_path / "Staging", "Andromeda Galaxy", "piggyback")
    assert s["count"] == 2 and s["newest"] == "2026-10-07T08:00:00Z" and s["queued"] == ["r2"]


# --- the watch loop with fakes ---------------------------------------------------

class Fakes:
    def __init__(self, cands, *, pi=(), down=False):
        self.cands, self.pi, self.down = cands, list(pi), down
        self.runs, self.posts = [], []

    def get(self, url):
        if self.down:
            raise OSError("no route to host")
        assert url == URL + "/api/integrations/candidates"
        return {"candidates": self.cands, "thresholds": THR}

    def post(self, url, body, timeout):
        self.posts.append(body["run"])
        return 200, {"ok": True, "version": body["version"]}

    def run_integrate(self, root):
        def _run(target, rig, trigger):
            self.runs.append((target, rig, trigger["reason"]))
            led = L.Ledger(campaign=target, rig=rig, run="new_run", version=1,
                           machine={"trigger": trigger})
            f = L.save(root / "new_run" / "ledger.json", led)
            return {"run_dir": str(f.parent), "ledger": str(f), "integration": {"ok": True}}
        return _run


def _opts_w(tmp_path, **kw):
    return watch.WatchOptions(base_url=URL, staging_root=tmp_path / "Staging", **kw)


def test_cycle_runs_one_integrate_and_posts_its_ledger(tmp_path):
    o = _opts_w(tmp_path)
    fk = Fakes([_cand(), {**_cand(), "target": "M33"}])
    out = watch.cycle(o, run_integrate=fk.run_integrate(o.staging_root), get=fk.get, post=fk.post,
                      pi_running=lambda: [], echo=lambda s: None, now=NOW)
    assert len(fk.runs) == 1 and fk.runs[0][2].startswith("goal met")
    assert out["ran"]["posted"] and fk.posts == ["new_run"]
    assert L.load(o.staging_root / "new_run" / "ledger.json").reported
    assert not (o.staging_root / watch.LOCK_NAME).exists()          # lock released


def test_cycle_never_runs_while_pixinsight_is_open(tmp_path):
    o = _opts_w(tmp_path)
    fk = Fakes([_cand()])
    out = watch.cycle(o, run_integrate=fk.run_integrate(o.staging_root), get=fk.get, post=fk.post,
                      pi_running=lambda: [4242], echo=lambda s: None, now=NOW)
    assert out["skipped"] == "pixinsight running" and fk.runs == []


def test_cycle_with_the_scope_down_retries_nothing_and_runs_nothing(tmp_path):
    o = _opts_w(tmp_path)
    _write(tmp_path, "queued_run")
    fk = Fakes([_cand()], down=True)

    def down_post(url, body, timeout):
        raise OSError("down")
    out = watch.cycle(o, run_integrate=fk.run_integrate(o.staging_root), get=fk.get,
                      post=down_post, pi_running=lambda: [], echo=lambda s: None, now=NOW)
    assert out["skipped"] == "scheduler unreachable" and fk.runs == []
    assert out["sweep"]["pending"] == 1 and report.pending(o.staging_root)


def test_dry_run_decides_but_runs_and_posts_nothing(tmp_path):
    o = _opts_w(tmp_path, dry_run=True)
    _write(tmp_path, "queued_run")
    fk = Fakes([_cand()])
    out = watch.cycle(o, run_integrate=fk.run_integrate(o.staging_root), get=fk.get, post=fk.post,
                      pi_running=lambda: [], echo=lambda s: None, now=NOW)
    assert fk.runs == [] and fk.posts == []
    assert out["decisions"][0]["run"] is False                      # its ledger is still queued
    o2 = _opts_w(tmp_path / "clean", dry_run=True)
    out = watch.cycle(o2, run_integrate=fk.run_integrate(o2.staging_root), get=fk.get,
                      post=fk.post, pi_running=lambda: [], echo=lambda s: None, now=NOW)
    assert out["would_run"] == {"target": "Andromeda Galaxy", "rig": "piggyback"} and fk.runs == []


def test_a_failed_run_counts_for_min_interval(tmp_path):
    o = _opts_w(tmp_path)
    fk = Fakes([_cand()])

    def boom(target, rig, trigger):
        raise RuntimeError("PixInsight not found")
    with pytest.raises(RuntimeError):
        watch.cycle(o, run_integrate=boom, get=fk.get, post=fk.post, pi_running=lambda: [],
                    echo=lambda s: None, now=NOW)
    s = watch.local_state(o.staging_root, "Andromeda Galaxy", "piggyback")
    assert s["newest"]
    d = watch.decide(_cand(), THR, s, datetime.now(timezone.utc))
    assert not d.run and "ago" in d.reason


def test_a_second_watcher_is_locked_out(tmp_path):
    o = _opts_w(tmp_path)
    o.staging_root.mkdir(parents=True)
    (o.staging_root / watch.LOCK_NAME).write_text("999999", encoding="ascii")
    fk = Fakes([_cand()])
    out = watch.cycle(o, run_integrate=fk.run_integrate(o.staging_root), get=fk.get, post=fk.post,
                      pi_running=lambda: [], echo=lambda s: None, now=NOW, alive=lambda pid: True)
    assert out["skipped"] == "locked" and fk.runs == []
    out = watch.cycle(o, run_integrate=fk.run_integrate(o.staging_root), get=fk.get, post=fk.post,
                      pi_running=lambda: [], echo=lambda s: None, now=NOW, alive=lambda pid: False)
    assert len(fk.runs) == 1                                        # stale lock taken over


def test_loop_once_returns_after_one_cycle(tmp_path):
    o = _opts_w(tmp_path)
    fk = Fakes([])
    res = watch.loop(o, once=True, run_integrate=fk.run_integrate(o.staging_root), get=fk.get,
                     post=fk.post, pi_running=lambda: [], echo=lambda s: None,
                     sleep=lambda s: pytest.fail("no sleep with --once"))
    assert len(res) == 1 and res[0]["decisions"] == []
    json.loads(watch.dump(res[0]))
