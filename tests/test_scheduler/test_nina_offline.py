"""NINA drops offline mid-recipe or mid-night (2026-10-09: an armed TPoint
recipe night lost NINA #1 a second time at 03:07:58Z; PS-150 had already
paged "not running" once that night, so nothing paged, the armer ended up
DISARMED and the Piggy-600 kept imaging an unknown field for 3.5 h).

  1. one priority-1 page per engaged outage after the grace, with what was
     running and the Piggy note; a recovery note; a second outage the same
     night pages again (the 10-09 miss);
  2. engagement: RUNNING / WATCHING / a dispatched recipe, kept when the
     armer goes DISARMED during the outage; nothing when not engaged;
  3. PS-150 leaves the push to this watch for an owned rig (chip + events
     stay), and pushes as before otherwise;
  4. config defaults, System page fields, morning report, observe only.
"""
import asyncio
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.scheduler import nina_offline as no
from photonscript.scheduler import nina_watchdog as nw
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.night_events import events_path
from photonscript.shared.phd2_store import night_of, read_jsonl

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 9, 3, 7, 58)      # 21:07 local, 2026-10-08 night
RECIPE = {"id": "tpoint", "label": "TPoint mapping", "status": "dispatched"}


def _cfg(tmp_path, **kw):
    kw.setdefault("nina_base_url", "http://localhost:1888/v2/api")
    kw.setdefault("piggyback_nina_base_url", "http://localhost:1889/v2/api")
    kw.setdefault("piggyback_enabled", True)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                              observatory_tz="America/Denver", **kw)


class _Sim:
    """Scripted NINA APIs + recorded pushes."""

    def __init__(self, rc16=True, piggy=True, piggy_leaf="Take Exposure"):
        self.up = {"1888": rc16, "1889": piggy}
        self.piggy_leaf = piggy_leaf
        self.pushes = []

    async def api(self, base):
        ok = self.up["1889" if "1889" in base else "1888"]
        return ok, "" if ok else "ConnectError: All connection attempts failed"

    async def leaf(self, base):
        return self.piggy_leaf, 300.0

    async def notify(self, cfg, msg, title="", priority=0):
        self.pushes.append((title, msg, priority))
        return True

    def reads(self, rig):
        if rig == "rc16":
            return {"seq_leaf": "Slew, center and rotate", "mount_parked": False,
                    "mount_tracking": True}
        return {"seq_leaf": self.piggy_leaf}


def _run(cfg, sim, mon, minutes, armer="RUNNING", recipe=RECIPE, start=0):
    out = None
    for i in range(start, start + minutes):
        out = asyncio.run(no.tick(cfg, armer, recipe, now=T0 + timedelta(minutes=i),
                                  api=sim.api, leaf=sim.leaf, notify=sim.notify,
                                  monitor=mon, last_reads=sim.reads))
    return out


# ------------------------------------------------------------- 1. pages

def test_outage_pages_once_after_grace_with_context_and_piggy_note(tmp_path):
    cfg = _cfg(tmp_path)
    sim, mon = _Sim(), no.OfflineWatch()
    _run(cfg, sim, mon, 2)                     # both up: context remembered
    sim.up["1888"] = False
    out = _run(cfg, sim, mon, 3, start=2)      # 0, 1, 2 min down
    assert sim.pushes == [] and out["rc16"] == "down"
    out = _run(cfg, sim, mon, 5, start=5)      # 3 min: page, then quiet
    assert out["rc16"] == "down"
    assert len(sim.pushes) == 1
    title, msg, prio = sim.pushes[0]
    assert title == no.TITLE and prio == 1
    assert msg.startswith("NINA #1 (RC16) OFFLINE")
    assert "armer was RUNNING with the TPoint mapping recipe (dispatched)" in msg
    assert "'Slew, center and rotate'" in msg and "Mount last seen tracking" in msg
    assert "NINA #2 (Piggy-600) is still imaging ('Take Exposure')" in msg
    assert "the Piggy is imaging an unknown field" in msg
    assert "restarts nothing" in msg and "\u2014" not in msg
    ev = [e for e in read_jsonl(events_path(cfg, night_of(cfg, T0)))
          if e.get("kind") == "nina_offline"]
    assert [e["value"] for e in ev] == ["down"] and ev[0]["piggy_imaging"] is True


