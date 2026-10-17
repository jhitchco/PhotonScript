"""ProTrack re-judged once the RC16 mount tracks (2026-10-12).

The arm-time TheSky audit (thesky_audit, PS-104 / PS-138) runs by day with
the mount parked, and ProTrack (Activate ProTrack + Enable tracking
adjustments) is greyed in TheSky until its mount unparks and tracks, so the
audit's ProTrack row now says INFO "not judged while parked". This module
judges it once per night, as soon as the RC16's NINA reports the mount
unparked and tracking (nina_watchdog's "tracked tonight"), or at the RC16's
first light when the mount read is missing:

    recheck()   the ProTrack audit row only: the same read-only TheSky
                scripts the audit runs for it (ping, mount_flags,
                tpoint_flags; never Connect, never a write) + the manual
                record hint, evaluated by the audit's own row code
    store()     the night's audit history: the night audit
                (<data_dir>/thesky_audit/<night>.json, and latest.json when
                it is that night) gets the re-judged protrack_on row in place
                of the parked one (counts and fail_ids recomputed, so the
                Guiding tab, protrack_status and tracking_drift read it) and
                the recheck appended to "rechecks"; no audit that night = a
                one-row audit (reason "protrack-recheck")
    tick()      the nina watch loop's hook (nina_watchdog.run_watchdog):
                once per night (in memory, and on disk via "rechecks", so a
                restart does not run it again); on an unguided night ONE push
                when ProTrack reads OFF, else nothing

Off with thesky_audit_enabled = false. Observe only: never touches NINA, the
mount or TheSky's settings.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from photonscript.scheduler import thesky_audit as ta
from photonscript.scheduler.phd2_audit import FAIL, INFO, PASS, STATUS_RANK, UNKNOWN, WARN
from photonscript.shared import phd2_store as store

logger = logging.getLogger(__name__)

RIG = "rc16"
ROW_ID = "protrack_on"
LIVE = ("ARMED", "RUNNING", "PAUSED_UNSAFE", "PAUSED_OPERATOR", "WATCHING")
NIGHT_RUNNING = ("RUNNING", "WATCHING")
PUSH_TITLE = "PhotonScript ProTrack"
PUSH_TEXT = "ProTrack is off and the night is unguided: subs will trail"
# a running leaf that takes a light (lower case); "flat" / "dark" / "bias" excluded
LIGHT_WORDS = ("exposure", "light")
NOT_LIGHT = ("flat", "dark", "bias")


def _row(config) -> dict | None:
    desired = ta.load_desired(config)
    return next((r for r in desired.get("check") or []
                 if r.get("id") == ROW_ID or r.get("computed") == "protrack"), None)


def collect(config, client=None) -> tuple[dict, dict]:
    """The TheSky reads behind the ProTrack row only (read-only scripts,
    the audit's own client). Never raises."""
    try:
        cl = client or ta._client(config)
        if not cl.ping():
            return ({"thesky_scripting": False},
                    {"ok": False, "note": f"TheSky TCP {cl.host}:{cl.port} not reachable"})
        o: dict = {"thesky_scripting": True}
        errs = []
        for name, fn, conv in (("mount_flags", cl.mount_flags, ta.mount_observed),
                               ("tpoint_flags", cl.tpoint_flags, ta.tpoint_observed)):
            try:
                o.update(conv(fn() or {}))
            except Exception as e:  # noqa: BLE001
                errs.append(f"{name}: {e}")
        o = {k: v for k, v in o.items() if v is not None}
        return o, {"ok": True, "note": "; ".join(errs) or f"TheSky {cl.host}:{cl.port} read"}
    except Exception as e:  # noqa: BLE001
        return {}, {"ok": False, "note": f"{type(e).__name__}: {e}"}


def recheck(config, trigger: str, *, client=None, armer_state: str | None = None,
            persist: bool = True, now: datetime | None = None) -> dict | None:
    """Re-judge the ProTrack row now; returns the recheck record
    {t_utc, night, trigger, row} (None without the row). Never raises."""
    row = _row(config)
    if row is None:
        return None
    now = now or datetime.utcnow()
    obs: dict = {s: {} for s in ta.SOURCES}
    src: dict = {}
    obs["thesky-script"], src["thesky-script"] = collect(config, client)
    try:
        rec = ta.load_manual(config)
        obs["manual_record"] = rec or {}
        obs["manual"], src["manual"] = ta.manual_observed(rec, config)
    except Exception as e:  # noqa: BLE001
        src["manual"] = {"ok": False, "note": str(e)}
    obs["armer_state"] = armer_state
    obs["_sources"] = src
    out = ta.evaluate_row(row, obs, config)
    out["rechecked"] = {"t_utc": store.iso_z(now), "trigger": trigger}
    res = {"t_utc": store.iso_z(now), "night": store.night_of(config, now),
           "trigger": trigger, "armer_state": armer_state,
           "source": src.get("thesky-script"), "row": out}
    if persist:
        store_recheck(config, res)
    return res


def _counts(rows) -> dict:
    return {s: sum(1 for r in rows if r.get("status") == s)
            for s in (PASS, WARN, FAIL, UNKNOWN, INFO)}


def _apply(audit: dict, res: dict) -> dict:
    rows = [r for r in audit.get("rows") or [] if r.get("id") != ROW_ID]
    rows.append(res["row"])
    rows.sort(key=lambda r: STATUS_RANK.get(r.get("status"), 9))
    audit["rows"] = rows
    audit["counts"] = _counts(rows)
    audit["fail_ids"] = [r["id"] for r in rows if r.get("status") == FAIL]
    audit["rechecks"] = list(audit.get("rechecks") or []) + [
        {k: res.get(k) for k in ("t_utc", "trigger", "armer_state")}
        | {"status": res["row"].get("status"), "current": res["row"].get("current"),
           "protrack": res["row"].get("protrack"), "note": res["row"].get("note")}]
    return audit


def store_recheck(config, res: dict) -> None:
    """Into the night's audit (and latest.json when it is that night)."""
    night = res["night"]
    try:
        audit = ta.load_night(config, night)
        if not audit:
            audit = {"t_utc": res["t_utc"], "night": night, "reason": "protrack-recheck",
                     "armer_state": res.get("armer_state"),
                     "sources": {"thesky-script": res.get("source")}, "rows": []}
        audit = _apply(audit, res)
        store.write_json(ta.night_path(config, night), audit)
        latest = ta.load_latest(config)
        if not latest or latest.get("night") == night:
            store.write_json(ta.audit_dir(config) / "latest.json", audit)
    except OSError as e:
        logger.warning("ProTrack recheck not saved: %s", e)


def done_tonight(config, night: str) -> bool:
    """A recheck is already in the night's audit history."""
    a = ta.load_night(config, night) or {}
    return bool(a.get("rechecks"))


def first_light(reads: dict | None) -> bool:
    """NINA's running leaf takes a light (an exposure, not flat / dark / bias)."""
    leaf = str((reads or {}).get("seq_leaf") or "").lower()
    if not leaf or any(w in leaf for w in NOT_LIGHT):
        return False
    return any(w in leaf for w in LIGHT_WORDS) and bool((reads or {}).get("seq_exposure_s"))


def trigger_of(rig_state: dict | None, armer_state: str) -> str | None:
    """'tracking' once the RC16 mount was seen unparked and tracking tonight,
    'first light' when NINA takes the first RC16 light on a running night
    (the mount read missing), else None."""
    s = rig_state or {}
    if s.get("tracked"):
        return "tracking"
    if armer_state in NIGHT_RUNNING and first_light(s.get("reads")):
        return "first light"
    return None


def unguided(armer) -> bool:
    try:
        return not bool(armer._use_guiding())
    except Exception:  # noqa: BLE001
        return False


class Recheck:
    """Once per night latch (memory; the disk record covers a restart)."""

    def __init__(self):
        self.night: str | None = None
        self.done = False

    def roll(self, night: str) -> None:
        if night != self.night:
            self.night, self.done = night, False


STATE = Recheck()


async def tick(cfg, armer, monitor=None, *, now: datetime | None = None, run=None,
               notify=None, state: Recheck | None = None) -> dict | None:
    """The nina watch loop's hook. Returns the recheck record when one ran.
    run(cfg, trigger, armer_state=) and notify are injectable for tests."""
    if not getattr(cfg, "thesky_audit_enabled", True):
        return None
    armer_state = str(getattr(armer, "state", "") or "")
    if armer_state not in LIVE:
        return None
    state = state or STATE
    now = now or datetime.utcnow()
    night = store.night_of(cfg, now)
    state.roll(night)
    if state.done:
        return None
    if monitor is None:
        from photonscript.scheduler.nina_watchdog import MONITOR as monitor
    if monitor.night not in (None, night):
        return None                       # the watch has not rolled to tonight yet
    trig = trigger_of(monitor.rigs.get(RIG), armer_state)
    if trig is None:
        return None
    state.done = True
    if await asyncio.to_thread(done_tonight, cfg, night):
        return None
    run = run or recheck
    res = await asyncio.to_thread(lambda: run(cfg, trig, armer_state=armer_state))
    row = (res or {}).get("row") or {}
    logger.info("ProTrack recheck (%s): %s %s", trig, row.get("status"), row.get("current"))
    if row.get("protrack") == "off" and unguided(armer):
        if notify is None:
            from photonscript.shared.pushover import notify
        try:
            await notify(cfg, f"{PUSH_TEXT} (TheSky: {row.get('current') or 'OFF'}). "
                              f"{ta.PROTRACK_FIX}", title=PUSH_TITLE)
        except Exception as e:  # noqa: BLE001
            logger.warning("ProTrack recheck push failed: %s", e)
    return res
