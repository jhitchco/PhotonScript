"""PS-73: night / file selection for the NINA and PHD2 logs, PHD2 log
discovery, and the guide-log night summary.

2026-09-26: both NINAs restarted at 07:04 local on the 27th, so "newest log"
was the morning's empty log and the night's was unreachable; PHD2's logs were
not under the configured Documents\\PHD2 at all."""
import os
from datetime import datetime
from pathlib import Path

import pytest

from photonscript.scheduler import app
from photonscript.scheduler import log_files as lf
from photonscript.scheduler import phd2_logs as pl
from photonscript.scheduler.routers import triage
from photonscript.shared.config import PhotonScriptConfig


def _touch(p: Path, body: str, mtime: datetime):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    t = mtime.timestamp()
    os.utime(p, (t, t))
    return p


def _nina(dir_, name, port, body, mtime):
    return _touch(dir_ / name, "2026-09-26T16:14:49|INFO|API.cs|Start|134|"
                  f"starting web server, listening at 0.0.0.0:{port}\n{body}", mtime)


def _cfg(tmp_path, **kw):
    kw.setdefault("nina_logs_dir", str(tmp_path / "nina"))
    kw.setdefault("phd2_logs_dir", str(tmp_path / "phd2cfg"))
    return PhotonScriptConfig(_env_file=None,
                              nina_base_url="http://localhost:1888/v2/api",
                              piggyback_nina_base_url="http://localhost:1889/v2/api",
                              **kw)


@pytest.fixture(autouse=True)
def _clear_port_cache():
    triage._PORT_CACHE.clear()


# --- log_files --------------------------------------------------------------

def test_file_start_from_nina_and_phd2_names():
    assert lf.file_start("20260927-070419-3.2.0.9001.26856-202609.log") == \
        datetime(2026, 9, 27, 7, 4, 19)
    assert lf.file_start("PHD2_GuideLog_2026-09-26_193012.txt") == \
        datetime(2026, 9, 26, 19, 30, 12)
    assert lf.file_start("notes.txt") is None


def test_in_night_keeps_the_evening_log_and_the_morning_restart(tmp_path):
    ev = _touch(tmp_path / "20260926-161000-a.log", "x", datetime(2026, 9, 27, 7, 3))
    mo = _touch(tmp_path / "20260927-070419-b.log", "x", datetime(2026, 9, 27, 7, 5))
    old = _touch(tmp_path / "20260925-160000-c.log", "x", datetime(2026, 9, 26, 7, 0))
    assert lf.in_night(ev, "2026-09-26") and lf.in_night(mo, "2026-09-26")
    assert not lf.in_night(old, "2026-09-26")
    assert lf.in_night(old, "2026-09-25")
    with pytest.raises(ValueError):
        lf.in_night(ev, "26 Sep")


def test_safe_name_blocks_paths():
    assert lf.safe_name("a.log") == "a.log"
    for bad in ("../a.log", "..\\a.log", "sub/a.log", "..", ""):
        assert lf.safe_name(bad) is None


# --- NINA --------------------------------------------------------------------

@pytest.fixture
def nina_night(tmp_path, monkeypatch):
    d = tmp_path / "nina"
    _nina(d, "20260926-161000-3.2.0.1111-202609.log", 1888,
          "rc16 evening\nAutofocus finished\nrc16 dawn\n", datetime(2026, 9, 27, 7, 3))
    _nina(d, "20260926-161449-3.2.0.2222-202609.log", 1889,
          "osc evening\n", datetime(2026, 9, 27, 7, 3, 30))
    _nina(d, "20260927-070419-3.2.0.9001.26856-202609.log", 1888,
          "rc16 morning restart\n", datetime(2026, 9, 27, 7, 5))
    _nina(d, "20260927-070421-3.2.0.9002.26857-202609.log", 1889,
          "osc morning restart\n", datetime(2026, 9, 27, 7, 6))
    monkeypatch.setattr(app, "_config", _cfg(tmp_path))
    return d


async def test_nina_date_reads_the_nights_log_after_a_restart(nina_night):
    newest = await app.api_nina_log()
    assert "rc16 morning restart" in newest and "rc16 evening" not in newest
    night = await triage.api_nina_log(date="2026-09-26")
    assert "rc16 evening" in night and "rc16 morning restart" in night
    assert night.index("rc16 evening") < night.index("rc16 morning restart")
    assert "osc" not in night
    pig = await triage.api_nina_log(rig="piggyback", date="2026-09-26",
                                    grep="evening")
    assert "osc evening" in pig and "rc16" not in pig.split("\n", 1)[1]


