"""Settings ledger: snapshots of every program's settings, stored only when
they change, judged by config/settings/desired_settings.toml, linked to subs.

Fixtures (fixtures/settings_ledger/) are REAL reads, all GETs, taken
2026-10-13 about 11:56Z while the armer was RUNNING:
  nina1_2026-10-13.json / nina2_*.json   ninaAPI 2.2.15.2 on the scope PC
      (:1888 RC16, :1889 piggyback600): profile/show?active=true and every
      equipment/<device>/info reply
  phd2_audit_2026-10-12.json   GET /api/phd2/audit (the stored PS-89 audit,
      11:47Z, PHD2 Stopped, calibration FAIL, PulseGuideFailedAlert silenced)
  thesky_audit_2026-10-12.json GET /api/thesky/audit (the arm-time PS-104
      audit, 03:44Z, TheSky time zone +0 / no DST: fixed by hand later)
  subs_2026-10-12.jsonl        7 sub records of that night (trimmed fields)
PHD2's registry profile and TheSky's live scripts are not reachable from
the desktop, so the PHD2 and TheSky sections here come from the audits
(the ledger's own fallback) or are built inline."""
import asyncio
import json
import shutil
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from photonscript.scheduler import settings_ledger as sl
from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig

FIX = Path(__file__).parent / "fixtures" / "settings_ledger"
ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 13, 3, 0, 0)          # night 2026-10-12
NIGHT = "2026-10-12"


def _cfg(tmp_path, **kw):
    kw.setdefault("data_dir", tmp_path / "data")
    kw.setdefault("settings_ledger_enabled", True)
    kw.setdefault("piggyback_enabled", True)
    kw.setdefault("phd2_darks_dir", str(tmp_path / "darks"))
    return PhotonScriptConfig(_env_file=None, **kw)


def _nina_fix(name):
    return json.loads((FIX / f"{name}_2026-10-13.json").read_text(encoding="utf-8"))


def _nina_collector(name, mutate=None):
    """A collector that answers from the real NINA capture."""
    def fn(config):
        fx = _nina_fix(name)
        if mutate:
            mutate(fx)
        settings = {"profile": sl.nina_profile_settings(fx["profile"]), "devices": {}}
        live = {}
        for d, body in fx["devices"].items():
            s, lv = sl.split_device(d, body["Response"])
            settings["devices"][d] = sl.redact(s)
            if lv:
                live[d] = lv
        return settings, live, {"ok": True, "note": "fixture"}
    return fn


def _down(config):
    return {}, {}, {"ok": False, "note": "not reachable"}


def _phd2(min_snr=10, state="Stopped", scale=0.381031, binning=3, grade="PASS",
          silenced=(), darks=True):
    def fn(config):
        return ({"api": {"profile": "Primary RC Profile (Guider)", "profile_id": 2,
                         "camera": "GP678C", "binning": binning, "pixel_scale": scale,
                         "exposure_ms": 2000, "search_region_px": 35,
                         "ra_params": {"algorithmName": "Lowpass2", "minMove": 0.56}},
                 "profile": {"id": "2", "min_snr": min_snr, "min_hfd_px": 1.6,
                             "auto_load_darks": darks, "auto_load_defect_map": False},
                 "silenced_alerts": list(silenced),
                 "calibration_record": {"grade": grade}},
                {"app_state": state}, {"ok": True, "note": "fake"})
    return fn


def _thesky(tz=-7.0, dst=17, protrack=True, tracking=True):
    def fn(config):
        return ({"time_zone": tz, "dst_index": dst, "use_computer_clock": True,
                 "protrack_on": protrack, "tpoint_model_active": True, "tpoint_points": 356,
                 "tpoint_rms_arcsec": 19.9, "site_latitude": 31.9069,
                 "site_longitude": 109.021},
                {"mount_tracking": tracking, "mount_parked": False},
                {"ok": True, "note": "fake"})
    return fn


def _collectors(**over):
    c = {"nina1": _nina_collector("nina1"), "nina2": _nina_collector("nina2"),
         "phd2": _phd2(), "thesky": _thesky(),
         "photonscript": sl.collect_photonscript}
    c.update(over)
    return c


def _take(cfg, when=T0, state="RUNNING", pushes=None, **over):
    async def go():
        return await sl.take(cfg, "test", armer_state=state, collectors=_collectors(**over),
                             now=when)
    return asyncio.run(go())


@pytest.fixture
def pushes(monkeypatch):
    sent = []

    async def _notify(config, msg, **kw):
        sent.append(msg)
        return True
    from photonscript.shared import pushover
    monkeypatch.setattr(pushover, "notify", _notify)
    return sent


def _row(res, rid):
    return next(r for r in res["rules"]["rows"] if r["id"] == rid)


# --------------------------------------------------------------------------
# the rules file and the pure helpers
# --------------------------------------------------------------------------

