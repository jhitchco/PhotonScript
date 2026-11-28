"""The NaN in the 2026-10-06 pointing sidecar: the Piggy-600's last record
(LIGHT/2026-10-07_06-11-35__0.00_300.00s_0086.fits, 12:14Z) took its mount
position from a pre-PS-173 mount-log line written while NINA #1 had lost
the mount: RA 0, Dec 0 and Altitude / Azimuth NaN. sub_pointing kept alt /
az NaN (only None is recomputed) and measured a 2560' "drift" to RA 0 /
Dec 0; GET /api/runs/2026-10-06/pointing now shows alt / az null there.
The producer now cleans a position before using it (pointing.clean_position)
and the dawn pass never reuses such a stored one."""
import json
import math
from datetime import datetime

from photonscript.scheduler import pointing_record as pr
from photonscript.shared import mount_log, pointing
from photonscript.shared.config import PhotonScriptConfig

START = datetime(2026, 10, 7, 12, 11, 35)
NAN = float("nan")


def _cfg(tmp_path):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"))


def _finite_everywhere(rec: dict) -> bool:
    return all(not (isinstance(v, float) and not math.isfinite(v)) for v in rec.values())


def test_offline_base_with_nan_alt_az_is_no_position(tmp_path):
    cfg = _cfg(tmp_path)
    prev = {"rig": "piggyback", "src": "mount-log", "mount_src": "mount-log",
            "mount_ra": 359.5984, "mount_dec": 56.8024, "target": "?"}
    base = {"src": "mount-log", "mount_ra": 0.0, "mount_dec": 0.0, "alt": NAN,
            "az": NAN, "airmass": None, "pier": "East"}
    rec = pointing.sub_pointing(cfg, "piggyback", None, START, 300, "?",
                                prev=prev, base=base)
    assert rec.get("mount_ra") is None and rec.get("src") is None
    assert rec.get("drift_arcmin") is None
    assert _finite_everywhere(rec)


def test_nan_alt_on_a_real_position_is_recomputed(tmp_path):
    cfg = _cfg(tmp_path)
    base = {"src": "mount-log", "mount_ra": 359.5984, "mount_dec": 56.8024,
            "alt": NAN, "az": NAN, "airmass": None, "pier": "West"}
    rec = pointing.sub_pointing(cfg, "piggyback", None, START, 300, "?", base=base)
    assert rec["mount_ra"] == 359.5984
    assert rec["alt"] is not None and math.isfinite(rec["alt"])
    assert rec["az"] is None and _finite_everywhere(rec)


def test_legacy_nan_mount_log_line_gives_no_position(tmp_path):
    """The raw pre-PS-173 line (json.dumps writes NaN) read by the live and
    the dawn path."""
    cfg = _cfg(tmp_path)
    p = mount_log.log_path(cfg, "2026-10-06")
    p.parent.mkdir(parents=True, exist_ok=True)
    good = {"t": "2026-10-07T12:00:00Z", "why": "heartbeat", "ra": 359.5984,
            "dec": 56.8024, "alt": 40.0, "az": 300.0, "pier": "West",
            "slewing": False, "tracking": True, "parked": False}
    bad = {"t": "2026-10-07T12:10:00Z", "why": "move", "ra": 0.0, "dec": 0.0,
           "alt": NAN, "az": NAN, "pier": "East", "slewing": False,
           "tracking": True, "parked": False}
    p.write_text(json.dumps(good) + "\n" + json.dumps(bad) + "\n", encoding="utf-8")
    assert "NaN" in p.read_text(encoding="utf-8")
    lines = mount_log.load(cfg, "2026-10-06")
    rec = pointing.sub_pointing(cfg, "piggyback", None, START, 300, "?",
                                mount_lines=lines)
    assert rec.get("mount_ra") is None and _finite_everywhere(rec)


def test_dawn_pass_never_reuses_a_stored_offline_position():
    stored = {"rig": "piggyback", "src": "mount-log", "mount_src": "mount-log",
              "mount_ra": 0.0, "mount_dec": 0.0, "alt": None, "az": None,
              "pier": "East"}
    assert pr._base_from_stored(stored) is None
    ok = dict(stored, mount_ra=359.5984, mount_dec=56.8024, alt=NAN)
    b = pr._base_from_stored(ok)
    assert b["mount_ra"] == 359.5984 and b["alt"] is None


def test_clean_position_keeps_a_header_position():
    pos = {"src": "header", "mount_ra": 10.68, "mount_dec": 41.27, "alt": 55.0,
           "az": 60.0, "airmass": 1.22, "pier": "West"}
    assert pointing.clean_position(pos) == pos
    assert pointing.clean_position(None) is None
    assert pointing.clean_position(dict(pos, mount_dec=None)) is None
