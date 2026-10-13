"""Night pilot: turn the sub doctor's diagnoses into bounded, logged
adjustments so the next sub is good.

2026-10-12 night: a stray PHD2 session (guiding on a FAIL calibration) on an
UNGUIDED night trailed every RC16 sub for hours; trailing at 300 s needed a
human to switch to 180 s; a restart race needed a manual retry; earlier a
dead focuser cost three nights. Jeremy (2026-10-13): "make adjustments over
the night to make sure we're doing a good job to get the most subs."

Input: the sub doctor's run events (runs/<night>_events.jsonl, kind
"sub_doctor": rig, cause, confidence high | medium | low, headline,
evidence {...}, suggested_action {id, ...}; scheduler/sub_doctor.py
EVENT_FIELDS). norm_diag() is the thin adapter: it also takes a numeric
confidence and a plain string action, so a schema change is one place.

Policy (POLICY / decide()): cause -> action, each with its own on/off key,
a cooldown, a max per night and a verification on the next
night_pilot_verify_subs (2) subs of that rig:

  guider_conflict  UNGUIDED night and PHD2 guiding / calibrating-free
                   active: stop_phd2 (PHD2 JSON-RPC stop_capture, the
                   guide-recover PS-167 client). GUIDED night with a FAIL
                   calibration or a non-star (hot pixel) lock on a bad
                   calibration: unguided (Armer.fallback_unguided: the
                   remainder re-dispatched unguided, PS-156 caps). Guided
                   with a good calibration, "start guiding": a human.
                   Verified on ecc.
  tracking         RC16, UNGUIDED, both rigs trailing (shared mount) or a
                   measured drift: shorten_subs, a step down
                   night_pilot_sub_steps (300 -> 180 -> 120) never below
                   night_pilot_sub_floor_s that prefers lengths with
                   matched darks (the night dark quota or the library),
                   then lengths whose darks scale from a bias at the same
                   epoch (pick_step / dark_match; the push says which), as
                   tonight-only exposure
                   overrides on the goal (ProjectStore.update), then
                   Restart tonight from now after the current sub (PS-143).
                   Verified on ecc; a step that did not lower the ecc is
                   undone (the previous length, or the goal default, and a
                   restart after the current sub when the guards allow),
                   "trailing is not drift-per-second" is recorded and a
                   human is asked; no more shortening tonight. A step that
                   helped can be followed by one more on a later diagnosis.
  focus            soft, round stars with the other rig sharp: run_af.
                   RC16: Restart tonight from now after the current sub
                   (every target start runs AF). Piggy-600: NINA #2 stopped
                   after its sub and the companion re-dispatched (the PS-64
                   Resume path; its image pass starts with AF). Verified on
                   HFR; two AFs that do not help -> a human.
  pointing         plate solves failing (evidence plate_solve_errors >=
                   night_pilot_solve_failures): skip_target, once per
                   night: the goal set inactive for tonight and Restart
                   tonight from now (the planner moves on).
  clouds, seeing   nothing (roof safety owns clouds): a run event only.
  focuser          hardware: one push for a human, nothing sent.

Guardrails (guard()): acts only while the armer is RUNNING (never ARMED,
PAUSED_*, WATCHING a sideloaded night, COMPLETE); never while a calibration
capture job runs, an arm recipe (TPoint mapping, tracking test, optics) is
in progress, NINA #1 runs a TPoint / tracking test / PHD2 calibration /
flat / dark / bias / focus calibration item, or PHD2 calibrates; never
with under RESTART_DARK_MIN of dark left for a re-dispatch;
night_pilot_max_actions (6) per night, one action per rig per
night_pilot_rig_cooldown_min (15); never twice what the sub doctor's own
opt-in remedy just did. It never arms, disarms, parks, unparks, touches the
roof or safety, or changes a PHD2 calibration.

Every decision is a run event (kind "night_pilot": act / advise / held /
human / annotate / verify / retry / failed / learned / revert), every
action and its result one Pushover ("Night pilot: <what> because
<evidence>; next check <when>"), and the morning report has a section.
night_pilot_mode: act (default, Jeremy 2026-10-13) | advise (the same
decisions, pushes "would ...", never a command) | off.

Tonight-only goal changes (sub length overrides, a skipped goal) are kept
in a ledger (<data_dir>/night_pilot/<night>.json) and reverted at the dawn
shutdown (Armer.dawn_shutdown -> dawn_revert), or at the next tick after a
night that ended any other way; a goal changed by hand since is left alone.
What the night taught (e.g. "unguided RC16 needs <= 180 s with this TPoint
model") is a suggestion in the morning report, never applied.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import statistics
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

MODES = ("act", "advise", "off")
KIND = "night_pilot"
DOCTOR_KIND = "sub_doctor"
REMEDY_KIND = "sub_doctor_remedy"
TITLE = "PhotonScript night pilot"
TICK_S = 60
CONF_RANK = {"low": 1, "medium": 2, "high": 3}
RESTART_DARK_MIN = 45.0       # no re-dispatch with less dark left (armer: 40)
RESTART_OVERHEAD_MIN = 6.0    # stop + slew + center + AF before the next sub
RESTART_FOLLOW_MIN = 30.0     # a restart not dispatched by then = failed
JOB_TIMEOUT_MIN = 30.0        # a background job (fallback, Piggy AF) lost
ECC_BETTER = 0.05             # ecc drop that counts as better
HFR_BETTER = 0.10             # relative HFR drop that counts as better
PHD2_ACTIVE = ("guiding", "lostlock", "looping", "selected", "settling",
               "lost_star")
# a running NINA #1 item whose name holds any of these: no action (lower case)
BUSY_WORDS = ("tpoint", "mapping", "tracking test", "phd2_calibration",
              "calibrat", "focus offset", "focus_cal", "through-focus",
              "through focus", "optics", "flat", "dark", "bias")

POLICY = {
    "stop_phd2": {"key": "night_pilot_stop_phd2", "max": 2, "cooldown_min": 30.0,
                  "metric": "ecc", "restart": False},
    "unguided": {"key": "night_pilot_unguided_fallback", "max": 1,
                 "cooldown_min": 0.0, "metric": "ecc", "restart": False},
    "shorten_subs": {"key": "night_pilot_shorten_subs", "max": 2,
                     "cooldown_min": 15.0, "metric": "ecc", "restart": True},
    "run_af": {"key": "night_pilot_af", "max": 2, "cooldown_min": 30.0,
               "metric": "hfr", "restart": True},
    "skip_target": {"key": "night_pilot_skip_target", "max": 1,
                    "cooldown_min": 0.0, "metric": "target", "restart": True},
}
ACTIONS = tuple(POLICY)
# the sub doctor's remedy ids that do what a pilot action does
REMEDY_SAME = {"stop_phd2": "stop_phd2", "run_af": "run_af",
               "shorten_subs": "shorten_subs"}
CAUSE_FAMILY = {"guider_conflict": "guider_conflict", "guider": "guider_conflict",
                "phd2": "guider_conflict", "tracking": "tracking",
                "trailing": "tracking", "trail": "tracking", "drift": "tracking",
                "focus": "focus", "focus_soft": "focus", "soft": "focus",
                "focuser": "focuser", "clouds": "clouds", "cloud": "clouds",
                "transparency": "clouds", "seeing": "seeing",
                "pointing": "pointing", "target_unusable": "pointing",
                "plate_solve": "pointing", "other": "other", "none": "none"}


# --------------------------------------------------------------------------
# config
# --------------------------------------------------------------------------

def mode(cfg) -> str:
    m = str(getattr(cfg, "night_pilot_mode", "act") or "act").strip().lower()
    return m if m in MODES else "act"


def _f(cfg, key: str, default: float) -> float:
    try:
        v = float(getattr(cfg, key, default))
    except (TypeError, ValueError):
        return float(default)
    return v if math.isfinite(v) else float(default)


def _i(cfg, key: str, default: int) -> int:
    try:
        return int(getattr(cfg, key, default))
    except (TypeError, ValueError):
        return int(default)


def action_on(cfg, action: str) -> bool:
    p = POLICY.get(action)
    return bool(p and getattr(cfg, p["key"], True))


def sub_steps(cfg) -> list[float]:
    """night_pilot_sub_steps ("300,180,120") as seconds, longest first."""
    raw = str(getattr(cfg, "night_pilot_sub_steps", "300,180,120") or "")
    out = set()
    for x in raw.replace(";", ",").split(","):
        try:
            v = float(x.strip())
        except ValueError:
            continue
        if math.isfinite(v) and v > 0:
            out.add(v)
    return sorted(out, reverse=True) or [300.0, 180.0, 120.0]


def sub_floor(cfg) -> float:
    return max(1.0, _f(cfg, "night_pilot_sub_floor_s", 120.0))


def next_step(cfg, current_s) -> float | None:
    """The next shorter sub length (one step), None at the floor."""
    try:
        cur = float(current_s)
    except (TypeError, ValueError):
        return None
    fl = sub_floor(cfg)
    lower = [s for s in sub_steps(cfg) if s < cur - 0.5 and s >= fl - 0.5]
    return max(lower) if lower else None


def dark_match(cfg, rig: str, length_s: float) -> dict:
    """Whether a sub length has darks: {"match": matched | scaled | none,
    "text"}. matched: a length the night dark quota fills
    (tracking_drift.dark_lengths, PS-160) or darks at it in the calibration
    library (calibration.darks_have); scaled: no darks, but a usable bias at
    the rig's epoch the darks can be scaled from
    (calibration.newest_usable_bias); none: neither. Never raises."""
    s = float(length_s)
    try:
        from photonscript.scheduler.tracking_drift import dark_lengths
        if any(abs(s - d) < 0.5 for d in dark_lengths(cfg, rig)):
            return {"match": "matched", "text": f"darks match: {s:g} s is in "
                    "the night dark quota"}
    except Exception as e:  # noqa: BLE001
        logger.debug("night pilot: dark lengths unavailable: %s", e)
    try:
        from photonscript.scheduler.calibration import darks_have
        n = darks_have(cfg, rig, s)
        if n:
            return {"match": "matched", "text": f"darks match: {n} {s:g} s "
                    "darks in the library"}
    except Exception as e:  # noqa: BLE001
        logger.debug("night pilot: dark count unavailable: %s", e)
    try:
        from photonscript.scheduler.calibration import newest_usable_bias
        b = newest_usable_bias(cfg, rig)
        if b:
            return {"match": "scaled", "text": f"no {s:g} s darks: scaled "
                    f"from the {b} bias at this epoch"}
    except Exception as e:  # noqa: BLE001
        logger.debug("night pilot: bias lookup unavailable: %s", e)
    return {"match": "none", "text": f"no {s:g} s darks and no bias at this "
            "epoch: darks owed"}


def pick_step(cfg, current_s, status=None, rig: str = "rc16") -> tuple[float | None, dict | None]:
    """The shorten step: of the ladder lengths below current_s and not
    under the floor, the longest one with matched darks, else the longest
    whose darks can be scaled from a bias, else the next step. (seconds,
    dark_match info), (None, None) at the floor. status: test hook
    (seconds -> dark_match dict)."""
    try:
        cur = float(current_s)
    except (TypeError, ValueError):
        return None, None
    fl = sub_floor(cfg)
    cands = [s for s in sub_steps(cfg) if s < cur - 0.5 and s >= fl - 0.5]
    if not cands:
        return None, None
    status = status or (lambda x: dark_match(cfg, rig, x))
    info = {}
    for want in ("matched", "scaled"):
        for s in cands:          # longest first
            if s not in info:
                info[s] = status(s) or {"match": "none", "text": ""}
            if info[s].get("match") == want:
                return s, info[s]
    s = cands[0]
    return s, info.get(s) or status(s)


def limits(cfg) -> dict:
    return {"max_actions": max(0, _i(cfg, "night_pilot_max_actions", 6)),
            "rig_cooldown_min": max(0.0, _f(cfg, "night_pilot_rig_cooldown_min", 15.0)),
            "verify_subs": max(1, _i(cfg, "night_pilot_verify_subs", 2)),
            "verify_timeout_min": max(5.0, _f(cfg, "night_pilot_verify_timeout_min", 60.0)),
            "solve_failures": max(1, _i(cfg, "night_pilot_solve_failures", 3)),
            "min_confidence": _conf(getattr(cfg, "night_pilot_min_confidence", "medium"))
            or "medium"}


# --------------------------------------------------------------------------
# the sub doctor adapter
# --------------------------------------------------------------------------

def _conf(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        x = float(v)
        return "high" if x >= 0.8 else ("medium" if x >= 0.5 else "low")
    s = str(v).strip().lower()
    return s if s in CONF_RANK else None


def _parse_t(v) -> datetime | None:
    from photonscript.shared.sub_time import parse_utc
    if isinstance(v, datetime):
        return v.replace(tzinfo=None) if v.tzinfo is None else \
            parse_utc(v.isoformat())
    return parse_utc(v)


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def norm_diag(row: dict) -> dict | None:
    """One sub doctor run event -> {t, dt, rig, cause, family, confidence,
    action, action_arg, evidence, headline, secondary}. None when it is not
    one. suggested_action may be {"id": "shorten_subs:180", ...} or the bare
    id; confidence high | medium | low or 0..1."""
    if not isinstance(row, dict) or row.get("kind") != DOCTOR_KIND:
        return None
    dt = _parse_t(row.get("t"))
    if dt is None:
        return None
    cause = str(row.get("cause") or row.get("value") or "none").strip().lower()
    sa = row.get("suggested_action")
    aid = sa.get("id") if isinstance(sa, dict) else sa
    aid = str(aid or "none").strip().lower()
    name, _, arg = aid.partition(":")
    ev = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
    return {"t": _iso(dt), "dt": dt, "rig": str(row.get("rig") or "rc16"),
            "cause": cause, "family": CAUSE_FAMILY.get(cause, "other"),
            "confidence": _conf(row.get("confidence")) or "low",
            "action": name, "action_arg": arg or None, "evidence": ev,
            "headline": str(row.get("headline") or cause),
            "secondary": list(row.get("secondary") or [])}


def _num(v):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _cal_bad(ev: dict) -> bool:
    cal = ev.get("calibration")
    if isinstance(cal, dict):
        return str(cal.get("grade") or "").upper() == "FAIL"
    return str(cal or ev.get("calibration_grade") or "").upper() == "FAIL"


def _drift_measured(ev: dict) -> bool:
    trail = ev.get("trail") if isinstance(ev.get("trail"), dict) else {}
    return bool(trail.get("rate_arcsec_min") or ev.get("drift_cap")
                or ev.get("rate_arcsec_min"))


# --------------------------------------------------------------------------
# the policy: cause -> decision
# --------------------------------------------------------------------------

def decide(cfg, d: dict, ctx: dict) -> dict:
    """{"kind": act | human | annotate, "action", "params", "what", "why"}
    for one diagnosis. ctx: guided (bool), phd2 (PHD2 app state or None),
    exp_s (the rig's current sub length), target, piggy (bool)."""
    fam, rig, ev = d["family"], d["rig"], d.get("evidence") or {}
    lim = limits(cfg)
    rank = CONF_RANK.get(d.get("confidence") or "low", 1)
    low = rank < CONF_RANK[lim["min_confidence"]]

    def out(kind, action=None, what="", why="", **params):
        return {"kind": kind, "action": action, "params": params,
                "what": what, "why": why}

    if fam in ("clouds", "seeing"):
        return out("annotate", why=f"{fam}: nothing to change (roof safety "
                                   "owns clouds; seeing is the sky)")
    if fam in ("none", "other"):
        return out("annotate", why="no cause the pilot can act on")
    if low:
        return out("annotate", why=f"{d.get('confidence')} confidence: "
                                   "watching only")
    if fam == "focuser":
        return out("human", what="check the focuser (USB / power), then AF",
                   why="a focuser problem is hardware: the pilot sends nothing")
    if fam == "guider_conflict":
        phd2 = str(ctx.get("phd2") or "").strip().lower().replace(" ", "")
        guided = bool(ctx.get("guided"))
        if not guided:
            if ctx.get("phd2") is not None and phd2 not in PHD2_ACTIVE:
                return out("annotate", why=f"PHD2 already {ctx.get('phd2')}")
            return out("act", "stop_phd2", what="stopped PHD2 guiding",
                       why="PHD2 is active on an UNGUIDED night")
        if _cal_bad(ev) or d.get("action") == "recalibrate_phd2":
            return out("act", "unguided", what="switched the rest of tonight "
                       "to UNGUIDED (PHD2 calibration FAIL)",
                       why="guided night on a FAIL PHD2 calibration")
        if d.get("action") == "reselect_guide_star":
            return out("human", what="reselect the PHD2 guide star",
                       why="non-star lock on a good calibration: the PS-91 "
                           "guard owns that recovery")
        return out("human", what="look at PHD2",
                   why="guided night, PHD2 not guiding: NINA's next "
                       "StartGuiding or a human")
    if fam == "tracking":
        if rig != "rc16":
            return out("human", what="check the Piggy-600 subs",
                       why="the pilot only changes RC16 sub lengths")
        if ctx.get("guided"):
            return out("human", what="check the guiding",
                       why="guided subs trailing: shorter subs are not the fix")
        if not (ev.get("shared_mount") or _drift_measured(ev)):
            return out("annotate", why="trailing without a shared-mount or "
                                       "drift measurement")
        cur = _num(ctx.get("exp_s")) or _num(ev.get("exp_s"))
        nxt, dk = pick_step(cfg, cur, ctx.get("dark_status"))
        if nxt is None:
            return out("human", what="look at the RC16 tracking",
                       why=f"subs already at {cur or '?'} s, the floor is "
                           f"{sub_floor(cfg):g} s")
        return out("act", "shorten_subs",
                   what=f"shortened RC16 subs {cur:g} -> {nxt:g} s",
                   why="RC16 trailing" + (" on both rigs (shared mount)"
                                          if ev.get("shared_mount") else
                                          " with a measured drift"),
                   from_s=cur, to_s=nxt, darks=(dk or {}).get("match"),
                   darks_text=(dk or {}).get("text"))
    if fam == "focus":
        if ev.get("other_rig_sharp") is False:
            return out("annotate", why="soft on both rigs: seeing, not focus")
        if rig != "rc16" and not ctx.get("piggy"):
            return out("annotate", why="Piggy-600 not enabled")
        return out("act", "run_af", what=f"asked for an autofocus on the "
                   f"{_label(cfg, rig)}", why="soft round stars on this rig only")
    if fam == "pointing":
        n = _num(ev.get("plate_solve_errors")) or 0
        if rig == "rc16" and n >= lim["solve_failures"]:
            return out("act", "skip_target", what="skipped to the next planned "
                       "target", why=f"{n:g} plate solves failed",
                       target=ctx.get("target"))
        return out("annotate", why="off target: the off-target alert owns it")
    return out("annotate", why="unknown cause")


def _label(cfg, rig: str) -> str:
    try:
        from photonscript.shared.rigs import rig_label
        return rig_label(cfg, rig)
    except Exception:  # noqa: BLE001
        return rig


# --------------------------------------------------------------------------
# state (one file per night)
# --------------------------------------------------------------------------

def state_dir(cfg) -> Path:
    return Path(getattr(cfg, "data_dir", ".")) / "night_pilot"


def state_path(cfg, night: str) -> Path:
    return state_dir(cfg) / f"{night}.json"


def load_state(cfg, night: str) -> dict:
    try:
        st = json.loads(state_path(cfg, night).read_text(encoding="utf-8"))
        if isinstance(st, dict):
            st.setdefault("night", night)
            return st
    except (OSError, ValueError):
        pass
    return {"night": night, "seen_t": None, "actions": [], "ledger": {},
            "given_up": {}, "notes": [], "learned": [], "seq": 0}


def save_state(cfg, st: dict) -> None:
    p = state_path(cfg, st["night"])
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1, default=str), encoding="utf-8")
        tmp.replace(p)
    except OSError as e:
        logger.warning("night pilot state not saved: %s", e)


