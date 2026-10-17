"""PS-58: staged, smoke-checked self-update, health verification and rollback.

Real git repositories in tmp_path stand in for GitHub (a bare origin) and the
scope PC checkout; the smoke check is injected except in the import-check
tests, which build tiny fake `photonscript` packages.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from photonscript.shared import supervisor as sv
from photonscript.shared import updater as up
from photonscript.shared.config import PhotonScriptConfig


def _git(repo, *args):
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}.items():
        monkeypatch.setenv(k, v)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                   check=True)
    dev = tmp_path / "dev"
    subprocess.run(["git", "clone", "-q", str(origin), str(dev)], check=True,
                   capture_output=True)
    _git(dev, "checkout", "-q", "-b", "main")
    (dev / "app.txt").write_text("A\n")
    _git(dev, "add", "app.txt")
    _git(dev, "commit", "-q", "-m", "A")
    _git(dev, "push", "-q", "origin", "main")
    scope = tmp_path / "scope"
    subprocess.run(["git", "clone", "-q", str(origin), str(scope)], check=True,
                   capture_output=True)
    a = _git(scope, "rev-parse", "HEAD")

    def push(text="B\n", name="app.txt"):
        (dev / name).write_text(text)
        _git(dev, "add", name)
        _git(dev, "commit", "-q", "-m", text.strip())
        _git(dev, "push", "-q", "origin", "main")
        return _git(dev, "rev-parse", "HEAD")

    cfg = PhotonScriptConfig(data_dir=tmp_path / "data")
    return {"scope": scope, "dev": dev, "A": a, "push": push, "cfg": cfg}


class Smoke:
    def __init__(self, ok=True, detail="imports ok"):
        self.ok, self.detail, self.calls = ok, detail, []

    def __call__(self, staging, python, run_tests=False, baseline=None):
        self.calls.append({"staging": staging, "files": sorted(
            p.name for p in Path(staging).iterdir()),
            "app": (Path(staging) / "app.txt").read_text(),
            "baseline": baseline, "run_tests": run_tests})
        return self.ok, self.detail


def _update(r, smoke=None, **kw):
    alerts = []
    rc, detail = up.self_update(r["cfg"], r["scope"], smoke=smoke or Smoke(),
                                notify=lambda t, m, p=0: alerts.append((t, m)),
                                **kw)
    return rc, detail, alerts


def _head(r):
    return _git(r["scope"], "rev-parse", "HEAD")


def test_unchanged(repos):
    rc, detail, alerts = _update(repos)
    assert rc == up.UNCHANGED and alerts == []
    assert not up.state_path(repos["cfg"]).exists()


def test_update_stages_checks_then_fast_forwards(repos):
    b = repos["push"]("B\n")
    smoke = Smoke()
    rc, detail, alerts = _update(repos, smoke=smoke)
    assert rc == up.UPDATED, detail
    assert _head(repos) == b and alerts == []
    call = smoke.calls[0]
    assert call["app"] == "B\n"                       # staged the NEW commit
    assert call["baseline"] == repos["scope"]         # compared to running code
    assert (repos["scope"] / "app.txt").read_text() == "B\n"
    st = up.load_state(repos["cfg"])
    assert st["status"] == "pending" and st["prev"] == repos["A"]
    assert st["target"] == b and st["last_good"] == repos["A"]
    assert up.pending_target(repos["cfg"]) == b
    assert not (repos["cfg"].data_dir / up.STAGING_NAME).exists()
    assert len(_git(repos["scope"], "worktree", "list").splitlines()) == 1


def test_failed_smoke_keeps_old_code_and_alerts(repos):
    b = repos["push"]("B\n")
    rc, detail, alerts = _update(repos, smoke=Smoke(False, "IMPORT-FAIL x"))
    assert rc == up.REFUSED
    assert _head(repos) == repos["A"]
    assert (repos["scope"] / "app.txt").read_text() == "A\n"
    st = up.load_state(repos["cfg"])
    assert st["status"] == "rejected" and st["target"] == b
    assert "IMPORT-FAIL x" in st["reason"]
    assert alerts and alerts[0][0] == "PhotonScript update refused"
    assert len(_git(repos["scope"], "worktree", "list").splitlines()) == 1


def test_dirty_checkout_refused(repos):
    repos["push"]("B\n")
    (repos["scope"] / "app.txt").write_text("hand edit\n")
    rc, detail, _ = _update(repos)
    assert rc == up.REFUSED and "local changes" in detail
    assert _head(repos) == repos["A"]


def test_non_fast_forward_refused(repos):
    repos["push"]("B\n")
    (repos["scope"] / "local.txt").write_text("x")
    _git(repos["scope"], "add", "local.txt")
    _git(repos["scope"], "commit", "-q", "-m", "local")
    local = _head(repos)
    rc, detail, _ = _update(repos)
    assert rc == up.REFUSED and "fast-forward" in detail
    assert _head(repos) == local


def _armer(cfg, state, dawn_in_h):
    dawn = (datetime.now(timezone.utc) + timedelta(hours=dawn_in_h))
    p = Path(cfg.data_dir) / "armer_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"state": state, "plan": {
        "dawn_utc": dawn.replace(tzinfo=None).isoformat() + "Z"}}))


def test_running_night_defers(repos):
    repos["push"]("B\n")
    _armer(repos["cfg"], "RUNNING", 5)
    rc, detail, alerts = _update(repos)
    assert rc == up.DEFERRED and _head(repos) == repos["A"] and alerts == []


def test_armed_or_finished_night_does_not_defer(repos):
    b = repos["push"]("B\n")
    _armer(repos["cfg"], "RUNNING", -2)               # that dawn has passed
    assert up.night_active(repos["cfg"]) is None
    _armer(repos["cfg"], "ARMED", 8)                  # restored after restart
    assert up.night_active(repos["cfg"]) is None
    rc, _, _ = _update(repos)
    assert rc == up.UPDATED and _head(repos) == b


def test_dry_run_changes_nothing(repos):
    repos["push"]("B\n")
    smoke = Smoke()
    rc, detail, _ = _update(repos, smoke=smoke, dry_run=True)
    assert rc == up.UNCHANGED and "dry run" in detail
    assert smoke.calls[0]["app"] == "B\n"
    assert _head(repos) == repos["A"]
    assert not up.state_path(repos["cfg"]).exists()


def test_rollback_records_bad_sha_and_skips_it(repos):
    b = repos["push"]("B\n")
    assert _update(repos)[0] == up.UPDATED
    up.mark_failed(repos["cfg"], "no health")
    _git(repos["scope"], "reset", "-q", "--hard", repos["A"])   # the wrapper
    alerts = []
    st = up.rollback_done(repos["cfg"], "",
                          notify=lambda t, m, p=0: alerts.append((t, m)))
    assert st["status"] == "rolled_back" and st["bad_sha"] == b
    assert st["target"] == repos["A"] and st["reason"] == "no health"
    assert alerts[0][0] == "PhotonScript rolled back"
    rc, detail, alerts = _update(repos)               # same bad SHA: skipped
    assert rc == up.REFUSED and "rolled back earlier" in detail
    assert alerts == [] and _head(repos) == repos["A"]
    c = repos["push"]("C\n")                          # a fix goes out
    rc, _, _ = _update(repos)
    assert rc == up.UPDATED and _head(repos) == c
    assert up.load_state(repos["cfg"])["prev"] == repos["A"]


def test_mark_good_sets_last_good(repos):
    b = repos["push"]("B\n")
    _update(repos)
    up.mark_good(repos["cfg"], b)
    st = up.load_state(repos["cfg"])
    assert st["status"] == "good" and st["last_good"] == b
    assert up.pending_target(repos["cfg"]) is None
    assert up.public_state(repos["cfg"])["status"] == "good"


# --- the real import check on tiny fake packages ---------------------------------------

def _fake_pkg(root: Path, extra: dict = None) -> Path:
    files = {"photonscript/__init__.py": "",
             "photonscript/cli.py": "", "photonscript/orchestrator.py": "",
             "photonscript/shared/__init__.py": "",
             "photonscript/shared/supervisor.py": "",
             "photonscript/shared/updater.py": "",
             "photonscript/scheduler/__init__.py": "",
             "photonscript/scheduler/app.py": "",
             "photonscript/scheduler/extra.py": ""}
    files.update(extra or {})
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    return root


def test_import_check_passes_clean_tree(tmp_path):
    root = _fake_pkg(tmp_path / "ok")
    ok, detail = up.smoke_check(root, sys.executable)
    assert ok, detail
    assert "0 failed" in detail


def test_import_check_fails_on_broken_entry_module(tmp_path):
    root = _fake_pkg(tmp_path / "bad", {"photonscript/cli.py": "def broken(:\n"})
    base = _fake_pkg(tmp_path / "base")
    ok, detail = up.smoke_check(root, sys.executable, baseline=base)
    assert not ok and "photonscript.cli" in detail and "SyntaxError" in detail


def test_import_check_catches_lazily_imported_module(tmp_path):
    root = _fake_pkg(tmp_path / "bad", {
        "photonscript/scheduler/extra.py": "import no_such_package_ps58\n"})
    base = _fake_pkg(tmp_path / "base")
    ok, detail = up.smoke_check(root, sys.executable, baseline=base)
    assert not ok and "photonscript.scheduler.extra" in detail


def test_import_check_tolerates_failures_the_running_code_has_too(tmp_path):
    """e.g. shared/database.py needs greenlet, which a venv may lack: that
    must not block every update."""
    extra = {"photonscript/scheduler/extra.py": "import no_such_package_ps58\n"}
    root = _fake_pkg(tmp_path / "new", extra)
    base = _fake_pkg(tmp_path / "base", extra)
    ok, detail = up.smoke_check(root, sys.executable, baseline=base)
    assert ok, detail
    assert "also fail on the running code" in detail


def test_import_check_is_isolated_from_the_installed_package(tmp_path):
    """The staging checkout, not the running one on PYTHONPATH, is imported."""
    root = _fake_pkg(tmp_path / "iso", {
        "photonscript/__init__.py": "MARK = 'staging'\n",
        "photonscript/cli.py": "import photonscript\n"
                               "assert photonscript.MARK == 'staging'\n"})
    ok, detail = up.smoke_check(root, sys.executable)
    assert ok, detail


# --- supervisor: verify the first start after an update ---------------------------------

def _pending(cfg, sha="b" * 40):
    up.save_state(cfg, {"status": "pending", "prev": "a" * 40, "target": sha})
    return sha


class _Clock:
    def __init__(self, step=1.0):
        self.t, self.step = 0.0, step

    def __call__(self):
        self.t += self.step
        return self.t


def _sup(cfg, code, probe, **kw):
    alerts = []
    rc = sv.run(cfg, [sys.executable, "-c", code],
                notify=lambda t, m, p=0: alerts.append(t),
                sleep=kw.pop("sleep", lambda s: None), clock=_Clock(),
                host="test", health_url="http://127.0.0.1:1/api/health",
                probe=probe, **kw)
    return rc, alerts


def test_healthy_update_is_recorded_good(tmp_path):
    cfg = PhotonScriptConfig(data_dir=tmp_path)
    sha = _pending(cfg)
    seen = []
    rc, _ = _sup(cfg, "import sys, time; time.sleep(0.3); sys.exit(42)",
                 lambda url, exp: seen.append(exp) or True, rollback=True)
    assert rc == sv.EXIT_UPDATE
    assert seen == [sha]
    st = up.load_state(cfg)
    assert st["status"] == "good" and st["last_good"] == sha


def test_unhealthy_update_asks_for_rollback(tmp_path):
    cfg = PhotonScriptConfig(data_dir=tmp_path)
    _pending(cfg)
    t0 = time.monotonic()
    rc, alerts = _sup(cfg, "import time; time.sleep(60)",
                      lambda url, exp: False, rollback=True, verify_s=20)
    assert rc == sv.EXIT_ROLLBACK == 43
    assert time.monotonic() - t0 < 30                  # child was killed
    st = up.load_state(cfg)
    assert st["status"] == "failed" and "did not report" in st["reason"]
    assert alerts == []                                # the wrapper alerts


def test_new_code_crashing_at_start_asks_for_rollback(tmp_path):
    cfg = PhotonScriptConfig(data_dir=tmp_path)
    _pending(cfg)
    rc, alerts = _sup(cfg, "import sys; sys.exit(1)", lambda url, exp: False,
                      rollback=True, verify_s=10_000,
                      sleep=lambda s: time.sleep(0.02))
    assert rc == sv.EXIT_ROLLBACK
    assert "exited with code 1" in up.load_state(cfg)["reason"]
    assert alerts == []                                # not a normal crash


def test_no_verification_without_wrapper_support(tmp_path):
    """An older wrapper can't act on 43: plain PS-44 behavior."""
    cfg = PhotonScriptConfig(data_dir=tmp_path)
    _pending(cfg)
    probed = []
    rc, _ = _sup(cfg, "import sys; sys.exit(42)",
                 lambda url, exp: probed.append(1) or False, rollback=False)
    assert rc == sv.EXIT_UPDATE and probed == []
    assert up.load_state(cfg)["status"] == "pending"


def test_no_verification_when_nothing_pending(tmp_path):
    cfg = PhotonScriptConfig(data_dir=tmp_path)
    probed = []
    rc, _ = _sup(cfg, "import sys; sys.exit(42)",
                 lambda url, exp: probed.append(1) or False, rollback=True)
    assert rc == sv.EXIT_UPDATE and probed == []


def test_health_probe_checks_commit(monkeypatch):
    import httpx

    class R:
        status_code = 200

        def __init__(self, d):
            self.d = d

        def json(self):
            return self.d

    monkeypatch.setattr(httpx, "get", lambda *a, **k: R({"ok": True, "commit": "x"}))
    assert sv.health_probe("http://h/api/health", "x")
    assert not sv.health_probe("http://h/api/health", "y")
    monkeypatch.setattr(httpx, "get", lambda *a, **k: (_ for _ in ()).throw(
        httpx.ConnectError("refused")))
    assert not sv.health_probe("http://h/api/health", "x")
