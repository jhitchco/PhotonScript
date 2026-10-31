"""TheSky refraction temperature: keep it current from a live sensor.

Why (2026-10-28): ProTrack and TheSky's pointing corrections refract with
TheSky's site temperature. The site lines TheSky wrote into TPoint's input
files read 20 to 38 C (indoor / electronics), while AARO nights run ~0 to
15 C. Refraction scales with 1 / (273 + T): 25 C too warm under-corrects by
~8% (~7" at 30 deg alt). The pressure is right (873 hPa, audit row
site_pressure PASS), so only the temperature is synced.

What, while a night is armed (armer RUNNING or PAUSED_UNSAFE, between
pre-config / dusk and the dawn shutdown), every thesky_temp_sync_min
(default 45) minutes:

  1. The live temperature (live_temperature): NINA #1's focuser
     (ninaAPI GET equipment/focuser/info, Temperature), else NINA #2's.
     NaN, missing, a disconnected focuser and values outside -30..45 C
     are skipped. (The last FITS FOCTEMP is not used: a sub can be an hour
     old, and the focuser reads the same probe live.)
  2. TheSky's current value at the pinned DocumentProperty index
     (thesky_docprop_temperature). Unpinned (-1) = nothing is written and
     one line per night says "temperature sync skipped: docprop unpinned":
     the index is never guessed. The index must not be a known site
     property (latitude, longitude, ...), nor the pressure / refraction
     index, and what it reads now must look like a temperature (-40..60),
     else nothing is written (a wrong pin cannot overwrite the latitude).
  3. Skip when |live - TheSky| < THRESHOLD_C (2 C).
  4. Never while the mount slews: TheSky's IsSlewComplete (and NINA #1's
     mount Slewing when it reads) must say not slewing; unreadable = skip.
  5. One script: sky6StarChart.SetDocumentProperty(index, value), then the
     same index read back. The ONLY TheSky write PhotonScript makes outside
     tpoint-autocal; it lives here, not in thesky_client.READ_ONLY_JS, so
     the PS-104 read-only denylist over that module stays strict.

Each write (and a refusal or failure) is one kind "thesky_temp_sync" line in
runs/<night>_events.jsonl plus a log line. Kill switch: thesky_temp_sync
(PS_THESKY_TEMP_SYNC, default on).
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

KIND = "thesky_temp_sync"
THRESHOLD_C = 2.0            # skip a write smaller than this
PLAUSIBLE_C = (-30.0, 45.0)  # a sensor reading outside this is skipped
THESKY_TEMP_RANGE = (-40.0, 60.0)  # what the pinned index must read now
SYNC_STATES = ("RUNNING", "PAUSED_UNSAFE")

# module state (one armer per process): last attempt, the running task and
# the night the "unpinned" line was logged for
_LIVE: dict = {"last_try": None, "task": None, "unpinned_night": None,
               "last": None}


def reset() -> None:
    """Tests: forget the rate limit and the once-per-night lines."""
    _LIVE.update(last_try=None, task=None, unpinned_night=None, last=None)


def enabled(config) -> bool:
    return bool(getattr(config, "thesky_temp_sync", True))


def interval_min(config) -> float:
    try:
        v = float(getattr(config, "thesky_temp_sync_min", 45.0))
    except (TypeError, ValueError):
        v = 45.0
    return max(5.0, v)


def _num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def plausible(t) -> bool:
    x = _num(t)
    return x is not None and PLAUSIBLE_C[0] <= x <= PLAUSIBLE_C[1]


# --------------------------------------------------------------------------
# the live sensor (read only)
# --------------------------------------------------------------------------

def _http_get_json(url: str, timeout: float = 5.0):
    import httpx
    return httpx.get(url, timeout=timeout).json()


def _payload(d):
    if isinstance(d, dict) and isinstance(d.get("Response"), dict):
        return d["Response"]
    return d if isinstance(d, dict) else {}


def focuser_reading(info) -> float | None:
    """A ninaAPI focuser/info payload -> a plausible temperature or None
    (disconnected, NaN, missing, outside -30..45 C)."""
    p = _payload(info)
    if not p or p.get("Connected") is False:
        return None
    t = _num(p.get("Temperature"))
    return t if plausible(t) else None


def sources(config) -> list[tuple[str, str]]:
    """(label, ninaAPI base) in the order tried."""
    out = []
    for label, key, default in (
            ("NINA #1 focuser", "nina_base_url", "http://localhost:1888/v2/api"),
            ("NINA #2 focuser", "piggyback_nina_base_url", "http://localhost:1889/v2/api")):
        base = str(getattr(config, key, "") or default).rstrip("/")
        if base:
            out.append((label, base))
    return out


def live_temperature(config, getter=None) -> dict:
    """{"temp_c", "source"} from the first focuser that reads a plausible
    temperature, {"temp_c": None, "source": None, "tried": [...]} when none
    does. getter(url) -> JSON (tests); default httpx GET. Never raises."""
    get = getter or _http_get_json
    tried = []
    for label, base in sources(config):
        try:
            t = focuser_reading(get(base + "/equipment/focuser/info"))
        except Exception as e:  # noqa: BLE001
            tried.append(f"{label}: {type(e).__name__}")
            continue
        if t is not None:
            return {"temp_c": round(t, 2), "source": label}
        tried.append(f"{label}: no plausible reading")
    return {"temp_c": None, "source": None, "tried": tried}


# --------------------------------------------------------------------------
# the write script (the one TheSky write in this module)
# --------------------------------------------------------------------------

def write_script(index: int, value: float) -> str:
    """SetDocumentProperty(index, value), then DocumentProperty(index) read
    back as before= / after= / err=. Refuses an index that is a known site
    property or out of range and an implausible value."""
    from photonscript.telescope_agent.thesky_client import DOCPROP_KNOWN, _JS_HELPERS
    i = int(index)
    if not 0 <= i <= 500 or i in DOCPROP_KNOWN:
        raise ValueError(f"refusing DocumentProperty index {index!r}")
    v = _num(value)
    if v is None or not plausible(v):
        raise ValueError(f"refusing an implausible temperature {value!r}")
    rd = f"(sky6StarChart.DocumentProperty({i}), sky6StarChart.DocPropOut)"
    return (_JS_HELPERS
            + f"var before = g(function(){{return {rd};}});"
            + "var err = '';"
            + f"try {{ sky6StarChart.SetDocumentProperty({i}, {v:.2f}); }} "
              "catch (e) { err = s(e.message || e); }"
            + f"var after = g(function(){{return {rd};}});"
            + "Out = 'before=' + before + ';after=' + after + ';err=' + err;")


def _pin_problem(config) -> tuple[int | None, str | None]:
    """(index, None) when the temperature index may be written, else
    (index or None, why not)."""
    from photonscript.telescope_agent.thesky_client import DOCPROP_KNOWN
    try:
        i = int(getattr(config, "thesky_docprop_temperature", -1))
    except (TypeError, ValueError):
        return None, "thesky_docprop_temperature is not a number"
    if i < 0:
        return None, "unpinned"
    if i > 500 or i in DOCPROP_KNOWN:
        return i, f"index {i} is a known site property or out of range"
    for other in ("pressure", "refraction"):
        try:
            if int(getattr(config, f"thesky_docprop_{other}", -1)) == i:
                return i, f"index {i} is also thesky_docprop_{other}"
        except (TypeError, ValueError):
            pass
    return i, None


# --------------------------------------------------------------------------
# one sync (synchronous: run it in a thread)
# --------------------------------------------------------------------------

def _mount_slewing(config, client, getter=None) -> tuple[bool | None, str]:
    """(True / False / None unknown, why). TheSky's IsSlewComplete decides;
    NINA #1's mount Slewing, when it reads, can only add a "slewing"."""
    try:
        st = client.slew_state() or {}
    except Exception as e:  # noqa: BLE001
        return None, f"TheSky slew state not read ({type(e).__name__})"
    from photonscript.telescope_agent.thesky_client import _truthy
    conn = st.get("connected")
    if conn is None:
        return None, "TheSky slew state not read"
    if _truthy(conn):
        sc = st.get("slew_complete")
        if sc is None:
            return None, "TheSky IsSlewComplete not read"
        if not _truthy(sc):
            return True, "TheSky: slew in progress"
    try:
        get = getter or _http_get_json
        base = str(getattr(config, "nina_base_url", "") or "").rstrip("/")
        if base:
            m = _payload(get(base + "/equipment/mount/info"))
            if m.get("Slewing") is True:
                return True, "NINA #1: mount slewing"
    except Exception:  # noqa: BLE001
        pass
    return False, "not slewing"


