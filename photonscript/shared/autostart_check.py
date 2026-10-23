"""`photonscript autostart-check` (PS-34a): is the autostart setup healthy?

Run on the scope PC after installing the "PhotonScript" scheduled task and
after every reboot. Prints one PASS / WARN / FAIL line per check (SKIP when a
check does not apply, INFO for context) and a verdict; exits 1 when anything
FAILs.

Read-only by default: it reads the scheduled task (PowerShell
Get-ScheduledTask), a few Winlogon registry values (never the password), the
process list, the PID / HOLD markers and the logs under data_dir, and makes
GET requests to /api/health, NINA and tailscale serve. The only thing that
changes state is ``--kill`` (hard-kill the service so the supervisor has to
bring it back), which is off unless passed and refuses while the armer is
ARMED, RUNNING or PAUSED_UNSAFE.

Every side effect goes through ``Env`` so the checks are unit tested with
fakes (tests/test_autostart_check.py).
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

PASS, WARN, FAIL, SKIP, INFO = "pass", "warn", "fail", "skip", "info"

# Same as photonscript.scheduler.armer.LIVE_STATES (a test keeps them equal);
# copied so this module does not import the scheduler. PS-152: includes the
# PS-136 WATCHING state (never kill the service under a watched night).
ARMER_ACTIVE = ("ARMED", "RUNNING", "PAUSED_UNSAFE", "PAUSED_OPERATOR",
                "WATCHING")

DEFAULT_TAILSCALE_URL = "https://teles-feb25.lobster-bleak.ts.net"
WINLOGON_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon"
# The only Winlogon values ever read. DefaultPassword is deliberately absent.
WINLOGON_VALUES = ("AutoAdminLogon", "DefaultUserName", "DefaultDomainName",
                   "AutoLogonCount")

# Task Scheduler result codes (Get-ScheduledTaskInfo LastTaskResult).
TASK_RESULTS = {
    0x0: "last run finished with exit 0, so the wrapper is not running now",
    0x1: "last run exited with code 1",
    0x3: "last run exited 3 (another PhotonScript was already running)",
    0x41300: "ready",
    0x41301: "running",
    0x41302: "disabled",
    0x41303: "has not run yet",
    0x41306: "terminated by the user (End task / Stop-ScheduledTask)",
    0x8004131F: "an instance was already running (IgnoreNew)",
    0x800710E0: "the operator or administrator refused the request",
    0xC000013A: "ended when the session closed (sign-out or Ctrl-C)",
}

# Get-ScheduledTask + Get-ScheduledTaskInfo as one compact JSON object.
# Enums are cast to strings and dates to ISO text so Windows PowerShell 5.1
# and PowerShell 7 give the same JSON. __TASK__ is replaced (quotes escaped).
TASK_QUERY_PS = r"""
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 can serialize arrays as {"value": [...], "Count": n}
# because of the System.Array type data; drop it for this session.
Remove-TypeData System.Array -ErrorAction SilentlyContinue
$name = '__TASK__'
try { $t = Get-ScheduledTask -TaskName $name }
catch { [pscustomobject]@{ found = $false; error = [string]$_.Exception.Message } | ConvertTo-Json -Compress; exit 0 }
$i = $null
try { $i = Get-ScheduledTaskInfo -TaskName $name } catch { }
function IsoDate($d) { if ($d) { return $d.ToString('o') } else { return $null } }
$trig = @(foreach ($x in $t.Triggers) { [pscustomobject]@{ kind = [string]$x.CimClass.CimClassName; enabled = [bool]$x.Enabled; delay = [string]$x.Delay; user = [string]$x.UserId } })
$act = @(foreach ($a in $t.Actions) { [pscustomobject]@{ execute = [string]$a.Execute; arguments = [string]$a.Arguments; workdir = [string]$a.WorkingDirectory } })
$lastRun = $null; $lastResult = $null; $nextRun = $null
if ($i) { $lastRun = IsoDate $i.LastRunTime; $lastResult = [int64]$i.LastTaskResult; $nextRun = IsoDate $i.NextRunTime }
[pscustomobject]@{
  found = $true
  state = [string]$t.State
  enabled = [bool]$t.Settings.Enabled
  logon_type = [string]$t.Principal.LogonType
  user = [string]$t.Principal.UserId
  run_level = [string]$t.Principal.RunLevel
  triggers = $trig
  actions = $act
  time_limit = [string]$t.Settings.ExecutionTimeLimit
  priority = $t.Settings.Priority
  multiple_instances = [string]$t.Settings.MultipleInstances
  last_run = $lastRun
  last_result = $lastResult
  next_run = $nextRun
} | ConvertTo-Json -Depth 4 -Compress
"""


@dataclass
class Check:
    name: str
    status: str
    detail: str

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class Options:
    repo: Path
    data_dir: Path
    url: str = "http://localhost:8100"
    task_name: str = "PhotonScript"
    user: str = "jeremy"
    hours: float = 24.0
    nina_urls: list = field(default_factory=list)   # [(label, base_url)]
    tailscale_url: str = DEFAULT_TAILSCALE_URL
    phd2: tuple = ("localhost", 4400)
    health_timeout: float = 10.0


# --- side effects (real implementations; tests pass fakes) ----------------------

def _run_powershell(script: str, timeout: float = 60.0) -> tuple[int, str, str]:
    exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32",
                       "WindowsPowerShell", "v1.0", "powershell.exe")
    if not os.path.exists(exe):
        exe = "powershell"
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    p = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                        "Bypass", "-EncodedCommand", enc],
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _http_get(url: str, timeout: float) -> tuple[int, object, float]:
    import httpx
    t0 = time.monotonic()
    r = httpx.get(url, timeout=timeout)
    dt = time.monotonic() - t0
    try:
        body = r.json()
    except ValueError:
        body = None
    return r.status_code, body, dt


def _read_winlogon() -> dict:
    import winreg  # Windows only
    out: dict = {}
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, WINLOGON_KEY) as k:
        for name in WINLOGON_VALUES:
            try:
                out[name] = winreg.QueryValueEx(k, name)[0]
            except FileNotFoundError:
                pass
    return out


def _processes() -> list[dict]:
    import psutil
    out = []
    for p in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
        try:
            info = p.info
            out.append({"pid": info["pid"], "ppid": info.get("ppid"),
                        "name": info.get("name") or "",
                        "cmdline": list(info.get("cmdline") or [])})
        except Exception:  # noqa: BLE001 - vanished or access denied
            continue
    return out


def _git_head(repo: Path) -> str:
    p = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                       capture_output=True, text=True, timeout=15)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip() or f"exit {p.returncode}")
    return p.stdout.strip()


def _tcp_ok(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _read_tail(path: Path, max_bytes: int = 2_000_000) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            return fh.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return None


def _force_kill(pid: int) -> tuple[bool, str]:
    from photonscript.shared import process_control
    return process_control.force_kill(pid)


@dataclass
class Env:
    is_windows: bool = os.name == "nt"
    powershell: Callable[[str], tuple] = _run_powershell
    http_get: Callable[[str, float], tuple] = _http_get
    winlogon: Callable[[], dict] = _read_winlogon
    processes: Callable[[], list] = _processes
    git_head: Callable[[Path], str] = _git_head
    tcp_ok: Callable[..., bool] = _tcp_ok
    read_text: Callable[[Path], Optional[str]] = _read_tail
    exists: Callable[[Path], bool] = os.path.exists
    kill: Callable[[int], tuple] = _force_kill
    now: Callable[[], datetime] = datetime.now          # local, naive (log clock)
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


def _as_list(x) -> list:
    """JSON array from PowerShell: tolerate a bare object (one element) and
    5.1's {"value": [...], "Count": n} wrapping."""
    if x is None:
        return []
    if isinstance(x, dict):
        return list(x["value"]) if "value" in x else [x]
    return list(x)


