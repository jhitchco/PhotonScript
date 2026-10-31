"""A synthetic sun for flat-plan tests (2026-10-24, flats v2).

Tests that mock the twilight (night_plan.compute_night_times) around "now"
need a sun that agrees with it: install(monkeypatch, sunset) replaces the
NOAA sun of sky_flat_model with a smooth day (altitude 60 cos(2 pi h / 24)
+ 3.09, h hours after a solar noon 6 h 15 min before `sunset`, so the sun
is at -0.833 deg at `sunset` and sinks about 0.26 deg / min there) and the
flat plan's search spans with the half-days of that sun. Azimuth: 255 in
the evening, 105 in the morning.
"""
import math
from datetime import timedelta

from photonscript.scheduler import flat_plan as fp
from photonscript.scheduler import sky_flat_model as sfm


def install(monkeypatch, sunset):
    noon = sunset - timedelta(hours=6.25)

    def alt_az(lat, lon, when):
        w = when.replace(tzinfo=None) if when.tzinfo else when
        h = (w - noon).total_seconds() / 3600.0
        alt = 60.0 * math.cos(2 * math.pi * h / 24.0) + 3.09
        return alt, (255.0 if (h % 24.0) < 12.0 else 105.0)
    monkeypatch.setattr(sfm, "sun_alt_az", alt_az)
    monkeypatch.setattr(fp, "spans", lambda config, night: (
        (noon + timedelta(minutes=1), noon + timedelta(hours=12)),
        (noon + timedelta(hours=12, minutes=1), noon + timedelta(hours=24))))
    return {"noon": noon, "sunrise": sfm.time_at_alt(
        None, -0.833, noon + timedelta(hours=12, minutes=1), noon + timedelta(hours=24))}
