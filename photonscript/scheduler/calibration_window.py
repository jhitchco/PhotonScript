"""PS-182: when can a rig shoot its darks (and bias) at the setpoint?

    windows(config, rig, minutes=0, now=None, ambient=None) -> dict
    defer(config, rig, darks, bias, reason, window) / deferred(config, rig)
    clear_deferred(config, rig)
    dawn_tick(config, armer_state_fn, now=None, **kw)   (async)

Darks need the roof closed and the sensor at the setpoint for hours. The
RC16 armer already fills its dark quota in unsafe (roof closed) night time
and the Piggy-600 companion while the roof is closed; that is opportunistic
(only on a cloudy night). The other windows are chosen here:

  now                 a daytime / evening capture job (armer idle): judged by
                      the live ambient (focuser temperature) or the history
  pre-config cool-down  dusk minus cool_lead_minutes to astro dusk: short,
                      good for a 50 x 0 s bias set (the night sequence shoots
                      it there when no usable bias exists, PS-182)
  night (roof closed) astro dusk to astro dawn, only while unsafe: filled by
                      the night quota automatically
  dawn after shutdown astro dawn + 30 min (after the dawn flats) to astro
                      dawn + calibration_dawn_window_min: the coolest hours
                      of the day, the roof closed, the armer COMPLETE

Each window carries the cooler_history prediction (reachable / power) and
the minutes it offers; "recommended" is the first schedulable window that
fits and is not predicted unreachable (now, then dawn). A capture job the
cooler could not serve (refused at start or stalled while cooling) is
saved as the rig's deferred plan for the dawn window: shown on the
completeness report, and started by dawn_tick when calibration_dawn_capture
is on (default off: Jeremy starts it).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

DEFERRED_NAME = "calibration_deferred.json"
DAWN_AFTER_FLATS_MIN = 30


def _night_times(config, now: datetime) -> dict:
    """Twilight crossings (UTC, naive) of the night that has not ended yet."""
    from photonscript.scheduler.night_plan import compute_night_times
    obs = config.get_observatory()
    best = None
    for back in (1, 0):
        d = (now - timedelta(days=back)).replace(hour=0, minute=0, second=0,
                                                 microsecond=0)
        try:
            tw = compute_night_times(obs, d)
        except Exception as e:  # noqa: BLE001
            logger.debug("night times failed: %s", e)
            continue
        end = tw.get("sunrise") or tw.get("astro_dawn")
        if end is not None and end > now:
            best = tw
            break
    return best or {}


def _iso(t: datetime | None) -> str | None:
    return t.isoformat(timespec="minutes") + "Z" if t else None


def windows(config, rig: str, *, minutes: float = 0.0, now: datetime | None = None,
            ambient: float | None = None, samples: list | None = None) -> dict:
    """The candidate windows for `rig` and the recommendation."""
    from photonscript.scheduler.cooler_history import load, reachability
    now = now or datetime.utcnow()
    tw = _night_times(config, now)
    if samples is None:
        samples = load(config, rig, now=now)
    cool_lead = int(getattr(config, "cool_lead_minutes", 30) or 30)
    dawn_len = int(getattr(config, "calibration_dawn_window_min", 150) or 150)
    out = []

    def add(name, start, end, *, schedulable, note, amb=None):
        if start is None or end is None or end <= now:
            return
        start = max(start, now)
        mid = start + (end - start) / 2
        pred = reachability(config, rig, ambient=amb, when=mid, samples=samples)
        pred.pop("model", None)
        avail = round((end - start).total_seconds() / 60.0)
        out.append({"name": name, "start": _iso(start), "end": _iso(end),
                    "minutes": avail, "fits": avail >= minutes,
                    "schedulable": schedulable, "note": note,
                    "reachable": pred["reachable"], "power_pct": pred["power_pct"],
                    "ambient_c": pred["ambient_c"], "why": pred["why"]})

    dusk, dawn = tw.get("astro_dusk"), tw.get("astro_dawn")
    sunset = tw.get("sunset")
    pre_start = (dusk - timedelta(minutes=cool_lead)) if dusk else None
    day_end = pre_start or sunset
    if day_end is None or now < day_end:
        add("now", now, day_end or now + timedelta(minutes=max(minutes, 60)),
            schedulable=True, amb=ambient,
            note="capture job now (armer idle, roof closed)")
    add("pre-config cool-down", pre_start, dusk, schedulable=False,
        note="night sequence start: bias set at the setpoint when none is usable")
    add("night (roof closed)", dusk, dawn, schedulable=False,
        note="filled by the night quota automatically, only while unsafe")
    if dawn is not None:
        add("dawn after shutdown", dawn + timedelta(minutes=DAWN_AFTER_FLATS_MIN),
            dawn + timedelta(minutes=dawn_len), schedulable=True,
            note="after the dawn flats: coolest hours, armer COMPLETE, roof closed")
    rec = None
    for w in out:
        if w["schedulable"] and w["fits"] and w["reachable"] is not False:
            rec = w["name"]
            break
    return {"rig": rig, "minutes_needed": round(float(minutes or 0)),
            "windows": out, "recommended": rec}


def dawn_window(config, rig: str, now: datetime | None = None) -> dict | None:
    return next((w for w in windows(config, rig, now=now)["windows"]
                 if w["name"] == "dawn after shutdown"), None)


# --- the deferred plan ------------------------------------------------------------

def _path(config) -> Path:
    return Path(getattr(config, "data_dir", "") or ".") / DEFERRED_NAME


def _load(config) -> dict:
    try:
        return json.loads(_path(config).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(config, data: dict) -> None:
    try:
        p = _path(config)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        logger.warning("deferred calibration plan not saved: %s", e)


def defer(config, rig: str, darks: list, bias: int, reason: str,
          window: dict | None = None) -> dict:
    """Save `rig`'s darks + bias plan for its dawn window (replaces an
    older one)."""
    data = _load(config)
    rec = {"rig": rig, "darks": [[float(e), int(n)] for e, n in darks or []],
           "bias": int(bias or 0), "reason": reason,
           "created": datetime.utcnow().isoformat(timespec="seconds") + "Z",
           "window": window, "attempted": None}
    data[rig] = rec
    _save(config, data)
    return rec


def deferred(config, rig: str | None = None):
    data = _load(config)
    return data.get(rig) if rig else data


def clear_deferred(config, rig: str) -> None:
    data = _load(config)
    if rig in data:
        data.pop(rig)
        _save(config, data)


def _in_window(w: dict | None, now: datetime) -> bool:
    if not w or not w.get("start") or not w.get("end"):
        return False
    try:
        s = datetime.fromisoformat(w["start"].rstrip("Z"))
        e = datetime.fromisoformat(w["end"].rstrip("Z"))
    except ValueError:
        return False
    return s <= now <= e


async def dawn_tick(config, armer_state_fn, now: datetime | None = None,
                    **kw) -> dict:
    """calibration_dawn_capture (default off): start a rig's deferred plan
    once its dawn window is open (the capture job's own guards still
    apply: armer idle, roof closed, NINA free). One attempt per plan."""
    if not getattr(config, "calibration_dawn_capture", False):
        return {"skipped": "calibration_dawn_capture is off"}
    from photonscript.scheduler import calibration_capture as cc
    now = now or datetime.utcnow()
    out = {}
    for rig, plan in (deferred(config) or {}).items():
        if not isinstance(plan, dict) or plan.get("attempted"):
            continue
        w = plan.get("window")
        if not w or str(w.get("end") or "") < now.isoformat(timespec="minutes") + "Z":
            w = dawn_window(config, rig, now=now)   # the stored dawn has passed
        if not _in_window(w, now):
            out[rig] = "waiting for the dawn window"
            continue
        if cc.busy(rig):
            out[rig] = "busy"
            continue
        data = _load(config)
        if rig in data:
            data[rig]["attempted"] = now.isoformat(timespec="seconds") + "Z"
            _save(config, data)
        ok, body = await cc.start_job(config, rig, armer_state_fn=armer_state_fn,
                                      darks=plan["darks"] or None,
                                      bias=plan["bias"], source="deferred: dawn",
                                      **kw)
        out[rig] = {"started": body.get("id")} if ok else body
        if ok:
            clear_deferred(config, rig)
    return out
