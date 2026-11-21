"""PS-256: COM heal, restart the mount's COM port through the admin task."""
import asyncio
import json
from datetime import datetime
from pathlib import Path

from photonscript.scheduler import com_heal as ch
from photonscript.scheduler import prearm as pa
from photonscript.shared.config import PhotonScriptConfig

NOW = datetime(2026, 11, 14, 19, 0)          # 12:00 MST
BAD = {"Name": "USB Serial Device (COM3)", "PNPClass": "Ports",
       "ConfigManagerErrorCode": 10, "DeviceID": "USB\\VID_1\\X"}


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"), **kw)


class _Runner:
    """Fake subprocess: schtasks /query and /run."""

    def __init__(self, registered=True, run_rc=0):
        self.registered, self.run_rc, self.calls = registered, run_rc, []

    def __call__(self, args, timeout=30.0):
        self.calls.append(list(args))
        if args[:2] == ["schtasks", "/query"]:
            return (0, "PhotonScript COM heal   N/A   Ready") if self.registered else \
                (1, "ERROR: The system cannot find the file specified.")
        if args[:2] == ["schtasks", "/run"]:
            return self.run_rc, "SUCCESS: Attempted to run the scheduled task."
        raise AssertionError(f"unexpected command {args}")


class _Pnp:
    def __init__(self, *reads):
        self.reads = list(reads)

    def __call__(self):
        return self.reads.pop(0) if len(self.reads) > 1 else self.reads[0]


class _Armer:
    def __init__(self, state="DISARMED", plan=None):
        self.state, self.plan = state, plan or {}


async def _idle_seq(cfg):
    return {"NINA #1": [], "NINA #2": None}


async def _nosleep(s):
    return None


def _heal(cfg, armer=None, **kw):
    sent = kw.pop("sent", [])

    async def notify(cfg, msg, **k):
        sent.append((msg, k))
    kw.setdefault("seq_fn", _idle_seq)
    kw.setdefault("sleep", _nosleep)
    kw.setdefault("notify", notify)
    kw.setdefault("now", NOW)
    return asyncio.run(ch.heal(cfg, armer or _Armer(), **kw))


# ------------------------------------------------------------------ detect

def test_detect_port_with_problem_code():
    assert ch.detect([BAD], "COM3")["unhealthy"] is True
    assert ch.detect([], "COM3")["unhealthy"] is False
    assert ch.detect(None, "COM3")["unhealthy"] is False


def test_detect_ignores_other_devices_and_disabled():
    others = [
        {**BAD, "Name": "USB Serial Device (COM30)"},                 # another port
        {**BAD, "Name": "USB Serial Device (COM4)"},
        {**BAD, "PNPClass": "USB", "Name": "Hub (COM3)"},             # not Ports
        {**BAD, "ConfigManagerErrorCode": 22},                       # disabled on purpose
        {**BAD, "ConfigManagerErrorCode": 0},
        {**BAD, "ConfigManagerErrorCode": None}]
    assert ch.port_problems(others, "COM3") == []
    assert ch.detect(others, "com3")["unhealthy"] is False
    assert ch.PNP_IGNORE_CODES == pa.PNP_IGNORE_CODES


def test_detect_error_201_optional():
    d = ch.detect([], "COM3", connect_error="TheSky script error: Error = 201.")
    assert d["unhealthy"] and d["error_201"] and d["problems"] == []
    assert not ch.detect([], "COM3", connect_error="Error 2010")["unhealthy"]


# ------------------------------------------------------------------ heal

def test_not_registered_is_a_clear_result_and_pages_once(tmp_path):
    cfg = _cfg(tmp_path)
    sent = []
    r = _heal(cfg, runner=_Runner(registered=False), pnp_fn=_Pnp(([BAD], None)), sent=sent)
    assert r["status"] == "not_registered"
    assert r["detail"] == "not registered: Jeremy runs deploy\\register-com-heal.ps1 once as admin"
    assert "Disable device" in r["fix"]
    assert len(sent) == 1
    # no attempt counted, and no second page today
    r = _heal(cfg, runner=_Runner(registered=False), pnp_fn=_Pnp(([BAD], None)), sent=sent)
    assert r["status"] == "not_registered" and len(sent) == 1
    assert ch.load_day(cfg, ch.local_day(cfg, NOW))["attempts"] == []


