"""Auto-arm supervisor (v2): re-arm every night for hands-off multi-day runs.

v1 armed one night at a time and needed a manual re-arm each evening, so a
COMPLETE night just sat idle. That is exactly why 2026-08-07..09 imaged
nothing despite an open roof: the armer finished the night of 08-06 and
nobody re-armed it. This loop watches the armer and, whenever it is idle
(DISARMED / COMPLETE / ERROR) and the next night's pre-config time is near,
arms it automatically. Because arm() rebuilds the plan from the project store
every time (remainder-aware), re-arming IS the nightly replan — the campaign
brain already exists; this just keeps pulling the trigger.

Controls (config / .env):
  PS_AUTO_ARM_ENABLED            master switch (default False — opt in)
  PS_AUTO_ARM_LEAD_HOURS         how long before pre-config the arm window
                                 opens (default 3.0h — recent enough that the
                                 preflight it runs reflects real equipment state)
  PS_AUTO_ARM_REQUIRE_PREFLIGHT  hard-gate on preflight go=true (default False)
  PS_NOON_ARM_ENABLED            noon auto re-arm (default True, 2026-09-15):
                                 when the armer is idle at 12:00 local, arm
                                 tonight's plan right then. Doubles as cooler
                                 belt #2 — arm() forces cooler + dew OFF, so a
                                 missed dawn shutdown is corrected by noon.
  PS_NOON_ARM_GUIDED             noon arms guided (default True) or unguided
                                 (TPoint + ProTrack). PS_NOON_ARM_GUIDING is the
                                 unused legacy string (guided | unguided, alias
                                 encoders | default)

Safety model: by default it ARMS AND NOTIFIES even when preflight fails
(Jeremy's call). The AARO site roof controller closes on weather independently
of NINA, so the worst case from a bad preflight is wasted frames that QA
rejects — not a soaked rig. Flip PS_AUTO_ARM_REQUIRE_PREFLIGHT=true to have it
skip-and-notify instead. Manual disarm always wins: the loop never touches an
armer that is ARMED / RUNNING / PAUSED_UNSAFE.

PS-125 guard: before any automatic arm (evening window or noon re-arm) the
loop reads NINA's /sequence/state and tonight's events. A sideload loaded
tonight (PS-123), a RUNNING item on NINA #1, or an unreadable NINA #1 skips
the arm (one Pushover per night per reason). Every decision (armed / skipped,
why) is logged as kind "auto_arm" in runs/<night>_events.jsonl and shown in
the dashboard chip.

PS-131: the same guard covers the automatic dusk sky flats (they stop, load
and start their own sequence too); their decisions carry action "dusk_flats".
And the noon re-arm's cooler-off (cooler belt #2) runs even when the noon arm
is skipped: once per night, before the pre-cool time, logged as kind
"noon_cooler_off".
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

TICK_SECONDS = 300  # 5 min
TERMINAL_STATES = ("DISARMED", "COMPLETE", "ERROR")


def auto_arm_decision(*, enabled: bool, state: str, now: datetime,
                      preconfig_utc: str | None, night_of: str | None,
                      last_armed_night: str | None,
                      lead_hours: float,
                      noon_arm: bool = False,
                      local_hour: float | None = None,
                      window_arm: bool = True) -> tuple[bool, str]:
    """Pure, side-effect-free: should the loop arm right now?

    Returns (arm, reason). Kept separate from all the async plumbing so the
    gating logic can be unit-tested without asyncio / httpx / astropy.

    window_arm gates the classic evening window (auto_arm_enabled); noon_arm +
    local_hour gate the noon re-arm (noon_arm_enabled) — from 12:00 local an
    idle armer arms for the coming night without waiting for the window.
    """
    if not enabled:
        return False, "auto-arm disabled"
    if state not in TERMINAL_STATES:
        return False, f"armer busy ({state})"
    if not preconfig_utc:
        return False, "no plan"
    if night_of and night_of == last_armed_night:
        return False, f"already auto-armed {night_of}"
    preconfig = datetime.fromisoformat(preconfig_utc.rstrip("Z"))
    window_open = preconfig - timedelta(hours=lead_hours)
    if window_arm and now >= preconfig:
        # Past pre-config but still idle (e.g. a night nobody armed): arm so
        # the remaining dark is captured rather than skipped. The armer's
        # ARMED tick dispatches immediately when now >= preconfig.
        return True, "past pre-config — arming for the remaining night"
    if window_arm and window_open <= now < preconfig:
        return True, "within arm window"
    if (noon_arm and local_hour is not None and local_hour >= 12
            and now < preconfig):
        return True, "noon auto re-arm — armed early for tonight"
    if now < window_open:
        return False, f"too early (window opens {window_open:%Y-%m-%d %H:%MZ})"
    return False, "nightly window arming disabled"


def _fail_summary(preflight: dict) -> str:
    fails = [c["name"] for c in preflight.get("checks", [])
             if c.get("status") == "fail"]
    return ", ".join(fails) if fails else "unknown"


# ---------------------------------------------------------------------------
# PS-125: never auto-arm over a sideloaded or running NINA sequence
# ---------------------------------------------------------------------------
# arm() stops, loads and starts its own sequence on NINA. A PS-123 sideload
# (loaded by hand, not started) or anything NINA is running would be replaced.
# The guard runs before every automatic arm (evening window and noon re-arm);
# a manual Arm is never blocked (the dashboard confirms first instead).

SKIP_SIDELOAD = "sideload"
SKIP_NINA_RUNNING = "nina_running"
SKIP_NINA_UNREADABLE = "nina_unreadable"
SKIP_PREFLIGHT = "preflight"
EVENT_KIND = "auto_arm"

SKIP_TITLES = {
    SKIP_SIDELOAD: "a sideloaded sequence is loaded",
    SKIP_NINA_RUNNING: "NINA is running",
    SKIP_NINA_UNREADABLE: "NINA state unreadable",
    SKIP_PREFLIGHT: "preflight failed",
}


def _night_key(config, now: datetime) -> str:
    from photonscript.shared.phd2_store import night_of
    return night_of(config, now)


def sideload_tonight(config, now: datetime | None = None,
                     rig: str | None = None) -> dict | None:
    """The latest successful PS-123 sideload ("sideload" event, ok=true) that
    belongs to tonight, else None. Tonight = runs/<night>_events.jsonl for the
    current noon-to-noon night, plus a load made the same morning (06:00 local
    or later), which night_of() still files under the previous night. rig
    (PS-136) keeps only that rig's loads."""
    from photonscript.shared.localtime import to_local
    from photonscript.shared.night_events import events_path
    from photonscript.shared.phd2_store import parse_z, read_jsonl
    now = now or datetime.utcnow()
    night = datetime.strptime(_night_key(config, now), "%Y-%m-%d")
    cutoff = night + timedelta(hours=6)  # local
    best = None
    for n in (night - timedelta(days=1), night):
        for r in read_jsonl(events_path(config, n.strftime("%Y-%m-%d"))):
            if r.get("kind") != "sideload" or not r.get("ok"):
                continue
            if rig is not None and r.get("rig") != rig:
                continue
            t = parse_z(r.get("t"))
            if t is None or t > now or to_local(config, t) < cutoff:
                continue
            if best is None or t >= best[0]:
                best = (t, r)
    return dict(best[1]) if best else None