def _short_user(u: str) -> str:
    return re.split(r"[\\/]", u or "")[-1].lower()


# --- scheduled task --------------------------------------------------------------

def task_checks(env: Env, opts: Options) -> list[Check]:
    if not env.is_windows:
        return [Check("task", SKIP, "not Windows: no Task Scheduler here")]
    script = TASK_QUERY_PS.replace("__TASK__", opts.task_name.replace("'", "''"))
    try:
        rc, out, err = env.powershell(script)
        info = json.loads((out or "").strip() or "{}")
    except Exception as e:  # noqa: BLE001
        return [Check("task.exists", WARN,
                      f"could not query the task: {type(e).__name__}: {e}")]
    if not info.get("found"):
        msg = info.get("error") or (err or "").strip() or f"powershell exit {rc}"
        if "No MSFT_ScheduledTask objects found" in msg:
            return [Check("task.exists", FAIL,
                          f"no scheduled task '{opts.task_name}': run "
                          "deploy\\install-autostart.ps1 from an elevated PowerShell")]
        return [Check("task.exists", WARN,
                      f"could not read task '{opts.task_name}': {msg[:200]} "
                      "(try again from an elevated PowerShell)")]

    out_checks = [Check("task.exists", PASS, f"'{opts.task_name}' is registered")]

    state = info.get("state") or "?"
    if not info.get("enabled", True) or state == "Disabled":
        out_checks.append(Check("task.state", FAIL,
                                "task is DISABLED: Enable-ScheduledTask PhotonScript"))
    elif state == "Running":
        out_checks.append(Check("task.state", PASS, "enabled, Running"))
    else:
        out_checks.append(Check("task.state", WARN,
                                f"enabled but {state}, so it is not running now: "
                                "Start-ScheduledTask PhotonScript"))

    lt = info.get("logon_type") or "?"
    who = info.get("user") or "?"
    if lt == "Interactive" and _short_user(who) == opts.user.lower():
        out_checks.append(Check("task.logon", PASS, f"Interactive as {who}"))
    elif lt in ("S4U", "Password"):
        out_checks.append(Check("task.logon", WARN,
                                f"{lt} as {who}: session 0, the environment that "
                                "went slow in PS-55; re-run install-autostart.ps1 "
                                "without -LogonType"))
    else:
        out_checks.append(Check("task.logon", FAIL,
                                f"logon type {lt} as {who}; expected Interactive "
                                f"as {opts.user}"))

    trig = _as_list(info.get("triggers"))
    logon = [t for t in trig if "Logon" in (t.get("kind") or "")]
    boot = [t for t in trig if "Boot" in (t.get("kind") or "")]
    if logon:
        t = logon[0]
        text = (f"at log on of {t.get('user') or 'any user'}, "
                f"delay {t.get('delay') or 'none'}")
        if not t.get("enabled", True):
            out_checks.append(Check("task.trigger", FAIL, "logon trigger is disabled"))
        elif t.get("user") and _short_user(t["user"]) != opts.user.lower():
            out_checks.append(Check("task.trigger", FAIL, f"{text}; expected {opts.user}"))
        else:
            out_checks.append(Check("task.trigger", PASS, text))
    elif boot:
        out_checks.append(Check("task.trigger", WARN,
                                f"at startup, delay {boot[0].get('delay') or 'none'} "
                                "(the S4U layout)"))
    else:
        kinds = ", ".join(t.get("kind") or "?" for t in trig) or "none"
        out_checks.append(Check("task.trigger", FAIL,
                                f"no logon or startup trigger ({kinds})"))

    acts = _as_list(info.get("actions"))
    wrapper = (str(opts.repo).rstrip("\\/") + "\\deploy\\run-photonscript.ps1")
    norm = lambda s: s.lower().replace("/", "\\")  # noqa: E731
    if not acts:
        out_checks.append(Check("task.action", FAIL, "task has no action"))
    else:
        a = acts[0]
        exe, args = a.get("execute") or "", a.get("arguments") or ""
        exe_name = re.split(r"[\\/]", exe)[-1]
        detail = f"{exe_name} {args}".strip()
        if "powershell" not in exe.lower() or "run-photonscript.ps1" not in args.lower():
            out_checks.append(Check("task.action", FAIL, f"unexpected action: {detail}"))
        elif norm(wrapper) not in norm(args):
            out_checks.append(Check("task.action", FAIL,
                                    f"wrapper is not {wrapper}: {detail}"))
        elif f"-launcher task-{lt}".lower() not in args.lower():
            out_checks.append(Check("task.action", WARN,
                                    f"no '-Launcher task-{lt}' tag: {detail}"))
        else:
            out_checks.append(Check("task.action", PASS, detail))

    problems = []
    if (info.get("time_limit") or "PT0S") != "PT0S":
        problems.append(f"time limit {info.get('time_limit')} (want none)")
    if info.get("priority") not in (4, None):
        problems.append(f"priority {info.get('priority')} (want 4)")
    if (info.get("multiple_instances") or "IgnoreNew") != "IgnoreNew":
        problems.append(f"multiple instances {info.get('multiple_instances')}")
    out_checks.append(Check("task.settings", WARN if problems else PASS,
                            "; ".join(problems) or
                            "no time limit, priority 4, one instance"))

    res = info.get("last_result")
    last = info.get("last_run") or "never"
    nxt = info.get("next_run") or "at next logon"
    if res is None:
        out_checks.append(Check("task.last_run", WARN, "no run info"))
    else:
        code = int(res) & 0xFFFFFFFF
        text = (f"0x{code:X} {TASK_RESULTS.get(code, 'unknown result')}; "
                f"last run {last}; next {nxt}")
        if code == 0x41301:
            st = PASS
        elif code in (0x0, 0x41300, 0x41303, 0x41306):
            st = WARN
        else:
            st = FAIL
        out_checks.append(Check("task.last_run", st, text))
    return out_checks


