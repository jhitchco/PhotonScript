"""PS-238: a park must stay parked (the Bisque driver's UnparkOnConnect).

Why: the Software Bisque ASCOM telescope driver has "Unpark on connect"
ticked (HKLM ...\\ASCOM.SoftwareBisque.Telescope UnparkOnConnect, and
UnparkOnConnect=1 in TheSky's ImagingSystem.ini). Whatever connects the
mount through that driver unparks it and starts tracking. 2026-11-14: the
dawn shutdown parked at 12:53Z with tracking off; at 17:46:08Z (10:46 MST),
the minute the daily app start launched NINA #1 and ran the connect-all, the
mount ledger shows unpark + tracking on, and it tracked under a closed roof
all afternoon. 2026-11-02: parks were undone within 5 s by NINA #1's mount
auto-reconnect.

Jeremy unticks the box himself; PhotonScript only audits it (the settings
ledger rule thesky_unpark_on_connect, read only, never a registry write)
and keeps its own hands off a parked mount:

    connect guard      connect_skip_reason(): armer.connect_all (arm, restore,
                       POST /api/equipment/connect, the PS-255 pre-arm) and
                       the preflight skip connecting a DISCONNECTED mount,
                       result "skipped (mount parked by day)" or "skipped
                       (roof unsafe)", while TheSky reads it parked (the
                       read-only park_state script, never Connect; the PS-67
                       mount log's last line when TheSky cannot say) or NINA
                       #1's safety monitor reads UNSAFE, and no night runs
                       (armer not RUNNING / PAUSED_UNSAFE / PAUSED_OPERATOR /
                       WATCHING, no dispatch in flight). The night's own
                       sequence connects the mount in its start area and
                       unparks once safe: that connect is not ours and is
                       never skipped. mount_connect_guard=false turns it off.
    park sticks        armer._park_stick_check (after the dawn shutdown's
                       park, the PS-77 unsafe stop, the paused-unsafe park and
                       make-safe): IsParked re-read ~30 s later; unparked =
                       disconnect NINA #1's mount, park again (the existing
                       park path with the PS-253 verify), one P1 push.
    unsafe watch       unsafe_tick() (nina_watchdog loop, every 60 s, any
                       armer state without a night of its own): a SAFE ->
                       UNSAFE change while the mount reads unparked pages P1
                       once per transition (and not again within
                       UNSAFE_REPAGE_MIN). Push only, never parks.
    daytime watch      dawn_mount_check (PS-185) extended past noon: unparked
                       AND tracking by day with no armed night pages once a
                       day.

Read only here apart from the armer's own re-park: GETs to NINA #1 and the
read-only TheSky script.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# armer states that own the mount: their sequence connects / unparks it, and
# the armer (or NINA's own loop) answers an unsafe roof
NIGHT_STATES = ("RUNNING", "PAUSED_UNSAFE", "PAUSED_OPERATOR", "WATCHING")
REASON_PARKED = "mount parked by day"
REASON_UNSAFE = "roof unsafe"
SKIP_PREFIX = "skipped ("
UNSAFE_REPAGE_MIN = 30.0
FIX_UNPARK = ("NINA #1 > Equipment > Mount > Bisque driver settings (or TheSky "
              "Bisque TCS): untick Unpark on connect")


def guard_on(cfg) -> bool:
    return bool(getattr(cfg, "mount_connect_guard", True))


def skipped(reason: str) -> str:
    return f"{SKIP_PREFIX}{reason})"


def is_skip(text) -> bool:
    return str(text or "").startswith(SKIP_PREFIX)


def current_armer():
    """The service's armer, None outside the service (the CLI)."""
    app_mod = sys.modules.get("photonscript.scheduler.app")
    if app_mod is None:
        return None
    try:
        return app_mod.get_armer()
    except Exception:  # noqa: BLE001
        return None


def night_running(armer) -> str | None:
    """Why the armer owns the mount right now, None when it does not."""
    if armer is None:
        return None
    state = str(getattr(armer, "state", "") or "")
    if state in NIGHT_STATES:
        return f"armer {state}"
    try:
        f = getattr(armer, "dispatch_in_flight", None)
        if callable(f) and f():
            return "a dispatch is in flight"
    except Exception:  # noqa: BLE001
        pass
    return None


# ------------------------------------------------------------------ reads

def mount_log_parked(cfg) -> bool | None:
    """The parked flag of the newest real PS-67 mount-log line (tonight,
    else the previous night), None when there is none."""
    try:
        from photonscript.shared import mount_log
        from photonscript.shared.phd2_store import night_of
        now = datetime.utcnow()
        for when in (now, now - timedelta(days=1)):
            real = [r for r in mount_log.load(cfg, night_of(cfg, when))
                    if not mount_log.is_offline(r)]
            if real:
                p = real[-1].get("parked")
                return None if p is None else bool(p)
    except Exception as e:  # noqa: BLE001
        logger.debug("mount log park read failed: %s", e)
    return None


async def read_parked(cfg) -> tuple[bool | None, str]:
    """(parked, source): TheSky's IsParked (read-only park_state script,
    only when TheSky has the mount connected; never Connect), else the
    mount log's last known state. (None, "") when nothing can say."""
    from photonscript.scheduler import dawn_mount_check as dm
    try:
        ts = await asyncio.wait_for(asyncio.to_thread(dm.read_thesky, cfg), 10)
    except Exception:  # noqa: BLE001
        ts = None
    if ts and ts.get("parked") is not None:
        return bool(ts["parked"]), "TheSky"
    ml = await asyncio.to_thread(mount_log_parked, cfg)
    if ml is not None:
        return ml, "mount log"
    return None, ""


