"""Pointing sanity at integrate time (integration/pointing_check.py): the
"NGC 2408" frames that were really M27 (header RA / DEC far from the
catalog), lights shot low, and a mount that stopped tracking."""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.integration import frames as fr
from photonscript.integration import pipeline as pl
from photonscript.integration import pointing_check as pc
from tests.test_integration.test_pipeline import _fits, _opts, lib  # noqa: F401

M27 = (299.9016, 22.72103)
N2408 = (115.125, 71.6667)
SITE = (31.907, -109.021)


def _frame(i, ra, dec, t, alt=None, az=None, exp=120.0, night="2026-10-12"):
    h = {"IMAGETYP": "LIGHT", "EXPTIME": exp, "RA": ra, "DEC": dec,
         "DATE-OBS": t.strftime("%Y-%m-%dT%H:%M:%S")}
    if alt is not None:
        h["CENTALT"] = alt
    if az is not None:
        h["CENTAZ"] = az
    f = fr.from_header(Path(f"L_{i:03d}.fits"), h)
    f.night = night
    return f


def test_catalog_has_the_rc16_targets_that_were_missing():
    for name, (ra, dec) in (("NGC 6535", (270.96, -0.2976)), ("NGC 2408", N2408)):
        got = pl.catalog_coords(name)
        assert got[0] == pytest.approx(ra, abs=0.01) and got[1] == pytest.approx(dec, abs=0.01)


def test_frames_read_the_altitude_headers():
    f = fr.from_header(Path("a.fits"), {"CENTALT": 45.5, "CENTAZ": 120.0})
    assert (f.alt_deg, f.az_deg) == (45.5, 120.0)
    g = fr.from_header(Path("b.fits"), {"OBJCTALT": "30.25", "OBJCTAZ": 99})
    assert (g.alt_deg, g.az_deg) == (30.25, 99.0)
    assert fr.from_header(Path("c.fits"), {}).alt_deg is None


def test_frames_far_from_the_catalog_position_are_off_target():
    t = datetime(2026, 10, 13, 3, 0)
    lights = [_frame(i, *M27, t + timedelta(minutes=3 * i), alt=60 - i) for i in range(3)]
    lights.append(_frame(9, N2408[0] + 0.2, N2408[1] - 0.3, t, alt=50))
    rep = pc.check(lights, *N2408, lat=SITE[0], lon=SITE[1])
    assert rep.counts() == {"off_target": 3} and rep.checked == 4
    assert "L_009.fits" not in rep.flagged
    assert "deg from the target" in rep.flagged["L_000.fits"][0]
    assert rep.lines() == ["3 light(s) more than 1 deg from the target's catalog position: "
                           "stacked anyway (integrate_pointing_check warn)"]
    assert pc.check(lights, *N2408, max_deg=120).counts() == {}


def test_low_altitude_from_the_header_or_computed():
    t = datetime(2026, 10, 13, 3, 0)
    low = _frame(0, *M27, t, alt=15.0)
    high = _frame(1, *M27, t + timedelta(minutes=3), alt=40.0)
    rep = pc.check([low, high], *M27)
    assert list(rep.flagged) == ["L_000.fits"] and "low_altitude (15.0 deg)" in rep.flagged["L_000.fits"][0]
    # no CENTALT: computed from RA / DEC, the site and DATE-OBS (M27 is below
    # the horizon from AARO at 17:00 UTC = 10:00 MST)
    calc = _frame(2, *M27, datetime(2026, 10, 13, 17, 0))
    alt = pc.altitude(calc, *SITE)
    assert alt is not None and alt < 0
    assert "L_002.fits" in pc.check([calc], *M27, lat=SITE[0], lon=SITE[1]).flagged
    assert pc.check([calc], *M27).flagged == {}      # no site: nothing computed
    assert pc.check([low], *M27, min_alt_deg=0).flagged == {}


def test_a_mount_that_stopped_tracking_is_caught():
    t = datetime(2026, 10, 13, 2, 0)
    # tracking: alt changes, RA stays
    good = [_frame(i, M27[0], M27[1], t + timedelta(minutes=5 * i), alt=60.0 - 0.8 * i, az=200 + i)
            for i in range(3)]
    # stopped: alt / az pinned, RA runs 15.04 deg / h (1.25 deg per 5 min)
    stop = [_frame(10 + i, M27[0] + 1.2534 * i, M27[1], t + timedelta(minutes=30 + 5 * i),
                   alt=55.3, az=230.1) for i in range(3)]
    rep = pc.check(good + stop, *M27, max_deg=10)
    assert sorted(rep.flagged) == ["L_010.fits", "L_011.fits", "L_012.fits"]
    assert all(v[0].startswith("mount_stopped") for v in rep.flagged.values())
    # a mount near transit (alt flat) that tracks: RA does not move, not flagged
    transit = [_frame(20 + i, M27[0], M27[1], t + timedelta(minutes=5 * i), alt=80.0)
               for i in range(3)]
    assert pc.check(transit, *M27).flagged == {}
    # different nights are never paired
    a = _frame(30, M27[0], M27[1], t, alt=50.0, night="2026-10-11")
    b = _frame(31, M27[0] + 1.2534, M27[1], t + timedelta(minutes=5), alt=50.0, night="2026-10-12")
    assert pc.stopped_pairs([a, b]) == []


