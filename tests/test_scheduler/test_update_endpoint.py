"""PS-58: POST /api/update refuses while a night or a grading job is active
and exits 42 through the orchestrator's graceful stop."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from photonscript import orchestrator
from photonscript.shared.config import PhotonScriptConfig


@pytest.fixture
def appmod(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    import photonscript.scheduler.runs as runs
    monkeypatch.setattr(app, "_config", PhotonScriptConfig(data_dir=tmp_path))
    monkeypatch.setattr(runs, "_backfill_state", {})
    monkeypatch.setattr(runs, "_regrade_all", {"running": False})
    exits = []
    monkeypatch.setattr(orchestrator, "request_exit",
                        lambda code: exits.append(code) or True)
    timers = []

    class FakeTimer:
        def __init__(self, s, fn):
            timers.append(s)
            self.daemon = False

        def start(self):
            pass

    import threading
    monkeypatch.setattr(threading, "Timer", FakeTimer)
    app._t_exits, app._t_timers = exits, timers
    return app


def _armer(app, monkeypatch, state):
    monkeypatch.setattr(app, "get_armer", lambda: SimpleNamespace(state=state))


async def _post(app, q=""):
    transport = httpx.ASGITransport(app=app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.post("/api/update" + q)


@pytest.mark.parametrize("state", ["RUNNING", "PAUSED_UNSAFE"])
async def test_refused_mid_night_even_with_allow_armed(appmod, monkeypatch, state):
    _armer(appmod, monkeypatch, state)
    r = await _post(appmod, "?allow_armed=true")
    assert r.status_code == 409 and state in r.json()["detail"]
    await asyncio.sleep(0.6)
    assert appmod._t_exits == []


async def test_armed_needs_allow_armed(appmod, monkeypatch):
    _armer(appmod, monkeypatch, "ARMED")
    r = await _post(appmod)
    assert r.status_code == 409 and "allow_armed" in r.json()["detail"]
    r = await _post(appmod, "?allow_armed=true")
    assert r.status_code == 200
    await asyncio.sleep(0.7)
    assert appmod._t_exits == [42]


async def test_refused_while_grading(appmod, monkeypatch):
    import photonscript.scheduler.runs as runs
    _armer(appmod, monkeypatch, "DISARMED")
    runs._backfill_state["2026-09-26"] = {"running": True}
    r = await _post(appmod)
    assert r.status_code == 409 and "grading 2026-09-26" in r.json()["detail"]
    runs._backfill_state["2026-09-26"]["running"] = False
    runs._regrade_all.update(running=True, done=3, total=9)
    r = await _post(appmod)
    assert r.status_code == 409 and "re-grade all (3 of 9" in r.json()["detail"]


async def test_graceful_exit_with_hard_fallback(appmod, monkeypatch):
    _armer(appmod, monkeypatch, "DISARMED")
    r = await _post(appmod)
    assert r.status_code == 200 and r.json()["ok"] is True
    assert appmod._t_exits == []              # the response goes out first
    await asyncio.sleep(0.7)
    assert appmod._t_exits == [42]
    assert appmod._t_timers == [appmod._UPDATE_HARD_EXIT_S] == [15.0]


# --- orchestrator stop path ---------------------------------------------------------

async def test_request_exit_sets_the_stop_event(tmp_path, monkeypatch):
    orch = orchestrator
    monkeypatch.setattr(orch, "_exit_code", None)
    assert orch.request_exit(42) is False     # no loop running yet
    cfg = PhotonScriptConfig(data_dir=tmp_path)
    ran = []

    async def agent():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            ran.append("cancelled")
            raise

    async def trigger():
        await asyncio.sleep(0.2)
        assert orch.request_exit(42) is True

    t = asyncio.create_task(trigger())
    await asyncio.wait_for(orch._run_with_shutdown(cfg, [], [agent()]), 10)
    await t
    assert ran == ["cancelled"]
    assert orch.requested_exit_code() == 42
    assert orch._stop_ctx["loop"] is None     # cleared after the run
    monkeypatch.setattr(orch, "_exit_code", None)


def test_cli_start_exits_with_requested_code(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    import photonscript.orchestrator as orch
    monkeypatch.setenv("PS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(orch, "start", lambda mode, config: None)
    monkeypatch.setattr(orch, "_exit_code", 42)
    r = CliRunner().invoke(cli.app, ["start"])
    assert r.exit_code == 42, r.output
    monkeypatch.setattr(orch, "_exit_code", None)
    r = CliRunner().invoke(cli.app, ["start"])
    assert r.exit_code == 0, r.output


# --- health exposes the update state -------------------------------------------------

async def test_health_reports_update_state(appmod, monkeypatch, tmp_path):
    from photonscript.scheduler.routers.health import api_health
    from photonscript.shared import updater
    _armer(appmod, monkeypatch, "DISARMED")
    d = await api_health()
    assert d["update"] is None
    updater.save_state(appmod._config, {"status": "rolled_back",
                                        "bad_sha": "b" * 40,
                                        "target": "a" * 40, "reason": "x"})
    d = await api_health()
    assert d["update"]["status"] == "rolled_back"
    assert d["update"]["bad_sha"] == "b" * 40
