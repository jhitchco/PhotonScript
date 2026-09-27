"""PS-57: GET /api/health and `photonscript status` slow-vs-down."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from typer.testing import CliRunner

from photonscript.shared import health
from photonscript.shared.config import PhotonScriptConfig


@pytest.fixture
def app_env(tmp_path, monkeypatch):
    from photonscript.scheduler import app as appmod
    monkeypatch.setattr(appmod, "_config", PhotonScriptConfig(data_dir=tmp_path))
    monkeypatch.setattr(appmod, "get_armer", lambda: SimpleNamespace(state="ARMED"))
    monkeypatch.setattr(health, "_MONITOR", None)
    return appmod


async def test_health_fields(app_env):
    from photonscript.scheduler.routers.health import api_health
    health.mark_started("full")
    d = await api_health()
    assert d["ok"] is True
    assert d["version"] == app_env.VERSION
    assert len(d["commit"]) == 40            # full SHA of this checkout
    assert d["mode"] == "full" and d["uptime_s"] >= 0 and d["pid"] > 0
    assert d["armer"] == "ARMED"
    assert d["piggyback_enabled"] in (True, False)
    assert d["loop"] is None                  # no monitor running in tests
    assert "process" in d and "iers" in d


async def test_health_reports_loop_lag(app_env, monkeypatch):
    from photonscript.scheduler.routers.health import api_health
    mon = health.LoopMonitor()
    mon.record(mon.clock(), 1.234)
    monkeypatch.setattr(health, "_MONITOR", mon)
    d = await api_health()
    assert d["loop"]["lag_ms"] == 1234.0
    assert d["loop"]["max_lag_ms_5min"] == 1234.0


async def test_health_route_is_mounted(app_env):
    transport = httpx.ASGITransport(app=app_env.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/health")
    assert r.status_code == 200 and r.json()["ok"] is True


# --- CLI ---------------------------------------------------------------------

def _resp(code, payload=None):
    return httpx.Response(code, json=payload or {},
                          request=httpx.Request("GET", "http://x/api/health"))


@pytest.mark.parametrize("exc,state", [
    (httpx.ConnectError("refused"), "refused"),
    (httpx.ReadTimeout("slow"), "timeout"),
    (httpx.ConnectTimeout("slow"), "timeout"),
    (RuntimeError("boom"), "error"),
])
def test_probe_failures(monkeypatch, exc, state):
    from photonscript import cli

    def fake_get(*a, **k):
        raise exc
    monkeypatch.setattr(httpx, "get", fake_get)
    assert cli._probe_health("http://x", timeout=1)["state"] == state


def test_probe_up_slow_and_old(monkeypatch):
    from photonscript import cli
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp(200, {"ok": True}))
    h = cli._probe_health("http://x")
    assert h["state"] == "up" and h["data"]["ok"] is True
    assert cli._probe_health("http://x", slow_s=-1)["state"] == "slow"
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp(404))
    assert cli._probe_health("http://x")["state"] == "old"
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _resp(500))
    assert cli._probe_health("http://x")["state"] == "error"


@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    monkeypatch.setenv("PS_DATA_DIR", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    from photonscript import cli
    return cli, CliRunner()


def test_status_says_stalled_on_timeout(cli_env, monkeypatch):
    cli, runner = cli_env
    calls = []

    def fake_get(url, **k):
        calls.append(url)
        raise httpx.ReadTimeout("no answer")
    monkeypatch.setattr(httpx, "get", fake_get)
    r = runner.invoke(cli.app, ["status", "--timeout", "3"])
    assert r.exit_code == 0
    assert "no answer within 3 s" in r.output and "stalls.log" in r.output
    assert len(calls) == 1          # does not wait on the other endpoints too


def test_status_shows_latency_lag_and_service(cli_env, monkeypatch):
    cli, runner = cli_env
    payload = {"ok": True, "pid": 4242, "mode": "full", "uptime_s": 3700,
               "armer": "ARMED", "version": "abc1234 (Sep 27 10:00)",
               "loop": {"lag_ms": 3.0, "max_lag_ms_5min": 40.0, "stalls_5min": 0},
               "process": {"session_id": 1, "priority_class": "normal",
                           "launcher": "task-Interactive"}}

    def fake_get(url, **k):
        if url.endswith("/api/health"):
            return _resp(200, payload)
        if url.endswith("/api/update/check"):
            return _resp(200, {"running": "abc1234 (Sep 27 10:00)", "behind": 0})
        return _resp(200, {"telescope": {"session_state": "idle"}})
    monkeypatch.setattr(httpx, "get", fake_get)
    r = runner.invoke(cli.app, ["status"])
    assert r.exit_code == 0, r.output
    out = " ".join(r.output.split())
    assert "up, answered in" in out and "loop lag 3 ms" in out
    assert "pid 4242" in out and "session 1" in out and "task-Interactive" in out
    assert "Armer: ARMED" in out and "IDLE" in out
