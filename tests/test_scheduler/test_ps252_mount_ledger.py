"""PS-252: mount truth ledger.

2026-11-12 00:43:51 to 00:44:12 MST TheSky slewed out of park to M33 Core P3
and reported arriving on it; the 00:49:07 Center solve put the tube 25.6 deg
further north (scope minus solve: RA -1m29s, Dec -25d34m). These tests
replay that shape: the ledger must measure the mapping error at each
Center, flag the jump, freeze an incident and push once; and it must notice
site / time zone / clock changes in TheSky.
"""
import asyncio
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.scheduler import mount_ledger as ml
from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import thesky_client as tc

LON = -109.021367
NOW = datetime(2026, 11, 12, 7, 50, 0)       # 00:50 MST


def _cfg(tmp_path, **kw):
    kw.setdefault("thesky_tcp_host", "127.0.0.1")
    kw.setdefault("thesky_tcp_port", 1)
    kw.setdefault("thesky_user_dir", str(tmp_path / "thesky"))
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"), **kw)


def _hms(h: float) -> str:
    s = round(h * 3600.0)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def _dms(d: float) -> str:
    sg = "-" if d < 0 else ""
    s = round(abs(d) * 3600.0)
    return f"{sg}{s // 3600:02d}° {(s % 3600) // 60:02d}' {s % 60:02d}\""


def center_line(t_local: str, scope_j2000, solved, target) -> str:
    """A NINA 3 Centering Solver line; the scope position is written in JNOW
    like NINA does (precessed from the J2000 point we want)."""
    t_utc = datetime.fromisoformat(t_local) + timedelta(hours=7)
    s_ra, s_dec = ml.precess(scope_j2000[0], scope_j2000[1], 2451545.0,
                             ml.julian_date(t_utc))
    return (f"{t_local}|INFO|CenteringSolver.cs|Center|212|Centering Solver - "
            f"Scope Position: RA: {_hms(s_ra / 15)}; Dec: {_dms(s_dec)}; Epoch: JNOW; "
            f"Offset: RA: 00:00:00; Dec: 00° 00' 00\"; "
            f"Centering Coordinates: RA: {_hms(target[0] / 15)}; Dec: {_dms(target[1])}; "
            f"Epoch: J2000; Solved: RA: {_hms(solved[0] / 15)}; Dec: {_dms(solved[1])}; "
            f"Epoch: J2000; Separation RA: 00:00:01; Dec: 00° 00' 10\"; "
            f"Distance: 00° 00' 30\"; Threshold: 1")


# ------------------------------------------------------------------ astronomy

def test_precession_round_trip_and_size():
    jd_now = ml.julian_date(NOW)
    ra, dec = ml.precess(24.05, 30.68, 2451545.0, jd_now)
    back = ml.precess(ra, dec, jd_now, 2451545.0)
    assert abs(back[0] - 24.05) < 1e-5 and abs(back[1] - 30.68) < 1e-5
    # about 3.07 s of RA a year at RA 0 Dec 0: ~27 years ~ 0.34 deg
    r0, d0 = ml.precess(0.0, 0.0, 2451545.0, jd_now)
    assert 0.30 < r0 < 0.38 and 0.10 < d0 < 0.18


def test_lst_matches_thesky_at_aaro():
    # live TheSky read 2026-11-12: JD 2461357.036740359, LST 9.058420345715282 h
    t = datetime(1970, 1, 1) + timedelta(days=2461357.036740359 - 2440587.5)
    assert abs(ml.lst_hours(t, LON) - 9.058420345715282) * 3600 < 2.0


# ------------------------------------------------------------------ Center mapping

def test_center_mapping_reproduces_the_25_6_degree_offset(tmp_path):
    cfg = _cfg(tmp_path)
    target = (24.05, 30.6847)                    # M33 Core P3
    solved = (24.05, 30.6847)                    # converged: camera on target
    scope = (24.05 - 22.25 / 60, 30.6847 - 25.575)   # TheSky's (wrong) reading
    c = ml.center_mapping(cfg, center_line("2026-11-12T00:49:45.1234", scope, solved, target))
    assert c is not None
    assert abs(c["mapping_north"] - (-1534.5)) < 0.5
    assert abs(c["mapping_east"] - (-22.25 * math.cos(math.radians(17.9)))) < 0.6
    assert c["t"] == "2026-11-12T07:49:45Z"


def test_center_mapping_small_on_a_good_night(tmp_path):
    cfg = _cfg(tmp_path)
    target = (23.6662, 30.776)
    c = ml.center_mapping(cfg, center_line("2026-11-10T22:03:00.5", (23.70, 30.80),
                                           (23.6662, 30.776), target))
    assert c["mapping_arcmin"] < 3.0


