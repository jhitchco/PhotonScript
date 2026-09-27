"""Safe self-update for the scope PC (PS-58).

Before: ``deploy/run-photonscript.ps1`` ran ``git pull --ff-only`` and started
whatever landed. A commit that failed to import, or started but never served
the API, left the observatory down until someone logged in.

Now the wrapper runs ``photonscript self-update`` (this module, executed by
the code that is ALREADY on disk and known to start):

1. ``git fetch``; the target is the upstream of the current branch.
2. Refuse (keep the old code, Pushover) when the tree is dirty, the target is
   not a fast-forward, the target is the SHA that was just rolled back, or a
   night is active in ``armer_state.json`` (deferred, not an error).
3. Check the target out into a STAGING worktree (``<data_dir>/update-staging``)
   and import every ``photonscript`` module from it with the service's own
   Python (catches syntax errors, missing dependencies and import-time
   crashes, including lazily imported modules). The entry modules must
   import; any other module only has to import if it does on the running
   code. Optionally run the fast test subset there (``update_smoke_tests``).
4. Only then fast-forward the real checkout, and record the switch in
   ``<data_dir>/update_state.json`` as ``pending`` with the previous SHA.

The supervisor then waits up to ``update_verify_s`` for GET /api/health to
report the new SHA. Healthy: ``good`` (the new SHA becomes ``last_good``).
Not healthy: the supervisor exits 43 and the wrapper does
``git reset --hard <prev>`` and calls ``photonscript rollback-done`` (again
from the restored, known-good code) which records ``rolled_back`` with the
``bad_sha`` and sends the alert. The next update skips that SHA until
something newer is pushed.

State file fields: status (pending | good | rejected | failed | rolled_back),
prev, target, last_good, bad_sha, reason, at (ISO UTC).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

# child of the supervisor logger: lands in logs/supervisor.log
logger = logging.getLogger("photonscript.supervisor.updater")

STATE_NAME = "update_state.json"
STAGING_NAME = "update-staging"

# self-update exit codes (read by deploy/run-photonscript.ps1)
UPDATED = 0
REFUSED = 2
UNCHANGED = 10
DEFERRED = 11

# Supervisor -> wrapper: "the new code did not come up healthy, roll back"
EXIT_ROLLBACK = 43

# Imported in the staging checkout before the switch. Every module under
# photonscript/ is imported (walk_packages), these first so a broken entry
# point is reported by name.
ENTRY_MODULES = ("photonscript.cli", "photonscript.orchestrator",
                 "photonscript.shared.supervisor", "photonscript.shared.updater",
                 "photonscript.scheduler.app")
SMOKE_TESTS = ("tests/test_supervisor.py", "tests/test_updater.py",
               "tests/test_health_api.py")

# A sequence is on the mount. ARMED (waiting for dusk) is not listed: the
# armer restores it after a restart, and POST /api/update decides about it.
_NIGHT_RUNNING = ("RUNNING", "PAUSED_UNSAFE")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- state file ------------------------------------------------------------------

def state_path(config) -> Path:
    return Path(config.data_dir) / STATE_NAME


def load_state(config) -> dict:
    try:
        d = json.loads(state_path(config).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(config, state: dict) -> None:
    p = state_path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def _update_state(config, **fields) -> dict:
    st = load_state(config)
    st.update(fields)
    st["at"] = _now()
    save_state(config, st)
    return st


def pending_target(config) -> Optional[str]:
    """The SHA waiting for its health verification, else None."""
    st = load_state(config)
    return st.get("target") if st.get("status") == "pending" else None


def mark_good(config, sha: str) -> dict:
    return _update_state(config, status="good", target=sha, last_good=sha,
                         reason="")


def mark_failed(config, reason: str) -> dict:
    """Supervisor: the new code did not come up; the wrapper rolls back."""
    return _update_state(config, status="failed", reason=reason)


def public_state(config) -> Optional[dict]:
    """For GET /api/health (deploy.ps1 reads it to spot a rollback)."""
    st = load_state(config)
    if not st:
        return None
    return {k: st.get(k) for k in ("status", "prev", "target", "last_good",
                                   "bad_sha", "reason", "at")}


# --- git ---------------------------------------------------------------------------

def _git(repo: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=True, timeout=timeout)


def _git_out(repo: Path, *args: str, timeout: float = 60) -> str:
    r = _git(repo, *args, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: "
                           f"{(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def night_active(config) -> Optional[str]:
    """Armer state from armer_state.json when a sequence is running (or
    paused unsafe) and its dawn has not passed; None otherwise. The wrapper
    also runs self-update at boot, so a reboot mid-night keeps the code."""
    try:
        saved = json.loads((Path(config.data_dir) / "armer_state.json")
                           .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    state = str(saved.get("state") or "")
    if state not in _NIGHT_RUNNING:
        return None
    dawn = (saved.get("plan") or {}).get("dawn_utc")
    if dawn:
        try:
            end = datetime.fromisoformat(str(dawn).rstrip("Z"))
            if end.tzinfo is None:
                end = end.replace(tzinfo=timezone.utc)
            if end < datetime.now(timezone.utc):
                return None                      # that night is over
        except ValueError:
            pass
    return state


# --- staging smoke check --------------------------------------------------------------

_IMPORT_ALL = r"""
import importlib, pkgutil, sys, traceback
root = sys.argv[1]
import photonscript
where = photonscript.__file__ or ""
if not where.replace("\\", "/").lower().startswith(root.replace("\\", "/").lower()):
    print("IMPORT-ISOLATION: photonscript resolved to %s, not the staging "
          "checkout %s" % (where, root))
    sys.exit(3)