def _event(cfg, night: str, value: str, detail: str, now: datetime,
           rig: str = "rc16", **extra) -> None:
    try:
        from photonscript.shared.night_events import events_path
        from photonscript.shared.phd2_store import append_jsonl
        append_jsonl(events_path(cfg, night),
                     {"t": _iso(now), "rig": rig, "src": "photonscript",
                      "kind": KIND, "value": value, "detail": detail, **extra})
    except Exception as e:  # noqa: BLE001
        logger.warning("night pilot event (%s) not logged: %s", value, e)


def _hm(cfg, t: datetime | None) -> str:
    try:
        from photonscript.scheduler.nina_watchdog import _local_hm
        return _local_hm(cfg, t)
    except Exception:  # noqa: BLE001
        return t.strftime("%H:%MZ") if t else "?"


# --------------------------------------------------------------------------
# I/O (every command and read the pilot makes; tests replace it)
# --------------------------------------------------------------------------

class PilotIO:
    """The real scope PC. Every method never raises."""

    async def nina1_running(self, cfg) -> tuple[list[str] | None, str | None]:
        """(names of NINA #1's RUNNING items, the running target)."""
        try:
            from photonscript.scheduler.nina_watchdog import rig_base
            from photonscript.scheduler.sideload import nina_running, read_sequence_state
            from photonscript.scheduler.where_panel import sequence_position
            tree, _err = await asyncio.wait_for(
                read_sequence_state(rig_base(cfg, "rc16")), 15)
        except Exception:  # noqa: BLE001
            return None, None
        if tree is None:
            return None, None
        try:
            return nina_running(tree), sequence_position(tree).get("target")
        except Exception:  # noqa: BLE001
            return None, None

    def _rpc(self, cfg):
        from photonscript.scheduler.guide_recover import Phd2Rpc
        return Phd2Rpc(str(getattr(cfg, "phd2_host", "localhost")),
                       int(getattr(cfg, "phd2_port", 4400)), timeout=8.0)

    async def phd2_state(self, cfg) -> str | None:
        def _read():
            with self._rpc(cfg) as c:
                return c.call("get_app_state")
        try:
            v = await asyncio.wait_for(asyncio.to_thread(_read), 20)
        except Exception:  # noqa: BLE001
            return None
        return str(v) if v is not None else None

    async def phd2_stop(self, cfg) -> tuple[bool, str]:
        """PHD2 stop_capture (the guide-recover client), then wait up to
        ~15 s for PHD2 to read Stopped. Never touches the calibration."""
        import time

        def _stop():
            with self._rpc(cfg) as c:
                c.call("stop_capture")
                st = None
                for _ in range(8):
                    time.sleep(2.0)
                    st = c.call("get_app_state")
                    if str(st).lower() == "stopped":
                        break
                return st
        try:
            st = await asyncio.wait_for(asyncio.to_thread(_stop), 60)
        except Exception as e:  # noqa: BLE001
            return False, f"PHD2 stop_capture failed: {e}"
        ok = str(st).lower() == "stopped"
        return ok, f"PHD2 now {st}"

    async def nina1_wait_idle(self, cfg, max_s: float = 300.0) -> bool | None:
        """Poll NINA #1 until nothing RUNNING (the restart race)."""
        polls = max(1, int(max_s / 3.0))
        misses = 0
        for _ in range(polls):
            names, _t = await self.nina1_running(cfg)
            if names is None:
                misses += 1
                if misses >= 3:
                    return None
            elif not names:
                return True
            await asyncio.sleep(3.0)
        return False

    async def piggy_af(self, cfg, armer) -> tuple[bool, str]:
        """Piggy-600 AF: stop NINA #2 after its sub (PS-64 night_pause),
        wait until it reads idle, re-dispatch the companion (its image pass
        starts with AF), as Resume does after a Piggy pause."""
        from photonscript.scheduler import night_pause as np_
        from photonscript.scheduler.sideload import nina_running, read_sequence_state
        from photonscript.shared.rigs import PIGGYBACK, rig_config
        try:
            base = rig_config(cfg, PIGGYBACK).nina_base_url
        except Exception as e:  # noqa: BLE001
            return False, f"no Piggy-600 NINA: {e}"
        res = await np_.stop_after_exposure(lambda: np_.camera_info(base),
                                            lambda: np_.sequence_stop(base))
        if not res.get("ok"):
            return False, "NINA #2 " + np_.describe(res)
        for _ in range(60):
            try:
                tree, _e = await read_sequence_state(base)
            except Exception:  # noqa: BLE001
                tree = None
            if tree is not None and not nina_running(tree):
                break
            await asyncio.sleep(3.0)
        try:
            await armer._dispatch_piggyback_companion()
        except Exception as e:  # noqa: BLE001
            return False, f"companion re-dispatch failed: {e}"
        return True, "NINA #2 " + np_.describe(res) + "; companion re-dispatched"

    def store(self):
        from photonscript.scheduler.app import get_store
        return get_store()

    def subs(self, cfg, night: str) -> list[dict]:
        try:
            from photonscript.scheduler.runs import _load_subs
            return _load_subs(cfg, night)
        except Exception:  # noqa: BLE001
            return []

    def events(self, cfg, night: str) -> list[dict]:
        try:
            from photonscript.shared import phd2_store as store
            from photonscript.shared.night_events import events_path
            return store.read_jsonl(events_path(cfg, night))
        except Exception:  # noqa: BLE001
            return []

    async def notify(self, cfg, msg: str, priority: int = 0) -> None:
        try:
            from photonscript.shared.pushover import notify
            await notify(cfg, msg, title=TITLE, priority=priority)
        except Exception as e:  # noqa: BLE001
            logger.warning("night pilot push failed: %s", e)