def test_shipped_rules_file_lints_clean_and_is_ascii():
    import tomllib
    text = sl.DEFAULT_RULES.read_text(encoding="utf-8")
    assert text.isascii() and chr(0x2014) not in text
    doc = tomllib.loads(text)
    assert sl.lint_rules(doc) == []
    ids = {r["id"] for r in doc["rule"]}
    # the 2026-10-12 problems each have a rule
    for rid in ("phd2_min_snr", "phd2_min_hfd", "phd2_pulse_alert", "phd2_darks_or_bpm",
                "phd2_cal_guiding", "nina1_platesolve_binning", "nina1_camera_gain",
                "nina2_camera_readout", "nina1_cooler_setpoint", "phd2_guide_scale",
                "thesky_time_zone", "thesky_dst", "thesky_protrack",
                "nina1_mount_tracking_rate", "nina1_focuser_reset"):
        assert rid in ids


def test_lint_catches_bad_rules():
    bad = {"rule": [{"id": "x", "app": "nope", "label": "", "equals": 1},
                    {"id": "x", "app": "nina1", "label": "L", "computed": "zzz",
                     "severity": "fail", "why": "w", "fix": "f", "when": ["sometimes"]}]}
    out = " | ".join(sl.lint_rules(bad))
    for frag in ("app must be", "no label", "severity", "duplicate id", "unknown computed",
                 "unknown when", "no path"):
        assert frag in out


def test_split_device_on_real_replies():
    fx = _nina_fix("nina1")
    s, live = sl.split_device("camera", fx["devices"]["camera"]["Response"])
    assert s["Gain"] == 200 and s["Offset"] == 256 and s["TemperatureSetPoint"] == 0
    assert s["ReadoutModeName"] == "High Conversion Gain"
    assert "Temperature" not in s and live["Temperature"] == 0 and "CoolerPower" in live
    assert "ExposureEndTime" not in s and "ExposureEndTime" not in live
    m, mlive = sl.split_device("mount", fx["devices"]["mount"]["Response"])
    assert "RightAscension" not in m and "Coordinates" not in m
    assert mlive["TrackingMode"] == "Sidereal" and m["GuideRateRightAscensionArcsecPerSec"]
    f, flive = sl.split_device("focuser", fx["devices"]["focuser"]["Response"])
    assert flive["Position"] == 5868 and "Position" not in f and f["StepSize"] == 600000
    p2, _ = sl.split_device("camera", _nina_fix("nina2")["devices"]["camera"]["Response"])
    assert p2["ReadoutModeName"] == "Low Conversion Gain" and p2["Gain"] == 100


def test_redact_and_profile():
    prof = _nina_fix("nina1")["profile"]
    prof["PlateSolveSettings"]["AstrometryAPIKey"] = "abc123"
    out = sl.nina_profile_settings(prof)
    assert "LastUsed" not in out
    assert out["PlateSolveSettings"]["AstrometryAPIKey"] == sl.REDACTED
    assert out["WeatherDataSettings"]["OpenWeatherMapAPIKey"] == ""   # empty stays empty
    assert out["PlateSolveSettings"]["Binning"] == 1
    cfg_out, _, meta = sl.collect_photonscript(PhotonScriptConfig(
        _env_file=None, pushover_api_token="tok", astrobin_api_secret="s"))
    assert meta["ok"] and cfg_out["pushover_api_token"] == sl.REDACTED
    assert cfg_out["astrobin_api_secret"] == sl.REDACTED and cfg_out["default_gain"] == 200


def test_flatten_diff_resolve():
    a = {"p": {"x": 1, "l": [{"n": "L", "o": 0}]}}
    b = {"p": {"x": 2, "l": [{"n": "L", "o": 5}], "y": True}}
    d = sl.diff_settings(a, b)
    assert d == {"p.l.0.o": [0, 5], "p.x": [1, 2], "p.y": [None, True]}
    assert sl.resolve(b, "p.l.0.n") == "L" and sl.resolve(b, "p.q.z") is None
    assert sl.digest(a) == sl.digest(json.loads(json.dumps(a)))


def test_parse_current():
    assert sl.parse_current("on") is True and sl.parse_current("off") is False
    assert sl.parse_current("1.6") == 1.6 and sl.parse_current("3") == 3
    assert sl.parse_current("manual (2026-10-13): on") is True
    assert sl.parse_current("109.021; LST says 109.02 W") == 109.021
    assert sl.parse_current("-") is None and sl.parse_current("GP678C") == "GP678C"


# --------------------------------------------------------------------------
# collecting NINA over ninaAPI (mock transport over the real replies)
# --------------------------------------------------------------------------

def _nina_transport(name, fail=()):
    fx = _nina_fix(name)

    def handler(req: httpx.Request):
        path = req.url.path
        if path.endswith("/profile/show"):
            assert req.url.params.get("active") == "true"
            return httpx.Response(200, json={"Response": fx["profile"], "Success": True})
        dev = path.split("/equipment/")[-1].split("/")[0]
        if dev in fail:
            return httpx.Response(500)
        return httpx.Response(200, json=fx["devices"][dev])
    return httpx.MockTransport(handler)


