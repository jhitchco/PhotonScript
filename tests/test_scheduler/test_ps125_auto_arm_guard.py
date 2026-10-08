"""PS-125: auto-arm switches on the dashboard, and the auto-arm guard.

The dashboard checkboxes save through POST /api/config (same .env write as
the System page) and survive a restart. Before any automatic arm the loop
skips when a sideload was loaded tonight, NINA #1 runs anything, or NINA #1
cannot be read; one Pushover per night per reason; every decision lands in
runs/<night>_events.jsonl and in the dashboard chip.
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from starlette.requests import Request

from photonscript.scheduler import auto_armer as aa
from photonscript.scheduler import sideload as sd
from photonscript.scheduler.routers import auto_arm as rt
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.night_events import events_path
from photonscript.shared.phd2_store import night_of, read_jsonl

ROOT = Path(__file__).resolve().parents[2]
# 2026-10-05 17:00 local (MDT, UTC-6) = 23:00Z
NOW = datetime(2026, 10, 5, 23, 0, 0)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _cfg(tmp_path, **kw):
    kw.setdefault("quality_eccentricity_max", 0.60)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _events(cfg, now=NOW):
    return read_jsonl(events_path(cfg, night_of(cfg, now)))


def _reader(trees):
    """Fake read_sequence_state: base_url -> (tree, err)."""
    calls = []

    async def read(base):
        calls.append(base)
        for key, val in trees.items():
            if key in base:
                return val
        return [], None
    read.calls = calls
    return read


IDLE = ([{"Name": "Start", "Status": "FINISHED"},
         {"Name": "Targets", "Status": "CREATED"}], None)
RUNNING = ([{"Name": "Targets", "Status": "RUNNING",
             "Items": [{"Name": "Heart Nebula", "Status": "RUNNING"}]}], None)
DOWN = (None, "ConnectError: refused")


def _sideload(cfg, when=NOW - timedelta(hours=2), ok=True):
    sd.record_event(cfg, "rc16", "PhotonScript_20261005_TT_M_2_then_tonight",
                    "x.json", "tracking_test_then_tonight", ok, "loaded", now=when)


# ------------------------------------------------------- persistence

def _request(body: dict) -> Request:
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    return Request({"type": "http", "method": "POST", "headers": []}, receive)


def test_checkbox_round_trip_survives_restart(tmp_path, monkeypatch):
    """The dashboard posts the System page's own keys to /api/config; the
    .env gets them and a fresh config (a restart) reads them back."""
    from photonscript.scheduler import app
    from photonscript.shared import envfile
    env = tmp_path / ".env"
    env.write_text("# scope\nPS_NOON_ARM_ENABLED=false\n", encoding="utf-8")
    cfg = _cfg(tmp_path, auto_arm_enabled=False, noon_arm_enabled=False)
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(envfile, "env_path", lambda root=None: env)
    res = _run(app.api_update_config(_request({
        "PS_AUTO_ARM_ENABLED": "true", "PS_NOON_ARM_ENABLED": "true",
        "PS_NOON_ARM_GUIDED": "false"})))
    assert res["updated"] == 3
    assert cfg.auto_arm_enabled is True and cfg.noon_arm_enabled is True
    assert cfg.noon_arm_guided is False                 # live-applied
    fresh = PhotonScriptConfig(_env_file=str(env))       # "restart"
    assert fresh.auto_arm_enabled is True
    assert fresh.noon_arm_enabled is True and fresh.noon_arm_guided is False
    assert env.read_text(encoding="utf-8").startswith("# scope\n")
    # and back off again
    _run(app.api_update_config(_request({"PS_AUTO_ARM_ENABLED": "false"})))
    assert PhotonScriptConfig(_env_file=str(env)).auto_arm_enabled is False


def test_dashboard_switches_use_the_system_page_keys():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    envs = {f[1] for f in _CONFIG_FIELDS}
    dash = (ROOT / "photonscript/scheduler/templates/dashboard.html").read_text(encoding="utf-8")
    js = (ROOT / "photonscript/scheduler/static/js/auto_arm_panel.js").read_text(encoding="utf-8")
    for key in ("PS_AUTO_ARM_ENABLED", "PS_NOON_ARM_ENABLED", "PS_NOON_ARM_GUIDED"):
        assert key in envs and f'data-env="{key}"' in dash
    assert 'id="autoArmBox"' in dash and "auto_arm_panel.js" in dash
    assert "/api/config" in js and "/api/auto-arm" in js and "confirm(" in js
    assert "arming replaces it" in js and "autoArmSideloadOk" in dash
    src = (ROOT / "photonscript/scheduler/auto_armer.py").read_text(encoding="utf-8")
    new = src.split("# PS-125: never auto-arm", 1)[1].split("async def run_auto_arm_loop")[0]
    assert new.isascii()
    for p in ("photonscript/scheduler/static/js/auto_arm_panel.js",
              "photonscript/scheduler/routers/auto_arm.py"):
        assert (ROOT / p).read_bytes().isascii()


# ----------------------------------------------------------- the guard

def test_guard_sideload_tonight_skips(tmp_path):
    cfg = _cfg(tmp_path)
    _sideload(cfg)
    read = _reader({"1888": IDLE})
    skip, why = _run(aa.auto_arm_guard(cfg, NOW, reader=read))
    assert skip == aa.SKIP_SIDELOAD and "sideloaded" in why
    assert read.calls == []          # no need to ask NINA


def test_guard_failed_or_old_sideload_does_not_count(tmp_path):
    cfg = _cfg(tmp_path)
    _sideload(cfg, ok=False)
    _sideload(cfg, when=NOW - timedelta(days=1))          # last night's
    skip, _ = _run(aa.auto_arm_guard(cfg, NOW, reader=_reader({"1888": IDLE})))
    assert skip is None


def test_guard_same_morning_sideload_counts(tmp_path):
    """A load at 09:00 local is filed under the previous night (noon to
    noon) but is for tonight."""
    cfg = _cfg(tmp_path)
    _sideload(cfg, when=datetime(2026, 10, 5, 15, 0))     # 09:00 MDT
    assert aa.sideload_tonight(cfg, NOW)["rig"] == "rc16"


def test_guard_nina_running_skips(tmp_path):
    cfg = _cfg(tmp_path)
    skip, why = _run(aa.auto_arm_guard(cfg, NOW, reader=_reader({"1888": RUNNING})))
    assert skip == aa.SKIP_NINA_RUNNING and "Heart Nebula" in why


def test_guard_nina_unreachable_does_not_arm(tmp_path):
    cfg = _cfg(tmp_path)
    skip, why = _run(aa.auto_arm_guard(cfg, NOW, reader=_reader({"1888": DOWN})))
    assert skip == aa.SKIP_NINA_UNREADABLE and "refused" in why


def test_guard_normal_night_clear_and_reads_nina2(tmp_path):
    cfg = _cfg(tmp_path, piggyback_enabled=True,
               nina_base_url="http://n1:1888/v2/api",
               piggyback_nina_base_url="http://n2:1889/v2/api")
    read = _reader({"1888": IDLE, "1889": RUNNING})
    skip, why = _run(aa.auto_arm_guard(cfg, NOW, reader=read))
    assert skip is None and "NINA #2 running" in why     # #2 reported, not blocking
    assert len(read.calls) == 2


# --------------------------------------------- the loop: arm or skip, log, push

class _Armer:
    def __init__(self):
        self.state = "DISARMED"
        self.armed = []

    async def arm(self, guiding=None):
        self.armed.append(guiding)
        self.state = "ARMED"
        return {"state": self.state}


@pytest.fixture
def loop_env(tmp_path, monkeypatch):
    """One tick of run_auto_arm_loop inside tonight's arm window."""
    from photonscript.scheduler import night_plan, preflight
    from photonscript.shared import pushover
    import photonscript.scheduler.auto_armer as mod
    cfg = _cfg(tmp_path, auto_arm_enabled=True, noon_arm_enabled=False,
               evening_forecast_enabled=False, auto_dusk_flats=False)
    now = datetime.utcnow()
    monkeypatch.setattr(night_plan, "build_night_plan", lambda c: {
        "night_of": night_of(cfg, now),
        "preconfig_utc": (now + timedelta(hours=1)).isoformat() + "Z"})

    async def pf(c):
        return {"go": True, "checks": []}
    monkeypatch.setattr(preflight, "run_preflight", pf)
    pushes = []

    async def notify(c, msg, title="", priority=0, sound="none"):
        pushes.append((title, msg))
        return True
    monkeypatch.setattr(pushover, "notify", notify)
    state = {"tree": IDLE}

    async def read(base, client=None):
        return state["tree"]
    monkeypatch.setattr(sd, "read_sequence_state", read)

    async def stop(_s):
        raise asyncio.CancelledError
    monkeypatch.setattr(mod.asyncio, "sleep", stop)
    armer = _Armer()

    def tick():
        with pytest.raises(asyncio.CancelledError):
            _run(aa.run_auto_arm_loop(cfg, lambda: armer))
    return {"cfg": cfg, "armer": armer, "pushes": pushes, "state": state,
            "tick": tick, "now": now}


