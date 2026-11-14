"""PS-255: pre-arm readiness, one checklist of everything the night needs."""
import asyncio
from datetime import datetime, timedelta

from photonscript.scheduler import prearm as pa
from photonscript.shared.config import PhotonScriptConfig


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"), **kw)


# ------------------------------------------------------------------ Windows

def test_active_hours_cover_wraps_midnight():
    # 11-13 values: 16:00 to 07:00; tonight needs 15:10 (arm window) to 06:45
    assert not pa.active_hours_cover(16, 7, 15 + 10 / 60, 6.75)
    assert pa.active_hours_cover(15, 9, 15 + 10 / 60, 6.75)
    assert pa.active_hours_cover(8, 17, 9, 16)
    assert not pa.active_hours_cover(None, 7, 15, 6)
    assert pa.suggest_active_hours(15 + 10 / 60, 6.75) == (15, 7)
    assert pa.suggest_active_hours(14, 10) == (14, 8)      # Windows caps at 18 h


def test_windows_rows_flag_pending_restart_and_short_active_hours():
    rows = pa.windows_rows({"ok": True, "reboot_pending": ["Windows Update"],
                            "active_start": 16, "active_end": 7, "paused": False},
                           15 + 10 / 60, 6.75)
    by = {r["label"]: r for r in rows}
    assert by["Restart pending"]["status"] == "warn"
    ah = by["Active hours cover the night"]
    assert ah["status"] == "warn" and "15:00 to 7:00" in ah["fix"]
    assert by["Updates paused"]["current"] == "not paused"
    assert pa.windows_rows({"ok": False, "note": "not Windows"}, 1, 2)[0]["status"] == "unknown"


def test_pnp_errors_com_port_fails_and_disabled_is_ignored():
    rows = pa.pnp_rows([
        {"Name": "USB Serial Device (COM3)", "PNPClass": "Ports",
         "ConfigManagerErrorCode": 10, "DeviceID": "USB\\VID_1\\X"},
        {"Name": "Bluetooth radio", "PNPClass": "Bluetooth", "ConfigManagerErrorCode": 43},
        {"Name": "Old NIC", "PNPClass": "Net", "ConfigManagerErrorCode": 22}])
    assert [r["status"] for r in rows] == ["fail", "warn"]
    assert "COM3" in rows[0]["label"] and "code 10" in rows[0]["current"]
    assert "Disable device" in rows[0]["fix"]


# ------------------------------------------------------------------ rows

def test_device_rows_critical_fail_others_warn_skips_info():
    rows = pa.device_rows({"rc16": {"camera": "connected", "mount": "Error 201",
                                    "guider": "skipped (unguided night)",
                                    "weather": "not connected"},
                           "piggyback": {"camera": "connected", "flatdevice": "ReadTimeout"}})
    st = {r["label"]: r["status"] for r in rows}
    assert st == {"rc16 camera": "pass", "rc16 mount": "fail", "rc16 guider": "info",
                  "rc16 weather": "warn", "piggyback camera": "pass",
                  "piggyback flatdevice": "warn"}
    assert "NINA #2" in next(r for r in rows if r["label"] == "piggyback flatdevice")["fix"]


def test_protrack_greyed_is_wait_not_off():
    rec = {"row": {"status": "warn", "current": "not judged (mount not connected)",
                   "protrack": "unknown"}}
    r = pa.protrack_row(rec)
    assert r["status"] == "wait" and "unparks" in r["note"]
    r = pa.protrack_row({"row": {"status": "fail", "current": "OFF", "note": "unticked"}})
    assert r["status"] == "fail" and r["fix"] == "unticked"