def test_collect_nina_reads_profile_and_devices(tmp_path):
    cfg = _cfg(tmp_path)

    async def go():
        http = httpx.AsyncClient(base_url="http://nina/v2/api",
                                 transport=_nina_transport("nina1", fail=("rotator",)))
        try:
            return await sl.collect_nina(cfg, "http://nina/v2/api", http=http)
        finally:
            await http.aclose()
    s, live, meta = asyncio.run(go())
    assert meta["ok"] and "rotator" in meta["note"]
    assert s["profile"]["Name"] == "RC16" and s["devices"]["rotator"] is None
    assert s["devices"]["camera"]["Gain"] == 200 and live["focuser"]["Position"] == 5868


def test_collect_nina_down_returns_fast(tmp_path):
    cfg = _cfg(tmp_path)

    def handler(req):
        raise httpx.ConnectError("refused")

    async def go():
        http = httpx.AsyncClient(base_url="http://nina", transport=httpx.MockTransport(handler))
        try:
            return await sl.collect_nina(cfg, "http://nina", http=http)
        finally:
            await http.aclose()
    s, live, meta = asyncio.run(go())
    assert meta["ok"] is False and s["devices"] is None and live == {}


# --------------------------------------------------------------------------
# taking snapshots: store when changed, carry, diff, push once
# --------------------------------------------------------------------------

def test_take_stores_only_on_change_and_pushes_once(tmp_path, pushes):
    cfg = _cfg(tmp_path)
    r1 = _take(cfg)
    assert r1["changed"] and r1["id"] == "20261013T030000Z"
    assert sl.snap_path(cfg, r1["id"]).exists()
    assert r1["rules"]["counts"].get("fail", 0) == 0, sl.format_rules(r1["rules"]["rows"])
    assert pushes == []
    r2 = _take(cfg, T0 + timedelta(hours=1))
    assert not r2["changed"] and r2["id"] == r1["id"]
    assert len(list(sl.night_dir(cfg, NIGHT).glob("2026*.json"))) == 1
    assert len(sl.index(cfg, NIGHT)) == 2

    def bin2(fx):
        fx["profile"]["PlateSolveSettings"]["Binning"] = 2
    r3 = _take(cfg, T0 + timedelta(hours=2), nina1=_nina_collector("nina1", bin2))
    assert r3["changed"] and r3["diff"] == {"nina1": {"profile.PlateSolveSettings.Binning": [1, 2]}}
    assert _row(r3, "nina1_platesolve_binning")["status"] == "fail"
    assert r3["new_fails"] == ["nina1_platesolve_binning"]
    assert len(pushes) == 1 and "plate solve binning 2 (want 1)" in pushes[0]
    r4 = _take(cfg, T0 + timedelta(hours=3), nina1=_nina_collector("nina1", bin2))
    assert r4["new_fails"] == [] and len(pushes) == 1          # once per rule per night
    st = sl.load_status(cfg, NIGHT)
    assert st["ever_failed"] == ["nina1_platesolve_binning"]
    assert st["last_change"]["nina1"] == "2026-10-13T05:00:00Z"
    ptr = sl.load_latest_pointer(cfg)
    assert ptr["id"] == r3["id"] and sl.current_id(cfg) == r3["id"]


def test_unreachable_app_is_carried_without_churn(tmp_path, pushes):
    cfg = _cfg(tmp_path)
    r1 = _take(cfg)
    r2 = _take(cfg, T0 + timedelta(hours=1), nina1=_down, phd2=_down)
    assert not r2["changed"] and r2["unread"] == ["nina1", "phd2"]
    sec = r2["snapshot"]["apps"]["nina1"]
    assert sec["carried"] and sec["settings"]["profile"]["Name"] == "RC16"
    row = _row(r2, "nina1_camera_gain")
    assert row["status"] == "pass" and "not read now" in row["note"]
    # live-only rules are not judged on a carried section
    assert _row(r2, "nina1_mount_tracking_rate")["status"] in ("unknown", "na")
    assert r1["id"] == r2["id"]


def test_part_carry_inside_nina(tmp_path, pushes):
    cfg = _cfg(tmp_path)
    _take(cfg)

    def no_cam(fx):
        fx["devices"]["camera"] = {"Response": None}
    r = _take(cfg, T0 + timedelta(hours=1), nina1=_nina_collector("nina1", no_cam))
    # a None device keeps the previous reading (no diff, no hash change)
    assert not r["changed"]


