"""Mount log: a disconnected or zero poll is an offline marker, never a
position. 2026-10-09 03:08Z NINA #1 lost the mount and the logger stored
RA 0 / Dec 0 with no alt / az; the timeline read it as idle, Piggy subs got
mount 0/0 pointing records and the slew gate took it as an "unpark". The
logger now writes one {"offline": true} line per outage and every reader
skips it (is_offline also knows the legacy 0 / 0 lines)."""
from datetime import datetime, timedelta

from photonscript.scheduler import dawn_autofile as da
from photonscript.scheduler import slew_gate as sg
from photonscript.shared import mount_log, pointing
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.mount_motion import MotionTracker

NIGHT = "2026-09-26"
T0 = datetime(2026, 9, 27, 4, 0, 0)          # 04:00Z = the night of 09-26


def _cfg(tmp_path, **kw):
    base = dict(_env_file=None, data_dir=str(tmp_path / "data"),
                image_watch_dir=str(tmp_path / "fits"),
                library_dir=str(tmp_path / "lib"),
                stamp_fits_object=False, piggyback_enabled=True,
                observatory_tz="UTC", library_attribute=False)
    base.update(kw)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return PhotonScriptConfig(**base)


def _t(sec):
    return T0 + timedelta(seconds=sec)


def _mount(ra_h=0.712, dec=41.27, slewing=False, tracking=True, park=False,
           pier="pierWest", connected=True):
    return {"Connected": connected, "RightAscension": ra_h, "Declination": dec,
            "Altitude": 60.0, "Azimuth": 90.0, "SideOfPier": pier,
            "Slewing": slewing, "TrackingEnabled": tracking, "AtPark": park}


# the 2026-10-09 03:08Z poll: connected per NINA, zeros, no alt / az
ZERO = {"Connected": True, "RightAscension": 0.0, "Declination": 0.0,
        "Altitude": None, "Azimuth": None, "SideOfPier": None,
        "Slewing": False, "TrackingEnabled": False, "AtPark": False}
GONE = {"Connected": False, "RightAscension": 0.0, "Declination": 0.0}


def _log(cfg, events):
    ml = mount_log.MountLogger(cfg)
    for sec, m in events:
        ml.observe(m, _t(sec))
    return mount_log.load(cfg, NIGHT)


def _outage(cfg):
    """Tracking 0 to 600 s, offline 600 to 1200 s (polls every 5 s),
    back tracking at 1200 s."""
    ev = [(s, _mount()) for s in range(0, 600, 60)]
    ev += [(s, ZERO if s < 900 else GONE) for s in range(600, 1200, 5)]
    ev += [(s, _mount()) for s in range(1200, 1800, 60)]
    return _log(cfg, ev)


# ------------------------------------------------------------- the logger

def test_offline_polls_are_detected():
    assert mount_log.poll_offline(ZERO) and mount_log.poll_offline(GONE)
    assert mount_log.poll_offline({"Connected": False})
    assert not mount_log.poll_offline(_mount())
    # a real pointing at RA 0 / Dec 0 still has alt / az
    assert not mount_log.poll_offline(_mount(ra_h=0.0, dec=0.0))
    assert mount_log.sample_from_nina(ZERO) is None
    assert mount_log.sample_from_nina(GONE) is None
    assert mount_log.sample_from_nina(_mount(ra_h=0.0, dec=0.0))["dec"] == 0.0


def test_logger_writes_one_marker_per_outage_and_a_reconnect(tmp_path):
    lines = _outage(_cfg(tmp_path))
    whys = [r["why"] for r in lines]
    assert whys.count("offline") == 1 and "reconnect" in whys
    off = next(r for r in lines if r["why"] == "offline")
    assert off["offline"] is True and off["dt"] == _t(600)
    assert off["ra"] is None and off["dec"] is None and off["parked"] is None
    assert not any(r.get("ra") == 0.0 for r in lines)   # never a 0/0 position
    rc = next(r for r in lines if r["why"] == "reconnect")
    assert rc["dt"] == _t(1200) and rc["ra"] is not None
    assert mount_log.is_offline(off) and not mount_log.is_offline(rc)
    assert da.mount_offline(off)        # the dawn filing reads the marker


def test_new_night_while_offline_gets_its_own_marker(tmp_path):
    cfg = _cfg(tmp_path)
    ml = mount_log.MountLogger(cfg)
    noon = datetime(2026, 9, 27, 12, 0, 0)   # UTC tz: night rolls at noon
    assert ml.observe(GONE, noon - timedelta(seconds=10))["why"] == "offline"
    assert ml.observe(GONE, noon - timedelta(seconds=5)) is None
    assert ml.observe(GONE, noon + timedelta(seconds=5))["why"] == "offline"