class Monitor:
    """In-process: background jobs (fallback, Piggy AF) and the last tick."""

    def __init__(self):
        self.jobs: dict[str, asyncio.Task] = {}
        self.last: dict = {}


MONITOR = Monitor()


# --------------------------------------------------------------------------
# guards
# --------------------------------------------------------------------------

def _dark_left_min(armer, now: datetime) -> float | None:
    try:
        return (armer._dawn() - now).total_seconds() / 60.0
    except Exception:  # noqa: BLE001
        return None


def _recipe_busy(armer) -> str | None:
    try:
        r = armer._recipe_tonight()
    except Exception:  # noqa: BLE001
        r = None
    if isinstance(r, dict) and r.get("status") in ("dispatched", "started"):
        return str(r.get("container") or r.get("name") or "an arm recipe")
    return None


def _calibration_busy() -> str | None:
    try:
        from photonscript.scheduler.calibration_capture import busy
        for rig in ("rc16", "piggyback"):
            if busy(rig):
                return rig
    except Exception:  # noqa: BLE001
        return None
    return None


def live_guard(cfg, armer, ctx: dict) -> str | None:
    """The part of guard() that does not depend on the action: armer state,
    calibration capture, arm recipe, NINA #1's running items, PHD2
    calibrating. Why not now, else None."""
    from photonscript.scheduler.armer import WATCH_STATE
    state = str(getattr(armer, "state", "") or "")
    if state == WATCH_STATE:
        return "watching a sideloaded night: the pilot never acts on it"
    if state != "RUNNING":
        return f"armer is {state or 'unknown'}: acts only while RUNNING"
    cal = _calibration_busy()
    if cal:
        return f"a calibration capture job runs on the {cal}"
    rec = _recipe_busy(armer)
    if rec:
        return f"the arm recipe {rec} is in progress"
    names = ctx.get("running")
    tgt = str(ctx.get("target") or "").strip().lower()
    for n in names or []:
        low = str(n).lower()
        if tgt and low.strip() == tgt:
            continue   # a target named "Dark Shark" is not a darks block
        if any(w in low for w in BUSY_WORDS):
            return f"NINA #1 runs '{n}'"
    if str(ctx.get("phd2") or "").lower() == "calibrating":
        return "PHD2 is calibrating"
    return None