async def read_safe(cfg) -> bool | None:
    """NINA #1's safety monitor IsSafe (GET), None when unknown or the
    monitor is not connected. Never connects it."""
    from photonscript.scheduler.nina_watchdog import _api_get
    sm = await _api_get(str(getattr(cfg, "nina_base_url", "") or ""),
                        "/equipment/safetymonitor/info")
    if not sm or sm.get("Connected") is False or sm.get("IsSafe") is None:
        return None
    return bool(sm.get("IsSafe"))


# ------------------------------------------------------------------ connect guard

async def connect_skip_reason(cfg, armer=None, *, parked_fn=None,
                              safe_fn=None) -> str | None:
    """REASON_PARKED / REASON_UNSAFE when connecting the mount now would let
    the Bisque driver unpark it with no night to need it, else None. armer
    None = the service's (none in the CLI = no night). Fails open (None) on
    an unexpected error: the old connect-all behaviour. Never raises."""
    try:
        if not guard_on(cfg):
            return None
        if night_running(armer if armer is not None else current_armer()):
            return None
        parked, src = await (parked_fn or read_parked)(cfg)
        if parked is True:
            logger.info("PS-238: mount connect skipped: parked (%s)", src)
            return REASON_PARKED
        safe = await (safe_fn or read_safe)(cfg)
        if safe is False:
            logger.info("PS-238: mount connect skipped: safety monitor UNSAFE")
            return REASON_UNSAFE
    except Exception as e:  # noqa: BLE001
        logger.warning("PS-238 connect guard failed (connecting as before): %s", e)
    return None


# ------------------------------------------------------------------ unsafe watch

class UnsafeWatch:
    """The last safety reading and the last page (in memory; a restart only
    loses the baseline, so the first read after it never pages)."""

    def __init__(self):
        self.last_safe: bool | None = None
        self.last_page: datetime | None = None
        self.paged_episode = False


WATCH = UnsafeWatch()


def _local_hm(cfg, now: datetime) -> str:
    try:
        from photonscript.shared.localtime import to_local
        return to_local(cfg, now).strftime("%H:%M") + " local"
    except Exception:  # noqa: BLE001
        return now.strftime("%H:%MZ")


def unsafe_text(cfg, m: dict, armer_state: str, now: datetime) -> str:
    trk = (", TRACKING" if m.get("tracking") is True else
           ", not tracking" if m.get("tracking") is False else "")
    return (f"Roof went UNSAFE at {_local_hm(cfg, now)} while the mount is "
            f"UNPARKED{trk} ({m.get('source') or '?'}), armer "
            f"{armer_state or 'unknown'}: no night is running, so nothing in "
            "PhotonScript parks it. To fix: TheSky: Telescope > Park (if NINA "
            "#1 keeps unparking it: NINA #1 > Equipment > Mount > Disconnect "
            f"first, then park in TheSky). For good: {FIX_UNPARK}. "
            "PhotonScript did not park it (PS-238).")


async def unsafe_tick(cfg, armer_state: str, now: datetime | None = None,
                      safe_fn=None, mount_fn=None, notify=None,
                      watch: UnsafeWatch | None = None) -> dict:
    """One pass: P1 on a SAFE -> UNSAFE change while the mount is unparked.
    nina_watch_mode off = nothing, panel = recorded, not pushed. While the
    armer has a night (NIGHT_STATES) it owns the unsafe response; the
    baseline is dropped so the night's end never reads as a transition.
    Never raises, never moves anything."""
    from photonscript.scheduler import nina_watchdog as nw
    watch = watch or WATCH
    now = now or datetime.utcnow()
    try:
        m = nw.mode(cfg)
        if m == "off":
            return {"state": "off"}
        if armer_state in NIGHT_STATES:
            watch.last_safe = None
            return {"state": "skip", "why": f"armer {armer_state} owns the unsafe response"}
        safe = await (safe_fn or read_safe)(cfg)
        if safe is None:
            return {"state": "unknown", "why": "safety monitor not read"}
        prev, watch.last_safe = watch.last_safe, safe
        if safe:
            watch.paged_episode = False
            return {"state": "safe"}
        if prev is not True:
            return {"state": "unsafe", "why": "no SAFE -> UNSAFE change"}
        if mount_fn is None:
            from photonscript.scheduler.dawn_mount_check import read_mount
            mount_fn = read_mount
        mnt = await mount_fn(cfg) or {}
        if mnt.get("parked") is not False:
            return {"state": "unsafe", "why": "turned unsafe; mount parked or unknown",
                    "mount": mnt}
        if (watch.paged_episode or (watch.last_page is not None and now - watch.last_page
                                    < timedelta(minutes=UNSAFE_REPAGE_MIN))):
            return {"state": "held", "why": f"paged within {UNSAFE_REPAGE_MIN:g} min",
                    "mount": mnt}
        msg = unsafe_text(cfg, mnt, armer_state, now)
        watch.last_page, watch.paged_episode = now, True
        logger.warning(msg)
        if m == "alert":
            if notify is None:
                from photonscript.shared.pushover import notify as _n
                notify = _n
            try:
                await notify(cfg, msg, title="PhotonScript UNSAFE, mount unparked",
                             priority=1, kind="act", safety=True)
            except Exception as e:  # noqa: BLE001
                logger.warning("PS-238 unsafe page not sent: %s", e)
        else:
            from photonscript.shared.pushover import record
            record(cfg, msg, title="PhotonScript UNSAFE, mount unparked",
                   priority=1, reason="nina_watch_mode panel")
        return {"state": "alert", "why": msg, "mount": mnt}
    except Exception as e:  # noqa: BLE001
        logger.warning("PS-238 unsafe watch failed: %s", e)
        return {"state": "error", "why": str(e)}
