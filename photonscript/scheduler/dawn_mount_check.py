"""PS-185: page when the mount is left unparked and tracking after dawn.

2026-10-09 was a DISARMED night (a manual TPoint run). Nothing parked the
mount: at 06:04 local it was still unparked and tracking (az 354, alt 62)
and TheSky then said "The mount cannot slew" until the Bisque control
software was restarted. The armer's dawn shutdown only runs for an armed
night, so nothing looked.

Every TICK_S, whatever the armer state, from tonight's astro dawn +
dawn_mount_check_after_dawn_min to the noon roll-over of the night:

  skip       the armer has a night (ARMED, RUNNING, a pause, WATCHING: its
             own dawn shutdown parks), or NINA #1 is running a sequence
             (dawn flats, a manual run): checked again next tick
  mount      TheSky's read-only TCP script (sky6RASCOMTele.IsParked(),
             IsTracking, alt / az; never Connect), else NINA #1's
             GET /equipment/mount/info (AtPark, TrackingEnabled)
  page       unparked AND tracking: one Pushover per night, priority 1,
             with the fix, plus a "dawn_mount" event in
             runs/<night>_events.jsonl (the latch: a PhotonScript restart
             does not page again)

dawn_mount_check_mode: alert (default) | off. Alert only: PhotonScript
never parks, stops tracking or restarts anything here.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

MODES = ("alert", "off")
TICK_S = 120
# the armer states whose own dawn shutdown handles the park
LIVE = ("ARMED", "RUNNING", "PAUSED_UNSAFE", "PAUSED_OPERATOR", "WATCHING")
KIND = "dawn_mount"
FIX = ("TheSky: Telescope > Park. If TheSky says 'The mount cannot slew', "
       "restart the Bisque control software (TheSky), reconnect the mount, "
       "then park.")


def mode(cfg) -> str:
    m = str(getattr(cfg, "dawn_mount_check_mode", "alert") or "alert").strip().lower()
    return m if m in MODES else "alert"


def after_dawn_min(cfg) -> float:
    try:
        v = float(getattr(cfg, "dawn_mount_check_after_dawn_min", 20.0))
        return max(0.0, v) if math.isfinite(v) else 20.0
    except (TypeError, ValueError):
        return 20.0


def dawn_of(cfg, now: datetime) -> tuple[str | None, datetime | None]:
    """(night, astro dawn UTC) of the noon-to-noon night `now` is in."""
    from photonscript.scheduler.armer import watch_plan
    plan = watch_plan(cfg, now)
    if "error" in plan or not plan.get("dawn_utc"):
        return plan.get("night_of"), None
    return plan["night_of"], datetime.fromisoformat(plan["dawn_utc"].rstrip("Z"))


# ------------------------------------------------------------------ reads

def _b(v):
    """True / False from a TheSky or ninaAPI flag, None when unknown."""
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "-1", "yes")


def _n(v):
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def read_thesky(cfg) -> dict | None:
    """{"parked", "tracking", "alt", "az", "source"} from TheSky's read-only
    park_state script, None when TheSky does not answer or its mount is not
    connected."""
    from photonscript.telescope_agent.thesky_client import TheSkyClient, _truthy
    try:
        cl = TheSkyClient(getattr(cfg, "thesky_tcp_host", "localhost"),
                          int(getattr(cfg, "thesky_tcp_port", 3040) or 3040),
                          timeout=5.0)
        kv = cl.park_state()
    except Exception as e:  # noqa: BLE001
        logger.debug("dawn mount check: TheSky read failed: %s", e)
        return None
    if not isinstance(kv, dict) or not _truthy(kv.get("connected")):
        return None
    return {"parked": _b(kv.get("parked")), "tracking": _b(kv.get("tracking")),
            "alt": _n(kv.get("alt")), "az": _n(kv.get("az")), "source": "TheSky"}


async def read_nina_mount(cfg) -> dict | None:
    """The same from NINA #1's mount info (GET), None when unknown."""
    from photonscript.scheduler.nina_watchdog import _api_get
    m = await _api_get(str(getattr(cfg, "nina_base_url", "") or ""),
                       "/equipment/mount/info")
    if not m or m.get("Connected") is False:
        return None
    return {"parked": _b(m.get("AtPark")),
            "tracking": _b(m.get("TrackingEnabled", m.get("Tracking"))),
            "alt": _n(m.get("Altitude")), "az": _n(m.get("Azimuth")),
            "source": "NINA #1"}


async def read_mount(cfg) -> dict | None:
    ts = await asyncio.to_thread(read_thesky, cfg)
    if ts and ts.get("parked") is not None and ts.get("tracking") is not None:
        return ts
    return await read_nina_mount(cfg) or ts