def test_loop_normal_night_arms_and_logs(loop_env):
    loop_env["tick"]()
    assert loop_env["armer"].armed == [None]
    ev = [e for e in _events(loop_env["cfg"], loop_env["now"]) if e["kind"] == "auto_arm"]
    assert [e["value"] for e in ev] == ["armed"]
    assert ev[0]["trigger"] == "within arm window"


def test_loop_sideload_skips_once_per_night(loop_env):
    _sideload(loop_env["cfg"], when=loop_env["now"] - timedelta(seconds=1))
    loop_env["tick"]()
    loop_env["tick"]()                       # next tick, same reason
    assert loop_env["armer"].armed == []
    assert len(loop_env["pushes"]) == 1
    title, msg = loop_env["pushes"][0]
    assert "skipped" in title and "sideloaded sequence is loaded" in msg
    ev = [e for e in _events(loop_env["cfg"], loop_env["now"]) if e["kind"] == "auto_arm"]
    assert [(e["value"], e["skip"]) for e in ev] == [("skipped", "sideload")]


def test_loop_nina_running_then_unreachable_one_push_each(loop_env):
    loop_env["state"]["tree"] = RUNNING
    loop_env["tick"]()
    loop_env["state"]["tree"] = DOWN
    loop_env["tick"]()
    loop_env["state"]["tree"] = RUNNING
    loop_env["tick"]()                       # logged again, not pushed again
    assert loop_env["armer"].armed == []
    assert [m for _, m in loop_env["pushes"]] and len(loop_env["pushes"]) == 2
    ev = [e for e in _events(loop_env["cfg"], loop_env["now"]) if e["kind"] == "auto_arm"]
    assert [e["skip"] for e in ev] == ["nina_running", "nina_unreadable", "nina_running"]
    assert [e["notified"] for e in ev] == [True, True, False]
    loop_env["state"]["tree"] = IDLE         # NINA finished: arms on the next tick
    loop_env["tick"]()
    assert loop_env["armer"].armed == [None]