def test_success_runs_task_and_reconnects_without_unpark(tmp_path):
    cfg = _cfg(tmp_path)
    run = _Runner()
    recon = []

    def reconnect(cfg):
        recon.append(True)
        return {"ok": True, "parked": "true", "detail": "connected (not unparked)"}
    sent = []
    r = _heal(cfg, runner=run, pnp_fn=_Pnp(([BAD], None), ([], None)), reconnect_fn=reconnect,
              sent=sent)
    assert r["status"] == "healed" and recon == [True] and sent == []
    assert ["schtasks", "/run", "/tn", "PhotonScript COM heal"] in run.calls
    assert r["devices_after"] == []
    d = ch.load_day(cfg, ch.local_day(cfg, NOW))
    assert len(d["attempts"]) == 1 and d["attempts"][0]["healed"] is True
    assert d["attempts"][0]["started"] and d["attempts"][0]["finished"]
    # the reconnect script connects without unparking and never unparks / slews
    js = ch.RECONNECT_JS
    assert "ConnectAndDoNotUnpark()" in js
    assert "Unpark()" not in js.replace("ConnectAndDoNotUnpark()", "")
    for bad in (".Connect()", "Slew", "Park(", "Sync", "SetTracking"):
        assert bad not in js


def test_dry_run_does_not_run_the_task(tmp_path):
    cfg = _cfg(tmp_path)
    run = _Runner()
    r = _heal(cfg, runner=run, pnp_fn=_Pnp(([BAD], None)), dry_run=True)
    assert r["status"] == "would_heal"
    assert not any(c[:2] == ["schtasks", "/run"] for c in run.calls)
    assert ch.load_day(cfg, ch.local_day(cfg, NOW))["attempts"] == []


def test_healthy_port_does_nothing(tmp_path):
    run = _Runner()
    r = _heal(_cfg(tmp_path), runner=run, pnp_fn=_Pnp(([], None)))
    assert r["status"] == "healthy" and run.calls == []


def test_failed_heal_pages_once_and_limit_holds(tmp_path):
    cfg = _cfg(tmp_path, com_heal_max_per_day=2)
    sent = []
    still_bad = _Pnp(([BAD], None))
    kw = dict(runner=_Runner(), pnp_fn=still_bad, reconnect_fn=lambda c: {"ok": True}, sent=sent)
    r1 = _heal(cfg, **kw)
    assert r1["status"] == "failed" and "still has problem code 10" in r1["detail"]
    assert len(sent) == 1
    msg, k = sent[0]
    assert k.get("priority") == 1 and k.get("kind") == "act"
    assert "Disable device" in msg and "Enable device" in msg and "COM3" in msg
    r2 = _heal(cfg, **kw)
    assert r2["status"] == "failed" and len(sent) == 1           # one page per day
    run = _Runner()
    r3 = _heal(cfg, **{**kw, "runner": run})
    assert r3["status"] == "limit" and len(sent) == 1
    assert not any(c[:2] == ["schtasks", "/run"] for c in run.calls)
    d = json.loads((Path(cfg.data_dir) / "com_heal" / "2026-11-14.json").read_text())
    assert len(d["attempts"]) == 2 and d["paged"]


def test_limit_pages_when_not_paged_yet(tmp_path):
    cfg = _cfg(tmp_path, com_heal_max_per_day=0)
    sent = []
    r = _heal(cfg, runner=_Runner(), pnp_fn=_Pnp(([BAD], None)), sent=sent)
    assert r["status"] == "limit" and len(sent) == 1 and r["paged"] is True


def test_refuses_while_a_night_runs(tmp_path):
    cfg = _cfg(tmp_path)
    for armer in (_Armer("RUNNING"), _Armer("PAUSED_UNSAFE"), _Armer("WATCHING"),
                  _Armer("ARMED", {"preconfig_utc": "2026-11-14T18:00:00Z"})):
        run = _Runner()
        r = _heal(cfg, armer, runner=run, pnp_fn=_Pnp(([BAD], None)))
        assert r["status"] == "refused", armer.state
        assert run.calls == []

    async def nina_busy(cfg):
        return {"NINA #1": ["Smart Exposure"], "NINA #2": []}
    r = _heal(cfg, _Armer("COMPLETE"), runner=_Runner(), pnp_fn=_Pnp(([BAD], None)),
              seq_fn=nina_busy)
    assert r["status"] == "refused" and "NINA #1" in r["detail"]
    # ARMED before pre-config is fine
    r = _heal(cfg, _Armer("ARMED", {"preconfig_utc": "2026-11-15T00:00:00Z"}),
              runner=_Runner(), pnp_fn=_Pnp(([BAD], None), ([], None)),
              reconnect_fn=lambda c: {"ok": True, "detail": "connected (not unparked)"})
    assert r["status"] == "healed"


def test_disabled(tmp_path):
    run = _Runner()
    r = _heal(_cfg(tmp_path, com_heal_enabled=False), runner=run, pnp_fn=_Pnp(([BAD], None)))
    assert r["status"] == "disabled" and run.calls == []


def test_status_reports_task_and_port(tmp_path):
    cfg = _cfg(tmp_path)
    s = asyncio.run(ch.status(cfg, NOW, runner=_Runner(registered=False),
                              pnp_fn=_Pnp(([BAD], None))))
    assert s["task"]["registered"] is False and "register-com-heal" in s["task"]["hint"]
    assert s["port_status"]["ok"] is False and s["attempts"] == []