async def auto_arm_guard(config, now: datetime | None = None,
                         reader=None) -> tuple[str | None, str]:
    """(skip_kind, message) before an automatic arm; skip_kind None = clear.

    Skips when a sideload was loaded tonight, when NINA #1's sequence state
    cannot be read (never arm blind), or when NINA #1 has a RUNNING item.
    NINA #2 (when enabled) is read too and named in the message, but only
    NINA #1 blocks: the armer dispatches the RC16 night there.
    reader(base_url) -> (tree, error) defaults to sideload.read_sequence_state.
    """
    from photonscript.scheduler import sideload as sd
    from photonscript.shared.rigs import PIGGYBACK, RC16, rig_config, rig_ids
    now = now or datetime.utcnow()
    reader = reader or sd.read_sequence_state
    sl = sideload_tonight(config, now)
    if sl:
        return SKIP_SIDELOAD, (
            f"a sideloaded sequence is loaded in NINA ({sl.get('value')} on "
            f"{sl.get('rig')}, {str(sl.get('t', ''))[11:16]} UTC); arming "
            "would replace it")
    tree, err = await reader(rig_config(config, RC16).nina_base_url)
    if err:
        return SKIP_NINA_UNREADABLE, (
            f"NINA #1 sequence state unreadable ({err}); not arming blind")
    running = sd.nina_running(tree)
    if running:
        return SKIP_NINA_RUNNING, (
            "NINA #1 is running a sequence (" + ", ".join(running[:3]) + ")")
    note = ""
    if PIGGYBACK in rig_ids(config):
        t2, e2 = await reader(rig_config(config, PIGGYBACK).nina_base_url)
        r2 = [] if e2 else sd.nina_running(t2)
        note = (f"; NINA #2 unreadable ({e2})" if e2 else
                f"; NINA #2 running ({', '.join(r2[:3])})" if r2 else
                "; NINA #2 idle")
    return None, "NINA #1 idle, no sideload tonight" + note


