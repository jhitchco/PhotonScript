"""Remote CLI (scheduler/remote_cli.py, routers/remote_cli.py, cli remote)."""
from __future__ import annotations

import io
import json
import sys
import time

import httpx
import pytest

from photonscript.scheduler import remote_cli as rc
from photonscript.shared.config import PhotonScriptConfig

TOKEN = "t" * 32

# never on the list: hardware, arming, sequences, service, deploy, notify,
# device connections, PixInsight, file moves / deletes
NEVER = ["start", "stop", "restart", "supervise", "self-update", "rollback-done",
         "notify", "preflight", "sequence", "phd2-selftest", "focus-move",
         "focus-ingest", "cooler-gate", "cover-gate", "settle-gate", "tpoint-sample",
         "calibration-capture", "guide-recover", "dawn-file",
         "regrade", "pointing-backfill", "slew-backfill", "rename-targets",
         "archive-library", "prune-nights", "autostart-check", "calibration-file",
         "integrate", "blend", "finish", "restage", "autointegrate", "integrate-watch",
         "ledger-import", "qa-focuser-zero", "monitor", "remote",
         "remote-screen"]


def _cfg(tmp_path, token=TOKEN):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"),
                              remote_cli_token=token)


def _wait(runner, jid, t=60):
    t0 = time.monotonic()
    while runner.view(jid)["status"] == "running":
        assert time.monotonic() - t0 < t, "job never finished"
        time.sleep(0.1)
    return runner.view(jid, None)


def _py(code):
    """argv_for: run a python snippet instead of the photonscript CLI."""
    return lambda args: [sys.executable, "-c", code, *args]


# tpoint-autocal is listed as a write command (--go only): Jeremy approved
# remote, supervised TPoint autocal runs on 2026-10-18.


def test_tpoint_autocal_go_is_write_gated():
    allow = rc.load_allowlist()
    assert allow.commands["tpoint-autocal"].write_flags == ("--go",)


# tpoint-refit is listed READ ONLY: the dry run (no flag) and --discover
# (one TheSky script that only enumerates TheSkyXAction member names). The
# live refit flags are not on the list at all, not even as write flags.

def test_tpoint_refit_only_read_flags():
    allow = rc.load_allowlist()
    r = allow.commands["tpoint-refit"]
    assert r.cls == "read"
    assert set(r.flags) == {"--discover", "--json"}
    assert r.write_flags == () and r.require == () and not r.night_safe
    for live in ("--go", "--fit", "--action", "--test"):
        assert live not in r.flags and live not in r.write_flags
    assert rc.validate(["tpoint-refit"], allow).cls == "read"
    assert rc.validate(["tpoint-refit", "--discover"], allow).cls == "read"
    assert rc.validate(["tpoint-refit", "--discover", "--json"], allow).cls == "read"
    assert rc.validate(["tpoint-refit", "--json"], allow).cls == "read"


@pytest.mark.parametrize("args", [
    ["tpoint-refit", "--go"],
    ["tpoint-refit", "--go", "--test"],
    ["tpoint-refit", "--fit", "action"],
    ["tpoint-refit", "--fit=console"],
    ["tpoint-refit", "--action", "TPointModule"],
    ["tpoint-refit", "--test"],
    ["tpoint-refit", "--discover", "--go"],
    ["tpoint-refit", "--url", "http://x"],
])
def test_tpoint_refit_live_flags_refused(args):
    with pytest.raises(rc.Refused) as e:
        rc.validate(args, rc.load_allowlist())
    assert e.value.status == 403
    assert "not allowed remotely" in e.value.detail


def test_tpoint_refit_discover_script_calls_nothing():
    from photonscript.telescope_agent import tpoint_refit as tr
    js = tr.discover_script()
    assert "for (var k in TheSkyXAction)" in js
    assert "typeof TheSkyXAction.execute" in js
    assert "execute(" not in js