def test_ledger_rows_keep_problems_and_skip_the_protrack_duplicate():
    rules = {"rows": [
        {"id": "rc16_cooler_setpoint", "app": "nina1", "label": "RC16 cooler setpoint",
         "status": "fail", "current": 10, "desired": "0", "fix": "Equipment > Camera"},
        {"id": "thesky_protrack", "app": "thesky", "label": "ProTrack on", "status": "unknown"},
        {"id": "ps_x", "app": "photonscript", "label": "X", "status": "warn"},
        {"id": "ok", "app": "nina1", "label": "fine", "status": "pass"}],
        "counts": {"fail": 1, "warn": 1, "pass": 1, "unknown": 1}}
    rows = pa.ledger_rows(rules)
    ids = [r["id"] for r in rows]
    assert ids == ["ledger:rc16_cooler_setpoint", "ledger:ps_x", "ledger:counts"]
    assert rows[1]["who"] == "claude" and rows[0]["who"] == "jeremy"


def test_mount_rows_disconnected_and_skew():
    assert pa.mount_rows(None, None)[0]["status"] == "fail"
    rows = pa.mount_rows({"connected": False}, None)
    assert rows[0]["status"] == "fail" and "201" in rows[0]["fix"]
    rows = pa.mount_rows({"connected": True, "parked": True, "tracking": False},
                         {"clock_skew_s": 0.01, "lst_skew_s": 9.0})
    st = {r["label"]: r["status"] for r in rows}
    assert st["Mount connected in TheSky"] == "pass"
    assert st["TheSky clock vs PC (s)"] == "pass" and st["TheSky LST vs computed (s)"] == "fail"


def test_live_rows_focuser_without_temperature_warns():
    rows = pa.live_rows({"rc16": {"camera": (0.0, 28.0), "focuser": None, "weather": 1},
                         "piggyback": {"camera": None, "focuser": 5, "weather": None}})
    st = {r["label"]: r["status"] for r in rows}
    assert st == {"rc16 camera": "pass", "rc16 focuser": "warn", "rc16 weather": "pass",
                  "piggyback camera": "warn", "piggyback focuser": "pass"}


# ------------------------------------------------------------------ push

def _rows():
    return [pa.row("Devices", "rc16 mount", "fail", current="Error 201", fix="reconnect",
                   rid="dev:rc16:mount"),
            pa.row("Basics", "Observatory apps running", "warn", who="auto"),
            pa.row("ProTrack", "ProTrack", "wait"),
            pa.row("Windows", "Active hours", "warn", fix="15 to 9", rid="win:ah")]


def test_push_only_what_needs_jeremy_and_not_repeated():
    rows = _rows()
    assert [r["id"] for r in pa.attention(rows)] == ["dev:rc16:mount", "win:ah"]
    txt = pa.push_text(rows, "https://x/prearm")
    assert txt.startswith("FAIL rc16 mount: Error 201. Fix: reconnect")
    assert "apps" not in txt and txt.endswith("https://x/prearm")
    assert pa.should_push(rows, "after_start", None)
    ids = ["dev:rc16:mount", "win:ah"]
    assert not pa.should_push(rows, "manual", ids)
    assert pa.should_push(rows, "before_arm", ids)          # a fail remains
    assert not pa.should_push(rows[1:], "before_arm", ["win:ah"])   # warn only, unchanged
    assert not pa.should_push([rows[2]], "after_start", None)


# ------------------------------------------------------------------ schedule

def test_slots_due_once_within_grace():
    t = datetime(2026, 11, 14, 19, 15)
    slots = {"after_start": t, "before_arm": t + timedelta(hours=2)}
    assert pa.due_slot(slots, [], t - timedelta(minutes=1)) is None
    assert pa.due_slot(slots, [], t + timedelta(minutes=5)) == "after_start"
    assert pa.due_slot(slots, ["after_start"], t + timedelta(minutes=5)) is None
    assert pa.due_slot(slots, ["after_start"], t + timedelta(hours=2, minutes=1)) == "before_arm"
    assert pa.due_slot(slots, [], t + timedelta(hours=5)) is None


def test_night_window_puts_the_second_slot_before_the_arm(tmp_path):
    cfg = _cfg(tmp_path)
    now = datetime(2026, 11, 14, 17, 0)      # 10:00 MST
    win = pa.night_window(cfg, now)
    slots = pa.slot_times(cfg, win)
    assert slots["after_start"] == win["app_start_utc"] + timedelta(minutes=30)
    if win.get("arm_open_utc"):
        assert slots["before_arm"] == win["arm_open_utc"] - timedelta(minutes=60)
        assert slots["after_start"] < slots["before_arm"]