def guard(cfg, armer, st: dict, d: dict, dec: dict, ctx: dict,
          now: datetime) -> str | None:
    """Why this action must not run now, else None."""
    why = live_guard(cfg, armer, ctx)
    if why:
        return why
    action, rig = dec["action"], d["rig"]
    pol = POLICY[action]
    lim = limits(cfg)
    if pol["restart"] or action == "unguided":
        left = _dark_left_min(armer, now)
        if left is not None and left < RESTART_DARK_MIN:
            return f"only {max(0, int(left))} min of dark left"
    acts = [a for a in st.get("actions") or [] if a.get("kind") == "act"]
    if len(acts) >= lim["max_actions"]:
        return f"{len(acts)} actions tonight (max {lim['max_actions']})"
    same = [a for a in acts if a.get("action") == action]
    if len(same) >= pol["max"]:
        return f"{action} done {len(same)} time(s) tonight (max {pol['max']})"
    pend = [a for a in acts if a.get("rig") == rig and a.get("result") is None
            and a.get("status") in ("pending", "running", "done")]
    if pend:
        return f"waiting for the {pend[-1]['action']} check on the {_label(cfg, rig)}"
    for a in acts:
        t = _parse_t(a.get("t"))
        if t is None:
            continue
        age = (now - t).total_seconds() / 60.0
        if a.get("rig") == rig and age < lim["rig_cooldown_min"]:
            return (f"one action per rig per {lim['rig_cooldown_min']:g} min "
                    f"(last {age:.0f} min ago)")
        if a.get("action") == action and age < pol["cooldown_min"]:
            return f"{action} cooldown ({pol['cooldown_min']:g} min)"
    if st.get("given_up", {}).get(f"{rig}|{d['family']}"):
        return f"gave up on {d['family']} tonight (pushed for a human)"
    rem = REMEDY_SAME.get(action)
    for r in ctx.get("remedies") or []:
        t = _parse_t(r.get("t"))
        if (rem and r.get("rig") == rig and str(r.get("value") or "").split(":")[0] == rem
                and t is not None and (now - t).total_seconds() / 60.0 < pol["cooldown_min"] + 15):
            return f"the sub doctor already ran {rem} at {_hm(cfg, t)}"
    return None


# --------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------

def _sub_start(r: dict) -> datetime | None:
    try:
        from photonscript.shared.sub_time import sub_start
        return sub_start(r)
    except Exception:  # noqa: BLE001
        return _parse_t(r.get("start_utc") or r.get("time"))


def _light(r: dict) -> bool:
    parts = str(r.get("file") or "").replace("\\", "/").upper().split("/")
    if any(p in ("FLAT", "FLATS", "DARK", "DARKS", "BIAS", "BIASES") for p in parts):
        return False
    return _num(r.get("exp_s") or r.get("exposure")) not in (None, 0.0)


def rig_subs(subs: list[dict], rig: str) -> list[dict]:
    out = [r for r in subs or [] if (r.get("rig") or "rc16") == rig and _light(r)
           and _sub_start(r) is not None]
    out.sort(key=_sub_start)
    return out


def _metric(r: dict, metric: str):
    if metric == "ecc":
        return _num(r.get("ecc"))
    if metric == "hfr":
        return _num(r.get("hfr"))
    return None


def baseline(d: dict, metric: str, subs: list[dict]) -> float | None:
    ev = d.get("evidence") or {}
    v = _num(ev.get("ecc") if metric == "ecc" else ev.get("hfr_px") or ev.get("hfr"))
    if v is not None:
        return v
    vals = [_metric(r, metric) for r in rig_subs(subs, d["rig"])
            if _sub_start(r) <= d["dt"]][-3:]
    vals = [x for x in vals if x is not None]
    return round(statistics.median(vals), 3) if vals else None


def judge(metric: str, before: float | None, after: list[dict]) -> tuple[bool, float | None]:
    """(improved, the median after) over the verification subs."""
    vals = [_metric(r, metric) for r in after]
    vals = [x for x in vals if x is not None]
    med = round(statistics.median(vals), 3) if vals else None
    passed = bool(after) and all(r.get("passed_qa") is True for r in after)
    if med is None or before is None:
        return passed, med
    if metric == "ecc":
        return passed or med <= before - ECC_BETTER, med
    return passed or med <= before * (1.0 - HFR_BETTER), med


def verify_pool(a: dict, subs: list[dict]) -> list[dict]:
    eff = _parse_t(a.get("effective_t"))
    if eff is None:
        return []
    pool = [r for r in rig_subs(subs, a["rig"]) if _sub_start(r) >= eff]
    if a["action"] == "shorten_subs":
        to = _num((a.get("params") or {}).get("to_s")) or 0
        pool = [r for r in pool if (_num(r.get("exp_s") or r.get("exposure")) or 0) <= to + 1]
    if a["action"] == "skip_target":
        old = str((a.get("params") or {}).get("target") or "").lower()
        pool = [r for r in pool if str(r.get("target") or "").lower() != old]
    return pool


