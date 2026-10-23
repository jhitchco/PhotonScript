"""PS-58: deploy/run-photonscript.ps1 update/rollback loop, run for real under
PowerShell 7 against a throwaway git repo and a fake `photonscript` exe.
Skipped where pwsh is not installed."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

PWSH = shutil.which("pwsh")
WRAPPER = Path(__file__).resolve().parents[1] / "deploy" / "run-photonscript.ps1"

pytestmark = [
    pytest.mark.skipif(PWSH is None, reason="pwsh not installed"),
    pytest.mark.skipif(os.name == "nt", reason="fake exe is a bash script"),
]

FAKE_EXE = r"""#!/bin/bash
# fake photonscript: logs calls, simulates self-update / supervise / rollback-done
echo "$*" >> "$FAKE/calls"
state="$DATA/update_state.json"
case "$1" in
  self-update)
    if [ -f "$FAKE/update_to" ]; then
      to=$(cat "$FAKE/update_to"); rm "$FAKE/update_to"
      prev=$(git -C "$REPO" rev-parse HEAD)
      git -C "$REPO" merge -q --ff-only "$to" || exit 2
      printf '{"status":"pending","prev":"%s","target":"%s","reason":""}' "$prev" "$to" > "$state"
      echo "updated"; exit 0
    fi
    exit 10 ;;
  supervise)
    rc=$(head -n1 "$FAKE/rcs"); sed -i '1d' "$FAKE/rcs"
    if [ "$rc" = "43" ]; then
      python3 - "$state" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p)); d.update(status="failed", reason="no health in 90 s")
json.dump(d, open(p, "w"))
PY
    fi
    exit "$rc" ;;
  rollback-done)
    python3 - "$state" <<'PY'
import json, sys
p = sys.argv[1]; d = json.load(open(p))
d.update(status="rolled_back", bad_sha=d["target"], target=d["prev"])
json.dump(d, open(p, "w"))
PY
    exit 0 ;;
esac
exit 0
"""


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def rig(tmp_path, monkeypatch):
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@x")
    repo, data, fake = tmp_path / "repo", tmp_path / "data", tmp_path / "fake"
    for d in (repo, data, fake):
        d.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "app.txt").write_text("A\n")
    (repo / ".gitignore").write_text(".env\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "A")
    a = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "next")
    (repo / "app.txt").write_text("B\n")
    _git(repo, "commit", "-q", "-am", "B")
    b = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "main")
    (repo / ".env").write_text(f"PS_DATA_DIR={data}\n")
    exe = fake / "photonscript"
    exe.write_text(FAKE_EXE)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, FAKE=str(fake), DATA=str(data), REPO=str(repo),
               USERPROFILE=str(tmp_path))

    def run(rcs, update_to=None):
        (fake / "rcs").write_text("\n".join(str(r) for r in rcs) + "\n")
        if update_to:
            (fake / "update_to").write_text(update_to)
        p = subprocess.run([PWSH, "-NoProfile", "-File", str(WRAPPER),
                            "-Repo", str(repo), "-Exe", str(exe)],
                           env=env, capture_output=True, text=True, timeout=120)
        calls = (fake / "calls").read_text().splitlines()
        log = (data / "logs" / "wrapper.log").read_text()
        st = json.loads((data / "update_state.json").read_text()) \
            if (data / "update_state.json").exists() else {}
        return p.returncode, calls, log, st

    return {"repo": repo, "A": a, "B": b, "run": run}


def _head(rig):
    return _git(rig["repo"], "rev-parse", "HEAD")


def test_unhealthy_update_is_rolled_back(rig):
    rc, calls, log, st = rig["run"]([43, 0], update_to=rig["B"])
    assert rc == 0, log
    assert _head(rig) == rig["A"]                      # reset to the old SHA
    assert [c.split()[0] for c in calls] == [
        "self-update", "supervise", "rollback-done", "supervise"]
    assert "--reason no health in 90 s" in calls[2]
    assert f"ROLLED BACK {rig['B']} -> {rig['A']}" in log
    assert "not updating after a rollback" in log
    assert st["status"] == "rolled_back" and st["bad_sha"] == rig["B"]


def test_supervisor_dying_right_after_update_is_rolled_back(rig):
    rc, calls, log, st = rig["run"]([1, 0], update_to=rig["B"])
    assert rc == 0, log
    assert _head(rig) == rig["A"]
    assert "supervisor exited 1 right after the update" in calls[2]


def test_update_request_loops_back_through_self_update(rig):
    rc, calls, log, _ = rig["run"]([42, 0])
    assert rc == 0
    assert [c.split()[0] for c in calls] == [
        "self-update", "supervise", "self-update", "supervise"]
    assert "update requested (42)" in log


def test_plain_exit_without_pending_update_ends_the_wrapper(rig):
    rc, calls, log, _ = rig["run"]([1])
    assert rc == 1
    assert [c.split()[0] for c in calls] == ["self-update", "supervise"]
    assert "ROLLED BACK" not in log
    assert _head(rig) == rig["A"]


def test_healthy_update_stays(rig):
    rc, calls, log, st = rig["run"]([0], update_to=rig["B"])
    assert rc == 0 and _head(rig) == rig["B"]
    assert "updated: updated" in log and "ROLLED BACK" not in log