def test_nina_mount_line_filter():
    assert ml.nina_mount_line("2026-11-12T05:52:31.0512|ERROR|AscomTelescope.cs|Park|"
                              "88|ASCOM.DriverException: NullReferenceException")
    assert ml.nina_mount_line("2026-11-12T05:52:35.0|INFO|MountVM.cs|Park|1|Mount has parked")
    assert ml.nina_mount_line("2026-11-11T10:46:35.0|INFO|TelescopeVM.cs|Sync|1|"
                              "Syncing telescope location: SiteLatitude 31.9")
    assert not ml.nina_mount_line("2026-11-12T00:49:07.0|INFO|CenteringSolver.cs|Center|1|"
                                  "Centering Solver - Scope Position: RA ...")
    assert not ml.nina_mount_line("2026-11-12T00:50:00.0|INFO|Camera.cs|Capture|1|Exposure done")


# ------------------------------------------------------------------ TheSky diffs

def _truth(**kw):
    base = {"connected": True, "parked": False, "tracking": True, "slew_complete": True,
            "last_slew_error": 0.0, "ra_h": 1.6033, "dec_d": 30.68, "az": 276.8,
            "alt": 60.7, "latitude": 31.9069, "longitude": 109.0211, "time_zone": -7.0,
            "elevation_m": 1300.0, "dst_index": 17.0, "use_computer_clock": True,
            "jd_now": ml.julian_date(NOW), "lst_h": ml.lst_hours(NOW, LON)}
    base.update(kw)
    return base


def test_diff_events_flags_moves_parks_site_and_errors(tmp_path):
    cfg = _cfg(tmp_path)
    a = _truth(parked=True, tracking=False)
    b = _truth(parked=False, tracking=True, slew_complete=False)
    ev = [e["what"] for e in ml.diff_events(a, b, {}, {})]
    assert ev == ["unpark", "tracking_on", "slew_start"]
    c = _truth(time_zone=-6.0, dst_index=3.0, last_slew_error=228.0)
    ev2 = ml.diff_events(_truth(), c, {}, {})
    names = [e["what"] for e in ev2]
    assert "slew_error" in names and "site_change" in names
    site = next(e for e in ev2 if e["what"] == "site_change")
    assert site["changed"]["time_zone"] == [-7.0, -6.0]
    assert ml.diff_events(None, _truth(), {}, {})[0]["what"] == "start"
    assert ml.diff_events(_truth(), None, {}, {})[0]["what"] == "thesky_unreachable"


def test_derive_skews_catch_a_time_zone_or_clock_error(tmp_path):
    cfg = _cfg(tmp_path, observatory_lon=LON)
    ok = ml.derive(cfg, _truth(), NOW)
    assert abs(ok["clock_skew_s"]) < 0.5 and abs(ok["lst_skew_s"]) < 1.0
    hour_off = ml.derive(cfg, _truth(lst_h=(ml.lst_hours(NOW, LON) + 1.0027) % 24,
                                     jd_now=ml.julian_date(NOW) + 1 / 24), NOW)
    assert abs(hour_off["lst_skew_s"] - 3610) < 15 and abs(hour_off["clock_skew_s"] - 3600) < 1
    ev = [e["what"] for e in ml.diff_events(_truth(), _truth(), hour_off, ok)]
    assert "lst_skew" in ev and "clock_skew" in ev


def test_mount_truth_is_read_only_and_in_the_onsite_script():
    js = tc.READ_ONLY_JS["mount_truth"]
    for bad in ("Sync", "Park(", "SlewTo", "Connect(", "SetTracking", "FindHome",
                "SetDocumentProperty", "Jog"):
        assert bad not in js.replace("IsParked()", "")
    assert "'mount_truth.lst_h = '" in tc.onsite_script()


# ------------------------------------------------------------------ tick

def _run(cfg, reads, scans, now0=NOW, step_s=15):
    pushes, state, results = [], {}, []

    async def notify(c, msg, **kw):
        pushes.append((msg, kw))
        return True

    async def go():
        for i, (r, s) in enumerate(zip(reads, scans)):
            now = now0 + timedelta(seconds=step_s * i)
            r = dict(r, jd_now=ml.julian_date(now), lst_h=ml.lst_hours(now, LON))
            res = await ml.tick(cfg, "RUNNING", now=now, read=lambda c, r=r: r,
                                nina_scan=lambda c, n, since, s=s: s,
                                notify=notify, state=state)
            results.append(res)
    asyncio.run(go())
    return pushes, results


