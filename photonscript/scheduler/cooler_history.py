"""PS-181: cooler history and setpoint reachability.

Darks are the hard part of calibration because the sensor has to sit at
the setpoint (0 C) for hours with the roof closed, and on a hot afternoon
the TEC cannot pull a 35 C sensor down that far. This module keeps a small
history of what each rig's cooler actually did and predicts, for a given
time or ambient temperature, whether the setpoint is reachable.

    record(config, rig, camera, focuser_temp=None, source="")   one sample
    load(config, rig, days)                                      samples
    fit(samples, setpoint, tol)                                  the model
    predict(model, setpoint, ambient=None, hour=None, max_power) one verdict
    reachability(config, rig, ambient=None, when=None)          load+fit+predict
    live_saturated(camera, setpoint, tol)                        TEC at 100 %
                                                                 above setpoint

Samples (data_dir/cooler_history/<rig>_<YYYY-MM>.jsonl, at most one per rig
every cooler_history_sample_s seconds): UTC time, sensor temperature, the
camera's setpoint, TEC power (%), cooler on, and the focuser temperature as
the ambient proxy (the only outside-ish thermometer each NINA reports).
The telescope agent's NINA poll and the calibration capture job feed it.

The model: at the setpoint the TEC power is roughly proportional to the
temperature difference it holds, power = k * (ambient - setpoint). k is a
least-squares fit through the origin over the samples that were at the
setpoint (2026-10-08 RC16: 62 % at 33 C ambient, 0 C setpoint, so k ~ 1.9
%/C and the 90 % limit sits near 47 C). Samples with the TEC at >= 98 %
and the sensor still above the setpoint mark an ambient where the cooler
could not cope. Per local hour the history also gives the typical ambient
(median focuser temperature), so a window can be judged before it comes.
Observe only: nothing here touches a camera.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

LOG_DIR = "cooler_history"
SATURATED_PCT = 98.0     # TEC flat out
MIN_DELTA_C = 3.0        # samples closer to the setpoint than this do not fit k
MIN_FIT_SAMPLES = 3      # fewer at-setpoint samples than this: no k
AT_SETPOINT_FRAC = 0.8   # an hour bucket counts as "reaches" above this share

_last: dict[str, float] = {}   # rig -> monotonic time of its last sample


def _dir(config) -> Path:
    return Path(getattr(config, "data_dir", "") or ".") / LOG_DIR


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def sample_period_s(config) -> float:
    try:
        return max(0.0, float(getattr(config, "cooler_history_sample_s", 300.0) or 0.0))
    except (TypeError, ValueError):
        return 0.0


def record(config, rig: str, camera: dict | None, *, focuser_temp=None,
           source: str = "", now: datetime | None = None,
           force: bool = False) -> dict | None:
    """Append one sample for `rig` from a ninaAPI camera info payload, at
    most once per cooler_history_sample_s (force skips the throttle). The
    sample, or None when skipped. Never raises."""
    try:
        period = sample_period_s(config)
        if period <= 0 or not camera:
            return None
        mono = time.monotonic()
        if not force and mono - _last.get(rig, -1e12) < period:
            return None
        temp = _num(camera.get("Temperature"))
        if temp is None:
            return None
        from photonscript.shared.rigs import rig_setpoint
        sp = _num(camera.get("TemperatureSetPoint"))
        now = now or datetime.utcnow()
        rec = {"t": now.isoformat(timespec="seconds") + "Z", "rig": rig,
               "temp": round(temp, 2),
               "setpoint": sp if sp is not None else rig_setpoint(config, rig),
               "power": (round(_num(camera.get("CoolerPower")), 1)
                         if _num(camera.get("CoolerPower")) is not None else None),
               "cooler_on": bool(camera.get("CoolerOn")),
               "amb": (round(_num(focuser_temp), 2)
                       if _num(focuser_temp) is not None else None),
               "src": source}
        d = _dir(config)
        d.mkdir(parents=True, exist_ok=True)
        with open(d / f"{rig}_{now:%Y-%m}.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        _last[rig] = mono
        return rec
    except Exception as e:  # noqa: BLE001 - history must never break a poll
        logger.debug("cooler history sample skipped: %s", e)
        return None


def load(config, rig: str, days: int | None = None,
         now: datetime | None = None) -> list[dict]:
    """Samples of `rig` from the last `days` (cooler_history_days), oldest
    first."""
    days = int(days if days is not None else getattr(config, "cooler_history_days", 10) or 10)
    now = now or datetime.utcnow()
    cutoff = (now - timedelta(days=days)).isoformat(timespec="seconds") + "Z"
    d = _dir(config)
    out: list[dict] = []
    months = {(now - timedelta(days=i)).strftime("%Y-%m") for i in range(days + 1)}
    for m in sorted(months):
        p = d / f"{rig}_{m}.jsonl"
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if str(r.get("t") or "") >= cutoff:
                out.append(r)
    out.sort(key=lambda r: r.get("t") or "")
    return out


def _hour(config, iso: str) -> int | None:
    try:
        from photonscript.shared.localtime import to_local
        t = datetime.fromisoformat(str(iso).rstrip("Z"))
        return to_local(config, t).hour if config is not None else t.hour
    except Exception:  # noqa: BLE001
        return None


def at_setpoint(s: dict, setpoint: float, tol: float) -> bool:
    t = _num(s.get("temp"))
    return bool(s.get("cooler_on")) and t is not None and abs(t - setpoint) <= tol


def saturated(s: dict, setpoint: float, tol: float) -> bool:
    t, p = _num(s.get("temp")), _num(s.get("power"))
    return (bool(s.get("cooler_on")) and t is not None and p is not None
            and p >= SATURATED_PCT and t > setpoint + tol)


def fit(samples: list[dict], setpoint: float, tol: float = 1.0,
        config=None) -> dict:
    """The reachability model from samples taken at this setpoint."""
    sp = float(setpoint)
    mine = [s for s in samples if _num(s.get("setpoint")) is None
            or abs(_num(s.get("setpoint")) - sp) < 0.5]
    pts = []
    for s in mine:
        a, p = _num(s.get("amb")), _num(s.get("power"))
        if at_setpoint(s, sp, tol) and a is not None and p is not None \
                and a - sp >= MIN_DELTA_C:
            pts.append((a - sp, p))
    k = None
    if len(pts) >= MIN_FIT_SAMPLES:
        den = sum(d * d for d, _p in pts)
        k = round(sum(d * p for d, p in pts) / den, 3) if den else None
    sat = [_num(s.get("amb")) for s in mine if saturated(s, sp, tol)
           and _num(s.get("amb")) is not None]
    hours: dict[int, dict] = {}
    for s in mine:
        h = _hour(config, s.get("t"))
        if h is None:
            continue
        b = hours.setdefault(h, {"amb": [], "on": 0, "at": 0, "power": [], "sat": 0})
        if _num(s.get("amb")) is not None:
            b["amb"].append(_num(s["amb"]))
        if s.get("cooler_on"):
            b["on"] += 1
            if at_setpoint(s, sp, tol):
                b["at"] += 1
                if _num(s.get("power")) is not None:
                    b["power"].append(_num(s["power"]))
            if saturated(s, sp, tol):
                b["sat"] += 1
    by_hour = {}
    for h, b in sorted(hours.items()):
        by_hour[h] = {
            "amb_c": round(statistics.median(b["amb"]), 1) if b["amb"] else None,
            "cooled": b["on"],
            "at_setpoint_frac": round(b["at"] / b["on"], 2) if b["on"] else None,
            "power_pct": (round(statistics.median(b["power"]), 0)
                          if b["power"] else None),
            "saturated": b["sat"]}
    return {"setpoint": sp, "tol": tol, "samples": len(mine),
            "fit_samples": len(pts), "k_pct_per_c": k,
            "saturated_samples": len(sat),
            "saturated_amb_min_c": round(min(sat), 1) if sat else None,
            "by_hour": by_hour}


def predict(model: dict, *, ambient: float | None = None, hour: int | None = None,
            max_power: float = 90.0) -> dict:
    """{"reachable": True / False / None, "power_pct", "ambient_c", "why"}.
    None = no history to judge by (callers then just try)."""
    sp = model.get("setpoint", 0.0)
    bucket = (model.get("by_hour") or {}).get(hour) if hour is not None else None
    amb = ambient
    src = "live ambient"
    if amb is None and bucket and bucket.get("amb_c") is not None:
        amb, src = bucket["amb_c"], f"typical ambient at {hour:02d}h"
    out = {"reachable": None, "power_pct": None, "ambient_c": amb, "why": ""}
    sat_min = model.get("saturated_amb_min_c")
    k = model.get("k_pct_per_c")
    if amb is not None and sat_min is not None and amb >= sat_min - 1.0:
        out.update(reachable=False,
                   why=(f"{src} {amb:g} C: the TEC ran flat out above "
                        f"{sp:g} C at {sat_min:g} C ambient before"))
        return out
    if amb is not None and k:
        p = round(k * max(0.0, amb - sp), 0)
        out["power_pct"] = p
        ok = p <= max_power
        out.update(reachable=ok,
                   why=(f"{src} {amb:g} C: predicted TEC {p:g} % at {sp:g} C "
                        f"({'within' if ok else 'over'} {max_power:g} %, "
                        f"k {k:g} %/C from {model.get('fit_samples')} samples)"))
        return out
    if bucket and bucket.get("at_setpoint_frac") is not None and bucket.get("cooled", 0) >= 3:
        ok = bucket["at_setpoint_frac"] >= AT_SETPOINT_FRAC
        out.update(reachable=ok, power_pct=bucket.get("power_pct"),
                   why=(f"history at {hour:02d}h: at {sp:g} C in "
                        f"{bucket['at_setpoint_frac'] * 100:.0f} % of cooled samples"))
        return out
    out["why"] = "no cooler history for this ambient / hour yet"
    return out


def reachability(config, rig: str, *, ambient: float | None = None,
                 when: datetime | None = None, samples: list | None = None) -> dict:
    """predict() for `rig` at its setpoint, `when` (UTC, default now) and
    an optional live ambient, plus the model it used."""
    from photonscript.shared.rigs import rig_setpoint
    sp = rig_setpoint(config, rig)
    tol = float(getattr(config, "calibration_temp_tol_c", 1.0) or 1.0)
    when = when or datetime.utcnow()
    if samples is None:
        samples = load(config, rig, now=when)
    model = fit(samples, sp, tol, config=config)
    hour = _hour(config, when.isoformat())
    res = predict(model, ambient=ambient, hour=hour,
                  max_power=float(getattr(config, "cooler_reach_max_power_pct", 90.0)))
    res["model"] = model
    res["hour"] = hour
    return res


def live_saturated(camera: dict | None, setpoint: float, tol: float) -> bool:
    """The camera reports the TEC flat out with the sensor above the
    setpoint right now."""
    if not camera:
        return False
    return saturated({"temp": camera.get("Temperature"),
                      "power": camera.get("CoolerPower"),
                      "cooler_on": camera.get("CoolerOn")}, setpoint, tol)
