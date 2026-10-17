"""PS-170: daily app lifecycle (close NINA x2 + PHD2 after the dawn
shutdown, relaunch before the noon re-arm).

  1. settings: launch time, endpoints per rig, needed apps per night;
  2. probes with fake ports / processes;
  3. stop guards (armer states, running sequences, sun, dawn shutdown and
     its verify, the wait after it);
  4. script reports: record, last, once-a-day stop, page once per day;
  5. preflight row (pass / warn / fail) and the armer page (once per night
     and phase, at arm and pre-config - 60 min);
  6. status() and the API routes; config fields; System page panel;
  7. the PowerShell scripts: ASCII, parse check, the guard rails in text.
"""
import asyncio
import json
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import app_lifecycle as al
from photonscript.shared.config import PhotonScriptConfig

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
# 2026-10-08 08:00 MDT = 14:00Z (sun well up at AARO)
MORNING = datetime(2026, 10, 8, 14, 0, 0)
_PROBE = al.probe   # the real one (tests monkeypatch al.probe)


def _cfg(tmp_path, **kw):
    kw.setdefault("piggyback_enabled", True)
    kw.setdefault("app_lifecycle_alert", True)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _apps(cfg, up=("nina1", "nina2", "phd2", "thesky")):
    return _PROBE(cfg, port_fn=lambda h, p: {
        1888: "nina1", 1889: "nina2", 4400: "phd2", 3040: "thesky"}.get(p) in up,
        procs_fn=lambda: {"nina.exe": 2, "phd2.exe": 1, "thesky64.exe": 1})


# ------------------------------------------------------------------ 1. settings

def test_parse_hhmm():
    assert al.parse_hhmm("11:45") == (11, 45)
    assert al.parse_hhmm(" 7:05 ") == (7, 5)
    assert al.parse_hhmm("25:00") == (11, 45)
    assert al.parse_hhmm("noon") == (11, 45)
    assert al.parse_hhmm(None, (12, 0)) == (12, 0)


def test_endpoints_follow_the_rigs(tmp_path):
    cfg = _cfg(tmp_path)
    ep = al.endpoints(cfg)
    assert ep["nina1"] == ("localhost", 1888) and ep["nina2"] == ("localhost", 1889)
    assert ep["phd2"] == ("localhost", 4400) and ep["thesky"] == ("localhost", 3040)
    solo = _cfg(tmp_path, piggyback_enabled=False)
    assert "nina2" not in al.endpoints(solo)
    assert al.expected(cfg, True) == ["nina1", "nina2", "phd2"]
    assert al.expected(cfg, False) == ["nina1", "nina2"]
    assert al.expected(solo, False) == ["nina1"]


def test_launch_settings_carry_config(tmp_path):
    cfg = _cfg(tmp_path, app_nina2_profile="Piggy-600 copy", app_phd2_profile_id=5)
    ls = al.launch_settings(cfg)
    assert ls["nina1_profile"] == "RC16" and ls["nina2_profile"] == "Piggy-600 copy"
    assert ls["nina1_port"] == 1888 and ls["nina2_port"] == 1889
    assert ls["nina2_enabled"] is True and ls["phd2_profile_id"] == 5
    assert ls["nina_exe"].endswith("NINA.exe") and ls["phd2_exe"].endswith("phd2.exe")


def test_start_time_utc_is_scope_local(tmp_path):
    cfg = _cfg(tmp_path)            # America/Denver, MDT = UTC-6 in October
    assert al.start_time_utc(cfg, MORNING) == datetime(2026, 10, 8, 17, 45)
    cfg2 = _cfg(tmp_path, app_lifecycle_start_local="12:00")
    assert al.start_time_utc(cfg2, MORNING) == datetime(2026, 10, 8, 18, 0)


# ------------------------------------------------------------------ 2. probes

def test_probe_and_missing(tmp_path):
    cfg = _cfg(tmp_path)
    apps = _apps(cfg, up=("nina1",))
    assert apps["nina1"]["answering"] and not apps["nina2"]["answering"]
    assert apps["nina1"]["processes"] == 2 and apps["phd2"]["processes"] == 1
    assert al.missing(apps, al.expected(cfg, True)) == ["nina2", "phd2"]
    assert al.describe(apps, ["nina2", "phd2"]) == \
        "NINA #2 (Piggy-600) (:1889), PHD2 (:4400)"


