"""`photonscript apps` (photonscript/app_start.py): start ONE missing
observatory app through deploy\\observatory-apps.ps1 -Start -Only, never a
second instance, never a close."""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from photonscript import app_start as ast
from photonscript import cli
from photonscript.scheduler import remote_cli as rc
from photonscript.shared.config import PhotonScriptConfig


def _cfg(tmp_path, **kw):
    kw.setdefault("piggyback_enabled", True)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


class FakeProc:
    def __init__(self, exits_after=None, code=0):
        self.polls = 0
        self.exits_after = exits_after
        self.code = code
        self.killed = False

    def poll(self):
        self.polls += 1
        if self.exits_after is not None and self.polls >= self.exits_after:
            return self.code
        return None

    def kill(self):   # start_app must never call this
        self.killed = True


class FakeLauncher:
    """Records the argv; the app 'comes up' when launched (up_after polls)."""

    def __init__(self, world, key_port, proc, up=True):
        self.world, self.key_port, self.proc, self.up = world, key_port, proc, up
        self.calls = []

    def __call__(self, argv, out_path):
        self.calls.append((list(argv), out_path))
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("2026-10-17 [start] NINA #2 launched\n", encoding="utf-8")
        if self.up:
            self.world["up"].add(self.key_port)
        return self.proc


def _world(up=(1888, 4400, 3040), ninas=1, phd2=1, sky=1):
    w = {"up": set(up), "procs": {"nina.exe": ninas, "phd2.exe": phd2,
                                  "thesky64.exe": sky}}
    w["port_fn"] = lambda h, p: p in w["up"]
    w["nina_fn"] = lambda h, p: "3.1.2.9001" if p in w["up"] else ""
    w["procs_fn"] = lambda: dict(w["procs"])
    return w


def _start(cfg, key, w, launcher, sid=1, timeout_s=60, **kw):
    t = {"now": 0.0}

    def sleep(s):
        t["now"] += s

    return ast.start_app(cfg, key, timeout_s=timeout_s, launcher=launcher,
                         port_fn=w["port_fn"], procs_fn=w["procs_fn"],
                         nina_fn=w["nina_fn"], sid_fn=lambda: sid,
                         sleep=sleep, clock=lambda: t["now"], **kw)


# ------------------------------------------------------------------ allowlist

def test_allowlist_entry():
    allow = rc.load_allowlist()
    assert "apps" in allow.commands
    assert rc.check_allowlist(allow) == []
    assert rc.validate(["apps"], allow).cls == "read"
    assert rc.validate(["apps", "--status", "--json"], allow).cls == "read"
    p = rc.validate(["apps", "--start", "--only", "nina2"], allow)
    assert p.cls == "write" and p.night_safe
    # night_safe: not refused mid-night (tonight the armer is PAUSED_UNSAFE)
    assert rc.write_blockers(p, "PAUSED_UNSAFE", []) == []
    assert rc.write_blockers(p, "RUNNING", []) == []


def test_remote_start_needs_confirm_write(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from photonscript.scheduler.routers import remote_cli as router
    cfg = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                             remote_cli_token="t" * 32)
    monkeypatch.setattr(router, "_cfg", lambda: cfg)
    monkeypatch.setattr(router, "_armer_state", lambda: "PAUSED_UNSAFE")
    monkeypatch.setattr(router, "_grading_jobs", lambda: [])
    started = []
    monkeypatch.setattr(rc.RUNNER, "start",
                        lambda *a, **k: started.append(a[1]) or {"id": "x"})
    monkeypatch.setattr(rc, "public", lambda job, tail=None: dict(job))
    app = FastAPI()
    app.include_router(router.router)
    api = TestClient(app)
    hdr = {"X-PhotonScript-Token": "t" * 32}
    body = {"args": ["apps", "--start", "--only", "nina2"]}
    r = api.post("/api/cli/run", json=body, headers=hdr)
    assert r.status_code == 403 and "confirm" in r.json()["detail"]
    assert started == []
    # with confirm it runs, mid-night (night_safe)
    r = api.post("/api/cli/run", json={**body, "confirm": "write"}, headers=hdr)
    assert r.status_code == 202, r.text
    assert started[0].cls == "write" and started[0].args == body["args"]


# ------------------------------------------------------------------ refusals

def test_refuses_when_already_running(tmp_path):
    cfg = _cfg(tmp_path)
    w = _world(up=(1888, 1889, 4400, 3040), ninas=2)
    launch = FakeLauncher(w, 1889, FakeProc(exits_after=1))
    with pytest.raises(ast.StartRefused, match="already answers"):
        _start(cfg, "nina2", w, launch)
    assert launch.calls == []


def test_refuses_phd2_running(tmp_path):
    cfg = _cfg(tmp_path)
    w = _world()
    launch = FakeLauncher(w, 4400, FakeProc(exits_after=1))
    with pytest.raises(ast.StartRefused, match="already answers"):
        _start(cfg, "phd2", w, launch)
    assert launch.calls == []


def test_refuses_second_nina_when_one_is_loading(tmp_path):
    # two NINA processes, only :1888 answers: NINA #2 is open (loading, API
    # off): a launch would make a third NINA
    cfg = _cfg(tmp_path)
    w = _world(ninas=2)
    launch = FakeLauncher(w, 1889, FakeProc(exits_after=1))
    with pytest.raises(ast.StartRefused, match="second one"):
        _start(cfg, "nina2", w, launch)
    assert launch.calls == []


def test_refuses_phd2_open_without_port(tmp_path):
    cfg = _cfg(tmp_path)
    w = _world(up=(1888, 3040), phd2=1)
    launch = FakeLauncher(w, 4400, FakeProc(exits_after=1))
    with pytest.raises(ast.StartRefused, match="second one"):
        _start(cfg, "phd2", w, launch)
    assert launch.calls == []


