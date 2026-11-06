"""PS-44: supervisor policy and loop.

The loop is exercised with real child processes (tiny `python -c` scripts that
exit with chosen codes or drop marker files), with sleep/clock injected so the
backoff costs no wall time.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared import process_control as pc
from photonscript.shared import supervisor as sv


def _cfg(tmp_path):
    return PhotonScriptConfig(data_dir=tmp_path)


# --- decide() ------------------------------------------------------------------

@pytest.mark.parametrize("rc,hold,restart,intr,want", [
    (0, True, False, False, sv.STOP),        # photonscript stop
    (1, True, False, False, sv.STOP),        # photonscript stop --force (taskkill)
    (42, True, False, False, sv.STOP),       # operator stop beats an update
    (1, False, False, True, sv.STOP),        # Ctrl-C at the supervisor
    (42, False, False, False, sv.UPDATE),
    (0, False, True, False, sv.RESTART),     # photonscript restart
    (3, False, False, False, sv.ALREADY),
    (1, False, False, False, sv.CRASH),
    (0, False, False, False, sv.CRASH),      # clean exit nobody asked for
    (-9, False, False, False, sv.CRASH),
])
def test_decide(rc, hold, restart, intr, want):
    assert sv.decide(rc, hold=hold, restart=restart, interrupted=intr) == want


# --- RestartPolicy ---------------------------------------------------------------

def test_backoff_doubles_and_caps():
    p = sv.RestartPolicy(loop_max=100)
    delays = [p.record_crash(now=i * 1000.0, ran_for_s=1) for i in range(9)]
    assert delays == [5, 10, 20, 40, 80, 160, 300, 300, 300]


def test_backoff_resets_after_healthy_run():
    p = sv.RestartPolicy(loop_max=100)
    assert p.record_crash(0, 1) == 5
    assert p.record_crash(1000, 1) == 10
    assert p.record_crash(5000, 3600) == 5   # ran an hour: back to the start


def test_crash_loop_gives_up_inside_window():
    p = sv.RestartPolicy()
    got = [p.record_crash(t, 1) for t in (0, 60, 120, 180)]
    assert None not in got
    assert p.record_crash(240, 1) is None    # 5th crash inside 15 min


def test_crash_loop_window_slides():
    p = sv.RestartPolicy()
    for t in (0, 60, 120, 180):
        assert p.record_crash(t, 1) is not None
    # 20 min later the old crashes have aged out of the window
    assert p.record_crash(1200 + 180, 1) is not None


# --- run() with real child processes --------------------------------------------

def _child(code: str) -> list:
    return [sys.executable, "-c", code]


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


def _run(cfg, cmd, **kw):
    alerts = []
    rc = sv.run(cfg, cmd, notify=lambda t, m, p=0: alerts.append((t, m, p)),
                sleep=lambda s: None, clock=_Clock(), host="test", **kw)
    return rc, alerts


def test_update_exit_is_passed_up(tmp_path):
    rc, alerts = _run(_cfg(tmp_path), _child("import sys; sys.exit(42)"))
    assert rc == sv.EXIT_UPDATE
    assert alerts == []
    assert not sv.supervisor_pid_path(_cfg(tmp_path)).exists()


def test_crash_then_operator_stop(tmp_path):
    cfg = _cfg(tmp_path)
    counter = tmp_path / "n"
    hold = sv.hold_path(cfg)
    code = (
        "import sys, pathlib\n"
        f"c = pathlib.Path({str(counter)!r})\n"
        "n = int(c.read_text()) + 1 if c.exists() else 1\n"
        "c.write_text(str(n))\n"
        f"if n >= 3: pathlib.Path({str(hold)!r}).write_text('x'); sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    rc, alerts = _run(cfg, _child(code))
    assert rc == sv.EXIT_OK
    assert counter.read_text() == "3"          # crashed twice, stopped on the 3rd
    assert [a[0] for a in alerts] == ["PhotonScript crashed"] * 2
    assert all(a[2] == 1 for a in alerts)
    assert hold.exists()                        # the supervisor leaves HOLD in place


def test_crash_loop_gives_up_with_one_alert(tmp_path):
    cfg = _cfg(tmp_path)
    policy = sv.RestartPolicy(loop_max=3)
    rc, alerts = _run(cfg, _child("import sys; sys.exit(7)"), policy=policy)
    assert rc == sv.EXIT_OK
    titles = [a[0] for a in alerts]
    assert titles == ["PhotonScript crashed", "PhotonScript crashed",
                      "PhotonScript is down"]
    assert "last exit 7" in alerts[-1][1]


def test_restart_marker_restarts_without_alert(tmp_path):
    cfg = _cfg(tmp_path)
    counter = tmp_path / "n"
    code = (
        "import sys, pathlib\n"
        f"c = pathlib.Path({str(counter)!r})\n"
        "n = int(c.read_text()) + 1 if c.exists() else 1\n"
        "c.write_text(str(n))\n"
        f"if n == 1: pathlib.Path({str(sv.restart_path(cfg))!r}).write_text('r'); sys.exit(0)\n"
        "sys.exit(42)\n"
    )
    rc, alerts = _run(cfg, _child(code))
    assert rc == sv.EXIT_UPDATE and counter.read_text() == "2"
    assert alerts == []
    assert not sv.restart_path(cfg).exists()


def test_already_running_child_ends_supervision(tmp_path):
    rc, alerts = _run(_cfg(tmp_path), _child("import sys; sys.exit(3)"))
    assert rc == sv.EXIT_OK and alerts == []


def test_second_supervisor_refuses(tmp_path):
    cfg = _cfg(tmp_path)
    # Pretend a live supervisor (our parent process) owns the pid file.
    sv.supervisor_pid_path(cfg).write_text(str(os.getppid()))
    rc, _ = _run(cfg, _child("import sys; sys.exit(0)"))
    assert rc == sv.EXIT_ALREADY_RUNNING
    assert sv.supervisor_pid_path(cfg).read_text() == str(os.getppid())


def test_stale_hold_is_cleared_on_start(tmp_path):
    cfg = _cfg(tmp_path)
    sv.create_hold(cfg)
    rc, _ = _run(cfg, _child("import sys; sys.exit(42)"))
    assert rc == sv.EXIT_UPDATE                 # HOLD from last time did not block


def test_hold_during_backoff_stops(tmp_path):
    cfg = _cfg(tmp_path)
    slept = []

    def sleep(s):
        slept.append(s)
        sv.create_hold(cfg)                      # operator runs `stop` mid-backoff

    rc = sv.run(cfg, _child("import sys; sys.exit(1)"), sleep=sleep,
                clock=_Clock(), notify=lambda *a: None)
    assert rc == sv.EXIT_OK and slept == [1.0]


def test_pid_file_kept_when_child_did_not_write_it(tmp_path):
    cfg = _cfg(tmp_path)
    pc.write_pid_file(cfg, pid=os.getppid())    # another live instance
    rc, _ = _run(cfg, _child("import sys; sys.exit(3)"))
    assert rc == sv.EXIT_OK
    assert pc.read_pid_file(cfg) == os.getppid()


def test_child_pid_file_cleared_after_hard_exit(tmp_path):
    cfg = _cfg(tmp_path)
    pidfile = pc.pid_file_path(cfg)
    code = (
        "import os, sys, pathlib\n"
        f"pathlib.Path({str(pidfile)!r}).write_text(str(os.getpid()))\n"
        "os._exit(42)\n"
    )
    rc, _ = _run(cfg, _child(code))
    assert rc == sv.EXIT_UPDATE
    assert not pidfile.exists()


# --- CLI ---------------------------------------------------------------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PS_DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)                  # no .env from the repo
    from photonscript import cli
    return cli, CliRunner(), _cfg(tmp_path)


def test_stop_sets_hold_even_when_not_running(cli_env):
    cli, runner, cfg = cli_env
    r = runner.invoke(cli.app, ["stop"])
    assert r.exit_code == 0
    assert sv.hold_active(cfg)


def test_start_refuses_second_copy_with_exit_3(cli_env):
    cli, runner, cfg = cli_env
    pc.write_pid_file(cfg, pid=os.getpid())      # a live pid
    r = runner.invoke(cli.app, ["start", "--mode", "scheduler"])
    assert r.exit_code == sv.EXIT_ALREADY_RUNNING


def test_restart_needs_a_supervisor(cli_env):
    cli, runner, cfg = cli_env
    pc.write_pid_file(cfg, pid=os.getpid())
    r = runner.invoke(cli.app, ["restart"])
    assert r.exit_code == 1
    assert not sv.restart_path(cfg).exists()


def test_restart_with_supervisor_drops_markers(cli_env):
    cli, runner, cfg = cli_env
    pc.write_pid_file(cfg, pid=os.getpid())
    sv.supervisor_pid_path(cfg).write_text(str(os.getppid()))
    r = runner.invoke(cli.app, ["restart"])
    assert r.exit_code == 0
    assert sv.restart_path(cfg).exists()
    assert pc.stop_sentinel_path(cfg).exists()


def test_notify_cli_without_keys(cli_env):
    cli, runner, _ = cli_env
    r = runner.invoke(cli.app, ["notify", "hello", "--title", "t"])
    assert r.exit_code == 0 and "not sent" in r.output


def test_status_reports_local_process(cli_env):
    cli, runner, cfg = cli_env
    pc.write_pid_file(cfg, pid=os.getpid())
    sv.create_hold(cfg)
    r = runner.invoke(cli.app, ["status", "--url", "http://127.0.0.1:9"])
    assert r.exit_code == 0
    assert f"pid {os.getpid()}" in r.output
    assert "HOLD" in r.output
    assert "Cannot reach" in r.output


def test_wrapper_script_is_ascii():
    root = Path(__file__).resolve().parents[1] / "deploy"
    for name in ("run-photonscript.ps1", "install-autostart.ps1"):
        (root / name).read_bytes().decode("ascii")