def test_port_open_on_a_real_listener():
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        assert al.port_open("127.0.0.1", port) is True
    finally:
        s.close()
    assert al.port_open("127.0.0.1", port, timeout=0.5) is False


# ------------------------------------------------------------------ 3. stop guards

def _blockers(cfg, **kw):
    base = dict(state="COMPLETE", night_of="2026-10-07", shutdown=None,
                running={}, sun_alt=20.0, now=MORNING)
    base.update(kw)
    return al.stop_blockers(cfg, **base)


def _shutdown(minutes_ago, verify=True):
    at = MORNING - timedelta(minutes=minutes_ago)
    return {"at": at.isoformat() + "Z",
            "verify": {"ok": True} if verify else None}


def test_stop_ok_after_a_verified_shutdown(tmp_path):
    cfg = _cfg(tmp_path)
    assert _blockers(cfg, shutdown=_shutdown(90)) == []


@pytest.mark.parametrize("state", ["ARMED", "RUNNING", "WATCHING",
                                   "PAUSED_UNSAFE", "PAUSED_OPERATOR"])
def test_live_armer_states_block(tmp_path, state):
    b = _blockers(_cfg(tmp_path), state=state, shutdown=_shutdown(90))
    assert b == [f"armer is {state}"]


def test_running_sequence_blocks_unreadable_does_not(tmp_path):
    cfg = _cfg(tmp_path)
    b = _blockers(cfg, state="DISARMED", night_of=None,
                  running={"NINA #2 (Piggy-600)": ["Dark library", "x"],
                           "NINA #1 (RC16)": None})
    assert b == ["NINA #2 (Piggy-600) is running a sequence (Dark library, x)"]


def test_calibration_capture_job_blocks(tmp_path):
    b = _blockers(_cfg(tmp_path), state="DISARMED", night_of=None,
                  capture_busy=["piggyback"])
    assert b == ["calibration capture job running on piggyback"]


def test_sun_below_minus_six_blocks(tmp_path):
    b = _blockers(_cfg(tmp_path), state="DISARMED", night_of=None, sun_alt=-9.0)
    assert len(b) == 1 and "sun at -9.0" in b[0]


def test_shutdown_verify_and_wait(tmp_path):
    cfg = _cfg(tmp_path)
    b = _blockers(cfg, shutdown=_shutdown(10, verify=False))
    assert "dawn shutdown cooler verify has not run yet" in b
    assert any(x.startswith("waiting until 14:20Z (30 min") for x in b)
    b = _blockers(cfg, shutdown=_shutdown(25))
    assert b == ["waiting until 14:05Z (30 min after the dawn shutdown)"]
    cfg10 = _cfg(tmp_path, app_lifecycle_stop_after_shutdown_min=10)
    assert _blockers(cfg10, shutdown=_shutdown(25)) == []


def test_complete_without_a_shutdown_record_blocks(tmp_path):
    cfg = _cfg(tmp_path)
    b = _blockers(cfg, state="COMPLETE", shutdown=None)
    assert b == ["night over but its dawn shutdown has not recorded yet"]
    # a stale record (yesterday) is not this night's
    b = _blockers(cfg, state="COMPLETE", shutdown=_shutdown(60 * 26))
    assert b == ["night over but its dawn shutdown has not recorded yet"]
    # a disarmed day (no night) has nothing to wait for
    assert _blockers(cfg, state="DISARMED", night_of=None) == []


# ------------------------------------------------------------------ 4. reports

