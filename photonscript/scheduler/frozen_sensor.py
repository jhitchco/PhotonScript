"""Frozen sensor watch (2026-10-16): a reading that should vary has not
changed at all.

A driver that stops updating keeps answering with its last value (the RC16
EAF thermometer sat at 21.29 C for hours, the same value while the device
was missing; an Alpaca station can serve a cached payload). Nothing errors,
so nothing else notices. Every PS-150 loop tick (60 s), dusk to dawn
(nina_watch_sun_alt_deg), per rig, read only (GETs):

  camera    NINA's camera (Temperature, CoolerPower) together, only while
            Connected and CoolerOn: a regulated sensor can hold its
            setpoint to 0.1 C, but its TEC power still moves. Cooler off =
            not judged (a warm sensor at a steady ambient may sit still).
  focuser   NINA's focuser Temperature (the EAF thermometer, the ambient
            proxy of the focus model), while Connected.
  weather   NINA's weather device: every finite numeric field of
            WEATHER_FIELDS together (temperature, humidity, dew point,
            pressure, sky temperature / quality / brightness, cloud cover,
            wind), while Connected. One field may legitimately stay put
            (humidity 100 %, no wind); the whole station may not.

The safety monitor is a boolean that legitimately reads the same all night:
it is not judged here (its liveness is the armer's dropout grace and the
agent's reconnect watchdog).

A reading identical (within EPS, "to many decimals") for
frozen_sensor_minutes (default 30) is frozen: a warning in the NINA watch
output (GET /api/nina/watch "frozen", the dashboard chip), a run event kind
"frozen_sensor" (value "frozen", then "clear" when it moves again), and one
Pushover per rig and sensor per night (priority 0). An unreadable reading
(device not connected, API down) resets that sensor's clock.
frozen_sensor_mode: alert (default) | panel (chip + events, no push) | off.

Observe only: this never commands NINA or changes what anything else does
with the value.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime

logger = logging.getLogger(__name__)

MODES = ("alert", "panel", "off")
KIND = "frozen_sensor"
TITLE = "PhotonScript frozen sensor"
EPS = 1e-6
SENSORS = ("camera", "focuser", "weather")
WEATHER_FIELDS = ("Temperature", "Humidity", "DewPoint", "Pressure",
                  "SkyTemperature", "SkyQuality", "SkyBrightness", "CloudCover",
                  "WindSpeed", "WindDirection")
LABEL = {"camera": "camera sensor temperature + cooler power",
         "focuser": "focuser (EAF) temperature",
         "weather": "weather station readings"}


def mode(cfg) -> str:
    m = str(getattr(cfg, "frozen_sensor_mode", "alert") or "alert").strip().lower()
    return m if m in MODES else "alert"


def limit_min(cfg) -> float:
    try:
        v = float(getattr(cfg, "frozen_sensor_minutes", 30.0))
    except (TypeError, ValueError):
        return 30.0
    return max(1.0, v) if math.isfinite(v) else 30.0


def _num(v) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


# ------------------------------------------------------------------ values

def camera_value(info: dict | None):
    """(temperature, cooler power) while connected with the cooler on, else
    None (not judged)."""
    if not isinstance(info, dict) or info.get("Connected") is False:
        return None
    if info.get("CoolerOn") is not True:
        return None
    t = _num(info.get("Temperature"))
    if t is None:
        return None
    return (t, _num(info.get("CoolerPower")))


def focuser_value(info: dict | None):
    if not isinstance(info, dict) or info.get("Connected") is False:
        return None
    t = _num(info.get("Temperature"))
    return None if t is None else (t,)


def weather_value(info: dict | None):
    """((field, value), ...) of the finite WEATHER_FIELDS, None when not
    connected or nothing is finite."""
    if not isinstance(info, dict) or info.get("Connected") is not True:
        return None
    vals = tuple((k, v) for k in WEATHER_FIELDS
                 if (v := _num(info.get(k))) is not None)
    return vals or None


def same(a, b) -> bool:
    """Two values (tuples of numbers or (name, number) pairs) are identical
    within EPS."""
    if a is None or b is None or len(a) != len(b):
        return False
    for x, y in zip(a, b):
        if isinstance(x, tuple):
            if x[0] != y[0] or abs(x[1] - y[1]) > EPS:
                return False
        elif x is None or y is None:
            if x is not y:
                return False
        elif abs(x - y) > EPS:
            return False
    return True


def show(sensor: str, value) -> str:
    if value is None:
        return "-"
    if sensor == "camera":
        t, p = value
        return f"{t:.2f} C" + ("" if p is None else f", cooler {p:g} %")
    if sensor == "focuser":
        return f"{value[0]:.2f} C"
    return ", ".join(f"{k} {v:g}" for k, v in value)


# ------------------------------------------------------------------ monitor

class FrozenWatch:
    """Per (rig, sensor): the value, since when it has been identical, and
    whether it is frozen; the once-a-night push latch."""

    def __init__(self):
        self.night: str | None = None
        self.tracks: dict[tuple[str, str], dict] = {}
        self.alerted: set[tuple[str, str]] = set()
        self.checked_at: datetime | None = None

    def roll(self, night: str | None) -> None:
        if night and night != self.night:
            self.night = night
            self.alerted = set()
            self.tracks = {}

    def feed(self, rig: str, sensor: str, value, now: datetime,
             limit: float) -> dict:
        """{"state": ok | frozen | unknown, "minutes", "became" (turned
        frozen this tick), "alert" (first time tonight for this rig and
        sensor: the push), "cleared" (was frozen, moved or went
        unreadable)}."""
        key = (rig, sensor)
        tr = self.tracks.get(key)
        res = {"state": "unknown", "minutes": None, "alert": False,
               "became": False, "cleared": False}
        if value is None:
            if tr and tr.get("frozen"):
                res["cleared"] = True
            self.tracks.pop(key, None)
            return res
        if tr is None or not same(tr["value"], value):
            if tr and tr.get("frozen"):
                res["cleared"] = True
            self.tracks[key] = {"value": value, "since": now, "seen": now,
                                "frozen": False}
            res.update(state="ok", minutes=0.0)
            return res
        tr["seen"] = now
        mins = (now - tr["since"]).total_seconds() / 60.0
        res["minutes"] = mins
        if mins >= limit:
            res["became"] = not tr["frozen"]
            tr["frozen"] = True
            res["state"] = "frozen"
            if key not in self.alerted:
                self.alerted.add(key)
                res["alert"] = True
        else:
            res["state"] = "ok"
        return res

    def view(self) -> dict:
        def iso(t):
            return t.replace(microsecond=0).isoformat() + "Z" if t else None
        sensors: dict = {}
        frozen = []
        for (rig, sensor), tr in sorted(self.tracks.items()):
            mins = ((tr["seen"] - tr["since"]).total_seconds() / 60.0)
            row = {"state": "frozen" if tr.get("frozen") else "ok",
                   "value": show(sensor, tr["value"]),
                   "unchanged_since": iso(tr["since"]),
                   "unchanged_min": round(mins, 1),
                   "pushed": (rig, sensor) in self.alerted}
            sensors.setdefault(rig, {})[sensor] = row
            if tr.get("frozen"):
                frozen.append({"rig": rig, "sensor": sensor,
                               "label": LABEL.get(sensor, sensor), **row})
        return {"night": self.night, "checked_at": iso(self.checked_at),
                "sensors": sensors, "frozen": frozen}


MONITOR = FrozenWatch()


# ------------------------------------------------------------------ io

async def read_rig(cfg, rig: str) -> dict:
    """{"camera", "focuser", "weather"} values for one rig (GETs only,
    never raises; None = not judged)."""
    from photonscript.scheduler.nina_watchdog import _api_get, rig_base
    base = rig_base(cfg, rig)
    out = {}
    for sensor, path, fn in (("camera", "/equipment/camera/info", camera_value),
                             ("focuser", "/equipment/focuser/info", focuser_value),
                             ("weather", "/equipment/weather/info", weather_value)):
        try:
            out[sensor] = fn(await _api_get(base, path))
        except Exception as e:  # noqa: BLE001
            logger.debug("frozen sensor: %s %s read failed: %s", rig, sensor, e)
            out[sensor] = None
    return out


def alert_text(cfg, rig: str, sensor: str, value, mins: float,
               since: datetime) -> str:
    from photonscript.scheduler.nina_watchdog import _local_hm, rig_name
    return (f"{rig_name(cfg, rig)}: the {LABEL.get(sensor, sensor)} has not "
            f"changed at all for {mins:.0f} min (since {_local_hm(cfg, since)} "
            f"local: {show(sensor, value)}). The driver may be frozen and "
            "serving a stale value. Check the device in NINA on the scope PC. "
            "Warning only: PhotonScript changed nothing.")


def _event(cfg, rig: str, value: str, detail: str, now: datetime, **extra) -> None:
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
        append_jsonl(events_path(cfg, night_of(cfg, now)),
                     {"t": iso_z(now), "rig": rig, "src": "photonscript",
                      "kind": KIND, "value": value, "detail": detail, **extra})
    except Exception as e:  # noqa: BLE001
        logger.warning("frozen sensor event (%s) not logged: %s", value, e)


async def _push(cfg, msg: str, notify=None) -> None:
    if notify is None:
        from photonscript.shared.pushover import notify as _n
        notify = _n
    try:
        await notify(cfg, msg, title=TITLE, priority=0, kind="info")
    except Exception as e:  # noqa: BLE001
        logger.warning("frozen sensor push failed: %s", e)


async def tick(cfg, now: datetime | None = None, window: bool | None = None,
               read=None, notify=None, monitor: FrozenWatch | None = None) -> dict:
    """One pass. read(cfg, rig) and notify are injectable for tests.
    Returns {rig: {sensor: feed result}} ({} when off or outside the
    window)."""
    from photonscript.scheduler.nina_watchdog import in_window
    from photonscript.shared.phd2_store import iso_z, night_of
    from photonscript.shared.rigs import rig_ids
    monitor = monitor or MONITOR
    now = now or datetime.utcnow()
    m = mode(cfg)
    if m == "off":
        monitor.tracks = {}
        return {}
    monitor.roll(night_of(cfg, now))
    win = in_window(cfg, now) if window is None else window
    if not win:
        monitor.tracks = {}
        return {}
    read = read or read_rig
    limit = limit_min(cfg)
    out: dict = {}
    for rig in rig_ids(cfg):
        try:
            vals = await read(cfg, rig)
        except Exception as e:  # noqa: BLE001
            logger.debug("frozen sensor: %s read failed: %s", rig, e)
            vals = {}
        for sensor in SENSORS:
            value = (vals or {}).get(sensor)
            tr = monitor.tracks.get((rig, sensor))
            since = tr["since"] if tr else now
            res = monitor.feed(rig, sensor, value, now, limit)
            out.setdefault(rig, {})[sensor] = res
            if res["became"]:
                msg = alert_text(cfg, rig, sensor, value, res["minutes"], since)
                logger.warning("frozen sensor: %s", msg)
                _event(cfg, rig, "frozen", msg, now, sensor=sensor,
                       since=iso_z(since), reading=show(sensor, value),
                       pushed=bool(res["alert"] and m == "alert"))
                if res["alert"] and m == "alert":
                    await _push(cfg, msg, notify)
            elif res["cleared"]:
                why = ("moved again" if value is not None
                       else "unreadable (not judged)")
                _event(cfg, rig, "clear", f"{LABEL.get(sensor, sensor)} {why}",
                       now, sensor=sensor)
    monitor.checked_at = now
    return out