# --- Windows auto-logon ------------------------------------------------------------

def autologon_checks(env: Env, opts: Options) -> list[Check]:
    if not env.is_windows:
        return [Check("autologon", SKIP, "not Windows")]
    try:
        wl = env.winlogon() or {}
    except Exception as e:  # noqa: BLE001
        return [Check("autologon", WARN,
                      f"could not read Winlogon: {type(e).__name__}: {e}")]
    on = str(wl.get("AutoAdminLogon", "0")).strip() == "1"
    who = str(wl.get("DefaultUserName", "") or "")
    dom = str(wl.get("DefaultDomainName", "") or "")
    shown = (f"{dom}\\{who}" if dom else who) or "(none)"
    out = []
    if on and _short_user(who) == opts.user.lower():
        out.append(Check("autologon", PASS,
                         f"AutoAdminLogon=1 for {shown} (password not read; the "
                         "reboot test proves it works)"))
    elif on:
        out.append(Check("autologon", FAIL,
                         f"auto-logon is for {shown}, not {opts.user}: the task "
                         f"starts only when {opts.user} logs on"))
    else:
        out.append(Check("autologon", FAIL,
                         f"auto-logon is off (DefaultUserName {shown}): after a "
                         "reboot nothing starts until someone logs on"))
    if "AutoLogonCount" in wl:
        out.append(Check("autologon.count", WARN,
                         f"AutoLogonCount={wl['AutoLogonCount']}: auto-logon stops "
                         "after that many boots; remove the value"))
    return out


