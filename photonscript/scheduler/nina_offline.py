"""NINA drops offline mid-recipe or mid-night: one page per outage.

2026-10-09 (an armed TPoint recipe night): NINA #1 went away about 02:42Z and
the PS-150 watchdog paged "NOT RUNNING" (02:46Z), then "OK again". At 03:07:58Z it
went away a second time (mount parked, NINA offline) and nothing paged:
PS-150 alerts once per rig and state per night. The armer ended up
DISARMED and the Piggy-600 kept imaging an unknown field for 3.5 h.

This watch runs in the same background loop as PS-150 (every 60 s, before
it), per rig (RC16, plus the Piggy-600 when piggyback_enabled):

  engaged   the armer is RUNNING or WATCHING, or tonight's arm recipe is
            dispatched / started, or was within ENGAGE_MEMORY_MIN when the
            outage began. An engaged outage stays engaged until NINA answers
            again, even when the armer later goes DISARMED / ERROR.
  down      GET <nina base>/version does not answer.
  page      an engaged outage lasting nina_offline_grace_min (default 3):
            one priority-1 Pushover per outage naming what was running (the
            armer state, the recipe, NINA's last running instruction, the
            mount as last seen). When NINA #1 is down and NINA #2 is still
            exposing, it says so: the mount is not tracking or unknown, so
            "the Piggy is imaging an unknown field".
  recovery  NINA answers again after a page: a recovery note (priority 0).

Both go to the run events (kind nina_offline, value down / up) for the
morning report. While an engaged outage is open, PS-150 keeps its chip and
events for that rig but leaves the push to this watch (owned()), so one
outage is one page. nina_offline_alert off: nothing here, PS-150 as before.

Observe only: this never starts, restarts, stops or commands NINA.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

ENGAGE_STATES = ("RUNNING", "WATCHING")
RECIPE_LIVE = ("dispatched", "started")
ENGAGE_MEMORY_MIN = 10.0   # an outage right after the armer left RUNNING
TITLE = "PhotonScript NINA offline"
IMAGING_WORDS = ("exposure", "light")


def enabled(cfg) -> bool:
    return bool(getattr(cfg, "nina_offline_alert", True))


def grace_min(cfg) -> float:
    try:
        v = float(getattr(cfg, "nina_offline_grace_min", 3.0))
    except (TypeError, ValueError):
        return 3.0
    return max(0.0, v) if math.isfinite(v) else 3.0


def recipe_live(armer) -> dict | None:
    """Tonight's arm recipe when it is dispatched or started, else None."""
    fn = getattr(armer, "_recipe_tonight", None)
    try:
        r = fn() if callable(fn) else getattr(armer, "recipe", None)
    except Exception:  # noqa: BLE001
        r = None
    if isinstance(r, dict) and r.get("status") in RECIPE_LIVE:
        return r
    return None


class OfflineWatch:
    """Per-rig outage state; one page per engaged outage."""

    def __init__(self):
        self.running_at: datetime | None = None
        self.context: dict = {}            # last "what was running" snapshot
        self.rigs: dict[str, dict] = {}

    def rig(self, rig: str) -> dict:
        return self.rigs.setdefault(rig, {
            "down_since": None, "engaged": False, "alerted": False,
            "context": None, "error": "", "recovered_at": None})

    def owned(self) -> set[str]:
        """Rigs whose push PS-150 should leave to this watch right now: an
        engaged outage is open, or it just recovered (this tick)."""
        return {r for r, s in self.rigs.items()
                if (s["down_since"] is not None and s["engaged"])
                or s.get("just_recovered")}


MONITOR = OfflineWatch()


# ------------------------------------------------------------------ texts

def _hm(cfg, t: datetime | None) -> str:
    from photonscript.scheduler.nina_watchdog import _local_hm
    return _local_hm(cfg, t)


def _imaging(leaf) -> bool:
    low = str(leaf or "").lower()
    return any(w in low for w in IMAGING_WORDS)


