"""Piggy guard: the Piggy-600 (NINA #2) taking lights while the RC16 mount is
parked, not tracking, offline or unknown (2026-10-09: NINA #2 imaged an
unknown field for 3.5 h after NINA #1 lost the mount).

  1. mount_log.state_at: tracking / parked / idle / offline marker / stale;
  2. tick(): grace, one push per night with what NINA #2 shoots, a clear
     event, flats / darks and a tracking mount never page, panel / off,
     piggyback disabled, the real mount log file;
  3. morning report line, config fields, the PS-150 loop hook, observe only.
"""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.scheduler import morning_report as mr
from photonscript.scheduler import piggy_guard as pg
from photonscript.shared import mount_log
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.night_events import events_path
from photonscript.shared.phd2_store import night_of, read_jsonl

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 9, 9, 10, 0)      # 03:10 local, the 10-08 night


def _cfg(tmp_path, **kw):
    kw.setdefault("piggyback_enabled", True)
    kw.setdefault("piggyback_nina_base_url", "http://localhost:1889/v2/api")
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                              observatory_tz="America/Denver", **kw)


def _line(dt, **kw):
    r = {"dt": dt, "ra": 10.0, "dec": 41.0, "alt": 50.0, "az": 100.0,
         "slewing": False, "tracking": True, "parked": False, "why": "heartbeat"}
    r.update(kw)
    return r


# ------------------------------------------------------------- 1. state_at

def test_state_at():
    lines = [_line(T0, why="start"),
             _line(T0 + timedelta(minutes=10), tracking=False, parked=True, why="park"),
             {"dt": T0 + timedelta(minutes=20), "offline": True, "ra": None, "dec": None,
              "tracking": False, "parked": None, "why": "offline"},
             _line(T0 + timedelta(minutes=30), tracking=False, why="reconnect")]
    assert mount_log.state_at(lines, T0 - timedelta(minutes=1))[0] == "unknown"
    assert mount_log.state_at(lines, T0 + timedelta(minutes=2))[0] == "tracking"
    assert mount_log.state_at(lines, T0 + timedelta(minutes=9))[0] == "unknown"  # stale
    assert mount_log.state_at(lines, T0 + timedelta(minutes=19))[0] == "parked"
    st, ln = mount_log.state_at(lines, T0 + timedelta(minutes=25))
    assert st == "offline" and ln["why"] == "offline"
    assert mount_log.state_at(lines, T0 + timedelta(hours=3))[0] == "idle"  # holds
    assert mount_log.state_at([], T0)[0] == "unknown"


# ------------------------------------------------------------- 2. tick

class _Sim:
    def __init__(self, mount="offline", leaf="Take Exposure", target="M31"):
        self.mount, self.leaf, self.target = mount, leaf, target
        self.pushes, self.piggy_reads = [], 0

    def mount_fn(self, cfg, now):
        return self.mount, {"dt": T0 - timedelta(minutes=2), "why": self.mount}

    async def piggy_fn(self, cfg):
        self.piggy_reads += 1
        if self.leaf is None:
            return None
        return {"leaf": self.leaf, "target": self.target, "exposure_s": 300.0,
                "path": ["Targets", self.target, self.leaf]}

    async def notify(self, cfg, msg, title="", priority=0, **_kw):
        self.pushes.append((title, msg, priority))
        return True


def _run(cfg, sim, minutes, start=0, mon=None, window=True):
    mon = mon or pg.PiggyGuard()
    out = []
    for i in range(start, start + minutes):
        out.append(asyncio.run(pg.tick(cfg, now=T0 + timedelta(minutes=i),
                                       window=window, mount=sim.mount_fn,
                                       piggy=sim.piggy_fn, notify=sim.notify,
                                       monitor=mon)))
    return out, mon


def _events(cfg):
    return [e for e in read_jsonl(events_path(cfg, night_of(cfg, T0)))
            if e.get("kind") == "piggy_guard"]


def test_pages_once_after_grace_with_what_nina2_shoots(tmp_path):
    cfg = _cfg(tmp_path)
    sim = _Sim()
    out, mon = _run(cfg, sim, 60)
    assert [o["state"] for o in out[:11]] == ["watching"] * 10 + ["alert"]
    assert len(sim.pushes) == 1
    title, msg, pri = sim.pushes[0]
    assert title == pg.TITLE and pri == 1
    assert "NINA #2 (Piggy-600)" in msg and "LIGHTS" in msg and "OFFLINE" in msg
    assert "M31: 'Take Exposure' 300 s" in msg and "changed nothing" in msg
    assert msg.isascii()
    ev = _events(cfg)
    assert [e["value"] for e in ev] == ["alert"]
    assert ev[0]["mount"] == "offline" and ev[0]["target"] == "M31"
    # the mount tracks again: a clear event; a second spell the same night
    # does not push again
    sim.mount = "tracking"
    _run(cfg, sim, 1, start=60, mon=mon)
    sim.mount = "parked"
    _run(cfg, sim, 30, start=61, mon=mon)
    assert len(sim.pushes) == 1
    assert [e["value"] for e in _events(cfg)] == ["alert", "clear"]