def test_reports_and_once_a_day_stop(tmp_path):
    cfg = _cfg(tmp_path)
    assert al.last_reports(cfg) == {} and not al.stop_done_today(cfg, MORNING)
    al.record_report(cfg, {"mode": "stop", "ok": True, "acted": True,
                           "dry_run": True, "message": "dry run"}, MORNING)
    assert not al.stop_done_today(cfg, MORNING)            # dry run does not count
    rec = al.record_report(cfg, {"mode": "stop", "ok": True, "acted": True,
                                 "steps": ["PHD2 shutdown ok"], "message": "closed"},
                           MORNING)
    assert rec["day"] == "2026-10-08" and rec["steps"] == ["PHD2 shutdown ok"]
    assert al.stop_done_today(cfg, MORNING)
    assert not al.stop_done_today(cfg, MORNING + timedelta(days=1))
    al.record_report(cfg, {"mode": "start", "ok": True, "acted": False,
                           "message": "all apps already running"}, MORNING)
    last = al.last_reports(cfg)
    assert last["stop"]["message"] == "closed" and last["start"]["acted"] is False
    junk = al.record_report(cfg, {"mode": "explode"}, MORNING)
    assert junk["mode"] == "other" and junk["ok"] is False


def test_page_once_per_mode_and_day(tmp_path):
    cfg = _cfg(tmp_path)
    r1 = al.record_report(cfg, {"mode": "start", "ok": False, "page": True,
                                "message": "TheSky not ready"}, MORNING)
    assert al.should_page(cfg, r1, MORNING)
    r2 = al.record_report(cfg, {"mode": "start", "ok": False, "page": True,
                                "message": "again"}, MORNING)
    assert not al.should_page(cfg, r2, MORNING)
    r3 = al.record_report(cfg, {"mode": "stop", "ok": False, "page": True}, MORNING)
    assert al.should_page(cfg, r3, MORNING)
    r4 = al.record_report(cfg, {"mode": "stop", "ok": True}, MORNING)
    assert not al.should_page(cfg, r4, MORNING)
    dry = al.record_report(cfg, {"mode": "status", "page": True, "dry_run": True}, MORNING)
    assert not al.should_page(cfg, dry, MORNING)
    assert "TheSky not ready" in al.page_text(r1)


# ------------------------------------------------------------------ 5. preflight + armer

def test_preflight_check(tmp_path):
    off = _cfg(tmp_path)
    on = _cfg(tmp_path, app_lifecycle_enabled=True)
    late = datetime(2026, 10, 8, 18, 30)      # 12:30 MDT, after 11:45 + 15
    early = datetime(2026, 10, 8, 17, 50)     # 11:50 MDT, inside the grace
    up = _apps(on)
    down = _apps(on, up=("nina1", "thesky"))
    assert al.preflight_check(on, up, late, True, True)["status"] == "pass"
    c = al.preflight_check(on, down, late, True, True)
    assert c["status"] == "fail" and "NINA #2" in c["detail"] and "PHD2" in c["detail"]
    assert al.preflight_check(on, down, early, True, True)["status"] == "warn"
    assert al.preflight_check(on, down, late, False, True)["status"] == "warn"
    assert al.preflight_check(off, down, late, True, True)["status"] == "warn"
    # unguided: PHD2 is not needed
    only_phd2_down = _apps(on, up=("nina1", "nina2"))
    assert al.preflight_check(on, only_phd2_down, late, True, False)["status"] == "pass"


def test_preflight_wires_the_check_and_push_switch():
    src = (ROOT / "photonscript/scheduler/preflight.py").read_text(encoding="utf-8")
    assert "checks.append(await asyncio.to_thread(_apps_check, config))" in src
    assert "async def run_preflight(config, test_push: bool = True)" in src
    app_src = (ROOT / "photonscript/scheduler/app.py").read_text(encoding="utf-8")
    assert "async def api_preflight(push: int = 1)" in app_src
    assert "run_preflight(get_config(), test_push=bool(push))" in app_src


def test_apps_check_runs_without_the_service(tmp_path, monkeypatch):
    from photonscript.scheduler import preflight as pf
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(al, "probe", lambda c: _apps(c))
    row = pf._apps_check(cfg)
    assert row["name"] == "Observatory apps running" and row["status"] == "pass"