async def test_nina_file_param_and_traversal(nina_night):
    out = await triage.api_nina_log(file="20260926-161000-3.2.0.1111-202609.log",
                                    grep="autofocus")
    assert "Autofocus finished" in out and "rc16 dawn" not in out
    assert "no NINA log named" in await triage.api_nina_log(file="../secret.log")
    assert "bad date" in await triage.api_nina_log(date="yesterday")
    assert "covers the night" in await triage.api_nina_log(date="2026-08-01")


def test_nina_logs_listing_names_each_rig(nina_night):
    res = triage.api_nina_logs()
    rigs = {r["file"]: r["rig"] for r in res["logs"]}
    assert rigs["20260926-161000-3.2.0.1111-202609.log"] == "rc16"
    assert rigs["20260927-070421-3.2.0.9002.26857-202609.log"] == "piggyback"
    assert res["logs"][0]["file"].startswith("20260927-070421")      # newest first
    only = triage.api_nina_logs(rig="piggyback", date="2026-09-26")
    assert only["count"] == 2 and all(r["rig"] == "piggyback" for r in only["logs"])


# --- PHD2 guide log fixture -----------------------------------------------------

GUIDE_LOG = """PHD2 version 2.6.13, Log version 2.5. Log enabled at 2026-09-26 19:30:12

Calibration Begins at 2026-09-26 20:05:01
Equipment Profile = OAG-RC16
Dither = both axes, Dither scale = 1.000, Image noise reduction = none, Guide-frame time lapse = 0, Server enabled
Pixel scale = 0.13 arc-sec/px, Binning = 1, Focal length = 3248 mm
Camera = OGMA GP678C, gain = 50, full size = 3840 x 2160, have dark, no defect map, pixel size = 2.0 um
Exposure = 2000 ms
Mount = TheSky,  connected, guiding enabled
RA = 20.20 hr, Dec = 66.1 deg, Hour angle = -1.20 hr, Pier side = East, Rotator pos = N/A, Alt = 52.1 deg, Az = 20.3 deg
Lock position = 100.0, 100.0, Star position = 100.1, 100.2, HFD = 3.10 px
Direction,Step,dx,dy,x,y,Dist
West,1,0.000,0.000,0.000,0.000,0.000
West,2,1.100,0.200,1.100,0.200,1.118
West calibration complete. Angle = 12.3 deg, Rate = 3.456 px/sec, Parity = +
North,1,0.000,0.000,0.000,0.000,0.000
North calibration complete. Angle = -77.0 deg, Rate = 1.234 px/sec, Parity = +
Calibration guide speeds: RA = 7.5 a-s/s, Dec = 7.5 a-s/s
Calibration complete, mount = TheSky.
Calibration Ends at 2026-09-26 20:09:40

Calibration Begins at 2026-09-27 01:10:00
Equipment Profile = OAG-RC16
Pixel scale = 0.13 arc-sec/px, Binning = 1, Focal length = 3248 mm
RA = 1.20 hr, Dec = 35.0 deg, Hour angle = 0.10 hr, Pier side = West, Rotator pos = N/A
Direction,Step,dx,dy,x,y,Dist
West,1,0.000,0.000,0.000,0.000,0.000
INFO: Calibration failed: RA calibration failed: star did not move enough
Calibration Ends at 2026-09-27 01:14:00

Guiding Begins at 2026-09-26 20:10:00
Dither = both axes, Dither scale = 1.000, Image noise reduction = none, Guide-frame time lapse = 0, Server enabled
Pixel scale = 0.13 arc-sec/px, Binning = 1, Focal length = 3248 mm
Equipment Profile = OAG-RC16
Exposure = 2000 ms
Mount = TheSky,  connected, guiding enabled, xAngle = 12.3, xRate = 3.456, yAngle = -77.0, yRate = 1.234, parity = +/+
RA Guide Speed = 7.5 a-s/s, Dec Guide Speed = 7.5 a-s/s, Cal Dec = 66.1, Last Cal Issue = None, Timestamp = 9/26/2026 8:09:40 PM
RA = 20.21 hr, Dec = 66.1 deg, Hour angle = -1.10 hr, Pier side = East, Rotator pos = N/A, Alt = 52.3 deg, Az = 20.0 deg
Lock position = 100.0, 100.0, Star position = 100.1, 100.2, HFD = 3.10 px
Frame,Time,mount,dx,dy,RARawDistance,DECRawDistance,RAGuideDistance,DECGuideDistance,RADuration,RADirection,DECDuration,DECDirection,XStep,YStep,StarMass,SNR,ErrorCode
1,2.1,"Mount",0.1,0.1,3.000,4.000,3.000,4.000,120,W,50,N,,,20000,30.1,0
2,4.2,"Mount",0.1,0.1,-3.000,-4.000,-3.000,-4.000,120,E,50,S,,,20000,30.2,0
3,6.3,"DROP",,,,,,,,,,,,,120,2.1,1,"Star lost - low SNR"
4,8.4,"DROP",,,,,,,,,,,,,110,1.9,1,"Star lost - low SNR"
INFO: DITHER by 1.234, -0.567, new lock pos = 101.2, 99.4
INFO: SETTLING STATE CHANGE, Settling started
5,10.5,"Mount",0.1,0.1,30.000,40.000,30.000,40.000,1000,W,500,N,,,20000,30.0,0
INFO: SETTLING STATE CHANGE, Settling complete
6,12.6,"Mount",0.1,0.1,3.000,4.000,3.000,4.000,120,W,50,N,,,20000,30.1,0
7,14.7,"DROP",,,,,,,,,,,,,90,1.0,2,"Star lost - mass changed"
Guiding Ends at 2026-09-26 20:30:00
"""


