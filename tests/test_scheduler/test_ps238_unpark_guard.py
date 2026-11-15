"""PS-238: a TheSky park is undone by NINA #1's mount connect (the Bisque
ASCOM driver's UnparkOnConnect).

2026-11-14: the dawn shutdown parked at 12:53Z with tracking off; at
17:46:08Z (10:46 MST) the mount ledger shows unpark + tracking on, the
minute the daily app start launched NINA #1 and ran the connect-all; it
tracked under a closed roof all afternoon (armer COMPLETE, TheSky connected
and reading parked until then). 2026-11-02: parks were undone within 5 s by
NINA #1's mount auto-reconnect.
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import dawn_mount_check as dm
from photonscript.scheduler import preflight
from photonscript.scheduler import prearm as pa
from photonscript.scheduler import settings_ledger as sl
from photonscript.scheduler import unpark_guard as ug
from photonscript.scheduler.armer import Armer
from photonscript.shared.config import PhotonScriptConfig

ROOT = Path(__file__).resolve().parents[2]
UNSAFE_AT = datetime(2026, 11, 14, 17, 46, 8)


def _cfg(tmp_path, **kw):
    kw.setdefault("data_dir", str(tmp_path / "data"))
    kw.setdefault("thesky_tcp_host", "127.0.0.1")
    kw.setdefault("thesky_tcp_port", 1)
    kw.setdefault("nina_base_url", "http://127.0.0.1:1/v2/api")
    kw.setdefault("mount_connect_guard", True)
    return PhotonScriptConfig(_env_file=None, **kw)


class _Armer:
    def __init__(self, state="DISARMED", in_flight=False):
        self.state = state
        self._f = in_flight

    def dispatch_in_flight(self):
        return self._f


def _fns(parked=(None, ""), safe=None, calls=None):
    async def p(cfg):
        if calls is not None:
            calls.append("parked")
        return parked

    async def s(cfg):
        if calls is not None:
            calls.append("safe")
        return safe
    return p, s


# ------------------------------------------------------------- 1. ledger rule

def _rule(tmp_path):
    doc = sl.load_rules(_cfg(tmp_path))
    assert not doc.get("lint"), doc.get("lint")
    return next(r for r in doc["rule"] if r["id"] == "thesky_unpark_on_connect")


def _snap(value, *, carried=False):
    return {"apps": {"thesky": {"settings": {"ascom_driver": {"unpark_on_connect": value}},
                                "carried": carried}}}


@pytest.mark.parametrize("value,status", [
    (False, "pass"), ("False", "pass"), (0, "pass"),
    (True, "fail"), ("True", "fail"), (1, "fail"),
    (None, "unknown")])
def test_ledger_rule_wants_unpark_on_connect_off(tmp_path, value, status):
    r = _rule(tmp_path)
    assert r["app"] == "thesky" and r["severity"] == "fail" and r["equals"] is False
    assert r["fix"] == ("NINA #1 > Equipment > Mount > Bisque driver settings "
                        "(or TheSky Bisque TCS): untick Unpark on connect")
    out = sl.evaluate_rule(r, _snap(value), _cfg(tmp_path))
    assert out["status"] == status


def test_ledger_rule_surfaces_on_the_prearm_page(tmp_path):
    cfg = _cfg(tmp_path)
    doc = {"rule": [_rule(tmp_path)]}
    for value, want in ((True, "fail"), (None, "unknown"), (False, None)):
        res = sl.evaluate(doc, _snap(value), cfg)
        rows = [x for x in pa.ledger_rows(res) if x["id"] == "ledger:thesky_unpark_on_connect"]
        if want is None:
            assert rows == []                  # a pass is not shown
        else:
            assert len(rows) == 1 and rows[0]["status"] == want
            assert "untick Unpark on connect" in rows[0]["fix"]
            assert rows[0]["who"] == "jeremy"


class _Key:
    def __init__(self, vals):
        self.vals = vals

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeReg:
    HKEY_LOCAL_MACHINE = "HKLM"

    def __init__(self, vals):
        self.vals = vals
        self.opened = []

    def OpenKey(self, root, path):
        self.opened.append((root, path))
        return _Key(self.vals)

    def QueryValueEx(self, key, name):
        if name not in key.vals:
            raise OSError(name)
        return key.vals[name], 1


def test_registry_read_is_read_only_and_feeds_the_thesky_section(monkeypatch):
    from photonscript.scheduler import phd2_profile_store as ps
    from photonscript.scheduler import thesky_audit as ta
    reg = _FakeReg({"UnparkOnConnect": "True", "UseDirectGuide": "False",
                    "CanGetPierSide": "True"})
    monkeypatch.setattr(ps, "_winreg", lambda: reg)
    a = ps.read_ascom()
    assert a["ascom_unpark_on_connect"]["value"] is True
    assert a["ascom_unpark_on_connect"]["verified"]
    assert a["ascom_unpark_on_connect"]["location"].endswith(
        "ASCOM.SoftwareBisque.Telescope\\UnparkOnConnect")
    assert all(root == "HKLM" for root, _p in reg.opened)
    monkeypatch.setattr(ta, "collect_thesky", lambda cfg, client=None: (
        {"time_zone": -7, "mount_parked": True}, {"ok": True, "note": "read"}))
    settings, live, meta = sl.collect_thesky(None)
    assert meta["ok"] and live == {"mount_parked": True}
    assert settings["ascom_driver"] == {"unpark_on_connect": True,
                                        "use_direct_guide": False,
                                        "can_get_pier_side": True}
    # off Windows / key missing: None, judged unknown (never a pass)
    monkeypatch.setattr(ps, "_winreg", lambda: None)
    assert sl.bisque_driver_flags()["unpark_on_connect"] is None
    src = (ROOT / "photonscript/scheduler/phd2_profile_store.py").read_text(encoding="utf-8")
    read_ascom = src.split("def read_ascom")[1].split("\ndef ")[0]
    for bad in ("SetValue", "CreateKey", "DeleteValue"):
        assert bad not in read_ascom


# ------------------------------------------------------------- 2. connect guard

def test_guard_skips_a_parked_mount_by_day(tmp_path):
    cfg = _cfg(tmp_path)
    p, s = _fns(parked=(True, "TheSky"))
    why = asyncio.run(ug.connect_skip_reason(cfg, _Armer("COMPLETE"), parked_fn=p, safe_fn=s))
    assert why == ug.REASON_PARKED == "mount parked by day"


def test_guard_skips_on_an_unsafe_roof(tmp_path):
    cfg = _cfg(tmp_path)
    for parked in ((False, "TheSky"), (None, "")):
        p, s = _fns(parked=parked, safe=False)
        why = asyncio.run(ug.connect_skip_reason(cfg, _Armer("ARMED"), parked_fn=p, safe_fn=s))
        assert why == ug.REASON_UNSAFE == "roof unsafe"


@pytest.mark.parametrize("parked,safe", [((False, "TheSky"), True), ((None, ""), None),
                                         ((False, "mount log"), None)])
def test_guard_connects_when_nothing_says_parked_or_unsafe(tmp_path, parked, safe):
    p, s = _fns(parked=parked, safe=safe)
    assert asyncio.run(ug.connect_skip_reason(_cfg(tmp_path), _Armer(), parked_fn=p,
                                              safe_fn=s)) is None


@pytest.mark.parametrize("state", ["RUNNING", "PAUSED_UNSAFE", "PAUSED_OPERATOR", "WATCHING"])
def test_guard_never_blocks_a_running_night(tmp_path, state):
    calls = []
    p, s = _fns(parked=(True, "TheSky"), safe=False, calls=calls)
    assert asyncio.run(ug.connect_skip_reason(_cfg(tmp_path), _Armer(state), parked_fn=p,
                                              safe_fn=s)) is None
    assert calls == []          # nothing even read


def test_guard_never_blocks_a_dispatch_and_can_be_turned_off(tmp_path):
    p, s = _fns(parked=(True, "TheSky"), safe=False)
    assert asyncio.run(ug.connect_skip_reason(
        _cfg(tmp_path), _Armer("ARMED", in_flight=True), parked_fn=p, safe_fn=s)) is None
    assert asyncio.run(ug.connect_skip_reason(
        _cfg(tmp_path, mount_connect_guard=False), _Armer(), parked_fn=p, safe_fn=s)) is None
    assert PhotonScriptConfig.model_fields["mount_connect_guard"].default is True


def test_guard_fails_open_on_an_unexpected_error(tmp_path):
    async def boom(cfg):
        raise RuntimeError("bad read")
    assert asyncio.run(ug.connect_skip_reason(_cfg(tmp_path), _Armer(),
                                              parked_fn=boom)) is None


def test_read_parked_prefers_thesky_then_the_mount_log(tmp_path, monkeypatch):
    from photonscript.shared import mount_log
    from photonscript.shared.phd2_store import append_jsonl, night_of
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(dm, "read_thesky", lambda c: {"parked": True, "tracking": False})
    assert asyncio.run(ug.read_parked(cfg)) == (True, "TheSky")
    monkeypatch.setattr(dm, "read_thesky", lambda c: None)   # TheSky's mount not connected
    assert asyncio.run(ug.read_parked(cfg)) == (None, "")
    now = datetime.utcnow()
    append_jsonl(mount_log.log_path(cfg, night_of(cfg, now)),
                 {"t": now.replace(microsecond=0).isoformat() + "Z", "rig": "rc16",
                  "ra": 10.0, "dec": 20.0, "parked": True, "tracking": False,
                  "slewing": False, "why": "park"})
    assert asyncio.run(ug.read_parked(cfg)) == (True, "mount log")


def _fake_nina_devices(monkeypatch, connected: set, connects: list):
    async def _connected(config, device, timeout=8):
        return device in connected, {"Connected": device in connected}, ""

    async def _connect(config, device, timeout=20):
        connects.append(device)
        connected.add(device)
        return ""

    async def _sleep(_s=0, result=None):
        return result
    monkeypatch.setattr(preflight, "_connected", _connected)
    monkeypatch.setattr(preflight, "_nina_connect", _connect)
    monkeypatch.setattr(preflight.asyncio, "sleep", _sleep)


def test_ensure_connected_skips_only_a_disconnected_mount(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    connects: list = []
    _fake_nina_devices(monkeypatch, set(), connects)

    async def parked(c):
        return True, "TheSky"
    monkeypatch.setattr(ug, "read_parked", parked)
    monkeypatch.setattr(ug, "current_armer", lambda: _Armer("COMPLETE"))
    ok, _p, err = asyncio.run(preflight._ensure_connected(cfg, "mount"))
    assert (ok, err) == (False, "skipped (mount parked by day)") and connects == []
    # other devices are untouched by the guard
    ok, _p, err = asyncio.run(preflight._ensure_connected(cfg, "camera"))
    assert ok and connects == ["camera"]
    # a night running: the mount connects as before
    monkeypatch.setattr(ug, "current_armer", lambda: _Armer("RUNNING"))
    ok, _p, err = asyncio.run(preflight._ensure_connected(cfg, "mount"))
    assert ok and connects == ["camera", "mount"]


def test_ensure_connected_leaves_an_already_connected_mount_alone(tmp_path, monkeypatch):
    connects: list = []
    _fake_nina_devices(monkeypatch, {"mount"}, connects)
    called = []

    async def guard(cfg, armer=None, **k):
        called.append(1)
        return ug.REASON_PARKED
    monkeypatch.setattr(ug, "connect_skip_reason", guard)
    ok, _p, err = asyncio.run(preflight._ensure_connected(_cfg(tmp_path), "mount"))
    assert ok and err == "" and not called and not connects


def test_connect_all_skips_the_parked_mount_last_and_prearm_shows_info(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    a = Armer(cfg)
    connects: list = []
    _fake_nina_devices(monkeypatch, set(), connects)
    order = []

    async def safe(c):
        order.append(("safe read", list(connects)))
        return False
    monkeypatch.setattr(ug, "read_parked", lambda c: asyncio.sleep(0, result=(None, "")))
    monkeypatch.setattr(ug, "read_safe", safe)
    monkeypatch.setattr(ug, "current_armer", lambda: a)
    a.state = "ARMED"                    # arm at noon: pre-config is hours away
    res = asyncio.run(a.connect_all_rigs())
    rc = res["rc16"]
    assert rc["mount"] == "skipped (roof unsafe)" and "mount" not in connects
    assert list(rc)[-1] == "mount"       # judged after the safety monitor
    assert "safetymonitor" in order[0][1]
    rows = {r["id"]: r for r in pa.device_rows(res)}
    assert rows["dev:rc16:mount"]["status"] == "info"
    assert rows["dev:rc16:mount"]["fix"] is None
    assert rows["dev:rc16:camera"]["status"] == "pass"


def test_preflight_and_the_night_sequence_still_connect_and_unpark():
    pf = (ROOT / "photonscript/scheduler/preflight.py").read_text(encoding="utf-8")
    assert 'Not connected on purpose: {err[9:-1]}' in pf
    assert "skipped (mount parked by day)"[9:-1] == "mount parked by day"
    # pre-config: the night's own start area connects the mount and unparks
    # once safe; that is NINA's sequence, never the connect guard
    seq = (ROOT / "photonscript/scheduler/nina_sequence_json.py").read_text(encoding="utf-8")
    start = seq.split('_connect("Safety Monitor"),')[1].split("_unpark(),")[0]
    assert '_connect("Mount")' in start and "_wait_until_safe()" in start
    assert "unpark_guard" not in seq


# ------------------------------------------------------------- 3. park sticks

def _stick_armer(tmp_path, monkeypatch, parked_reads, busy=()):
    a = Armer(_cfg(tmp_path))
    a.state = "COMPLETE"
    calls, pushes, steps = [], [], []
    reads = list(parked_reads)

    async def _ts():
        return reads.pop(0) if reads else True
    monkeypatch.setattr(a, "_thesky_parked", _ts)

    async def _nina(key, *x, **k):
        calls.append(key)
        return {"Success": True}
    monkeypatch.setattr(a, "_nina", _nina)

    async def _park_step():
        steps.append(1)
        return "park ok"
    monkeypatch.setattr(a, "_park_step", _park_step)

    async def _busy(cfg):
        return None if busy is None else list(busy)
    monkeypatch.setattr(dm, "nina_busy", _busy)

    async def notify(cfg, msg, **k):
        pushes.append((msg, k))
        return True
    return a, calls, pushes, steps, notify


def test_park_undone_is_reparked_and_pages_once(tmp_path, monkeypatch):
    a, calls, pushes, steps, notify = _stick_armer(tmp_path, monkeypatch, [False, True])
    res = asyncio.run(a._park_stick_check("dawn shutdown", "COMPLETE", delay_s=0,
                                          notify_fn=notify))
    assert res["result"] == "re-parked" and res["held"] is True
    assert calls == ["mount_disconnect", "mount_connect"] and steps == [1]
    assert len(pushes) == 1
    msg, k = pushes[0]
    assert k["priority"] == 1 and k["kind"] == "act" and k["safety"] is True
    assert "after the dawn shutdown park" in msg and "UNPARKED" in msg
    assert "it held" in msg and "untick Unpark on connect" in msg
    assert a.park_stick["result"] == "re-parked"


def test_park_undone_twice_is_pushed_not_fought(tmp_path, monkeypatch):
    a, calls, pushes, steps, notify = _stick_armer(tmp_path, monkeypatch, [False, False])
    res = asyncio.run(a._park_stick_check("unsafe stop", "COMPLETE", delay_s=0,
                                          notify_fn=notify))
    assert res["held"] is False and steps == [1] and len(pushes) == 1
    assert "UNPARKED AGAIN" in pushes[0][0]
    assert "then TheSky: Telescope > Park" in pushes[0][0]


def test_park_that_sticks_does_nothing(tmp_path, monkeypatch):
    a, calls, pushes, steps, notify = _stick_armer(tmp_path, monkeypatch, [True])
    res = asyncio.run(a._park_stick_check("dawn shutdown", "COMPLETE", delay_s=0,
                                          notify_fn=notify))
    assert res["result"] == "held" and not calls and not steps and not pushes


def test_park_stick_skips_when_the_armer_moved_on_or_nina_runs(tmp_path, monkeypatch):
    a, calls, pushes, steps, notify = _stick_armer(tmp_path, monkeypatch, [False])
    a.state = "RUNNING"            # re-armed and dispatched meanwhile
    res = asyncio.run(a._park_stick_check("make-safe", "DISARMED", delay_s=0,
                                          notify_fn=notify))
    assert res["result"].startswith("skip") and not calls and not pushes
    a, calls, pushes, steps, notify = _stick_armer(tmp_path / "b", monkeypatch, [False],
                                                   busy=["Unpark Scope"])
    res = asyncio.run(a._park_stick_check("dawn shutdown", "COMPLETE", delay_s=0,
                                          notify_fn=notify))
    assert "NINA #1 runs Unpark Scope" in res["result"] and not calls and not pushes
    a, calls, pushes, steps, notify = _stick_armer(tmp_path / "c", monkeypatch, [False])
    a.dispatch_started_at = datetime.utcnow()
    res = asyncio.run(a._park_stick_check("dawn shutdown", "COMPLETE", delay_s=0,
                                          notify_fn=notify))
    assert res["result"].startswith("skip") and not calls


async def test_every_photonscript_park_schedules_the_reread(tmp_path, monkeypatch):
    import photonscript.scheduler.armer as armer_mod
    monkeypatch.setattr(Armer, "_park_stick_s", 30.0)
    a = Armer(_cfg(tmp_path))
    seen = []

    async def _check(why, state0, delay_s=None, notify_fn=None):
        seen.append((why, state0))
        return {}
    monkeypatch.setattr(a, "_park_stick_check", _check)

    async def _nina(key, *x, **k):
        if key == "mount_info":
            return {"Response": {"Connected": True, "AtPark": False}}
        if key == "guider":
            return {"Response": {"Connected": False}}
        return {"Success": True}
    monkeypatch.setattr(a, "_nina", _nina)

    async def _parked():
        return True
    monkeypatch.setattr(a, "_park_verified", lambda: asyncio.sleep(0, result=(True, "TheSky")))
    monkeypatch.setattr(armer_mod, "notify", lambda *x, **k: asyncio.sleep(0, result=True))
    report = await a.make_safe()
    assert "park:ok" in report
    await asyncio.sleep(0)
    assert seen and seen[-1][0] == "make-safe"
    # the dawn shutdown's park step
    a._park_stick_task = None
    a.state = "COMPLETE"
    a._park_stick_later("dawn shutdown")
    await asyncio.sleep(0)
    assert seen[-1] == ("dawn shutdown", "COMPLETE")
    # off: nothing scheduled
    monkeypatch.setattr(Armer, "_park_stick_s", 0.0)
    a._park_stick_task = None
    a._park_stick_later("dawn shutdown")
    await asyncio.sleep(0)
    assert len(seen) == 2


def test_park_hooks_are_wired():
    src = (ROOT / "photonscript/scheduler/armer.py").read_text(encoding="utf-8")
    for why in ("dawn shutdown", "make-safe", "paused-unsafe park", "unsafe stop"):
        assert f'self._park_stick_later("{why}")' in src
    from photonscript.scheduler.armer import NINA_PATHS
    assert NINA_PATHS["mount_disconnect"] == ["/equipment/mount/disconnect"]


# ------------------------------------------------------------- 4. unsafe watch

def _unsafe_run(cfg, safe_seq, mount, armer="DISARMED", start=UNSAFE_AT, step_min=1.0,
                watch=None):
    pushes = []
    watch = watch or ug.UnsafeWatch()

    async def notify(c, msg, **k):
        pushes.append((msg, k))
        return True
    res = []
    for i, safe in enumerate(safe_seq):
        async def sf(c, v=safe):
            return v

        async def mf(c):
            return dict(mount) if mount else None
        res.append(asyncio.run(ug.unsafe_tick(cfg, armer, now=start + timedelta(minutes=i * step_min),
                                              safe_fn=sf, mount_fn=mf, notify=notify,
                                              watch=watch)))
    return res, pushes, watch


UNPARKED = {"parked": False, "tracking": True, "source": "TheSky"}


@pytest.mark.parametrize("armer", ["DISARMED", "COMPLETE", "ARMED", "ERROR"])
def test_safe_to_unsafe_with_the_mount_unparked_pages_once(tmp_path, armer):
    res, pushes, _w = _unsafe_run(_cfg(tmp_path), [True, False, False, False], UNPARKED,
                                  armer=armer)
    assert [r["state"] for r in res] == ["safe", "alert", "unsafe", "unsafe"]
    assert len(pushes) == 1
    msg, k = pushes[0]
    assert k["priority"] == 1 and k["kind"] == "act" and k["safety"] is True
    assert "UNSAFE" in msg and "UNPARKED, TRACKING" in msg and f"armer {armer}" in msg


def test_unsafe_flap_is_held_then_pages_again_later(tmp_path):
    cfg = _cfg(tmp_path)
    res, pushes, w = _unsafe_run(cfg, [True, False, True, False], UNPARKED)
    assert [r["state"] for r in res] == ["safe", "alert", "safe", "held"]
    assert len(pushes) == 1
    res, pushes2, _ = _unsafe_run(cfg, [True, False], UNPARKED,
                                  start=UNSAFE_AT + timedelta(minutes=ug.UNSAFE_REPAGE_MIN + 5),
                                  watch=w)
    assert res[-1]["state"] == "alert" and len(pushes2) == 1


def test_unsafe_with_the_mount_parked_or_no_baseline_is_quiet(tmp_path):
    res, pushes, _ = _unsafe_run(_cfg(tmp_path), [True, False],
                                 {"parked": True, "tracking": False, "source": "TheSky"})
    assert res[-1]["state"] == "unsafe" and not pushes
    res, pushes, _ = _unsafe_run(_cfg(tmp_path / "b"), [True, False], None)
    assert not pushes                      # mount unknown
    res, pushes, _ = _unsafe_run(_cfg(tmp_path / "c"), [False, False], UNPARKED)
    assert not pushes                      # never seen safe (a restart by day)
    res, pushes, _ = _unsafe_run(_cfg(tmp_path / "d"), [True, None, False], UNPARKED)
    assert res[1]["state"] == "unknown" and len(pushes) == 1   # unreadable keeps the baseline


def test_a_running_night_owns_the_unsafe_response(tmp_path):
    cfg = _cfg(tmp_path)
    w = ug.UnsafeWatch()
    res, pushes, w = _unsafe_run(cfg, [True], UNPARKED, watch=w)
    res, pushes, w = _unsafe_run(cfg, [False], UNPARKED, armer="RUNNING", watch=w)
    assert res[0]["state"] == "skip" and not pushes and w.last_safe is None
    res, pushes, w = _unsafe_run(cfg, [False], UNPARKED, armer="COMPLETE", watch=w)
    assert not pushes                      # the night's end is not a transition


def test_unsafe_watch_modes(tmp_path, monkeypatch):
    res, pushes, _ = _unsafe_run(_cfg(tmp_path, nina_watch_mode="off"), [True, False], UNPARKED)
    assert res[-1]["state"] == "off" and not pushes
    held = []
    from photonscript.shared import pushover
    monkeypatch.setattr(pushover, "record", lambda c, m, **k: held.append(m))
    res, pushes, _ = _unsafe_run(_cfg(tmp_path / "b", nina_watch_mode="panel"),
                                 [True, False], UNPARKED)
    assert res[-1]["state"] == "alert" and not pushes and len(held) == 1


def test_unsafe_watch_is_wired_into_the_nina_watchdog_loop():
    src = (ROOT / "photonscript/scheduler/nina_watchdog.py").read_text(encoding="utf-8")
    assert "unpark_guard.unsafe_tick(cfg, state)" in src
    g = (ROOT / "photonscript/scheduler/unpark_guard.py").read_text(encoding="utf-8")
    unsafe = g.split("# ------------------------------------------------------------------ unsafe watch")[1]
    for bad in ("mount_park", "/park", "/connect", "/disconnect", ".Park(", "SetTracking"):
        assert bad not in unsafe


# ------------------------------------------------------------- 5. daytime watch

AFTERNOON = datetime(2026, 11, 14, 21, 0)      # 14:00 MST, night 2026-11-14
MORNING = datetime(2026, 11, 14, 17, 50)       # 10:50 MST, night 2026-11-13
TRACKING = {"parked": False, "tracking": True, "alt": 32.2, "az": 0.1, "source": "TheSky"}


def _day_run(cfg, armer="DISARMED", now=AFTERNOON, mount=TRACKING, busy=(), state=None,
             sun=True):
    pushes = []

    async def read(c):
        return dict(mount) if mount else None

    async def busy_fn(c):
        return None if busy is None else list(busy)

    async def notify(c, msg, **k):
        pushes.append((msg, k))
        return True
    st = state if state is not None else {"paged": set()}
    res = asyncio.run(dm.tick(cfg, armer, now=now, read=read, busy_fn=busy_fn,
                              notify=notify, state=st, sun=sun))
    return res, pushes, st


def _events(tmp_path):
    out = []
    for p in (tmp_path / "data" / "runs").glob("*_events.jsonl"):
        out += [json.loads(x) for x in p.read_text().splitlines()]
    return [e for e in out if e.get("kind") == "dawn_mount"]


def test_2026_11_14_tracking_by_day_pages_once_a_day(tmp_path):
    cfg = _cfg(tmp_path)
    res, pushes, st = _day_run(cfg, armer="COMPLETE")
    assert res["state"] == "alert" and res["night"] == "2026-11-14" and res["day"]
    assert len(pushes) == 1
    msg, k = pushes[0]
    assert k["priority"] == 1 and k["safety"] is True
    assert "TRACKING by day" in msg
    assert "TheSky: Telescope > Tracking off, or park; untick UnparkOnConnect" in msg
    assert [e["value"] for e in _events(tmp_path)] == [dm.DAY_VALUE]
    res, pushes, _ = _day_run(cfg, state=st)
    assert res["state"] == "paged" and not pushes
    res, pushes, _ = _day_run(cfg)                 # a restart: the event is the latch
    assert res["state"] == "paged" and not pushes


def test_this_mornings_after_dawn_page_counts_for_the_day(tmp_path):
    cfg = _cfg(tmp_path)
    res, pushes, _ = _day_run(cfg, now=MORNING, sun=None)
    assert res["state"] == "alert" and res["night"] == "2026-11-13" and not res.get("day")
    res, pushes, _ = _day_run(cfg)                 # same local day, afternoon
    assert res["state"] == "paged" and not pushes
    # the next day pages again
    res, pushes, _ = _day_run(cfg, now=AFTERNOON + timedelta(days=1))
    assert res["state"] == "alert" and len(pushes) == 1


@pytest.mark.parametrize("armer", ["ARMED", "RUNNING", "PAUSED_UNSAFE", "WATCHING"])
def test_by_day_an_armed_night_is_left_alone(tmp_path, armer):
    res, pushes, _ = _day_run(_cfg(tmp_path), armer=armer)
    assert res["state"] == "skip" and not pushes


def test_by_day_parked_sun_down_or_nina_busy_is_quiet(tmp_path):
    res, pushes, _ = _day_run(_cfg(tmp_path), mount={"parked": True, "tracking": False,
                                                     "source": "TheSky"})
    assert res["state"] == "ok" and not pushes
    res, pushes, _ = _day_run(_cfg(tmp_path), sun=False)
    assert res["state"] == "idle" and not pushes
    res, pushes, _ = _day_run(_cfg(tmp_path), busy=["Take Many Exposures"])
    assert res["state"] == "skip" and not pushes
    res, pushes, _ = _day_run(_cfg(tmp_path, dawn_mount_check_mode="off"))
    assert res["state"] == "off" and not pushes


def test_sun_up_by_the_site():
    cfg = PhotonScriptConfig(_env_file=None)
    assert dm.sun_up(cfg, AFTERNOON) is True
    assert dm.sun_up(cfg, datetime(2026, 11, 15, 6, 0)) is False    # 23:00 MST
    assert dm.prev_night("2026-11-01") == "2026-10-31"


# ------------------------------------------------------------- hygiene

@pytest.mark.parametrize("rel", [
    "photonscript/scheduler/unpark_guard.py",
    "photonscript/scheduler/dawn_mount_check.py",
    "tests/test_scheduler/test_ps238_unpark_guard.py"])
def test_ascii_no_em_dash(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert not [ln for ln in text.splitlines() if any(ord(c) > 127 for c in ln)]
