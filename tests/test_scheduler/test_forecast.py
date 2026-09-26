"""Tests for forecast night scoring."""

from datetime import datetime

from photonscript.scheduler.forecast import score_nights, _score_hour, _rate


def test_hour_scoring_gates():
    assert _score_hour(10, 5, 40, 0) == 1.0     # clear
    assert _score_hour(45, 5, 40, 0) == 0.75    # partly cloudy, workable
    assert _score_hour(70, 5, 40, 0) == 0.35    # marginal gaps
    assert _score_hour(90, 5, 40, 0) == 0.0     # overcast
    assert _score_hour(10, 50, 40, 0) == 0.0    # too windy
    assert _score_hour(10, 5, 95, 0) == 0.0     # dew risk
    assert _score_hour(10, 5, 40, 60) == 0.0    # rain risk


def test_ratings():
    assert _rate(80) == "green"
    assert _rate(45) == "yellow"
    assert _rate(10) == "red"


def test_score_nights_clear_night():
    # 6h dark window, all hours clear -> 100% green
    hourly = {
        "time": [f"2026-07-03T{h:02d}:00" for h in range(24)],
        "cloud_cover": [10] * 24,
        "wind_speed_10m": [5] * 24,
        "relative_humidity_2m": [30] * 24,
        "precipitation_probability": [0] * 24,
    }
    windows = [{
        "date": "2026-07-03",
        "astro_dark_start": datetime(2026, 7, 3, 9, 0),   # UTC
        "astro_dark_end": datetime(2026, 7, 3, 15, 0),
    }]
    nights = score_nights(hourly, windows, utc_offset_hours=-6)
    assert len(nights) == 1
    n = nights[0]
    assert n["dark_hours"] == 6.0
    assert n["usable_hours"] == 6.0
    assert n["usable_pct"] == 100
    assert n["rating"] == "green"


def test_score_nights_cloudy_night_is_red():
    hourly = {
        "time": [f"2026-07-03T{h:02d}:00" for h in range(24)],
        "cloud_cover": [95] * 24,
        "wind_speed_10m": [5] * 24,
        "relative_humidity_2m": [30] * 24,
        "precipitation_probability": [0] * 24,
    }
    windows = [{
        "date": "2026-07-03",
        "astro_dark_start": datetime(2026, 7, 3, 9, 0),
        "astro_dark_end": datetime(2026, 7, 3, 15, 0),
    }]
    n = score_nights(hourly, windows, -6)[0]
    assert n["usable_hours"] == 0.0
    assert n["rating"] == "red"