def test_audit_fallback_when_never_read(tmp_path, pushes):
    cfg = _cfg(tmp_path)
    from photonscript.scheduler import phd2_audit, thesky_audit
    phd2_audit.audit_dir(cfg).mkdir(parents=True, exist_ok=True)
    thesky_audit.audit_dir(cfg).mkdir(parents=True, exist_ok=True)
    shutil.copy(FIX / "phd2_audit_2026-10-12.json", phd2_audit.audit_dir(cfg) / "latest.json")
    shutil.copy(FIX / "thesky_audit_2026-10-12.json", thesky_audit.audit_dir(cfg) / "latest.json")
    r = _take(cfg, phd2=_down, thesky=_down)
    phd2 = r["snapshot"]["apps"]["phd2"]
    assert phd2["carried"] and phd2["source"] == "newest stored audit"
    assert phd2["read_utc"] == "2026-10-13T11:47:58Z"
    # the real 2026-10-12 findings
    assert _row(r, "phd2_pulse_alert")["status"] == "warn"
    assert _row(r, "phd2_darks_or_bpm")["status"] == "warn"
    assert _row(r, "phd2_cal_grade")["status"] == "warn"
    assert _row(r, "phd2_min_snr")["status"] == "unknown"     # PS-89 does not report it
    assert _row(r, "phd2_guide_scale")["status"] == "pass"    # 0.381 vs 0.377 at bin 3
    assert _row(r, "thesky_time_zone")["status"] == "fail"
    assert _row(r, "thesky_time_zone")["current"] == "0"
    assert _row(r, "thesky_dst")["status"] == "fail"
    assert _row(r, "thesky_tpoint_points")["status"] == "pass"
    assert set(r["new_fails"]) == {"thesky_time_zone", "thesky_dst"}


# --------------------------------------------------------------------------
# the rules on the real captures and on the 2026-10-12 failure modes
# --------------------------------------------------------------------------

def test_real_nina_captures_pass_the_camera_rules(tmp_path, pushes):
    r = _take(_cfg(tmp_path))
    for rid in ("nina1_camera_gain", "nina1_camera_offset", "nina1_camera_readout",
                "nina1_cooler_setpoint", "nina1_platesolve_binning", "nina1_phd2_profile",
                "nina2_camera_gain", "nina2_camera_offset", "nina2_camera_readout",
                "nina1_mount_tracking_rate", "ps_gain_matches_nina1"):
        assert _row(r, rid)["status"] == "pass", (rid, _row(r, rid))
    # the real NINA #1 settle: 5 px x 0.381"/px = 1.9" (PS-89 wants 0.5 to 1)
    s = _row(r, "nina1_settle_arcsec")
    assert s["status"] == "warn" and s["current"] == '5 px = 1.91"'
    assert _row(r, "nina2_no_phd2")["status"] == "pass"       # NINA #2 guider disconnected


def test_phd2_rules(tmp_path, pushes):
    cfg = _cfg(tmp_path)
    r = _take(cfg, phd2=_phd2(min_snr=50, silenced=("PulseGuideFailedAlert",), darks=False))
    assert _row(r, "phd2_min_snr")["status"] == "fail"
    assert _row(r, "phd2_min_snr")["current"] == "50"
    assert _row(r, "phd2_pulse_alert")["status"] == "warn"
    assert _row(r, "phd2_darks_or_bpm")["status"] == "warn"
    g = _take(cfg, T0 + timedelta(hours=1), phd2=_phd2(state="Guiding", grade="FAIL"))
    assert _row(g, "phd2_cal_guiding")["status"] == "fail"
    assert _row(g, "phd2_cal_grade")["status"] == "na"
    s = _take(cfg, T0 + timedelta(hours=2), phd2=_phd2(state="Stopped", grade="FAIL"))
    assert _row(s, "phd2_cal_guiding")["status"] == "na"
    assert _row(s, "phd2_cal_grade")["status"] == "warn"
    # a 5.4x guide-scale mismatch (2.06 vs 0.38"/px)
    m = _take(cfg, T0 + timedelta(hours=3), phd2=_phd2(scale=2.06))
    row = _row(m, "phd2_guide_scale")
    assert row["status"] == "fail" and "x PhotonScript" in row["note"]


def test_thesky_rules(tmp_path, pushes):
    cfg = _cfg(tmp_path)
    r = _take(cfg, thesky=_thesky(tz=0.0, dst=0, protrack=False))
    assert _row(r, "thesky_time_zone")["status"] == "fail"
    assert _row(r, "thesky_dst")["status"] == "fail"
    assert _row(r, "thesky_protrack")["status"] == "fail"
    ok = _take(cfg, T0 + timedelta(hours=1), thesky=_thesky())
    for rid in ("thesky_time_zone", "thesky_dst", "thesky_protrack", "thesky_site_latitude",
                "thesky_site_longitude", "thesky_computer_clock"):
        assert _row(ok, rid)["status"] == "pass", rid
    parked = _take(cfg, T0 + timedelta(hours=2), thesky=_thesky(protrack=False, tracking=False))
    assert _row(parked, "thesky_protrack")["status"] == "na"   # greyed while parked