def test_no_position_and_unknown_target_are_noted():
    rep = pc.check([_frame(0, None, None, datetime(2026, 10, 13, 3))], None, None)
    assert rep.no_position == 1 and rep.flagged == {}
    assert any("not in the catalog" in n for n in rep.notes)
    assert any("Piggy-600" in n for n in rep.notes)
    assert pc.check([], 1.0, 2.0, mode="off").summary()["mode"] == "off"
    with pytest.raises(ValueError):
        pc.check([], 1.0, 2.0, mode="loud")


def test_config_settings_defaults_and_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    from photonscript.shared.config import PhotonScriptConfig
    c = PhotonScriptConfig(_env_file=None)
    assert pc.config_settings(c) == {"pointing_check": "warn", "pointing_max_deg": 1.0,
                                     "pointing_min_alt_deg": 20.0}
    f = {x[0]: x for x in _CONFIG_FIELDS}
    assert f["integrate_pointing_check"][1] == "PS_INTEGRATE_POINTING_CHECK"
    assert f["integrate_pointing_max_deg"][4] == "float" and f["integrate_min_alt_deg"][4] == "float"
    assert pl.config_options(c, "rc16")["pointing_check"] == "warn"
    with pytest.raises(ValueError):
        pc.config_settings(PhotonScriptConfig(_env_file=None, integrate_pointing_check="nope"))


# --- the integrate run -------------------------------------------------------------

@pytest.fixture
def ngc2408_lib(lib):  # noqa: F811
    """lib plus an "NGC 2408" folder whose 120 s lights point at M27 (two)
    and at NGC 2408 (one)."""
    from astropy.io import fits
    d = lib / "NGC 2408" / "OSC"
    for i, (ra, dec) in enumerate([M27, M27, N2408]):
        p = d / f"2026-09-21_04-0{i}-00__0.00_120.00s_000{i}.fits"
        _fits(p, "LIGHT", 120.0, f"2026-09-21T04:0{i}:00", f"2026-09-21T10:0{i}:00")
        with fits.open(p, mode="update") as h:
            h[0].header["RA"], h[0].header["DEC"], h[0].header["CENTALT"] = ra, dec, 50.0
    return lib


def test_integrate_warns_and_stacks_by_default(ngc2408_lib, tmp_path):
    said = []
    res = pl.run(_opts(ngc2408_lib, tmp_path, target="NGC 2408", dry_run=True), echo=said.append)
    p = res["pointing"]
    assert p["mode"] == "warn" and p["counts"] == {"off_target": 2} and p["checked"] == 3
    assert res["lights_selected"] == 3
    assert res["qa"]["pointing"]["counts"] == {"off_target": 2}
    assert any(s.startswith("  WARNING: pointing: 2 light(s) more than 1 deg") for s in said)


def test_integrate_refuse_leaves_them_out(ngc2408_lib, tmp_path):
    res = pl.run(_opts(ngc2408_lib, tmp_path, target="NGC 2408", dry_run=True,
                       pointing_check="refuse"), echo=lambda s: None)
    assert res["lights_selected"] == 1 and res["pointing"]["counts"] == {"off_target": 2}


def test_integrate_refuse_with_nothing_left_stops(ngc2408_lib, tmp_path):
    with pytest.raises(pl.PipelineError, match="failed the pointing check"):
        pl.run(_opts(ngc2408_lib, tmp_path, target="NGC 2408", dry_run=True, pointing_check="refuse",
                     pointing_min_alt_deg=60.0), echo=lambda s: None)


def test_integrate_packet_lists_the_pointing_issue(ngc2408_lib, tmp_path):
    out = tmp_path / "Staging" / "n2408"
    pl.run(_opts(ngc2408_lib, tmp_path, target="NGC 2408", out=out), echo=lambda s: None)
    packet = next((out / "astrobin").glob("*_packet.md")).read_text("ascii")
    assert "Pointing: 2 light(s) more than 1 deg from the target's catalog position" in packet


def test_frames_without_position_are_unchanged(lib, tmp_path):  # noqa: F811
    res = pl.run(_opts(lib, tmp_path, dry_run=True), echo=lambda s: None)
    assert res["pointing"]["flagged"] == {} and res["pointing"]["no_position"] == res["lights_selected"]
