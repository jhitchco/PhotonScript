"""Remote CLI: run an allowlisted photonscript command on the scope PC.

The desktop (Claude or Jeremy) posts {"args": [...]} to POST /api/cli/run
(routers/remote_cli.py); this module validates the args against
config/remote_cli.toml, runs `python -m photonscript <args>` in a child
process (no shell: an argv list), keeps a capped tail of its output, kills
the child's whole process tree on the hard timeout and appends every run and
every refusal to <data_dir>/remote_cli/audit.jsonl.

Safety model (an observatory with a mount and a roof):
- allowlist, never a denylist: a command not in the TOML is refused (403);
- per-command flag allowlist, checked against the command's own typer
  options (a flag the command does not have is 400, one it has but that is
  not allowed remotely is 403), and the full argv is parsed by click before
  anything runs (types, extra arguments);
- `write` jobs need confirm="write" and are refused mid-night unless the
  command is night_safe;
- one job at a time per class; hard timeout; output capped.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_PATH = REPO_ROOT / "config" / "remote_cli.toml"

MAX_ARGS = 64
MAX_ARG_LEN = 512
MIN_TOKEN_LEN = 16
# armer states during which a write job (not night_safe) is refused; the same
# set /api/update refuses a restart in (app._update_blockers)
NIGHT_STATES = ("RUNNING", "PAUSED_UNSAFE", "WATCHING", "PAUSED_OPERATOR")
# env the child never inherits: the token itself, and PS_MONITOR_URL (typer
# envvar of every --url option: it would turn a local report into an HTTP one)
CHILD_ENV_DROP = ("PS_REMOTE_CLI_TOKEN", "PS_MONITOR_URL")

_NUM = re.compile(r"^-\d+(\.\d*)?([eE][-+]?\d+)?$|^-\.\d+$")


class Refused(Exception):
    """A request the remote CLI will not run: HTTP status + message."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Rule:
    name: str
    cls: str = "read"
    flags: tuple = ()
    write_flags: tuple = ()
    require: tuple = ()
    night_safe: bool = False


@dataclass
class Allowlist:
    commands: dict = field(default_factory=dict)
    default_timeout_s: int = 300
    max_timeout_s: int = 1800
    output_cap_bytes: int = 262144
    tail_bytes: int = 65536
    keep_jobs: int = 50


def load_allowlist(path: Optional[Path] = None) -> Allowlist:
    """Parse config/remote_cli.toml. A missing or broken file = nothing allowed."""
    p = Path(path or ALLOWLIST_PATH)
    try:
        doc = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        logger.warning("remote CLI allowlist %s unreadable (%s): nothing is allowed", p, e)
        return Allowlist()
    lim = doc.get("limits") or {}
    cmds = {}
    for name, c in (doc.get("commands") or {}).items():
        cls = str(c.get("class") or "read")
        if cls not in ("read", "write"):
            logger.warning("remote CLI allowlist: %s has class %r, skipped", name, cls)
            continue
        cmds[name] = Rule(name=name, cls=cls, flags=tuple(c.get("flags") or ()),
                          write_flags=tuple(c.get("write_flags") or ()),
                          require=tuple(c.get("require") or ()),
                          night_safe=bool(c.get("night_safe", False)))
    out = Allowlist(commands=cmds)
    for k in ("default_timeout_s", "max_timeout_s", "output_cap_bytes", "tail_bytes",
              "keep_jobs"):
        if k in lim:
            setattr(out, k, int(lim[k]))
    return out


# ------------------------------------------------------------------ the CLI

_GROUP = None


def cli_group():
    """The click group behind photonscript.cli.app (cached)."""
    global _GROUP
    if _GROUP is None:
        import typer.main
        from photonscript import cli
        _GROUP = typer.main.get_command(cli.app)
    return _GROUP


def command_flags(cmd) -> dict:
    """{flag: param name} for every option string of a click command."""
    out = {}
    for p in cmd.params:
        for o in list(getattr(p, "opts", []) or []) + list(getattr(p, "secondary_opts", []) or []):
            if o.startswith("-"):
                out[o] = p.name
    return out