def _mount_words(ctx: dict) -> str:
    parked, tracking = ctx.get("mount_parked"), ctx.get("mount_tracking")
    if parked is True:
        return "parked"
    if tracking is True:
        return "tracking"
    if tracking is False:
        return "not tracking"
    return "unknown"


def running_text(ctx: dict) -> str:
    """'the armer was RUNNING with the TPoint recipe (dispatched); NINA was
    on 'Take Exposure''."""
    bits = [f"the armer was {ctx.get('armer') or '?'}"]
    if ctx.get("recipe"):
        bits[0] += (f" with the {ctx['recipe']} recipe "
                    f"({ctx.get('recipe_status') or '?'})")
    if ctx.get("leaf"):
        bits.append(f"NINA was on '{ctx['leaf']}'")
    elif ctx.get("leaf") == "":
        bits.append("NINA's sequence was idle")
    return "; ".join(bits)


def alert_text(cfg, rig: str, s: dict, now: datetime,
               piggy: dict | None = None) -> str:
    from photonscript.scheduler.nina_watchdog import rig_base, rig_name
    who = rig_name(cfg, rig)
    short = who.split(" (")[0]
    ctx = s.get("context") or {}
    mins = (now - s["down_since"]).total_seconds() / 60.0
    msg = (f"{who} OFFLINE: its API ({rig_base(cfg, rig)}) has not answered "
           f"for {mins:.0f} min (since {_hm(cfg, s['down_since'])} local; "
           f"{s.get('error') or 'no reply'}). When it went: "
           f"{running_text(ctx)}.")
    if rig == "rc16":
        msg += f" Mount last seen {_mount_words(ctx)}."
    if piggy and piggy.get("imaging"):
        leaf = piggy.get("leaf") or "an exposure"
        msg += (f" {rig_name(cfg, 'piggyback')} is still imaging ('{leaf}') "
                "while the mount is unknown (last seen "
                f"{_mount_words(ctx)}): the Piggy is imaging an unknown field.")
    msg += (f" To fix: look at {short} on the scope PC (remote desktop), "
            "restart it if it is gone, reconnect the equipment, then re-arm. "
            "PhotonScript restarts nothing.")
    return msg


def recovery_text(cfg, rig: str, s: dict, now: datetime) -> str:
    from photonscript.scheduler.nina_watchdog import rig_name
    mins = (now - s["down_since"]).total_seconds() / 60.0
    return (f"{rig_name(cfg, rig)} answers again after {mins:.0f} min offline "
            f"(since {_hm(cfg, s['down_since'])} local). Check that tonight's "
            "sequence is running (re-arm if the armer is not RUNNING).")


def _event(cfg, rig: str, value: str, detail: str, now: datetime, **extra) -> None:
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
        append_jsonl(events_path(cfg, night_of(cfg, now)),
                     {"t": iso_z(now), "rig": rig, "src": "photonscript",
                      "kind": "nina_offline", "value": value, "detail": detail,
                      **extra})
    except Exception as e:  # noqa: BLE001
        logger.warning("nina offline event (%s) not logged: %s", value, e)


# ------------------------------------------------------------------ tick

def _ps150_reads(rig: str) -> dict:
    """PS-150's last reads of a rig (its running leaf, the RC16 mount)."""
    try:
        from photonscript.scheduler.nina_watchdog import MONITOR as W
        return dict((W.rigs.get(rig) or {}).get("reads") or {})
    except Exception:  # noqa: BLE001
        return {}


