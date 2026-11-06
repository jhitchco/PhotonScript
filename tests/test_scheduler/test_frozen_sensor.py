"""Frozen sensor watch (2026-10-16): a weather / camera / focuser reading that
has not changed at all for frozen_sensor_minutes is a warning (chip, event,
one push per rig and sensor per night). Never acted on."""
import json
from datetime import datetime, timedelta

import pytest

from photonscript.scheduler import frozen_sensor as fz
from photonscript.shared.config import PhotonScriptConfig

T0 = datetime(2026, 10, 16, 4, 0, 0)
CAM = {"Connected": True, "CoolerOn": True, "Temperature": -10.0, "CoolerPower": 43.0}
WX = {"Connected": True, "Temperature": 8.123456, "Humidity": 61.2,
      "DewPoint": 1.04, "Pressure": 1012.3, "WindSpeed": 0.0,
      "SkyTemperature": float("nan")}


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(data_dir=str(tmp_path), **kw)


def _reader(env):
    async def read(cfg, rig):
        return {"camera": fz.camera_value(env.get("camera")),
                "focuser": fz.focuser_value(env.get("focuser")),
                "weather": fz.weather_value(env.get("weather"))}
    return read


async def _run(cfg, env, minutes, mon, notes):
    async def notify(cfg_, msg, **kw):
        notes.append((kw.get("title"), msg, kw.get("priority")))
    out = None
    for m in minutes:
        out = await fz.tick(cfg, now=T0 + timedelta(minutes=m), window=True,
                            read=_reader(env), notify=notify, monitor=mon)
    return out


# --- threshold -----------------------------------------------------------------

def test_feed_threshold_is_exact_minutes():
    mon = fz.FrozenWatch()
    v = (8.123456,)
    assert mon.feed("rc16", "focuser", v, T0, 30)["state"] == "ok"
    r = mon.feed("rc16", "focuser", v, T0 + timedelta(minutes=29, seconds=59), 30)
    assert r["state"] == "ok" and not r["alert"]
    r = mon.feed("rc16", "focuser", v, T0 + timedelta(minutes=30), 30)
    assert r["state"] == "frozen" and r["alert"] and r["became"]
    r = mon.feed("rc16", "focuser", v, T0 + timedelta(minutes=31), 30)
    assert r["state"] == "frozen" and not r["alert"]      # once


def test_a_tiny_change_is_a_change_but_eps_noise_is_not():
    mon = fz.FrozenWatch()
    mon.feed("rc16", "focuser", (8.123456,), T0, 30)
    r = mon.feed("rc16", "focuser", (8.1234561,), T0 + timedelta(minutes=31), 30)
    assert r["state"] == "frozen"                          # within 1e-6
    r = mon.feed("rc16", "focuser", (8.12,), T0 + timedelta(minutes=32), 30)
    assert r["state"] == "ok" and r["cleared"]


def test_unreadable_resets_the_clock():
    mon = fz.FrozenWatch()
    mon.feed("rc16", "focuser", (21.29,), T0, 30)
    mon.feed("rc16", "focuser", None, T0 + timedelta(minutes=20), 30)
    r = mon.feed("rc16", "focuser", (21.29,), T0 + timedelta(minutes=35), 30)
    assert r["state"] == "ok"


def test_camera_needs_cooler_on_and_both_values_still():
    assert fz.camera_value({**CAM, "CoolerOn": False}) is None
    assert fz.camera_value({**CAM, "Connected": False}) is None
    mon = fz.FrozenWatch()
    mon.feed("rc16", "camera", fz.camera_value(CAM), T0, 30)
    # temperature pinned at setpoint but TEC power moving: not frozen
    r = mon.feed("rc16", "camera", fz.camera_value({**CAM, "CoolerPower": 44.0}),
                 T0 + timedelta(minutes=40), 30)
    assert r["state"] == "ok"


