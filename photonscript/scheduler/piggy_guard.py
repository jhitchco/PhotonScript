"""Piggy guard: the Piggy-600 exposing lights while the RC16 mount is not
tracking.

2026-10-09: NINA #1 lost the mount about 03:08Z and the armer ended up
DISARMED, while NINA #2 kept taking Piggy-600 lights for 3.5 h of an
unknown field. The Piggy rides the RC16 mount (NINA #1 owns it, PS-25), so
a Piggy light is only worth anything while that mount tracks.

Every TICK (the PS-150 loop, after the NINA watchdog), when
piggyback_enabled and the sun is at or below nina_watch_sun_alt_deg:

  mount   the RC16 mount state now from the mount log
          (shared/mount_log.state_at): tracking | slewing | parked | idle
          (unparked, not tracking) | offline (the explicit marker the
          logger writes when NINA #1 loses the mount) | unknown (no line,
          or a tracking line older than mount_log.STALE_S: the poll
          stopped). Bad: parked, idle, offline, unknown.
  piggy   only while the mount is bad: NINA #2's running instruction
          (GET /sequence/state, read only) is a light exposure (an
          exposure / light leaf, not a flat, dark or bias).

Both for piggy_guard_grace_min (default 10): one Pushover per night saying
what NINA #2 is shooting (target, instruction, exposure) and what the
mount is doing, a run event kind "piggy_guard" (value "alert", then
"clear" when it ends), and a morning report line. Independent of NINA #1
being offline and of the armer (an unarmed night pages too).
piggy_guard_mode: alert (default) | panel (events and morning line, no
push) | off.

Observe only: this never stops NINA #2, moves or parks the mount.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime

logger = logging.getLogger(__name__)

MODES = ("alert", "panel", "off")
KIND = "piggy_guard"
TITLE = "PhotonScript Piggy guard"
BAD_MOUNT = ("parked", "idle", "offline", "unknown")
LIGHT_WORDS = ("exposure", "light")
NOT_LIGHT = ("flat", "dark", "bias")
MOUNT_WORDS = {"parked": "PARKED", "idle": "unparked but NOT TRACKING",
               "offline": "OFFLINE in NINA #1 (no position)",
               "unknown": "UNKNOWN (no fresh mount log line)"}


def mode(cfg) -> str:
    m = str(getattr(cfg, "piggy_guard_mode", "alert") or "alert").strip().lower()
    return m if m in MODES else "alert"


def grace_min(cfg) -> float:
    try:
        v = float(getattr(cfg, "piggy_guard_grace_min", 10.0))
    except (TypeError, ValueError):
        return 10.0
    return max(0.0, v) if math.isfinite(v) else 10.0


def is_light(leaf) -> bool:
    low = str(leaf or "").lower()
    return (any(w in low for w in LIGHT_WORDS)
            and not any(w in low for w in NOT_LIGHT))


def mount_now(cfg, now: datetime) -> tuple[str, dict | None]:
    """(state, the mount log line it comes from) for the RC16 mount now."""
    from photonscript.shared import mount_log
    from photonscript.shared.phd2_store import night_of
    try:
        lines = mount_log.load(cfg, night_of(cfg, now))
    except Exception as e:  # noqa: BLE001
        logger.debug("piggy guard: mount log unreadable: %s", e)
        lines = []
    return mount_log.state_at(lines, now)


async def piggy_now(cfg) -> dict | None:
    """{"leaf", "target", "exposure_s", "path"} of NINA #2's running sequence,
    None when NINA #2 does not answer or runs nothing (never raises)."""
    from photonscript.scheduler.nina_watchdog import rig_base
    try:
        from photonscript.scheduler.sideload import read_sequence_state
        from photonscript.scheduler.where_panel import sequence_position
        tree, _err = await asyncio.wait_for(
            read_sequence_state(rig_base(cfg, "piggyback")), 10)
    except Exception:  # noqa: BLE001
        return None
    if tree is None:
        return None
    pos = sequence_position(tree)
    if not pos.get("running"):
        return None
    return {k: pos.get(k) for k in ("leaf", "target", "exposure_s", "path")}


class PiggyGuard:
    """since: when the Piggy was first seen shooting lights with the mount
    bad (this spell); alerted: the night already paged."""

    def __init__(self):
        self.night: str | None = None
        self.since: datetime | None = None
        self.alerted = False
        self.open = False          # an alerted spell not cleared yet
        self.last: dict = {}

    def roll(self, night: str | None) -> None:
        if night and night != self.night:
            self.night, self.alerted = night, False
            self.since, self.open = None, False


MONITOR = PiggyGuard()


def _hm(cfg, t) -> str:
    from photonscript.scheduler.nina_watchdog import _local_hm
    return _local_hm(cfg, t)


def shooting_text(p: dict | None) -> str:
    """"M31: 'Take Exposure' 300 s" from piggy_now()."""
    p = p or {}
    bits = []
    if p.get("target"):
        bits.append(str(p["target"]))
    leaf = f"'{p.get('leaf') or 'an exposure'}'"
    if p.get("exposure_s"):
        leaf += f" {float(p['exposure_s']):g} s"
    bits.append(leaf)
    return ": ".join(bits)


