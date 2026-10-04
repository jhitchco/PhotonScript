"""PHD2 calibration manager, the live half (PS-93). RC16 agent only.

NINA's PHD2_CALIBRATION slot (nina_sequence_json) slews to a field near
Dec +5 by the meridian, forces a calibration, stops guiding and holds for
phd2_cal_hold_s. This listens to PHD2's own events:

  * Calibrating        step counts and distance moved per direction
  * CalibrationComplete / CalibrationFailed
                       read get_calibration_data and NINA's mount (Dec,
                       hour angle, pier side, altitude), grade it
                       (scheduler.phd2_calibration.grade) and store it;
  * inside the armer's plan (cal_plan.json, mount on the planned field) a
    FAIL is retried ONCE over PHD2 during the hold: wait until NINA has
    stopped guiding, guide(recalibrate=True) under phd2_ops.hold("calmanager"),
    grade the new one, stop_capture before the hold ends. A second FAIL (or
    no time left) applies phd2_cal_fail_action: "keep" guiding on it with one
    alert per night (the approved default), or "unguided" (PS-85 fallback);
  * CalibrationDataFlipped  noted on the active record;
  * ConfigurationChange and every (re)connect: snapshot PHD2's profile,
    binning, scale and calibrated flag (live.json, read by the armer's
    needs_calibration); a profile / binning change against the active record,
    or PHD2 uncalibrated with its mount connected, while a night runs asks
    the armer for one re-dispatch with the slot (option C, 1 h of dark left).

flip_check() is called by the agent's PS-92 post-flip watch: a Dec runaway
(phd2_calibration.dec_runaway) alerts with the Reverse Dec fix
(phd2_flip_action=alert); a clean pass marks flip[pier] verified.
Event listeners never await PHD2 (they run inside the socket reader): work
is scheduled as tasks.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time

from photonscript.scheduler import phd2_calibration as pc
from photonscript.shared import phd2_store as store
from photonscript.telescope_agent import phd2_ops

logger = logging.getLogger(__name__)

RETRY_MIN_LEFT_S = 150.0   # hold left before a retry is worth starting
STOP_WAIT_S = 180.0        # NINA's StartGuiding settles, then StopGuiding
FIELD_TOL_DEG = 3.0        # mount this close to the planned field = in plan
STOPPED_STABLE_S = 10.0    # PHD2 idle this long = NINA is past StopGuiding
POLL_S = 1.0
_DIRS = {"west": "West", "east": "East", "north": "North", "south": "South",
         "w": "West", "e": "East", "n": "North", "s": "South"}


def _sep_deg(ra1_h, dec1, ra2_h, dec2) -> float:
    a1, a2 = math.radians(ra1_h * 15.0), math.radians(ra2_h * 15.0)
    d1, d2 = math.radians(dec1), math.radians(dec2)
    c = (math.sin(d1) * math.sin(d2)
         + math.cos(d1) * math.cos(d2) * math.cos(a1 - a2))
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


class CalManager:
    def __init__(self, config, phd2, nina, clock=time.time):
        self.config = config
        self.phd2 = phd2
        self.nina = nina
        self.clock = clock
        self._steps: dict[str, int] = {}
        self._moved: dict[str, float] = {}
        self._waiter: asyncio.Future | None = None
        self._stopped_at: float | None = None
        self._outcome_at: float | None = None
        self.tasks: set[asyncio.Task] = set()
        self.last: dict | None = None     # last graded record (tests, API)

    def attach(self) -> None:
        self.phd2.on_event(self.on_event)

    def _spawn(self, coro) -> None:
        t = asyncio.get_running_loop().create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)

    # ------------------------------------------------------------ events

    async def on_event(self, ev: dict) -> None:
        t = ev.get("Event")
        if t == "StartCalibration":
            self._steps, self._moved = {}, {}
        elif t == "Calibrating":
            d = _DIRS.get(str(ev.get("dir") or "").strip().lower())
            if d:
                try:
                    self._steps[d] = max(self._steps.get(d, 0), int(ev.get("step") or 0))
                    self._moved[d] = round(max(self._moved.get(d, 0.0),
                                               float(ev.get("dist") or 0.0)), 1)
                except (TypeError, ValueError):
                    pass
        elif t in ("CalibrationComplete", "CalibrationFailed"):
            outcome = {"result": "complete" if t == "CalibrationComplete" else "failed",
                       "message": ev.get("Reason"), "steps": dict(self._steps),
                       "moved": dict(self._moved), "t": self.clock()}
            self._outcome_at = outcome["t"]
            self._stopped_at = None
            if self._waiter is not None and not self._waiter.done():
                self._waiter.set_result(outcome)      # a retry is waiting on it
            else:
                self._spawn(self.handle(outcome))
        elif t in ("GuidingStopped", "LoopingExposuresStopped"):
            self._stopped_at = self.clock()
        elif t == "CalibrationDataFlipped":
            pc.note_flipped(self.config)
        elif t == "ConfigurationChange":
            self._spawn(self.check_live("PHD2 configuration change"))

    # ------------------------------------------------------------ grading

    async def _mount(self) -> dict:
        try:
            return await self.nina.get_mount_info() or {}
        except Exception as e:  # noqa: BLE001
            logger.debug("calmanager: NINA mount info failed: %s", e)
            return {}

    async def snapshot(self) -> dict:
        """PHD2's profile, binning, guide scale and calibrated flag."""
        live = {}
        for key, rpc in (("profile", "get_profile"), ("binning", "get_camera_binning"),
                         ("scale_arcsec_px", "get_pixel_scale"),
                         ("calibration", "get_calibration_data"),
                         ("equipment", "get_current_equipment")):
            try:
                live[key] = await self.phd2.call(rpc, ["Mount"] if rpc ==
                                                 "get_calibration_data" else None)
            except Exception as e:  # noqa: BLE001
                logger.debug("calmanager: %s failed: %s", rpc, e)
        prof = live.get("profile")
        cal = live.pop("calibration", None)
        eq = live.pop("equipment", None) or {}
        out = {"profile": prof.get("name") if isinstance(prof, dict) else prof,
               "binning": live.get("binning"),
               "scale_arcsec_px": live.get("scale_arcsec_px"),
               "calibrated": (bool(cal.get("calibrated")) if isinstance(cal, dict)
                              else None),
               "mount_connected": (bool((eq.get("mount") or {}).get("connected"))
                                   if isinstance(eq, dict) else None)}
        return out

    async def _record(self, outcome: dict, live: dict, mount: dict) -> dict:
        from photonscript.telescope_agent.pulse_selftest import guide_speeds
        cal = {}
        if outcome["result"] == "complete":
            try:
                cal = await self.phd2.get_calibration_data()
            except Exception as e:  # noqa: BLE001
                logger.warning("calmanager: get_calibration_data failed: %s", e)
        moved = outcome.get("moved") or {}
        if live.get("cal_distance_px") is None and moved.get("West"):
            # PHD2 stops an axis once it has moved the calibration distance
            live = dict(live, cal_distance_px=moved.get("West"))
        sp = guide_speeds(self.config, mount)
        return pc.record_from_api(cal, mount=mount, steps=outcome.get("steps"),
                                  moved_px=moved, result=outcome["result"],
                                  message=outcome.get("message"), live=live,
                                  speeds={"ra": sp.get("ra"), "dec": sp.get("dec")})

    def _in_plan(self, plan, mount: dict) -> bool:
        if not pc.plan_active(plan):
            return False
        f = (plan or {}).get("field") or {}
        ra, dec = mount.get("RightAscension"), mount.get("Declination")
        if ra is None or dec is None or f.get("ra_hours") is None:
            return True
        try:
            return _sep_deg(float(ra), float(dec), float(f["ra_hours"]),
                            float(f["dec_degrees"])) <= FIELD_TOL_DEG
        except (TypeError, ValueError):
            return True

    async def grade_outcome(self, outcome: dict, context: str) -> dict:
        live = await self.snapshot()
        pc.save_live(self.config, live)
        mount = await self._mount()
        rec = await self._record(outcome, live, mount)
        prev = pc.last_good(self.config, rec.get("pier_side"))
        rec = pc.graded(rec, prev)
        rec["context"] = context
        rec["night"] = store.night_of(self.config)
        pc.save_record(self.config, rec)
        self.last = rec
        logger.info("PHD2 calibration (%s): %s ortho %s at Dec %s HA %s pier %s; %s",
                    context, rec["grade"], rec.get("ortho_err_deg"), rec.get("dec_deg"),
                    rec.get("ha_hr"), rec.get("pier_side"),
                    "; ".join(rec["reasons"] + rec["warnings"]) or "clean")
        return rec

    async def handle(self, outcome: dict) -> dict | None:
        """Grade a calibration that just ended; inside the plan, retry a FAIL
        once during the hold. Never raises."""
        try:
            plan = pc.load_plan(self.config)
            mount = await self._mount()
            in_plan = self._in_plan(plan, mount)
            rec = await self.grade_outcome(outcome, "plan" if in_plan else "other")
            if not in_plan:
                if rec["grade"] == pc.FAIL:
                    await self._alert(rec, "PHD2 calibrated outside the PhotonScript "
                                      "slot and the result FAILED")
                return rec
            if rec["grade"] != pc.FAIL:
                plan.update(status="done", grade=rec["grade"])
                pc.save_plan(self.config, plan)
                return rec
            if int(plan.get("attempts") or 0) >= 1:
                return await self._give_up(plan, rec)
            plan.update(status="graded", grade=rec["grade"])
            pc.save_plan(self.config, plan)
            return await self._retry(plan, rec)
        except Exception as e:  # noqa: BLE001
            logger.warning("calmanager: grading failed: %s", e)
            return None

    async def _wait_stopped(self, since: float) -> float | None:
        """When NINA's StopGuiding (or a failed calibration) left PHD2
        stopped: the start of the hold. None if PHD2 keeps guiding."""
        deadline = self.clock() + STOP_WAIT_S
        idle_since = None
        while self.clock() < deadline:
            if self._stopped_at is not None and self._stopped_at >= since:
                return self._stopped_at
            state = getattr(self.phd2, "app_state", None)
            if state in ("Stopped", "Looping", "Selected"):
                idle_since = idle_since or self.clock()
                if self.clock() - idle_since >= STOPPED_STABLE_S:
                    return idle_since
            else:
                idle_since = None
            await asyncio.sleep(POLL_S)
        return None

    async def _retry(self, plan: dict, first: dict) -> dict:
        hold = float(plan.get("hold_s") or getattr(self.config, "phd2_cal_hold_s", 240))
        t0 = await self._wait_stopped(self._outcome_at or self.clock())
        if t0 is None:
            return await self._give_up(plan, first, "NINA did not stop guiding "
                                       "after the calibration: no retry")
        end = t0 + hold
        left = end - self.clock()
        if left < RETRY_MIN_LEFT_S:
            return await self._give_up(plan, first, f"only {left:.0f} s of the hold "
                                       "left: no retry")
        plan.update(status="retrying", attempts=1)
        pc.save_plan(self.config, plan)
        loop = asyncio.get_running_loop()
        outcome = None
        try:
            async with phd2_ops.hold("calmanager", wait_s=10):
                self._waiter = loop.create_future()
                logger.warning("PHD2 calibration FAILED (%s): retrying once in the hold",
                               "; ".join(first.get("reasons") or []))
                try:
                    await self.phd2.start_guiding(recalibrate=True)
                    outcome = await asyncio.wait_for(
                        self._waiter, max(1.0, end - 15.0 - self.clock()))
                except asyncio.TimeoutError:
                    outcome = {"result": "aborted", "message": "retry ran past the hold",
                               "steps": dict(self._steps), "moved": dict(self._moved)}
                except Exception as e:  # noqa: BLE001
                    outcome = {"result": "aborted", "message": f"retry refused: {e}",
                               "steps": {}, "moved": {}}
                finally:
                    self._waiter = None
                    try:   # never leave PHD2 guiding into the next slew
                        await self.phd2.stop_capture()
                    except Exception as e:  # noqa: BLE001
                        logger.warning("calmanager: stop_capture after retry: %s", e)
        except phd2_ops.PHD2Busy as e:
            return await self._give_up(plan, first, f"no retry: {e}")
        rec = await self.grade_outcome(outcome, "retry")
        if rec["grade"] != pc.FAIL:
            plan.update(status="done", grade=rec["grade"])
            pc.save_plan(self.config, plan)
            return rec
        return await self._give_up(plan, rec)

    async def _give_up(self, plan: dict, rec: dict, why: str = "") -> dict:
        plan.update(status="failed", grade=rec.get("grade"))
        pc.save_plan(self.config, plan)
        action = str(getattr(self.config, "phd2_cal_fail_action", "keep") or "keep").lower()
        tail = (" Switching the rest of the night to unguided."
                if action == "unguided" else
                " Guiding continues on this calibration (phd2_cal_fail_action=keep).")
        await self._alert(rec, "PHD2 calibration FAILED" + (f" ({why})" if why else
                                                            " twice") + "." + tail)
        if action == "unguided":
            from photonscript.scheduler.armer import request_fallback_unguided
            await request_fallback_unguided(self.config, "PHD2 calibration failed")
        return rec

    async def _alert(self, rec: dict, head: str) -> None:
        from photonscript.shared.pushover import notify, record
        night = store.night_of(self.config)
        step = rec.get("recommended_step_ms")
        msg = (f"{head}: {'; '.join(rec.get('reasons') or []) or rec.get('grade')}"
               f" (Dec {rec.get('dec_deg')}, HA {rec.get('ha_hr')} h, pier "
               f"{rec.get('pier_side') or '?'}).")
        if step:
            msg += f" Set PHD2 Calibration Step to about {step} ms."
        if store.alert_once(self.config, f"calibration-{night}"):
            await notify(self.config, msg, title="PhotonScript PHD2 calibration",
                         priority=1)
        else:
            record(self.config, msg, title="PhotonScript PHD2 calibration",
                   priority=1, reason="calibration-once-per-night")

    # ------------------------------------------------------------ invalidation

    def connected(self) -> None:
        """The agent (re)connected to PHD2 and its event loop runs."""
        self._spawn(self.check_live("PHD2 connect"))

    async def check_live(self, why: str) -> str | None:
        """Snapshot PHD2 and, if the active calibration no longer applies
        while a night runs, ask the armer for one re-dispatch with the slot.
        Returns the invalidation reason (or None)."""
        try:
            live = await self.snapshot()
        except Exception as e:  # noqa: BLE001
            logger.debug("calmanager: snapshot failed: %s", e)
            return None
        pc.save_live(self.config, live)
        if pc.cfg_mode(self.config) == "never" or pc.plan_active(pc.load_plan(self.config)):
            return None
        rec = pc.load_active(self.config, seed=False)
        reason = None
        if live.get("calibrated") is False and live.get("mount_connected"):
            reason = "PHD2 has no calibration"
        elif rec:
            for key, name in (("profile", "PHD2 profile"), ("binning", "guide binning")):
                a, b = rec.get(key), live.get(key)
                if a not in (None, "") and b not in (None, "") and str(a) != str(b):
                    reason = f"{name} changed ({a} -> {b})"
                    break
        if reason is None:
            self._invalid = None
            return None
        from photonscript.scheduler.armer import (armer_guided_now,
                                                  request_recalibration)
        if not armer_guided_now(self.config):
            # PS-66: no guided night armed (unguided or idle): nothing to
            # recalibrate for, and never a re-dispatch of an unguided night
            if reason != getattr(self, "_invalid", None):
                self._invalid = reason
                logger.info("PHD2 calibration invalid (%s, after %s); no "
                            "guided night armed, no recalibration", reason, why)
            return reason
        if reason != getattr(self, "_invalid", None):   # PHD2 sends many changes
            self._invalid = reason
            logger.warning("PHD2 calibration invalid (%s, after %s)", reason, why)
        await request_recalibration(self.config, reason)
        return reason

    # ------------------------------------------------------------ meridian flip

    async def flip_check(self, frames, rates_px_s, scale, pier: str | None,
                         passive: str | None) -> dict:
        """After a pier-side change (the agent's PS-92 watch, 3 min of
        guiding): Dec runaway -> flip[pier] not verified + one alert per
        night with the Reverse Dec fix (phd2_flip_action=alert); a clean
        pass -> flip[pier] verified."""
        y_rate = rates_px_s[1] if rates_px_s else None
        r = pc.dec_runaway(frames, y_rate, scale)
        bad = bool(r.get("runaway")) or passive == "REVERSED"
        if bad:
            pc.set_flip(self.config, pier, False, r.get("reason") or "Dec reversed")
            if flip_alerts(self.config):
                from photonscript.telescope_agent.pulse_selftest import passive_reversed_alert
                await passive_reversed_alert(self.config, r.get("reason") or "reversed",
                                             store.night_of(self.config), pier)
        elif r.get("runaway") is False and passive is None:
            pc.set_flip(self.config, pier, True, "Dec holds after the flip")
        return r


def flip_alerts(config) -> bool:
    return str(getattr(config, "phd2_flip_action", "alert") or "alert").lower() != "off"