def test_running_only_rules_and_disconnected_devices(tmp_path, pushes):
    cfg = _cfg(tmp_path)

    def warm(fx):
        fx["devices"]["camera"]["Response"]["TemperatureSetPoint"] = 20
        fx["devices"]["safetymonitor"]["Response"]["Connected"] = False
    day = _take(cfg, state="DISARMED", nina1=_nina_collector("nina1", warm))
    assert _row(day, "nina1_cooler_setpoint")["status"] == "na"
    assert _row(day, "nina1_safety_connected")["status"] == "na"
    night = _take(cfg, T0 + timedelta(hours=1), nina1=_nina_collector("nina1", warm))
    assert _row(night, "nina1_cooler_setpoint")["status"] == "fail"
    assert _row(night, "nina1_safety_connected")["status"] == "fail"

    def unplugged(fx):
        fx["devices"]["camera"]["Response"].update(Connected=False, Gain=0)
    off = _take(cfg, T0 + timedelta(hours=2), nina1=_nina_collector("nina1", unplugged))
    assert _row(off, "nina1_camera_gain")["status"] == "na"


def test_focus_offsets_and_focuser_reset(tmp_path, pushes):
    cfg = _cfg(tmp_path, focus_filter_offsets="Ha:120")

    def nina_offsets(fx):
        fx["profile"]["FocuserSettings"]["UseFilterWheelOffsets"] = True
    r = _take(cfg, nina1=_nina_collector("nina1", nina_offsets))
    assert _row(r, "nina1_focus_offsets_once")["status"] == "fail"
    ok = _take(cfg, T0 + timedelta(hours=1))
    assert _row(ok, "nina1_focus_offsets_once")["status"] == "pass"

    def zeroed(fx):
        fx["devices"]["focuser"]["Response"]["Position"] = 3
    z = _take(cfg, T0 + timedelta(hours=2), nina1=_nina_collector("nina1", zeroed))
    row = _row(z, "nina1_focuser_reset")
    assert row["status"] == "fail" and row["current"] == "3 (was 5868)"
    assert not z["changed"]          # a position is a live reading, not a setting


def test_focuser_reset_dawn_disconnect_is_no_alarm(tmp_path, pushes):
    """2026-10-24 replay: nina1 focuser 5674 at the RUNNING -> PAUSED_UNSAFE
    snapshot, then 0 at the PAUSED_UNSAFE -> COMPLETE one at 12:39Z, where
    the dawn shutdown had disconnected the devices (focuser Temperature NaN,
    camera Temperature 0 / CameraState NoState, weather NaN). Not a reset."""
    cfg = _cfg(tmp_path)

    def at_5674(fx):
        fx["devices"]["focuser"]["Response"]["Position"] = 5674
    a = _take(cfg, T0, state="PAUSED_UNSAFE", nina1=_nina_collector("nina1", at_5674))
    assert _row(a, "nina1_focuser_reset")["status"] == "pass"

    def dawn(fx):
        # the live readings of that snapshot carried no Connected field
        f = fx["devices"]["focuser"]["Response"]
        f.pop("Connected", None)
        f.update(Position=0, Temperature=float("nan"))
        fx["devices"]["camera"]["Response"].update(Temperature=0, CameraState="NoState")
        for k in ("Temperature", "Humidity", "DewPoint"):
            fx["devices"]["weather"]["Response"][k] = float("nan")
    b = _take(cfg, T0 + timedelta(hours=9, minutes=39), state="COMPLETE",
              nina1=_nina_collector("nina1", dawn))
    row = _row(b, "nina1_focuser_reset")
    assert row["status"] != "fail", row
    assert not [m for m in pushes if "position counter" in m.lower()]

    def unplugged(fx):
        fx["devices"]["focuser"]["Response"].update(Connected=False, Position=0)
    c = _take(cfg, T0 + timedelta(hours=9, minutes=45), state="COMPLETE",
              nina1=_nina_collector("nina1", unplugged))
    assert _row(c, "nina1_focuser_reset")["status"] != "fail"


def test_focuser_reset_after_reconnect_is_judged_against_last_connected(tmp_path, pushes):
    """A real reset shows after a reconnect: connected 5868, disconnected
    (Position 0, not judged), connected again at 3 -> FAIL against 5868."""
    cfg = _cfg(tmp_path)
    _take(cfg, T0)

    def unplugged(fx):
        fx["devices"]["focuser"]["Response"].update(Connected=False, Position=0)
    off = _take(cfg, T0 + timedelta(hours=1), nina1=_nina_collector("nina1", unplugged))
    assert _row(off, "nina1_focuser_reset")["status"] != "fail"
    assert off["snapshot"]["apps"]["nina1"]["live"]["focuser"]["Connected"] is False

    def back_at_3(fx):
        fx["devices"]["focuser"]["Response"]["Position"] = 3
    z = _take(cfg, T0 + timedelta(hours=2), nina1=_nina_collector("nina1", back_at_3))
    row = _row(z, "nina1_focuser_reset")
    assert row["status"] == "fail" and row["current"] == "3 (was 5868)"