def test_screen_snapshot_listed_read_only_without_out():
    allow = rc.load_allowlist()
    r = allow.commands["screen-snapshot"]
    assert r.cls == "read" and r.write_flags == ()
    assert set(r.flags) == {"--scale", "--monitor", "--quality", "--json"}
    assert rc.validate(["screen-snapshot", "--scale", "0.5", "--json"], allow).cls == "read"
    with pytest.raises(rc.Refused) as e:
        rc.validate(["screen-snapshot", "--out", "C:/x.jpg"], allow)
    assert e.value.status == 403


# thesky-audit is listed for --discover-docprops ONLY (2026-10-28): a read
# only DocumentProperty scan. The audit itself and the Image Link flags stay
# local.

def test_thesky_audit_only_discovery():
    allow = rc.load_allowlist()
    r = allow.commands["thesky-audit"]
    assert r.cls == "read" and r.write_flags == () and not r.night_safe
    assert set(r.flags) == {"--discover-docprops", "--json"}
    assert r.require == ("--discover-docprops",)
    assert rc.validate(["thesky-audit", "--discover-docprops"], allow).cls == "read"
    assert rc.validate(["thesky-audit", "--discover-docprops", "--json"], allow).cls == "read"
    assert rc.validate(["thesky-audit", "--help"], allow).cls == "read"
    for args in (["thesky-audit"], ["thesky-audit", "--json"],
                 ["thesky-audit", "--discover-docprops", "--imagelink"],
                 ["thesky-audit", "--thesky-imagelink"],
                 ["thesky-audit", "--discover-docprops", "--url", "http://x"]):
        with pytest.raises(rc.Refused) as e:
            rc.validate(args, allow)
        assert e.value.status == 403, args


# ------------------------------------------------------------ allowlist

def test_allowlist_matches_the_cli():
    allow = rc.load_allowlist()
    assert len(allow.commands) >= 20
    assert rc.check_allowlist(allow) == []


@pytest.mark.parametrize("name", NEVER)
def test_hardware_and_service_commands_are_not_listed_and_refused(name):
    allow = rc.load_allowlist()
    assert name not in allow.commands
    with pytest.raises(rc.Refused) as e:
        rc.validate([name], allow)
    assert e.value.status == 403
    assert "allowlist" in e.value.detail


def test_no_url_or_path_flags_anywhere():
    allow = rc.load_allowlist()
    banned = {"--url", "--nina-url", "--library", "--measure", "--records", "--mirror",
              "--desktop-library", "--out", "--file", "--force", "--save", "--kill",
              "--thesky-imagelink", "--imagelink", "--nina-events"}
    for r in allow.commands.values():
        assert not banned & set(r.flags), (r.name, banned & set(r.flags))
    # dry-run-only commands keep --apply off
    for name in ("qa-backfill", "qa-rescore"):
        assert "--apply" not in allow.commands[name].flags


def test_validate_allowed_and_classes():
    allow = rc.load_allowlist()
    p = rc.validate(["frame-check", "--last", "3", "--type", "SNAPSHOT", "--compare"], allow)
    assert (p.command, p.cls) == ("frame-check", "read")
    assert rc.validate(["unguided-limits"], allow).cls == "read"
    assert rc.validate(["unguided-limits", "--refresh"], allow).cls == "write"
    assert rc.validate(["guide-rms-backfill", "--date", "2026-10-12"], allow).cls == "read"
    assert rc.validate(["guide-rms-backfill", "--apply"], allow).cls == "write"
    assert rc.validate(["settings-backfill", "--apply"], allow).cls == "write"
    p = rc.validate(["settings-snapshot", "--no-push"], allow)
    assert p.cls == "write" and p.night_safe
    assert rc.validate(["frame-check", "--last=4"], allow).cls == "read"
    assert rc.validate(["tracking-test-report", "--pa", "-12.5"], allow).cls == "read"
    assert rc.validate(["settings", "--diff", "last_good", "latest"], allow).cls == "read"


def test_help_of_any_command_is_read():
    allow = rc.load_allowlist()
    assert rc.validate(["--help"], allow).cls == "read"
    assert rc.validate(["self-update", "--help"], allow).cls == "read"
    with pytest.raises(rc.Refused) as e:
        rc.validate(["self-update", "--dry-run", "--help"], allow)
    assert e.value.status == 400
    with pytest.raises(rc.Refused) as e:
        rc.validate(["nosuch", "--help"], allow)
    assert e.value.status == 400