def test_alert_text(tmp_path):
    cfg = _cfg(tmp_path)
    assert al.alert_text(cfg, _apps(cfg), True, "At arm", None) is None
    msg = al.alert_text(cfg, _apps(cfg, up=("nina1",)), True, "At arm",
                        "2026-10-09T00:30:00Z")
    assert msg.startswith("At arm: NINA #2 (Piggy-600) (:1889), PHD2 (:4400) not running")
    assert "before pre-config 00:30Z" in msg and "observatory-apps.ps1 -Start" in msg
    assert al.alert_text(cfg, _apps(cfg, up=("nina1", "nina2")), False, "x", None) is None
    quiet = _cfg(tmp_path, app_lifecycle_alert=False)
    assert al.alert_text(quiet, _apps(quiet, up=()), True, "x", None) is None


def _armer(tmp_path, monkeypatch, up):
    import photonscript.scheduler.armer as armer_mod
    from photonscript.scheduler.armer import Armer
    cfg = _cfg(tmp_path)
    a = Armer(cfg)
    a.guiding_override = "guided"
    a.plan = {"night_of": "2026-10-08", "preconfig_utc": "2026-10-09T00:30:00Z"}
    monkeypatch.setattr(al, "probe", lambda c: _apps(c, up=up))
    notes = []

    async def _fake_notify(c, msg, **kw):
        notes.append((msg, kw))
    monkeypatch.setattr(armer_mod, "notify", _fake_notify)
    return a, notes


def test_armer_pages_once_per_night_and_phase(tmp_path, monkeypatch):
    a, notes = _armer(tmp_path, monkeypatch, up=("nina1",))
    asyncio.run(a._apps_alert("At arm"))
    asyncio.run(a._apps_alert("At arm"))
    assert len(notes) == 1 and notes[0][1]["priority"] == 1
    assert "NINA #2" in notes[0][0] and "PHD2" in notes[0][0]
    asyncio.run(a._apps_alert("Pre-config in 60 min"))
    assert len(notes) == 2
    a.plan["night_of"] = "2026-10-09"           # next night pages again
    asyncio.run(a._apps_alert("At arm"))
    assert len(notes) == 3


def test_armer_quiet_when_everything_answers(tmp_path, monkeypatch):
    a, notes = _armer(tmp_path, monkeypatch, up=("nina1", "nina2", "phd2"))
    asyncio.run(a._apps_alert("At arm"))
    assert notes == []
    a.guiding_override = "unguided"             # PHD2 down on an unguided night
    monkeypatch.setattr(al, "probe", lambda c: _apps(c, up=("nina1", "nina2")))
    asyncio.run(a._apps_alert("Pre-config in 60 min"))
    assert notes == []


def test_armed_tick_pages_inside_the_hour_before_preconfig(tmp_path, monkeypatch):
    a, notes = _armer(tmp_path, monkeypatch, up=("nina1", "nina2"))
    import photonscript.scheduler.armer as armer_mod
    pc = datetime(2026, 10, 9, 0, 30)
    a.state = "ARMED"
    a.plan["preconfig_utc"] = pc.isoformat() + "Z"

    async def _noop(*a_, **k):
        return None
    monkeypatch.setattr(a, "_maybe_predusk_tune", _noop)
    monkeypatch.setattr(a, "_is_safe", _noop)

    class _Clock(datetime):
        now_value = pc - timedelta(minutes=90)

        @classmethod
        def utcnow(cls):
            return cls.now_value
    monkeypatch.setattr(armer_mod, "datetime", _Clock)
    asyncio.run(a._tick())
    assert notes == []                          # 90 min out: not yet
    _Clock.now_value = pc - timedelta(minutes=45)
    asyncio.run(a._tick())
    assert len(notes) == 1 and notes[0][0].startswith("Pre-config in 60 min: PHD2")
    asyncio.run(a._tick())
    assert len(notes) == 1


def test_arm_calls_the_apps_alert():
    src = (ROOT / "photonscript/scheduler/armer.py").read_text(encoding="utf-8")
    arm = src[src.index("    async def arm(self"):src.index("    async def _apps_alert")]
    assert 'await self._apps_alert("At arm")' in arm


# ------------------------------------------------------------------ 6. status + API

class _FakeArmer:
    def __init__(self, state="COMPLETE", shutdown=None, night="2026-10-07"):
        self.state = state
        self.shutdown = shutdown
        self.plan = {"night_of": night}

    def _use_guiding(self):
        return True


