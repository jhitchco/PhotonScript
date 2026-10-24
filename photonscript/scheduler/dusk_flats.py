"""Automatic dusk sky flats, both rigs (2026-10-21).

The 2026-09-09 "checkmark" dispatched dusk flats for stale RC16 filters, but
it never ran in practice (RC16 L/R/G/B flats reached 109 days):

  * it lived inside the auto-arm block of run_auto_arm_loop, so with
    auto_arm_enabled and noon_arm_enabled both off (TPoint nights, and the
    scope's settings since 2026-10-20) it was never called;
  * it required an IDLE armer (DISARMED / COMPLETE / ERROR), so a noon
    re-arm or a manual afternoon arm (ARMED from noon on) blocked it, even
    though an ARMED armer touches NINA #1 only at pre-config;
  * it anchored sunset on the UTC date, which rolls at 18:00 local (MDT), so
    the window closed about 40 min before sunset instead of at sunset + 5;
  * it logged nothing when it did not dispatch.

This module runs from the auto-arm loop on every tick, whatever auto-arm is
set to, and logs one line per evening state change ("auto-flats: ...") plus
a decision row (kind auto_arm, action dusk_flats) in tonight's events:

  window   sunset - dusk_flats_lead_min .. stop - 15 min, where stop is
           nautical dusk (or the night's pre-config, if earlier) minus
           dusk_flats_end_margin_min. The sequences themselves are capped at
           stop, so dusk flats never block the night's dispatch, an arm or a
           TPoint run.
  gates    auto_dusk_flats; flats_when (dawn = never at dusk); something
           stale (RC16 filters: stale_flat_filters; Piggy-600: the owed OSC
           set; 2026-10-24: RC16 narrowband only with dusk_flats_narrowband,
           else it stays owed for the dawn: "dawn_only" when nothing else
           is); armer not RUNNING / PAUSED / WATCHING; no capture job on
           the RC16; the safety monitor readable (2026-10-24: a closed roof
           no longer holds the dispatch, NINA waits for it; with
           flats_dispatch_roof_closed off it is retried each tick until the
           window ends); the PS-125 guard.
  dispatch RC16 on NINA #1 (armer.dispatch_raw, the stale filters; holds its
           flat pointing for the Piggy-600 when the OSC set is owed) and,
           when owed, the Piggy-600 OSC set on NINA #2 (cooled to its
           setpoint, NINA SkyFlat histogram targeting like its dawn set),
           in parallel, riding the RC16 slew.

The auto-arm loop holds its own automatic arm until the flats stop (it
would otherwise skip with "NINA is running"); a manual or remote Arm is
never held (it only touches NINA at pre-config, after the flats stop).

2026-10-24 (flats v2, flat_plan.py / sky_flat_model.py): the owed filters
(minus what this morning's dawn window saved, counted frames) are planned
by sun altitude; nothing fits = SKIP_NO_WINDOW (owed for the dawn). The
dispatch goes out flats_dusk_cool_lead_min + 5 min before the first window
(or sunset), with the roof closed too (flats_dispatch_roof_closed): NINA
cools, waits for the roof (polled every 10 s) and shoots each filter in its
window; a window that passed while the roof was closed is skipped there and
stays owed. The plan is stored (flat_plan.save_plan) and
maybe_report_windows counts the frames each window really saved, both
twilights, and pushes that (never "complete" on NINA's word), after
learning from the window (sky_flat_model.refresh).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

BUSY_STATES = ("RUNNING", "PAUSED_UNSAFE", "PAUSED_OPERATOR", "WATCHING")

SKIP_DISABLED = "flats_disabled"
SKIP_FLATS_WHEN = "flats_when_dawn"
SKIP_NOTHING_STALE = "nothing_stale"
SKIP_ARMER_BUSY = "armer_busy"
SKIP_CAPTURE_BUSY = "capture_busy"
SKIP_ROOF_CLOSED = "roof_closed"
SKIP_SAFETY_UNREADABLE = "safety_unreadable"
SKIP_DISPATCH_FAILED = "dispatch_failed"
SKIP_WINDOW_CLOSED = "window_closed"
SKIP_DAWN_ONLY = "dawn_only"
SKIP_NO_WINDOW = "no_window"

TITLES = {
    SKIP_DISABLED: "auto_dusk_flats is off",
    SKIP_FLATS_WHEN: "flats_when=dawn",
    SKIP_NOTHING_STALE: "no flats stale",
    SKIP_ARMER_BUSY: "armer busy",
    SKIP_CAPTURE_BUSY: "a calibration capture job is running",
    SKIP_ROOF_CLOSED: "roof closed (safety monitor UNSAFE)",
    SKIP_SAFETY_UNREADABLE: "safety monitor unreadable",
    SKIP_DISPATCH_FAILED: "dispatch failed",
    SKIP_WINDOW_CLOSED: "dusk flat window closed",
    SKIP_DAWN_ONLY: "only dawn-only flats owed",
    SKIP_NO_WINDOW: "no owed set fits a dusk window",
}


async def read_roof_safe(config) -> bool | None:
    """NINA #1's safety monitor: True = SAFE (roof open), False = UNSAFE,
    None = unreadable. Connects the monitor when NINA has it disconnected
    (connect only, like arm's connect-all; nothing moves)."""
    from photonscript.scheduler.preflight import _ensure_connected
    try:
        ok, payload, _err = await _ensure_connected(config, "safetymonitor",
                                                    attempts=1)
    except Exception:  # noqa: BLE001
        return None
    if ok and "IsSafe" in (payload or {}):
        return bool(payload.get("IsSafe"))
    return None


async def _piggy_safety(pcfg) -> bool:
    """Does NINA #2 see a safety monitor (read only)? Then the OSC flats
    carry their own SafetyMonitor condition."""
    from photonscript.scheduler.preflight import _connected
    try:
        ok, payload, _err = await _connected(pcfg, "safetymonitor")
    except Exception:  # noqa: BLE001
        return False
    return bool(ok and "IsSafe" in (payload or {}))


def _note(config, state: dict, skip: str | None, detail: str, now: datetime,
          *, final: bool = False, value: str = "skipped",
          trigger: str = "auto dusk flats", row: bool = True) -> None:
    """One log line per evening state change, plus a decision row (row
    False: the log line only)."""
    from photonscript.scheduler.auto_armer import ACTION_FLATS, log_decision
    key = (value, skip, detail)
    if state.get("last") != key:
        state["last"] = key
        if value == "dispatched":
            logger.info("auto-flats %s: dispatched (%s)", state.get("night"), detail)
        else:
            logger.info("auto-flats %s: %s: %s (%s)", state.get("night"),
                        "skipped for tonight" if final else "not yet",
                        TITLES.get(skip, skip), detail)
        try:
            if row:
                log_decision(config, value, skip, detail, trigger, now,
                             action=ACTION_FLATS)
        except Exception:  # noqa: BLE001 - logging never breaks the loop
            logger.warning("auto-flats decision log failed", exc_info=True)
    if skip:
        state["reason"] = f"{TITLES.get(skip, skip)} ({detail})"
    if final:
        state["done"] = True


def _stale(config, state: dict) -> tuple[list[str], dict]:
    """(RC16 stale filters, Piggy-600 flat status), computed once an
    evening (the health scan and the owed view are not free)."""
    from photonscript.scheduler import calibration as cal
    if "stale" not in state:
        try:
            state["stale"] = list(cal.stale_flat_filters(config))
        except Exception:  # noqa: BLE001
            logger.warning("auto-flats: stale filters unavailable", exc_info=True)
            state["stale"] = []
        pb = {"owed": False, "reasons": [], "shot_today": False}
        if (getattr(config, "piggyback_enabled", False)
                and getattr(config, "piggyback_dusk_flats", True)):
            try:
                pb = cal.piggyback_flat_status(config)
            except Exception:  # noqa: BLE001
                logger.warning("auto-flats: Piggy-600 status unavailable",
                               exc_info=True)
        # 2026-10-24: what this morning's dawn window saved (counted frames,
        # not yet filed by the dawn sweep in every case) is not owed again
        try:
            from photonscript.scheduler.dusk_flats_check import window_counts
            prev = (datetime.strptime(state["night"], "%Y-%m-%d")
                    - timedelta(days=1)).strftime("%Y-%m-%d")
            need = int(getattr(config, "flats_min_saved", 10) or 1)
            counts = window_counts(config, prev, "dawn") or {}
            done = {f for f, k in (counts.get("rc16") or {}).items() if k >= need}
            state["stale"] = [f for f in state["stale"] if f not in done]
            if (counts.get("piggyback") or {}).get("OSC", 0) >= need:
                pb = {**pb, "owed": False}
        except Exception:  # noqa: BLE001
            logger.warning("auto-flats: dawn count unavailable", exc_info=True)
        state["pb"] = pb
    return state["stale"], state["pb"]


async def maybe_dispatch_dusk_flats(config, armer, state: dict, notify, *,
                                    now: datetime | None = None,
                                    guard=None) -> str | None:
    """One tick. state is the loop's per-evening dict (night, done, until,
    last, reason). Returns what happened this tick ("dispatched", a skip
    kind, or None outside the window / already decided)."""
    from photonscript.scheduler import auto_armer as aa
    from photonscript.scheduler import calibration as cal
    now = now or datetime.utcnow()
    t = cal.dusk_flat_times(config, now)
    if not t:
        return None
    if state.get("night") != t["night"]:
        state.clear()
        state["night"] = t["night"]
    if state.get("done") or now < t["open"]:
        return None
    if not getattr(config, "auto_dusk_flats", True):
        _note(config, state, SKIP_DISABLED, "set auto_dusk_flats on to use them",
              now, final=True, row=False)
        return SKIP_DISABLED
    if now > t["last_dispatch"]:
        # a decision row only when this evening saw the window (a restart
        # after it just logs the line)
        _note(config, state, SKIP_WINDOW_CLOSED,
              "no dusk flats tonight; last reason: "
              + (state.get("reason") or "the service was not running in the "
                 "window"), now, final=True, row=bool(state.get("reason")))
        return SKIP_WINDOW_CLOSED
    when = cal.flats_when(config)
    if when == "dawn":
        _note(config, state, SKIP_FLATS_WHEN, "the night sequences shoot "
              "flats at dawn only", now, final=True)
        return SKIP_FLATS_WHEN
    owed, pb = _stale(config, state)
    pb_owed = bool(pb.get("owed"))
    # 2026-10-24: RC16 narrowband is too dim at dusk (dusk_flat_filters)
    stale = cal.dusk_flat_filters(config, owed)
    dawn_only = [f for f in owed if f not in stale]
    if not stale and not pb_owed:
        if dawn_only:
            _note(config, state, SKIP_DAWN_ONLY, f"owed: {', '.join(dawn_only)}; "
                  "RC16 narrowband is too dim at dusk, the dawn set shoots it "
                  "(dusk_flats_narrowband on to try anyway)", now, final=True)
            return SKIP_DAWN_ONLY
        _note(config, state, SKIP_NOTHING_STALE, "RC16 and Piggy-600 flats are "
              "current (a dawn set today counts)", now, final=True)
        return SKIP_NOTHING_STALE
    # 2026-10-24: plan by sun altitude (flat_plan.py); the routine is
    # dispatched early enough to be cooled before the first window and
    # waits in NINA for the roof, starting within seconds of it reading safe
    from photonscript.scheduler import flat_plan as fp
    plan = fp.plan(config, "dusk", t["night"], stale, osc=pb_owed,
                   ready=max(now + timedelta(minutes=3), t["open"]))
    rc_slots = plan["rigs"]["rc16"]["slots"]
    pb_slots = (plan["rigs"].get("piggyback") or {}).get("slots") or []
    skipped = dict(plan["rigs"]["rc16"]["skipped"])
    if not rc_slots and not pb_slots:
        why = "; ".join(f"{f}: {w}" for f, w in list(skipped.items()) + list(
            ((plan["rigs"].get("piggyback") or {}).get("skipped") or {}).items()))
        _note(config, state, SKIP_NO_WINDOW, f"{why or 'nothing fits'}; owed for "
              "the dawn" + (f" (and {', '.join(dawn_only)})" if dawn_only else ""),
              now, final=True)
        return SKIP_NO_WINDOW
    lead = int(getattr(config, "flats_dusk_cool_lead_min", 30) or 30)
    first = plan["first_start"]
    dispatch_at = min(first, t["sunset"]) - timedelta(minutes=lead + 5)
    if now < dispatch_at:
        return None     # not yet: dispatched cool_lead + 5 min before
    rc_names = [s["filter"] for s in rc_slots]
    pb_owed = bool(pb_slots)
    want = (", ".join(rc_names) if rc_names else "no RC16 filters") + (
        "; Piggy-600 OSC (" + "; ".join(pb.get("reasons") or ["owed"]) + ")"
        if pb_owed else "")
    if armer.state in BUSY_STATES:
        _note(config, state, SKIP_ARMER_BUSY, f"armer {armer.state}; wanted: {want}",
              now)
        return SKIP_ARMER_BUSY
    try:
        from photonscript.scheduler.calibration_capture import busy as cap_busy
        if cap_busy("rc16"):
            _note(config, state, SKIP_CAPTURE_BUSY, f"RC16; wanted: {want}", now)
            return SKIP_CAPTURE_BUSY
    except Exception:  # noqa: BLE001
        pass
    safe = await read_roof_safe(config)
    if safe is False and not getattr(config, "flats_dispatch_roof_closed", True):
        _note(config, state, SKIP_ROOF_CLOSED, "closed roof makes junk flats; "
              f"retrying until {t['last_dispatch']:%H:%M}Z; wanted: {want}", now)
        return SKIP_ROOF_CLOSED
    if safe is None:
        _note(config, state, SKIP_SAFETY_UNREADABLE, "NINA #1 safety monitor "
              f"not readable (is NINA running?); wanted: {want}", now)
        return SKIP_SAFETY_UNREADABLE
    label = ("auto dusk flats (" + (",".join(rc_names) if rc_names else "OSC hold")
             + (", Piggy-600" if pb_owed and rc_names else "") + ")")
    skip, why = await (guard or aa.auto_arm_guard)(config)
    if skip:   # PS-131: a sideload tonight, NINA #1 running or unreadable
        await aa.skip_and_notify(config, skip, why, label, notify,
                                 action=aa.ACTION_FLATS)
        logger.info("auto-flats %s: not yet: %s (%s)", state["night"], skip, why)
        state["reason"] = f"{aa.SKIP_TITLES.get(skip, skip)} ({why})"
        state["last"] = ("skipped", skip, why)
        return skip
    seq_text, start_local = cal.generate_dusk_flats_json(
        config, only_filters=rc_names, hold_for_osc=pb_owed, now=now, plan=plan)
    ok = await armer.dispatch_raw(json.loads(seq_text), label)
    if not ok:
        _note(config, state, SKIP_DISPATCH_FAILED,
              f"NINA #1: {getattr(armer, 'detail', '') or 'refused'}", now)
        return SKIP_DISPATCH_FAILED
    piggy = await _dispatch_piggyback(config, now, plan) if pb_owed else None
    fp.save_plan(config, plan, dispatched_at=now,
                 dispatched={"rc16": True,
                             "piggyback": bool(piggy and piggy.startswith("dispatched"))},
                 roof_at_dispatch=("open" if safe else "closed"))
    state["until"] = t["stop"] + timedelta(minutes=5)
    stop_local = _local(config, t["stop"])
    owed_dawn = list(skipped) + dawn_only
    detail = (f"RC16: {', '.join(rc_names) or 'pointing hold only'}, starts "
              f"{start_local} local, done by {stop_local:%H:%M}"
              + (f" (left for dawn: {', '.join(owed_dawn)})" if owed_dawn else "")
              + (f"; Piggy-600 OSC: {piggy}" if piggy else "")
              + ("; roof closed at dispatch: NINA waits for it" if safe is False else ""))
    _note(config, state, None, detail, now, final=True, value="dispatched",
          trigger=label)
    try:
        await notify(config, f"Auto dusk flats dispatched: {detail}. Plan: "
                     + fp.describe(config, plan) + ". The night arm is untouched "
                     "(flats stop before nautical dusk); the saved frames are "
                     "counted after the window.",
                     title="PhotonScript auto flats", kind="info")
    except Exception:  # noqa: BLE001
        logger.warning("auto-flats push failed", exc_info=True)
    return "dispatched"


def _local(config, dt: datetime) -> datetime:
    from photonscript.shared.localtime import utc_offset_hours
    return dt + timedelta(hours=utc_offset_hours(config, dt))


async def _dispatch_piggyback(config, now: datetime, plan: dict | None = None) -> str:
    """The Piggy-600 OSC dusk set on NINA #2 (rides the RC16 slew). Returns
    a short outcome for the log line; never raises."""
    from photonscript.scheduler import calibration as cal
    from photonscript.scheduler import sideload as sd
    from photonscript.shared.rigs import PIGGYBACK, nina_dispatch, rig_config
    try:
        pcfg = rig_config(config, PIGGYBACK)
        from photonscript.scheduler.calibration_capture import busy as cap_busy
        if cap_busy(PIGGYBACK):
            return "skipped (a calibration capture job is running on NINA #2)"
        tree, err = await sd.read_sequence_state(pcfg.nina_base_url)
        if err:
            return f"skipped (NINA #2 unreadable: {err})"
        running = sd.nina_running(tree)
        if running:
            return f"skipped (NINA #2 running {', '.join(running[:2])})"
        has_safety = await _piggy_safety(pcfg)
        seq_text, start_local = cal.generate_dusk_flats_json(
            pcfg, osc=True, owns_mount=False, safety=has_safety, now=now, plan=plan)
        seq = json.loads(seq_text)
        from photonscript.scheduler.sequence_lint import format_result
        from photonscript.scheduler.sideload import lint_companion
        lint = lint_companion(seq)
        if not lint.ok:
            return f"NOT started, lint failed ({format_result(lint)[:200]})"
        res = await nina_dispatch(pcfg.nina_base_url, seq, config=config,
                                  rig=PIGGYBACK)
        if res.get("ok"):
            return (f"dispatched, starts {start_local} local, cooled to "
                    f"{float(pcfg.camera_setpoint_c):g} C"
                    + ("" if has_safety else ", no safety monitor on NINA #2"))
        return f"dispatch failed ({res.get('detail')})"
    except Exception as e:  # noqa: BLE001
        logger.warning("auto-flats: Piggy-600 dispatch error", exc_info=True)
        return f"error ({e})"


# ---- the window report (2026-10-24) -------------------------------------------

REPORT_TAIL_MIN = 12     # report this long after a window's planned end


async def maybe_report_windows(config, notify, *, now: datetime | None = None) -> list[str]:
    """One tick: for this evening's dusk plan and this morning's dawn plan
    (flat_plan.save_plan), once each, REPORT_TAIL_MIN after the plan's end:
    learn from the window (sky_flat_model.refresh), count the FLAT frames
    each rig actually wrote, and push "Dusk flats <night>: RC16 L 15, B 15;
    Piggy-600 OSC 25 ..." or "... none saved (reason)". Returns the lines
    pushed. Never raises."""
    import asyncio
    from photonscript.scheduler import flat_plan as fp
    from photonscript.scheduler.dusk_flats_check import window_report
    from photonscript.shared.localtime import night_base_date
    now = now or datetime.utcnow()
    out = []
    if not getattr(config, "flats_report_push", True):
        return out
    base = night_base_date(config, now)
    cands = [(base.strftime("%Y-%m-%d"), "dusk"),
             ((base - timedelta(days=1)).strftime("%Y-%m-%d"), "dawn"),
             (base.strftime("%Y-%m-%d"), "dawn")]
    for night, phase in cands:
        try:
            p = fp.load_plan(config, night, phase)
            if not p or p.get("reported") or not p.get("end"):
                continue
            if now < p["end"] + timedelta(minutes=REPORT_TAIL_MIN):
                continue
            if now > p["end"] + timedelta(hours=10):
                fp.mark(config, night, phase, reported="stale")
                continue
            from photonscript.scheduler import sky_flat_model as sfm
            learn = await asyncio.to_thread(sfm.refresh, config, night)
            rep = await asyncio.to_thread(window_report, config, night, phase,
                                          learn)
            fp.mark(config, night, phase, reported=now, result=rep)
            line = rep.get("line")
            if line:
                out.append(line)
                logger.info("flats report: %s", line)
                # PS-212: the story flats push, once per window; the dusk
                # count rides in "imaging now" when that push is still to come
                # and the dawn count in the held end-of-night report
                kind, held, ns = "story", False, None
                try:
                    from photonscript.scheduler import night_story as ns
                    if phase == "dusk" and ns.imaging_pending(config, now):
                        kind = "info"
                    held = phase == "dawn" and bool(
                        ns.load_state(config, night).get("end_pending"))
                    if held:
                        kind = "info"
                except Exception:  # noqa: BLE001
                    pass
                await notify(config, line, title="PhotonScript flats", kind=kind,
                             fact=f"flats:{night}:{phase}")
                if held and ns is not None:
                    await ns.flush_end(config, night, dawn_line=line, now=now)
        except Exception as e:  # noqa: BLE001 - reporting never breaks the loop
            logger.warning("flats report %s %s failed: %s", night, phase, e,
                           exc_info=True)
    return out