# --- processes ------------------------------------------------------------------------

def classify(cmdline) -> Optional[str]:
    toks = [str(t).strip('"').lower() for t in cmdline or []]
    if "run-photonscript.ps1" in " ".join(toks):
        return "wrapper"
    has_ps = any(t == "photonscript.cli" or
                 re.search(r"(^|[\\/])photonscript(\.exe|-script\.py)?$", t)
                 for t in toks)
    if not has_ps:
        return None
    if "supervise" in toks:
        return "supervisor"
    if "start" in toks:
        return "service"
    return None


def group_processes(procs: list) -> dict:
    """{role: [[pid, child pid, ...], ...]}: one chain per logical instance.
    A venv launcher and the python it spawns share a role; a process whose
    parent has the same role belongs to the parent's instance."""
    roles: dict = {"wrapper": [], "supervisor": [], "service": []}
    by_role: dict = {}
    for p in procs:
        r = classify(p.get("cmdline"))
        if r:
            by_role.setdefault(r, []).append(p)
    for r, ps in by_role.items():
        pids = {p["pid"] for p in ps}
        children: dict = {}
        for p in ps:
            children.setdefault(p.get("ppid"), []).append(p["pid"])
        for p in ps:
            if p.get("ppid") in pids:
                continue
            chain, todo = [], [p["pid"]]
            while todo:
                c = todo.pop(0)
                chain.append(c)
                todo.extend(children.get(c, []))
            roles[r].append(chain)
    return roles