async def tick(cfg, armer_state: str, recipe: dict | None = None,
               now: datetime | None = None, api=None, leaf=None, notify=None,
               monitor: OfflineWatch | None = None, last_reads=None) -> dict:
    """One pass. api(base) -> (answered, error), leaf(base) -> (name,
    exposure_s), last_reads(rig) -> PS-150's last reads, notify: injectable
    for tests. Returns {rig: "up" | "down" | "alert" | "recovered" | "off"}."""
    from photonscript.scheduler import nina_watchdog as nw
    from photonscript.shared.rigs import rig_ids
    monitor = monitor or MONITOR
    now = now or datetime.utcnow()
    api = api or nw.api_version
    leaf = leaf or nw.running_leaf
    last_reads = last_reads or _ps150_reads
    out: dict = {}
    for s in monitor.rigs.values():
        s["just_recovered"] = False
    if not enabled(cfg):
        for s in monitor.rigs.values():
            s.update(down_since=None, engaged=False, alerted=False)
        return {r: "off" for r in rig_ids(cfg)}
    state = str(armer_state or "")
    if state in ENGAGE_STATES or recipe:
        monitor.running_at = now
    recent = (monitor.running_at is not None
              and now - monitor.running_at <= timedelta(minutes=ENGAGE_MEMORY_MIN))
    grace = grace_min(cfg)
    rigs = rig_ids(cfg)
    up: dict[str, bool] = {}
    for rig in rigs:
        ok, err = await api(nw.rig_base(cfg, rig))
        up[rig] = bool(ok)
        s = monitor.rig(rig)
        if ok:
            if s["down_since"] is not None and s["alerted"]:
                msg = recovery_text(cfg, rig, s, now)
                _event(cfg, rig, "up", msg, now)
                await _push(cfg, msg, 0, notify)
                s["just_recovered"] = True
                out[rig] = "recovered"
            else:
                out[rig] = "up"
            s.update(down_since=None, engaged=False, alerted=False, error="")
            reads = last_reads(rig)
            s["context"] = {
                "armer": state, "leaf": reads.get("seq_leaf"),
                "recipe": (recipe or {}).get("label") or (recipe or {}).get("id"),
                "recipe_status": (recipe or {}).get("status"),
                "mount_parked": reads.get("mount_parked"),
                "mount_tracking": reads.get("mount_tracking")}
            continue
        s["error"] = err
        if s["down_since"] is None:
            s["down_since"] = now
            s["engaged"] = bool(recent)
            ctx = dict(s.get("context") or {})
            if state in ENGAGE_STATES or recipe or not ctx.get("armer"):
                ctx["armer"] = state or ctx.get("armer")
            if recipe:
                ctx["recipe"] = recipe.get("label") or recipe.get("id")
                ctx["recipe_status"] = recipe.get("status")
            s["context"] = ctx
        out[rig] = "down"
        if (s["engaged"] and not s["alerted"]
                and (now - s["down_since"]).total_seconds() >= grace * 60.0):
            piggy = None
            if rig == "rc16" and "piggyback" in rigs:
                piggy = await _piggy(cfg, api, leaf, up)
            msg = alert_text(cfg, rig, s, now, piggy)
            _event(cfg, rig, "down", msg, now, error=err,
                   since=s["down_since"].replace(microsecond=0).isoformat() + "Z",
                   piggy_imaging=bool(piggy and piggy.get("imaging")))
            await _push(cfg, msg, 1, notify)
            s["alerted"] = True
            out[rig] = "alert"
    return out


async def _piggy(cfg, api, leaf, up: dict) -> dict:
    """{"imaging", "leaf"} for NINA #2 right now (never raises)."""
    from photonscript.scheduler import nina_watchdog as nw
    base = nw.rig_base(cfg, "piggyback")
    try:
        ok = up.get("piggyback")
        if ok is None:
            ok, _err = await api(base)
        if not ok:
            return {"imaging": False, "leaf": None}
        name, _exp = await leaf(base)
    except Exception:  # noqa: BLE001
        return {"imaging": False, "leaf": None}
    return {"imaging": _imaging(name), "leaf": name}


async def _push(cfg, msg: str, priority: int, notify=None) -> None:
    if notify is None:
        from photonscript.shared.pushover import notify as _n
        notify = _n
    try:
        await notify(cfg, msg, title=TITLE, priority=priority)
    except Exception as e:  # noqa: BLE001
        logger.warning("nina offline push failed: %s", e)
