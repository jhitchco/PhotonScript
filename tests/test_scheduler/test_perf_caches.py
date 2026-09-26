"""2026-09 page-performance pass: vectorized astronomy + request caches.

The dashboard took ~13 s and /api/runs ~50 s on the scope PC. These tests pin
the equivalence of the fast paths to the old per-sample math and the
invalidation behavior of each cache.
"""

import json
import time
from datetime import datetime, timedelta

import pytest
from astropy.coordinates import AltAz
from astropy.time import Time

from photonscript.shared import astronomy as astro
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget, ObservatoryLocation

OBS = ObservatoryLocation(latitude=31.906944, longitude=-109.021367,
                          elevation=1250.0)


def _scalar_visibility(target, obs, date_utc, min_altitude=30.0):
    """The pre-2026-09 algorithm: one astropy transform per 10-min sample."""
    tw = astro.get_twilight_times(obs, date_utc)
    start, end = tw["astro_dark_start"], tw["astro_dark_end"]
    samples = int((end - start).total_seconds() / 600)
    visible = []
    for i in range(samples + 1):
        t = start + timedelta(minutes=i * 10)
        frame = AltAz(obstime=Time(t), location=astro.get_earth_location(obs))
        alt = float(astro.get_sky_coord(target).transform_to(frame).alt.deg)
        if alt >= min_altitude:
            visible.append(t)
    if not visible:
        return {"visible": False, "hours": 0.0, "rise_time": None, "set_time": None}
    return {"visible": True, "hours": round(len(visible) * 10 / 60, 1),
            "rise_time": visible[0], "set_time": visible[-1],
            "transit_time": astro.compute_transit_time(target, obs, date_utc)}


@pytest.mark.parametrize("when", [datetime(2026, 1, 5, 3, 4, 5),
                                  datetime(2026, 9, 26, 20, 53, 17, 123456)])
def test_vectorized_visibility_matches_scalar(when):
    for target in astro.get_seasonal_targets(when.month)[:6]:
        assert astro.compute_visibility_window(target, OBS, when) == \
            _scalar_visibility(target, OBS, when)


def test_rank_matches_per_target_windows():
    when = datetime(2026, 9, 26, 20, 53, 17)
    targets = astro.get_seasonal_targets(9)[:15]
    ranked = astro.rank_targets_for_night(targets, OBS, when)
    assert ranked, "September should have visible targets"
    for r in ranked:
        assert r["visibility"] == astro.compute_visibility_window(
            r["target"], OBS, when)
    hours = [r["visibility"]["hours"] for r in ranked]
    assert hours == sorted(hours, reverse=True)


def test_rank_handles_empty_target_list():
    assert astro.rank_targets_for_night([], OBS, datetime(2026, 9, 26)) == []


def test_twilight_is_memoized_and_mutation_safe():
    when = datetime(2026, 3, 1, 12, 0)
    a = astro.get_twilight_times(OBS, when)
    before = astro._twilight_cached.cache_info().hits
    b = astro.get_twilight_times(OBS, when)
    assert astro._twilight_cached.cache_info().hits == before + 1
    assert a == b and a is not b
    a["astro_dark_start"] = None           # a caller scribbling on its copy
    assert astro.get_twilight_times(OBS, when)["astro_dark_start"] is not None


# --- /api/runs caches -------------------------------------------------------