def process_checks(env: Env, opts: Options, health_pid: Optional[int]) -> list[Check]:
    data = Path(opts.data_dir)
    out = []
    if env.exists(data / "HOLD"):
        out.append(Check("hold", FAIL, "HOLD marker set (photonscript stop), so the "
                         "supervisor will not restart it; Start-ScheduledTask "
                         "PhotonScript clears it"))
    if env.exists(data / "STOP"):
        out.append(Check("stop_sentinel", WARN,
                         "a STOP sentinel is waiting to be consumed"))

    try:
        procs = env.processes()
    except Exception as e:  # noqa: BLE001
        out.append(Check("processes", WARN, f"could not list processes: {e}"))
        return out
    g = group_processes(procs)

    def _fmt(chains):
        return ", ".join("pid " + ">".join(str(x) for x in c) for c in chains)

    for role, label in (("wrapper", "run-photonscript.ps1 wrapper"),
                        ("supervisor", "photonscript supervise"),
                        ("service", "photonscript start")):
        chains = g[role]
        if len(chains) == 1:
            out.append(Check(f"process.{role}", PASS, f"one {label} ({_fmt(chains)})"))
        elif not chains:
            out.append(Check(f"process.{role}", FAIL, f"no {label} process"))
        else:
            out.append(Check(f"process.{role}", FAIL,
                             f"{len(chains)} {label} instances ({_fmt(chains)}): "
                             "is a console copy running beside the task?"))

    wr = [p for p in procs if classify(p.get("cmdline")) == "wrapper"]
    if wr:
        untagged = [p["pid"] for p in wr
                    if not re.search(r"-launcher\s+task-",
                                     " ".join(map(str, p["cmdline"])).lower())]
        if untagged:
            out.append(Check("process.launched_by", FAIL,
                             f"wrapper pid {untagged[0]} has no '-Launcher task-...' "
                             "tag: it was started by hand in a console"))
        else:
            out.append(Check("process.launched_by", PASS,
                             "the wrapper was started by the scheduled task"))

    if health_pid is not None and g["service"]:
        members = {p for c in g["service"] for p in c}
        if health_pid in members:
            out.append(Check("process.api_pid", PASS,
                             f"/api/health pid {health_pid} is the supervised service"))
        else:
            out.append(Check("process.api_pid", FAIL,
                             f"/api/health answers from pid {health_pid}, which is "
                             "not the supervised service"))
    return out


# --- /api/health ---------------------------------------------------------------------

def get_health(env: Env, opts: Options) -> tuple[Optional[dict], Optional[float], str]:
    url = opts.url.rstrip("/") + "/api/health"
    try:
        code, body, dt = env.http_get(url, opts.health_timeout)
    except Exception as e:  # noqa: BLE001
        kind = type(e).__name__
        why = (f"no answer within {opts.health_timeout:.0f} s (listening but stalled?)"
               if "Timeout" in kind else
               "connection refused (nothing listening)" if "Connect" in kind
               else f"{kind}: {e}")
        return None, None, f"{url}: {why}"
    if code != 200 or not isinstance(body, dict):
        return None, dt, f"{url}: HTTP {code}"
    return body, dt, ""


def _fmt_s(s) -> str:
    if s is None:
        return "?"
    s = int(s)
    h, rem = divmod(s, 3600)
    return f"{h}h {rem // 60}m" if h else f"{rem // 60}m {rem % 60}s"


