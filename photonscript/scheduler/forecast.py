"""7-night observing forecast.

Pulls hourly cloud/wind/humidity/precipitation from Open-Meteo (free, no key)
and scores each of the next 7 nights: estimated usable dark hours and a
green / yellow / red rating. Cross-check links: Clear Outside and the AARO
status page.

Scoring per dark hour:
  cloud <= 25%            -> 1.0 usable hour
  cloud <= 60%            -> 0.5 usable hour (workable, watch it)
  cloud  > 60%            -> 0
  wind > 35 km/h, precip probability > 30%, or humidity > 90% -> 0 (hard gate)

Night rating: green >= 65% usable, yellow >= 30%, else red.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import httpx

OPEN_METEO = ("https://api.open-meteo.com/v1/forecast"
              "?latitude={lat}&longitude={lon}"
              "&hourly=cloud_cover,wind_speed_10m,relative_humidity_2m,"
              "precipitation_probability"
              "&forecast_days=8&timezone=auto")

CLOUD_GOOD, CLOUD_OK = 25, 60
WIND_MAX_KMH, PRECIP_MAX_PCT, HUMIDITY_MAX_PCT = 35, 30, 90


def _score_hour(cloud, wind, humidity, precip) -> float:
    if wind is not None and wind > WIND_MAX_KMH:
        return 0.0
    if precip is not None and precip > PRECIP_MAX_PCT:
        return 0.0
    if humidity is not None and humidity > HUMIDITY_MAX_PCT:
        return 0.0
    if cloud is None:
        return 0.0
    if cloud <= CLOUD_GOOD:
        return 1.0
    if cloud <= CLOUD_OK:
        return 0.5
    return 0.0


def _rate(pct: float) -> str:
    if pct >= 65:
        return "green"
    if pct >= 30:
        return "yellow"
    return "red"


def score_nights(hourly: dict, dark_windows: list[dict],
                 utc_offset_hours: float) -> list[dict]:
    """Pure scoring: hourly Open-Meteo data + per-night dark windows (UTC)."""
    times = hourly["time"]  # local ISO strings
    by_time = {t: i for i, t in enumerate(times)}
    cloud = hourly.get("cloud_cover", [])
    wind = hourly.get("wind_speed_10m", [])
    hum = hourly.get("relative_humidity_2m", [])
    precip = hourly.get("precipitation_probability", [])

    nights = []
    for w in dark_windows:
        start_utc, end_utc = w["astro_dark_start"], w["astro_dark_end"]
        if not start_utc or not end_utc:
            continue
        usable, dark_hours, clouds_seen = 0.0, 0.0, []
        t = start_utc
        while t < end_utc:
            local = t + timedelta(hours=utc_offset_hours)
            key = local.strftime("%Y-%m-%dT%H:00")
            idx = by_time.get(key)
            step = min(1.0, (end_utc - t).total_seconds() / 3600)
            dark_hours += step
            if idx is not None:
                c = cloud[idx] if idx < len(cloud) else None
                usable += step * _score_hour(
                    c,
                    wind[idx] if idx < len(wind) else None,
                    hum[idx] if idx < len(hum) else None,
                    precip[idx] if idx < len(precip) else None)
                if c is not None:
                    clouds_seen.append(c)
            t += timedelta(hours=1)

        pct = round(usable / dark_hours * 100) if dark_hours else 0
        nights.append({
            "date": w["date"],
            "dark_hours": round(dark_hours, 1),
            "usable_hours": round(usable, 1),
            "usable_pct": pct,
            "rating": _rate(pct),
            "avg_cloud_pct": round(sum(clouds_seen) / len(clouds_seen))
            if clouds_seen else None,
        })
    return nights


def _cache_path(config):
    from pathlib import Path
    return Path(config.data_dir) / "forecast_cache.json"


async def get_forecast(config) -> dict:
    """Fetch Open-Meteo and score the next 7 nights.

    On fetch failure, falls back to the last successful forecast (marked
    stale) so a transient network blip doesn't blank the outlook.
    """
    import json as _json
    from photonscript.shared.astronomy import get_twilight_times

    obs = config.get_observatory()
    url = OPEN_METEO.format(lat=obs.latitude, lon=obs.longitude)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(url)
            r.raise_for_status()
            data = r.json()
    except Exception as e:  # noqa: BLE001 — degrade to cached forecast
        cache = _cache_path(config)
        if cache.exists():
            stale = _json.loads(cache.read_text(encoding="utf-8"))
            stale["stale"] = True
            stale["stale_reason"] = f"{type(e).__name__}: {e}"
            return stale
        raise RuntimeError(f"Open-Meteo fetch failed "
                           f"({type(e).__name__}: {e or 'no detail'})") from e

    utc_offset = data.get("utc_offset_seconds", 0) / 3600
    windows = []
    now = datetime.utcnow()
    for d in range(7):
        night = now + timedelta(days=d)
        tw = get_twilight_times(obs, night.replace(hour=0, minute=0,
                                                   second=0, microsecond=0))
        windows.append({
            "date": (night + timedelta(hours=utc_offset)).strftime("%Y-%m-%d"),
            "astro_dark_start": tw.get("astro_dark_start"),
            "astro_dark_end": tw.get("astro_dark_end"),
        })

    result = {
        "fetched_at": now.isoformat() + "Z",
        "source": "open-meteo.com",
        "cross_check": {
            "clear_outside": f"https://clearoutside.com/forecast/"
                             f"{obs.latitude:.2f}/{obs.longitude:.2f}",
            "aaro_status": "https://status.astronomyacres.com/",
        },
        "nights": score_nights(data["hourly"], windows, utc_offset),
    }
    try:
        cache = _cache_path(config)
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(_json.dumps(result), encoding="utf-8")
    except OSError:
        pass
    return result
