"""The dashboard's tonight and the forecast's 7 nights follow the LOCAL
night (localtime.night_window: noon to noon, rolled after dawn), not the
UTC date. 2026-10-13 MDT (UTC-6): 17:57 is still 10-13 UTC, 18:02 is
already 10-14 UTC, and 00:04 is the night that began on 10-13."""
from datetime import datetime, timedelta

import pytest

from photonscript.shared.config import PhotonScriptConfig

# local MDT -> naive UTC
T_1757 = datetime(2026, 10, 13, 23, 57)
T_1802 = datetime(2026, 10, 14, 0, 2)
T_0004 = datetime(2026, 10, 14, 6, 4)
DUSK_1013_UTC = datetime(2026, 10, 14, 2, 5)    # astro dusk 20:05 MDT 10-13


@pytest.fixture
def cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data")


def _near(dt, ref, minutes=30):
    return abs(dt.replace(tzinfo=None) - ref) < timedelta(minutes=minutes)


@pytest.mark.parametrize("now", [T_1757, T_1802, T_0004])
def test_dashboard_targets_are_tonights_local_night(now, cfg, monkeypatch):
    import photonscript.scheduler.app as app
    monkeypatch.setattr(app, "_dashboard_cache", {})
    seen = []
    monkeypatch.setattr(app, "rank_targets_for_night",
                        lambda targets, obs, day: seen.append(day) or [])
    twilight, ranked = app._dashboard_targets(cfg.get_observatory(), now, cfg)
    assert seen == [datetime(2026, 10, 13)]
    assert _near(twilight["astro_dark_start"], DUSK_1013_UTC)
    assert list(app._dashboard_cache)[0][0].isoformat() == "2026-10-13"


def test_dashboard_cache_rolls_after_dawn_not_at_utc_midnight(cfg, monkeypatch):
    import photonscript.scheduler.app as app
    monkeypatch.setattr(app, "_dashboard_cache", {})
    calls = []
    monkeypatch.setattr(app, "rank_targets_for_night",
                        lambda targets, obs, day: calls.append(day) or [])
    obs = cfg.get_observatory()
    for now in (T_1757, T_1802, T_0004):
        app._dashboard_targets(obs, now, cfg)
    assert calls == [datetime(2026, 10, 13)]            # one night, one ranking
    app._dashboard_targets(obs, datetime(2026, 10, 14, 15, 0), cfg)  # 09:00 MDT
    assert calls[-1] == datetime(2026, 10, 14)          # dawn passed: next night


@pytest.mark.parametrize("now", [T_1757, T_1802, T_0004])
def test_forecast_nights_start_at_tonights_local_date(now, cfg, monkeypatch, tmp_path):
    from photonscript.scheduler import forecast as fc
    got = {}

    def _score(hourly, windows, off):
        got["windows"] = windows
        return []
    monkeypatch.setattr(fc, "score_nights", _score)
    monkeypatch.setattr(fc, "_cache_path", lambda c: tmp_path / "fc.json")
    fc._score_forecast(cfg, {"hourly": {}, "utc_offset_seconds": -6 * 3600}, now=now)
    w = got["windows"]
    assert [x["date"] for x in w] == [f"2026-10-{d}" for d in range(13, 20)]
    assert _near(w[0]["astro_dark_start"], DUSK_1013_UTC)
    for x in w:   # every label pairs with its own evening's dark window
        assert x["astro_dark_start"].replace(tzinfo=None).date().isoformat() > x["date"]
        assert (x["astro_dark_start"].replace(tzinfo=None) - timedelta(hours=6)
                ).date().isoformat() == x["date"]