def decisions_tonight(config, now: datetime | None = None) -> list[dict]:
    """Tonight's logged auto-arm decisions (oldest first)."""
    from photonscript.shared.night_events import events_path
    from photonscript.shared.phd2_store import read_jsonl
    now = now or datetime.utcnow()
    return [r for r in read_jsonl(events_path(config, _night_key(config, now)))
            if r.get("kind") == EVENT_KIND]


ACTION_ARM = "arm"
ACTION_FLATS = "dusk_flats"   # PS-131


def _action(r: dict) -> str:
    return r.get("action") or ACTION_ARM   # PS-125 lines carry no action


def log_decision(config, decision: str, skip: str | None, detail: str,
                 trigger: str = "", now: datetime | None = None,
                 notified: bool = False,
                 action: str = ACTION_ARM) -> dict | None:
    """Append one auto-arm decision (kind "auto_arm", value "armed" or
    "skipped") to tonight's events file. A skip identical to the last logged
    decision for the same action is not repeated (the loop re-checks every 5
    minutes). action "dusk_flats" (PS-131) marks the automatic dusk flats.
    Returns the line, or None when deduped."""
    from photonscript.shared.night_events import events_path
    from photonscript.shared.phd2_store import append_jsonl, iso_z
    now = now or datetime.utcnow()
    prior = [r for r in decisions_tonight(config, now) if _action(r) == action]
    if prior and decision == "skipped":
        last = prior[-1]
        if last.get("value") == "skipped" and last.get("skip") == skip:
            return None
    line = {"t": iso_z(now), "rig": "rc16", "src": "photonscript",
            "kind": EVENT_KIND, "value": decision, "skip": skip,
            "trigger": trigger, "detail": detail, "notified": bool(notified)}
    if action != ACTION_ARM:
        line["action"] = action
    append_jsonl(events_path(config, _night_key(config, now)), line)
    return line


def skip_notified_tonight(config, skip: str, now: datetime | None = None,
                          action: str = ACTION_ARM) -> bool:
    """True when tonight's events already carry a pushed skip of this kind
    for this action: one Pushover per night per reason, across restarts."""
    return any(r.get("value") == "skipped" and r.get("skip") == skip
               and r.get("notified") and _action(r) == action
               for r in decisions_tonight(config, now))


