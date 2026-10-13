"""2026-10-12: "Skipping Andromeda Galaxy tonight: broadband-only and the
moon defers broadband" from 18:02 local on, on a BB night with a ~7% moon
(the 17:57 plan still had Andromeda 02:05-04:23Z then the Heart).

Two bugs, both replayed here:
  1. moon_window_tonight() anchored to the UTC date, so once UTC rolled
     over (18:00 MDT) it described TOMORROW night (dusk 10-14 02:05Z);
  2. night_moon() cached by a date label that callers spell differently
     (the forecast by local date, moon_window_tonight by the UTC date of
     dusk), so "2026-10-14" served the 10-14/15 night's 20% moon to it,
     and a moon up at dusk at 20% defers broadband.
Also pinned: the noon-to-noon night anchor (after local midnight the
planner planned the next night: "Dark window: 2026-10-14 ..." at 00:04
local), the bright-moon rule unchanged, and the log reason text.
"""
import logging
from datetime import datetime

import pytest

from photonscript.scheduler import moon
from photonscript.scheduler.moon import (broadband_deferred, describe_moon,
                                         moon_window_tonight, night_moon)
from photonscript.scheduler.target_planner import plan_night_sequence
from photonscript.shared.astronomy import get_twilight_times
from photonscript.shared.localtime import night_base_date
from photonscript.shared.models import (CelestialTarget, ExposurePlan,
                                        FilterType, ImagingProject)
from tests.test_scheduler.test_ps180_target_windows import _cfg

F = FilterType
AT_2224 = datetime(2026, 10, 13, 4, 24)    # 22:24 MDT 10-12, the log line
AT_1757 = datetime(2026, 10, 12, 23, 57)   # 17:57 MDT, last good plan
AT_0004 = datetime(2026, 10, 13, 6, 4)     # 00:04 MDT 10-13


@pytest.fixture(autouse=True)
def _fresh_cache():
    moon._cache.clear()
    yield
    moon._cache.clear()


def _frozen_utcnow(monkeypatch, when):
    class _DT(datetime):
        @classmethod
        def utcnow(cls):
            return when
    monkeypatch.setattr(moon, "datetime", _DT)


def _window(cfg, day):
    tw = get_twilight_times(cfg.get_observatory(), datetime(2026, 10, day))
    return tw["astro_dark_start"], tw["astro_dark_end"]


# --- the night anchor ----------------------------------------------------------

@pytest.mark.parametrize("utc, local_date", [
    (datetime(2026, 10, 12, 20, 0), 12),   # 14:00 MDT
    (AT_1757, 12),
    (AT_2224, 12),                          # UTC already 10-13
    (AT_0004, 12),                          # local already 10-13
    (datetime(2026, 10, 13, 14, 0), 12),   # 08:00 MDT (callers roll at dawn)
    (datetime(2026, 10, 13, 18, 30), 13),  # 12:30 MDT
])
def test_night_base_date_is_noon_to_noon_local(tmp_path, utc, local_date):
    assert night_base_date(_cfg(tmp_path), utc) == datetime(2026, 10, local_date)


# --- moon_window_tonight -------------------------------------------------------

@pytest.mark.parametrize("now", [AT_1757, AT_2224, AT_0004])
def test_moon_window_is_tonights_all_night(tmp_path, now):
    cfg = _cfg(tmp_path)
    dusk, _dawn = _window(cfg, 12)
    mw = moon_window_tonight(cfg, now=now)
    assert mw["available"]
    assert mw["dusk_utc"] == dusk                 # 10-13 02:05Z, not 10-14
    assert mw["illum_pct"] <= 10
    assert broadband_deferred(mw) is False


def test_moon_window_rolls_to_the_next_night_after_dawn(tmp_path):
    cfg = _cfg(tmp_path)
    mw = moon_window_tonight(cfg, now=datetime(2026, 10, 13, 15, 0))  # 09:00 MDT
    assert mw["dusk_utc"] == _window(cfg, 13)[0]


