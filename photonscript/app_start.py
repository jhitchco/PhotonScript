"""`photonscript apps`: status of the observatory apps, and starting ONE
missing app on the scope PC (NINA #1, NINA #2 or PHD2).

--start runs deploy\\observatory-apps.ps1 -Start -Only <app> (PS-170): the
script launches the app with its profile, waits for its port, then runs the
same connect-only step the arm does (POST /api/equipment/connect, which only
connects devices that are disconnected) and preflight. This module never
closes, kills or restarts an app, and refuses to start anything when:

  - the app already answers on its port (nothing to do);
  - a process of that app is open but its port does not answer (a NINA
    still loading, or one with its API off): a launch would make a second
    instance;
  - this process runs in session 0 (a Task Scheduler "run whether logged
    on or not" service): a GUI app started from there is invisible and
    unusable on jeremy's desktop;
  - the app is TheSky (the script never launches TheSky: open it by hand);
  - NINA #2 is asked for while the piggyback rig is off.

How the GUI reaches the desktop: the PhotonScript service runs as the
"PhotonScript" scheduled task, logon type Interactive (install-autostart.ps1,
launcher task-Interactive), in jeremy's desktop session. The remote CLI runs
this command as a child of the service, so it is in that session too, and
the script's Start-Process puts NINA / PHD2 on that desktop, exactly as the
"PhotonScript Apps Start" task (Interactive, install-app-lifecycle-tasks.ps1)
does.

The script is started detached from this process with its output in
<data_dir>\\logs\\apps-start-last.out, and this command returns before the
remote CLI hard timeout: the remote CLI kills the whole process tree of a
job that times out, and a NINA still parented to a live script would go
with it. When the wait runs out the script keeps running on its own.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "deploy" / "observatory-apps.ps1"
APPS = ("nina1", "nina2", "phd2", "thesky")
LAUNCHABLE = ("nina1", "nina2", "phd2")
DEFAULT_TIMEOUT_S = 240    # under the remote CLI's default 300 s hard timeout
OUT_NAME = "apps-start-last.out"


class StartRefused(Exception):
    """Nothing was started: why."""


@dataclass
class StartResult:
    app: str
    ok: bool
    answered: bool
    script_exit: Optional[int]
    script_done: bool
    waited_s: float
    version: str = ""
    argv: list = field(default_factory=list)
    output: str = ""
    message: str = ""


# ------------------------------------------------------------------ probes

def session_id() -> Optional[int]:
    """Windows session of this process (0 = no desktop); None elsewhere."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        sid = ctypes.c_ulong()
        if ctypes.windll.kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):
            return int(sid.value)
    except Exception:  # noqa: BLE001
        pass
    return None


def nina_version(host: str, port: int, timeout: float = 3.0) -> str:
    """NINA's Advanced API version ("" when it does not answer)."""
    try:
        import httpx
        r = httpx.get(f"http://{host}:{port}/v2/api/version", timeout=timeout)
        if r.status_code == 200:
            body = r.json()
            v = body.get("Response") if isinstance(body, dict) else body
            return str(v or "answers")
    except Exception:  # noqa: BLE001
        pass
    return ""


def app_answers(key: str, host: str, port: int, port_fn: Callable,
                nina_fn: Callable = nina_version) -> tuple[bool, str]:
    """(answers, version). NINA must answer /v2/api/version; PHD2 / TheSky a
    TCP connect."""
    if key in ("nina1", "nina2"):
        v = nina_fn(host, port)
        return bool(v), v
    return bool(port_fn(host, port)), ""


def snapshot(cfg, port_fn: Callable = None, procs_fn: Callable = None) -> dict:
    """app_lifecycle.probe plus the session: {"apps": {...}, "session_id"}."""
    from photonscript.scheduler import app_lifecycle as al
    kw = {}
    if port_fn is not None:
        kw["port_fn"] = port_fn
    if procs_fn is not None:
        kw["procs_fn"] = procs_fn
    return {"apps": al.probe(cfg, **kw), "session_id": session_id()}


def format_status(snap: dict) -> str:
    lines = []
    for key, a in snap["apps"].items():
        procs = a.get("processes")
        lines.append(f"{a['label']:<20} :{a['port']:<5} "
                     f"{'answers' if a['answering'] else 'NOT answering'}"
                     f"{'' if procs is None else f'  ({procs} process(es) of that exe)'}")
    sid = snap.get("session_id")
    if sid is not None:
        lines.append(f"this process: session {sid}"
                     + (" (no desktop: --start refuses here)" if sid == 0 else ""))
    return "\n".join(lines)


# ------------------------------------------------------------------ guards