@pytest.mark.parametrize("args,status,word", [
    (["frame-check", "--bogus"], 400, "unknown flag"),
    (["frame-check", "--url", "http://x"], 403, "not allowed remotely"),
    (["frame-check", "--nina-url=http://x"], 403, "not allowed remotely"),
    (["frame-check", "--last", "abc"], 400, "valid int"),
    (["frame-check", "--last"], 400, "requires an argument"),
    (["frame-check", "extra"], 400, "unexpected extra"),
    (["frame-check", "--", "--url"], 400, "bare --"),
    (["settings-snapshot"], 403, "--no-push"),
    (["qa-backfill", "--date", "2026-10-01", "--apply"], 403, "not allowed remotely"),
    (["nosuch"], 403, "allowlist"),
    (["--version"], 400, "first arg"),
    ([], 400, "non-empty"),
    ("frame-check --last 3", 400, "list"),
    (["frame-check", 3], 400, "string"),
    (["frame-check", "--type", "a\nb"], 400, "control"),
])
def test_validate_refusals(args, status, word):
    with pytest.raises(rc.Refused) as e:
        rc.validate(args, rc.load_allowlist())
    assert e.value.status == status
    assert word in e.value.detail


def test_broken_allowlist_allows_nothing(tmp_path):
    p = tmp_path / "x.toml"
    p.write_text("[commands.frame-check\n", encoding="utf-8")
    allow = rc.load_allowlist(p)
    assert allow.commands == {}
    with pytest.raises(rc.Refused):
        rc.validate(["frame-check"], allow)


def test_write_blockers():
    allow = rc.load_allowlist()
    w = rc.validate(["unguided-limits", "--refresh"], allow)
    assert rc.write_blockers(w, "IDLE", []) == []
    for st in ("RUNNING", "WATCHING", "PAUSED_UNSAFE", "PAUSED_OPERATOR"):
        assert rc.write_blockers(w, st, [])
    assert rc.write_blockers(w, "IDLE", ["grading 2026-10-13"])
    safe = rc.validate(["settings-snapshot", "--no-push"], allow)
    assert rc.write_blockers(safe, "RUNNING", ["grading x"]) == []
    read = rc.validate(["frame-check"], allow)
    assert rc.write_blockers(read, "RUNNING", ["grading x"]) == []


def test_token_rules():
    assert rc.token_ok("", "x")[:2] == (False, 403)
    assert rc.token_ok("short", "short")[:2] == (False, 403)
    assert rc.token_ok(TOKEN, "")[:2] == (False, 401)
    assert rc.token_ok(TOKEN, "u" * 32)[:2] == (False, 401)
    assert rc.token_ok(TOKEN, TOKEN)[0] is True


def test_child_env_drops_the_token(monkeypatch):
    monkeypatch.setenv("PS_REMOTE_CLI_TOKEN", TOKEN)
    monkeypatch.setenv("PS_MONITOR_URL", "http://x")
    env = rc.child_env()
    assert "PS_REMOTE_CLI_TOKEN" not in env and "PS_MONITOR_URL" not in env
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_token_is_not_a_dashboard_config_field():
    from photonscript.scheduler import app as app_mod
    assert "PS_REMOTE_CLI_TOKEN" not in {f[1] for f in app_mod._CONFIG_FIELDS}


# ------------------------------------------------------------ runner

def _plan(args=("frame-check",), cls="read"):
    return rc.Plan(list(args), args[0], cls, False)


def test_runner_runs_and_audits(tmp_path):
    cfg = _cfg(tmp_path)
    r = rc.Runner(argv_for=_py("import sys; print('hello', sys.argv[1:]); "
                               "sys.stderr.write('warn'); sys.exit(3)"))
    j = r.start(cfg, _plan(("frame-check", "--last", "3")), 30, rc.load_allowlist(),
                {"ip": "100.1.2.3"})
    v = _wait(r, j["id"])
    assert v["status"] == "failed" and v["exit_code"] == 3
    assert "hello ['frame-check', '--last', '3']" in v["stdout"]
    assert v["stderr"] == "warn"
    a = rc.read_audit(cfg)
    assert a[0]["event"] == "run" and a[0]["ip"] == "100.1.2.3"
    assert a[0]["args"] == ["frame-check", "--last", "3"]
    assert a[0]["exit"] == 3 and a[0]["class"] == "read"
    assert a[0]["duration_s"] >= 0