def health_checks(env: Env, opts: Options, h: Optional[dict],
                  latency: Optional[float], err: str) -> list[Check]:
    if h is None:
        return [Check("api.health", FAIL, err)]
    out = []
    if latency < 1.0:
        out.append(Check("api.health", PASS, f"answered in {latency * 1000:.0f} ms"))
    elif latency < 2.0:
        out.append(Check("api.health", WARN, f"answered in {latency:.1f} s (want < 1 s)"))
    else:
        out.append(Check("api.health", FAIL, f"SLOW: {latency:.1f} s (want < 1 s)"))

    pr = h.get("process") or {}
    up = h.get("uptime_s")
    out.append(Check("service", INFO,
                     f"pid {h.get('pid')}, mode {h.get('mode')}, version "
                     f"{h.get('version')}, up {_fmt_s(up)}, armer {h.get('armer')}"))

    launcher = pr.get("launcher") or "unknown"
    if launcher == "task-Interactive":
        out.append(Check("service.launcher", PASS, launcher))
    elif launcher.startswith("task-"):
        out.append(Check("service.launcher", WARN,
                         f"{launcher}: under the task, but not Interactive"))
    else:
        out.append(Check("service.launcher", FAIL,
                         f"{launcher}: not started by the scheduled task. Stop that "
                         "copy (photonscript stop), then Start-ScheduledTask "
                         "PhotonScript"))

    not_here = SKIP if not env.is_windows else WARN
    sid = pr.get("session_id")
    if sid is None:
        out.append(Check("service.session", not_here, "no session id reported"))
    elif int(sid) == 0:
        out.append(Check("service.session", FAIL,
                         "session 0 (no desktop, the PS-55 slow environment)"))
    else:
        out.append(Check("service.session", PASS, f"session {sid} (a desktop session)"))

    user = pr.get("user")
    if user:
        ok = _short_user(user) == opts.user.lower()
        out.append(Check("service.user", PASS if ok else WARN,
                         user if ok else f"runs as {user}, expected {opts.user}"))

    prio = pr.get("priority_class")
    if prio is None:
        out.append(Check("service.priority", not_here, "not reported"))
    elif prio == "normal":
        out.append(Check("service.priority", PASS, "normal"))
    else:
        out.append(Check("service.priority",
                         FAIL if prio in ("idle", "below_normal") else WARN,
                         f"{prio} (want normal)"))

    thr = pr.get("power_throttling")
    if thr is None:
        out.append(Check("service.throttling", not_here, "not reported"))
    elif thr == "off":
        out.append(Check("service.throttling", PASS, "power throttling off"))
    else:
        out.append(Check("service.throttling", FAIL if thr == "on" else WARN,
                         f"power throttling {thr} (want off)"))

    if pr.get("elevated") is True:
        out.append(Check("service.elevated", WARN,
                         "running elevated (Administrator): files it writes end up "
                         "owned by Administrators"))

    commit = h.get("commit") or ""
    try:
        head = env.git_head(opts.repo)
    except Exception as e:  # noqa: BLE001
        out.append(Check("service.commit", WARN, f"git rev-parse failed: {e}"))
    else:
        if commit and commit == head:
            out.append(Check("service.commit", PASS,
                             f"{commit[:7]} = HEAD of {opts.repo}"))
        else:
            out.append(Check("service.commit", WARN,
                             f"running {commit[:7] or '?'} but {opts.repo} HEAD is "
                             f"{head[:7]}: photonscript restart runs what is on disk"))

    loop = h.get("loop")
    if not loop:
        out.append(Check("loop.lag", WARN, "no loop monitor in /api/health (older build)"))
    else:
        lag = loop.get("lag_ms") or 0
        mx = loop.get("max_lag_ms_5min") or 0
        stalls = loop.get("stalls_5min") or 0
        hb = loop.get("heartbeat_age_s") or 0
        txt = (f"now {lag:.0f} ms, max {mx:.0f} ms in 5 min, {stalls} stall(s) in "
               f"5 min, heartbeat {hb:.1f} s ago")
        if stalls or hb >= 5 or lag >= 1000:
            out.append(Check("loop.lag", FAIL, txt))
        elif lag >= 250 or mx >= 2000:
            note = (" (startup spike? run again once it has been up 10 min)"
                    if up is not None and up < 600 and lag < 250 else "")
            out.append(Check("loop.lag", WARN, txt + note))
        else:
            out.append(Check("loop.lag", PASS, txt))
    return out


# --- logs ---------------------------------------------------------------------------

_TS = r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)"
_STALL = re.compile(_TS + r" Event loop STALLED for ([\d.]+) s \((new|still)\)")
_SUP = re.compile(_TS + r" \[supervisor\]\s+\w+\s+(.*)$")


def _ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def started_local(h: Optional[dict]) -> Optional[datetime]:
    """started_at (UTC ISO) as a naive local time, the clock the logs use."""
    try:
        return datetime.fromisoformat(h["started_at"]).astimezone().replace(tzinfo=None)
    except Exception:  # noqa: BLE001
        return None


def parse_stalls(text: str) -> list[tuple[datetime, float, bool]]:
    return [(_ts(m.group(1)), float(m.group(2)), m.group(3) == "new")
            for m in _STALL.finditer(text or "")]


def parse_supervisor(text: str) -> list[tuple[datetime, str]]:
    out = []
    for line in (text or "").splitlines():
        m = _SUP.match(line)
        if m:
            out.append((_ts(m.group(1)), m.group(2)))
    return out