def test_weather_is_the_whole_station_and_skips_nan():
    v = fz.weather_value(WX)
    assert ("SkyTemperature", float("nan")) not in v and len(v) == 5
    assert fz.weather_value({**WX, "Connected": False}) is None
    mon = fz.FrozenWatch()
    mon.feed("rc16", "weather", v, T0, 30)
    # wind stays 0 and humidity stays put, but the temperature moves
    r = mon.feed("rc16", "weather", fz.weather_value({**WX, "Temperature": 8.0}),
                 T0 + timedelta(minutes=45), 30)
    assert r["state"] == "ok"


# --- tick: warning, event, one push a night, no action -------------------------

@pytest.mark.asyncio
async def test_tick_flags_frozen_once_per_night_and_clears(tmp_path):
    cfg = _cfg(tmp_path)
    mon, notes = fz.FrozenWatch(), []
    env = {"camera": CAM, "focuser": {"Connected": True, "Temperature": 21.29},
           "weather": WX}
    await _run(cfg, env, range(0, 30, 5), mon, notes)
    assert not notes and not mon.view()["frozen"]
    await _run(cfg, env, [30, 31, 40], mon, notes)
    frozen = mon.view()["frozen"]
    assert {f["sensor"] for f in frozen} == {"camera", "focuser", "weather"}
    assert len(notes) == 3 and all(n[2] == 0 for n in notes)   # one per sensor
    assert all("changed nothing" in n[1] for n in notes)
    # the focuser moves again, then freezes again the same night: no 2nd push
    env["focuser"] = {"Connected": True, "Temperature": 20.5}
    await _run(cfg, env, [41], mon, notes)
    assert "focuser" not in {f["sensor"] for f in mon.view()["frozen"]}
    await _run(cfg, env, [72], mon, notes)
    assert "focuser" in {f["sensor"] for f in mon.view()["frozen"]}
    assert len(notes) == 3
    lines = [json.loads(x) for p in (tmp_path / "runs").glob("*_events.jsonl")
             for x in p.read_text().splitlines()]
    kinds = [(x["value"], x["sensor"]) for x in lines if x["kind"] == "frozen_sensor"]
    assert ("clear", "focuser") in kinds
    assert kinds.count(("frozen", "focuser")) == 2


@pytest.mark.asyncio
async def test_varying_readings_never_flag(tmp_path):
    cfg = _cfg(tmp_path)
    mon, notes = fz.FrozenWatch(), []
    for m in range(0, 120, 5):
        env = {"camera": {**CAM, "CoolerPower": 40.0 + (m % 3)},
               "focuser": {"Connected": True, "Temperature": 10 - m * 0.01},
               "weather": {**WX, "Pressure": 1012.3 + m * 0.01}}
        await _run(cfg, env, [m], mon, notes)
    assert not notes and not mon.view()["frozen"]


@pytest.mark.asyncio
async def test_panel_mode_and_threshold_config(tmp_path):
    cfg = _cfg(tmp_path, frozen_sensor_mode="panel", frozen_sensor_minutes=10)
    mon, notes = fz.FrozenWatch(), []
    env = {"focuser": {"Connected": True, "Temperature": 21.29}}
    await _run(cfg, env, [0, 9], mon, notes)
    assert not mon.view()["frozen"]
    await _run(cfg, env, [10], mon, notes)
    assert mon.view()["frozen"] and not notes            # panel: no push


@pytest.mark.asyncio
async def test_off_and_outside_window_do_nothing(tmp_path):
    mon, notes = fz.FrozenWatch(), []
    env = {"focuser": {"Connected": True, "Temperature": 21.29}}
    cfg = _cfg(tmp_path, frozen_sensor_mode="off")
    await _run(cfg, env, [0, 60], mon, notes)
    assert mon.view()["frozen"] == [] and not notes
    cfg = _cfg(tmp_path)

    async def boom(cfg_, rig):
        raise AssertionError("no reads outside the window")
    assert await fz.tick(cfg, now=T0, window=False, read=boom, monitor=mon) == {}


def test_new_night_resets_latch_and_tracks():
    mon = fz.FrozenWatch()
    mon.roll("2026-10-15")
    mon.feed("rc16", "focuser", (21.29,), T0, 30)
    mon.feed("rc16", "focuser", (21.29,), T0 + timedelta(minutes=31), 30)
    mon.roll("2026-10-16")
    assert not mon.alerted and not mon.tracks
