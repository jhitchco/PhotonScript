"""PS-174: NINA #1's log grew ~330 MB/day (20261005-081746 log: 836 MB in
2.5 days). PhotonScript's share of the ninaAPI traffic is cut (dashboard
status reads shared through a short cache, a hidden tab stops polling, the
agent reads /sequence/json every 15 s instead of 5 s), the morning report
watches the log sizes, and /api/nina/log/top counts what fills a log from
its tail only."""
import asyncio
import os
from datetime import datetime
from pathlib import Path

import pytest

from photonscript.scheduler import app
from photonscript.scheduler import nina_log_watch as nlw
from photonscript.scheduler.routers import triage
from photonscript.shared import ttl_cache
from photonscript.shared.config import PhotonScriptConfig


# --- ttl_cache ---------------------------------------------------------------

def test_ttl_cache_single_flight_and_expiry(monkeypatch):
    calls = {"n": 0}

    async def compute():
        calls["n"] += 1
        await asyncio.sleep(0.05)
        return calls["n"]

    async def main():
        a = await asyncio.gather(*[ttl_cache.cached("k", 10, compute)
                                   for _ in range(8)])
        assert a == [1] * 8 and calls["n"] == 1        # 8 tabs, one read
        assert await ttl_cache.cached("k", 10, compute) == 1
        assert await ttl_cache.cached("k", 10, compute, fresh=True) == 2
        ttl_cache.invalidate("k")
        assert await ttl_cache.cached("k", 10, compute) == 3
        assert await ttl_cache.cached("k", 0, compute) == 4   # expired

    asyncio.run(main())


def test_ttl_cache_does_not_cache_errors():
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise RuntimeError("nina down")

    async def main():
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await ttl_cache.cached("e", 10, boom)
    asyncio.run(main())
    assert calls["n"] == 2


def test_scope_and_rigs_share_one_nina_read(monkeypatch):
    seen = {"scope": 0, "rigs": 0}

    async def scope():
        seen["scope"] += 1
        return {"ok": True}

    async def rigs():
        seen["rigs"] += 1
        return {"rigs": []}

    monkeypatch.setattr(app, "_api_scope", scope)
    monkeypatch.setattr(app, "_api_rigs", rigs)

    async def main():
        for _ in range(5):
            await app.api_scope()
            await app.api_rigs()
        await app.api_rigs(fresh=True)

    asyncio.run(main())
    assert seen == {"scope": 1, "rigs": 2}


def test_dashboard_polls_pause_when_hidden_and_reuse_scope():
    html = (Path(app.__file__).parent / "templates" / "dashboard.html").read_text(
        encoding="utf-8")
    assert "setInterval(whenShown(loadScope)" in html
    assert "setInterval(whenShown(loadStrip)" in html
    assert "if (document.hidden) return;   // PS-174: no NINA reads" in html
    assert "fresh ? window._scopeLast" in html


# --- agent /sequence/json cadence ------------------------------------------------

def test_agent_reads_sequence_json_every_third_cycle(monkeypatch):
    from photonscript.shared.models import TelescopeState
    from photonscript.telescope_agent import agent as agent_mod

    calls = {"seq": 0, "cam": 0}

    class Nina:
        async def get_camera_info(self):
            calls["cam"] += 1
            return {"Temperature": 0.0, "CoolerOn": True}

        async def get_mount_info(self):
            return {"RightAscension": 1.0, "Declination": 2.0, "Tracking": True}

        async def get_filter_wheel_info(self):
            return {}

        async def get_focuser_info(self):
            return {"Position": 1}

        async def get_sequence_status(self):
            calls["seq"] += 1
            return {"State": "RUNNING", "CurrentTarget": {"Name": "M33"}}

        async def set_dew_heater(self, power):
            pass

    a = agent_mod.TelescopeAgent.__new__(agent_mod.TelescopeAgent)
    a.config = PhotonScriptConfig(_env_file=None, camera_setpoint_c=0.0,
                                  mount_log_enabled=False)
    a.nina = Nina()
    a.state = TelescopeState()
    a._alerted = set()
    a._cool_bad_since = None
    a._cool_fix_attempts = 0
    a._dew_last_set = 0.0
    a._dew_api_broken = False
    a._armer_state = lambda: None
    a._running = True
    left = {"n": 9}

    async def tick(_):
        left["n"] -= 1
        if left["n"] <= 0:
            a._running = False

    monkeypatch.setattr(agent_mod.asyncio, "sleep", tick)
    asyncio.run(a._nina_poll_loop())
    assert calls["cam"] == 9                 # equipment every 5 s cycle
    assert calls["seq"] == 3                 # sequence tree every 3rd
    assert a.state.current_target == "M33"