def _next_check(cfg, a: dict, now: datetime) -> datetime:
    exp = _num((a.get("params") or {}).get("to_s")) or _num(a.get("exp_s")) or 300.0
    n = limits(cfg)["verify_subs"]
    lead = RESTART_OVERHEAD_MIN * 60 + exp if POLICY[a["action"]]["restart"] else exp
    return now + timedelta(seconds=lead + n * exp)


# --------------------------------------------------------------------------
# tonight-only goal changes (ledger) and the dawn revert
# --------------------------------------------------------------------------

def _rc16_plans(proj) -> list:
    return [p for p in getattr(proj, "exposure_plans", []) or []
            if getattr(p, "rig", "rc16") == "rc16"]


def _ledger_entry(st: dict, proj) -> dict:
    led = st.setdefault("ledger", {})
    e = led.get(proj.id)
    if e is None:
        e = led[proj.id] = {
            "target": proj.target.name,
            "original_overrides": dict(proj.exposure_overrides or {}) or None,
            "original_active": bool(proj.active),
            "applied_overrides": None, "applied_active": None,
            "changes": [], "reverted": False}
    return e


def apply_sub_cap(st: dict, store, target: str, to_s: float) -> tuple[bool, str]:
    """Cap every RC16 filter of the target's goal at to_s via exposure
    overrides (tonight only, ledgered). (ok, detail)."""
    from photonscript.scheduler.integrations import resolve_project
    proj = resolve_project(target, store.projects.values()) if target else None
    if proj is None:
        return False, f"no goal for target {target!r}"
    new = dict(proj.exposure_overrides or {})
    changed = []
    for p in _rc16_plans(proj):
        fv = p.filter_type.value
        cur = float(new.get(fv) or p.exposure_seconds)
        if cur > to_s + 0.5:
            new[fv] = float(to_s)
            changed.append(f"{fv} {cur:g} -> {to_s:g} s")
    if not changed:
        return False, f"{proj.target.name}: no RC16 filter longer than {to_s:g} s"
    e = _ledger_entry(st, proj)
    store.update(proj.id, exposure_overrides=new)
    e["applied_overrides"] = dict(new)
    e["changes"].append({"what": "sub cap", "to_s": to_s, "filters": changed})
    e["reverted"] = False
    return True, f"{proj.target.name}: " + ", ".join(changed)


def apply_skip(st: dict, store, target: str) -> tuple[bool, str]:
    from photonscript.scheduler.integrations import resolve_project
    proj = resolve_project(target, store.projects.values()) if target else None
    if proj is None:
        return False, f"no goal for target {target!r}"
    e = _ledger_entry(st, proj)
    store.update(proj.id, active=False)
    e["applied_active"] = False
    e["changes"].append({"what": "skipped tonight"})
    e["reverted"] = False
    return True, f"{proj.target.name} inactive for tonight"


def _norm_ov(v) -> dict:
    return {str(k): float(x) for k, x in (v or {}).items() if x}


def undo_sub_cap(st: dict, store, a: dict) -> tuple[bool, str]:
    """Undo one shorten step that did not help: the goal's sub lengths go
    back to what they were before it (the previous step's cap, or the goal
    defaults). A goal edited by hand since is left alone. (ok, detail)."""
    from photonscript.scheduler.integrations import resolve_project
    p = a.get("params") or {}
    target = p.get("target")
    proj = resolve_project(target, store.projects.values()) if target else None
    if proj is None:
        return False, f"no goal for target {target!r}"
    e = (st.get("ledger") or {}).get(proj.id)
    if e is None or e.get("applied_overrides") is None:
        return False, f"{proj.target.name}: no sub cap of tonight to undo"
    if _norm_ov(proj.exposure_overrides) != _norm_ov(e["applied_overrides"]):
        return False, (f"{proj.target.name}: sub lengths changed by hand "
                       "since: left as they are")
    prev = dict(p.get("prev_overrides") or {})
    store.update(proj.id, exposure_overrides=prev)
    e["applied_overrides"] = dict(prev)
    e["changes"].append({"what": "sub cap undone", "to_s": p.get("from_s")})
    back = (", ".join(f"{k} {float(v):g} s" for k, v in prev.items()) if prev
            else "the goal defaults")
    return True, f"{proj.target.name}: sub lengths back to {back}"


def revert(cfg, st: dict, store, now: datetime | None = None,
           why: str = "dawn") -> list[str]:
    """Undo the night's goal changes (sub length overrides, skipped goals).
    A goal changed by hand since the pilot changed it is left alone and
    said so. Returns one text per goal."""
    now = now or datetime.utcnow()
    out = []
    for pid, e in (st.get("ledger") or {}).items():
        if e.get("reverted"):
            continue
        proj = store.projects.get(pid)
        if proj is None:
            e["reverted"] = True
            out.append(f"{e.get('target')}: goal gone, nothing to restore")
            continue
        bits = []
        if e.get("applied_overrides") is not None:
            if _norm_ov(proj.exposure_overrides) == _norm_ov(e["applied_overrides"]):
                store.update(pid, exposure_overrides=dict(e.get("original_overrides") or {}))
                orig = e.get("original_overrides")
                bits.append("sub lengths restored to "
                            + (", ".join(f"{k} {v:g} s" for k, v in orig.items())
                               if orig else "the goal defaults"))
            else:
                bits.append("sub lengths changed by hand since: left as they are")
        if e.get("applied_active") is False:
            if proj.active is False and e.get("original_active", True):
                store.update(pid, active=True)
                bits.append("active again")
            elif proj.active is not False:
                bits.append("already active again")
        e["reverted"] = True
        e["reverted_t"] = _iso(now)
        txt = f"{e.get('target')}: " + ("; ".join(bits) or "nothing to restore")
        out.append(txt)
        _event(cfg, st["night"], "revert", txt, now, why=why, project=pid)
    return out


def dawn_revert(cfg, night: str | None, store=None, now: datetime | None = None) -> str | None:
    """Armer.dawn_shutdown hook: revert tonight's ledger. A step text for the
    shutdown report, None when there was nothing to revert. Never raises."""
    if not night:
        return None
    try:
        st = load_state(cfg, night)
        if not any(not e.get("reverted") for e in (st.get("ledger") or {}).values()):
            return None
        store = store or PilotIO().store()
        done = revert(cfg, st, store, now, why="dawn shutdown")
        save_state(cfg, st)
        return ("night pilot restored " + "; ".join(done)) if done else None
    except Exception as e:  # noqa: BLE001
        logger.warning("night pilot dawn revert failed: %s", e)
        return "night pilot restore FAILED (see the log)"


def revert_stale(cfg, tonight: str | None, store, now: datetime | None = None) -> list[str]:
    """Revert any earlier night's ledger the dawn shutdown did not (a
    disarm, a crash, an error state at dawn)."""
    out = []
    d = state_dir(cfg)
    if not d.is_dir():
        return out
    for p in sorted(d.glob("*.json")):
        night = p.stem
        if night == tonight:
            continue
        st = load_state(cfg, night)
        if any(not e.get("reverted") for e in (st.get("ledger") or {}).values()):
            out += revert(cfg, st, store, now, why="night over")
            save_state(cfg, st)
    return out


# --------------------------------------------------------------------------
# the tick
# --------------------------------------------------------------------------

def _night(cfg, armer, now: datetime) -> str:
    plan = getattr(armer, "plan", None)
    n = plan.get("night_of") if isinstance(plan, dict) else None
    if n:
        return n
    from photonscript.shared.phd2_store import night_of
    return night_of(cfg, now)


def _note_once(st: dict, key: str) -> bool:
    notes = st.setdefault("notes", [])
    if key in notes:
        return False
    notes.append(key)
    return True


def _because(d: dict) -> str:
    h = str(d.get("headline") or d.get("cause") or "?")
    return h if len(h) <= 220 else h[:217] + "..."