def test_runner_one_job_per_class(tmp_path):
    cfg = _cfg(tmp_path)
    r = rc.Runner(argv_for=_py("import time; time.sleep(2)"))
    allow = rc.load_allowlist()
    j = r.start(cfg, _plan(), 30, allow, {})
    with pytest.raises(rc.Refused) as e:
        r.start(cfg, _plan(), 30, allow, {})
    assert e.value.status == 409
    j2 = r.start(cfg, _plan(("unguided-limits", "--refresh"), "write"), 30, allow, {})
    _wait(r, j["id"])
    _wait(r, j2["id"])
    r.start(cfg, _plan(), 30, allow, {})   # free again


def test_runner_timeout_kills_the_process_tree(tmp_path):
    import psutil
    cfg = _cfg(tmp_path)
    code = ("import subprocess, sys, time\n"
            "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "print(c.pid, flush=True)\n"
            "time.sleep(120)\n")
    r = rc.Runner(argv_for=_py(code))
    j = r.start(cfg, _plan(), 2, rc.load_allowlist(), {})
    v = _wait(r, j["id"], 60)
    assert v["status"] == "timeout"
    assert "hard timeout" in v["stderr"]
    child = int(v["stdout"].split()[0])
    t0 = time.monotonic()
    while psutil.pid_exists(child) and time.monotonic() - t0 < 10:
        try:
            if psutil.Process(child).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.2)
    alive = False
    try:
        alive = psutil.Process(child).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        pass
    assert not alive
    assert rc.read_audit(cfg)[0]["status"] == "timeout"


def test_output_is_capped(tmp_path):
    cfg = _cfg(tmp_path)
    allow = rc.load_allowlist()
    allow.output_cap_bytes = 4096
    r = rc.Runner(argv_for=_py("import sys; sys.stdout.write('x' * 1000000 + 'END')"))
    j = r.start(cfg, _plan(), 30, allow, {})
    v = _wait(r, j["id"])
    assert v["status"] == "done"
    assert v["stdout_bytes"] == 1000003
    assert len(v["stdout"]) == 4096 and v["stdout"].endswith("END")
    assert v["truncated"] is True
    assert r.view(j["id"], 10)["stdout"] == "x" * 7 + "END"


def test_real_cli_help_runs(tmp_path):
    cfg = _cfg(tmp_path)
    r = rc.Runner()
    j = r.start(cfg, rc.validate(["frame-check", "--help"], rc.load_allowlist()), 120,
                rc.load_allowlist(), {})
    v = _wait(r, j["id"], 120)
    assert v["exit_code"] == 0 and "--compare" in v["stdout"]


# ------------------------------------------------------------ endpoints

@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler.routers import remote_cli as router
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
    runner = rc.Runner(argv_for=_py("import sys; print('ran', *sys.argv[1:])"))
    monkeypatch.setattr(rc, "RUNNER", runner)
    state = {"armer": "IDLE", "jobs": []}
    monkeypatch.setattr(router, "_armer_state", lambda: state["armer"])
    monkeypatch.setattr(router, "_grading_jobs", lambda: state["jobs"])
    c = TestClient(app_mod.app)
    c.cfg, c.runner, c.state = cfg, runner, state
    return c


H = {"X-PhotonScript-Token": TOKEN}