def alert_text(cfg, mount: str, line: dict | None, piggy: dict | None,
               since: datetime, now: datetime) -> str:
    from photonscript.scheduler.nina_watchdog import rig_name
    mins = (now - since).total_seconds() / 60.0
    seen = ""
    if line and line.get("dt"):
        seen = f" (mount log {line.get('why') or 'line'} at {_hm(cfg, line['dt'])} local)"
    return (f"{rig_name(cfg, 'piggyback')} is taking LIGHTS while the RC16 mount is "
            f"{MOUNT_WORDS.get(mount, mount)}{seen}, for {mins:.0f} min since "
            f"{_hm(cfg, since)} local. NINA #2 is shooting {shooting_text(piggy)}: "
            "the Piggy rides the RC16 mount, so these subs are of an unknown or "
            "drifting field. To fix: look at NINA #1 and the mount on the scope "
            "PC (remote desktop); stop NINA #2's sequence if the mount cannot "
            "track again. PhotonScript changed nothing.")


def _event(cfg, value: str, detail: str, now: datetime, **extra) -> None:
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
        append_jsonl(events_path(cfg, night_of(cfg, now)),
                     {"t": iso_z(now), "rig": "piggyback", "src": "photonscript",
                      "kind": KIND, "value": value, "detail": detail, **extra})
    except Exception as e:  # noqa: BLE001
        logger.warning("piggy guard event (%s) not logged: %s", value, e)


async def tick(cfg, now: datetime | None = None, window: bool | None = None,
               mount=None, piggy=None, notify=None,
               monitor: PiggyGuard | None = None) -> dict:
    """One pass. mount(cfg, now) -> (state, line), piggy(cfg) -> piggy_now()
    and notify are injectable for tests. Returns {"state": off | idle | ok
    | watching | alert | alerted, "mount", "piggy"}."""
    from photonscript.scheduler.nina_watchdog import in_window
    from photonscript.shared.phd2_store import iso_z, night_of
    monitor = monitor or MONITOR
    now = now or datetime.utcnow()
    m = mode(cfg)
    if m == "off" or not getattr(cfg, "piggyback_enabled", False):
        monitor.since = None
        return {"state": "off"}
    monitor.roll(night_of(cfg, now))
    win = in_window(cfg, now) if window is None else window
    if not win:
        await _clear(cfg, monitor, now, "outside the night window")
        return {"state": "idle"}
    mount = mount or mount_now
    piggy = piggy or piggy_now
    mstate, line = mount(cfg, now)
    pig = None
    if mstate in BAD_MOUNT:
        pig = await piggy(cfg)
    shooting = bool(pig and is_light(pig.get("leaf")))
    monitor.last = {"t": iso_z(now), "mount": mstate, "piggy": pig}
    if not (mstate in BAD_MOUNT and shooting):
        await _clear(cfg, monitor, now,
                     "the mount tracks again" if mstate not in BAD_MOUNT
                     else "NINA #2 is not taking lights")
        return {"state": "ok", "mount": mstate, "piggy": pig}
    if monitor.since is None:
        monitor.since = now
    held = (now - monitor.since).total_seconds() / 60.0
    if monitor.alerted:
        return {"state": "alerted", "mount": mstate, "piggy": pig}
    if held < grace_min(cfg):
        return {"state": "watching", "mount": mstate, "piggy": pig}
    msg = alert_text(cfg, mstate, line, pig, monitor.since, now)
    _event(cfg, "alert", msg, now, mount=mstate, since=iso_z(monitor.since),
           target=(pig or {}).get("target"), leaf=(pig or {}).get("leaf"),
           exposure_s=(pig or {}).get("exposure_s"))
    monitor.alerted = monitor.open = True
    if m == "alert":
        await _push(cfg, msg, 1, notify)
    return {"state": "alert", "mount": mstate, "piggy": pig}


async def _clear(cfg, monitor: PiggyGuard, now: datetime, why: str) -> None:
    if monitor.open:
        _event(cfg, "clear", why, now)
        monitor.open = False
    monitor.since = None


async def _push(cfg, msg: str, priority: int, notify=None) -> None:
    if notify is None:
        from photonscript.shared.pushover import notify as _n
        notify = _n
    try:
        await notify(cfg, msg, title=TITLE, priority=priority)
    except Exception as e:  # noqa: BLE001
        logger.warning("piggy guard push failed: %s", e)


def morning_line(cfg, date: str) -> str | None:
    """"Piggy guard: NINA #2 took lights 03:20-06:10 local while the RC16
    mount was offline (M31: 'Take Exposure' 300 s)" from the night's run
    events, None when it never fired."""
    from photonscript.shared import phd2_store as store
    from photonscript.shared.night_events import events_path
    alert = clear = None
    for r in store.read_jsonl(events_path(cfg, date)):
        if r.get("kind") != KIND:
            continue
        if r.get("value") == "alert" and alert is None:
            alert = r
        elif r.get("value") == "clear" and alert is not None and clear is None:
            clear = r
    if alert is None:
        return None
    start = _hm(cfg, store.parse_z(alert.get("since") or alert.get("t")))
    end = _hm(cfg, store.parse_z(clear.get("t"))) if clear else "dawn"
    shoot = shooting_text({"target": alert.get("target"), "leaf": alert.get("leaf"),
                           "exposure_s": alert.get("exposure_s")})
    return (f"Piggy guard: NINA #2 took lights {start}-{end} local while the "
            f"RC16 mount was {alert.get('mount') or '?'} ({shoot})")
