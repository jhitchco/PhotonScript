"""PS-145: the PS-67 pointing record compares positions in one epoch.

ninaAPI (the mount log) reports the Paramount in JNow; targets, plate solves
and NINA's FITS RA / DEC are J2000. In 2026 that is about 19' at M31, which
the record used to add to every mount-log offset and drift."""
from datetime import datetime, timedelta

import pytest

from photonscript.scheduler.off_target import precess_from_j2000, precess_to_j2000
from photonscript.shared import mount_log, pointing
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.target_names import target_key

NIGHT = "2026-10-07"
M31 = ("Andromeda Galaxy", 10.6847, 41.2690)
T0 = datetime(2026, 10, 8, 3, 0, 0)


def _cfg(tmp_path, **kw):
    base = dict(_env_file=None, data_dir=str(tmp_path / "data"),
                image_watch_dir=str(tmp_path / "fits"),
                library_dir=str(tmp_path / "lib"),
                stamp_fits_object=False, piggyback_enabled=True,
                observatory_tz="UTC")
    base.update(kw)
    (tmp_path / "data").mkdir(exist_ok=True)
    return PhotonScriptConfig(**base)


@pytest.fixture(autouse=True)
def _targets(monkeypatch):
    idx = {target_key(M31[0]): (*M31, False)}
    monkeypatch.setattr(pointing, "coord_index", lambda cfg: idx)


def _lines_at(cfg, ra_deg, dec_deg, t=T0):
    ml = mount_log.MountLogger(cfg)
    ml.observe({"RightAscension": ra_deg / 15.0, "Declination": dec_deg,
                "Altitude": 60.0, "Azimuth": 90.0, "SideOfPier": "pierWest",
                "Slewing": False, "TrackingEnabled": True, "AtPark": False}, t)
    return mount_log.load(cfg, NIGHT)


def test_inverse_precession_round_trips_and_matches_astropy():
    from astropy.coordinates import FK5, SkyCoord
    import astropy.units as u
    for ra, dec in ((10.6847, 41.269), (294.0, 12.0), (359.9, 89.5), (0.05, -60.0)):
        back = precess_to_j2000(*precess_from_j2000(ra, dec, T0), T0)
        assert back[0] == pytest.approx(ra, abs=1e-7) and back[1] == pytest.approx(dec, abs=1e-7)
    jnow = precess_from_j2000(M31[1], M31[2], T0)
    ref = SkyCoord(M31[1] * u.deg, M31[2] * u.deg, frame=FK5(equinox="J2000")) \
        .transform_to(FK5(equinox=f"J{2000 + (T0 - datetime(2000, 1, 1, 12)).days / 365.25:.4f}"))
    assert pointing.sep_arcmin(jnow[0], jnow[1], ref.ra.deg, ref.dec.deg) < 0.05
    assert pointing.sep_arcmin(M31[1], M31[2], *jnow) > 15      # ~19' at M31 in 2026


def test_mount_log_jnow_position_on_the_target_is_on_target(tmp_path):
    cfg = _cfg(tmp_path)
    lines = _lines_at(cfg, *precess_from_j2000(M31[1], M31[2], T0))
    rec = pointing.sub_pointing(cfg, "piggyback", {}, T0 + timedelta(seconds=10),
                                120, M31[0], mount_lines=lines)
    assert rec["src"] == "mount-log" and rec["mount_epoch"] == "JNow"
    assert rec["off_target_arcmin"] < 0.1 and rec["flag"] == ""
    assert rec["target_ra"] == pytest.approx(M31[1])           # target stays J2000


def test_forcing_j2000_restores_the_old_raw_comparison(tmp_path):
    cfg = _cfg(tmp_path, off_target_mount_epoch="j2000")
    lines = _lines_at(cfg, *precess_from_j2000(M31[1], M31[2], T0))
    rec = pointing.sub_pointing(cfg, "piggyback", {}, T0 + timedelta(seconds=10),
                                120, M31[0], mount_lines=lines)
    assert rec["mount_epoch"] == "J2000"
    assert rec["off_target_arcmin"] > 15


def test_header_position_is_j2000_and_not_precessed(tmp_path):
    cfg = _cfg(tmp_path)
    hdr = {"RA": M31[1], "DEC": M31[2], "PIERSIDE": "West"}
    rec = pointing.sub_pointing(cfg, "rc16", hdr, T0, 300, M31[0])
    assert rec["mount_src"] == "header" and rec["mount_epoch"] == "J2000"
    assert rec["off_target_arcmin"] < 0.05


def test_drift_between_a_solve_and_a_mount_log_sub_has_no_epoch_jump(tmp_path):
    """The 2026-10-07 Piggy-600 log showed ~14.5' drift between alternating
    solved (J2000) and mount-log (JNow) subs that never moved."""
    cfg = _cfg(tmp_path)
    lines = _lines_at(cfg, *precess_from_j2000(M31[1], M31[2], T0))
    first = pointing.sub_pointing(
        cfg, "piggyback", {}, T0 + timedelta(seconds=10), 120, M31[0],
        mount_lines=lines, solve={"solved": True, "ra": M31[1], "dec": M31[2]})
    second = pointing.sub_pointing(
        cfg, "piggyback", {}, T0 + timedelta(seconds=200), 120, M31[0],
        mount_lines=lines, prev=first)
    assert first["src"] == "solve" and second["src"] == "mount-log"
    assert second["drift_arcmin"] < 0.1


def test_model_error_compares_a_jnow_mount_with_the_j2000_solve(tmp_path):
    cfg = _cfg(tmp_path)
    jra, jdec = precess_from_j2000(M31[1], M31[2], T0)
    lines = _lines_at(cfg, jra, jdec)
    rec = pointing.sub_pointing(
        cfg, "rc16", None, T0 + timedelta(seconds=10), 120, M31[0],
        mount_lines=lines, solve={"solved": True, "ra": M31[1], "dec": M31[2] - 2 / 60.0})
    assert rec["mount_src"] == "mount-log"
    assert rec["model_err_arcmin"] == pytest.approx(2.0, abs=0.05)


def test_old_records_without_mount_epoch_are_read_as_stored():
    rec = {"mount_ra": 10.0, "mount_dec": 41.0, "t": "2026-10-08T03:00:00Z",
           "src": "mount-log"}
    assert pointing.judged_position(rec) == (10.0, 41.0, "mount")
    rec["mount_epoch"] = "JNow"
    ra, dec, _ = pointing.judged_position(rec)
    assert pointing.sep_arcmin(10.0, 41.0, ra, dec) > 15


def test_dawn_pass_reuses_the_stored_epoch():
    from photonscript.scheduler.pointing_record import _base_from_stored
    b = _base_from_stored({"mount_ra": 1.0, "mount_dec": 2.0, "src": "mount-log",
                           "mount_src": "mount-log", "mount_epoch": "JNow"})
    assert b["mount_epoch"] == "JNow"