def test_endpoint_token_disabled_and_wrong(api):
    api.cfg.remote_cli_token = ""
    r = api.post("/api/cli/run", json={"args": ["frame-check"]}, headers=H)
    assert r.status_code == 403 and "disabled" in r.json()["detail"]
    api.cfg.remote_cli_token = TOKEN
    r = api.post("/api/cli/run", json={"args": ["frame-check"]})
    assert r.status_code == 401
    r = api.post("/api/cli/run", json={"args": ["frame-check"]},
                 headers={"X-PhotonScript-Token": "w" * 32})
    assert r.status_code == 401
    assert api.get("/api/cli/jobs/abc").status_code == 401
    ev = rc.read_audit(api.cfg)
    assert [e["event"] for e in ev] == ["refused"] * 3
    assert TOKEN not in json.dumps(ev)


def test_endpoint_run_and_poll(api):
    r = api.post("/api/cli/run", json={"args": ["frame-check", "--last", "3"],
                                       "timeout_s": 60}, headers=H)
    assert r.status_code == 202, r.text
    jid = r.json()["id"]
    assert r.json()["class"] == "read"
    v = _wait(api.runner, jid)
    assert v["exit_code"] == 0
    d = api.get(f"/api/cli/jobs/{jid}", headers=H).json()
    assert d["stdout"].strip() == "ran frame-check --last 3"
    lst = api.get("/api/cli/jobs").json()
    assert lst["enabled"] is True
    assert lst["audit"][0]["id"] == jid and "stdout" not in lst["audit"][0]
    assert api.get("/api/cli/jobs/nope", headers=H).status_code == 404


def test_endpoint_refuses_hardware_and_unknown(api):
    for args, code in ((["focus-move", "L"], 403), (["stop"], 403),
                       (["frame-check", "--bogus"], 400)):
        r = api.post("/api/cli/run", json={"args": args}, headers=H)
        assert r.status_code == code, (args, r.text)
    assert api.runner.jobs == {}
    assert rc.read_audit(api.cfg)[0]["event"] == "refused"


def test_endpoint_write_gating(api):
    body = {"args": ["unguided-limits", "--refresh"]}
    r = api.post("/api/cli/run", json=body, headers=H)
    assert r.status_code == 403 and "confirm" in r.json()["detail"]
    api.state["armer"] = "RUNNING"
    r = api.post("/api/cli/run", json={**body, "confirm": "write"}, headers=H)
    assert r.status_code == 409 and "RUNNING" in r.json()["detail"]
    # night_safe write runs mid-night
    r = api.post("/api/cli/run", json={"args": ["settings-snapshot", "--no-push"],
                                       "confirm": "write"}, headers=H)
    assert r.status_code == 202
    _wait(api.runner, r.json()["id"])
    api.state["armer"] = "IDLE"
    api.state["jobs"] = ["grading 2026-10-13"]
    r = api.post("/api/cli/run", json={**body, "confirm": "write"}, headers=H)
    assert r.status_code == 409 and "grading" in r.json()["detail"]
    api.state["jobs"] = []
    r = api.post("/api/cli/run", json={**body, "confirm": "write"}, headers=H)
    assert r.status_code == 202 and r.json()["class"] == "write"
    _wait(api.runner, r.json()["id"])


def test_endpoint_timeout_is_clamped(api):
    r = api.post("/api/cli/run", json={"args": ["frame-check"], "timeout_s": 99999},
                 headers=H)
    assert r.json()["timeout_s"] == rc.load_allowlist().max_timeout_s
    _wait(api.runner, r.json()["id"])
    r = api.post("/api/cli/run", json={"args": ["frame-check"], "timeout_s": "x"},
                 headers=H)
    assert r.status_code == 400


# ------------------------------------------------------------ desktop client