async def nina_busy(cfg) -> list[str] | None:
    """NINA #1's RUNNING items ([] idle), None when it cannot be read."""
    from photonscript.scheduler import sideload as sd
    try:
        tree, err = await asyncio.wait_for(
            sd.read_sequence_state(cfg.nina_base_url), 15)
    except Exception:  # noqa: BLE001
        return None
    return None if err else sd.nina_running(tree)


# ------------------------------------------------------------------ latch

def already_paged(cfg, night: str) -> bool:
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import read_jsonl
        return any(r.get("kind") == KIND and r.get("value") == "alert"
                   for r in read_jsonl(events_path(cfg, night)))
    except Exception:  # noqa: BLE001
        return False


def _event(cfg, night: str, value: str, detail: str, now: datetime, **extra) -> None:
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl, iso_z
        append_jsonl(events_path(cfg, night),
                     {"t": iso_z(now), "rig": "rc16", "src": "photonscript",
                      "kind": KIND, "value": value, "detail": detail, **extra})
    except Exception as e:  # noqa: BLE001
        logger.warning("dawn mount event not logged: %s", e)


def alert_text(cfg, m: dict, dawn: datetime, now: datetime, busy) -> str:
    from photonscript.shared.localtime import to_local
    try:
        hm = to_local(cfg, now).strftime("%H:%M")
        dhm = to_local(cfg, dawn).strftime("%H:%M")
    except Exception:  # noqa: BLE001
        hm, dhm = now.strftime("%H:%MZ"), dawn.strftime("%H:%MZ")
    where = (f" (az {m['az']:g}, alt {m['alt']:g})"
             if m.get("az") is not None and m.get("alt") is not None else "")
    nina = (" NINA #1 could not be read." if busy is None
            else " NINA #1 runs no sequence.")
    return (f"Mount left UNPARKED and TRACKING at {hm} local, after astro dawn "
            f"({dhm}){where}, with no armed night ({m.get('source')})."
            f"{nina} To fix: {FIX} PhotonScript did not park it "
            "(dawn_mount_check_mode=alert).")


# ------------------------------------------------------------------ tick

async def tick(cfg, armer_state: str, now: datetime | None = None,
               read=None, busy_fn=None, notify=None, state: dict | None = None) -> dict:
    """One check. read(cfg) -> mount dict, busy_fn(cfg) -> running items
    and notify are injectable for tests; state is the in-memory latch
    ({"paged": set of nights}). Returns {"state", "why", ...}."""
    now = now or datetime.utcnow()
    state = state if state is not None else _STATE
    if mode(cfg) == "off":
        return {"state": "off", "why": "dawn_mount_check_mode off"}
    night, dawn = dawn_of(cfg, now)
    if dawn is None:
        return {"state": "idle", "why": "no astro dawn computed"}
    if now < dawn + timedelta(minutes=after_dawn_min(cfg)):
        return {"state": "idle", "why": "before astro dawn + "
                f"{after_dawn_min(cfg):g} min", "night": night}
    if night in state.setdefault("paged", set()) or already_paged(cfg, night):
        state["paged"].add(night)
        return {"state": "paged", "why": "already paged tonight", "night": night}
    if armer_state in LIVE:
        return {"state": "skip", "why": f"armer {armer_state}: its dawn "
                "shutdown parks", "night": night}
    busy = await (busy_fn or nina_busy)(cfg)
    if busy:
        return {"state": "skip", "why": "NINA #1 is running " + busy[-1],
                "night": night}
    m = await (read or read_mount)(cfg)
    if not m:
        return {"state": "unknown", "why": "neither TheSky nor NINA reads the mount",
                "night": night}
    if m.get("parked") is False and m.get("tracking") is True:
        msg = alert_text(cfg, m, dawn, now, busy)
        state["paged"].add(night)
        _event(cfg, night, "alert", msg, now, source=m.get("source"),
               alt=m.get("alt"), az=m.get("az"), armer=armer_state)
        if notify is None:
            from photonscript.shared.pushover import notify as _notify
            notify = _notify
        try:
            await notify(cfg, msg, title="PhotonScript mount after dawn",
                         priority=1, kind="act", safety=True)
        except Exception as e:  # noqa: BLE001
            logger.warning("dawn mount page not sent: %s", e)
        logger.warning(msg)
        return {"state": "alert", "why": msg, "night": night, "mount": m}
    return {"state": "ok", "why": ("parked" if m.get("parked") else
                                   "not tracking" if m.get("tracking") is False
                                   else "unknown park / tracking"),
            "night": night, "mount": m}


_STATE: dict = {"paged": set()}


async def run_dawn_mount_check(get_config, get_armer, tick_seconds: int = TICK_S) -> None:
    """Background loop (app startup). Never raises, never moves the mount."""
    while True:
        try:
            await tick(get_config(), str(getattr(get_armer(), "state", "") or ""))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("dawn mount check failed: %s", e)
        await asyncio.sleep(tick_seconds)