def test_piggyback_off_skips_nina2(tmp_path, pushes):
    cfg = _cfg(tmp_path, piggyback_enabled=False)
    r = _take(cfg)
    assert "nina2" not in r["snapshot"]["apps"]
    assert _row(r, "nina2_camera_gain")["status"] == "na"


# --------------------------------------------------------------------------
# reading back: at a time, for a sub, diffs, last good night
# --------------------------------------------------------------------------

def test_at_local_time_and_refs(tmp_path, pushes):
    cfg = _cfg(tmp_path)
    r1 = _take(cfg)                                   # 21:00 local (MDT)

    def bin2(fx):
        fx["profile"]["PlateSolveSettings"]["Binning"] = 2
    r2 = _take(cfg, T0 + timedelta(hours=5), nina1=_nina_collector("nina1", bin2))  # 02:00
    assert sl.local_to_utc(cfg, NIGHT, "21:30") == datetime(2026, 10, 13, 3, 30)
    assert sl.local_to_utc(cfg, NIGHT, "01:00") == datetime(2026, 10, 13, 7, 0)
    assert sl.at(cfg, sl.local_to_utc(cfg, NIGHT, "23:00"))["snapshot"]["id"] == r1["id"]
    assert sl.at(cfg, sl.local_to_utc(cfg, NIGHT, "02:30"))["snapshot"]["id"] == r2["id"]
    assert sl.at(cfg, datetime(2026, 10, 12, 0, 0)) is None
    assert sl.resolve_ref(cfg, f"{NIGHT}@23:00") == r1["id"]
    assert sl.resolve_ref(cfg, NIGHT) == r2["id"] == sl.resolve_ref(cfg, "latest")
    d = sl.diff(cfg, r1["id"], "latest")
    assert d["ok"] and d["changed"] == 1
    assert d["diff"]["nina1"]["profile.PlateSolveSettings.Binning"] == [1, 2]


def test_last_good_night(tmp_path, pushes):
    cfg = _cfg(tmp_path)
    good = _take(cfg, datetime(2026, 10, 11, 3, 0))           # night 10-10, clean

    def bin2(fx):
        fx["profile"]["PlateSolveSettings"]["Binning"] = 2
    _take(cfg, datetime(2026, 10, 12, 3, 0), nina1=_nina_collector("nina1", bin2))  # 10-11 bad
    _take(cfg, T0)                                             # 10-12 clean again
    assert sl.last_good(cfg) == {"night": "2026-10-10", "id": good["id"]}
    st = sl.status(cfg)
    assert st["ok"] and st["night"] == NIGHT and st["last_good"]["night"] == "2026-10-10"
    assert st["apps"]["nina1"]["last_change_utc"] == "2026-10-13T03:00:00Z"
    assert sl.diff(cfg, "last_good", "latest")["changed"] == 0