# --- log size watch ----------------------------------------------------------------

def _log(d: Path, name: str, port: int, mb: float, mtime: datetime) -> Path:
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    with open(p, "wb") as fh:
        fh.write(f"2026-10-05T08:17:46|INFO|API.cs|Start|1|starting web server, "
                 f"listening at 0.0.0.0:{port}\n".encode())
        fh.truncate(int(mb * 1e6))           # sparse: no real 800 MB write
    t = mtime.timestamp()
    os.utime(p, (t, t))
    return p


@pytest.fixture
def logs(tmp_path, monkeypatch):
    triage._PORT_CACHE.clear()
    d = tmp_path / "nina"
    _log(d, "20261005-081746-3.2.0.9001-202610.log", 1888, 836,
         datetime(2026, 10, 7, 20, 17))       # 2.5 days, ~334 MB/day
    _log(d, "20261005-081750-3.2.0.9002-202610.log", 1889, 20,
         datetime(2026, 10, 7, 20, 17))
    cfg = PhotonScriptConfig(_env_file=None, nina_logs_dir=str(d),
                             piggyback_enabled=True,
                             nina_base_url="http://localhost:1888/v2/api",
                             piggyback_nina_base_url="http://localhost:1889/v2/api")
    monkeypatch.setattr(app, "_config", cfg)
    return cfg


def test_log_sizes_warn_on_the_big_rc16_log(logs):
    rows = {r["rig"]: r for r in nlw.log_sizes(logs)}
    rc = rows["rc16"]
    assert rc["mb"] == pytest.approx(836, abs=1) and rc["warn"]
    assert 300 < rc["mb_per_day"] < 350 and "over 500" in rc["why"]
    assert rows["piggyback"]["warn"] is False
    line = nlw.morning_line(logs)
    assert line.startswith("NINA log size: RC16 20261005-081746")
    assert "Piggy" not in line and "/api/nina/log/top" in line


def test_log_sizes_pace_rule_and_off(logs, tmp_path):
    cfg = logs.model_copy(update={"nina_log_warn_mb": 1000.0})
    rc = next(r for r in nlw.log_sizes(cfg) if r["rig"] == "rc16")
    assert not rc["warn"]                    # 836 < 1000, 334 x 2 < 1000
    cfg = logs.model_copy(update={"nina_log_warn_mb": 600.0})
    rc = next(r for r in nlw.log_sizes(cfg) if r["rig"] == "rc16")
    assert rc["warn"] and "over 600" in rc["why"]
    cfg = logs.model_copy(update={"nina_log_warn_mb": 0})
    assert not any(r["warn"] for r in nlw.log_sizes(cfg))
    assert nlw.morning_line(cfg) is None
    # a young log growing 600 MB/day: under the line, but on pace for it
    d = tmp_path / "young"
    _log(d, "20261007-200000-3.2.0.9001-202610.log", 1888, 300,
         datetime(2026, 10, 8, 8, 0))
    triage._PORT_CACHE.clear()
    cfg = logs.model_copy(update={"nina_log_warn_mb": 1000.0,
                                  "nina_logs_dir": str(d)})
    rc = next(r for r in nlw.log_sizes(cfg) if r["rig"] == "rc16")
    assert rc["mb_per_day"] == pytest.approx(600, abs=5)
    assert rc["warn"] and "on pace" in rc["why"]