def test_default_now_uses_the_clock_with_the_same_anchor(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _frozen_utcnow(monkeypatch, AT_2224)
    assert moon_window_tonight(cfg)["dusk_utc"] == _window(cfg, 12)[0]


# --- night_moon cache ------------------------------------------------------------

def test_night_moon_cache_is_keyed_by_the_window_not_the_label(tmp_path):
    """The forecast labels the 10-14/15 night "2026-10-14" (local date);
    moon_window_tonight labelled the 10-13/14 night "2026-10-14" (UTC date
    of its dusk). Same label, different nights: each gets its own moon."""
    cfg = _cfg(tmp_path)
    s14, e14 = _window(cfg, 14)
    s13, e13 = _window(cfg, 13)
    later = night_moon(cfg, "2026-10-14", s14, e14)
    tonight = night_moon(cfg, "2026-10-14", s13, e13)
    assert later["illum_pct"] >= 20 and tonight["illum_pct"] < 20


# --- the planner replay ------------------------------------------------------------

def _projects():
    def plan(f, exp, count, acq=0):
        return ExposurePlan(filter_type=f, exposure_seconds=exp, count=count,
                            acquired=acq, gain=200, offset=256)
    return [
        ImagingProject(id="m31", priority=50, target=CelestialTarget(
            name="Andromeda Galaxy", ra_hours=0.712, dec_degrees=41.27,
            object_type="galaxy"), exposure_plans=[
            plan(F.LUMINANCE, 450, 46, 19), plan(F.RED, 300, 15, 2),
            plan(F.GREEN, 300, 15, 2), plan(F.BLUE, 300, 15, 3)]),
        ImagingProject(id="heart", priority=60, target=CelestialTarget(
            name="Heart Nebula", ra_hours=2.555, dec_degrees=61.47,
            object_type="emission nebula"), exposure_plans=[
            plan(F.HA, 300, 54), plan(F.OIII, 300, 63, 2),
            plan(F.SII, 300, 63)]),
    ]


@pytest.mark.parametrize("now", [AT_1757, datetime(2026, 10, 13, 0, 2)])
def test_10_12_broadband_kept_after_the_utc_rollover(tmp_path, monkeypatch,
                                                     caplog, now):
    """18:02 local with the dashboard's forecast cache primed: Andromeda
    stays in the plan (it was skipped from 18:02 on)."""
    cfg = _cfg(tmp_path)
    _frozen_utcnow(monkeypatch, now)
    s14, e14 = _window(cfg, 14)
    night_moon(cfg, "2026-10-14", s14, e14)        # the forecast's 10-14 night
    with caplog.at_level(logging.INFO, "photonscript.scheduler.target_planner"):
        targets = plan_night_sequence(_projects(), cfg, now)
    names = [t.name for t in targets]
    assert "Andromeda Galaxy" in names, caplog.text
    assert "Skipping Andromeda" not in caplog.text


def test_after_local_midnight_the_planner_keeps_tonight(tmp_path, caplog):
    cfg = _cfg(tmp_path)
    with caplog.at_level(logging.INFO, "photonscript.scheduler.target_planner"):
        plan_night_sequence(_projects(), cfg, AT_0004)
    dusk, dawn = _window(cfg, 12)
    assert f"Dark window: {dusk} to {dawn}" in caplog.text


# --- the rule and its reason --------------------------------------------------------

def test_bright_moon_up_at_dusk_still_defers():
    up = {"available": True, "down_at_dusk": False, "alt_at_dusk_deg": 35.0,
          "set_local": "01:10"}
    assert broadband_deferred({**up, "illum_pct": 60})
    assert broadband_deferred({**up, "illum_pct": 20})
    for faint in (0, 1, 3, 13, 19):
        assert not broadband_deferred({**up, "illum_pct": faint})


def test_describe_moon_says_why():
    up = {"available": True, "down_at_dusk": False, "illum_pct": 62,
          "alt_at_dusk_deg": 34.6, "set_local": "01:10"}
    txt = describe_moon(up)
    assert "62% lit" in txt and "up at dusk (alt 35 deg)" in txt
    assert "sets 01:10 local" in txt and "broadband deferred" in txt
    down = {"available": True, "down_at_dusk": True, "illum_pct": 3,
            "alt_at_dusk_deg": -12.0, "rise_local_hh": 23, "rise_local_mm": 5}
    txt = describe_moon(down)
    assert "3% lit" in txt and "rises 23:05 local" in txt
    assert txt.endswith("broadband OK")
    assert "unavailable" in describe_moon({"available": False})


def test_skip_log_line_carries_the_reason(tmp_path, monkeypatch, caplog):
    from photonscript.scheduler import moon as moon_mod
    bright = {"available": True, "down_at_dusk": False, "illum_pct": 70,
              "alt_at_dusk_deg": 40.0, "set_local": "03:00"}
    monkeypatch.setattr(moon_mod, "moon_window_tonight",
                        lambda config, now=None: dict(bright))
    with caplog.at_level(logging.INFO, "photonscript.scheduler.target_planner"):
        targets = plan_night_sequence(_projects(), _cfg(tmp_path), AT_1757)
    assert "Andromeda Galaxy" not in [t.name for t in targets]
    assert ("Skipping Andromeda Galaxy tonight: broadband-only and the moon "
            "defers broadband (moon 70% lit, up at dusk (alt 40 deg), sets "
            "03:00 local: broadband deferred") in caplog.text