def test_status_view(tmp_path):
    cfg = _cfg(tmp_path, app_lifecycle_enabled=True)

    async def _seq(c):
        return {"NINA #1 (RC16)": [], "NINA #2 (Piggy-600)": None}
    out = asyncio.run(al.status(
        cfg, _FakeArmer(shutdown=_shutdown(90)), now=MORNING, seq_fn=_seq,
        thesky_fn=lambda: {"answering": True, "mount_connected": True},
        probe_fn=lambda: _apps(cfg, up=("nina1", "nina2", "thesky")),
        sun_fn=lambda now: 25.0, busy_fn=lambda c: []))
    assert out["enabled"] is True and out["stop_ok"] is True
    assert out["missing"] == ["phd2"] and out["needed"] == ["nina1", "nina2", "phd2"]
    assert out["thesky"]["mount_connected"] is True
    assert out["shutdown"]["verify_done"] is True
    assert out["launch"]["nina2_profile"] == "Piggy-600"
    assert out["start_utc"] == "2026-10-08T17:45:00Z"
    json.dumps(out)                              # JSON-able for the route

    blocked = asyncio.run(al.status(
        cfg, _FakeArmer(state="RUNNING"), now=MORNING, seq_fn=_seq,
        probe_fn=lambda: _apps(cfg, up=()), sun_fn=lambda now: -12.0))
    assert blocked["stop_ok"] is False and "armer is RUNNING" in blocked["stop_blockers"]
    assert blocked["thesky"] == {"answering": False}