def test_morning_report_carries_the_log_line(logs, tmp_path, monkeypatch):
    from photonscript.scheduler import morning_report as mr
    card = {"date": "2026-10-07", "rigs": [], "nina_logs": {
        "logs": [], "line": nlw.morning_line(logs)}}
    assert any(x.startswith("NINA log size") for x in mr.card_lines(card))
    monkeypatch.setattr(mr, "latest_night", lambda *a, **k: "2026-10-07")
    out = mr.report_card(logs.model_copy(update={"data_dir": str(tmp_path)}),
                         projects=[], calibration=False)
    assert out["nina_logs"]["line"].startswith("NINA log size")
    assert any(x.startswith("NINA log size") for x in out["lines"])


# --- top messages --------------------------------------------------------------------

def _spammy(p: Path, n: int = 20000):
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("2026-10-05T08:17:46.0001|INFO|API.cs|Start|1|starting web "
                 "server, listening at 0.0.0.0:1888\n")
        for i in range(n):
            fh.write(f"2026-10-07T03:{i // 600 % 60:02d}:{i // 10 % 60:02d}.{i:04d}"
                     f"|INFO|ImageHistoryVM.cs|Reload|{100 + i % 3}|Reloading image "
                     f"{i} from disk\n")
            if i % 10 == 0:
                fh.write(f"2026-10-07T03:00:00.0000|DEBUG|SequenceController.cs|Get|"
                         f"55|GET /v2/api/sequence/json took {i % 97} ms\n")
            if i % 1000 == 0:
                fh.write("   at NINA.Foo.Bar() in C:\\x.cs:line 3\n")
    return p


def test_top_messages_groups_shapes_from_the_tail(tmp_path):
    p = _spammy(tmp_path / "20261005-081746-3.2.0.9001-202610.log")
    res = nlw.top_messages(p, mb=50, top=5)
    assert res["reached_start"] and res["lines"] > 20000
    top = res["top"][0]
    assert top["key"] == "INFO|ImageHistoryVM.cs|Reload|Reloading image # from disk"
    assert top["count"] == 20000 and top["share_pct"] > 80
    assert res["top"][1]["key"].startswith("DEBUG|SequenceController.cs|Get|GET")
    assert res["top"][1]["count"] == 2000
    assert any(t["key"] == "(continuation lines)" for t in res["top"])
    # a 1 MB cap reads only the tail
    small = nlw.top_messages(p, mb=1, top=3)
    assert not small["reached_start"] and small["scanned_mb"] <= 1.05


def test_nina_log_top_endpoint(logs):
    d = Path(logs.nina_logs_dir)
    p = _spammy(d / "20261007-120000-3.2.0.9003-202610.log", 500)
    t = datetime(2026, 10, 8, 2, 0).timestamp()
    os.utime(p, (t, t))
    res = triage.api_nina_log_top(rig="rc16", mb=5, top=3)
    assert res["file"] == p.name and res["top"][0]["count"] == 500
    assert {r["rig"] for r in res["sizes"]} == {"rc16", "piggyback"}
    assert "error" in triage.api_nina_log_top(file="../x.log")


def test_where_panel_shares_its_nina_reads(monkeypatch):
    from photonscript.scheduler import sideload as sd
    from photonscript.scheduler import where_panel as wp
    hits = {}

    async def _get(base, path):
        hits[path] = hits.get(path, 0) + 1
        return None

    async def _read(base, client=None):
        hits["seq"] = hits.get("seq", 0) + 1
        return [], None

    monkeypatch.setattr(wp, "_get", _get)
    monkeypatch.setattr(sd, "read_sequence_state", _read)
    cfg = PhotonScriptConfig(_env_file=None, piggyback_enabled=False)

    async def main():
        for _ in range(4):           # four tabs / monitor ticks inside the TTL
            await wp.collect(cfg, None, tel={})
    asyncio.run(main())
    assert hits and all(v == 1 for v in hits.values()), hits