def _event(config, now: datetime, value: str, detail: str, **extra) -> None:
    """One kind "thesky_temp_sync" line in runs/<night>_events.jsonl.
    Never raises."""
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
        append_jsonl(events_path(config, night_of(config, now)),
                     {"t": iso_z(now), "rig": "rc16", "src": "photonscript",
                      "kind": KIND, "value": value, "detail": detail, **extra})
    except Exception as e:  # noqa: BLE001
        logger.warning("TheSky temperature sync event (%s) not logged: %s", value, e)


def sync_once(config, now: datetime | None = None, *, client=None,
              getter=None, night: str | None = None) -> dict:
    """One attempt. Returns {"action": written | skipped | refused |
    failed, "why", ...}. Never raises."""
    now = now or datetime.utcnow()
    try:
        return _sync(config, now, client, getter, night)
    except Exception as e:  # noqa: BLE001
        logger.warning("TheSky temperature sync failed: %s", e)
        return {"action": "failed", "why": f"{type(e).__name__}: {e}"}


def _sync(config, now, client, getter, night) -> dict:
    if not enabled(config):
        return {"action": "skipped", "why": "thesky_temp_sync off"}
    idx, problem = _pin_problem(config)
    if problem == "unpinned":
        if night is None:
            from photonscript.shared.phd2_store import night_of
            night = night_of(config, now)
        if _LIVE.get("unpinned_night") != night:
            _LIVE["unpinned_night"] = night
            msg = "temperature sync skipped: docprop unpinned"
            logger.info("%s (set thesky_docprop_temperature from "
                        "photonscript thesky-audit --discover-docprops)", msg)
            _event(config, now, "unpinned", msg)
        return {"action": "skipped", "why": "unpinned"}
    if problem:
        logger.warning("TheSky temperature sync refused: %s", problem)
        _event(config, now, "refused", problem, index=idx)
        return {"action": "refused", "why": problem, "index": idx}

    live = live_temperature(config, getter)
    t = live.get("temp_c")
    if t is None:
        why = "no live temperature (" + "; ".join(live.get("tried") or []) + ")"
        logger.info("TheSky temperature sync skipped: %s", why)
        return {"action": "skipped", "why": why}

    if client is None:
        from photonscript.telescope_agent.thesky_client import client_from_config
        client = client_from_config(config)
    try:
        cur_raw = (client.docprops([idx]) or {}).get(idx)
    except Exception as e:  # noqa: BLE001
        why = f"TheSky not read ({type(e).__name__}: {e})"
        logger.info("TheSky temperature sync skipped: %s", why)
        return {"action": "skipped", "why": why, "live_c": t}
    cur = _num(cur_raw)
    if cur is None or not THESKY_TEMP_RANGE[0] <= cur <= THESKY_TEMP_RANGE[1]:
        why = (f"DocumentProperty({idx}) reads {cur_raw!r}, not a temperature: "
               "check thesky_docprop_temperature")
        logger.warning("TheSky temperature sync refused: %s", why)
        _event(config, now, "refused", why, index=idx, live_c=t, source=live["source"])
        return {"action": "refused", "why": why, "index": idx, "live_c": t}
    if abs(t - cur) < THRESHOLD_C:
        why = f"TheSky {cur:.1f} C vs {live['source']} {t:.1f} C: under {THRESHOLD_C:g} C"
        logger.info("TheSky temperature sync skipped: %s", why)
        return {"action": "skipped", "why": why, "thesky_c": cur, "live_c": t}

    slewing, swhy = _mount_slewing(config, client, getter)
    if slewing is not False:
        logger.info("TheSky temperature sync skipped: %s", swhy)
        return {"action": "skipped", "why": swhy, "thesky_c": cur, "live_c": t}

    try:
        from photonscript.telescope_agent.thesky_client import parse_kv
        rep = parse_kv(client.run_script(write_script(idx, t)))
    except Exception as e:  # noqa: BLE001
        why = f"{type(e).__name__}: {e}"
        logger.warning("TheSky temperature sync failed: %s", why)
        _event(config, now, "failed", why, index=idx, before_c=cur, live_c=t,
               source=live["source"])
        return {"action": "failed", "why": why, "thesky_c": cur, "live_c": t}
    after = _num(rep.get("after"))
    if rep.get("err") or after is None or abs(after - t) > 0.05:
        why = (f"SetDocumentProperty({idx}, {t:.2f}) "
               + (f"error {rep.get('err')}" if rep.get("err") else
                  f"read back {rep.get('after')!r}"))
        logger.warning("TheSky temperature sync failed: %s", why)
        _event(config, now, "failed", why, index=idx, before_c=cur, live_c=t,
               after_c=after, source=live["source"])
        return {"action": "failed", "why": why, "thesky_c": cur, "live_c": t,
                "after_c": after}
    detail = (f"TheSky refraction temperature {cur:.1f} -> {t:.1f} C "
              f"({live['source']}, DocumentProperty({idx}))")
    logger.info("TheSky temperature sync: %s", detail)
    _event(config, now, "written", detail, index=idx, before_c=cur, after_c=after,
           live_c=t, source=live["source"])
    res = {"action": "written", "why": detail, "thesky_c": cur, "live_c": t,
           "after_c": after, "index": idx, "source": live["source"]}
    _LIVE["last"] = dict(res, t=now.isoformat() + "Z")
    return res