async def skip_and_notify(config, skip: str, detail: str, trigger: str,
                          notifier, now: datetime | None = None,
                          action: str = ACTION_ARM) -> dict | None:
    """Log a skip; push it once per night per reason (per action)."""
    now = now or datetime.utcnow()
    push = not skip_notified_tonight(config, skip, now, action)
    line = log_decision(config, "skipped", skip, detail, trigger, now,
                        notified=push, action=action)
    if line is not None and push:
        if action == ACTION_FLATS:
            msg = (f"Auto dusk flats skipped: {SKIP_TITLES.get(skip, skip)}. "
                   f"{detail}. Shoot flats by hand if you need them.")
            title = "PhotonScript auto flats skipped"
        else:
            msg = (f"Auto-arm skipped: {SKIP_TITLES.get(skip, skip)}. "
                   f"{detail}. A manual Arm still works.")
            title = "PhotonScript auto-arm skipped"
        await notifier(config, msg, title=title, priority=1)
    return line


# ---------------------------------------------------------------------------
# PS-131: the noon cooler-off runs even when the noon arm is skipped
# ---------------------------------------------------------------------------
# arm() forces cooler + dew OFF on every rig (cooler belt #2: a missed dawn
# shutdown is corrected by noon). When the PS-125 guard (or a required
# preflight) skips the noon arm, that step still runs: once per night, and
# never at or after the pre-cool time (dusk minus cool_lead_minutes), so it
# cannot fight a sequence that is cooling for tonight. A rig with a PS-113
# calibration capture job active is left alone (darks need the cooler).

COOLER_OFF_KIND = "noon_cooler_off"


def noon_cooler_off_tonight(config, now: datetime | None = None) -> dict | None:
    """Tonight's logged noon cooler-off, else None."""
    from photonscript.shared.night_events import events_path
    from photonscript.shared.phd2_store import read_jsonl
    now = now or datetime.utcnow()
    rows = [r for r in read_jsonl(events_path(config, _night_key(config, now)))
            if r.get("kind") == COOLER_OFF_KIND]
    return rows[-1] if rows else None


async def noon_cooler_off(config, armer, plan: dict, skip: str,
                          now: datetime | None = None) -> dict | None:
    """Force the coolers + dew heaters off on every rig after a skipped noon
    arm. Returns the logged line, or None when not run (switched off with
    cooler_off_until_precool, already done tonight, or past pre-cool)."""
    from photonscript.scheduler.calibration_capture import busy as cal_busy
    from photonscript.shared.night_events import events_path
    from photonscript.shared.phd2_store import append_jsonl, iso_z
    from photonscript.shared.rigs import rig_ids
    now = now or datetime.utcnow()
    if not getattr(config, "cooler_off_until_precool", True):
        return None
    if noon_cooler_off_tonight(config, now):
        return None
    dusk = plan.get("dusk_utc")
    if dusk:
        lead = int(getattr(config, "cool_lead_minutes", 30))
        precool = (datetime.fromisoformat(str(dusk).rstrip("Z"))
                   - timedelta(minutes=lead))
        if now >= precool:
            return None
    hold = {r: "calibration capture running" for r in rig_ids(config)
            if cal_busy(r)}
    res = await armer.cooler_dew_off(skip_rigs=hold)
    line = {"t": iso_z(now), "rig": "all", "src": "photonscript",
            "kind": COOLER_OFF_KIND, "value": "done", "skip": skip,
            "detail": "; ".join(f"{k}: {v}" for k, v in res.items())}
    append_jsonl(events_path(config, _night_key(config, now)), line)
    logger.info("noon cooler-off after skipped arm (%s): %s", skip,
                line["detail"])
    return line