def test_refuses_session_zero(tmp_path):
    cfg = _cfg(tmp_path)
    w = _world()
    launch = FakeLauncher(w, 1889, FakeProc(exits_after=1))
    with pytest.raises(ast.StartRefused, match="session 0"):
        _start(cfg, "nina2", w, launch, sid=0)
    assert launch.calls == []


def test_refuses_thesky_and_nina2_without_piggyback(tmp_path):
    w = _world()
    launch = FakeLauncher(w, 3040, FakeProc(exits_after=1))
    with pytest.raises(ast.StartRefused, match="never launched"):
        _start(_cfg(tmp_path), "thesky", w, launch)
    with pytest.raises(ast.StartRefused, match="not used"):
        _start(_cfg(tmp_path, piggyback_enabled=False), "nina2", w, launch)
    with pytest.raises(ast.StartRefused, match="unknown app"):
        _start(_cfg(tmp_path), "pixinsight", w, launch)
    assert launch.calls == []


def test_refuses_when_process_list_unreadable(tmp_path):
    cfg = _cfg(tmp_path)
    w = _world()
    w["procs_fn"] = lambda: {}
    launch = FakeLauncher(w, 1889, FakeProc(exits_after=1))
    with pytest.raises(ast.StartRefused, match="process list"):
        _start(cfg, "nina2", w, launch)
    assert launch.calls == []


# ------------------------------------------------------------------ launch

def test_script_argv():
    argv = ast.script_argv("nina2", 220)
    assert argv[0].lower().endswith("powershell.exe")
    assert argv[1:6] == ["-NoProfile", "-NonInteractive", "-ExecutionPolicy",
                         "Bypass", "-File"]
    assert Path(argv[6]).name == "observatory-apps.ps1"
    assert Path(argv[6]).parent.name == "deploy"
    assert argv[7:] == ["-Start", "-Only", "nina2", "-StartTimeoutS", "220"]
    assert ast.script_argv("phd2", 60, no_connect=True)[-1] == "-NoConnect"
    # never -Stop, never TheSky
    assert "-Stop" not in argv
    with pytest.raises(ValueError):
        ast.script_argv("thesky", 60)


def test_start_nina2_with_fake_launcher(tmp_path):
    cfg = _cfg(tmp_path)
    w = _world()                              # tonight: 1888 up, 1889 down, 1 NINA
    proc = FakeProc(exits_after=3, code=0)
    launch = FakeLauncher(w, 1889, proc)
    r = _start(cfg, "nina2", w, launch, timeout_s=240)
    assert len(launch.calls) == 1
    argv, out_path = launch.calls[0]
    assert argv[-5:] == ["-Start", "-Only", "nina2", "-StartTimeoutS", "220"]
    assert out_path == Path(cfg.data_dir) / "logs" / ast.OUT_NAME
    assert r.ok and r.answered and r.script_done and r.script_exit == 0
    assert r.version == "3.1.2.9001" and ":1889" in r.message
    assert "launched" in r.output
    assert not proc.killed


def test_start_times_out_without_killing(tmp_path):
    cfg = _cfg(tmp_path)
    w = _world()
    proc = FakeProc(exits_after=None)         # script never ends
    launch = FakeLauncher(w, 1889, proc, up=False)
    r = _start(cfg, "nina2", w, launch, timeout_s=60)
    assert not r.ok and not r.script_done and r.script_exit is None
    assert r.waited_s >= 60 and "still running" in r.message
    assert not proc.killed


def test_start_answers_while_script_connects(tmp_path):
    cfg = _cfg(tmp_path)
    w = _world()
    proc = FakeProc(exits_after=None)         # connect / preflight still going
    r = _start(cfg, "nina2", w, FakeLauncher(w, 1889, proc), timeout_s=60)
    assert r.ok and not r.script_done and "still running" in r.message


def test_nina_port_open_but_api_down_is_not_running(tmp_path):
    # TCP answers on 1889 but /v2/api/version does not: not "already
    # running" by the port alone, but the NINA process count still guards
    cfg = _cfg(tmp_path)
    w = _world(up=(1888, 1889, 4400, 3040), ninas=1)
    w["nina_fn"] = lambda h, p: "3.1" if p == 1888 else ""
    launch = FakeLauncher(w, 1889, FakeProc(exits_after=1))
    with pytest.raises(ast.StartRefused, match="already answers"):
        _start(cfg, "nina2", w, launch)


# ------------------------------------------------------------------ CLI

def test_cli_start_needs_only():
    res = CliRunner().invoke(cli.app, ["apps", "--start"])
    assert res.exit_code == 2 and "--only" in res.output


def test_cli_refusal_exit_2(monkeypatch):
    def refuse(cfg, key, **kw):
        raise ast.StartRefused("NINA #2 (Piggy-600) already answers on :1889")
    monkeypatch.setattr(ast, "start_app", refuse)
    res = CliRunner().invoke(cli.app, ["apps", "--start", "--only", "nina2"])
    assert res.exit_code == 2 and "REFUSED" in res.output


def test_cli_status(monkeypatch):
    monkeypatch.setattr(ast, "snapshot", lambda cfg: {
        "apps": {"nina2": {"label": "NINA #2 (Piggy-600)", "port": 1889,
                           "answering": False, "processes": 1}},
        "session_id": 1})
    res = CliRunner().invoke(cli.app, ["apps", "--status"])
    assert res.exit_code == 0 and "NOT answering" in res.output


def test_script_has_only_guard():
    s = (Path(ast.SCRIPT)).read_text(encoding="utf-8")
    assert '[ValidateSet("", "nina1", "nina2", "phd2")]' in s
    assert "-Only {0} refused" in s and "-NoConnect" in s