def test_api_routes(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app as app_mod
    from photonscript.shared import pushover
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
    monkeypatch.setattr(app_mod, "get_armer", lambda: _FakeArmer(state="DISARMED", night=None))
    monkeypatch.setattr(al, "probe", lambda c: _apps(c, up=()))
    sent = []

    async def _fake_notify(c, msg, **kw):
        sent.append((msg, kw))
        return True
    monkeypatch.setattr(pushover, "notify", _fake_notify)
    client = TestClient(app_mod.app)
    r = client.get("/api/apps/status")
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert r.json()["missing"] == ["nina1", "nina2", "phd2"]
    body = {"mode": "start", "ok": False, "acted": False, "page": True,
            "message": "TheSky not ready", "steps": ["TheSky: 0 process(es)"]}
    r = client.post("/api/apps/report", json=body)
    assert r.status_code == 200 and r.json()["paged"] is True
    r = client.post("/api/apps/report", json=body)
    assert r.json()["paged"] is False                 # once a day
    assert len(sent) == 1 and sent[0][1]["priority"] == 1
    r = client.post("/api/apps/report", content=b"not json")
    assert r.status_code == 200 and r.json()["recorded"]["mode"] == "other"
    r = client.get("/api/apps/status")
    assert r.json()["last"]["start"]["message"] == "TheSky not ready"


def test_config_defaults_and_fields():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    c = PhotonScriptConfig(_env_file=None)
    assert c.app_lifecycle_enabled is False           # nothing acts by default
    assert c.app_lifecycle_stop_after_shutdown_min == 30.0
    assert c.app_lifecycle_start_local == "11:45"
    assert c.app_nina1_profile == "RC16" and c.app_nina2_profile == "Piggy-600"
    assert c.app_phd2_profile_id == 2
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    for env, typ in (("PS_APP_LIFECYCLE_ENABLED", "bool"),
                     ("PS_APP_LIFECYCLE_ALERT", "bool"),
                     ("PS_APP_LIFECYCLE_STOP_AFTER_SHUTDOWN_MIN", "float"),
                     ("PS_APP_LIFECYCLE_START_LOCAL", "str"),
                     ("PS_APP_NINA_EXE", "str"), ("PS_APP_NINA1_PROFILE", "str"),
                     ("PS_APP_NINA2_PROFILE", "str"), ("PS_APP_PHD2_EXE", "str"),
                     ("PS_APP_PHD2_PROFILE_ID", "int")):
        assert by_env[env][4] == typ and hasattr(c, by_env[env][0])
        assert by_env[env][3] == "App lifecycle"


def test_system_panel_and_report_only_service_side():
    html = (ROOT / "photonscript/scheduler/templates/system.html").read_text(encoding="utf-8")
    assert 'id="appsPanel"' in html and "/api/apps/status" in html
    for name in ("photonscript/scheduler/app_lifecycle.py",
                 "photonscript/scheduler/routers/apps.py"):
        src = (ROOT / name).read_text(encoding="utf-8")
        assert src.isascii() and "\u2014" not in src
        # the service never launches or kills an app
        for bad in ("subprocess", "Popen", "os.startfile", ".kill(", "terminate("):
            assert bad not in src, (name, bad)


# ------------------------------------------------------------------ 7. PowerShell

def _ps(name: str) -> str:
    return (DEPLOY / name).read_bytes().decode("ascii")   # also: pure ASCII


@pytest.mark.parametrize("name", ["observatory-apps.ps1",
                                  "install-app-lifecycle-tasks.ps1"])
def test_scripts_parse_in_powershell(name):
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        pytest.skip("no PowerShell on this machine")
    path = str(DEPLOY / name).replace("'", "''")
    cmd = ("$e = $null; $t = $null; [void][System.Management.Automation.Language."
           f"Parser]::ParseFile('{path}', [ref]$t, [ref]$e); "
           "if ($e) { $e | ForEach-Object { $_.ToString() }; exit 1 } else { exit 0 }")
    r = subprocess.run([exe, "-NoProfile", "-NonInteractive", "-Command", cmd],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr


def test_script_guard_rails():
    s = _ps("observatory-apps.ps1")
    assert "\u2014" not in s
    # scheduled runs obey the enabled flag; a stop needs the service's guards
    assert "if ($Scheduled -and $st -and -not $st.enabled)" in s
    assert "REFUSED: the PhotonScript service does not answer" in s
    assert "$st.stop_blockers" in s and "$st.stop_done_today" in s
    # -Force waives only the timing blockers, never the armer / sequence / sun
    waive = s[s.index("$waivable = "):s.index("\n", s.index("$waivable = "))]
    assert "waiting until" in waive and "armer" not in waive and "sun" not in waive
    # PHD2 graceful order, NINA closed by window, kill only after the timeout
    i1, i2, i3 = (s.index('@("stop_capture"'), s.index('@("set_connected", @($false))'),
                  s.index('@("shutdown"'))
    assert i1 < i2 < i3
    close = s[s.index("function Close-Gracefully"):s.index("# ---", s.index("function Close-Gracefully"))]
    assert close.index("CloseMainWindow") < close.index("WaitForExit") < close.index("Stop-Process")
    # TheSky only with -IncludeTheSky; its check is read-only
    assert s.count("Close-Gracefully $p \"TheSky\"") == 1
    sky = s[s.index("if ($IncludeTheSky) {\n        foreach"):]
    assert sky.index("Close-Gracefully") < 200
    tsk = s[s.index("function Get-TheSkyMount"):s.index("# ---", s.index("function Get-TheSkyMount"))]
    assert "IsConnected" in tsk
    for bad in ("Connect()", "Park", "SlewTo", "Disconnect"):
        assert bad not in tsk
    # NINA is never launched without a resolved profile id
    assert '-ArgumentList @("--profileid", $pid2)' in s
    assert s.index("Resolve-NinaProfile $n[2]") < s.index('-ArgumentList @("--profileid"')
    assert "NOT launched" in s
    # start is idempotent and ends in connect + preflight without a test push
    assert "already answers" in s
    assert '"/api/equipment/connect"' in s and '"/api/preflight?push=0"' in s
    assert '"/api/apps/report"' in s


def test_installer_registers_two_interactive_tasks():
    s = _ps("install-app-lifecycle-tasks.ps1")
    assert '[string]$StopTask = "PhotonScript Apps Stop"' in s
    assert '[string]$StartTask = "PhotonScript Apps Start"' in s
    assert "-LogonType Interactive" in s and "S4U" not in s.replace("session-0", "")
    assert '[string]$StartAt = "11:45"' in s
    assert "-Stop" in s and "-Start" in s and "-Scheduled" in s
    assert "$stopTrigger.Repetition = $rep.Repetition" in s
    assert "[switch]$DryRun" in s and "[switch]$Uninstall" in s
    assert "Unregister-ScheduledTask" in s