def decision_chip(last: dict | None, offset_hours: float = 0.0) -> str:
    """Dashboard chip text for the last auto-arm decision tonight."""
    if not last:
        return "No auto-arm decision yet tonight"
    from photonscript.shared.phd2_store import parse_z
    t = parse_z(last.get("t"))
    when = (t + timedelta(hours=offset_hours)).strftime("%H:%M") if t else "?"
    if last.get("value") == "armed":
        return f"Auto-armed at {when} local ({last.get('trigger') or 'auto'})"
    reason = SKIP_TITLES.get(last.get("skip"), last.get("skip") or "?")
    if _action(last) == ACTION_FLATS:
        return (f"Auto dusk flats skipped at {when} local: {reason} "
                f"({last.get('detail', '')})")
    return f"Auto-arm skipped at {when} local: {reason} ({last.get('detail', '')})"


def next_actions(config, preconfig_utc: str | None, now: datetime | None = None,
                 armer_state: str = "DISARMED") -> dict:
    """What each automatic arm does next, for the dashboard checkboxes: the
    evening window opens at pre-config minus auto_arm_lead_hours, the noon
    re-arm fires at 12:00 local. UTC ISO times plus a short text."""
    from photonscript.shared.localtime import utc_offset_hours
    now = now or datetime.utcnow()
    off = utc_offset_hours(config, now)

    def hhmm(dt):
        return (dt + timedelta(hours=off)).strftime("%H:%M")

    lead = float(getattr(config, "auto_arm_lead_hours", 3.0))
    ev = {"enabled": bool(getattr(config, "auto_arm_enabled", False)),
          "lead_hours": lead, "window_open_utc": None,
          "preconfig_utc": preconfig_utc}
    if not ev["enabled"]:
        ev["next"] = "off"
    elif not preconfig_utc:
        ev["next"] = "no plan tonight"
    if preconfig_utc:
        wo = datetime.fromisoformat(preconfig_utc.rstrip("Z")) - timedelta(hours=lead)
        ev["window_open_utc"] = wo.isoformat() + "Z"
        if ev["enabled"]:
            if armer_state not in TERMINAL_STATES:
                ev["next"] = f"armer is {armer_state}: nothing to do tonight"
            elif now < wo:
                ev["next"] = f"next auto-arm window opens {hhmm(wo)} local"
            else:
                ev["next"] = (f"auto-arm window open since {hhmm(wo)} local "
                              "(checks every 5 min)")
    local = now + timedelta(hours=off)
    noon = local.replace(hour=12, minute=0, second=0, microsecond=0)
    if local >= noon:
        noon += timedelta(days=1)
    na = {"enabled": bool(getattr(config, "noon_arm_enabled", False)),
          "guided": bool(getattr(config, "noon_arm_guided", True)),
          "at_utc": (noon - timedelta(hours=off)).isoformat() + "Z"}
    na["next"] = (f"next noon re-arm {noon:%a} 12:00 local "
                  f"({'guided' if na['guided'] else 'unguided'})"
                  if na["enabled"] else "off")
    return {"auto_arm": ev, "noon_arm": na}


