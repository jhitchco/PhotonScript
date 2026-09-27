"""PS-34a: `photonscript autostart-check` (shared/autostart_check.py).

Every side effect (PowerShell, registry, process list, HTTP, git, files, kill,
clock) is faked through autostart_check.Env, so these run anywhere. One test
runs the real Get-ScheduledTask query script in PowerShell with the two
cmdlets stubbed; it is skipped when no PowerShell is installed.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from photonscript.shared import autostart_check as ac

REAL_ENV = ac.Env     # the CLI tests monkeypatch ac.Env

REPO = Path(r"C:\astro\PhotonScript")
NOW = datetime(2026, 9, 27, 13, 0, 0)            # local, naive (the log clock)
SHA = "23f9f4b51dfa6b3f918944134d27ab12478c610c"
ARGS = ('-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File '
        '"C:\\astro\\PhotonScript\\deploy\\run-photonscript.ps1" -Launcher task-Interactive')


def task_info(**kw):
    d = {
        "found": True, "state": "Running", "enabled": True,
        "logon_type": "Interactive", "user": "SCOPE\\jeremy", "run_level": "Limited",
        "triggers": [{"kind": "MSFT_TaskLogonTrigger", "enabled": True,
                      "delay": "PT30S", "user": "SCOPE\\jeremy"}],
        "actions": [{"execute": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
                     "arguments": ARGS, "workdir": str(REPO)}],
        "time_limit": "PT0S", "priority": 4, "multiple_instances": "IgnoreNew",
        "last_run": "2026-09-27T12:40:00.0000000-06:00", "last_result": 0x41301,
        "next_run": None,
    }
    d.update(kw)
    return d


def started_at(local: datetime) -> str:
    return local.astimezone(timezone.utc).isoformat(timespec="seconds")


def health(**kw):
    d = {
        "ok": True, "version": "23f9f4b (Sep 27 13:02)", "commit": SHA, "mode": "full",
        "started_at": started_at(NOW - timedelta(minutes=20)), "uptime_s": 1200.0,
        "pid": 3684, "armer": "DISARMED",
        "loop": {"lag_ms": 12.0, "max_lag_ms_5min": 180.0, "stalls_5min": 0,
                 "last_stall_at": None, "heartbeat_age_s": 0.2},
        "process": {"pid": 3684, "user": "jeremy", "launcher": "task-Interactive",
                    "session_id": 2, "priority_class": "normal",
                    "power_throttling": "off", "elevated": False},
    }
    for k, v in kw.items():
        if k in ("launcher", "session_id", "priority_class", "power_throttling",
                 "elevated", "user"):
            d["process"][k] = v
        else:
            d[k] = v
    return d


PROCS = [
    {"pid": 100, "ppid": 4, "name": "powershell.exe", "cmdline": [
        r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "-NoProfile",
        "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File",
        r"C:\astro\PhotonScript\deploy\run-photonscript.ps1", "-Launcher",
        "task-Interactive"]},
    {"pid": 200, "ppid": 100, "name": "photonscript.exe", "cmdline": [
        r"C:\astro\venv\Scripts\photonscript.exe", "supervise", "--mode", "full"]},
    {"pid": 201, "ppid": 200, "name": "python.exe", "cmdline": [
        r"C:\astro\venv\Scripts\python.exe", r"C:\astro\venv\Scripts\photonscript.exe",
        "supervise", "--mode", "full"]},
    {"pid": 300, "ppid": 201, "name": "python.exe", "cmdline": [
        r"C:\astro\venv\Scripts\python.exe", "-m", "photonscript.cli", "start",
        "--mode", "full"]},
    {"pid": 3684, "ppid": 300, "name": "python.exe", "cmdline": [
        r"C:\Python312\python.exe", "-m", "photonscript.cli", "start", "--mode", "full"]},
    {"pid": 500, "ppid": 4, "name": "NINA.exe", "cmdline": [r"C:\NINA\NINA.exe"]},
    {"pid": 600, "ppid": 4, "name": "photonscript.exe", "cmdline": [
        r"C:\astro\venv\Scripts\photonscript.exe", "autostart-check"]},
]

SUP_LOG = "\n".join([
    "2026-09-27 12:40:31 [supervisor] INFO    Supervisor started (pid 200): python -m photonscript.cli start --mode full",
    "2026-09-27 12:40:31 [supervisor] INFO    Started PhotonScript (pid 300, start #1)",
])


class Fake:
    """Builds an ac.Env from plain data."""

    def __init__(self, tmp: Path, **kw):
        self.tmp = tmp
        self.task = kw.pop("task", task_info())
        self.task_err = kw.pop("task_err", "")
        self.winlogon = kw.pop("winlogon", {"AutoAdminLogon": "1",
                                            "DefaultUserName": "jeremy",
                                            "DefaultDomainName": "SCOPE"})
        self.procs = kw.pop("procs", PROCS)
        self.healths = list(kw.pop("healths", [health()]))
        self.latency = kw.pop("latency", 0.05)
        self.head = kw.pop("head", SHA)
        self.files = kw.pop("files", {"logs/supervisor.log": SUP_LOG})
        self.exists_set = set(kw.pop("exists", ()))
        self.nina = kw.pop("nina", {"1888": "2.2.5.0", "1889": "2.2.5.0"})
        self.tcp = kw.pop("tcp", True)
        self.is_windows = kw.pop("is_windows", True)
        self.kill_result = kw.pop("kill_result", (True, "killed"))
        self.killed: list = []
        self.gets: list = []
        self.t = 0.0
        self.now_dt = kw.pop("now", NOW)
        self.on_sleep = kw.pop("on_sleep", None)
        assert not kw, kw

    def http_get(self, url, timeout):
        self.gets.append(url)
        if url.endswith("/api/health") and "8100" in url:
            h = self.healths[0] if len(self.healths) == 1 else self.healths.pop(0)
            if isinstance(h, Exception):
                raise h
            return 200, h, self.latency
        if "ts.net" in url:
            return 200, {}, 0.3
        for port, ver in self.nina.items():
            if f":{port}/" in url:
                return 200, {"Response": ver, "Success": True}, 0.02
        raise httpx.ConnectError("refused")

    def read_text(self, path):
        rel = Path(path).relative_to(self.tmp).as_posix()
        return self.files.get(rel)

    def exists(self, path):
        return Path(path).relative_to(self.tmp).as_posix() in self.exists_set

    def powershell(self, script):
        assert "__TASK__" not in script
        if self.task is None:
            return 0, json.dumps({"found": False, "error": self.task_err or
                                  "No MSFT_ScheduledTask objects found with property "
                                  "'TaskName' equal to 'PhotonScript'."}), ""
        return 0, json.dumps(self.task), ""

    def kill(self, pid):
        self.killed.append(pid)
        return self.kill_result

    def sleep(self, s):
        self.t += s
        self.now_dt += timedelta(seconds=s)
        if self.on_sleep:
            self.on_sleep(self)

    def env(self) -> ac.Env:
        return REAL_ENV(
            is_windows=self.is_windows, powershell=self.powershell,
            http_get=self.http_get, winlogon=lambda: dict(self.winlogon),
            processes=lambda: list(self.procs), git_head=lambda repo: self.head,
            tcp_ok=lambda h, p, t=2.0: self.tcp, read_text=self.read_text,
            exists=self.exists, kill=self.kill, now=lambda: self.now_dt,
            clock=lambda: self.t, sleep=self.sleep)


def opts(tmp: Path, **kw) -> ac.Options:
    base = dict(repo=REPO, data_dir=tmp,
                nina_urls=[("1", "http://localhost:1888/v2/api"),
                           ("2", "http://localhost:1889/v2/api")])
    base.update(kw)
    return ac.Options(**base)


def by_name(checks):
    return {c.name: c for c in checks}


# --- the whole thing -------------------------------------------------------------

def test_healthy_setup_all_pass(tmp_path):
    f = Fake(tmp_path)
    checks, h = ac.run_checks(f.env(), opts(tmp_path))
    bad = [c for c in checks if c.status not in (ac.PASS, ac.INFO)]
    assert bad == []
    text, rc = ac.verdict(checks)
    assert rc == 0 and text.startswith("AUTOSTART OK:")
    names = set(by_name(checks))
    for n in ("task.exists", "task.state", "task.logon", "task.trigger", "task.action",
              "task.settings", "task.last_run", "autologon", "api.health",
              "service.launcher", "service.session", "service.priority",
              "service.throttling", "service.commit", "loop.lag", "process.wrapper",
              "process.supervisor", "process.service", "process.launched_by",
              "process.api_pid", "log.stalls", "log.supervisor", "nina.1", "nina.2",
              "phd2", "tailscale"):
        assert n in names, n


def test_checks_are_read_only(tmp_path):
    f = Fake(tmp_path)
    ac.run_checks(f.env(), opts(tmp_path))
    assert f.killed == []
    assert all(u.endswith(("/api/health", "/version")) for u in f.gets)


def test_verdict_exit_codes():
    assert ac.verdict([ac.Check("a", ac.PASS, "")])[1] == 0
    t, rc = ac.verdict([ac.Check("a", ac.PASS, ""), ac.Check("b", ac.WARN, "")])
    assert rc == 0 and "warnings" in t
    t, rc = ac.verdict([ac.Check("a", ac.FAIL, ""), ac.Check("b", ac.SKIP, "")])
    assert rc == 1 and t.startswith("AUTOSTART FAIL: 1 fail")


def test_armer_active_states_match_the_armer():
    from photonscript.scheduler.armer import ACTIVE_STATES
    assert tuple(ACTIVE_STATES) == ac.ARMER_ACTIVE


# --- scheduled task ------------------------------------------------------------------

def test_task_missing_fails(tmp_path):
    c = ac.task_checks(Fake(tmp_path, task=None).env(), opts(tmp_path))
    assert [(x.name, x.status) for x in c] == [("task.exists", ac.FAIL)]
    assert "install-autostart.ps1" in c[0].detail


def test_task_unreadable_warns(tmp_path):
    f = Fake(tmp_path, task=None, task_err="Access is denied.")
    c = ac.task_checks(f.env(), opts(tmp_path))
    assert c[0].status == ac.WARN and "elevated" in c[0].detail


def test_task_s4u_layout_warns(tmp_path):
    t = task_info(logon_type="S4U",
                  triggers=[{"kind": "MSFT_TaskBootTrigger", "enabled": True,
                             "delay": "PT1M", "user": ""}],
                  actions=[{"execute": "powershell.exe",
                            "arguments": ARGS.replace("task-Interactive", "task-S4U"),
                            "workdir": ""}])
    c = by_name(ac.task_checks(Fake(tmp_path, task=t).env(), opts(tmp_path)))
    assert c["task.logon"].status == ac.WARN and "PS-55" in c["task.logon"].detail
    assert c["task.trigger"].status == ac.WARN
    assert c["task.action"].status == ac.PASS


@pytest.mark.parametrize("kw,name,status", [
    ({"enabled": False, "state": "Disabled"}, "task.state", ac.FAIL),
    ({"state": "Ready"}, "task.state", ac.WARN),
    ({"user": "SCOPE\\admin"}, "task.logon", ac.FAIL),
    ({"triggers": []}, "task.trigger", ac.FAIL),
    ({"triggers": [{"kind": "MSFT_TaskLogonTrigger", "enabled": False,
                    "delay": "PT30S", "user": "SCOPE\\jeremy"}]}, "task.trigger", ac.FAIL),
    ({"triggers": [{"kind": "MSFT_TaskLogonTrigger", "enabled": True,
                    "delay": "PT30S", "user": "SCOPE\\bob"}]}, "task.trigger", ac.FAIL),
    ({"actions": [{"execute": "powershell.exe", "workdir": "",
                   "arguments": ARGS.replace("C:\\astro\\PhotonScript",
                                             "C:\\dev\\PhotonScript")}]},
     "task.action", ac.FAIL),
    ({"actions": [{"execute": "powershell.exe", "workdir": "",
                   "arguments": ARGS.replace(" -Launcher task-Interactive", "")}]},
     "task.action", ac.WARN),
    ({"actions": []}, "task.action", ac.FAIL),
    ({"time_limit": "PT72H"}, "task.settings", ac.WARN),
    ({"priority": 7}, "task.settings", ac.WARN),
    ({"last_result": 0}, "task.last_run", ac.WARN),
    ({"last_result": 0x41303}, "task.last_run", ac.WARN),
    ({"last_result": 1}, "task.last_run", ac.FAIL),
    ({"last_result": 0xC000013A}, "task.last_run", ac.FAIL),
    ({"last_result": None}, "task.last_run", ac.WARN),
])
def test_task_problems(tmp_path, kw, name, status):
    c = by_name(ac.task_checks(Fake(tmp_path, task=task_info(**kw)).env(), opts(tmp_path)))
    assert c[name].status == status, c[name]


def test_task_json_quirks_from_windows_powershell(tmp_path):
    good = task_info()
    t = task_info(triggers={"value": good["triggers"], "Count": 1},
                  actions=good["actions"][0])
    c = ac.task_checks(Fake(tmp_path, task=t).env(), opts(tmp_path))
    assert all(x.status == ac.PASS for x in c), c


def test_task_last_result_is_decoded(tmp_path):
    c = by_name(ac.task_checks(Fake(tmp_path, task=task_info(last_result=0xC000013A)).env(),
                               opts(tmp_path)))
    assert "0xC000013A" in c["task.last_run"].detail
    assert "session closed" in c["task.last_run"].detail


def test_task_skipped_off_windows(tmp_path):
    c = ac.task_checks(Fake(tmp_path, is_windows=False).env(), opts(tmp_path))
    assert [x.status for x in c] == [ac.SKIP]


def test_task_name_is_quoted_in_the_script(tmp_path):
    seen = []
    env = Fake(tmp_path).env()
    env.powershell = lambda s: (seen.append(s), (0, json.dumps(task_info()), ""))[1]
    ac.task_checks(env, opts(tmp_path, task_name="Photon'Script"))
    assert "$name = 'Photon''Script'" in seen[0]


# --- auto-logon ------------------------------------------------------------------------

@pytest.mark.parametrize("wl,status", [
    ({"AutoAdminLogon": "1", "DefaultUserName": "jeremy"}, ac.PASS),
    ({"AutoAdminLogon": "1", "DefaultUserName": "JEREMY", "DefaultDomainName": "SCOPE"},
     ac.PASS),
    ({"AutoAdminLogon": "0", "DefaultUserName": "jeremy"}, ac.FAIL),
    ({"DefaultUserName": "jeremy"}, ac.FAIL),
    ({"AutoAdminLogon": "1", "DefaultUserName": "admin"}, ac.FAIL),
])
def test_autologon(tmp_path, wl, status):
    c = ac.autologon_checks(Fake(tmp_path, winlogon=wl).env(), opts(tmp_path))
    assert c[0].status == status


def test_autologon_count_warns(tmp_path):
    wl = {"AutoAdminLogon": "1", "DefaultUserName": "jeremy", "AutoLogonCount": 2}
    c = by_name(ac.autologon_checks(Fake(tmp_path, winlogon=wl).env(), opts(tmp_path)))
    assert c["autologon"].status == ac.PASS
    assert c["autologon.count"].status == ac.WARN


def test_winlogon_reader_never_touches_the_password(monkeypatch):
    queried = []

    class Key:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def query(key, name):
        queried.append(name)
        if name == "DefaultPassword":
            raise AssertionError("password read")
        if name in ("AutoAdminLogon", "DefaultUserName"):
            return ("1" if name == "AutoAdminLogon" else "jeremy", 1)
        raise FileNotFoundError(name)

    fake = types.SimpleNamespace(HKEY_LOCAL_MACHINE=object(),
                                 OpenKey=lambda root, path: Key(),
                                 QueryValueEx=query,
                                 EnumValue=lambda *a: (_ for _ in ()).throw(
                                     AssertionError("enumerated values")))
    monkeypatch.setitem(sys.modules, "winreg", fake)
    out = ac._read_winlogon()
    assert out == {"AutoAdminLogon": "1", "DefaultUserName": "jeremy"}
    assert "DefaultPassword" not in queried
    assert "DefaultPassword" not in ac.WINLOGON_VALUES
    assert "DefaultPassword" not in ac.TASK_QUERY_PS


# --- processes ---------------------------------------------------------------------------

@pytest.mark.parametrize("cmd,role", [
    (["python.exe", "-m", "photonscript.cli", "start", "--mode", "full"], "service"),
    ([r"C:\astro\venv\Scripts\photonscript.exe", "start"], "service"),
    ([r"C:\astro\venv\Scripts\photonscript.exe", "supervise", "--mode", "full"],
     "supervisor"),
    (["/tmp/venv/bin/photonscript", "supervise"], "supervisor"),
    (["powershell", "-File", r"C:\astro\PhotonScript\deploy\run-photonscript.ps1"],
     "wrapper"),
    ([r"C:\astro\venv\Scripts\photonscript.exe", "autostart-check"], None),
    ([r"C:\astro\venv\Scripts\photonscript.exe", "status"], None),
    (["python.exe", "-m", "http.server", "start"], None),
    (["notepad.exe", "photonscript-start.txt"], None),
    ([], None),
])
def test_classify(cmd, role):
    assert ac.classify(cmd) == role


def test_launcher_chains_count_as_one_instance():
    g = ac.group_processes(PROCS)
    assert g["wrapper"] == [[100]]
    assert g["supervisor"] == [[200, 201]]
    assert g["service"] == [[300, 3684]]


def test_process_checks_pass(tmp_path):
    c = by_name(ac.process_checks(Fake(tmp_path).env(), opts(tmp_path), 3684))
    assert all(x.status == ac.PASS for x in c.values()), c
    assert "pid 300>3684" in c["process.service"].detail


def test_second_copy_and_console_wrapper_fail(tmp_path):
    console_copy = [
        {"pid": 900, "ppid": 4, "name": "powershell.exe", "cmdline": [
            "powershell", "-ExecutionPolicy", "Bypass", "-File",
            r"C:\astro\PhotonScript\deploy\run-photonscript.ps1"]},
        {"pid": 901, "ppid": 900, "name": "python.exe", "cmdline": [
            "python.exe", "-m", "photonscript.cli", "start", "--mode", "full"]},
    ]
    f = Fake(tmp_path, procs=PROCS + console_copy)
    c = by_name(ac.process_checks(f.env(), opts(tmp_path), 901))
    assert c["process.wrapper"].status == ac.FAIL
    assert c["process.service"].status == ac.FAIL
    assert "2 photonscript start instances" in c["process.service"].detail
    assert c["process.launched_by"].status == ac.FAIL
    assert "900" in c["process.launched_by"].detail
    assert c["process.api_pid"].status == ac.PASS     # 901 is a service pid


def test_nothing_running_fails(tmp_path):
    f = Fake(tmp_path, procs=[p for p in PROCS if p["pid"] in (500, 600)])
    c = by_name(ac.process_checks(f.env(), opts(tmp_path), None))
    for role in ("wrapper", "supervisor", "service"):
        assert c[f"process.{role}"].status == ac.FAIL
    assert "process.api_pid" not in c


def test_hold_and_stop_markers(tmp_path):
    f = Fake(tmp_path, exists={"HOLD", "STOP"})
    c = by_name(ac.process_checks(f.env(), opts(tmp_path), 3684))
    assert c["hold"].status == ac.FAIL and "Start-ScheduledTask" in c["hold"].detail
    assert c["stop_sentinel"].status == ac.WARN


def test_api_pid_not_the_service(tmp_path):
    c = by_name(ac.process_checks(Fake(tmp_path).env(), opts(tmp_path), 4242))
    assert c["process.api_pid"].status == ac.FAIL


# --- /api/health -----------------------------------------------------------------------

@pytest.mark.parametrize("h,latency,name,status", [
    (health(), 0.05, "api.health", ac.PASS),
    (health(), 1.4, "api.health", ac.WARN),
    (health(), 6.0, "api.health", ac.FAIL),
    (health(launcher="console"), 0.05, "service.launcher", ac.FAIL),
    (health(launcher="unknown"), 0.05, "service.launcher", ac.FAIL),
    (health(launcher="task-S4U"), 0.05, "service.launcher", ac.WARN),
    (health(session_id=0), 0.05, "service.session", ac.FAIL),
    (health(priority_class="below_normal"), 0.05, "service.priority", ac.FAIL),
    (health(priority_class="high"), 0.05, "service.priority", ac.WARN),
    (health(power_throttling="on"), 0.05, "service.throttling", ac.FAIL),
    (health(power_throttling="system-managed"), 0.05, "service.throttling", ac.WARN),
    (health(elevated=True), 0.05, "service.elevated", ac.WARN),
    (health(user="SYSTEM"), 0.05, "service.user", ac.WARN),
    (health(commit="0" * 40), 0.05, "service.commit", ac.WARN),
    (health(loop=None), 0.05, "loop.lag", ac.WARN),
    (health(loop={"lag_ms": 1500, "max_lag_ms_5min": 1500, "stalls_5min": 0,
                  "heartbeat_age_s": 0.1}), 0.05, "loop.lag", ac.FAIL),
    (health(loop={"lag_ms": 10, "max_lag_ms_5min": 9000, "stalls_5min": 1,
                  "heartbeat_age_s": 0.1}), 0.05, "loop.lag", ac.FAIL),
    (health(loop={"lag_ms": 10, "max_lag_ms_5min": 100, "stalls_5min": 0,
                  "heartbeat_age_s": 8.0}), 0.05, "loop.lag", ac.FAIL),
    (health(loop={"lag_ms": 300, "max_lag_ms_5min": 300, "stalls_5min": 0,
                  "heartbeat_age_s": 0.1}), 0.05, "loop.lag", ac.WARN),
])
def test_health_checks(tmp_path, h, latency, name, status):
    c = by_name(ac.health_checks(Fake(tmp_path).env(), opts(tmp_path), h, latency, ""))
    assert c[name].status == status, c[name]


def test_startup_lag_spike_is_explained(tmp_path):
    h = health(uptime_s=120.0, loop={"lag_ms": 20, "max_lag_ms_5min": 2400,
                                     "stalls_5min": 0, "heartbeat_age_s": 0.1})
    c = by_name(ac.health_checks(Fake(tmp_path).env(), opts(tmp_path), h, 0.05, ""))
    assert c["loop.lag"].status == ac.WARN and "startup spike" in c["loop.lag"].detail


def test_git_failure_warns(tmp_path):
    env = Fake(tmp_path).env()
    env.git_head = lambda repo: (_ for _ in ()).throw(RuntimeError("not a git repo"))
    c = by_name(ac.health_checks(env, opts(tmp_path), health(), 0.05, ""))
    assert c["service.commit"].status == ac.WARN


def test_windows_fields_skipped_off_windows(tmp_path):
    h = health()
    for k in ("session_id", "priority_class", "power_throttling"):
        h["process"].pop(k)
    c = by_name(ac.health_checks(Fake(tmp_path, is_windows=False).env(),
                                 opts(tmp_path), h, 0.05, ""))
    assert c["service.session"].status == ac.SKIP
    assert c["service.priority"].status == ac.SKIP


@pytest.mark.parametrize("exc,words", [
    (httpx.ConnectError("refused"), "connection refused"),
    (httpx.ReadTimeout("slow"), "no answer within 10 s"),
    (ValueError("boom"), "ValueError: boom"),
])
def test_health_unreachable(tmp_path, exc, words):
    f = Fake(tmp_path, healths=[exc])
    checks, h = ac.run_checks(f.env(), opts(tmp_path))
    c = by_name(checks)
    assert h is None
    assert c["api.health"].status == ac.FAIL and words in c["api.health"].detail
    assert "service.launcher" not in c
    assert ac.verdict(checks)[1] == 1


def test_health_http_error(tmp_path):
    env = Fake(tmp_path).env()
    env.http_get = lambda url, t: (500, None, 0.1)
    h, lat, err = ac.get_health(env, opts(tmp_path))
    assert h is None and "HTTP 500" in err


# --- logs ----------------------------------------------------------------------------------

def stall_line(t: datetime, secs: float, kind: str = "new") -> str:
    return (f"{t:%Y-%m-%d %H:%M:%S} Event loop STALLED for {secs:.1f} s ({kind}), "
            f"loop thread stack:\n  File \"x.py\", line 1, in f\n")


def test_stall_since_this_start_fails(tmp_path):
    t = NOW - timedelta(minutes=5)
    f = Fake(tmp_path, files={"logs/supervisor.log": SUP_LOG,
                              "logs/stalls.log": stall_line(t, 5.2) +
                              stall_line(t + timedelta(seconds=60), 65.0, "still")})
    c = by_name(ac.log_checks(f.env(), opts(tmp_path), health()))
    assert c["log.stalls"].status == ac.FAIL
    assert "1 stall(s) since this start" in c["log.stalls"].detail
    assert "longest report 65 s" in c["log.stalls"].detail


def test_old_stall_warns_and_very_old_is_ignored(tmp_path):
    f = Fake(tmp_path, files={"logs/supervisor.log": SUP_LOG, "logs/stalls.log":
                              stall_line(NOW - timedelta(hours=3), 6.0) +
                              stall_line(NOW - timedelta(days=3), 6.0)})
    c = by_name(ac.log_checks(f.env(), opts(tmp_path), health()))
    assert c["log.stalls"].status == ac.WARN and c["log.stalls"].detail.startswith("1 ")
    c = by_name(ac.log_checks(f.env(), opts(tmp_path, hours=2), health()))
    assert c["log.stalls"].status == ac.PASS


def test_empty_stalls_log_passes(tmp_path):
    f = Fake(tmp_path, files={"logs/stalls.log": "", "logs/supervisor.log": SUP_LOG})
    c = by_name(ac.log_checks(f.env(), opts(tmp_path), health()))
    assert c["log.stalls"].status == ac.PASS
    assert c["log.supervisor"].status == ac.PASS
    assert "1 start(s), 0 crash restart(s)" in c["log.supervisor"].detail


def test_supervisor_crash_warns_and_give_up_fails(tmp_path):
    crash = ("2026-09-27 12:50:00 [supervisor] WARNING PhotonScript exited with code 1 "
             "after 9 min -> crash")
    f = Fake(tmp_path, files={"logs/supervisor.log": SUP_LOG + "\n" + crash})
    c = by_name(ac.log_checks(f.env(), opts(tmp_path), health()))
    assert c["log.supervisor"].status == ac.WARN
    assert "09-27 12:50" in c["log.supervisor"].detail
    gave = ("2026-09-27 12:55:00 [supervisor] ERROR   PhotonScript on SCOPE crashed 5 "
            "times in 15 min (last exit 1). Giving up; it is DOWN until someone starts it.")
    f = Fake(tmp_path, files={"logs/supervisor.log": SUP_LOG + "\n" + crash + "\n" + gave})
    c = by_name(ac.log_checks(f.env(), opts(tmp_path), health()))
    assert c["log.supervisor"].status == ac.FAIL


def test_planned_restarts_are_not_crashes(tmp_path):
    lines = SUP_LOG + "\n" + "\n".join([
        "2026-09-27 12:50:00 [supervisor] INFO    PhotonScript exited with code 0 after 9 min -> restart",
        "2026-09-27 12:51:00 [supervisor] INFO    PhotonScript exited with code 42 after 1 min -> update",
    ])
    c = by_name(ac.log_checks(Fake(tmp_path, files={"logs/supervisor.log": lines}).env(),
                              opts(tmp_path), health()))
    assert c["log.supervisor"].status == ac.PASS


def test_started_local_round_trip():
    assert ac.started_local(health()) == NOW - timedelta(minutes=20)
    assert ac.started_local({}) is None
    assert ac.started_local(None) is None


# --- neighbours --------------------------------------------------------------------------

def test_nina_down_warns_with_hint(tmp_path):
    f = Fake(tmp_path, nina={"1888": "2.2.5.0"}, tcp=False)
    c = by_name(ac.neighbour_checks(f.env(), opts(tmp_path)))
    assert c["nina.1"].status == ac.PASS and "2.2.5.0" in c["nina.1"].detail
    assert c["nina.2"].status == ac.WARN and "NINA #2" in c["nina.2"].detail
    assert c["phd2"].status == ac.WARN


def test_tailscale_optional(tmp_path):
    c = by_name(ac.neighbour_checks(Fake(tmp_path).env(), opts(tmp_path, tailscale_url="")))
    assert "tailscale" not in c


# --- restart watch --------------------------------------------------------------------------

def notif(t_local: datetime, sent: bool, reason: str) -> str:
    return json.dumps({"ts": t_local.astimezone(timezone.utc).isoformat(),
                       "title": "PhotonScript crashed", "message": "x", "priority": 1,
                       "sent": sent, "reason": reason})


def _restart_sequence(tmp_path, sent=True, reason="sent", down_polls=2, **kw):
    down = [httpx.ConnectError("refused")] * down_polls
    new = health(pid=5000)
    crash = (f"{NOW + timedelta(seconds=3):%Y-%m-%d %H:%M:%S} [supervisor] WARNING "
             "PhotonScript exited with code 1 after 20 min -> crash")
    files = {"logs/supervisor.log": SUP_LOG + "\n" + crash,
             "notifications.jsonl": notif(NOW + timedelta(seconds=3), sent, reason)}
    return Fake(tmp_path, healths=[health(), health()] + down + [new], files=files, **kw)


def test_watch_restart_operator_kill(tmp_path):
    f = _restart_sequence(tmp_path)
    said = []
    c = by_name(ac.watch_restart(f.env(), opts(tmp_path), say=said.append))
    assert f.killed == []
    assert any("Stop-Process -Id 3684 -Force" in s for s in said)
    assert c["watch.recover"].status == ac.PASS
    assert "new pid 5000" in c["watch.recover"].detail
    assert c["watch.supervisor"].status == ac.PASS
    assert c["watch.pushover"].status == ac.PASS


def test_watch_restart_with_kill(tmp_path):
    f = _restart_sequence(tmp_path)
    c = by_name(ac.watch_restart(f.env(), opts(tmp_path), kill=True, say=lambda s: None))
    assert f.killed == [3684]
    assert c["watch.recover"].status == ac.PASS


@pytest.mark.parametrize("armer", ["ARMED", "RUNNING", "PAUSED_UNSAFE"])
def test_kill_refused_while_armed(tmp_path, armer):
    f = Fake(tmp_path, healths=[health(armer=armer)])
    c = ac.watch_restart(f.env(), opts(tmp_path), kill=True, say=lambda s: None)
    assert f.killed == []
    assert c[0].name == "watch.kill" and c[0].status == ac.FAIL


def test_kill_failure_reported(tmp_path):
    f = Fake(tmp_path, kill_result=(False, "access denied"))
    c = ac.watch_restart(f.env(), opts(tmp_path), kill=True, say=lambda s: None)
    assert c[0].status == ac.FAIL and "access denied" in c[0].detail


def test_watch_never_killed(tmp_path):
    f = Fake(tmp_path)          # always the same pid
    c = ac.watch_restart(f.env(), opts(tmp_path), wait_for_kill_s=10, poll_s=2,
                         say=lambda s: None)
    assert c[0].name == "watch.down" and c[0].status == ac.FAIL


def test_watch_never_recovers(tmp_path):
    f = Fake(tmp_path, healths=[health(), health()] + [httpx.ConnectError("x")] * 200)
    c = ac.watch_restart(f.env(), opts(tmp_path), max_recover_s=20, poll_s=2,
                         say=lambda s: None)
    assert c[-1].name == "watch.recover" and c[-1].status == ac.FAIL


def test_slow_recovery_warns(tmp_path):
    f = _restart_sequence(tmp_path, down_polls=40)
    c = by_name(ac.watch_restart(f.env(), opts(tmp_path), poll_s=2, say=lambda s: None))
    assert c["watch.recover"].status == ac.WARN


def test_pushover_suppressed_in_daytime_is_explained(tmp_path):
    f = _restart_sequence(tmp_path, sent=False, reason="quiet-daytime")
    c = by_name(ac.watch_restart(f.env(), opts(tmp_path), say=lambda s: None))
    assert c["watch.pushover"].status == ac.WARN
    assert "4 h" in c["watch.pushover"].detail


def test_old_crash_lines_do_not_count(tmp_path):
    f = _restart_sequence(tmp_path)
    old = NOW - timedelta(hours=2)
    f.files = {"logs/supervisor.log":
               f"{old:%Y-%m-%d %H:%M:%S} [supervisor] WARNING PhotonScript exited "
               "with code 1 after 1 min -> crash",
               "notifications.jsonl": notif(old, True, "sent")}
    c = by_name(ac.watch_restart(f.env(), opts(tmp_path), say=lambda s: None))
    assert c["watch.supervisor"].status == ac.WARN
    assert c["watch.pushover"].status == ac.WARN


def test_watch_needs_a_running_service(tmp_path):
    f = Fake(tmp_path, healths=[httpx.ConnectError("x")])
    c = ac.watch_restart(f.env(), opts(tmp_path), say=lambda s: None)
    assert c[0].name == "watch.baseline" and c[0].status == ac.FAIL


# --- CLI ---------------------------------------------------------------------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    from photonscript import cli
    from photonscript.shared.config import PhotonScriptConfig
    monkeypatch.setattr(cli, "_config_for_repo",
                        lambda repo: PhotonScriptConfig(data_dir=tmp_path))
    holder = {}

    def make_env(**kw):
        return holder["fake"].env()
    monkeypatch.setattr(ac, "Env", make_env)
    return cli, holder, tmp_path


def test_cli_ok(cli_env):
    cli, holder, tmp = cli_env
    holder["fake"] = Fake(tmp)
    r = CliRunner().invoke(cli.app, ["autostart-check", "--repo", str(REPO)])
    assert r.exit_code == 0, r.output
    assert "PASS  task.logon" in r.output
    assert "AUTOSTART OK" in r.output


def test_cli_fail_exit_1(cli_env):
    cli, holder, tmp = cli_env
    holder["fake"] = Fake(tmp, healths=[health(session_id=0)])
    r = CliRunner().invoke(cli.app, ["autostart-check", "--repo", str(REPO)])
    assert r.exit_code == 1
    assert "FAIL  service.session" in r.output


def test_cli_json(cli_env):
    cli, holder, tmp = cli_env
    holder["fake"] = Fake(tmp)
    r = CliRunner().invoke(cli.app, ["autostart-check", "--repo", str(REPO), "--json", "--tailscale-url", ""])
    assert r.exit_code == 0, r.output
    d = json.loads(r.output)
    assert d["exit"] == 0 and any(c["name"] == "task.action" for c in d["checks"])
    assert not any(c["name"] == "tailscale" for c in d["checks"])


def test_cli_kill_requires_watch(cli_env):
    cli, holder, tmp = cli_env
    holder["fake"] = Fake(tmp)
    r = CliRunner().invoke(cli.app, ["autostart-check", "--repo", str(REPO), "--kill"])
    assert r.exit_code == 2
    assert holder["fake"].killed == []


def test_cli_watch_restart_with_kill(cli_env):
    cli, holder, tmp = cli_env
    holder["fake"] = _restart_sequence(tmp)
    # the main checks read /api/health once before the watch starts
    holder["fake"].healths.insert(0, health())
    r = CliRunner().invoke(cli.app, ["autostart-check", "--repo", str(REPO), "--watch-restart", "--kill"])
    assert holder["fake"].killed == [3684]
    assert "PASS  watch.recover" in r.output, r.output
    assert r.exit_code == 0


# --- the PowerShell query itself (needs PowerShell) ----------------------------------------

STUBS = r"""
function Get-ScheduledTask { param($TaskName)
  if ($TaskName -ne 'PhotonScript') { throw "No MSFT_ScheduledTask objects found with property 'TaskName' equal to '$TaskName'." }
  [pscustomobject]@{
    State = 'Running'
    Settings = [pscustomobject]@{ Enabled = $true; ExecutionTimeLimit = 'PT0S'; Priority = 4; MultipleInstances = 'IgnoreNew' }
    Principal = [pscustomobject]@{ LogonType = 'Interactive'; UserId = 'SCOPE\jeremy'; RunLevel = 'Limited' }
    Triggers = @([pscustomobject]@{ CimClass = [pscustomobject]@{ CimClassName = 'MSFT_TaskLogonTrigger' }; Enabled = $true; Delay = 'PT30S'; UserId = 'SCOPE\jeremy' })
    Actions = @([pscustomobject]@{ Execute = 'C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'; Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\astro\PhotonScript\deploy\run-photonscript.ps1" -Launcher task-Interactive'; WorkingDirectory = 'C:\astro\PhotonScript' })
  }
}
function Get-ScheduledTaskInfo { param($TaskName)
  [pscustomobject]@{ LastRunTime = [datetime]'2026-09-27T11:00:00'; LastTaskResult = 267009; NextRunTime = $null }
}
"""


def _pwsh():
    for name in (os.environ.get("PS_TEST_PWSH", ""), "pwsh", "powershell"):
        if name and shutil.which(name):
            return shutil.which(name)
    return None


@pytest.mark.skipif(_pwsh() is None, reason="no PowerShell on this machine")
@pytest.mark.parametrize("task,found", [("PhotonScript", True), ("Nope", False)])
def test_task_query_script_runs_in_powershell(tmp_path, task, found):
    script = STUBS + ac.TASK_QUERY_PS.replace("__TASK__", task)
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    p = subprocess.run([_pwsh(), "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    info = json.loads(p.stdout.strip())
    assert info["found"] is found
    if not found:
        env = Fake(tmp_path).env()
        env.powershell = lambda s: (0, p.stdout, p.stderr)
        assert ac.task_checks(env, opts(tmp_path))[0].status == ac.FAIL
        return
    assert info["logon_type"] == "Interactive" and info["last_result"] == 267009
    assert isinstance(info["triggers"], list) and isinstance(info["actions"], list)
    assert info["next_run"] is None and info["last_run"].startswith("2026-09-27T11:00:00")
    env = Fake(tmp_path).env()
    env.powershell = lambda s: (0, p.stdout, p.stderr)
    c = ac.task_checks(env, opts(tmp_path))
    assert all(x.status == ac.PASS for x in c), c