# --------------------------------------------------------------------------
# the armer's hook
# --------------------------------------------------------------------------

def _parse(s) -> datetime | None:
    try:
        return datetime.fromisoformat(str(s).rstrip("Z")) if s else None
    except ValueError:
        return None


def in_window(armer, now: datetime) -> bool:
    """RUNNING / PAUSED_UNSAFE, at or after pre-config (or dusk) and before
    the dawn shutdown."""
    if str(getattr(armer, "state", "")) not in SYNC_STATES:
        return False
    plan = getattr(armer, "plan", None) or {}
    start = _parse(plan.get("preconfig_utc")) or _parse(plan.get("dusk_utc"))
    if start is None or now < start:
        return False
    end = None
    try:
        end = armer._shutdown_due_at()
    except Exception:  # noqa: BLE001
        dawn = _parse(plan.get("dawn_utc"))
        end = dawn + timedelta(minutes=30) if dawn else None
    return end is not None and now < end


def armer_tick(armer, now: datetime | None = None, *, client=None, getter=None):
    """The armer's hook (every tick while RUNNING / PAUSED_UNSAFE). Rate
    limited to thesky_temp_sync_min; the sync runs as a background task so
    the tick never waits on TheSky. Returns the task or None. Never raises."""
    try:
        cfg = armer.config
        now = now or datetime.utcnow()
        if not enabled(cfg) or not in_window(armer, now):
            return None
        last = _LIVE.get("last_try")
        if last is not None and 0 <= (now - last).total_seconds() < interval_min(cfg) * 60:
            return None
        prev = _LIVE.get("task")
        if prev is not None and not prev.done():
            return None
        _LIVE["last_try"] = now
        night = (getattr(armer, "plan", None) or {}).get("night_of")

        async def _run():
            try:
                return await asyncio.wait_for(asyncio.to_thread(
                    sync_once, cfg, now, client=client, getter=getter, night=night), 120)
            except Exception as e:  # noqa: BLE001
                logger.warning("TheSky temperature sync task failed: %s", e)
                return None
        _LIVE["task"] = asyncio.get_running_loop().create_task(_run())
        return _LIVE["task"]
    except Exception as e:  # noqa: BLE001
        logger.warning("TheSky temperature sync hook failed: %s", e)
        return None
