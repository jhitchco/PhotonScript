"""Pre-sunset evening forecast Pushover formatting.

The scheduler pushes tonight's viewing outlook a few hours before sunset; these
cover the pure formatter that turns a scored forecast night + the astro-dark
window into the notification title/body.
"""

from datetime import datetime

from photonscript.scheduler.forecast import (
    _good_windows, format_evening_forecast)


DUSK = datetime(2026, 9, 21, 20, 34)   # local astronomical dark start
DAWN = datetime(2026, 9, 22, 4, 58)    # local astronomical dark end
XCHK = {"clear_outside": "https://clearoutside.com/forecast/31.91/-109.02",
        "aaro_status": "https://status.astronomyacres.com/"}


def _hourly(*scores):
    # scores keyed to consecutive local hours starting at 20:00
    return [{"local": f"{20 + i}" if 20 + i < 24 else f"{20 + i - 24}",
             "score": s, "cloud": 10} for i, s in enumerate(scores)]


def test_good_windows_contiguous_runs():
    hourly = [
        {"local": "20", "score": 1.0}, {"local": "21", "score": 0.75},
        {"local": "22", "score": 0.35},  # marginal breaks the run
        {"local": "23", "score": 1.0}, {"local": "0", "score": 1.0},
    ]
    assert _good_windows(hourly) == ["20:00-22:00", "23:00-01:00"]


def test_good_windows_none_when_all_poor():
    assert _good_windows([{"local": "21", "score": 0.0},
                          {"local": "22", "score": 0.35}]) == []


def test_green_night_message_has_rating_gate_windows_and_moon():
    night = {
        "date": "2026-09-21", "dark_hours": 8.4, "usable_hours": 7.9,
        "usable_pct": 94, "rating": "green", "avg_cloud_pct": 12,
        "hourly": _hourly(1.0, 1.0, 0.75, 0.0, 1.0, 1.0, 1.0, 1.0),
        "moon": {"illum_pct": 18, "moon_free_h": 6.1, "tag": "dark"},
    }
    title, msg = format_evening_forecast(night, DUSK, DAWN, XCHK)
    assert "GREEN" in title and "7.9/8.4h" in title
    assert "usable" in msg
    # the astronomical-dark gate, with local HH:MM bounds and span
    assert "Astro dark (gate): 20:34-04:58 local (8.4h)" in msg
    assert "Usable windows:" in msg
    assert "Moon: 18% illum, 6.1 dark h moon-free" in msg
    assert XCHK["clear_outside"] in msg


def test_red_night_reports_no_usable_windows():
    night = {
        "date": "2026-09-21", "dark_hours": 8.0, "usable_hours": 0.4,
        "usable_pct": 5, "rating": "red", "avg_cloud_pct": 88,
        "hourly": _hourly(0.0, 0.0, 0.35, 0.0, 0.0, 0.0, 0.0, 0.0),
        "moon": {"illum_pct": 80, "moon_free_h": 0.0, "tag": "bright"},
    }
    title, msg = format_evening_forecast(night, DUSK, DAWN, XCHK)
    assert "RED" in title
    assert "none expected" in msg


def test_missing_night_is_graceful_but_still_gives_the_gate():
    title, msg = format_evening_forecast(None, DUSK, DAWN, XCHK)
    assert "unavailable" in title.lower()
    assert "Astro dark (gate):" in msg


def test_stale_forecast_is_flagged():
    night = {"date": "2026-09-21", "dark_hours": 8.0, "usable_hours": 5.0,
             "usable_pct": 63, "rating": "yellow", "avg_cloud_pct": 45,
             "hourly": _hourly(1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0),
             "moon": {"illum_pct": 40, "moon_free_h": 3.0}}
    _, msg = format_evening_forecast(night, DUSK, DAWN, XCHK, stale=True)
    assert "stale" in msg.lower()