def check_allowlist(allow: Allowlist) -> list[str]:
    """Problems in the allowlist vs the real CLI (for the tests): a listed
    command the CLI does not have, or a listed flag the command does not take."""
    group = cli_group()
    bad = []
    for name, r in allow.commands.items():
        cmd = group.commands.get(name)
        if cmd is None:
            bad.append(f"{name}: no such photonscript command")
            continue
        have = command_flags(cmd)
        for f in set(r.flags) | set(r.write_flags) | set(r.require):
            if f not in have:
                bad.append(f"{name}: {f} is not an option of the command")
        for f in set(r.write_flags) | set(r.require):
            if f not in r.flags:
                bad.append(f"{name}: {f} is in write_flags/require but not in flags")
    return bad


@dataclass
class Plan:
    args: list
    command: str
    cls: str
    night_safe: bool


def _flag_tokens(argv: list) -> list[str]:
    """The option names in argv (`--x=1` -> `--x`); negative numbers are values."""
    out = []
    for a in argv:
        if a.startswith("-") and not _NUM.match(a):
            out.append(a.split("=", 1)[0])
    return out


def validate(args, allow: Allowlist) -> Plan:
    """Check a request's argv against the allowlist and the command's own
    options; raises Refused. Nothing runs here."""
    if not isinstance(args, list) or not args:
        raise Refused(400, "args must be a non-empty list of strings, e.g. "
                           '["frame-check", "--last", "3"]')
    if len(args) > MAX_ARGS:
        raise Refused(400, f"too many args (max {MAX_ARGS})")
    for a in args:
        if not isinstance(a, str):
            raise Refused(400, "every arg must be a string")
        if len(a) > MAX_ARG_LEN or "\x00" in a or "\n" in a or "\r" in a:
            raise Refused(400, f"arg too long or with a control character: {a[:40]!r}")
    if args == ["--help"]:
        return Plan(list(args), "--help", "read", True)
    name, rest = args[0], list(args[1:])
    group = cli_group()
    cmd = group.commands.get(name)
    if name.startswith("-"):
        raise Refused(400, f"{name}: the first arg is the photonscript command "
                           "(global options are not accepted)")
    if rest == ["--help"]:
        if cmd is None:
            raise Refused(400, f"{name}: no such photonscript command")
        return Plan(list(args), name, "read", True)
    if "--help" in rest:
        raise Refused(400, "--help only as `<command> --help`")
    rule = allow.commands.get(name)
    if rule is None or cmd is None:
        raise Refused(403, f"{name}: not on the remote CLI allowlist "
                           "(config/remote_cli.toml). Commands that move hardware, "
                           "arm / disarm, start or stop sequences, restart or update "
                           "the service, notify, connect devices or delete files "
                           "never are; run those at the scope PC.")
    if "--" in rest:
        raise Refused(400, "a bare -- is not accepted")
    have = command_flags(cmd)
    used = _flag_tokens(rest)
    for f in used:
        if f not in have:
            raise Refused(400, f"{name}: unknown flag {f} (not an option of the command)")
        if f not in rule.flags:
            raise Refused(403, f"{name}: flag {f} is not allowed remotely "
                               f"(allowed: {', '.join(rule.flags) or 'none'})")
    for f in rule.require:
        if f not in used:
            raise Refused(403, f"{name}: runs remotely only with {f}")
    # click parses the whole argv (types, missing values, extra arguments)
    # without invoking the command
    # (typer may vendor its own click: match the exceptions by shape)
    try:
        ctx = cmd.make_context(name, list(rest), resilient_parsing=False)
        ctx.close()
    except Exception as e:  # noqa: BLE001
        if type(e).__name__ != "Exit":
            msg = e.format_message() if hasattr(e, "format_message") else str(e)
            raise Refused(400, f"{name}: {msg}")
    cls = "write" if rule.cls == "write" or any(f in used for f in rule.write_flags) \
        else "read"
    return Plan(list(args), name, cls, rule.night_safe)


