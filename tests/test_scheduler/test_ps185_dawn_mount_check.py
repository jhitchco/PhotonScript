"""PS-185: page once when the mount is left unparked and tracking after
astro dawn on a night the armer does not own (2026-10-09: DISARMED, a
manual TPoint run; at 06:04 local the mount was still tracking at az 354
alt 62 and TheSky then said "The mount cannot slew").
"""
import asyncio
import inspect
import json
from datetime import datetime
from pathlib import Path

import pytest

from photonscript.scheduler import dawn_mount_check as dm
from photonscript.shared.config import PhotonScriptConfig

ROOT = Path(__file__).resolve().parents[2]
# night 2026-10-09 at the AARO site: astro dawn about 11:56Z (05:56 local)
DAWN_PLUS_30 = datetime(2026, 10, 10, 12, 30)
BEFORE = datetime(2026, 10, 10, 11, 0)
EVENING = datetime(2026, 10, 10, 2, 0)


def _cfg(tmp_path, **kw):
    kw.setdefault("thesky_tcp_host", "127.0.0.1")
    kw.setdefault("thesky_tcp_port", 1)
    kw.setdefault("nina_base_url", "http://127.0.0.1:1/v2/api")
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"), **kw)


TRACKING = {"parked": False, "tracking": True, "alt": 62.0, "az": 354.0,
            "source": "TheSky"}


def _run(cfg, armer="DISARMED", now=DAWN_PLUS_30, mount=TRACKING, busy=(),
         state=None):
    pushes = []

    async def read(c):
        return dict(mount) if mount else None

    async def busy_fn(c):
        return None if busy is None else list(busy)

    async def notify(c, msg, **k):
        pushes.append((msg, k.get("priority", 0)))
        return True
    st = state if state is not None else {"paged": set()}
    res = asyncio.run(dm.tick(cfg, armer, now=now, read=read, busy_fn=busy_fn,
                              notify=notify, state=st))
    return res, pushes, st


def _events(tmp_path):
    out = []
    for p in (tmp_path / "data" / "runs").glob("*_events.jsonl"):
        out += [json.loads(x) for x in p.read_text().splitlines()]
    return [e for e in out if e.get("kind") == "dawn_mount"]


def test_10_09_shape_pages_once_priority_1(tmp_path):
    cfg = _cfg(tmp_path)
    res, pushes, st = _run(cfg)
    assert res["state"] == "alert" and res["night"] == "2026-10-09"
    assert len(pushes) == 1 and pushes[0][1] == 1
    msg = pushes[0][0]
    assert "UNPARKED and TRACKING" in msg and "(az 354, alt 62)" in msg
    assert "TheSky: Telescope > Park" in msg
    assert "restart the Bisque control software" in msg
    assert "did not park it" in msg
    ev = _events(tmp_path)
    assert [e["value"] for e in ev] == ["alert"] and ev[0]["az"] == 354.0
    # the next tick, and a PhotonScript restart (fresh latch), stay quiet
    res2, p2, _ = _run(cfg, state=st)
    assert res2["state"] == "paged" and not p2
    res3, p3, _ = _run(cfg)
    assert res3["state"] == "paged" and not p3


@pytest.mark.parametrize("armer", ["ARMED", "RUNNING", "WATCHING", "PAUSED_UNSAFE"])
def test_an_armed_night_is_left_to_the_dawn_shutdown(tmp_path, armer):
    res, pushes, _ = _run(_cfg(tmp_path), armer=armer)
    assert res["state"] == "skip" and not pushes


@pytest.mark.parametrize("armer", ["DISARMED", "COMPLETE", "ERROR"])
def test_runs_when_the_armer_has_no_night(tmp_path, armer):
    res, pushes, _ = _run(_cfg(tmp_path), armer=armer)
    assert res["state"] == "alert" and len(pushes) == 1


def test_a_running_nina_sequence_waits(tmp_path):
    res, pushes, _ = _run(_cfg(tmp_path), busy=["Take Many Exposures"])
    assert res["state"] == "skip" and "Take Many Exposures" in res["why"]
    assert not pushes
    # NINA unreadable does not hold the page back
    res, pushes, _ = _run(_cfg(tmp_path / "b"), busy=None)
    assert res["state"] == "alert" and "could not be read" in pushes[0][0]