def test_guide_log_summary_numbers():
    secs = pl.parse_guide_log(GUIDE_LOG, "PHD2_GuideLog_2026-09-26_193012.txt")
    out = pl.summarize_sections(secs, PhotonScriptConfig(_env_file=None))
    t = out["totals"]
    assert t["calibrations"] == 2 and t["calibrations_failed"] == 1
    assert t["frames"] == 4 and t["dropped_frames"] == 3 and t["star_lost"] == 3
    assert t["dithers"] == 1
    c0, c1 = out["calibrations"]
    assert (c0["dec_deg"], c0["pier_side"], c0["result"]) == (66.1, "East", "complete")
    assert c0["hour_angle_hr"] == -1.2
    assert c0["axes"]["West"] == {"angle_deg": 12.3, "rate_px_s": 3.456}
    assert (c1["dec_deg"], c1["pier_side"], c1["result"]) == (35.0, "West", "failed")
    s = out["sessions"][0]
    assert s["scale_source"] == "log" and s["pixel_scale_arcsec"] == 0.13
    assert s["cal_dec"] == "66.1" and s["pier_side"] == "East" and s["dec_deg"] == 66.1
    # RA px: 3, -3, 30, 3 -> rms sqrt((9+9+900+9)/4); Dec px: 4, -4, 40, 4
    rra = ((9 + 9 + 900 + 9) / 4) ** 0.5
    assert s["rms_ra_px"] == pytest.approx(rra, abs=1e-3)
    assert s["rms_ra_arcsec"] == pytest.approx(rra * 0.13, abs=1e-3)
    assert t["rms_dec_arcsec"] == pytest.approx(rra * 4 / 3 * 0.13, abs=1e-3)
    # the settled figures leave out the dither's settling frame
    assert s["settled"]["rms_ra_px"] == pytest.approx(3.0)
    assert s["settled"]["rms_total_arcsec"] == pytest.approx(5 * 0.13)
    assert out["star_lost_reasons"] == {"Star lost - low SNR": 2,
                                        "Star lost - mass changed": 1}


def test_placeholder_scale_uses_config_optics_or_stays_pixels():
    log = GUIDE_LOG.replace("Pixel scale = 0.13", "Pixel scale = 1.00")
    secs = pl.parse_guide_log(log)
    s = pl.summarize_sections(secs, PhotonScriptConfig(_env_file=None))["sessions"][0]
    assert s["scale_source"] == "config"
    assert s["pixel_scale_arcsec"] == pytest.approx(2.0 * 0.24 / 3.76, abs=1e-4)
    s = pl.summarize_sections(
        secs, PhotonScriptConfig(_env_file=None, guide_camera_pixel_um=0))
    assert s["sessions"][0]["units"] == "px"
    assert s["totals"]["rms_total_arcsec"] is None
    assert s["totals"]["frames_without_scale"] == 4