def write_blockers(plan: Plan, armer_state: str, jobs: list[str]) -> list[str]:
    """Why a write job must wait (empty = go)."""
    if plan.cls != "write" or plan.night_safe:
        return []
    why = []
    st = str(armer_state or "").upper()
    if st in NIGHT_STATES:
        why.append(f"armer is {st}: {plan.command} is a write command and not "
                   "night_safe; run it after the night")
    if jobs:
        why.append("grading job(s) running: " + "; ".join(jobs) + ". Try again when they finish.")
    return why


def token_ok(configured: str, given: str) -> tuple[bool, int, str]:
    """(ok, status, detail) for the shared token. Empty or short configured
    token = the feature is off."""
    import hmac
    configured = (configured or "").strip()
    if len(configured) < MIN_TOKEN_LEN:
        return False, 403, ("remote CLI is disabled: set PS_REMOTE_CLI_TOKEN (16+ "
                            "characters) in the scope PC's .env and restart PhotonScript")
    if not given or not hmac.compare_digest(configured.encode(), str(given).strip().encode()):
        return False, 401, "missing or wrong X-PhotonScript-Token header"
    return True, 200, ""


# ------------------------------------------------------------------ audit

def audit_path(config) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "remote_cli" / "audit.jsonl"


_AUDIT_LOCK = threading.Lock()


def audit(config, rec: dict) -> None:
    p = audit_path(config)
    line = json.dumps({"t": _now_z(), **rec}, default=str)
    try:
        with _AUDIT_LOCK:
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError as e:
        logger.warning("remote CLI audit not written: %s", e)


def read_audit(config, limit: int = 50) -> list[dict]:
    """Newest first."""
    p = audit_path(config)
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for ln in reversed(lines):
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


def _now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ runner

class _Tail:
    """Keeps the last `cap` bytes of a stream and counts the rest."""

    def __init__(self, cap: int):
        self.cap = max(1024, int(cap))
        self.buf = bytearray()
        self.total = 0
        self.lock = threading.Lock()

    def add(self, b: bytes) -> None:
        with self.lock:
            self.total += len(b)
            self.buf += b
            if len(self.buf) > self.cap:
                del self.buf[:len(self.buf) - self.cap]

    def text(self, n: Optional[int] = None) -> str:
        with self.lock:
            b = bytes(self.buf if n is None else self.buf[-max(0, n):])
        return b.decode("utf-8", errors="replace")

    @property
    def truncated(self) -> bool:
        return self.total > len(self.buf)


def child_env() -> dict:
    env = {k: v for k, v in os.environ.items() if k not in CHILD_ENV_DROP}
    env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "NO_COLOR": "1",
                "TERM": "dumb", "COLUMNS": "160"})
    return env


def kill_tree(pid: int) -> None:
    """Kill a process and every descendant."""
    try:
        import psutil
        try:
            parent = psutil.Process(pid)
        except psutil.NoSuchProcess:
            return
        procs = parent.children(recursive=True) + [parent]
        for p in procs:
            try:
                p.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(procs, timeout=10)
    except Exception as e:  # noqa: BLE001
        logger.warning("remote CLI kill of %s failed: %s", pid, e)


def default_argv(args: list) -> list:
    return [sys.executable, "-m", "photonscript", *args]