# ------------------------------------------------------------------ run

class _Armer:
    state = "COMPLETE"

    async def connect_all_rigs(self):
        return {"rc16": {"camera": "connected", "mount": "Error 201"}}

    def _use_guiding(self):
        return False


def test_run_builds_saves_and_pushes_once(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, auto_arm_enabled=True)

    async def pf(cfg, test_push=True):
        return {"checks": [{"name": "Disk space", "status": "pass", "detail": "900 GB"}]}

    async def no_read(cfg, rig):
        return {"camera": (0.0, 1.0), "focuser": 18.0, "weather": 1}

    async def take(cfg, reason, armer_state=None, push=None):
        return {"rules": {"rows": [], "counts": {"pass": 70}}}

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
    monkeypatch.setattr(pr, "recheck", lambda *a, **k: {"row": {
        "status": "info", "current": "not judged (parked)"}})
    monkeypatch.setattr(tpp, "evaluate", lambda cfg, guided=False: {"level": "ok", "line": "ok"})
    monkeypatch.setattr(pa, "read_windows", lambda: {"ok": False, "note": "test"})
    monkeypatch.setattr(pa, "read_pnp", lambda: ([], None))
    sent = []

    async def notify(cfg, msg, **kw):
        sent.append((msg, kw))

    now = datetime(2026, 11, 14, 19, 20)
    rep = asyncio.run(pa.run(cfg, _Armer(), trigger="after_start", push=True, now=now,
                             notify=notify))
    assert rep["go"] is False and rep["attention_ids"] == ["dev:rc16:mount"]
    assert len(sent) == 1 and "rc16 mount" in sent[0][0]
    secs = [r["section"] for r in rep["rows"]]
    assert secs == sorted(secs, key=pa.SECTIONS.index)
    assert {r["status"] for r in rep["rows"] if r["section"] == "ProTrack"} == {"wait"}
    day = pa.load_day(cfg, rep["day"])
    assert day["done"] == ["after_start"] and day["pushed"] == ["dev:rc16:mount"]
    assert pa.latest(cfg)["trigger"] == "after_start"
    # same problems at a manual run: no second push
    asyncio.run(pa.run(cfg, _Armer(), trigger="manual", push=True, now=now, notify=notify))
    assert len(sent) == 1
    assert "rc16 mount" in pa.page_html(pa.latest(cfg))


def test_routes_are_defined_and_included():
    from pathlib import Path
    from photonscript.scheduler.routers import prearm as r
    paths = {getattr(x, "path", "") for x in r.router.routes}
    assert {"/prearm", "/api/prearm", "/api/prearm/run"} <= paths
    src = (Path(r.__file__).parents[1] / "app.py").read_text(encoding="utf-8")
    assert "app.include_router(_prearm_router.router)" in src
    assert "from photonscript.scheduler.prearm import run_loop" in src


# ------------------------------------------------------------------ ProTrack text

def test_arm_does_not_push_protrack_while_not_judged(monkeypatch):
    from photonscript.scheduler import armer as am
    sent = []

    async def notify(cfg, msg, **kw):
        sent.append(msg)
    monkeypatch.setattr(am, "notify", notify)
    a = am.Armer.__new__(am.Armer)
    a.config = PhotonScriptConfig(_env_file=None)
    for cur in ("not judged (parked)", "not judged (mount not connected)"):
        asyncio.run(a._warn_protrack_unguided(
            {"rows": [{"id": "protrack_on", "status": "warn", "current": cur,
                       "protrack": "unknown"}]}))
    assert sent == []
    asyncio.run(a._warn_protrack_unguided(
        {"rows": [{"id": "protrack_on", "status": "fail", "current": "OFF", "protrack": "off"}]}))
    assert len(sent) == 1 and "ProTrack is OFF" in sent[0]