def start_blockers(key: str, apps: dict, sid: Optional[int]) -> list[str]:
    """Why `--start --only key` must not launch anything (empty = go)."""
    from photonscript.scheduler import app_lifecycle as al
    if key not in APPS:
        return [f"unknown app {key!r} (one of {', '.join(APPS)})"]
    if key == "thesky":
        return ["TheSky is never launched from here (observatory-apps.ps1 only "
                "checks it): open TheSky64 at the scope PC and connect the mount"]
    a = apps.get(key)
    if a is None:
        return [f"{al.LABELS.get(key, key)} is not used by this config "
                "(piggyback rig off?)"]
    why = []
    if a.get("answering"):
        why.append(f"{a['label']} already answers on :{a['port']}: nothing started, "
                   "nothing closed")
    procs = a.get("processes")
    if procs is None:
        why.append("the process list cannot be read (psutil), so a second instance "
                   "cannot be ruled out")
    else:
        if key in ("nina1", "nina2"):
            ninas_up = sum(1 for k in ("nina1", "nina2")
                           if (apps.get(k) or {}).get("answering"))
            if procs > ninas_up:
                why.append(f"{procs} NINA process(es) open but only {ninas_up} NINA "
                           "port(s) answer: a NINA is loading or has its API off. "
                           "Not launching a second one; check the scope PC desktop")
        elif procs > 0 and not a.get("answering"):
            why.append(f"{a['label']} is open ({procs} process(es)) but :{a['port']} "
                       "does not answer: not launching a second one")
    if sid == 0:
        why.append("this process runs in session 0 (no desktop): a GUI app started "
                   "here would be invisible and unusable. Run the 'PhotonScript Apps "
                   "Start' task instead (schtasks /Run /TN \"PhotonScript Apps "
                   "Start\", Interactive) or start it at the scope PC")
    return why


# ------------------------------------------------------------------ launch

def script_argv(key: str, start_timeout_s: int, *, no_connect: bool = False,
                script: Path = SCRIPT) -> list[str]:
    """powershell argv for observatory-apps.ps1 -Start -Only <key>."""
    if key not in LAUNCHABLE:
        raise ValueError(key)
    ps = Path(os.environ.get("SystemRoot", r"C:\Windows")) / \
        "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    argv = [str(ps), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(script), "-Start", "-Only", key,
            "-StartTimeoutS", str(int(start_timeout_s))]
    if no_connect:
        argv.append("-NoConnect")
    return argv


def popen_detached(argv: list, out_path: Path):
    """Start the script with its own console-less process group, output to a
    file (never a pipe: the script must survive this process exiting)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(out_path, "wb")
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                               | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kw["start_new_session"] = True
    try:
        return subprocess.Popen(argv, cwd=str(REPO_ROOT), stdin=subprocess.DEVNULL,
                                stdout=fh, stderr=subprocess.STDOUT, shell=False, **kw)
    finally:
        fh.close()


def start_app(cfg, key: str, *, timeout_s: float = DEFAULT_TIMEOUT_S,
              no_connect: bool = False, launcher: Callable = popen_detached,
              port_fn: Callable = None, procs_fn: Callable = None,
              nina_fn: Callable = nina_version, sid_fn: Callable = session_id,
              sleep: Callable = time.sleep, clock: Callable = time.monotonic,
              poll_s: float = 3.0) -> StartResult:
    """Guard, launch the script, wait for the app's port (and the script).
    Raises StartRefused before anything runs."""
    from photonscript.scheduler import app_lifecycle as al
    port_fn = port_fn or al.port_open
    apps = al.probe(cfg, port_fn=port_fn, **({"procs_fn": procs_fn} if procs_fn else {}))
    a = apps.get(key) or {}
    # NINA "answers" means its API version answers, not just the TCP port
    if key in ("nina1", "nina2") and a and not a.get("answering"):
        up, _v = app_answers(key, a["host"], a["port"], port_fn, nina_fn)
        a["answering"] = up
    why = start_blockers(key, apps, sid_fn())
    if why:
        raise StartRefused("; ".join(why))
    timeout_s = max(30.0, float(timeout_s))
    start_timeout = int(max(20, timeout_s - 20))
    argv = script_argv(key, start_timeout, no_connect=no_connect)
    out_path = Path(getattr(cfg, "data_dir", ".")) / "logs" / OUT_NAME
    proc = launcher(argv, out_path)
    t0 = clock()
    answered, version, code = False, "", None
    while True:
        if not answered:
            answered, version = app_answers(key, a["host"], a["port"], port_fn, nina_fn)
        code = proc.poll()
        if code is not None:
            # the script waited for the port itself; one last look
            if not answered:
                answered, version = app_answers(key, a["host"], a["port"], port_fn, nina_fn)
            break
        if clock() - t0 >= timeout_s:
            break
        sleep(poll_s)
    waited = round(clock() - t0, 1)
    try:
        output = out_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        output = ""
    done = code is not None
    if answered and done and code == 0:
        msg = f"{a['label']} answers on :{a['port']}" + (f" (NINA {version})" if version else "")
    elif answered and not done:
        msg = (f"{a['label']} answers on :{a['port']}; the script is still running "
               f"(connect / preflight), see <data_dir>\\logs\\observatory-apps.log")
    elif answered:
        msg = f"{a['label']} answers on :{a['port']} but the script exited {code}"
    elif done:
        msg = f"{a['label']} did NOT answer; the script exited {code}"
    else:
        msg = (f"{a['label']} did not answer within {timeout_s:g} s; the script is "
               "still running on its own (see <data_dir>\\logs\\observatory-apps.log)")
    return StartResult(app=key, ok=bool(answered), answered=bool(answered),
                       script_exit=code, script_done=done, waited_s=waited,
                       version=version, argv=argv, output=output, message=msg)


def result_dict(r: StartResult) -> dict:
    return {"app": r.app, "ok": r.ok, "answered": r.answered,
            "script_exit": r.script_exit, "script_done": r.script_done,
            "waited_s": r.waited_s, "version": r.version, "argv": r.argv,
            "message": r.message, "output": r.output}


def print_json(obj) -> None:
    sys.stdout.write(json.dumps(obj, indent=2, default=str) + "\n")