def test_recovery_note_and_second_outage_pages_again(tmp_path):
    cfg = _cfg(tmp_path)
    sim, mon = _Sim(), no.OfflineWatch()
    _run(cfg, sim, mon, 1)
    sim.up["1888"] = False
    _run(cfg, sim, mon, 4, start=1)
    sim.up["1888"] = True
    out = _run(cfg, sim, mon, 1, start=5)
    assert out["rc16"] == "recovered" and "rc16" in mon.owned()
    assert len(sim.pushes) == 2
    assert "answers again after 4 min offline" in sim.pushes[1][1]
    assert sim.pushes[1][2] == 0
    _run(cfg, sim, mon, 1, start=6)
    assert mon.owned() == set()
    sim.up["1888"] = False                     # the 03:07 second drop
    _run(cfg, sim, mon, 4, start=7)
    assert len(sim.pushes) == 3 and "OFFLINE" in sim.pushes[2][1]


def test_piggy_not_imaging_or_down_has_no_unknown_field_note(tmp_path):
    cfg = _cfg(tmp_path)
    sim, mon = _Sim(piggy_leaf="Wait for Time Span"), no.OfflineWatch()
    _run(cfg, sim, mon, 1)
    sim.up["1888"] = False
    _run(cfg, sim, mon, 4, start=1)
    assert "unknown field" not in sim.pushes[0][1]
    sim2, mon2 = _Sim(), no.OfflineWatch()
    _run(cfg, sim2, mon2, 1)
    sim2.up["1888"] = sim2.up["1889"] = False
    _run(cfg, sim2, mon2, 4, start=1)
    texts = [p[1] for p in sim2.pushes]
    assert len(texts) == 2 and not any("unknown field" in t for t in texts)
    assert any(t.startswith("NINA #2 (Piggy-600) OFFLINE") for t in texts)


# ------------------------------------------------------------- 2. engagement

def test_engaged_outage_survives_disarm_and_unengaged_is_quiet(tmp_path):
    cfg = _cfg(tmp_path)
    sim, mon = _Sim(), no.OfflineWatch()
    _run(cfg, sim, mon, 1)
    sim.up["1888"] = False
    _run(cfg, sim, mon, 1, start=1)            # down while RUNNING
    _run(cfg, sim, mon, 4, start=2, armer="DISARMED", recipe=None)
    assert len(sim.pushes) == 1                # still pages
    # never armed tonight: nothing (PS-150 covers an unarmed NINA)
    sim2, mon2 = _Sim(rc16=False), no.OfflineWatch()
    _run(cfg, sim2, mon2, 10, armer="DISARMED", recipe=None)
    assert sim2.pushes == [] and mon2.owned() == set()
    # a dispatched recipe alone engages (armer state of a sideload watch)
    sim3, mon3 = _Sim(rc16=False), no.OfflineWatch()
    _run(cfg, sim3, mon3, 4, armer="ARMED", recipe=RECIPE)
    assert len(sim3.pushes) == 1
    # WATCHING engages; PAUSED_UNSAFE alone does not
    sim4, mon4 = _Sim(rc16=False), no.OfflineWatch()
    _run(cfg, sim4, mon4, 4, armer="WATCHING", recipe=None)
    assert len(sim4.pushes) == 1
    sim5, mon5 = _Sim(rc16=False), no.OfflineWatch()
    _run(cfg, sim5, mon5, 6, armer="PAUSED_UNSAFE", recipe=None)
    assert sim5.pushes == []


def test_outage_just_after_running_ended_is_engaged(tmp_path):
    cfg = _cfg(tmp_path)
    sim, mon = _Sim(), no.OfflineWatch()
    _run(cfg, sim, mon, 1)                     # RUNNING at T0
    sim.up["1888"] = False
    _run(cfg, sim, mon, 5, start=3, armer="ERROR", recipe=None)
    assert len(sim.pushes) == 1
    assert "armer was RUNNING" in sim.pushes[0][1]


def test_off_and_grace(tmp_path):
    sim, mon = _Sim(rc16=False), no.OfflineWatch()
    out = _run(_cfg(tmp_path, nina_offline_alert=False), sim, mon, 10)
    assert sim.pushes == [] and out["rc16"] == "off" and mon.owned() == set()
    sim2, mon2 = _Sim(rc16=False), no.OfflineWatch()
    _run(_cfg(tmp_path, nina_offline_grace_min=10), sim2, mon2, 10)
    assert sim2.pushes == []
    _run(_cfg(tmp_path, nina_offline_grace_min=10), sim2, mon2, 1, start=10)
    assert len(sim2.pushes) == 1


def test_recipe_live_uses_tonights_recipe():
    class A:
        recipe = {"status": "dispatched", "label": "x"}

        def _recipe_tonight(self):
            return None                          # an old night's recipe
    assert no.recipe_live(A()) is None

    class B:
        recipe = None

        def _recipe_tonight(self):
            return {"status": "started", "label": "TPoint"}
    assert no.recipe_live(B())["label"] == "TPoint"

    class C:
        recipe = {"status": "done"}
    assert no.recipe_live(C()) is None