async def gather_ctx(cfg, armer, io, night: str, events: list[dict],
                     live: bool = True) -> dict:
    """What the decisions need. live=False (no new diagnosis this tick)
    skips the NINA #1 and PHD2 reads: verification needs only the logs."""
    running, target = (await io.nina1_running(cfg)) if live else (None, None)
    try:
        guided = bool(armer._use_guiding())
    except Exception:  # noqa: BLE001
        guided = bool(getattr(cfg, "guided_default", False))
    subs = io.subs(cfg, night)
    rc = rig_subs(subs, "rc16")
    exp_s = _num(rc[-1].get("exp_s") or rc[-1].get("exposure")) if rc else None
    last_target = rc[-1].get("target") if rc else None
    return {"running": running, "target": target or last_target,
            "last_target": last_target, "guided": guided,
            "phd2": (await io.phd2_state(cfg)) if live else None,
            "exp_s": exp_s, "subs": subs,
            "events": events, "piggy": bool(getattr(cfg, "piggyback_enabled", False)),
            "remedies": [r for r in events if r.get("kind") == REMEDY_KIND and r.get("ok")]}


async def tick(cfg, armer, io: PilotIO | None = None, now: datetime | None = None) -> dict:
    """One pass: follow up restarts and jobs, verify past actions, then act
    on (or advise about) new diagnoses. Returns a small summary."""
    io = io or PilotIO()
    now = now or datetime.utcnow()
    m = mode(cfg)
    if m == "off":
        return {"state": "off"}
    night = _night(cfg, armer, now)
    try:
        revert_stale(cfg, night, io.store(), now)
    except Exception as e:  # noqa: BLE001
        logger.warning("night pilot stale revert failed: %s", e)
    st = load_state(cfg, night)
    events = io.events(cfg, night)
    diags = {}
    seen = _parse_t(st.get("seen_t"))
    for row in events:
        d = norm_diag(row)
        if d is None or (seen is not None and d["dt"] <= seen):
            continue
        prev = diags.get(d["rig"])
        if prev is None or d["dt"] >= prev["dt"]:
            diags[d["rig"]] = d
    ctx = await gather_ctx(cfg, armer, io, night, events, live=bool(diags))
    await _follow_up(cfg, armer, io, st, ctx, now)
    await _verify(cfg, armer, io, st, ctx, now)
    # night ended without the dawn shutdown (disarmed, error): revert now
    from photonscript.scheduler.armer import LIVE_STATES
    if str(getattr(armer, "state", "")) not in LIVE_STATES and \
            any(not e.get("reverted") for e in (st.get("ledger") or {}).values()):
        revert(cfg, st, io.store(), now, why=f"armer {armer.state}")
    newest = max((d["dt"] for d in diags.values()), default=None)
    done = []
    for d in sorted(diags.values(), key=lambda x: x["dt"]):
        done.append(await _handle(cfg, armer, io, st, d, ctx, now, m))
    if newest is not None:
        st["seen_t"] = _iso(newest)
    save_state(cfg, st)
    MONITOR.last = {"t": _iso(now), "mode": m, "night": night, "handled": done}
    return {"state": m, "night": night, "handled": done}


async def _handle(cfg, armer, io, st, d, ctx, now, m) -> dict:
    dec = decide(cfg, d, ctx)
    rig, fam = d["rig"], d["family"]
    night = st["night"]
    if dec["kind"] == "annotate":
        if _note_once(st, f"annotate|{rig}|{fam}|{d['cause']}"):
            _event(cfg, night, "annotate", f"{_label(cfg, rig)} {d['cause']}: "
                   f"{dec['why']}", now, rig=rig, cause=d["cause"])
        return {"rig": rig, "kind": "annotate", "cause": d["cause"]}
    if dec["kind"] == "human":
        if _note_once(st, f"human|{rig}|{fam}"):
            msg = (f"Night pilot: needs a human: {dec['what']} because "
                   f"{_because(d)} ({dec['why']}). Nothing was sent.")
            _event(cfg, night, "human", msg, now, rig=rig, cause=d["cause"])
            await io.notify(cfg, msg, priority=0)
        return {"rig": rig, "kind": "human", "cause": d["cause"]}
    action = dec["action"]
    why_not = guard(cfg, armer, st, d, dec, ctx, now)
    if why_not:
        if _note_once(st, f"held|{rig}|{action}|" + re.sub(r"\d+", "#", why_not)):
            _event(cfg, night, "held", f"{action} on the {_label(cfg, rig)} held: "
                   f"{why_not}", now, rig=rig, cause=d["cause"], action=action)
        return {"rig": rig, "kind": "held", "action": action, "why": why_not}
    advise = m == "advise" or not action_on(cfg, action)
    st["seq"] = int(st.get("seq") or 0) + 1
    pol = POLICY[action]
    a = {"id": f"a{st['seq']}", "t": _iso(now), "rig": rig, "cause": d["cause"],
         "family": fam, "action": action, "what": dec["what"],
         "because": _because(d), "params": dec["params"],
         "exp_s": ctx.get("exp_s"), "kind": "advise" if advise else "act",
         "baseline": {"metric": pol["metric"],
                      "value": baseline(d, pol["metric"], ctx["subs"])},
         "status": None, "result": None, "effective_t": None, "detail": ""}
    if advise:
        why = ("night_pilot_mode=advise" if m == "advise"
               else f"{pol['key']} is off")
        a["status"] = "advised"
        if _note_once(st, f"advise|{rig}|{action}"):
            st.setdefault("actions", []).append(a)
            dk = (dec.get("params") or {}).get("darks_text")
            msg = (f"Night pilot (advise): would have {dec['what']} because "
                   f"{a['because']}" + (f" ({dk})" if dk else "")
                   + f" ({why}; nothing was sent).")
            _event(cfg, night, "advise", msg, now, rig=rig, action=action,
                   cause=d["cause"], aid=a["id"])
            await io.notify(cfg, msg, priority=0)
        return {"rig": rig, "kind": "advise", "action": action}
    st.setdefault("actions", []).append(a)
    await _execute(cfg, armer, io, st, a, ctx, now)
    nxt = _next_check(cfg, a, now)
    a["next_check"] = _iso(nxt)
    ok = a["status"] in ("pending", "running", "done")
    if ok:
        msg = (f"Night pilot: {a['what']} because {a['because']}"
               + (f" ({a['detail']})" if a.get("detail") else "")
               + f"; next check {_hm(cfg, nxt)} local.")
        _event(cfg, night, "act", msg, now, rig=rig, action=action,
               cause=d["cause"], aid=a["id"], params=a["params"],
               baseline=a["baseline"], status=a["status"])
        await io.notify(cfg, msg, priority=0)
    else:
        await _fail(cfg, io, st, a, now, a.get("detail") or "failed")
    return {"rig": rig, "kind": "act", "action": action, "status": a["status"]}


async def _execute(cfg, armer, io, st, a, ctx, now) -> None:
    action = a["action"]
    if action == "stop_phd2":
        ok, detail = await io.phd2_stop(cfg)
        a.update(status="done" if ok else "failed", detail=detail,
                 effective_t=_iso(now) if ok else None)
        return
    if action == "unguided":
        _job(a, armer.fallback_unguided(f"night pilot: {a['because']}"))
        a.update(status="running", detail="re-dispatching the remainder unguided")
        return
    if action == "run_af" and a["rig"] != "rc16":
        _job(a, io.piggy_af(cfg, armer))
        a.update(status="running", detail="NINA #2 stops after its sub, then "
                 "the companion (AF first) is re-dispatched")
        return
    target = _goal_target(io.store(), (a["params"] or {}).get("target"),
                          ctx.get("target"), ctx.get("last_target"))
    if action == "shorten_subs":
        prev = _goal_overrides(io.store(), target)
        ok, detail = apply_sub_cap(st, io.store(), target, a["params"]["to_s"])
        if not ok:
            a.update(status="failed", detail=detail)
            return
        a["params"]["target"] = target
        a["params"]["prev_overrides"] = prev    # what an undo goes back to
        a["detail"] = detail + " for tonight"
        if a["params"].get("darks_text"):
            a["detail"] += "; " + a["params"]["darks_text"]
    elif action == "skip_target":
        ok, detail = apply_skip(st, io.store(), target)
        if not ok:
            a.update(status="failed", detail=detail)
            return
        a["params"]["target"] = target
        a["detail"] = detail
    await _restart(cfg, armer, a, now)


def _goal_overrides(store, target) -> dict | None:
    """The goal's exposure overrides now (before a sub cap), None for none."""
    from photonscript.scheduler.integrations import resolve_project
    try:
        proj = resolve_project(target, store.projects.values()) if target else None
    except Exception:  # noqa: BLE001
        proj = None
    return (dict(proj.exposure_overrides or {}) or None) if proj is not None else None


