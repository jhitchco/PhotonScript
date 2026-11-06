"""Preflight check 5, "Sequence generate + lint", independent of the season
and the moon (2026-10-25).

The check built a sequence for a fixed synthetic target (M51's position, RA
13.5 h, Dec +47.2) with one L set. On 2026-10-25 (100% moon up all night)
the moon rule deferred broadband, the generator dropped the L-only target
and the lint failed with "No MeridianFlipTrigger found" and "No
DeepSkyObjectContainer targets found"; the auto-arm then pushed a false
"preflight FAILED". The target is now placed at the local sidereal time of
tonight's mid-dark (Dec +40, so it transits high mid-night) and shoots L and
Ha (narrowband is never moon-deferred).
"""
import json
from datetime import datetime

import pytest

from photonscript.scheduler import moon
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import preflight
from photonscript.shared.config import PhotonScriptConfig

DATES = [datetime(2026, m, d, 22, 0) for m in range(1, 13) for d in (1, 15)]


def _cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"))


@pytest.fixture(autouse=True)
def _clear():
    moon._cache.clear()
    yield
    moon._cache.clear()


def _moon_on(monkeypatch, cfg, when):
    mw = moon.moon_window_tonight(cfg, now=when)
    monkeypatch.setattr(nsj, "_moon_window", lambda: mw)
    return mw


def _alt_deg(cfg, ra_h, dec_d, when):
    from astropy import units as u
    from astropy.coordinates import AltAz, SkyCoord
    from astropy.time import Time
    from photonscript.shared.astronomy import get_earth_location
    loc = get_earth_location(cfg.get_observatory())
    c = SkyCoord(ra=ra_h * 15 * u.deg, dec=dec_d * u.deg)
    return float(c.transform_to(AltAz(obstime=Time(when), location=loc)).alt.deg)


def test_2026_10_25_full_moon_passes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    now = datetime(2026, 10, 25, 22, 43)
    mw = _moon_on(monkeypatch, cfg, now)
    assert mw["illum_pct"] >= 95 and moon.broadband_deferred(mw)
    chk = preflight.sequence_check(cfg, now)
    assert chk["status"] == "pass", chk["detail"]


def test_the_old_fixed_l_only_target_fails_under_that_moon(tmp_path, monkeypatch):
    """The cause, kept as a record: an L-only target is emptied by the moon."""
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    from photonscript.scheduler.sequence_lint import lint
    from photonscript.shared.models import (ExposurePlan, FilterType,
                                            NinaSequenceTarget)
    cfg = _cfg(tmp_path)
    _moon_on(monkeypatch, cfg, datetime(2026, 10, 25, 22, 43))
    t = NinaSequenceTarget(name="PreflightTest", ra_hours=13.5, dec_degrees=47.2,
                           exposures=[ExposurePlan(filter_type=FilterType.LUMINANCE,
                                                   exposure_seconds=180, count=5)])
    res = lint(json.loads(nsj.generate_nina_json(
        build_sequence_for_night("PreflightTest", [t]))))
    errs = " ".join(f.detail for f in res.findings if f.level == "ERROR")
    assert not res.ok and "No DeepSkyObjectContainer targets found" in errs


@pytest.mark.parametrize("when", DATES, ids=[f"{d:%m-%d}" for d in DATES])
def test_check_passes_across_the_year(tmp_path, monkeypatch, when):
    cfg = _cfg(tmp_path)
    _moon_on(monkeypatch, cfg, when)
    chk = preflight.sequence_check(cfg, when)
    assert chk["status"] == "pass", (when, chk["detail"])


@pytest.mark.parametrize("when", DATES[::3], ids=[f"{d:%m-%d}" for d in DATES[::3]])
def test_target_is_high_at_mid_dark(tmp_path, when):
    from photonscript.shared.astronomy import get_twilight_times
    from photonscript.shared.localtime import night_base_date
    cfg = _cfg(tmp_path)
    ra, dec = preflight.preflight_target_radec(cfg, when)
    assert 0 <= ra < 24 and dec == preflight.PREFLIGHT_DEC_DEG
    tw = get_twilight_times(cfg.get_observatory(), night_base_date(cfg, when))
    d0, d1 = tw["astro_dark_start"], tw["astro_dark_end"]
    mid = d0 + (d1 - d0) / 2
    assert _alt_deg(cfg, ra, dec, mid) > 75          # near transit
    from datetime import timedelta
    for dh in (-3, 3):                               # up most of the night
        assert _alt_deg(cfg, ra, dec, mid + timedelta(hours=dh)) > 40


def test_unknown_moon_geometry_still_passes(tmp_path, monkeypatch):
    """No moon window (broadband deferred to be safe): the Ha set remains."""
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(nsj, "_moon_window", lambda: {"available": False})
    assert preflight.sequence_check(cfg, datetime(2026, 10, 25, 22))["status"] == "pass"