def test_tracking_mount_and_non_lights_never_page(tmp_path):
    cfg = _cfg(tmp_path)
    sim = _Sim(mount="tracking")
    out, _ = _run(cfg, sim, 30)
    assert sim.pushes == [] and sim.piggy_reads == 0      # NINA #2 not even read
    assert {o["state"] for o in out} == {"ok"}
    for leaf in ("Take Sky Flat", "Take Dark", "Bias exposure", "Wait for Time", None):
        sim = _Sim(mount="parked", leaf=leaf)
        _run(cfg, sim, 30)
        assert sim.pushes == [], leaf
    sim = _Sim(mount="slewing")
    _run(cfg, sim, 30)
    assert sim.pushes == []


def test_short_spell_resets_the_grace(tmp_path):
    cfg = _cfg(tmp_path)
    sim = _Sim(mount="idle")
    _, mon = _run(cfg, sim, 8)
    sim.mount = "tracking"
    _run(cfg, sim, 1, start=8, mon=mon)
    sim.mount = "idle"
    out, _ = _run(cfg, sim, 8, start=9, mon=mon)
    assert sim.pushes == [] and out[-1]["state"] == "watching"


def test_panel_off_disabled_and_daytime(tmp_path):
    cfg = _cfg(tmp_path, piggy_guard_mode="panel")
    sim = _Sim()
    _run(cfg, sim, 20)
    assert sim.pushes == [] and [e["value"] for e in _events(cfg)] == ["alert"]
    for kw in ({"piggy_guard_mode": "off"}, {"piggyback_enabled": False}):
        c = _cfg(tmp_path / str(len(kw)) / next(iter(kw)), **kw)
        sim = _Sim()
        out, _ = _run(c, sim, 20)
        assert sim.pushes == [] and {o["state"] for o in out} == {"off"}
    sim = _Sim()
    out, _ = _run(_cfg(tmp_path / "day"), sim, 20, window=False)
    assert sim.pushes == [] and sim.piggy_reads == 0


def test_grace_config_and_night_roll(tmp_path):
    cfg = _cfg(tmp_path, piggy_guard_grace_min=3)
    sim = _Sim()
    out, mon = _run(cfg, sim, 5)
    assert out[3]["state"] == "alert"
    _run(cfg, sim, 5, start=24 * 60, mon=mon)       # the next night pages again
    assert len(sim.pushes) == 2
    assert pg.grace_min(PhotonScriptConfig(_env_file=None, piggy_guard_grace_min=-1)) == 0.0
    assert pg.mode(PhotonScriptConfig(_env_file=None, piggy_guard_mode="junk")) == "alert"


def test_reads_the_real_mount_log(tmp_path):
    """The default mount reader: the RC16 agent's offline marker in
    runs/<night>_mount.jsonl."""
    cfg = _cfg(tmp_path, piggy_guard_grace_min=0)
    ml = mount_log.MountLogger(cfg)
    ml.observe({"RightAscension": 0.7, "Declination": 41.2, "Altitude": 60.0,
                "Azimuth": 30.0, "TrackingEnabled": True, "AtPark": False,
                "Slewing": False}, now=T0 - timedelta(minutes=30))
    ml.observe({"Connected": False}, now=T0 - timedelta(minutes=5))
    assert pg.mount_now(cfg, T0)[0] == "offline"
    sim = _Sim()
    out = asyncio.run(pg.tick(cfg, now=T0, window=True, piggy=sim.piggy_fn,
                              notify=sim.notify, monitor=pg.PiggyGuard()))
    assert out["state"] == "alert" and out["mount"] == "offline"
    assert pg.mount_now(_cfg(tmp_path / "empty"), T0)[0] == "unknown"


# ------------------------------------------------------------- 3. report etc.

def test_morning_report_line(tmp_path):
    cfg = _cfg(tmp_path)
    sim = _Sim()
    _, mon = _run(cfg, sim, 15)
    sim.mount = "tracking"
    _run(cfg, sim, 1, start=200, mon=mon)
    line = pg.morning_line(cfg, night_of(cfg, T0))
    assert line == ("Piggy guard: NINA #2 took lights 03:10-06:30 local while the "
                    "RC16 mount was offline (M31: 'Take Exposure' 300 s)")
    assert pg.morning_line(_cfg(tmp_path / "none"), night_of(cfg, T0)) is None
    card = mr.report_card(cfg, night_of(cfg, T0), projects=[], calibration=False,
                          completeness=False)
    assert card["piggy_guard"] == line and line in card["lines"]
    assert mr.card_line_priority(line) == mr.PRI_RIG


def test_config_fields_and_loop_hook():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    c = PhotonScriptConfig(_env_file=None)
    assert c.piggy_guard_mode == "alert" and c.piggy_guard_grace_min == 10.0
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    assert by_env["PS_PIGGY_GUARD_MODE"][0] == "piggy_guard_mode"
    assert by_env["PS_PIGGY_GUARD_GRACE_MIN"][4] == "float"
    src = (ROOT / "photonscript/scheduler/nina_watchdog.py").read_text(encoding="utf-8")
    body = src[src.index("async def run_watchdog"):]
    assert body.index("await tick(cfg, state") < body.index("piggy_guard.tick(cfg)")


def test_observe_only():
    src = (ROOT / "photonscript/scheduler/piggy_guard.py").read_text(encoding="utf-8")
    assert src.isascii()
    for bad in (".post(", "sequence_stop", "start_sequence", "park(", "nina_warm"):
        assert bad not in src, bad