# --- PHD2 discovery + endpoints ----------------------------------------------------

def test_discovery_falls_back_to_documents_then_other_users(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(pl.Path, "home", classmethod(lambda cls: home))
    users = tmp_path / "Users"
    _touch(users / "astro" / "OneDrive - x" / "Documents" / "PHD2" /
           "PHD2_GuideLog_2026-09-26_193012.txt", GUIDE_LOG, datetime(2026, 9, 27, 6))
    monkeypatch.setattr(pl, "_users_roots", lambda: [users])
    cfg = _cfg(tmp_path)
    f = pl.find_logs(cfg)
    assert f["why"] == "another user profile" and len(f["files"]) == 1
    assert f["searched"][0]["why"] == "configured" and not f["searched"][0]["exists"]
    # the user's own Documents\PHD2 wins over other profiles
    _touch(home / "Documents" / "PHD2" / "PHD2_GuideLog_2026-09-25_190000.txt",
           GUIDE_LOG, datetime(2026, 9, 26, 6))
    assert pl.find_logs(cfg)["why"] == "this user's Documents"
    # and the configured folder wins over both once it has logs
    _touch(tmp_path / "phd2cfg" / "PHD2_DebugLog_2026-09-26_193012.txt", "dbg\n",
           datetime(2026, 9, 27, 6))
    assert pl.find_logs(cfg, "debug")["why"] == "configured"
    assert pl.find_logs(cfg, "guide")["why"] == "this user's Documents"


@pytest.fixture
def phd2_dir(tmp_path, monkeypatch):
    d = tmp_path / "phd2cfg"
    _touch(d / "PHD2_GuideLog_2026-09-26_193012.txt", GUIDE_LOG,
           datetime(2026, 9, 27, 6, 30))
    _touch(d / "PHD2_GuideLog_2026-09-27_130000.txt",
           "PHD2 version 2.6.13, Log version 2.5.\nGuiding Begins at 2026-09-27 13:00:00\n",
           datetime(2026, 9, 27, 13, 1))
    monkeypatch.setattr(pl, "_users_roots", list)
    monkeypatch.setattr(pl.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    monkeypatch.setattr(app, "_config", _cfg(tmp_path))
    return d


async def test_phd2_log_date_and_file(phd2_dir):
    newest = await triage.api_phd2_log()
    assert "2026-09-27_130000" in newest
    night = await triage.api_phd2_log(date="2026-09-26", grep="star lost")
    assert "PHD2_GuideLog_2026-09-26_193012.txt" in night and "low SNR" in night
    one = await triage.api_phd2_log(file="PHD2_GuideLog_2026-09-26_193012.txt",
                                    grep="calibration complete")
    assert "West calibration complete" in one
    assert "bad file name" in await triage.api_phd2_log(file="..\\x.txt")


async def test_phd2_log_missing_lists_where_it_looked(tmp_path, monkeypatch):
    monkeypatch.setattr(pl, "_users_roots", list)
    monkeypatch.setattr(pl.Path, "home", classmethod(lambda cls: tmp_path / "h"))
    monkeypatch.setattr(app, "_config", _cfg(tmp_path))
    out = await triage.api_phd2_log()
    assert "no PHD2" in out and str(tmp_path / "phd2cfg") in out and "Documents" in out
    assert triage.api_phd2_summary(date="2026-09-26")["ok"] is False


def test_phd2_logs_and_summary_endpoints(phd2_dir):
    lst = triage.api_phd2_logs()
    assert lst["why"] == "configured" and lst["count"] == 2
    assert lst["logs"][0]["file"] == "PHD2_GuideLog_2026-09-27_130000.txt"
    s = triage.api_phd2_summary(date="2026-09-26")
    assert s["ok"] and s["totals"]["star_lost"] == 3
    assert [c["dec_deg"] for c in s["calibrations"]] == [66.1, 35.0]
    assert s["files"][0]["file"] == "PHD2_GuideLog_2026-09-26_193012.txt"
