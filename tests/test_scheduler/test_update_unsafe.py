"""2026-11-08: deploy while PAUSED_UNSAFE with the roof closed.

38cb967 was refused at 01:52Z: clouds, roof shut, RC16 parked, nothing could
image for hours. deploy.ps1 -AllowPaused (allow_paused=true) now also covers
PAUSED_UNSAFE, but only when the safety monitor reads unsafe (roof closed)
and NINA reads the mount parked; it fails closed on anything unreadable. The
self-update allowance is bound to the night and to PAUSED_UNSAFE.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from photonscript.shared import updater as up
from tests.test_scheduler.test_update_paused import (  # noqa: F401  (fixtures)
    NEVER, _git, _paused, _post, _update, api, env, repos)


def _unsafe(env, safe=False, safety_connected=True, parked=True,
            mount_connected=True, **cfg):
    a = _paused(env, **cfg)
    a.state = "PAUSED_UNSAFE"
    a.pause_info = None
    a.fake.update(safety={"Connected": safety_connected, "IsSafe": safe},
                  mount={"Connected": mount_connected, "AtPark": parked})
    inner = a._nina

    async def _nina(key, method="GET", json_body=None, **kw):
        if key == "safety":
            a.calls.append(key)
            v = a.fake["safety"]
            return None if v is None else {"Response": v}
        if key == "mount_info":
            a.calls.append(key)
            v = a.fake["mount"]
            return None if v is None else {"Response": v}
        return await inner(key, method, json_body, **kw)
    a._nina = _nina
    a._persist()
    return a


async def test_unsafe_needs_allow_paused(api, env):
    a = api.use(_unsafe(env))
    r = await _post(api.app)
    assert r.status_code == 409
    d = r.json()["detail"]
    assert "PAUSED_UNSAFE" in d and "allow_paused" in d   # the dashboard retries on it
    assert a.calls == []
    await asyncio.sleep(0.6)
    assert api.exits == []


async def test_roof_closed_and_parked_restarts(api, env):
    a = api.use(_unsafe(env))
    r = await _post(api.app, "?allow_paused=true")
    assert r.status_code == 200, r.json()
    await asyncio.sleep(0.7)
    assert api.exits == [42]
    assert set(a.calls) == {"safety", "mount_info"}       # read only
    assert not set(a.calls) & NEVER
    assert a.state == "PAUSED_UNSAFE"
    allow = json.loads((env.tmp / up.PAUSED_ALLOW_NAME).read_text(encoding="utf-8"))
    assert allow["state"] == "PAUSED_UNSAFE"
    assert allow["night_of"] == a.plan["night_of"]


@pytest.mark.parametrize("why, kw", [
    ("reads safe", {"safe": True}),
    ("safety monitor is unreadable", {"safety_connected": False}),
    ("not parked", {"parked": False}),
    ("mount unreadable", {"mount_connected": False}),
])
async def test_unsafe_refused_unless_roof_closed_and_parked(api, env, why, kw):
    a = api.use(_unsafe(env, **kw))
    r = await _post(api.app, "?allow_paused=true")
    assert r.status_code == 409 and why in r.json()["detail"], r.json()
    assert not (env.tmp / up.PAUSED_ALLOW_NAME).exists()
    assert not set(a.calls) & NEVER
    await asyncio.sleep(0.6)
    assert api.exits == []


@pytest.mark.parametrize("which", ["safety", "mount"])
async def test_unreadable_blocks(api, env, which):
    a = _unsafe(env)
    a.fake[which] = None
    api.use(a)
    r = await _post(api.app, "?allow_paused=true")
    assert r.status_code == 409, r.json()


async def test_calibration_capture_blocks(api, env):
    a = _unsafe(env)
    a._calibration_busy = lambda: "piggyback"
    api.use(a)
    r = await _post(api.app, "?allow_paused=true")
    assert r.status_code == 409 and "calibration capture" in r.json()["detail"]


# ----------------------------------------------------------- self-update side

def test_self_update_defers_unsafe_without_allowance(env, repos):
    a = _unsafe(env)
    rc, detail = _update(a.config, repos)
    assert rc == up.DEFERRED and "PAUSED_UNSAFE" in detail
    assert _git(repos.scope, "rev-parse", "HEAD") == repos.A


def test_self_update_goes_ahead_with_the_unsafe_allowance(env, repos):
    a = _unsafe(env)
    a.prepare_unsafe_update_restart()
    rc, detail = _update(a.config, repos)
    assert rc == up.UPDATED, detail
    assert _git(repos.scope, "rev-parse", "HEAD") == repos.B
    assert not (env.tmp / up.PAUSED_ALLOW_NAME).exists()   # one shot


@pytest.mark.parametrize("spoil", ["running", "operator pause", "other night"])
def test_unsafe_allowance_must_match(env, repos, spoil):
    a = _unsafe(env)
    a.prepare_unsafe_update_restart()
    if spoil == "running":
        a.state = "RUNNING"            # the roof reopened and imaging resumed
    elif spoil == "operator pause":
        a.state = "PAUSED_OPERATOR"
        a.pause_info = {"since": "x", "phase": "paused"}
    else:
        a.plan["night_of"] = "2026-01-01"
    a._persist()
    rc, _ = _update(a.config, repos)
    assert rc == up.DEFERRED
    assert _git(repos.scope, "rev-parse", "HEAD") == repos.A