def test_sub_records_are_stamped_and_resolved(tmp_path, pushes):
    cfg = _cfg(tmp_path)
    from photonscript.scheduler import runs
    runs.runs_dir(cfg).mkdir(parents=True, exist_ok=True)
    r1 = _take(cfg, datetime(2026, 10, 13, 3, 0))
    recs = [json.loads(x) for x in (FIX / "subs_2026-10-12.jsonl").read_text(
        encoding="utf-8").splitlines()]
    runs.append_sub_record(cfg, NIGHT, dict(recs[0]))        # 03:31Z piggyback sub
    stamped = runs._load_subs(cfg, NIGHT)[0]
    assert stamped["settings_id"] == r1["id"]
    # an older record with no stamp: found by its capture time
    with open(runs.runs_dir(cfg) / f"{NIGHT}_subs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(recs[-1]) + "\n")                  # 11:56Z rc16 sub

    def bin2(fx):
        fx["profile"]["PlateSolveSettings"]["Binning"] = 2
    r2 = _take(cfg, datetime(2026, 10, 13, 11, 0), nina1=_nina_collector("nina1", bin2))
    hit = sl.for_sub(cfg, Path(recs[-1]["file"]).name)
    assert hit["ok"] and hit["id"] == r2["id"] and hit["how"] == "by capture time"
    assert hit["sub"]["gain"] == 200 and hit["sub"]["readout"] == "High Conversion Gain"
    first = sl.for_sub(cfg, recs[0]["file"])
    assert first["id"] == r1["id"] and first["how"] == "stamped on the sub record"
    assert sl.for_sub(cfg, "nope.fits")["ok"] is False


def test_current_id_off_when_disabled(tmp_path, pushes):
    cfg = _cfg(tmp_path)
    _take(cfg)
    assert sl.current_id(cfg)
    assert sl.current_id(_cfg(tmp_path, settings_ledger_enabled=False)) is None


# --------------------------------------------------------------------------
# backfill from the stored audits and sub records
# --------------------------------------------------------------------------

def _seed_history(cfg):
    from photonscript.scheduler import phd2_audit, runs, thesky_audit
    for d in (phd2_audit.audit_dir(cfg), thesky_audit.audit_dir(cfg), runs.runs_dir(cfg)):
        Path(d).mkdir(parents=True, exist_ok=True)
    shutil.copy(FIX / "phd2_audit_2026-10-12.json", phd2_audit.audit_dir(cfg) / f"{NIGHT}.json")
    shutil.copy(FIX / "thesky_audit_2026-10-12.json",
                thesky_audit.audit_dir(cfg) / f"{NIGHT}.json")
    shutil.copy(FIX / "subs_2026-10-12.jsonl", runs.runs_dir(cfg) / f"{NIGHT}_subs.jsonl")


def test_backfill_dry_run_then_apply(tmp_path, pushes):
    cfg = _cfg(tmp_path, phd2_logs_dir=str(tmp_path / "nologs"))
    _seed_history(cfg)
    dry = sl.backfill(cfg, dry_run=True)
    assert dry["nights"][0]["night"] == NIGHT and not sl.night_dir(cfg, NIGHT).exists()
    assert any("NINA" in c for c in dry["cannot"])
    rep = sl.backfill(cfg)
    n = rep["nights"][0]
    assert n["snapshots"] >= 3 and "thesky_time_zone" in n["ever_failed"]
    lines = sl.index(cfg, NIGHT)
    assert all(ln["source"] == "backfill" for ln in lines)
    # the camera settings per rig and the focuser positions came from the subs
    snap = sl.load_snapshot(cfg, n["last_id"])
    assert snap["apps"]["nina1"]["settings"]["devices"]["camera"]["Gain"] == 200
    assert snap["apps"]["nina2"]["settings"]["devices"]["camera"]["ReadoutModeName"] == \
        "Low Conversion Gain"
    assert snap["apps"]["phd2"]["settings"]["silenced_alerts"] == ["PulseGuideFailedAlert"]
    assert any((ln.get("live") or {}).get("nina1", {}).get("focuser") for ln in lines)
    # at a time inside the night
    hit = sl.at(cfg, datetime(2026, 10, 13, 4, 30))
    assert hit and hit["snapshot"]["source"] == "backfill"
    assert pushes == []
    # a second run leaves the night alone; force rewrites a backfilled night
    assert sl.backfill(cfg)["skipped"][0]["night"] == NIGHT
    again = sl.backfill(cfg, force=True)
    assert again["nights"][0]["snapshots"] == n["snapshots"]
    assert len(sl.index(cfg, NIGHT)) == len(lines)


def test_backfill_never_touches_live_nights(tmp_path, pushes):
    cfg = _cfg(tmp_path, phd2_logs_dir=str(tmp_path / "nologs"))
    _seed_history(cfg)
    _take(cfg)
    before = sl.index(cfg, NIGHT)
    rep = sl.backfill(cfg, force=True)
    assert rep["nights"] == [] and "never rewritten" in rep["skipped"][0]["why"]
    assert sl.index(cfg, NIGHT) == before


# --------------------------------------------------------------------------
# armer hooks, the background snapshotter, CLI, routes, System page
# --------------------------------------------------------------------------

def test_armer_requests_on_state_change_only(tmp_path, monkeypatch):
    from photonscript.scheduler.armer import Armer
    cfg = _cfg(tmp_path)
    seen = []
    monkeypatch.setattr(sl, "request", lambda c, reason, state=None: seen.append(reason) or True)
    a = Armer(cfg)
    a._set_state("ARMED", "x")
    a._set_state("ARMED", "y")              # same state, new detail: no snapshot
    a._set_state("RUNNING")
    assert seen == ["state DISARMED -> ARMED", "state ARMED -> RUNNING"]
    a._ledger_hourly(datetime.utcnow())     # just requested: not due
    assert len(seen) == 2
    a._ledger_hourly(datetime.utcnow() + timedelta(minutes=61))
    assert seen[-1] == "hourly"


def test_request_is_off_without_loop_or_when_disabled(tmp_path):
    assert sl.request(_cfg(tmp_path), "x") is False                # no running loop
    assert sl.request(_cfg(tmp_path, settings_ledger_enabled=False), "x") is False
    assert sl.hourly_due(_cfg(tmp_path, settings_ledger_interval_min=0), None) is False


def test_snapshotter_coalesces(tmp_path, monkeypatch):
    calls = []

    async def fake_take(config, reason, armer_state=None):
        calls.append(reason)
        await asyncio.sleep(0.01)
        return {"id": "x", "changed": False}
    monkeypatch.setattr(sl, "take", fake_take)
    snap = sl.Snapshotter()

    async def go():
        snap.request(_cfg(tmp_path), "a")
        await asyncio.sleep(0)              # "a" runs; the next two wait
        snap.request(_cfg(tmp_path), "b")
        snap.request(_cfg(tmp_path), "c")
        snap.request(_cfg(tmp_path), "b")
        await snap._task
    asyncio.run(go())
    assert calls == ["a", "b, c"]


def test_cli_settings(tmp_path, pushes, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    cfg = _cfg(tmp_path)
    r1 = _take(cfg)
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)
    run = CliRunner()
    out = run.invoke(cli.app, ["settings", "--night", NIGHT, "--at", "22:00"])
    assert out.exit_code == 0, out.output
    assert r1["id"] in out.output and "PlateSolveSettings.Binning = 1" in out.output
    st = run.invoke(cli.app, ["settings"])
    assert st.exit_code == 0 and "Settings ledger: night 2026-10-12" in st.output
    d = run.invoke(cli.app, ["settings", "--diff", r1["id"], "latest", "--json"])
    assert d.exit_code == 0 and json.loads(d.output)["changed"] == 0
    none = run.invoke(cli.app, ["settings", "--night", "2026-01-01", "--at", "22:00"])
    assert none.exit_code == 1


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    return TestClient(app.app), cfg


def test_routes(client, pushes):
    c, cfg = client
    assert c.get("/api/settings-ledger/status").json()["ok"] is False
    r1 = _take(cfg)
    st = c.get("/api/settings-ledger/status").json()
    assert st["ok"] and st["id"] == r1["id"] and st["rows"]
    assert c.get("/api/settings-ledger/nights").json()["nights"] == [NIGHT]
    assert len(c.get(f"/api/settings-ledger/night/{NIGHT}").json()["lines"]) == 1
    hit = c.get(f"/api/settings-ledger/at?night={NIGHT}&at=22:00").json()
    assert hit["ok"] and hit["snapshot"]["id"] == r1["id"]
    assert c.get(f"/api/settings-ledger/snapshot/{r1['id']}").json()["id"] == r1["id"]
    assert c.get("/api/settings-ledger/snapshot/latest").json()["id"] == r1["id"]
    assert c.get("/api/settings-ledger/snapshot/zzz").status_code == 404
    assert c.get("/api/settings-ledger/diff?a=latest&b=latest").json()["changed"] == 0
    assert c.get("/api/settings-ledger/at?night=x&at=99").status_code == 400
    assert c.post("/api/settings-ledger/backfill").json()["dry_run"] is True


def test_system_page_and_config_fields(monkeypatch):
    html = (ROOT / "photonscript" / "scheduler" / "templates" / "system.html").read_text(
        encoding="utf-8")
    assert 'id="settingsLedger"' in html and "settings_ledger_panel.js" in html
    js = (ROOT / "photonscript" / "scheduler" / "static" / "js" /
          "settings_ledger_panel.js").read_text(encoding="utf-8")
    assert js.isascii() and "/api/settings-ledger/status" in js
    from photonscript.scheduler.app import _CONFIG_FIELDS
    envs = {f[1] for f in _CONFIG_FIELDS}
    for e in ("PS_SETTINGS_LEDGER_ENABLED", "PS_SETTINGS_LEDGER_INTERVAL_MIN",
              "PS_SETTINGS_LEDGER_PUSH", "PS_SETTINGS_LEDGER_APPS", "PS_SETTINGS_RULES_FILE"):
        assert e in envs
    monkeypatch.delenv("PS_SETTINGS_LEDGER_ENABLED", raising=False)   # conftest pins it off
    c = PhotonScriptConfig(_env_file=None)
    assert c.settings_ledger_enabled is True and c.settings_ledger_interval_min == 60.0


def test_module_is_read_only_toward_programs():
    """No PHD2 setter, no NINA write endpoint, no TheSky write in the ledger."""
    import re
    src = (ROOT / "photonscript" / "scheduler" / "settings_ledger.py").read_text(
        encoding="utf-8")
    assert src.isascii()
    assert not re.search(r"\.(set_\w+|guide_pulse|post|put|delete)\(", src)
    assert not re.search(r"/(connect|disconnect|cool|move|slew|park|start|stop)\b", src)


def test_backfill_from_a_real_guide_log(tmp_path, pushes):
    """The PHD2 guide-log header (fixtures/phd2, a real 2026-10-07 log)
    gives the settings PHD2 guided with that night."""
    logs = tmp_path / "phd2logs"
    logs.mkdir()
    src = Path(__file__).parent / "fixtures" / "phd2" / "PHD2_GuideLog_2026-10-07_201941.txt"
    shutil.copy(src, logs / src.name)
    cfg = _cfg(tmp_path, phd2_logs_dir=str(logs))
    rep = sl.backfill(cfg, ["2026-10-07"])
    n = rep["nights"][0]
    assert n["sources"] == ["PHD2 guide log"] and n["snapshots"] == 1
    snap = sl.load_snapshot(cfg, n["last_id"])
    api = snap["apps"]["phd2"]["settings"]["api"]
    log = snap["apps"]["phd2"]["settings"]["log"]
    assert api["exposure_ms"] == log["exposure_ms"] and api.get("binning")
    assert log.get("ra_algorithm") and snap["night"] == "2026-10-07"
