"""One PixInsight queue at a time (integration/pilock.py): the lock file
honored by integrate / blend / finish / integrate-watch / autointegrate and
the hand-run deploy launchers (deploy/pixinsight-lock.ps1). conftest gives
every test its own lock file (PS_PIXINSIGHT_LOCK)."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from photonscript.integration import pilock, runner
from photonscript.integration import pipeline as pl
from photonscript.integration import watch
from tests.test_integration.test_pipeline import _opts, lib  # noqa: F401
from tests.test_integration.test_ps31_33_watch import NOW, Fakes, _cand, _opts_w

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
PS = shutil.which("powershell") or shutil.which("pwsh")
QUIET = dict(echo=lambda s: None)


def _other(*, pid=4, command="integrate M 27 [rc16]", **extra):
    """A lock held by another process (the tests fake pid liveness where it
    matters; the pid only has to differ from ours)."""
    p = pilock.lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    info = {"pid": pid, "pid_created": None, "command": command,
            "started": "2026-10-12T23:10:00", "started_epoch": time.time(), "host": "DESK", **extra}
    p.write_text(json.dumps(info), encoding="ascii")
    return info


@pytest.fixture
def others_alive(monkeypatch):
    monkeypatch.setattr(pilock, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(pilock, "_pid_created", lambda pid: None)


def test_lock_path_default_and_env(monkeypatch, tmp_path):
    monkeypatch.delenv(pilock.ENV_PATH)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LA"))
    assert pilock.lock_path() == tmp_path / "LA" / "PhotonScript" / "pixinsight.lock"
    monkeypatch.setenv(pilock.ENV_PATH, str(tmp_path / "x.lock"))
    assert pilock.lock_path() == tmp_path / "x.lock"


def test_acquire_writes_the_holder_and_release_removes_it():
    lk = pilock.acquire("integrate M 27 [rc16]", **QUIET)
    info = json.loads(pilock.lock_path().read_text("ascii"))
    assert info["pid"] == os.getpid() and info["command"] == "integrate M 27 [rc16]"
    assert info["started"] and info["started_epoch"] > 0 and "pid_created" in info
    lk.release()
    assert not pilock.lock_path().exists()
    lk.release()                                    # twice is harmless


def test_lock_is_reentrant_inside_one_process():
    with pilock.acquire("integrate X", **QUIET):
        with pilock.acquire("PixInsight integrate_run.js", **QUIET) as inner:
            assert inner.info["command"] == "integrate X"
        assert pilock.lock_path().exists()          # the inner release keeps the job's lock
    assert not pilock.lock_path().exists()


def test_a_live_holder_refuses_with_a_clear_message(others_alive):
    _other()
    with pytest.raises(pilock.PixInsightLocked) as e:
        pilock.acquire("blend M31", wait_s=0, **QUIET)
    msg = str(e.value)
    assert "PixInsight queue busy" in msg and "'integrate M 27 [rc16]' (pid 4" in msg
    assert "2026-10-12T23:10:00" in msg and str(pilock.lock_path()) in msg
    assert isinstance(e.value, runner.PixInsightBusy)    # the CLIs already report this one
    assert json.loads(pilock.lock_path().read_text())["pid"] == 4   # left alone


def test_a_second_job_waits_bounded_then_gives_up(others_alive):
    _other()
    t = [0.0]
    sleeps, said = [], []

    def sleep(s):
        sleeps.append(s)
        t[0] += s
    with pytest.raises(pilock.PixInsightLocked) as e:
        pilock.acquire("finish X", wait_s=90, poll_s=30, sleep=sleep, clock=lambda: t[0],
                       echo=said.append)
    assert sum(sleeps) == pytest.approx(90) and "waited 2 min" in str(e.value)
    assert any("waiting up to" in s for s in said)


def test_a_waiting_job_starts_when_the_holder_ends(others_alive):
    _other()
    t = [0.0]

    def sleep(s):
        t[0] += s
        if t[0] >= 60:
            pilock.lock_path().unlink(missing_ok=True)   # the other job finished
    lk = pilock.acquire("integrate Y", wait_s=3600, poll_s=30, sleep=sleep, clock=lambda: t[0],
                        **QUIET)
    assert t[0] == 60 and json.loads(pilock.lock_path().read_text())["command"] == "integrate Y"
    lk.release()


@pytest.mark.parametrize("info,alive,created,why", [
    ({"pid": 4}, False, None, "not running"),
    ({"pid": 4, "pid_created": 1000.0}, True, 5000.0, "another process"),
    ({"pid": 4, "starting": True, "started_epoch": time.time() - 600}, True, None, "never started"),
    ({"pid": 0}, True, None, "no pid"),
    ({"unreadable": True, "age_s": 120}, True, None, "unreadable"),
])
def test_stale_locks(info, alive, created, why):
    assert why in pilock.stale_reason(info, alive=lambda p: alive, created=lambda p: created)


@pytest.mark.parametrize("info", [
    {"pid": 4, "pid_created": 1000.0},
    {"pid": 4, "starting": True, "started_epoch": time.time() - 10},
    {"unreadable": True, "age_s": 2},
])
def test_held_locks(info):
    assert pilock.stale_reason(info, alive=lambda p: True, created=lambda p: 1000.5) == ""


def test_a_stale_lock_is_taken_over(monkeypatch):
    monkeypatch.setattr(pilock, "_pid_alive", lambda pid: pid == os.getpid())
    _other(pid=999999)
    said = []
    lk = pilock.acquire("integrate Z", echo=said.append)
    assert json.loads(pilock.lock_path().read_text())["pid"] == os.getpid()
    assert any("stale lock (pid 999999 is not running" in s for s in said)
    lk.release()


def test_half_written_lock_waits_old_one_is_taken():
    p = pilock.lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{", encoding="ascii")
    with pytest.raises(pilock.PixInsightLocked):
        pilock.acquire("x", **QUIET)
    old = time.time() - 300
    os.utime(p, (old, old))
    pilock.acquire("x", **QUIET).release()


def test_release_never_deletes_a_lock_another_job_took_over():
    lk = pilock.acquire("integrate A", **QUIET)
    pilock._held.clear()
    _other(command="taken over")
    lk.release()
    assert json.loads(pilock.lock_path().read_text())["command"] == "taken over"


def test_job_is_a_no_op_when_disabled_and_wait_min_from_config():
    with pilock.job("integrate dry", enabled=False):
        assert not pilock.lock_path().exists()

    class C:
        pixinsight_lock_wait_min = 15
    assert pilock.wait_min(C()) == 15.0
    assert pilock.wait_min(object()) == 60.0
    C.pixinsight_lock_wait_min = -3
    assert pilock.wait_min(C()) == 0.0


def test_run_script_refuses_while_another_job_holds_the_lock(tmp_path, others_alive):
    _other()
    exe = tmp_path / "PixInsight.exe"
    exe.write_text("x")
    started = []
    with pytest.raises(pilock.PixInsightLocked):
        runner.run_script(tmp_path / "s.js", tmp_path / "l.log", exe=str(exe),
                          running=lambda: [], popen=lambda a: started.append(a), **QUIET)
    assert started == []


# --- the jobs ------------------------------------------------------------------

def _fake_run(seen):
    def fake_run(script, log_path, **kw):
        info = json.loads(pilock.lock_path().read_text())
        seen.append((Path(script).name, info["pid"], info["command"]))
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text("EXIT OK\n")
        return {"ok": True, "exit_code": 0, "minutes": 0.0, "last_line": "EXIT OK", "timed_out": False}
    return fake_run


def test_integrate_holds_the_lock_across_all_its_launches(lib, tmp_path, monkeypatch):  # noqa: F811
    seen = []
    monkeypatch.setattr(pl.runner, "run_script", _fake_run(seen))
    pl.run(_opts(lib, tmp_path, out=tmp_path / "Staging" / "r1", run_pixinsight=True), **QUIET)
    assert [s[0] for s in seen] == ["integrate_run.js", "finish_run.js"]
    assert all(s[1] == os.getpid() and s[2] == "integrate M31 [piggyback]" for s in seen)
    assert not pilock.lock_path().exists()


def test_integrate_stops_before_staging_while_another_job_runs(lib, tmp_path, others_alive):  # noqa: F811
    _other()
    out = tmp_path / "Staging" / "r2"
    with pytest.raises(runner.PixInsightBusy, match="PixInsight queue busy"):
        pl.run(_opts(lib, tmp_path, out=out, run_pixinsight=True, lock_wait_min=0), **QUIET)
    assert not out.exists()                           # nothing staged


def test_dry_run_and_no_pixinsight_take_no_lock(lib, tmp_path, others_alive):  # noqa: F811
    _other()
    pl.run(_opts(lib, tmp_path, dry_run=True), **QUIET)
    pl.run(_opts(lib, tmp_path, out=tmp_path / "Staging" / "np", run_pixinsight=False), **QUIET)


def test_config_default_system_page_and_integrate_options():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    from photonscript.shared.config import PhotonScriptConfig
    c = PhotonScriptConfig(_env_file=None)
    assert c.pixinsight_lock_wait_min == 60.0
    f = {x[0]: x for x in _CONFIG_FIELDS}["pixinsight_lock_wait_min"]
    assert f[1] == "PS_PIXINSIGHT_LOCK_WAIT_MIN" and f[3] == "Integration" and f[4] == "float"
    assert pl.config_options(c, "rc16")["lock_wait_min"] == 60.0
    c2 = PhotonScriptConfig(_env_file=None, pixinsight_lock_wait_min=5)
    assert pl.config_options(c2, "rc16")["lock_wait_min"] == 5.0


def test_watch_cycle_skips_while_the_queue_is_busy(tmp_path, others_alive):
    _other()
    o = _opts_w(tmp_path)
    fk = Fakes([_cand()])
    said = []
    out = watch.cycle(o, run_integrate=fk.run_integrate(o.staging_root), get=fk.get, post=fk.post,
                      pi_running=lambda: [], echo=said.append, now=NOW)
    assert out["skipped"] == "pixinsight queue busy" and fk.runs == []
    assert any("'integrate M 27 [rc16]'" in s for s in said)
    assert not (o.staging_root / watch.LOCK_NAME).exists()


def test_watch_cycle_holds_the_lock_while_it_integrates(tmp_path):
    o = _opts_w(tmp_path)
    fk = Fakes([_cand()])
    held = []
    inner = fk.run_integrate(o.staging_root)

    def run_integrate(t, rig, trig):
        held.append(json.loads(pilock.lock_path().read_text())["command"])
        return inner(t, rig, trig)
    watch.cycle(o, run_integrate=run_integrate, get=fk.get, post=fk.post,
                pi_running=lambda: [], echo=lambda s: None, now=NOW)
    assert held == ["integrate-watch"] and not pilock.lock_path().exists()


def test_autointegrate_cycle_skips_while_the_queue_is_busy(tmp_path, others_alive):
    from tests.test_integration import test_ps161_autointegrate as t161
    _other()
    o = t161._opts(tmp_path, t161._lib(tmp_path), settle_min=0)
    fk = t161.Integrate([t161._cand()])
    out = t161._cycle(o, fk)
    assert out["skipped"] == "pixinsight queue busy" and fk.runs == []


# --- the hand-run launchers ----------------------------------------------------

@pytest.mark.parametrize("name", ["run-integration.ps1", "run-integration-osc.ps1",
                                  "run-finish-osc.ps1", "prepare-integration.ps1"])
def test_launchers_take_the_lock_before_starting_pixinsight(name):
    s = (DEPLOY / name).read_text("utf-8")
    assert "pixinsight-lock.ps1" in s and "Enter-PixInsightLock" in s
    starts = [k for k in range(len(s)) if s.startswith("Start-Process", k)]
    assert starts
    for i in starts:
        assert s.rfind("Enter-PixInsightLock", 0, i) >= 0, f"{name}: Start-Process before the lock"
    assert "Set-PixInsightLockOwner" in s or "Remove-PixInsightLock" in s
    assert "& $PixInsight" not in s                  # a pid is needed for the lock owner


def test_lock_helper_is_ascii_and_matches_the_python_rules():
    s = (DEPLOY / "pixinsight-lock.ps1").read_text("ascii")
    assert "PS_PIXINSIGHT_LOCK" in s and "PhotonScript\\pixinsight.lock" in s
    assert f"PiLockStartingStaleS = {int(pilock.STARTING_STALE_S)}" in s
    assert f"PiLockUnreadableStaleS = {int(pilock.UNREADABLE_STALE_S)}" in s
    assert chr(0x2014) not in s


def _ps(script: str, lock: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PS_PIXINSIGHT_LOCK=str(lock))
    body = ". '" + str(DEPLOY / "pixinsight-lock.ps1") + "'\n" + script
    return subprocess.run([PS, "-NoProfile", "-NonInteractive", "-Command", body], env=env,
                          capture_output=True, text=True, timeout=120)


@pytest.mark.skipif(PS is None, reason="PowerShell not installed")
def test_ps1_lock_round_trip_with_python(tmp_path, monkeypatch):
    lock = tmp_path / "ps" / "pixinsight.lock"
    monkeypatch.setenv(pilock.ENV_PATH, str(lock))
    # a launcher's lock, owned by a live process (this one), holds for Python
    r = _ps('Enter-PixInsightLock -Command "run-integration.ps1 M 27" -WaitMin 0\n'
            f'$p = Get-Process -Id {os.getpid()}\n'
            'Set-PixInsightLockOwner -Process $p -Command "run-integration.ps1 M 27"', lock)
    assert r.returncode == 0, r.stderr
    info = json.loads(lock.read_text("ascii"))
    assert info["pid"] == os.getpid() and info["command"] == "run-integration.ps1 M 27"
    assert not info.get("starting") and info["pid_created"]
    assert pilock.stale_reason(info) == ""
    with pytest.raises(pilock.PixInsightLocked):
        pilock.acquire("integrate X", **QUIET)
    # a launcher refuses a live lock (exit 3, names the holder)
    r = _ps('Enter-PixInsightLock -Command "run-finish-osc.ps1 X" -WaitMin 0', lock)
    assert r.returncode == 3 and "PixInsight queue busy" in (r.stderr + r.stdout)
    assert "run-integration.ps1 M 27" in (r.stderr + r.stdout)
    # and takes over a stale one (dead pid)
    lock.write_text(json.dumps({"pid": 999999, "command": "gone", "started": "x",
                                "started_epoch": 0}), encoding="ascii")
    r = _ps('Enter-PixInsightLock -Command "run-finish-osc.ps1 X" -WaitMin 0\n'
            'Remove-PixInsightLock', lock)
    assert r.returncode == 0, r.stderr
    assert "taking over a stale lock" in r.stdout and not lock.exists()
    # the launcher's own "starting" lock ends with its shell (a dead pid for Python)
    r = _ps('Enter-PixInsightLock -Command "starting" -WaitMin 0', lock)
    assert r.returncode == 0, r.stderr
    info = json.loads(lock.read_text("ascii"))
    assert info["starting"] is True
    assert "not running" in pilock.stale_reason(info)
    pilock.acquire("integrate after", **QUIET).release()   # Python takes it over