# ------------------------------------------------------------- 3. PS-150

def _down_read(cfg, rig):
    async def read(c, r):
        return {"api_ok": False, "api_error": "ConnectError", "process": False,
                "pid": 7984, "log_file": "x.log", "log_mtime": T0,
                "log_size": 10, "closed_at": None, "seq_leaf": None,
                "seq_exposure_s": None}
    return read


def _ps150(cfg, n, owned, start=0, mon=None, read=None):
    pushes = []

    async def notify(c, msg, title="", priority=0):
        pushes.append((title, msg, priority))
    mon = mon or nw.NinaWatch()
    for i in range(start, start + n):
        asyncio.run(nw.tick(cfg, "RUNNING", now=T0 + timedelta(minutes=i),
                            read=read or _down_read(cfg, "rc16"), notify=notify,
                            window=True, monitor=mon, owned=owned))
    return pushes, mon


def test_ps150_leaves_the_page_to_an_owned_rig(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, piggyback_enabled=False)
    held = []
    monkeypatch.setattr("photonscript.shared.pushover.record",
                        lambda c, m, **kw: held.append((m, kw.get("reason"))))
    pushes, mon = _ps150(cfg, 5, {"rc16"})
    assert pushes == [] and len(held) == 1 and held[0][1] == "nina-offline-owns"
    assert mon.view()["rigs"]["rc16"]["state"] == "not_running"   # chip stays
    ev = [e for e in read_jsonl(events_path(cfg, night_of(cfg, T0)))
          if e.get("kind") == "nina_watch"]
    assert [e["value"] for e in ev] == ["not_running"]

    async def ok(c, r):
        return {"api_ok": True, "api_error": "", "process": True, "pid": 1,
                "log_file": "x.log", "log_mtime": T0, "log_size": 11,
                "closed_at": None, "seq_leaf": "", "seq_exposure_s": None}
    more, _ = _ps150(cfg, 1, {"rc16"}, start=5, mon=mon, read=ok)
    assert more == [] and len(held) == 2          # recovery recorded, not pushed
    # not owned: PS-150 pushes as before
    plain, _ = _ps150(_cfg(tmp_path / "b", piggyback_enabled=False), 5, set())
    assert len(plain) == 1 and "NOT RUNNING" in plain[0][1]


def test_run_watchdog_runs_offline_watch_first(monkeypatch):
    src = (ROOT / "photonscript/scheduler/nina_watchdog.py").read_text()
    body = src[src.index("async def run_watchdog"):]
    assert body.index("nina_offline.tick") < body.index("await tick(cfg, state")
    assert "owned=owned" in body


# ------------------------------------------------------------- 4. config etc.

def test_config_defaults_and_fields():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    cfg = PhotonScriptConfig(_env_file=None)
    assert cfg.nina_offline_alert is True and cfg.nina_offline_grace_min == 3.0
    by = {f[0]: f for f in _CONFIG_FIELDS}
    assert by["nina_offline_alert"][1] == "PS_NINA_OFFLINE_ALERT"
    assert by["nina_offline_alert"][3] == "Nanny / Alerts"
    assert by["nina_offline_grace_min"][4] == "float"


def test_morning_report_counts_offline_as_a_stall(tmp_path):
    from photonscript.scheduler import morning_report as mr
    from photonscript.shared.phd2_store import append_jsonl
    cfg = _cfg(tmp_path)
    append_jsonl(events_path(cfg, "2026-10-08"),
                 {"t": "2026-10-09T03:10:58Z", "rig": "rc16",
                  "kind": "nina_offline", "value": "down", "detail": "x"})
    append_jsonl(events_path(cfg, "2026-10-08"),
                 {"t": "2026-10-09T03:40:00Z", "rig": "rc16",
                  "kind": "nina_offline", "value": "up", "detail": "y"})
    card = mr.report_card(cfg, "2026-10-08", projects=[], calibration=False,
                          completeness=False)
    line = next(x for x in card["lines"] if x.startswith("RC16"))
    assert "stalls: offline 1" in line


def test_observe_only():
    src = (ROOT / "photonscript/scheduler/nina_offline.py").read_text()
    code = src[src.index('"""', 3) + 3:]
    for bad in ("post(", "start_sequence", "stop_sequence", "load_sequence",
                "subprocess", "Popen", "/connect"):
        assert bad not in code, bad