def _fake_server(final, status=202, detail="nope"):
    seen = {"posts": [], "gets": 0}

    def handler(req: httpx.Request):
        seen["token"] = req.headers.get("X-PhotonScript-Token")
        if req.method == "POST":
            seen["posts"].append(json.loads(req.content))
            if status != 202:
                return httpx.Response(status, json={"detail": detail})
            return httpx.Response(202, json={"id": "j1", "class": "read",
                                             "status": "running"})
        seen["gets"] += 1
        if seen["gets"] < 2:
            return httpx.Response(200, json={"id": "j1", "status": "running"})
        return httpx.Response(200, json={"id": "j1", **final})
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_client_prints_output_and_exits_with_the_remote_code():
    from photonscript.cli import remote_run
    cl, seen = _fake_server({"status": "failed", "exit_code": 1, "stdout": "table\n",
                             "stderr": "warn\n"})
    out, err = io.StringIO(), io.StringIO()
    code = remote_run("http://scope:8100/", TOKEN, ["frame-check", "--last", "3"],
                      confirm_write=True, timeout_s=60, poll_s=0, client=cl,
                      out=out, err=err)
    assert code == 1
    assert out.getvalue() == "table\n" and "warn" in err.getvalue()
    assert seen["posts"][0] == {"args": ["frame-check", "--last", "3"], "timeout_s": 60,
                                "confirm": "write"}
    assert seen["token"] == TOKEN
    assert TOKEN not in out.getvalue() + err.getvalue()


def test_client_refused_timeout_and_no_wait():
    from photonscript.cli import remote_run
    cl, _ = _fake_server({}, status=403, detail="focus-move: not on the remote CLI allowlist")
    err = io.StringIO()
    assert remote_run("http://s", TOKEN, ["focus-move"], poll_s=0, client=cl,
                      out=io.StringIO(), err=err) == 2
    assert "403" in err.getvalue() and "allowlist" in err.getvalue()
    cl, _ = _fake_server({"status": "timeout", "exit_code": None, "stdout": ""})
    assert remote_run("http://s", TOKEN, ["frame-check"], poll_s=0, client=cl,
                      out=io.StringIO(), err=io.StringIO()) == 124
    cl, seen = _fake_server({"status": "done", "exit_code": 0})
    out = io.StringIO()
    assert remote_run("http://s", TOKEN, ["frame-check"], wait=False, client=cl,
                      out=out, err=io.StringIO()) == 0
    assert "j1" in out.getvalue() and seen["gets"] == 0


def test_client_unreachable():
    from photonscript.cli import remote_run

    def boom(req):
        raise httpx.ConnectError("down")
    cl = httpx.Client(transport=httpx.MockTransport(boom))
    err = io.StringIO()
    assert remote_run("http://s", TOKEN, ["frame-check"], client=cl,
                      out=io.StringIO(), err=err) == 2
    assert "unreachable" in err.getvalue()


def test_cli_remote_command_passes_args_through(monkeypatch, tmp_path):
    from typer.testing import CliRunner
    from photonscript import cli
    monkeypatch.setattr(cli, "_config_for_repo", lambda *_a, **_k: _cfg(tmp_path))
    got = {}

    def fake(url, token, args, **kw):
        got.update(url=url, token=token, args=args, **kw)
        return 5
    monkeypatch.setattr(cli, "remote_run", fake)
    res = CliRunner().invoke(cli.app, ["remote", "--url", "http://s:8100", "--confirm-write",
                                       "--", "unguided-limits", "--refresh", "--json"])
    assert res.exit_code == 5, res.output
    assert got["args"] == ["unguided-limits", "--refresh", "--json"]
    assert got["url"] == "http://s:8100" and got["confirm_write"] is True
    assert got["token"] == TOKEN and TOKEN not in res.output
    res = CliRunner().invoke(cli.app, ["remote", "frame-check", "--last", "3", "--compare"])
    assert got["args"] == ["frame-check", "--last", "3", "--compare"]
    assert got["confirm_write"] is False


def test_cli_remote_needs_a_token(monkeypatch, tmp_path):
    from typer.testing import CliRunner
    from photonscript import cli
    monkeypatch.delenv("PS_REMOTE_CLI_TOKEN", raising=False)
    monkeypatch.setattr(cli, "_config_for_repo", lambda *_a, **_k: _cfg(tmp_path, ""))
    res = CliRunner().invoke(cli.app, ["remote", "--", "frame-check"])
    assert res.exit_code == 2 and "PS_REMOTE_CLI_TOKEN" in res.output


def test_system_page_has_the_remote_commands_panel(api):
    r = api.get("/system")
    assert r.status_code == 200
    assert 'id="remoteCliPanel"' in r.text and "/api/cli/jobs" in r.text