bad = []
for name in sys.argv[2:]:
    try:
        importlib.import_module(name)
    except BaseException as e:
        bad.append((name, "%s: %s" % (type(e).__name__, e)))
for m in pkgutil.walk_packages(photonscript.__path__, "photonscript."):
    if m.name in sys.modules:
        continue
    try:
        importlib.import_module(m.name)
    except BaseException as e:
        bad.append((m.name, "%s: %s" % (type(e).__name__, e)))
for name, err in bad:
    print("IMPORT-FAIL %s: %s" % (name, err))
print("IMPORTED %d modules, %d failed" % (
    sum(1 for k in sys.modules if k.startswith("photonscript")), len(bad)))
sys.exit(1 if bad else 0)
"""


def _staging_env(staging: Path) -> dict:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(staging) + (os.pathsep + env["PYTHONPATH"]
                                        if env.get("PYTHONPATH") else "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def import_check(root: Path, python: str = sys.executable,
                 timeout: float = 240) -> dict:
    """Import every photonscript module from checkout ``root`` in a fresh
    ``python``. Returns {"ok", "failures": {module: error}, "isolation": str,
    "summary": str}."""
    try:
        r = subprocess.run([python, "-c", _IMPORT_ALL, str(root),
                            *ENTRY_MODULES], cwd=str(root),
                           env=_staging_env(root), capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "failures": {}, "isolation": "",
                "summary": f"import check timed out after {timeout:g} s"}
    out = (r.stdout + r.stderr).strip()
    failures, isolation = {}, ""
    for ln in out.splitlines():
        if ln.startswith("IMPORT-FAIL "):
            name, _, err = ln[len("IMPORT-FAIL "):].partition(": ")
            failures[name] = err
        elif ln.startswith("IMPORT-ISOLATION"):
            isolation = ln
    summary = next((ln for ln in reversed(out.splitlines())
                    if ln.startswith("IMPORTED ")), out[-300:])
    return {"ok": r.returncode == 0, "failures": failures,
            "isolation": isolation, "summary": summary}


def smoke_check(staging: Path, python: str = sys.executable, *,
                run_tests: bool = False, baseline: Optional[Path] = None,
                timeout: float = 240) -> tuple[bool, str]:
    """Import every photonscript module from ``staging`` with ``python``;
    optionally run SMOKE_TESTS there. (ok, detail).

    A failing entry module (cli, orchestrator, supervisor, updater, the
    scheduler app) always fails the check. Any other module fails it only if
    it imports fine from ``baseline`` (the running checkout): a module that
    already fails on the scope for environment reasons (an optional
    dependency it never loads) must not block every future update."""
    res = import_check(staging, python, timeout)
    if res["isolation"]:
        return False, res["isolation"]
    fails = res["failures"]
    if not res["ok"] and not fails:
        return False, res["summary"]
    entry = {m: e for m, e in fails.items() if m in ENTRY_MODULES}
    if entry:
        return False, "; ".join(f"{m}: {e}" for m, e in entry.items())[:600]
    note = ""
    if fails:
        known = (import_check(baseline, python, timeout)["failures"]
                 if baseline is not None else {})
        new = {m: e for m, e in fails.items() if m not in known}
        if new:
            return False, "; ".join(f"{m}: {e}" for m, e in new.items())[:600]
        note = (f"; {len(fails)} module(s) also fail on the running code: "
                + ", ".join(sorted(fails)))
    detail = res["summary"] + note
    if not run_tests:
        return True, detail
    env = _staging_env(staging)
    have = subprocess.run([python, "-c", "import pytest"], env=env,
                          capture_output=True, timeout=60)
    if have.returncode != 0:
        return True, detail + "; tests skipped (pytest not installed)"
    tests = [t for t in SMOKE_TESTS if (staging / t).exists()]
    try:
        t = subprocess.run([python, "-m", "pytest", "-q", "-x",
                            "-p", "no:cacheprovider", *tests],
                           cwd=str(staging), env=env, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"smoke tests timed out after {timeout:g} s"
    tail = (t.stdout + t.stderr).strip().splitlines()[-1:] or [""]
    if t.returncode != 0:
        return False, f"smoke tests failed: {tail[0]}"
    return True, f"{detail}; tests: {tail[0]}"


def _remove_staging(repo: Path, staging: Path) -> None:
    _git(repo, "worktree", "remove", "--force", str(staging), timeout=60)
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    _git(repo, "worktree", "prune", timeout=30)


# --- the update -----------------------------------------------------------------------

def self_update(config, repo: Path, *, python: str = sys.executable,
                run_tests: Optional[bool] = None, dry_run: bool = False,
                notify: Optional[Callable[[str, str, int], None]] = None,
                smoke: Callable[..., tuple] = smoke_check) -> tuple[int, str]:
    """Fetch, stage, smoke-check and fast-forward. Returns (rc, detail) with
    rc in UPDATED / UNCHANGED / DEFERRED / REFUSED; the old code stays in
    place for every rc except UPDATED. ``dry_run`` stages and smoke-checks
    the upstream (or HEAD when up to date) and changes nothing else."""
    repo = Path(repo)
    notify = notify or (lambda title, msg, prio=0: None)
    if run_tests is None:
        run_tests = bool(getattr(config, "update_smoke_tests", False))
    host = os.environ.get("COMPUTERNAME", "") or "scope PC"

    def refuse(reason: str, target: str = "", alert: bool = True) -> tuple[int, str]:
        _update_state(config, status="rejected", target=target, reason=reason)
        logger.warning("Update refused, keeping the current code: %s", reason)
        if alert:
            notify("PhotonScript update refused",
                   f"{host}: {reason}. Still running the previous code.", 1)
        return REFUSED, reason

    try:
        head = _git_out(repo, "rev-parse", "HEAD")
        fetch = _git(repo, "fetch", "--quiet", timeout=120)
        if fetch.returncode != 0:
            return refuse(f"git fetch failed: {(fetch.stderr or '').strip()[:200]}")
        try:
            target = _git_out(repo, "rev-parse", "@{u}")
        except RuntimeError:
            target = _git_out(repo, "rev-parse", "origin/main")
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
        return refuse(f"git error: {e}")

    if dry_run:
        return _dry_run(config, repo, target, python, bool(run_tests), smoke)
    if target == head:
        return UNCHANGED, f"up to date at {head[:7]}"
    st = load_state(config)
    if st.get("bad_sha") == target:
        return refuse(f"{target[:7]} was rolled back earlier (bad start); "
                      "push a fix to deploy again", target, alert=False)
    active = night_active(config)
    if active:
        logger.info("Update to %s deferred: armer is %s", target[:7], active)
        return DEFERRED, f"night active ({active}); update deferred"
    if _git(repo, "merge-base", "--is-ancestor", head, target).returncode != 0:
        return refuse(f"{target[:7]} is not a fast-forward of {head[:7]}", target)
    dirty = _git(repo, "status", "--porcelain", "--untracked-files=no")
    if dirty.returncode != 0 or dirty.stdout.strip():
        return refuse("the scope checkout has local changes: "
                      + " ".join(dirty.stdout.split()[:6]), target)

    staging = Path(config.data_dir) / STAGING_NAME
    try:
        _remove_staging(repo, staging)
        add = _git(repo, "worktree", "add", "--detach", "--force",
                   str(staging), target, timeout=120)
        if add.returncode != 0:
            return refuse(f"could not stage {target[:7]}: "
                          f"{(add.stderr or '').strip()[:200]}", target)
        ok, detail = smoke(staging, python, run_tests=run_tests,
                           baseline=repo)
    finally:
        _remove_staging(repo, staging)
    if not ok:
        return refuse(f"{target[:7]} failed the smoke check ({detail})", target)

    ff = _git(repo, "merge", "--ff-only", "--quiet", target, timeout=120)
    if ff.returncode != 0:
        return refuse(f"fast-forward to {target[:7]} failed: "
                      f"{(ff.stderr or '').strip()[:200]}", target)
    last_good = st.get("last_good") or head
    _update_state(config, status="pending", prev=head, target=target,
                  last_good=last_good, reason=detail)
    logger.info("Updated %s -> %s (%s); waiting for the health check",
                head[:7], target[:7], detail)
    return UPDATED, f"{head[:7]} -> {target[:7]} ({detail})"


def _dry_run(config, repo: Path, target: str, python: str, run_tests: bool,
             smoke: Callable[..., tuple]) -> tuple[int, str]:
    staging = Path(config.data_dir) / STAGING_NAME
    try:
        _remove_staging(repo, staging)
        add = _git(repo, "worktree", "add", "--detach", "--force",
                   str(staging), target, timeout=120)
        if add.returncode != 0:
            return REFUSED, f"could not stage {target[:7]}: {add.stderr.strip()[:200]}"
        ok, detail = smoke(staging, python, run_tests=run_tests,
                           baseline=repo)
    finally:
        _remove_staging(repo, staging)
    return (UNCHANGED if ok else REFUSED), f"dry run {target[:7]}: {detail}"


def rollback_done(config, reason: str = "",
                  notify: Optional[Callable[[str, str, int], None]] = None) -> dict:
    """Called by the wrapper AFTER it reset the checkout to ``prev`` (so this
    runs the restored code). Records the bad SHA and alerts once."""
    notify = notify or (lambda title, msg, prio=0: None)
    st = load_state(config)
    bad = st.get("target") or ""
    prev = st.get("prev") or ""
    why = reason or st.get("reason") or "no healthy /api/health"
    st = _update_state(config, status="rolled_back", bad_sha=bad,
                       target=prev, reason=why)
    host = os.environ.get("COMPUTERNAME", "") or "scope PC"
    notify("PhotonScript rolled back",
           f"{host}: {bad[:7]} did not come up healthy ({why}). Rolled back "
           f"to {prev[:7]}; that SHA is skipped until a newer one is pushed.", 1)
    return st
