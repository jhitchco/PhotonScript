"""2026-10-14: deploy while PAUSED_OPERATOR (deploy.ps1 -AllowPaused).

The night was paused (NINA #1 and #2 stopped, coolers and tracking kept) for
polar alignment and a 228-point TPoint run in TheSky, and the scope refused
the deploy: the only way in was disarm = make_safe (stop, warm, park). These
tests pin:
  1. POST /api/update?allow_paused=true: allowed only while PAUSED_OPERATOR
     with the pause settled, nothing in flight, NINA #1 and #2 idle and no
     grading job; never while RUNNING / PAUSED_UNSAFE / WATCHING;
  2. the paused night round-trips through armer_state.json and restore()
     brings it back PAUSED_OPERATOR with no device command (no connect-all,
     unguided guard, make_safe, park, warm, cooler, load);
  3. self-update only goes ahead with the one-shot allowance, and a refused
     smoke check or a rollback leaves the night PAUSED_OPERATOR;
  4. deploy.ps1 / dashboard plumbing (static).
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from photonscript import orchestrator
from photonscript.scheduler import armer as armer_mod
from photonscript.scheduler import sideload as sd
from photonscript.scheduler.armer import PAUSE_STATE, Armer
from photonscript.shared import updater as up
from photonscript.shared.config import PhotonScriptConfig

ROOT = Path(__file__).resolve().parents[2]
# what a restore of a paused night must never send
NEVER = {"mount_park", "camera_warm", "camera_cool", "guider_stop",
         "guider_start", "guider_disconnect", "sequence_load",
         "sequence_start", "sequence_stop", "connect"}
RUNNING_TREE = [{"Name": "Targets_Container", "Status": "RUNNING",
                 "Items": [{"Name": "M 31_Container", "Status": "RUNNING"}]}]


def _cfg(tmp_path, **kw):
    kw.setdefault("dawn_flats_window_min", 0)
    kw.setdefault("image_watch_dir", str(tmp_path / "nina"))
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _z(d):
    return d.replace(microsecond=0).isoformat() + "Z"


def _plan(dawn_in_h=5.0):
    now = datetime.utcnow()
    return {"night_of": now.strftime("%Y-%m-%d"),
            "preconfig_utc": _z(now - timedelta(hours=3)),
            "dusk_utc": _z(now - timedelta(hours=2)),
            "dawn_utc": _z(now + timedelta(hours=dawn_in_h)),
            "naut_dawn_utc": None, "sunrise_utc": None, "dark_hours": 8.0,
            "targets": ["M 31"]}


@pytest.fixture
def env(tmp_path, monkeypatch):
    notes = []

    async def _notify(c, msg, **kw):
        notes.append(msg)
    monkeypatch.setattr(armer_mod, "notify", _notify)
    nina2 = {"tree": [], "err": None}

    async def _read(base, client=None):
        return (None if nina2["tree"] is None else nina2["tree"]), nina2["err"]
    monkeypatch.setattr(sd, "read_sequence_state", _read)
    return SimpleNamespace(tmp=tmp_path, notes=notes, nina2=nina2)


def _fake_nina(a, nina1=()):
    a.calls = []
    a.fake = {"nina1": list(nina1)}

    async def _nina(key, method="GET", json_body=None, **kw):
        a.calls.append(key)
        if key == "sequence_state":
            t = a.fake["nina1"]
            return None if t is None else {"Response": t}
        if key == "safety":
            return {"Response": {"Connected": True, "IsSafe": True}}
        return {"Success": True}
    a._nina = _nina


def _paused(env, phase="paused", piggy="pause", nina1=(), **cfg):
    cfg.setdefault("piggyback_enabled", True)
    a = Armer(_cfg(env.tmp, **cfg))
    a.state = PAUSE_STATE
    a.plan = _plan()
    a.guiding_override = "unguided"
    a.recipe = {"id": "tpoint", "night": a.plan["night_of"], "status": "done"}
    a._ug_state = {"night": a.plan["night_of"], "episode": None, "actions": 1,
                   "last_action": None, "connected_alerted": True,
                   "alerted": ["x"], "episodes": 1}
    a.pause_info = {"since": "2026-10-14T04:10:00Z", "phase": phase,
                    "when": "after_exposure", "piggy": piggy, "parked": False,
                    "rigs": {"rc16": {"ok": True, "how": "after_exposure"},
                             "piggyback": {"ok": True, "how": "idle"}}}
    _fake_nina(a, nina1)
    a._persist()
    return a


# ------------------------------------------------------- 1. the update gate

@pytest.fixture
def api(env, monkeypatch):
    import photonscript.scheduler.app as app
    import photonscript.scheduler.runs as runs
    monkeypatch.setattr(runs, "_backfill_state", {})
    monkeypatch.setattr(runs, "_regrade_all", {"running": False})
    exits = []
    monkeypatch.setattr(orchestrator, "request_exit",
                        lambda code: exits.append(code) or True)

    class FakeTimer:
        def __init__(self, s, fn):
            self.daemon = False

        def start(self):
            pass
    import threading
    monkeypatch.setattr(threading, "Timer", FakeTimer)
    box = {}

    def use(a):
        box["a"] = a
        monkeypatch.setattr(app, "_config", a.config)
        monkeypatch.setattr(app, "get_armer", lambda: box["a"])
        return a
    return SimpleNamespace(app=app, exits=exits, use=use)


async def _post(app, q=""):
    transport = httpx.ASGITransport(app=app.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.post("/api/update" + q)


async def test_paused_needs_allow_paused(api, env):
    a = api.use(_paused(env))
    r = await _post(api.app)
    assert r.status_code == 409 and "allow_paused" in r.json()["detail"]
    r = await _post(api.app, "?allow_armed=true")     # -AllowArmed is not enough
    assert r.status_code == 409 and "allow_paused" in r.json()["detail"]
    assert a.calls == []                               # nothing read or sent
    assert not (env.tmp / up.PAUSED_ALLOW_NAME).exists()
    await asyncio.sleep(0.6)
    assert api.exits == []


async def test_paused_settled_and_idle_restarts(api, env):
    a = api.use(_paused(env))
    r = await _post(api.app, "?allow_paused=true")
    assert r.status_code == 200, r.json()
    await asyncio.sleep(0.7)
    assert api.exits == [42]
    # read only: NINA #1 state read, nothing sent; state unchanged
    assert a.calls == ["sequence_state"] and a.state == PAUSE_STATE
    allow = json.loads((env.tmp / up.PAUSED_ALLOW_NAME).read_text(encoding="utf-8"))
    assert allow["night_of"] == a.plan["night_of"]
    assert allow["pause_since"] == "2026-10-14T04:10:00Z"
    saved = json.loads((env.tmp / "armer_state.json").read_text(encoding="utf-8"))
    assert saved["state"] == PAUSE_STATE
    assert saved["pause"]["update_restart"]["night_of"] == a.plan["night_of"]


@pytest.mark.parametrize("state", ["RUNNING", "PAUSED_UNSAFE", "WATCHING"])
async def test_never_mid_night_states(api, env, state):
    a = _paused(env)
    a.state = state
    api.use(a)
    r = await _post(api.app, "?allow_paused=true&allow_armed=true")
    assert r.status_code == 409 and state in r.json()["detail"]
    assert not (env.tmp / up.PAUSED_ALLOW_NAME).exists()
    await asyncio.sleep(0.6)
    assert api.exits == []


@pytest.mark.parametrize("why, setup", [
    ("not settled", lambda a, e: a.pause_info.update(phase="stopping")),
    ("not settled", lambda a, e: a.pause_info.update(phase="resuming",
                                                     resume={"step": "x"})),
    ("in flight", lambda a, e: a.pause_info.update(restart={"step": "x"})),
    ("NINA #1 is running", lambda a, e: a.fake.update(nina1=RUNNING_TREE)),
    ("NINA #1 sequence state unreadable", lambda a, e: a.fake.update(nina1=None)),
    ("NINA #2 is running", lambda a, e: e.nina2.update(tree=RUNNING_TREE)),
    ("NINA #2 sequence state unreadable",
     lambda a, e: e.nina2.update(tree=None, err="ConnectError")),
    ("calibration capture job",
     lambda a, e: setattr(a, "_calibration_busy", lambda: "rc16")),
])
async def test_paused_refused_until_safe(api, env, why, setup):
    a = _paused(env)
    setup(a, env)
    api.use(a)
    r = await _post(api.app, "?allow_paused=true")
    assert r.status_code == 409 and why in r.json()["detail"], r.json()
    assert not (env.tmp / up.PAUSED_ALLOW_NAME).exists()
    assert "update_restart" not in a.pause_info
    assert not set(a.calls) & NEVER
    await asyncio.sleep(0.6)
    assert api.exits == []


async def test_paused_refused_while_grading(api, env):
    import photonscript.scheduler.runs as runs
    api.use(_paused(env))
    runs._backfill_state["2026-10-14"] = {"running": True}
    r = await _post(api.app, "?allow_paused=true")
    assert r.status_code == 409 and "grading 2026-10-14" in r.json()["detail"]
    assert not (env.tmp / up.PAUSED_ALLOW_NAME).exists()


async def test_nina2_not_read_without_piggyback(api, env):
    a = api.use(_paused(env, piggyback_enabled=False))
    env.nina2.update(tree=None)                    # would block if read
    r = await _post(api.app, "?allow_paused=true")
    assert r.status_code == 200, r.json()
    assert a.calls == ["sequence_state"]


# ------------------------------------------------- 2. persistence + restore

def _restored(a, monkeypatch=None):
    b = Armer(a.config)
    _fake_nina(b)
    b.rec = []

    async def rec(name, *args, **kw):
        b.rec.append(name)
        return True
    b.connect_all_rigs = lambda: rec("connect_all_rigs")
    b._unguided_guard = lambda phase: rec("unguided_guard")
    b.make_safe = lambda: rec("make_safe")
    b.dawn_shutdown = lambda reason="": rec("dawn_shutdown")
    b.cooler_dew_off = lambda skip_rigs=None: rec("cooler_dew_off")
    b._dispatch_and_start = lambda **kw: rec("dispatch")
    b._reconcile_cooler = lambda now: rec("reconcile_cooler")
    b._dispatch_piggyback_companion = lambda: rec("piggy_companion")
    assert b.restore() is True
    return b


async def test_round_trip_restores_the_paused_night_quietly(api, env):
    a = api.use(_paused(env))
    from photonscript.scheduler import deep_night
    dn = deep_night.state_path(a.config, a.plan["night_of"])
    dn.parent.mkdir(parents=True, exist_ok=True)
    dn.write_text(json.dumps({"night": a.plan["night_of"], "mode": "deep"}))
    r = await _post(api.app, "?allow_paused=true")
    assert r.status_code == 200
    b = _restored(a)
    try:
        await asyncio.sleep(0.05)                     # let startup tasks run
        assert b.state == PAUSE_STATE
        assert b.plan["night_of"] == a.plan["night_of"]
        assert b.guiding_override == "unguided"
        assert b.recipe == a.recipe
        assert b._ug_state == a._ug_state             # guard latches kept
        p = b.pause_info
        assert p["phase"] == "paused" and p["piggy"] == "pause"
        assert p["since"] == "2026-10-14T04:10:00Z" and p["parked"] is False
        assert "update_restart" not in p and p["update_restarted"]["restored"]
        assert b._pause_task is None
        # no device command at all on restore
        assert b.rec in ([], ["reconcile_cooler"])   # the paused tick's nanny
        assert not set(b.calls) & NEVER
        assert "for an update while paused" in env.notes[-1]
        assert json.loads(dn.read_text())["mode"] == "deep"   # override kept
        # a later plain restart is not reported as an update
        c = _restored(b)
        c._task.cancel()
        await asyncio.sleep(0.05)
        assert "restarted while paused" in env.notes[-1]
    finally:
        b._task.cancel()


async def test_resume_after_restore_redispatches_as_usual(env):
    a = _paused(env)
    b = _restored(a)
    b._task.cancel()
    res = await b.resume()
    assert res["ok"] and res["pause"]["phase"] == "resuming"
    await b._pause_task
    assert "dispatch" in b.rec and b.state == "RUNNING"


async def test_crash_restore_mid_stop_still_finishes_the_stop(env):
    # unchanged behavior: only a settled pause is quiet
    a = _paused(env, phase="stopping")
    b = Armer(a.config)
    _fake_nina(b)
    seen = []

    async def _conn():
        seen.append("connect_all_rigs")
        return {}
    b.connect_all_rigs = _conn
    b._finish_pause = lambda: asyncio.sleep(0)
    assert b.restore() is True
    b._task.cancel()
    await asyncio.sleep(0.05)
    assert seen == ["connect_all_rigs"]


# --------------------------------------------- 3. self-update + rollback

def _git(repo, *args):
    import subprocess
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                       text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    import subprocess
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"}.items():
        monkeypatch.setenv(k, v)
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                   check=True)
    dev = tmp_path / "dev"
    subprocess.run(["git", "clone", "-q", str(origin), str(dev)], check=True,
                   capture_output=True)
    _git(dev, "checkout", "-q", "-b", "main")
    (dev / "app.txt").write_text("A\n")
    _git(dev, "add", "app.txt")
    _git(dev, "commit", "-q", "-m", "A")
    _git(dev, "push", "-q", "origin", "main")
    scope = tmp_path / "scope"
    subprocess.run(["git", "clone", "-q", str(origin), str(scope)], check=True,
                   capture_output=True)
    a = _git(scope, "rev-parse", "HEAD")
    (dev / "app.txt").write_text("B\n")
    _git(dev, "commit", "-q", "-am", "B")
    _git(dev, "push", "-q", "origin", "main")
    return SimpleNamespace(scope=scope, A=a, B=_git(dev, "rev-parse", "HEAD"))


def _update(cfg, repos, ok=True):
    return up.self_update(cfg, repos.scope, notify=lambda *a: None,
                          smoke=lambda *a, **k: (ok, "imports ok"))


def _allow(a):
    a.prepare_update_restart()


def test_self_update_defers_paused_without_allowance(env, repos):
    a = _paused(env)
    rc, detail = _update(a.config, repos)
    assert rc == up.DEFERRED and "PAUSED_OPERATOR" in detail
    assert _git(repos.scope, "rev-parse", "HEAD") == repos.A


def test_self_update_goes_ahead_with_allowance_once(env, repos):
    a = _paused(env)
    _allow(a)
    rc, detail = _update(a.config, repos)
    assert rc == up.UPDATED, detail
    assert _git(repos.scope, "rev-parse", "HEAD") == repos.B
    assert not (env.tmp / up.PAUSED_ALLOW_NAME).exists()   # one shot
    saved = json.loads((env.tmp / "armer_state.json").read_text(encoding="utf-8"))
    assert saved["state"] == PAUSE_STATE                    # untouched


@pytest.mark.parametrize("spoil", ["expired", "other pause", "settled no more",
                                   "running"])
def test_allowance_must_match_the_pause(env, repos, spoil):
    a = _paused(env)
    _allow(a)
    p = env.tmp / up.PAUSED_ALLOW_NAME
    if spoil == "expired":
        rec = json.loads(p.read_text())
        rec["at"] = (datetime.now(timezone.utc)
                     - timedelta(seconds=up.PAUSED_ALLOW_TTL_S + 5)).isoformat()
        p.write_text(json.dumps(rec))
    elif spoil == "other pause":
        a.pause_info["since"] = "2026-10-14T05:00:00Z"
        a._persist()
    elif spoil == "settled no more":
        a.pause_info["phase"] = "resuming"
        a._persist()
    else:
        a.state = "RUNNING"
        a._persist()
    rc, _ = _update(a.config, repos)
    assert rc == up.DEFERRED
    assert _git(repos.scope, "rev-parse", "HEAD") == repos.A
    assert not p.exists()


def test_failed_smoke_check_keeps_old_code_and_the_pause(env, repos):
    a = _paused(env)
    _allow(a)
    rc, _ = _update(a.config, repos, ok=False)
    assert rc == up.REFUSED
    assert _git(repos.scope, "rev-parse", "HEAD") == repos.A
    assert not (env.tmp / up.PAUSED_ALLOW_NAME).exists()


async def test_rollback_comes_back_paused(env, repos):
    a = _paused(env)
    _allow(a)
    assert _update(a.config, repos)[0] == up.UPDATED
    # the new code restores (quietly), then fails its health check: the
    # wrapper resets to prev and the old code restores the same night
    b = _restored(a)
    b._task.cancel()
    up.mark_failed(a.config, "no healthy /api/health")
    _git(repos.scope, "reset", "--hard", repos.A)
    st = up.rollback_done(a.config)
    assert st["status"] == "rolled_back" and st["bad_sha"] == repos.B
    c = _restored(b)
    try:
        await asyncio.sleep(0.05)
        assert c.state == PAUSE_STATE and c.pause_info["phase"] == "paused"
        assert c.rec in ([], ["reconcile_cooler"])
        assert not set(c.calls) & NEVER
    finally:
        c._task.cancel()


# ------------------------------------------------------------- 4. plumbing

def test_deploy_ps1_passes_allow_paused():
    s = (ROOT / "deploy" / "deploy.ps1").read_bytes().decode("ascii")
    assert "[switch]$AllowPaused" in s
    assert 'if ($AllowPaused) { $updateQuery += "allow_paused=true" }' in s
    assert 'if ($AllowArmed) { $updateQuery += "allow_armed=true" }' in s
    assert '($updateQuery -join "&")' in s
    assert "-AllowPaused if it is PAUSED_OPERATOR" in s


def test_dashboard_offers_allow_paused():
    s = (ROOT / "photonscript" / "scheduler" / "templates" / "system.html"
         ).read_text(encoding="utf-8")
    assert "/allow_paused/.test(d.detail" in s
    assert "/api/update?allow_paused=true" in s