def test_tick_flags_the_mapping_jump_once_and_freezes_an_incident(tmp_path):
    cfg = _cfg(tmp_path, observatory_lon=LON)
    target = (24.05, 30.6847)
    good = ml.center_mapping(cfg, center_line("2026-11-12T00:30:00.0",
                                              (24.08, 30.70), target, target))
    bad = ml.center_mapping(cfg, center_line("2026-11-12T00:49:45.0",
                                             (24.05 - 0.37, 30.6847 - 25.575), target, target))
    nina = {"t": "2026-11-12T07:44:30Z", "t_local": "2026-11-12T00:44:30",
            "level": "WARNING", "line": "x|WARNING|Telescope.cs|Slew|1|slow"}
    reads = [_truth(), _truth(slew_complete=False), _truth(), _truth(), _truth()]
    scans = [([good], [], "2026-11-12T00:30:00.0"),        # first scan = backfill
             ([], [], None),
             ([], [nina], "2026-11-12T00:44:30.0"),
             ([bad], [], "2026-11-12T00:49:45.0"),
             ([], [], None)]
    pushes, results = _run(cfg, reads, scans)
    assert results[1]["events"] == ["slew_start"] and results[1]["slewing"]
    assert ml.next_sleep(cfg, results[1]) == 3.0 and ml.next_sleep(cfg, results[0]) == 15.0
    assert results[3]["incidents"] == ["mapping_jump"]
    assert len(pushes) == 1
    msg, kw = pushes[0]
    assert kw["priority"] == 1 and "-1534" in msg
    assert "slew_start" in msg and "slew_end" in msg and "NINA WARNING" in msg
    inc = list((Path(cfg.data_dir) / "mount" / "incidents").glob("2026-11-11_*_mapping_jump.json"))
    assert len(inc) == 1
    rep = json.loads(inc[0].read_text())
    assert any(r.get("kind") == "center" for r in rep["ledger"])
    v = ml.view(cfg, "2026-11-11")
    assert v["mapping"]["mapping_north"] < -1500
    assert [round(s["mapping_arcmin"]) > 1000 for s in v["mapping_series"]] == [False, True]
    kinds = {r["kind"] for r in v["lines"]}
    assert {"sample", "event", "center", "nina"} <= kinds


def test_backfilled_centers_never_alert(tmp_path):
    cfg = _cfg(tmp_path, observatory_lon=LON)
    target = (24.05, 30.6847)
    a = ml.center_mapping(cfg, center_line("2026-11-12T00:30:00.0", (24.05, 30.70), target, target))
    b = ml.center_mapping(cfg, center_line("2026-11-12T00:49:45.0", (24.05, 5.10), target, target))
    pushes, results = _run(cfg, [_truth()], [([a, b], [], "2026-11-12T00:49:45.0")])
    assert pushes == [] and results[0]["incidents"] == []


def test_observe_mode_writes_incidents_but_does_not_push(tmp_path):
    cfg = _cfg(tmp_path, observatory_lon=LON, mount_ledger_mode="observe")
    pushes, results = _run(cfg, [_truth(), _truth(time_zone=-6.0)], [([], [], None)] * 2)
    assert results[1]["incidents"] == ["site_change"] and pushes == []
    assert list((Path(cfg.data_dir) / "mount" / "incidents").glob("*_site_change.json"))


def test_off_mode_does_nothing(tmp_path):
    cfg = _cfg(tmp_path, mount_ledger_mode="off")
    res = asyncio.run(ml.tick(cfg, now=NOW, read=lambda c: _truth()))
    assert res == {"state": "off"}
    assert not (Path(cfg.data_dir) / "mount").exists()


def test_ini_watch_records_versions_and_diffs_keys(tmp_path):
    cfg = _cfg(tmp_path, observatory_lon=LON)
    d = Path(cfg.thesky_user_dir)
    d.mkdir(parents=True)
    ini = d / "ImagingSystem.ini"
    ini.write_text("[Mount]\nParkPositionDec2=-1518044\nParkPositionHA2=-1443541\nParked=1\n")
    st: dict = {}
    first = ml.check_ini(cfg, st, NOW)
    assert first["what"] == "ini_seen"
    assert ml.check_ini(cfg, st, NOW) is None
    ini.write_text("[Mount]\nParkPositionDec2=-1518219\nParkPositionHA2=-1438563\nParked=0\n")
    import os
    os.utime(ini, (1794474499 + 100, 1794474499 + 100))
    ch = ml.check_ini(cfg, st, NOW)
    assert ch["what"] == "ini_change"
    assert ch["changed"]["Mount/Parked"] == ["1", "0"]
    assert ch["changed"]["Mount/ParkPositionDec2"] == ["-1518044", "-1518219"]
    assert len(list((Path(cfg.data_dir) / "mount" / "ini").glob("*_ImagingSystem.ini"))) == 2


def test_scan_nina_reads_new_lines_only(tmp_path):
    cfg = _cfg(tmp_path, observatory_lon=LON)
    target = (24.05, 30.6847)
    log = tmp_path / "nina.log"
    lines = [
        "2026-11-12T00:43:21.0|INFO|MountVM.cs|Unpark|1|Unparking mount",
        center_line("2026-11-12T00:49:07.0", (24.05, 5.1), target, target),
        "2026-11-12T00:50:00.0|INFO|Camera.cs|Capture|1|Exposure done",
        "2026-11-12T05:52:31.0|ERROR|AscomTelescope.cs|Park|88|NullReferenceException",
    ]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    centers, nina, newest = ml.scan_nina(cfg, log, None)
    assert len(centers) == 1 and [n["level"] for n in nina] == ["INFO", "ERROR"]
    assert newest.startswith("2026-11-12T05:52:31")
    c2, n2, new2 = ml.scan_nina(cfg, log, "2026-11-12T00:50:00.0")
    assert c2 == [] and [n["level"] for n in n2] == ["ERROR"]