# ------------------------------------------------------- status + chip text

def test_chip_text():
    assert aa.decision_chip(None) == "No auto-arm decision yet tonight"
    armed = {"t": "2026-10-05T23:05:00Z", "value": "armed",
             "trigger": "within arm window"}
    assert aa.decision_chip(armed, -6) == "Auto-armed at 17:05 local (within arm window)"
    skipped = {"t": "2026-10-05T18:00:00Z", "value": "skipped", "skip": "sideload",
               "detail": "PhotonScript_x on rc16"}
    assert aa.decision_chip(skipped, -6) == (
        "Auto-arm skipped at 12:00 local: a sideloaded sequence is loaded "
        "(PhotonScript_x on rc16)")
    assert "NINA state unreadable" in aa.decision_chip(
        {"t": "2026-10-05T18:00:00Z", "value": "skipped", "skip": "nina_unreadable"})


def test_next_actions_text(tmp_path):
    cfg = _cfg(tmp_path, auto_arm_enabled=True, auto_arm_lead_hours=3.0,
               noon_arm_enabled=True, noon_arm_guided=False)
    # pre-config 19:47 local = 01:47Z; window opens 16:47 local
    n = aa.next_actions(cfg, "2026-10-06T01:47:00Z", datetime(2026, 10, 5, 20, 0))
    assert n["auto_arm"]["next"] == "next auto-arm window opens 16:47 local"
    assert n["auto_arm"]["window_open_utc"] == "2026-10-05T22:47:00Z"
    assert n["noon_arm"]["next"] == "next noon re-arm Tue 12:00 local (unguided)"
    assert n["noon_arm"]["at_utc"] == "2026-10-06T18:00:00Z"
    n = aa.next_actions(cfg, "2026-10-06T01:47:00Z", NOW)
    assert n["auto_arm"]["next"].startswith("auto-arm window open since 16:47")
    off = aa.next_actions(_cfg(tmp_path, auto_arm_enabled=False, noon_arm_enabled=False),
                          "2026-10-06T01:47:00Z", NOW)
    assert off["auto_arm"]["next"] == "off" and off["noon_arm"]["next"] == "off"
    busy = aa.next_actions(cfg, "2026-10-06T01:47:00Z", NOW, "RUNNING")
    assert "RUNNING" in busy["auto_arm"]["next"]


def test_status_endpoint_payload(tmp_path):
    cfg = _cfg(tmp_path, auto_arm_enabled=True)
    _sideload(cfg)
    aa.log_decision(cfg, "skipped", aa.SKIP_SIDELOAD, "PhotonScript_x on rc16",
                    "within arm window", now=NOW, notified=True)
    s = rt.auto_arm_status(cfg, "DISARMED", "2026-10-06T01:47:00Z", NOW)
    assert s["sideload"]["kind"] == "sideload"
    assert s["last_decision"]["skip"] == "sideload"
    assert s["chip"].startswith("Auto-arm skipped at 17:00 local: a sideloaded")
    assert s["auto_arm"]["enabled"] is True and s["armer"] == "DISARMED"