# ------------------------------------------------------------------ prearm hook

def test_prearm_adds_heal_row_and_rereads_pnp(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    calls = []

    async def fake_heal(cfg, armer, **kw):
        calls.append(kw)
        return {"status": "healed", "port": "COM3", "detail": "COM3 restarted",
                "devices_after": []}
    monkeypatch.setattr(ch, "heal", fake_heal)
    row, pnp = asyncio.run(pa._com_heal(cfg, _Armer(), [BAD], "after_start", NOW))
    assert row["section"] == "Windows" and row["status"] == "pass" and row["id"] == "com_heal"
    assert pnp == [] and calls[0]["devs"] == [BAD]
    assert pa.pnp_rows(pnp) == []

    async def failed_heal(cfg, armer, **kw):
        return {"status": "failed", "port": "COM3", "detail": "still code 10",
                "fix": ch.manual_fix("COM3"), "devices_after": [BAD]}
    monkeypatch.setattr(ch, "heal", failed_heal)
    row, pnp = asyncio.run(pa._com_heal(cfg, _Armer(), [BAD], "after_start", NOW))
    assert row["status"] == "fail" and "Disable device" in row["fix"]
    assert pa.pnp_rows(pnp)[0]["status"] == "fail"
    # a healthy port: no heal, no row
    calls.clear()
    monkeypatch.setattr(ch, "heal", fake_heal)
    assert asyncio.run(pa._com_heal(cfg, _Armer(), [], "manual", NOW)) == (None, [])
    assert calls == []


def test_prearm_run_includes_heal_row(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)

    async def pf(cfg, test_push=True):
        return {"checks": []}

    async def no_read(cfg, rig):
        return {"camera": (0.0, 1.0)}

    async def take(cfg, reason, armer_state=None, push=None):
        return {"rules": {"rows": [], "counts": {}}}

    async def fake_heal(cfg, armer, **kw):
        return {"status": "healed", "port": "COM3", "detail": "COM3 restarted",
                "devices_after": []}
    import photonscript.scheduler.preflight as pre
    import photonscript.scheduler.frozen_sensor as fs
    import photonscript.scheduler.settings_ledger as sl
    import photonscript.scheduler.mount_ledger as ml
    import photonscript.scheduler.protrack_recheck as pr
    import photonscript.scheduler.tpoint_preflight as tpp
    monkeypatch.setattr(pre, "run_preflight", pf)
    monkeypatch.setattr(fs, "read_rig", no_read)
    monkeypatch.setattr(sl, "take", take)
    monkeypatch.setattr(ml, "read_thesky", lambda cfg: {"connected": True, "parked": True})
    monkeypatch.setattr(pr, "recheck", lambda *a, **k: {"row": {"status": "pass"}})
    monkeypatch.setattr(tpp, "evaluate", lambda cfg, guided=False: {"level": "ok", "line": "ok"})
    monkeypatch.setattr(pa, "read_windows", lambda: {"ok": False, "note": "test"})
    monkeypatch.setattr(pa, "read_pnp", lambda: ([BAD], None))
    monkeypatch.setattr(ch, "heal", fake_heal)
    rep = asyncio.run(pa.run(cfg, None, trigger="manual", push=False, now=NOW))
    win = {r["id"]: r for r in rep["rows"] if r["section"] == "Windows"}
    assert win["com_heal"]["status"] == "pass"
    assert "pnp:none" in win and not any(k.startswith("pnp:USB") for k in win)


# ------------------------------------------------------------------ wiring

def test_routes_settings_and_script():
    from photonscript.scheduler.routers import com_heal as r
    paths = {getattr(x, "path", "") for x in r.router.routes}
    assert {"/api/com-heal", "/api/com-heal/run"} <= paths
    root = Path(r.__file__).parents[1]
    src = (root / "app.py").read_text(encoding="utf-8")
    assert "app.include_router(_com_heal_router.router)" in src
    for env in ("PS_COM_HEAL_ENABLED", "PS_COM_HEAL_PORT", "PS_COM_HEAL_TASK_NAME",
                "PS_COM_HEAL_MAX_PER_DAY"):
        assert env in src
    cfg = PhotonScriptConfig(_env_file=None)
    assert (cfg.com_heal_enabled, cfg.com_heal_port, cfg.com_heal_task_name,
            cfg.com_heal_max_per_day) == (True, "COM3", "PhotonScript COM heal", 2)
    ps1 = (root.parents[1] / "deploy" / "register-com-heal.ps1").read_text(encoding="utf-8")
    assert "/restart-device" in ps1 and "RunLevel Highest" in ps1 and "-Unregister" in ps1
    assert "GRGX" in ps1
    for f in (ps1, Path(ch.__file__).read_text(encoding="utf-8"),
              Path(r.__file__).read_text(encoding="utf-8")):
        assert chr(0x2014) not in f and chr(0x2013) not in f