async def run_auto_arm_loop(config, get_armer, *, tick_seconds: int = TICK_SECONDS):
    """Background supervisor. Start once at app startup (guarded by the flag,
    but it also re-checks the flag every tick so it can be toggled live)."""
    from photonscript.scheduler.night_plan import build_night_plan
    from photonscript.scheduler.preflight import run_preflight
    from photonscript.shared.pushover import notify

    last_armed_night: str | None = None
    last_skip_night: str | None = None
    flats_state: dict = {}
    forecast_state: dict = {}

    async def _maybe_send_evening_forecast():
        """A few hours before sunset, push tonight's viewing outlook once:
        rating + usable dark hours, the astronomical-dark gate window, the best
        sky windows, and the moon. Independent of auto-arm (runs every tick)."""
        from datetime import timedelta
        from photonscript.scheduler import night_plan as _np
        from photonscript.scheduler.forecast import (
            get_forecast, format_evening_forecast)
        from photonscript.shared.localtime import utc_offset_hours

        if not getattr(config, "evening_forecast_enabled", True):
            return
        now = datetime.utcnow()
        tz = utc_offset_hours(config, now)
        local_now = now + timedelta(hours=tz)
        night = local_now.strftime("%Y-%m-%d")
        if forecast_state.get("night") == night:
            return
        obs = config.get_observatory()
        base = datetime(local_now.year, local_now.month, local_now.day)
        tw = _np.compute_night_times(obs, base)
        sunset = tw.get("sunset")
        if not sunset:
            return
        lead = float(getattr(config, "evening_forecast_lead_hours", 3.0))
        # Fire once inside [sunset - lead, sunset): early enough to plan the
        # night, and if the app started late we still catch it up to sunset.
        if not (sunset - timedelta(hours=lead) <= now < sunset):
            return
        try:
            forecast = await get_forecast(config)
        except Exception as e:  # noqa: BLE001 — never let a fetch blip loop-crash
            logger.warning("evening forecast fetch failed: %s", e)
            return
        nights = forecast.get("nights") or []
        tonight = next((n for n in nights if n.get("date") == night),
                       nights[0] if nights else None)
        astro_dusk = tw.get("astro_dusk")
        astro_dawn = tw.get("astro_dawn")
        to_local = lambda dt: dt + timedelta(hours=tz) if dt else None
        title, msg = format_evening_forecast(
            tonight, to_local(astro_dusk), to_local(astro_dawn),
            cross_check=forecast.get("cross_check"),
            stale=bool(forecast.get("stale")))
        await notify(config, msg, title=title)
        forecast_state["night"] = night
        logger.info("evening forecast pushed for %s", night)

    async def _maybe_dispatch_dusk_flats(armer):
        """The 'checkmark' (2026-09-09): if any filter's flats are stale as
        sunset approaches and the armer is idle, dispatch the dusk sky-flat
        run for JUST those filters, then hold arming until it finishes."""
        import json as _json
        from datetime import timedelta
        from photonscript.scheduler import night_plan as _np
        from photonscript.scheduler.calibration import (
            generate_dusk_flats_json, stale_flat_filters)

        if not getattr(config, "auto_dusk_flats", True):
            return
        if armer.state not in TERMINAL_STATES:
            return
        now = datetime.utcnow()
        night = now.strftime("%Y-%m-%d")
        if flats_state.get("night") == night:
            return
        obs = config.get_observatory()
        tw = _np.compute_night_times(
            obs, now.replace(hour=0, minute=0, second=0, microsecond=0))
        sunset = tw.get("sunset")
        if not sunset:
            return
        if not (sunset - timedelta(minutes=90) <= now
                <= sunset + timedelta(minutes=5)):
            return
        stale = stale_flat_filters(config)
        if not stale:
            flats_state["night"] = night  # all fresh - done for today
            return
        # PS-131: dispatch_raw stops, loads and starts its own sequence, so
        # the PS-125 guard applies (sideload tonight, NINA #1 running or
        # unreadable): skip, one push per night per reason, retry next tick.
        skip, why = await auto_arm_guard(config)
        if skip:
            await skip_and_notify(config, skip, why,
                                  f"auto dusk flats ({','.join(stale)})",
                                  notify, action=ACTION_FLATS)
            logger.info("auto-flats skipped (%s): %s", skip, why)
            return
        seq_text, start_local = generate_dusk_flats_json(
            config, only_filters=stale)
        ok = await armer.dispatch_raw(
            _json.loads(seq_text), f"auto dusk flats ({','.join(stale)})")
        if ok:
            flats_state["night"] = night
            flats_state["until"] = sunset + timedelta(
                minutes=15 + 8 * len(stale) + 10)
            await notify(
                config,
                f"Auto dusk flats dispatched for stale filters: "
                f"{', '.join(stale)} (starts {start_local} local). "
                "Auto-arm resumes when they finish.",
                title="PhotonScript auto flats")
            logger.info("auto-flats: dispatched for %s", stale)
    logger.info("Auto-arm loop started (enabled=%s)",
                getattr(config, "auto_arm_enabled", False))

    while True:
        try:
            # Evening forecast push runs independent of auto-arm state.
            try:
                await _maybe_send_evening_forecast()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.error("evening forecast error: %s", e)

            noon_enabled = bool(getattr(config, "noon_arm_enabled", False))
            if getattr(config, "auto_arm_enabled", False) or noon_enabled:
                armer = get_armer()
                plan = build_night_plan(config)
                if "error" in plan:
                    logger.warning("auto-arm: no plan (%s)", plan["error"])
                else:
                    await _maybe_dispatch_dusk_flats(armer)
                    from photonscript.shared.localtime import utc_offset_hours
                    now = datetime.utcnow()
                    local_hour = (now + timedelta(
                        hours=utc_offset_hours(config, now))).hour
                    arm_now, reason = auto_arm_decision(
                        enabled=True,
                        state=armer.state,
                        now=now,
                        preconfig_utc=plan.get("preconfig_utc"),
                        night_of=plan.get("night_of"),
                        last_armed_night=last_armed_night,
                        lead_hours=float(getattr(config, "auto_arm_lead_hours", 3.0)),
                        noon_arm=noon_enabled,
                        local_hour=local_hour,
                        window_arm=bool(getattr(config, "auto_arm_enabled", False)),
                    )
                    fu = flats_state.get("until")
                    if arm_now and fu and datetime.utcnow() < fu:
                        arm_now = False
                        reason = "holding for auto dusk flats to finish"
                    logger.debug("auto-arm tick: %s (%s)", arm_now, reason)
                    skip = None
                    if arm_now:
                        # PS-125: never over a sideload or a running NINA
                        skip, why = await auto_arm_guard(config)
                        if skip:
                            await skip_and_notify(config, skip, why, reason, notify)
                            logger.info("auto-arm skipped (%s): %s", skip, why)
                            if reason.startswith("noon"):
                                # PS-131: skip only the arm, not cooler belt #2
                                await noon_cooler_off(config, armer, plan, skip)
                    if arm_now and not skip:
                        night = plan["night_of"]
                        pf = await run_preflight(config)
                        require = bool(getattr(config, "auto_arm_require_preflight", False))
                        if require and not pf.get("go", False):
                            log_decision(config, "skipped", SKIP_PREFLIGHT,
                                         f"preflight go=false ({_fail_summary(pf)})",
                                         reason, notified=last_skip_night != night)
                            if reason.startswith("noon"):
                                await noon_cooler_off(config, armer, plan,
                                                      SKIP_PREFLIGHT)
                            if last_skip_night != night:
                                await notify(
                                    config,
                                    f"Auto-arm SKIPPED {night}: preflight go=false "
                                    f"({_fail_summary(pf)}). Fix it and it arms next tick.",
                                    title="PhotonScript auto-arm skipped", priority=1)
                                last_skip_night = night
                        else:
                            guiding = None
                            if reason.startswith("noon"):
                                # single checkbox: guided when set, else unguided
                                guiding = ("guided"
                                           if getattr(config, "noon_arm_guided",
                                                      True) else "unguided")
                            await armer.arm(guiding=guiding)  # sends its own ARMED Pushover
                            last_armed_night = night
                            log_decision(config, "armed", None,
                                         f"armer {armer.state}, preflight "
                                         f"go={bool(pf.get('go', False))}", reason)
                            if not pf.get("go", False):
                                await notify(
                                    config,
                                    f"Auto-armed {night} but preflight FAILED "
                                    f"({_fail_summary(pf)}) — imaging anyway per config. "
                                    f"Roof controller still closes on weather.",
                                    title="PhotonScript auto-arm warning", priority=1)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.error("auto-arm loop error: %s", e)
        await asyncio.sleep(tick_seconds)