@pytest.mark.parametrize("now", [BEFORE, EVENING])
def test_before_dawn_plus_n_is_idle(tmp_path, now):
    res, pushes, _ = _run(_cfg(tmp_path), now=now)
    assert res["state"] == "idle" and not pushes


@pytest.mark.parametrize("mount,why", [
    ({"parked": True, "tracking": False}, "parked"),
    ({"parked": False, "tracking": False}, "not tracking"),
    ({"parked": None, "tracking": None}, "unknown")])
def test_parked_or_idle_mount_is_ok(tmp_path, mount, why):
    res, pushes, _ = _run(_cfg(tmp_path), mount={**mount, "source": "TheSky"})
    assert res["state"] == "ok" and why in res["why"] and not pushes


def test_no_mount_read(tmp_path):
    res, pushes, _ = _run(_cfg(tmp_path), mount=None)
    assert res["state"] == "unknown" and not pushes


def test_off_mode_and_defaults(tmp_path):
    c = PhotonScriptConfig(_env_file=None)
    assert c.dawn_mount_check_mode == "alert"
    assert c.dawn_mount_check_after_dawn_min == 20.0
    res, pushes, _ = _run(_cfg(tmp_path, dawn_mount_check_mode="off"))
    assert res["state"] == "off" and not pushes
    res, pushes, _ = _run(_cfg(tmp_path / "b", dawn_mount_check_after_dawn_min=60))
    assert res["state"] == "idle"          # 12:30Z is dawn + 34 min


def test_thesky_read_is_read_only_and_falls_back_to_nina(tmp_path, monkeypatch):
    from photonscript.telescope_agent import thesky_client as tc
    js = tc.READ_ONLY_JS["park_state"]
    assert "IsParked()" in js and "IsTracking" in js and "GetAzAlt()" in js
    for bad in ("Connect()", "Park()", "SetTracking", "Unpark", "SlewTo"):
        assert bad not in js.replace("IsConnected", "")
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(dm, "read_thesky", lambda c: None)

    async def nina(c):
        return {"parked": False, "tracking": True, "alt": 10.0, "az": 5.0,
                "source": "NINA #1"}
    monkeypatch.setattr(dm, "read_nina_mount", nina)
    assert asyncio.run(dm.read_mount(cfg))["source"] == "NINA #1"
    monkeypatch.setattr(dm, "read_thesky", lambda c: dict(TRACKING))
    assert asyncio.run(dm.read_mount(cfg))["source"] == "TheSky"


def test_thesky_parse(monkeypatch, tmp_path):
    from photonscript.telescope_agent import thesky_client as tc
    monkeypatch.setattr(tc.TheSkyClient, "park_state", lambda self: {
        "connected": "1", "parked": "0", "tracking": "1", "alt": "62.04",
        "az": "354.2"})
    m = dm.read_thesky(_cfg(tmp_path))
    assert m == {"parked": False, "tracking": True, "alt": 62.0, "az": 354.2,
                 "source": "TheSky"}
    monkeypatch.setattr(tc.TheSkyClient, "park_state",
                        lambda self: {"connected": "0"})
    assert dm.read_thesky(_cfg(tmp_path)) is None


def test_never_moves_the_mount():
    src = inspect.getsource(dm)
    for bad in ("park_mount", "mount_park", '"park"', "sequence_stop",
                "SetTracking", ".Park("):
        assert bad not in src


def test_wired_at_startup_and_on_the_system_page():
    app = (ROOT / "photonscript/scheduler/app.py").read_text(encoding="utf-8")
    assert "run_dawn_mount_check" in app
    assert '("dawn_mount_check_mode", "PS_DAWN_MOUNT_CHECK_MODE"' in app
    assert '("dawn_mount_check_after_dawn_min", "PS_DAWN_MOUNT_CHECK_AFTER_DAWN_MIN"' in app


@pytest.mark.parametrize("rel", [
    "photonscript/scheduler/dawn_mount_check.py",
    "tests/test_scheduler/test_ps185_dawn_mount_check.py"])
def test_ascii_no_em_dash(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert not [ln for ln in text.splitlines() if any(ord(c) > 127 for c in ln)]