def _config(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"))


def test_load_subs_cache_invalidates_on_write(tmp_path):
    from photonscript.scheduler import runs
    cfg = _config(tmp_path)
    p = runs.runs_dir(cfg) / "2026-09-20_subs.jsonl"
    p.write_text(json.dumps({"file": "a.fits", "hfr": 2.0}) + "\n", encoding="utf-8")
    first = runs._load_subs(cfg, "2026-09-20")
    first[0]["hfr"] = 99                    # callers mutate rows; cache must not
    assert runs._load_subs(cfg, "2026-09-20")[0]["hfr"] == 2.0
    runs.append_sub_record(cfg, "2026-09-20", {"file": "b.fits", "hfr": 3.0})
    assert [s["file"] for s in runs._load_subs(cfg, "2026-09-20")] == ["a.fits", "b.fits"]
    # same-size rewrite (the coarse-mtime hazard) is caught by explicit invalidation
    rows = runs._load_subs(cfg, "2026-09-20")
    rows[0]["hfr"], rows[1]["hfr"] = 3.0, 2.0
    runs._rewrite_subs(cfg, "2026-09-20", rows)
    assert [s["hfr"] for s in runs._load_subs(cfg, "2026-09-20")] == [3.0, 2.0]
    p.unlink()
    assert runs._load_subs(cfg, "2026-09-20") == []


def test_fits_counts_cached_until_ttl_or_invalidate(tmp_path, monkeypatch):
    from photonscript.scheduler import runs
    night = tmp_path / "fits" / "2020-01-01"
    (night / "M31" / "LIGHT").mkdir(parents=True)
    (night / "FLAT").mkdir()
    (night / "M31" / "LIGHT" / "a.fits").write_bytes(b"")
    (night / "FLAT" / "f.fits").write_bytes(b"")
    assert runs._night_fits_counts(night, "2020-01-01") == (1, 1)
    (night / "M31" / "LIGHT" / "b.fits").write_bytes(b"")
    assert runs._night_fits_counts(night, "2020-01-01") == (1, 1)   # old night: cached
    runs.invalidate_fits_counts("2020-01-01")
    assert runs._night_fits_counts(night, "2020-01-01") == (2, 1)
    # recent nights use the short TTL
    monkeypatch.setattr(runs, "_FITS_COUNT_TTL_RECENT", 0.0)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    recent = tmp_path / "fits" / today
    recent.mkdir(parents=True)
    assert runs._night_fits_counts(recent, today) == (0, 0)
    (recent / "c.fits").write_bytes(b"")
    assert runs._night_fits_counts(recent, today) == (1, 0)


def test_list_runs_uses_cached_counts(tmp_path):
    from photonscript.scheduler import runs
    cfg = _config(tmp_path)
    night = tmp_path / "fits" / "2020-02-02"
    night.mkdir(parents=True)
    (night / "x.fits").write_bytes(b"")
    runs.invalidate_fits_counts()
    out = runs.list_runs(cfg)
    assert out[0]["date"] == "2020-02-02" and out[0]["lights"] == 1


# --- Syncthing stale-while-revalidate -----------------------------------------

def test_syncthing_names_serve_stale_and_refresh_in_background(monkeypatch):
    import photonscript.scheduler.app as app
    monkeypatch.setattr(app, "_syncthing_settings", lambda: ("u", "k", "f", "d"))
    calls = []

    def fake_refresh(settings):
        calls.append(time.time())
        names = {f"n{len(calls)}"}
        app._remoteneed_cache.update(t=time.time(), names=names, entries=[])
        return names

    monkeypatch.setattr(app, "_refresh_remoteneed", fake_refresh)
    monkeypatch.setattr(app, "_remoteneed_cache", {"t": 0.0, "names": None})
    assert app._syncthing_pending_names() == {"n1"}          # cold: waits once
    assert app._syncthing_pending_names() == {"n1"} and len(calls) == 1  # fresh
    app._remoteneed_cache["t"] = time.time() - 60             # stale but usable
    assert app._syncthing_pending_names() == {"n1"}           # served immediately
    for _ in range(50):
        if len(calls) == 2:
            break
        time.sleep(0.02)
    assert len(calls) == 2                                    # refreshed in bg
    assert app._syncthing_pending_names() == {"n2"}
    app._remoteneed_cache["t"] = time.time() - 3600           # too old: refetch inline
    assert app._syncthing_pending_names() == {"n3"}


def test_syncthing_not_configured_returns_none(monkeypatch):
    import photonscript.scheduler.app as app
    monkeypatch.setattr(app, "_syncthing_settings", lambda: None)
    assert app._syncthing_pending_names() is None


# --- dashboard + forecast -----------------------------------------------------

def test_dashboard_targets_cached_per_day(monkeypatch):
    import photonscript.scheduler.app as app
    monkeypatch.setattr(app, "_dashboard_cache", {})
    calls = []
    real = app.rank_targets_for_night

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(app, "rank_targets_for_night", counting)
    t1 = datetime(2026, 9, 26, 14, 0, 7)
    tw1, r1 = app._dashboard_targets(OBS, t1)
    tw2, r2 = app._dashboard_targets(OBS, t1 + timedelta(hours=3, seconds=11))
    assert len(calls) == 1 and r1 is r2 and tw1 == tw2
    app._dashboard_targets(OBS, t1 + timedelta(days=1))
    assert len(calls) == 2


async def test_forecast_memoized(monkeypatch):
    from photonscript.scheduler import forecast
    monkeypatch.setattr(forecast, "_forecast_mem", {})
    n = []

    async def fake_fetch(config):
        n.append(1)
        return {"nights": [{"date": "2026-09-26"}], "stale": False}

    monkeypatch.setattr(forecast, "_fetch_forecast", fake_fetch)
    cfg = PhotonScriptConfig(_env_file=None)
    a = await forecast.get_forecast(cfg)
    a["nights"].clear()                      # caller mutation must not leak
    b = await forecast.get_forecast(cfg)
    assert len(n) == 1 and b["nights"] == [{"date": "2026-09-26"}]
    monkeypatch.setattr(forecast, "_FORECAST_TTL_S", 0.0)
    await forecast.get_forecast(cfg)
    assert len(n) == 2


async def test_stale_forecast_not_memoized(monkeypatch):
    from photonscript.scheduler import forecast
    monkeypatch.setattr(forecast, "_forecast_mem", {})
    n = []

    async def fake_fetch(config):
        n.append(1)
        return {"nights": [], "stale": True}

    monkeypatch.setattr(forecast, "_fetch_forecast", fake_fetch)
    cfg = PhotonScriptConfig(_env_file=None)
    await forecast.get_forecast(cfg)
    await forecast.get_forecast(cfg)
    assert len(n) == 2