def log_checks(env: Env, opts: Options, h: Optional[dict]) -> list[Check]:
    out = []
    since = env.now() - timedelta(hours=opts.hours)
    start = started_local(h)
    logs = Path(opts.data_dir) / "logs"

    text = env.read_text(logs / "stalls.log")
    if text is None:
        out.append(Check("log.stalls", PASS, "no stalls.log (the loop has never stalled)"))
    else:
        ev = [e for e in parse_stalls(text) if e[0] >= since]
        episodes = [e for e in ev if e[2]]
        this_run = [e for e in episodes if start and e[0] >= start]
        longest = max((e[1] for e in ev), default=0.0)
        if this_run:
            out.append(Check("log.stalls", FAIL,
                             f"{len(this_run)} stall(s) since this start "
                             f"(first {this_run[0][0]:%H:%M:%S}, longest report "
                             f"{longest:.0f} s); read {logs / 'stalls.log'}"))
        elif episodes:
            out.append(Check("log.stalls", WARN,
                             f"{len(episodes)} stall(s) in the last {opts.hours:g} h, "
                             "none since this start"))
        else:
            out.append(Check("log.stalls", PASS, f"no stalls in the last {opts.hours:g} h"))

    text = env.read_text(logs / "supervisor.log")
    if text is None:
        out.append(Check("log.supervisor", WARN, "no supervisor.log yet"))
    else:
        lines = [(t, m) for t, m in parse_supervisor(text) if t >= since]
        crashes = [t for t, m in lines if m.rstrip().endswith("-> crash")]
        gave_up = [t for t, m in lines if "Giving up" in m]
        starts = [t for t, m in lines if m.startswith("Started PhotonScript")]
        txt = (f"last {opts.hours:g} h: {len(starts)} start(s), {len(crashes)} "
               f"crash restart(s)")
        if crashes:
            txt += " at " + ", ".join(f"{t:%m-%d %H:%M}" for t in crashes[-5:])
        if gave_up:
            out.append(Check("log.supervisor", FAIL,
                             f"{txt}; crash-loop give-up at {gave_up[-1]:%m-%d %H:%M}"))
        elif crashes:
            out.append(Check("log.supervisor", WARN,
                             f"{txt} (expected only after a kill test)"))
        else:
            out.append(Check("log.supervisor", PASS, txt))
    return out


# --- neighbours -------------------------------------------------------------------------

def neighbour_checks(env: Env, opts: Options) -> list[Check]:
    out = []
    for label, base in opts.nina_urls:
        url = base.rstrip("/") + "/version"
        try:
            code, body, dt = env.http_get(url, 5.0)
        except Exception as e:  # noqa: BLE001
            out.append(Check(f"nina.{label}", WARN,
                             f"{base} not reachable ({type(e).__name__}): start NINA "
                             f"#{label} before arming"))
            continue
        ver = body.get("Response") if isinstance(body, dict) else None
        if code == 200:
            out.append(Check(f"nina.{label}", PASS,
                             f"{base} answered ({ver or 'ok'}, {dt * 1000:.0f} ms)"))
        else:
            out.append(Check(f"nina.{label}", WARN, f"{url}: HTTP {code}"))
    host, port = opts.phd2
    if host:
        if env.tcp_ok(host, port, 2.0):
            out.append(Check("phd2", PASS, f"{host}:{port} listening"))
        else:
            out.append(Check("phd2", WARN, f"{host}:{port} not listening: start PHD2 "
                                           "before a guided night"))
    if opts.tailscale_url:
        url = opts.tailscale_url.rstrip("/") + "/api/health"
        try:
            code, _, dt = env.http_get(url, 10.0)
        except Exception as e:  # noqa: BLE001
            out.append(Check("tailscale", WARN, f"{opts.tailscale_url} not reachable "
                                                f"({type(e).__name__})"))
        else:
            out.append(Check("tailscale", PASS if code == 200 else WARN,
                             f"{opts.tailscale_url}: HTTP {code} in {dt * 1000:.0f} ms"))
    return out


# --- all together -------------------------------------------------------------------------

def run_checks(env: Env, opts: Options) -> tuple[list[Check], Optional[dict]]:
    checks: list[Check] = []
    checks += task_checks(env, opts)
    checks += autologon_checks(env, opts)
    h, lat, err = get_health(env, opts)
    checks += health_checks(env, opts, h, lat, err)
    checks += process_checks(env, opts, h.get("pid") if h else None)
    checks += log_checks(env, opts, h)
    checks += neighbour_checks(env, opts)
    return checks, h


def verdict(checks: list[Check]) -> tuple[str, int]:
    fails = sum(c.status == FAIL for c in checks)
    warns = sum(c.status == WARN for c in checks)
    passes = sum(c.status == PASS for c in checks)
    if fails:
        return f"AUTOSTART FAIL: {fails} fail, {warns} warn, {passes} pass", 1
    if warns:
        return f"AUTOSTART OK with warnings: {warns} warn, {passes} pass", 0
    return f"AUTOSTART OK: {passes} pass", 0