def test_legacy_zero_line_is_offline():
    assert mount_log.is_offline({"ra": 0.0, "dec": 0.0, "alt": None,
                                 "az": None, "why": "unpark"})
    assert not mount_log.is_offline({"ra": 0.0, "dec": 0.0, "alt": 5.0,
                                     "az": 1.0})
    assert not mount_log.is_offline({"ra": None, "dec": None})


# ------------------------------------------------------------- readers

def test_segments_and_position_skip_the_outage(tmp_path):
    lines = _outage(_cfg(tmp_path))
    states = [s["state"] for s in mount_log.segments(lines)]
    assert states == ["tracking", "offline", "tracking"]
    assert mount_log.position_at(lines, _t(300))["dec"] == 41.27
    assert mount_log.position_at(lines, _t(900)) is None
    assert mount_log.position_at(lines, _t(1300))["dec"] == 41.27


def test_pointing_record_never_takes_an_offline_sample(tmp_path):
    cfg = _cfg(tmp_path)
    lines = _outage(cfg)
    rec = pointing.sub_pointing(cfg, "piggyback", {}, _t(700), 120, None,
                                mount_lines=lines)
    assert rec["mount_src"] is None and rec.get("mount_ra") is None
    ok = pointing.sub_pointing(cfg, "piggyback", {}, _t(1300), 120, None,
                               mount_lines=lines)
    assert ok["mount_src"] == "mount-log" and ok["mount_dec"] == 41.27
    # the legacy 0 / 0 line too: no "mount 0/0" record
    legacy = [{"dt": _t(0), "ra": 10.68, "dec": 41.27, "alt": 60.0,
               "az": 90.0, "tracking": True, "why": "start"},
              {"dt": _t(600), "ra": 0.0, "dec": 0.0, "alt": None, "az": None,
               "tracking": False, "parked": False, "why": "unpark"}]
    old = pointing.sub_pointing(cfg, "piggyback", {}, _t(700), 120, None,
                                mount_lines=legacy)
    assert old.get("mount_ra") is None


def test_slew_gate_skips_offline_lines(tmp_path):
    cfg = _cfg(tmp_path)
    lines = _outage(cfg)
    # no window from the marker or across the outage (same pointing after)
    assert sg.windows_from_mount_log(lines, pad_s=10) == []
    # the log does not cover a sub during the outage, or one spanning it
    assert not sg.mount_log_covers(lines, _t(700), _t(820))
    assert not sg.mount_log_covers(lines, _t(500), _t(620))
    assert sg.mount_log_covers(lines, _t(1300), _t(1420))
    # NightWindows falls back to the RC16 frames for the outage
    rc16 = [{"start": _t(0), "end": _t(300), "ra": 10.68, "dec": 41.27},
            {"start": _t(1300), "end": _t(1600), "ra": 30.0, "dec": 41.27}]
    nw = sg.NightWindows(cfg, lines=lines, rc16_frames=rc16)
    assert nw.assess(_t(700), _t(820))["src"] == "rc16-frames"
    assert nw.assess(_t(1400), _t(1520))["src"] == "mount-log"


def test_slew_gate_legacy_zero_unpark_is_not_an_event():
    legacy = [{"dt": _t(0), "ra": 10.68, "dec": 41.27, "alt": 60.0,
               "az": 90.0, "tracking": False, "parked": True, "why": "park"},
              {"dt": _t(600), "ra": 0.0, "dec": 0.0, "alt": None, "az": None,
               "tracking": False, "parked": False, "why": "unpark"}]
    w = sg.windows_from_mount_log(legacy, pad_s=10)
    assert w == [(_t(-10), _t(10))]          # the real park only
    assert not sg.mount_log_covers(legacy, _t(700), _t(820))


def test_armer_parked_reads_the_last_real_line(tmp_path):
    from photonscript.shared.phd2_store import append_jsonl, night_of
    from tests.test_scheduler.test_ps159_shutdown_state import _armer
    a = _armer(tmp_path)
    now = datetime.utcnow().replace(microsecond=0)
    p = mount_log.log_path(a.config, night_of(a.config, now))
    append_jsonl(p, {"t": (now - timedelta(seconds=60)).isoformat() + "Z",
                     "ra": 52.9, "dec": 89.5, "alt": 31.8, "az": 0.5,
                     "parked": True, "why": "park"})
    append_jsonl(p, {"t": now.isoformat() + "Z", "offline": True, "ra": None,
                     "dec": None, "parked": None, "why": "offline"})
    assert a._mount_log_parked() is True


def test_split_guard_tracker_ignores_offline_polls():
    tr = MotionTracker()
    assert tr.observe(mount_log.sample_from_nina(_mount()), 0.0) is None
    # the zero poll is no sample, so it is never a jump to RA 0 / Dec 0
    assert tr.observe(mount_log.sample_from_nina(ZERO), 5.0) is None
    assert tr.observe(mount_log.sample_from_nina(_mount()), 10.0) is None