class Runner:
    """Runs jobs in background threads; one job at a time per class."""

    def __init__(self, argv_for: Callable[[list], list] = default_argv,
                 cwd: Path = REPO_ROOT):
        self.argv_for = argv_for
        self.cwd = cwd
        self.jobs: dict = {}
        self._order: list = []
        self._busy: dict = {"read": None, "write": None}
        self._lock = threading.Lock()

    def busy(self, cls: str) -> Optional[str]:
        with self._lock:
            return self._busy.get(cls)

    def start(self, config, plan: Plan, timeout_s: float, allow: Allowlist,
              client: dict) -> dict:
        jid = uuid.uuid4().hex[:12]
        with self._lock:
            if self._busy.get(plan.cls):
                raise Refused(409, f"a {plan.cls} job is already running "
                                   f"({self._busy[plan.cls]}); one at a time")
            self._busy[plan.cls] = jid
            job = {"id": jid, "args": list(plan.args), "command": plan.command,
                   "class": plan.cls, "status": "running", "exit_code": None,
                   "started": _now_z(), "finished": None, "duration_s": None,
                   "timeout_s": timeout_s, "client": client,
                   "_out": _Tail(allow.output_cap_bytes), "_err": _Tail(allow.output_cap_bytes)}
            self.jobs[jid] = job
            self._order.append(jid)
            while len(self._order) > max(1, allow.keep_jobs):
                old = self._order.pop(0)
                if self.jobs.get(old, {}).get("status") == "running":
                    self._order.insert(0, old)
                    break
                self.jobs.pop(old, None)
        t = threading.Thread(target=self._run, args=(config, job), daemon=True,
                             name=f"remote-cli-{jid}")
        t.start()
        return job

    def _run(self, config, job: dict) -> None:
        t0 = time.monotonic()
        status, code = "error", None
        try:
            kw = {}
            if os.name == "nt":
                kw["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                       | getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                kw["start_new_session"] = True
            proc = subprocess.Popen(self.argv_for(job["args"]), cwd=str(self.cwd),
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=child_env(),
                                    shell=False, **kw)
            job["pid"] = proc.pid
            readers = [threading.Thread(target=_pump, args=(proc.stdout, job["_out"]),
                                        daemon=True),
                       threading.Thread(target=_pump, args=(proc.stderr, job["_err"]),
                                        daemon=True)]
            for r in readers:
                r.start()
            try:
                code = proc.wait(timeout=job["timeout_s"])
                status = "done" if code == 0 else "failed"
            except subprocess.TimeoutExpired:
                kill_tree(proc.pid)
                try:
                    code = proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    code = None
                status = "timeout"
                job["_err"].add(f"\n[remote CLI] killed after {job['timeout_s']:g} s "
                                "(hard timeout)\n".encode())
            for r in readers:
                r.join(timeout=5)
        except Exception as e:  # noqa: BLE001
            job["_err"].add(f"[remote CLI] could not run: {type(e).__name__}: {e}\n".encode())
            status = "error"
        finally:
            job["exit_code"] = code
            job["duration_s"] = round(time.monotonic() - t0, 2)
            job["finished"] = _now_z()
            job["status"] = status
            with self._lock:
                if self._busy.get(job["class"]) == job["id"]:
                    self._busy[job["class"]] = None
            audit(config, {"event": "run", "id": job["id"], **job["client"],
                           "args": job["args"], "class": job["class"],
                           "status": status, "exit": code,
                           "duration_s": job["duration_s"],
                           "stdout_bytes": job["_out"].total,
                           "stderr_bytes": job["_err"].total})

    def view(self, jid: str, tail: Optional[int] = None) -> Optional[dict]:
        job = self.jobs.get(jid)
        if job is None:
            return None
        return public(job, tail)

    def recent(self) -> list[dict]:
        with self._lock:
            ids = list(reversed(self._order))
        return [public(self.jobs[i], 0) for i in ids if i in self.jobs]


def _pump(stream, tail: _Tail) -> None:
    try:
        while True:
            b = stream.read1(65536) if hasattr(stream, "read1") else stream.read(65536)
            if not b:
                break
            tail.add(b)
    except (OSError, ValueError):
        pass


def public(job: dict, tail: Optional[int] = None) -> dict:
    """The job without its buffers; with tail > 0 the last `tail` bytes of
    stdout / stderr (None = everything kept)."""
    out = {k: v for k, v in job.items() if not k.startswith("_")}
    o, e = job["_out"], job["_err"]
    out["stdout_bytes"], out["stderr_bytes"] = o.total, e.total
    out["truncated"] = o.truncated or e.truncated
    if tail != 0:
        out["stdout"] = o.text(tail)
        out["stderr"] = e.text(tail)
    return out


RUNNER = Runner()