# --- restart watch ------------------------------------------------------------------------

def watch_restart(env: Env, opts: Options, *, kill: bool = False,
                  wait_for_kill_s: float = 300.0, max_recover_s: float = 180.0,
                  poll_s: float = 2.0, say: Callable[[str], None] = print) -> list[Check]:
    """Record the service pid, let it be killed (by the operator, or here with
    ``kill``), then poll /api/health until a NEW pid answers in under 1 s.
    Reports time to recover, the supervisor's crash line and whether the
    "PhotonScript crashed" Pushover went out (notifications.jsonl)."""
    h, _, err = get_health(env, opts)
    if h is None:
        return [Check("watch.baseline", FAIL, f"service is not up: {err}")]
    pid0 = h.get("pid")
    armer = str(h.get("armer") or "")
    say(f"Service pid {pid0}, armer {armer or '?'}.")
    t_wall = env.now()

    if kill:
        if armer in ARMER_ACTIVE:
            return [Check("watch.kill", FAIL,
                          f"refusing to kill: armer is {armer}. Disarm first "
                          "(daytime test only)")]
        ok, detail = env.kill(int(pid0))
        if not ok:
            return [Check("watch.kill", FAIL, f"kill of pid {pid0} failed: {detail}")]
        say(f"Killed pid {pid0} ({detail}).")
    else:
        say(f"Now kill it from another PowerShell:  Stop-Process -Id {pid0} -Force")
        say(f"(waiting up to {wait_for_kill_s:.0f} s)")
    t_start = env.clock()

    # phase 1: the old pid goes away
    t_down = None
    limit = 60.0 if kill else wait_for_kill_s
    while env.clock() - t_start <= limit:
        h2, _, _ = get_health(env, opts)
        if h2 is None or h2.get("pid") != pid0:
            t_down = env.clock()
            break
        env.sleep(poll_s)
    if t_down is None:
        return [Check("watch.down", FAIL,
                      f"pid {pid0} still answering after {limit:.0f} s")]
    say("Old service is gone; waiting for the supervisor to start a new one.")

    # phase 2: a new pid answers fast
    new = None
    while env.clock() - t_down <= max_recover_s:
        h2, lat2, _ = get_health(env, opts)
        if h2 is not None and h2.get("pid") not in (None, pid0) \
                and lat2 is not None and lat2 < 1.0:
            new = h2
            break
        env.sleep(poll_s)
    if new is None:
        return [Check("watch.recover", FAIL,
                      f"no healthy new service within {max_recover_s:.0f} s; read "
                      "supervisor.log and Get-ScheduledTaskInfo PhotonScript")]
    took = env.clock() - (t_start if kill else t_down)
    out = [Check("watch.recover", PASS if took <= 60 else WARN,
                 f"new pid {new.get('pid')} healthy {took:.0f} s after the "
                 f"{'kill' if kill else 'old pid went away'} (expect about 10 to "
                 "40 s: 5 s backoff plus startup)")]

    slack = t_wall - timedelta(seconds=5)
    logs = Path(opts.data_dir) / "logs"
    sup = [m for t, m in parse_supervisor(env.read_text(logs / "supervisor.log") or "")
           if t >= slack and m.rstrip().endswith("-> crash")]
    out.append(Check("watch.supervisor", PASS if sup else WARN,
                     sup[-1] if sup else
                     "no '-> crash' line in supervisor.log after the kill"))

    notes = []
    for line in (env.read_text(Path(opts.data_dir) / "notifications.jsonl") or "").splitlines():
        try:
            rec = json.loads(line)
            ts = datetime.fromisoformat(rec["ts"]).astimezone().replace(tzinfo=None)
        except Exception:  # noqa: BLE001
            continue
        if rec.get("title") == "PhotonScript crashed" and ts >= slack:
            notes.append(rec)
    if not notes:
        out.append(Check("watch.pushover", WARN,
                         "no 'PhotonScript crashed' entry in notifications.jsonl"))
    elif notes[-1].get("sent"):
        out.append(Check("watch.pushover", PASS,
                         "'PhotonScript crashed' was sent: check the phone"))
    else:
        why = notes[-1].get("reason") or "?"
        hint = (" (daytime rule: one alert per title per 4 h, so an earlier test "
                "today used it up)" if why == "quiet-daytime" else "")
        out.append(Check("watch.pushover", WARN, f"alert NOT sent: {why}{hint}"))
    return out