def _goal_target(store, *names) -> str | None:
    """The first name that resolves to a goal (NINA #1's running target can
    be a mosaic panel or a container name; the last RC16 sub's target is the
    fallback), else the first name given."""
    from photonscript.scheduler.integrations import resolve_project
    given = [n for n in names if n]
    for n in given:
        try:
            if resolve_project(n, store.projects.values()) is not None:
                return n
        except Exception:  # noqa: BLE001
            continue
    return given[0] if given else None


async def _restart(cfg, armer, a: dict, now: datetime, retry: bool = False) -> None:
    try:
        res = await armer.restart(when="after_exposure", now=now)
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "detail": str(e)}
    if not res.get("ok"):
        a.update(status="failed",
                 detail=(a.get("detail", "") + "; " if a.get("detail") else "")
                 + f"restart refused: {res.get('detail')}")
        return
    a["restart"] = {"req_t": _iso(now), "retried": retry or bool(
        (a.get("restart") or {}).get("retried"))}
    if res.get("redispatched"):
        a.update(status="done", effective_t=_iso(now))
    else:
        a["status"] = "pending"
        if not retry:
            a["detail"] = (a.get("detail", "") + "; " if a.get("detail") else "") \
                + "restart after the current sub"


def _job(a: dict, coro) -> None:
    async def _run():
        try:
            return await coro
        except Exception as e:  # noqa: BLE001
            return (False, str(e))
    try:
        MONITOR.jobs[a["id"]] = asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        coro.close()
        a.update(status="failed", detail="no event loop")


async def _fail(cfg, io, st, a, now, detail: str) -> None:
    a.update(status="failed", result="failed", detail=detail)
    st.setdefault("given_up", {})[f"{a['rig']}|{a['family']}"] = _iso(now)
    msg = (f"Night pilot: FAILED to act ({a['what']}) because "
           f"{a['because']}: {detail}. Needs a human; no more tries for "
           f"{a['family']} on the {_label(cfg, a['rig'])} tonight.")
    _event(cfg, st["night"], "failed", msg, now, rig=a["rig"], aid=a["id"],
           action=a["action"])
    await io.notify(cfg, msg, priority=1)


async def _follow_up(cfg, armer, io, st, ctx, now) -> None:
    """Background jobs finished? Restarts dispatched (the 10-13 race:
    one retry once NINA #1 reads idle)?"""
    for a in st.get("actions") or []:
        if a.get("kind") != "act" or a.get("result") is not None:
            continue
        if a.get("status") == "running":
            task = MONITOR.jobs.get(a["id"])
            t0 = _parse_t(a.get("t")) or now
            if task is None or (not task.done() and
                                (now - t0).total_seconds() / 60 > JOB_TIMEOUT_MIN):
                if task is None and (now - t0).total_seconds() / 60 < 1:
                    continue
                await _fail(cfg, io, st, a, now, "the background job was lost "
                            "(service restart?) or timed out")
                continue
            if not task.done():
                continue
            MONITOR.jobs.pop(a["id"], None)
            res = task.result()
            ok, detail = (res if isinstance(res, tuple) else (bool(res), ""))
            if ok:
                a.update(status="done", effective_t=_iso(now),
                         detail=detail or a.get("detail"))
                _event(cfg, st["night"], "dispatched", f"{a['what']}: "
                       f"{detail or 'done'}", now, rig=a["rig"], aid=a["id"])
            else:
                await _fail(cfg, io, st, a, now, detail or
                            f"{a['action']} failed ({getattr(armer, 'detail', '')})")
            continue
        if a.get("status") != "pending":
            continue
        req = _parse_t((a.get("restart") or {}).get("req_t"))
        lr = getattr(armer, "last_restart", None) or {}
        at = _parse_t(lr.get("at"))
        if req is not None and at is not None and at >= req - timedelta(seconds=1):
            if lr.get("ok"):
                a.update(status="done", effective_t=_iso(at))
                _event(cfg, st["night"], "dispatched", f"{a['what']}: restart "
                       "dispatched", now, rig=a["rig"], aid=a["id"])
                continue
            if not (a.get("restart") or {}).get("retried"):
                # 2026-10-13: the restart's load ran before NINA #1 had wound
                # down. Wait until it reads idle, then one retry.
                idle = await io.nina1_wait_idle(cfg)
                _event(cfg, st["night"], "retry", f"restart failed "
                       f"({lr.get('detail')}); NINA #1 idle={idle}; retrying once",
                       now, rig=a["rig"], aid=a["id"])
                a["restart"]["retried"] = True
                await _restart(cfg, armer, a, now, retry=True)
                if a["status"] == "failed":
                    await _fail(cfg, io, st, a, now, a["detail"])
                continue
            await _fail(cfg, io, st, a, now, f"restart failed twice: {lr.get('detail')}")
            continue
        if req is not None and (now - req).total_seconds() / 60 > RESTART_FOLLOW_MIN:
            await _fail(cfg, io, st, a, now, "the restart was not dispatched "
                        f"within {RESTART_FOLLOW_MIN:g} min")


async def _verify(cfg, armer, io, st, ctx, now) -> None:
    lim = limits(cfg)
    for a in st.get("actions") or []:
        if a.get("kind") != "act" or a.get("status") != "done" or a.get("result"):
            continue
        eff = _parse_t(a.get("effective_t"))
        if eff is None:
            continue
        pool = verify_pool(a, ctx["subs"])
        n = lim["verify_subs"]
        night, rig = st["night"], a["rig"]
        if len(pool) < n:
            if (now - eff).total_seconds() / 60 > lim["verify_timeout_min"]:
                a["result"] = "no_subs"
                msg = (f"Night pilot: no {_label(cfg, rig)} subs in "
                       f"{lim['verify_timeout_min']:g} min after '{a['what']}' "
                       f"({len(pool)} of {n}): check NINA. No more tries for "
                       f"{a['family']} tonight.")
                st.setdefault("given_up", {})[f"{rig}|{a['family']}"] = _iso(now)
                _event(cfg, night, "verify", msg, now, rig=rig, aid=a["id"],
                       result="no_subs")
                await io.notify(cfg, msg, priority=1)
            continue
        after = pool[:n]
        metric = a["baseline"]["metric"]
        before = a["baseline"]["value"]
        if metric == "target":
            improved, med = True, None
        else:
            improved, med = judge(metric, before, after)
        a["result"] = "improved" if improved else "not_improved"
        a["after"] = {"metric": metric, "value": med, "n": len(after),
                      "files": [r.get("file") for r in after]}
        nums = (f"{metric} {before if before is not None else '?'} -> "
                f"{med if med is not None else '?'} on the next {len(after)} subs"
                if metric != "target" else f"next {len(after)} subs on "
                f"{after[-1].get('target')}")
        if improved:
            msg = f"Night pilot: '{a['what']}' worked: {nums}."
            _event(cfg, night, "verify", msg, now, rig=rig, aid=a["id"],
                   result="improved", before=before, after=med)
            _learn(cfg, st, a, before, med, now)
            await io.notify(cfg, msg, priority=0)
            continue
        undo = None
        if a["action"] == "shorten_subs":
            undo = await _undo_shorten(cfg, armer, io, st, a, now)
        esc, human = _escalate(cfg, st, a, now, undo)
        msg = f"Night pilot: '{a['what']}' did NOT help: {nums}. {esc}"
        _event(cfg, night, "verify", msg, now, rig=rig, aid=a["id"],
               result="not_improved", before=before, after=med, human=human)
        if a["action"] == "shorten_subs":
            _learn(cfg, st, a, before, med, now)
        await io.notify(cfg, msg, priority=1 if human else 0)


