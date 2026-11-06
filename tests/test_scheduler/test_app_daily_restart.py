"""2026-10-26: the daily close / reopen of NINA x2 + PHD2 did not happen.

The 10-25 night ended at 12:39:31Z (dawn shutdown while paused); a deploy
restarted the service at 12:55Z, before the cooler verify (due 17 min after
the shutdown) ran. The verify task died with the process, the restored
shutdown record kept "verify": None, and every scheduled stop until 11:00
local was blocked by "dawn shutdown cooler verify has not run yet". The
11:45 start found the apps running and left them stale.

  1. a lost verify is derived read-only once overdue (status, stop guards);
  2. the armer re-arms a lost verify on restore;
  3. restart_blockers: the start-time restart guards (ARMED far from
     pre-config is fine, the noon re-arm margin, the switch);
  4. restart_in_progress holds the automatic arm;
  5. reports carry restart / fresh / blocked; fresh_for_day / fresh_line
     and where they show (story, heads-up, morning report);
  6. the script: restart only behind restart_ok, pages a stale day.
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import app_lifecycle as al
from photonscript.shared.config import PhotonScriptConfig

ROOT = Path(__file__).resolve().parents[2]
SHUTDOWN_AT = datetime(2026, 10, 26, 12, 39, 31)     # the 10-25 dawn shutdown
SHUTDOWN = {"at": SHUTDOWN_AT.isoformat() + "Z", "reason": "dawn",
            "steps": ["stop ok"], "verify": None, "failed": []}
_PROBE = al.probe


def _cfg(tmp_path, **kw):
    kw.setdefault("piggyback_enabled", True)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _apps(cfg):
    return _PROBE(cfg, port_fn=lambda h, p: True,
                  procs_fn=lambda: {"nina.exe": 2, "phd2.exe": 1, "thesky64.exe": 1})


class _Armer:
    def __init__(self, state="DISARMED", shutdown=None, plan=None):
        self.state = state
        self.shutdown = shutdown
        self.plan = plan or {}


async def _idle(c):
    return {"NINA #1 (RC16)": [], "NINA #2 (Piggy-600)": []}


def _status(cfg, armer, now, cams=None):
    async def _cams(c):
        return cams if cams is not None else {"rc16": {"CoolerOn": False},
                                              "piggyback": None}
    return asyncio.run(al.status(
        cfg, armer, now=now, seq_fn=_idle,
        thesky_fn=lambda: {"answering": True, "mount_connected": True},
        probe_fn=lambda: _apps(cfg), sun_fn=lambda n: 10.0,
        busy_fn=lambda c: [], cam_fn=_cams))


# ------------------------------------------------------------------ 1. lost verify

def test_verify_overdue_timing(tmp_path):
    cfg = _cfg(tmp_path)
    assert al.verify_delay_s(cfg) == 17 * 60          # gradual_warm_minutes 0
    # due 12:56:31Z; lost only VERIFY_OVERDUE_MIN after that
    assert not al.verify_overdue(cfg, SHUTDOWN, datetime(2026, 10, 26, 12, 58))
    assert not al.verify_overdue(cfg, SHUTDOWN, datetime(2026, 10, 26, 13, 5))
    assert al.verify_overdue(cfg, SHUTDOWN, datetime(2026, 10, 26, 13, 7))
    done = {**SHUTDOWN, "verify": {"ok": True}}
    assert not al.verify_overdue(cfg, done, datetime(2026, 10, 26, 15, 0))
    assert not al.verify_overdue(cfg, None, datetime(2026, 10, 26, 15, 0))
    # a day-old record is not last night's
    assert not al.verify_overdue(cfg, SHUTDOWN, SHUTDOWN_AT + timedelta(hours=21))
    warm = _cfg(tmp_path, gradual_warm_minutes=10)
    assert al.verify_delay_s(warm) == 27 * 60


def test_derive_verify():
    ok = al.derive_verify({"rc16": {"CoolerOn": False, "Temperature": 18.0},
                           "piggyback": None})
    assert ok["ok"] is True and ok["derived"] is True
    assert ok["rigs"]["piggyback"] == "unreachable"
    bad = al.derive_verify({"rc16": {"CoolerOn": True}, "piggyback": None})
    assert bad["ok"] is False and bad["cooler_on"] == ["rc16"]
    assert al.effective_shutdown(SHUTDOWN, bad) is SHUTDOWN
    eff = al.effective_shutdown(SHUTDOWN, ok)
    assert eff["verify"]["derived"] is True and SHUTDOWN["verify"] is None


def test_2026_10_26_replay_stop_no_longer_blocked_all_morning(tmp_path):
    """Restarted at 12:55Z: armer DISARMED, the restored record unverified."""
    cfg = _cfg(tmp_path, app_lifecycle_enabled=True)
    armer = _Armer(state="DISARMED", shutdown=dict(SHUTDOWN))
    # what the stop saw all morning before the fix
    old = al.stop_blockers(cfg, state="DISARMED", night_of=None, shutdown=SHUTDOWN,
                           running={}, sun_alt=10.0, now=datetime(2026, 10, 26, 15, 0))
    assert old == ["dawn shutdown cooler verify has not run yet"]
    # 13:00Z: verify not due + 10 min yet, the 30 min wait still runs
    early = _status(cfg, armer, datetime(2026, 10, 26, 13, 0))
    assert early["stop_ok"] is False
    assert "dawn shutdown cooler verify has not run yet" in early["stop_blockers"]
    # 13:15Z (07:15 local, the next stop run): derived, every cooler off
    out = _status(cfg, armer, datetime(2026, 10, 26, 13, 15))
    assert out["stop_ok"] is True and out["stop_blockers"] == []
    assert out["shutdown"]["verify_done"] is True
    assert out["shutdown"]["verify_derived"] is True
    assert armer.shutdown["verify"] is None          # status never writes it
    json.dumps(out)


def test_lost_verify_with_a_cooler_still_on_keeps_blocking(tmp_path):
    cfg = _cfg(tmp_path)
    out = _status(cfg, _Armer(shutdown=dict(SHUTDOWN)), datetime(2026, 10, 26, 13, 15),
                  cams={"rc16": {"CoolerOn": True}, "piggyback": {"CoolerOn": False}})
    assert out["stop_ok"] is False and out["restart_ok"] is False
    assert any("cooler still reads ON (rc16)" in b for b in out["stop_blockers"])
    assert out["shutdown"]["verify_done"] is False


# ------------------------------------------------------------------ 2. armer restore

def _real_armer(tmp_path, shutdown):
    from photonscript.scheduler.armer import Armer
    cfg = _cfg(tmp_path)
    (tmp_path / "armer_state.json").write_text(json.dumps(
        {"state": "COMPLETE", "plan": {"night_of": "2026-10-25"},
         "shutdown": shutdown}), encoding="utf-8")
    return Armer(cfg)


def test_restore_rearms_a_lost_verify(tmp_path, monkeypatch):
    now = datetime.utcnow().replace(microsecond=0)
    sh = {"at": (now - timedelta(minutes=16)).isoformat() + "Z", "verify": None}
    a = _real_armer(tmp_path, sh)
    calls = []

    async def _verify(delay_s=300):
        calls.append(delay_s)
    monkeypatch.setattr(a, "_verify_shutdown", _verify)

    async def _go():
        assert a.restore() is False                  # the night is over
        await asyncio.sleep(0)
    asyncio.run(_go())
    assert len(calls) == 1 and 30 <= calls[0] <= 70   # what is left of 17 min


def test_restore_watched_night_uses_the_read_only_verify(tmp_path, monkeypatch):
    now = datetime.utcnow().replace(microsecond=0)
    sh = {"at": (now - timedelta(minutes=60)).isoformat() + "Z", "verify": None,
          "watched": True}
    a = _real_armer(tmp_path, sh)
    seen = []

    async def _watched(delay_s=300):
        seen.append(("watched", delay_s))

    async def _shut(delay_s=300):
        seen.append(("shutdown", delay_s))
    monkeypatch.setattr(a, "_verify_watched", _watched)
    monkeypatch.setattr(a, "_verify_shutdown", _shut)

    async def _go():
        a.restore()
        await asyncio.sleep(0)
    asyncio.run(_go())
    assert seen == [("watched", 30)]                  # overdue: soon


@pytest.mark.parametrize("sh", [
    {"at": "2026-10-26T12:39:31Z", "verify": {"ok": True}},   # verified
    None,                                                      # no record
])
def test_restore_leaves_a_verified_or_missing_record(tmp_path, monkeypatch, sh):
    a = _real_armer(tmp_path, sh)
    monkeypatch.setattr(a, "_verify_shutdown", lambda **k: pytest.fail("no verify"))

    async def _go():
        a.restore()
    asyncio.run(_go())


def test_restore_skips_an_old_record_and_no_loop(tmp_path, monkeypatch):
    old = {"at": (datetime.utcnow() - timedelta(hours=5)).isoformat() + "Z",
           "verify": None}
    a = _real_armer(tmp_path, old)
    monkeypatch.setattr(a, "_verify_shutdown", lambda **k: pytest.fail("too old"))

    async def _go():
        a.restore()
    asyncio.run(_go())
    # without a running loop (a sync caller) it is a no-op, never raises
    fresh = {"at": datetime.utcnow().isoformat() + "Z", "verify": None}
    b = _real_armer(tmp_path, fresh)
    assert b.restore() is False and b._resume_shutdown_verify() is None


# ------------------------------------------------------------------ 3. restart guards

START = datetime(2026, 10, 26, 17, 45)         # 11:45 MDT


def _rb(cfg, **kw):
    base = dict(state="DISARMED", plan={}, shutdown={**SHUTDOWN, "verify": {"ok": True}},
                running={}, sun_alt=40.0, now=START)
    base.update(kw)
    return al.restart_blockers(cfg, **base)


def test_restart_ok_on_an_idle_day(tmp_path):
    assert _rb(_cfg(tmp_path)) == []
    assert _rb(_cfg(tmp_path), state="COMPLETE",
               plan={"night_of": "2026-10-25"}) == []


def test_restart_armed_far_from_preconfig_is_fine(tmp_path):
    cfg = _cfg(tmp_path)
    far = {"night_of": "2026-10-26", "preconfig_utc": "2026-10-27T01:20:00Z"}
    assert _rb(cfg, state="ARMED", plan=far) == []
    near = {"night_of": "2026-10-26", "preconfig_utc": "2026-10-26T18:15:00Z"}
    b = _rb(cfg, state="ARMED", plan=near)
    assert b == ["armer is ARMED and pre-config (18:15Z) is less than 60 min away"]
    cfg30 = _cfg(tmp_path, app_lifecycle_restart_min_before_preconfig=20)
    assert _rb(cfg30, state="ARMED", plan=near) == []
    # ARMED with no plan time is not assumed safe
    assert _rb(cfg, state="ARMED", plan={"night_of": "x"})[0].startswith("armer is ARMED")


@pytest.mark.parametrize("state", ["RUNNING", "WATCHING", "PAUSED_UNSAFE",
                                   "PAUSED_OPERATOR"])
def test_restart_live_states_block(tmp_path, state):
    assert _rb(_cfg(tmp_path), state=state) == [f"armer is {state}"]


def test_restart_sequence_sun_capture_block(tmp_path):
    cfg = _cfg(tmp_path)
    b = _rb(cfg, running={"NINA #1 (RC16)": ["Dusk flats"]})
    assert b == ["NINA #1 (RC16) is running a sequence (Dusk flats)"]
    assert "sun at -8.0" in _rb(cfg, sun_alt=-8.0)[0]
    assert _rb(cfg, capture_busy=["rc16"]) == ["calibration capture job running on rc16"]


def test_restart_noon_rearm_margin_and_switch(tmp_path):
    noon = _cfg(tmp_path, noon_arm_enabled=True)
    assert _rb(noon) == []                                       # 11:45
    late = START + timedelta(minutes=11)                          # 11:56
    assert _rb(noon, now=late)[0].startswith("the noon re-arm is due at 12:00")
    assert _rb(_cfg(tmp_path, noon_arm_enabled=False), now=late) == []   # noon arm off
    far = {"night_of": "2026-10-26", "preconfig_utc": "2026-10-27T01:20:00Z"}
    assert _rb(noon, now=late, state="ARMED", plan=far) == []    # already armed
    off = _cfg(tmp_path, app_lifecycle_restart_at_start=False)
    assert _rb(off) == ["app_lifecycle_restart_at_start is off"]


def test_status_carries_restart_fields(tmp_path):
    cfg = _cfg(tmp_path, app_lifecycle_enabled=True)
    out = _status(cfg, _Armer(shutdown={**SHUTDOWN, "verify": {"ok": True}}), START)
    assert out["restart_ok"] is True and out["restart_blockers"] == []
    assert out["restart_at_start"] is True and out["restart_in_progress"] is False
    assert out["fresh_today"]["fresh"] is None and out["stop_done_today"] is False


# ------------------------------------------------------------------ 4. noon re-arm hold

def _post(cfg, t, **body):
    return al.record_report(cfg, body, now=t)


def test_restart_in_progress_window(tmp_path):
    cfg = _cfg(tmp_path)
    assert al.restart_in_progress(cfg, START) is False
    _post(cfg, START, mode="start", ok=True, restart="begin")
    assert al.restart_in_progress(cfg, START + timedelta(minutes=5)) is True
    _post(cfg, START + timedelta(minutes=1), mode="stop", ok=True, acted=True)
    assert al.restart_in_progress(cfg, START + timedelta(minutes=5)) is True
    assert al.restart_in_progress(cfg, START + timedelta(minutes=21)) is False
    _post(cfg, START + timedelta(minutes=6), mode="start", ok=True, acted=True,
          restart="done", fresh=True)
    assert al.restart_in_progress(cfg, START + timedelta(minutes=7)) is False


def test_auto_armer_holds_for_the_restart(tmp_path):
    from photonscript.scheduler import auto_armer
    cfg = _cfg(tmp_path)
    now = datetime.utcnow().replace(microsecond=0)
    assert auto_armer._apps_restarting(cfg, now) is False
    _post(cfg, now, mode="start", ok=True, restart="begin")
    assert auto_armer._apps_restarting(cfg, now + timedelta(minutes=2)) is True
    src = (ROOT / "photonscript/scheduler/auto_armer.py").read_text(encoding="utf-8")
    i = src.index("if arm_now and _apps_restarting(config, now):")
    assert i < src.index("skip, why = await auto_arm_guard(config)")
    assert "holding for the daily app restart" in src


# ------------------------------------------------------------------ 5. reports, fresh

def test_record_report_keeps_restart_fields(tmp_path):
    cfg = _cfg(tmp_path)
    r = _post(cfg, START, mode="start", ok=True, restart="done", fresh=True)
    assert r["restart"] == "done" and r["fresh"] is True
    r = _post(cfg, START, mode="stop", ok=False, blocked=True, message="stop blocked: x")
    assert r["blocked"] is True and "restart" not in r and "fresh" not in r
    r = _post(cfg, START, mode="start", restart="bogus")
    assert "restart" not in r


def test_fresh_after_a_morning_stop(tmp_path):
    cfg = _cfg(tmp_path)
    assert al.fresh_line(cfg, "2026-10-26") is None
    _post(cfg, datetime(2026, 10, 26, 13, 15), mode="stop", ok=True, acted=True,
          message="closed")
    f = al.fresh_for_day(cfg, "2026-10-26")
    assert f["fresh"] is True and f["restarted"] is False
    assert al.fresh_line(cfg, "2026-10-26").endswith("not launched yet local.")
    _post(cfg, datetime(2026, 10, 26, 17, 46), mode="start", ok=True, acted=True,
          restart="none", fresh=True)
    assert al.fresh_line(cfg, "2026-10-26") == (
        "Apps restarted fresh 2026-10-26: closed 07:15, launched 11:46 local.")


def test_fresh_by_the_start_time_restart(tmp_path):
    cfg = _cfg(tmp_path)
    _post(cfg, datetime(2026, 10, 26, 12, 30), mode="stop", ok=False, blocked=True,
          message="stop blocked: dawn shutdown cooler verify has not run yet")
    assert al.fresh_for_day(cfg, "2026-10-26")["fresh"] is False
    _post(cfg, START, mode="start", ok=True, restart="begin")
    _post(cfg, START + timedelta(minutes=1), mode="stop", ok=True, acted=True,
          message="closed by the start-time restart")
    _post(cfg, START + timedelta(minutes=4), mode="start", ok=True, acted=True,
          restart="done", fresh=True)
    f = al.fresh_for_day(cfg, "2026-10-26")
    assert f["fresh"] is True and f["restarted"] is True
    line = al.fresh_line(cfg, "2026-10-26")
    assert line.startswith("Apps restarted fresh 2026-10-26 by the 11:49 start")
    assert "cooler verify has not run yet" in line


def test_stale_day_line(tmp_path):
    cfg = _cfg(tmp_path)
    _post(cfg, START, mode="start", ok=True, restart="blocked", fresh=False,
          steps=["note: x", "NOT restarted: armer is RUNNING"])
    line = al.fresh_line(cfg, "2026-10-26")
    assert line.startswith("Apps NOT restarted 2026-10-26 (NOT restarted: armer is RUNNING)")
    assert chr(0x2014) not in line
    # a dry run never counts
    cfg2 = _cfg(tmp_path / "b")
    _post(cfg2, START, mode="stop", ok=True, acted=True, dry_run=True)
    assert al.fresh_for_day(cfg2, "2026-10-26")["fresh"] is None


def test_lines_reach_story_headsup_and_morning_report(tmp_path, monkeypatch):
    from photonscript.scheduler import morning_report as mr
    from photonscript.scheduler import night_story as ns
    cfg = _cfg(tmp_path)
    _post(cfg, START, mode="start", ok=True, restart="blocked", fresh=False,
          steps=["NOT restarted: armer is RUNNING"])
    # heads-up: a stale day sits with the warnings
    plan = {"night_of": "2026-10-26", "preconfig_utc": "2026-10-27T01:20:00Z",
            "dusk_utc": "2026-10-27T01:50:00Z", "dawn_utc": "2026-10-27T12:10:00Z",
            "dark_hours": 10.3}
    txt = ns.plan_headsup_text(cfg, plan, guided=False, apps=ns._apps_line(cfg, "2026-10-26"))
    assert "Apps NOT restarted 2026-10-26" in txt
    # the morning report card of night 2026-10-26
    card = mr.report_card(cfg, "2026-10-26", projects=[], calibration=False,
                          completeness=False)
    assert card["apps"].startswith("Apps NOT restarted 2026-10-26")
    assert card["apps"] in mr.card_lines(card, calibration=False)
    assert mr.card_line_priority(card["apps"]) == mr.PRI_RIG


def test_report_route_writes_the_story_line(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app as app_mod
    from photonscript.shared import pushover
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
    sent = []

    async def _notify(c, msg, **kw):
        sent.append((msg, kw))
        return kw.get("kind") != "info"
    monkeypatch.setattr(pushover, "notify", _notify)
    client = TestClient(app_mod.app)
    client.post("/api/apps/report", json={"mode": "stop", "ok": True, "acted": True,
                                          "message": "closed"})
    assert sent == []
    client.post("/api/apps/report", json={"mode": "start", "ok": True, "acted": True,
                                          "restart": "none", "fresh": True})
    assert len(sent) == 1 and sent[0][1]["kind"] == "info"
    assert sent[0][0].startswith("Apps restarted fresh")
    # a stale scheduled start pages (act) instead of the info line
    cfg2 = _cfg(tmp_path / "b")
    monkeypatch.setattr(app_mod, "get_config", lambda: cfg2)
    r = client.post("/api/apps/report", json={
        "mode": "start", "ok": True, "page": True, "restart": "blocked", "fresh": False,
        "message": "all apps already running; apps NOT fresh today: armer is RUNNING"})
    assert r.json()["paged"] is True and sent[-1][1]["kind"] == "act"
    assert len(sent) == 2


def test_config_fields():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    c = PhotonScriptConfig(_env_file=None)
    assert c.app_lifecycle_restart_at_start is True
    assert c.app_lifecycle_restart_min_before_preconfig == 60.0
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    assert by_env["PS_APP_LIFECYCLE_RESTART_AT_START"][4] == "bool"
    assert by_env["PS_APP_LIFECYCLE_RESTART_MIN_BEFORE_PRECONFIG"][4] == "float"


# ------------------------------------------------------------------ 6. the script

def _ps() -> str:
    return (ROOT / "deploy" / "observatory-apps.ps1").read_bytes().decode("ascii")


def test_script_restart_rails():
    s = _ps()
    assert chr(0x2014) not in s
    start = s[s.index("# ---------------------------------------------------------------- -Start"):]
    # restart only for a scheduled (or -Restart) start, behind restart_ok,
    # after the TheSky check, never with -Only, and never TheSky
    assert "$restartWanted = [bool]($Scheduled -or $Restart)" in start
    assert "-not $st.stop_done_today -and -not $Only" in start
    assert start.index('if ($mount -ne "connected")') < start.index("Close-Apps $false")
    assert start.index("-not $st.restart_ok") < start.index("Close-Apps $false")
    assert start.index('@{ restart = "begin" }') < start.index("Close-Apps $false")
    assert start.index("Close-Apps $false") < start.index('"stop"')
    # a failed close launches nothing and pages
    fail = start[start.index("if ($closeFailed) {"):]
    assert fail.index("$true \"\" @{ restart = \"failed\"") < fail.index("exit 1")
    # a stale scheduled day pages, unless the restart is switched off
    assert "$pageStale = [bool]($Scheduled -and $st.restart_at_start)" in start
    assert "apps NOT fresh today: " in start
    # a blocked scheduled stop reports once per distinct reason
    stop = s[s.index("# ---------------------------------------------------------------- -Stop"):
             s.index("# ---------------------------------------------------------------- -Start")]
    assert "@{ blocked = $true }" in stop and "-replace '[0-9.:-]+', '#'" in stop