async def _undo_shorten(cfg, armer, io, st, a, now) -> str:
    """A shorten step that did not lower the ecc only cost signal per sub
    (2026-10-12: 300, 180 and 120 s all trailed alike and the night stayed
    at 120 s for nothing). Put the lengths back (undo_sub_cap), then
    restart after the current sub so they take effect when the live guards
    and the dark left allow; otherwise they apply at the next dispatch.
    Returns the text for the push."""
    ok, detail = undo_sub_cap(st, io.store(), a)
    rec = {"t": _iso(now), "ok": ok, "detail": detail, "restart": None}
    a["undo"] = rec
    if not ok:
        _event(cfg, st["night"], "revert", f"undo of '{a['what']}' not done: "
               f"{detail}", now, rig=a["rig"], aid=a["id"])
        return f"sub lengths NOT restored ({detail})"
    try:
        running, target = await io.nina1_running(cfg)
    except Exception:  # noqa: BLE001
        running, target = None, None
    why = live_guard(cfg, armer, {"running": running, "target": target})
    left = _dark_left_min(armer, now)
    if why is None and left is not None and left < RESTART_DARK_MIN:
        why = f"only {max(0, int(left))} min of dark left"
    if why is None:
        try:
            res = await armer.restart(when="after_exposure", now=now)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "detail": str(e)}
        rec["restart"] = {"ok": bool(res.get("ok")), "detail": res.get("detail")}
        how = ("restart after the current sub" if res.get("ok") else
               f"restart refused ({res.get('detail')}): from the next dispatch")
    else:
        rec["restart"] = {"ok": False, "detail": why}
        how = f"from the next dispatch; no restart now: {why}"
    txt = f"{detail} ({how})"
    _event(cfg, st["night"], "revert", f"undid '{a['what']}': {txt}", now,
           rig=a["rig"], aid=a["id"], why="did not help")
    return txt


def _escalate(cfg, st, a, now, undo: str | None = None) -> tuple[str, bool]:
    """What happens after an action that did not help: (text, human)."""
    key = f"{a['rig']}|{a['family']}"
    acts = [x for x in st.get("actions") or [] if x.get("kind") == "act"
            and x.get("action") == a["action"] and x.get("rig") == a["rig"]]
    if a["action"] == "shorten_subs":
        # one step that did not help is enough: trailing that does not
        # follow the sub length is not a drift-per-second problem
        st.setdefault("given_up", {})[key] = _iso(now)
        return ("Shorter subs did not stop the trailing, so it is not a "
                f"drift-per-second problem: {undo or 'sub lengths unchanged'}. "
                "Needs a human (PHD2 stopped? TheSky tracking / ProTrack, "
                "flexure). No more shortening tonight.", True)
    if a["action"] == "run_af":
        fails = [x for x in acts if x.get("result") == "not_improved"]
        if len(fails) < 2 and len(acts) < POLICY["run_af"]["max"]:
            return ("One more AF at the next diagnosis.", False)
        st.setdefault("given_up", {})[key] = _iso(now)
        return ("Autofocus did not fix it twice: check the focuser (position, "
                "USB) and the AF curve. No more tries tonight.", True)
    if a["action"] == "stop_phd2":
        # PHD2 stopped but the subs still trail: it was not (only) PHD2. Not
        # given up: PHD2 started again later (2026-10-12, 08:30Z) is stopped
        # again (max 2 per night, 30 min cooldown, only while PHD2 is active)
        return ("Stopping PHD2 was not enough: needs a human (tracking, "
                "flexure, focus). PHD2 is stopped again if it restarts.", True)
    st.setdefault("given_up", {})[key] = _iso(now)
    return ("Needs a human. No more tries for this tonight.", True)


def _learn(cfg, st, a, before, after, now) -> None:
    """A suggestion for the morning report (never applied)."""
    model = ""
    try:
        from photonscript.scheduler.tracking_drift import tpoint_model
        tm = tpoint_model(cfg, st["night"]) or {}
        if tm.get("model_date"):
            model = f" with the TPoint model of {tm['model_date']}"
    except Exception:  # noqa: BLE001
        pass
    p = a.get("params") or {}
    txt = None
    if a["action"] == "shorten_subs" and a["result"] == "improved":
        txt = (f"unguided RC16 needs <= {p.get('to_s'):g} s{model} "
               f"({p.get('from_s'):g} s ecc {before}, {p.get('to_s'):g} s ecc "
               f"{after}): consider unguided_max_exposure_s {p.get('to_s'):g} or "
               f"the goal's sub length for {p.get('target')}")
    elif a["action"] == "shorten_subs":
        txt = (f"RC16 trailing is not drift-per-second: it did not follow the "
               f"sub length ({p.get('from_s'):g} s ecc {before}, {p.get('to_s'):g} s "
               f"ecc {after}"
               + (f"; put back to {p.get('from_s'):g} s"
                  if (a.get("undo") or {}).get("ok") else "")
               + "): look for a "
               "non-drift cause (PHD2, flexure, mount) before shortening subs")
    elif a["action"] == "stop_phd2":
        txt = ("a PHD2 session left guiding on an unguided night trailed the "
               f"RC16 (ecc {before} -> {after} once stopped): stop PHD2 before "
               "arming unguided")
    elif a["action"] == "run_af":
        txt = (f"{_label(cfg, a['rig'])} went soft mid-night and an AF fixed it "
               f"(HFR {before} -> {after}): consider a shorter AF interval / "
               "HFR trigger")
    if txt and txt not in st.setdefault("learned", []):
        st["learned"].append(txt)
        _event(cfg, st["night"], "learned", txt, now, rig=a["rig"], aid=a["id"])


# --------------------------------------------------------------------------
# dashboard + morning report
# --------------------------------------------------------------------------

def _action_line(cfg, a: dict) -> str:
    t = _hm(cfg, _parse_t(a.get("t")))
    res = a.get("result")
    after = a.get("after") or {}
    if res in ("improved", "not_improved"):
        r = ("worked" if res == "improved" else "did not help")
        if after.get("value") is not None:
            r += f" ({after['metric']} {a['baseline'].get('value')} -> {after['value']})"
    elif res == "no_subs":
        r = "no subs to check"
    elif res == "failed" or a.get("status") == "failed":
        r = f"FAILED ({a.get('detail')})"
    elif a.get("kind") == "advise":
        r = "advise only, nothing sent"
    else:
        r = f"checking at {_hm(cfg, _parse_t(a.get('next_check')))}"
    pre = "would have " if a.get("kind") == "advise" else ""
    return f"{t} {pre}{a['what']} -> {r}"


def status(cfg, night: str | None = None, now: datetime | None = None) -> dict:
    """GET /api/night/pilot: mode, tonight's actions and the dashboard line."""
    from photonscript.shared.phd2_store import night_of
    now = now or datetime.utcnow()
    night = night or night_of(cfg, now)
    st = load_state(cfg, night)
    acts = st.get("actions") or []
    m = mode(cfg)
    last = acts[-1] if acts else None
    if m == "off":
        line = "Night pilot: off"
    elif last is None:
        line = f"Night pilot ({m}): no action tonight"
    else:
        line = f"Night pilot ({m}): " + _action_line(cfg, last)
        n_act = sum(1 for a in acts if a.get("kind") == "act")
        if n_act > 1:
            line += f" ({n_act} actions tonight)"
    return {"mode": m, "night": night, "line": line, "last": last,
            "actions": acts, "limits": limits(cfg),
            "ledger": st.get("ledger") or {}, "learned": st.get("learned") or [],
            "given_up": st.get("given_up") or {}, "tick": MONITOR.last}


def morning_lines(cfg, date: str) -> list[str]:
    """The morning report section: one line for the night's actions, one
    per learned suggestion. Empty when the pilot did nothing."""
    p = state_path(cfg, date)
    if not p.exists():
        return []
    st = load_state(cfg, date)
    acts = [a for a in st.get("actions") or [] if a.get("kind") in ("act", "advise")]
    out = []
    if acts:
        out.append("Night pilot: " + "; ".join(_action_line(cfg, a) for a in acts[:6])
                   + (f"; +{len(acts) - 6} more" if len(acts) > 6 else ""))
    rev = [f"{e.get('target')}" for e in (st.get("ledger") or {}).values()
           if e.get("reverted")]
    left = [f"{e.get('target')}" for e in (st.get("ledger") or {}).values()
            if not e.get("reverted")]
    if rev:
        out.append("Night pilot restored tonight-only changes: " + ", ".join(rev))
    if left:
        out.append("Night pilot: tonight-only changes NOT restored yet: " + ", ".join(left))
    for t in st.get("learned") or []:
        out.append("Night pilot learned: " + t)
    return out


async def run_pilot(get_config, get_armer, tick_s: int = TICK_S) -> None:
    """Background loop (app startup). Never raises."""
    while True:
        try:
            await tick(get_config(), get_armer())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("night pilot tick failed: %s", e)
        await asyncio.sleep(tick_s)
