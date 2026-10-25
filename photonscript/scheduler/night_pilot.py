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
                   restart after the current sub when the guards allow,
                   followed up like an action's: one retry on the race),
                   "trailing is not drift-per-second" is recorded and a
                   human is asked; no more shortening tonight. A step that
                   helped can be followed by one more on a later diagnosis.
  focus            soft, round stars with the other rig sharp: run_af.
                   RC16 (night_pilot_af_direct, on): a direct ninaAPI
                   autofocus between two subs (direct_af: camera idle, an
                   imaging block, never a flat / dark / bias / TPoint /
                   calibration item; verified on AUTOFOCUS-FINISHED and
                   the focuser position); refused or not finished, or the
                   switch off: Restart tonight from now after the current
                   sub (every target start runs AF). Piggy-600: NINA #2 stopped
                   after its sub and the companion re-dispatched (the PS-64
                   Resume path; its image pass starts with AF). Verified on
                   HFR; two AFs that do not help -> a human.
  pointing         plate solves failing (evidence plate_solve_errors >=
                   night_pilot_solve_failures): skip_target, once per
                   night: the goal set inactive for tonight and Restart
                   tonight from now (the planner moves on).
  clouds, seeing   nothing (roof safety owns clouds): a run event only.
  optics           star elongation that is not tracking (the sub doctor's
                   optics_vs_tracking verdict, action check_optics): never
                   a sub length or exposure change. One Pushover per rig
                   per night ("<rig> star elongation looks optical (focus /
                   tilt / collimation); shorter subs will not help" plus the
                   evidence line), a run event (kind night_pilot "optics"),
                   the advice list of GET /api/night/pilot and a morning
                   report line. While a rig's newest diagnosis is optics
                   (medium / high), shortening its subs (shorten_subs, the
                   sub watch's limit_subs) is held. Low confidence: a run
                   event only.
  focuser          hardware: one push for a human, nothing sent.

Sub-length ladder (2026-10-25, scheduler/sub_ladder.py; ladder()): not a
diagnosis but the subs themselves. Per rig: N round accepted subs at a
length -> the next rung (night_pilot_ladder_<rig>); M of the last W failing
at a rung it stepped to -> back one rung, held for the night. Never on an
optics reading, too few stars (narrowband: a higher bar), a sky-limited
background or a rung without darks. Applied like a sub cap: a tonight-only
ledgered goal override (RC16 filters / the Piggy "OSC" key), then the RC16
restart after the current sub or the Piggy-600 companion re-dispatch
(plan["piggy_length"] updated). One story push per step;
night_pilot_ladder_mode off | advise (default) | act. Runs before the
diagnoses each tick; a shorten waits out a ladder step on the same rig.
The night's best rung per rig / target is a suggestion in the next plan
heads-up (<data_dir>/night_pilot/ladder_learned.json).

Guardrails (guard()): acts only while the armer is RUNNING (never ARMED,
PAUSED_*, WATCHING a sideloaded night, COMPLETE); never while a calibration
capture job runs, an arm recipe (TPoint mapping, tracking test, optics) is
in progress, NINA #1 runs a TPoint / tracking test / PHD2 calibration /
flat / dark / bias / focus calibration item, or PHD2 calibrates; never
with under RESTART_DARK_MIN of dark left for a re-dispatch;
night_pilot_max_actions (6) per night, one action per rig per
night_pilot_rig_cooldown_min (15). It is the one actor for remedies: the
sub doctor's own opt-in remedies are retired (a sub_doctor_remedy event in
an old log still holds the same action). It never arms, disarms, parks, unparks, touches the
roof or safety, or changes a PHD2 calibration.

Every decision is a run event (kind "night_pilot": act / advise / held /
human / annotate / optics / verify / retry / failed / learned / revert), every
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

from photonscript.shared.fileops import replace_retry

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
# Sub watch actions (2026-10-14, scheduler/sub_watch.py): each its own mode
# key, off | advise | act, default advise (Jeremy: advise until he says act);
# night_pilot_mode caps them (advise there makes every action advise).
POLICY.update({
    "refocus": {"key": None, "mode_key": "night_pilot_refocus_mode", "max": 2,
                "cooldown_min": 30.0, "metric": "hfr", "restart": True},
    "limit_subs": {"key": None, "mode_key": "night_pilot_limit_subs_mode", "max": 2,
                   "cooldown_min": 30.0, "metric": "ecc", "restart": True},
    "guider_stop": {"key": None, "mode_key": "night_pilot_guider_stop_mode", "max": 2,
                    "cooldown_min": 30.0, "metric": "ecc", "restart": False},
})
ACTIONS = tuple(POLICY)
# actions that do the same thing share their max per night and cooldown, so
# a sub doctor diagnosis and a sub watch signal never both act
GROUP = {"stop_phd2": "guider", "guider_stop": "guider", "run_af": "af",
         "refocus": "af", "shorten_subs": "shorten", "limit_subs": "shorten"}
ACTION_MODES = ("off", "advise", "act")
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
                "plate_solve": "pointing", "optics": "optics",
                "optical": "optics", "other": "other", "none": "none"}
OPTICS_TEXT = ("{rig} star elongation looks optical (focus / tilt / "
               "collimation); shorter subs will not help")


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
    if not p:
        return False
    if p.get("mode_key"):
        return action_mode(cfg, action) == "act"
    return bool(getattr(cfg, p["key"], True))


def action_mode(cfg, action: str) -> str:
    """A sub watch action's own mode (off | advise | act, default advise);
    the older actions answer act / advise from their on/off switch."""
    p = POLICY.get(action) or {}
    if not p.get("mode_key"):
        return "act" if action_on(cfg, action) else "advise"
    m = str(getattr(cfg, p["mode_key"], "advise") or "advise").strip().lower()
    return m if m in ACTION_MODES else "advise"


def group(action: str | None) -> str | None:
    return GROUP.get(action or "", action)


def switch_text(action: str) -> str:
    p = POLICY.get(action) or {}
    if p.get("mode_key"):
        return f"{p['mode_key']}=advise"
    return f"{p.get('key')} is off"


def sub_steps(cfg) -> list[float]:
    """night_pilot_sub_steps ("300,240,180,120") as seconds, longest first."""
    raw = str(getattr(cfg, "night_pilot_sub_steps", "300,240,180,120") or "")
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

    if d.get("source") == "watch":
        return decide_watch(cfg, d, ctx, low)
    if fam in ("clouds", "seeing"):
        return out("annotate", why=f"{fam}: nothing to change (roof safety "
                                   "owns clouds; seeing is the sky)")
    if fam in ("none", "other"):
        return out("annotate", why="no cause the pilot can act on")
    if fam == "optics":
        # never a sub length or exposure change: the elongation does not
        # come from tracking, so shorter subs do not fix it
        if low:
            return out("annotate", why=f"{d.get('confidence')} confidence "
                       "optics: watching only (no sub length change)")
        return out("optics", what=OPTICS_TEXT.format(rig=_label(cfg, rig)),
                   why="optics, not tracking: the pilot sends nothing and "
                       "does not change the sub length")
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
        how = ("a direct ninaAPI autofocus between subs, a restart if NINA "
               "refuses it" if rig == "rc16" and af_direct_on(cfg) else
               "a restart after the sub (the target start runs AF)"
               if rig == "rc16" else "NINA #2 companion re-dispatch, AF first")
        return out("act", "run_af", what=f"asked for an autofocus on the "
                   f"{_label(cfg, rig)}", why="soft round stars on this rig only",
                   how=how)
    if fam == "pointing":
        n = _num(ev.get("plate_solve_errors")) or 0
        if rig == "rc16" and n >= lim["solve_failures"]:
            return out("act", "skip_target", what="skipped to the next planned "
                       "target", why=f"{n:g} plate solves failed",
                       target=ctx.get("target"))
        return out("annotate", why="off target: the off-target alert owns it")
    return out("annotate", why="unknown cause")


def decide_watch(cfg, d: dict, ctx: dict, low: bool) -> dict:
    """decide() for a sub watch signal (scheduler/sub_watch.py): the signal
    already names its action; this checks it still applies and words it."""
    fam, rig, ev = d["family"], d["rig"], d.get("evidence") or {}
    action = d.get("action") or "none"

    def out(kind, action=None, what="", why="", **params):
        return {"kind": kind, "action": action, "params": params,
                "what": what, "why": why}

    if ev.get("human"):
        return out("human", what=f"check the {_label(cfg, rig)} tracking",
                   why="a cause shorter subs do not fix: the pilot sends nothing")
    if action == "none" or action not in POLICY:
        return out("annotate", why=_because(d))
    if low:
        return out("annotate", why=f"{d.get('confidence')} confidence: watching "
                                   f"only ({_because(d)})")
    if action_mode(cfg, action) == "off":
        return out("annotate", why=f"{POLICY[action]['mode_key']}=off ({_because(d)})")
    if action == "guider_stop":
        if ctx.get("guided"):
            return out("annotate", why="guided night: PHD2 is meant to guide")
        return out("act", "guider_stop", what="stopped PHD2 guiding (unguided "
                   f"night, seen on the {_label(cfg, rig)} subs)",
                   why="PHD2 is active on an UNGUIDED night")
    if action == "refocus":
        if rig != "rc16" and not ctx.get("piggy"):
            return out("annotate", why="Piggy-600 not enabled")
        how = ("a direct ninaAPI autofocus between subs, a restart if NINA "
               "refuses it" if rig == "rc16" and af_direct_on(cfg) else
               "a restart after the sub (the target start runs AF)"
               if rig == "rc16" else "NINA #2 companion re-dispatch, AF first")
        return out("act", "refocus", what=f"refocused the {_label(cfg, rig)} "
                   f"(HFR drift)", why="HFR risen over the night's best, other "
                   "rig steady", how=how, best_hfr=ev.get("best_hfr"))
    if action == "limit_subs":
        cur = _num(ctx.get("exp_s")) or _num(ev.get("exp_s"))
        lim = _num(ev.get("limit_s"))
        if cur is None or lim is None:
            return out("annotate", why="sub length or limit unknown")
        from photonscript.scheduler.unguided_limits import snap_length
        to, dk = snap_length(cfg, rig, lim, cur, status=ctx.get("dark_status"))
        if to is None or to >= cur - 0.5:
            return out("annotate", why=f"no ladder length under {cur:g} s at or "
                       f"under the limit {lim:g} s")
        return out("act", "limit_subs",
                   what=f"cut RC16 subs {cur:g} -> {to:g} s (learned unguided "
                        f"limit, {ev.get('region')})",
                   why="RC16 trailing past the region's learned unguided limit",
                   from_s=cur, to_s=to, limit_s=lim, region=ev.get("region"),
                   darks=(dk or {}).get("match"), darks_text=(dk or {}).get("text"))
    return out("annotate", why=_because(d))


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
        replace_retry(tmp, p)
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
# direct autofocus between subs (night_pilot_af_direct)
# --------------------------------------------------------------------------

AF_IDLE_MAX_S = 600.0     # wait at most this for the running sub to end
AF_START_S = 90.0         # AUTOFOCUS-STARTING within this of the request
AF_FINISH_S = 600.0       # AUTOFOCUS-FINISHED within this (an AF is ~2.5 min)
AF_POLL_S = 5.0
AF_CAMERA_POLL_S = 2.0
AF_GRACE_S = 60.0         # download + save margin past the exposure end


def af_direct_on(cfg) -> bool:
    return bool(getattr(cfg, "night_pilot_af_direct", True))


def _af_events_since(hist, n0: int, t0: datetime | None) -> list[dict]:
    """The event-history rows after the request: by index (the history is
    append-only since NINA started), by time when it got shorter (NINA
    restarted)."""
    from photonscript.scheduler.af_stall import parse_time
    rows = [e for e in (hist if isinstance(hist, list) else []) if isinstance(e, dict)]
    if len(rows) >= n0:
        return rows[n0:]
    out = []
    for e in rows:
        t = parse_time(e.get("Time"))
        if t is not None and t0 is not None and t >= t0 - timedelta(seconds=5):
            out.append(e)
    return out


def imaging_block(tree) -> tuple[bool, str]:
    """(ok, why) for a direct AF: NINA #1's sequence runs, inside an
    imaging block (a running exposure item: Smart Exposure, Take Exposure),
    and nothing on the running path is a slew, center, AF, flat, dark,
    bias, TPoint, tracking test or calibration (the running target's own
    containers are skipped, so a "Dark Shark Nebula" is not a dark)."""
    from photonscript.scheduler.off_target import NOT_IMAGING
    from photonscript.scheduler.sideload import nina_running
    from photonscript.scheduler.where_panel import sequence_position
    if tree is None:
        return False, "NINA #1 sequence unreadable"
    names = nina_running(tree)
    if not names:
        return False, "NINA #1 sequence not running"
    try:
        tgt = str(sequence_position(tree).get("target") or "").strip().lower()
    except Exception:  # noqa: BLE001
        tgt = ""
    exposing = False
    for n in names:
        low = str(n).strip().lower()
        if tgt and (low == tgt or low.startswith(tgt)):
            continue
        for w in BUSY_WORDS + NOT_IMAGING:
            if w in low:
                return False, f"NINA #1 runs '{n}'"
        if "exposure" in low:
            exposing = True
    if not exposing:
        return False, "NINA #1 is not in an imaging block"
    return True, "imaging block"


async def direct_af(get, read_tree, *, sleep=asyncio.sleep, clock=None,
                    utcnow=None) -> dict:
    """One autofocus on NINA #1 between two subs, through ninaAPI (GET
    /equipment/focuser/auto-focus), instead of a restart (stop, slew,
    center, AF: ~6 min and a sub). get: async path -> ninaAPI Response
    payload or None. read_tree: async () -> the sequence tree or None.

    Only when the camera is idle between exposures (waits for the running
    sub to end, at most AF_IDLE_MAX_S), the sequence is in an imaging block
    and not in a flat / dark / bias / TPoint / tracking test / calibration
    item (imaging_block). Verified: AUTOFOCUS-STARTING then
    AUTOFOCUS-FINISHED in NINA's event history after the request, and a
    focuser position read back. Returns {ok, refused (nothing was started:
    the caller falls back to the restart path), detail, position_before,
    position_after, finished_t}. Never raises."""
    import time

    from photonscript.scheduler.af_stall import FINISHED, POINT, START, parse_time
    from photonscript.scheduler.night_pause import camera_busy, seconds_left
    clock = clock or time.monotonic
    utcnow = utcnow or datetime.utcnow
    out = {"ok": False, "refused": True, "detail": "", "position_before": None,
           "position_after": None, "finished_t": None}

    def refuse(why):
        out["detail"] = why
        return out
    try:
        # 1. between exposures: wait for the running sub to end
        t0 = clock()
        cam = await get("/equipment/camera/info")
        busy = camera_busy(cam)
        if busy is None:
            return refuse("NINA #1 camera unreadable")
        if busy:
            left = None
            try:
                from datetime import timezone
                left = seconds_left(cam, utcnow().replace(tzinfo=timezone.utc))
            except Exception:  # noqa: BLE001
                left = None
            limit = AF_IDLE_MAX_S if left is None else min(AF_IDLE_MAX_S,
                                                           left + AF_GRACE_S)
            while busy:
                if clock() - t0 > limit:
                    return refuse("the camera never went idle between subs")
                await sleep(AF_CAMERA_POLL_S)
                busy = camera_busy(await get("/equipment/camera/info"))
                if busy is None:
                    return refuse("NINA #1 camera unreadable")
        # 2. in an imaging block, nothing calibrating
        ok, why = imaging_block(await read_tree())
        if not ok:
            return refuse(why)
        foc = await get("/equipment/focuser/info")
        out["position_before"] = _num((foc or {}).get("Position")) \
            if isinstance(foc, dict) else None
        hist = await get("/event-history")
        n0 = len(hist) if isinstance(hist, list) else 0
        t_req = utcnow()
        # 3. the request
        if await get("/equipment/focuser/auto-focus") is None:
            return refuse("ninaAPI refused the autofocus request")
        # 4. NINA's event history: started, then finished
        started = False
        t1 = clock()
        while True:
            await sleep(AF_POLL_S)
            new = _af_events_since(await get("/event-history"), n0, t_req)
            names = [str(e.get("Event") or "").upper() for e in new]
            if START in names:
                started = True
                out["refused"] = False
                after = names[names.index(START) + 1:]
                if FINISHED in after:
                    fin = new[names.index(START) + 1 + after.index(FINISHED)]
                    ft = parse_time(fin.get("Time"))
                    out["finished_t"] = _iso(ft) if ft else _iso(utcnow())
                    break
                bad = [x for x in after if x.startswith("AUTOFOCUS-")
                       and x not in (POINT, START, FINISHED)]
                if bad:
                    out["detail"] = f"the autofocus ended with {bad[-1]}"
                    return out
            waited = clock() - t1
            if not started and waited > AF_START_S:
                return refuse(f"NINA did not start an autofocus within "
                              f"{AF_START_S:g} s")
            if waited > AF_START_S + AF_FINISH_S:
                out["detail"] = (f"no AUTOFOCUS-FINISHED within "
                                 f"{(AF_START_S + AF_FINISH_S) / 60:g} min")
                return out
        # 5. the new focuser position
        foc = await get("/equipment/focuser/info")
        pos = _num((foc or {}).get("Position")) if isinstance(foc, dict) else None
        out["position_after"] = pos
        if not pos or pos <= 0:
            out["detail"] = (f"AUTOFOCUS-FINISHED but the focuser reads "
                             f"{pos if pos is not None else 'nothing'}")
            return out
        b = out["position_before"]
        out["ok"] = True
        out["detail"] = ("AUTOFOCUS-FINISHED, focuser "
                         + (f"{b:g} -> {pos:g}" if b is not None else f"at {pos:g}"))
        return out
    except Exception as e:  # noqa: BLE001
        out["detail"] = f"direct autofocus error: {e}"
        return out


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
        from photonscript.telescope_agent import phd2_ops
        try:
            # one PhotonScript actor commands PHD2 at a time (PS-91): the
            # retired sub doctor remedy held this lock, so the pilot does too
            async with phd2_ops.hold("night_pilot", wait_s=10):
                st = await asyncio.wait_for(asyncio.to_thread(_stop), 60)
        except phd2_ops.PHD2Busy as e:
            return False, f"PHD2 held by {e.owner}: not stopped"
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

    async def nina1_autofocus(self, cfg) -> tuple[bool, str]:
        """RC16 direct autofocus between subs (direct_af over ninaAPI GETs).
        (ok, detail); not ok = the caller falls back to the restart."""
        from photonscript.scheduler.nina_watchdog import rig_base
        from photonscript.scheduler.night_pause import nina_get
        from photonscript.scheduler.sideload import read_sequence_state
        base = rig_base(cfg, "rc16")
        if not base:
            return False, "no NINA #1 URL"

        async def get(path):
            return await nina_get(base, path, timeout=30.0)

        async def tree():
            try:
                t, _e = await asyncio.wait_for(read_sequence_state(base), 20)
                return t
            except Exception:  # noqa: BLE001
                return None
        res = await direct_af(get, tree)
        return bool(res.get("ok")), str(res.get("detail") or "")

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

    async def notify(self, cfg, msg: str, priority: int = 0,
                     kind: str = "info", fact: str | None = None) -> None:
        try:
            from photonscript.shared.pushover import notify
            await notify(cfg, msg, title=TITLE, priority=priority, kind=kind,
                         fact=fact)
        except Exception as e:  # noqa: BLE001
            logger.warning("night pilot push failed: %s", e)

    # ---- the sub watch and the closed roof (2026-10-14)

    def dark_status(self, cfg):
        """seconds -> dark_match dict for the RC16 (None: dark_match)."""
        return None

    def ladder_dark_status(self, cfg, rig: str):
        """seconds -> dark_match dict for the sub ladder (None: dark_match)."""
        return None

    async def piggy_redispatch(self, cfg, armer) -> tuple[bool, str]:
        """The sub ladder's Piggy-600 length change: the same stop after the
        sub + companion re-dispatch as the Piggy AF (the companion reads the
        new length from plan["piggy_length"])."""
        return await self.piggy_af(cfg, armer)

    def limits_tables(self, cfg) -> dict:
        """{rig: the learned unguided limits table} from the store (never
        recomputed here)."""
        try:
            from photonscript.scheduler.unguided_limits import load
            return dict(((load(cfg) or {}).get("rigs") or {}))
        except Exception:  # noqa: BLE001
            return {}

    async def af_finished(self, cfg) -> dict:
        """{rig: [AUTOFOCUS-FINISHED times, naive UTC]} from each NINA's
        event history (GET only; NINA keeps its current session)."""
        from photonscript.scheduler.af_stall import FINISHED, parse_time
        from photonscript.scheduler.night_pause import nina_get
        from photonscript.shared.rigs import PIGGYBACK, rig_config
        out = {}
        for rig in ("rc16", PIGGYBACK):
            try:
                base = rig_config(cfg, rig).nina_base_url.rstrip("/")
                hist = await asyncio.wait_for(nina_get(base, "/event-history",
                                                       timeout=20.0), 25)
            except Exception:  # noqa: BLE001
                continue
            out[rig] = [t for t in (parse_time(e.get("Time")) for e in hist or []
                                    if isinstance(e, dict)
                                    and str(e.get("Event") or "").upper() == FINISHED)
                        if t is not None]
        return out

    def roof_closed_since(self, cfg, now: datetime) -> datetime | None:
        """When the current unsafe (roof closed) spell began, None when the
        safety monitor reads safe (shared.safety_history). PS-228: the
        lookback is ROOF_LOOKBACK_H (longer than a day), not 14 h: a window
        is clipped to the lookback start, so a roof closed since the
        previous afternoon used to read as a new spell every minute
        (05:52 local minus 14 h = "closed at 15:52")."""
        try:
            from photonscript.shared.safety_history import unsafe_windows
            wins, _src = unsafe_windows(
                cfg, now - timedelta(hours=ROOF_LOOKBACK_H), now)
        except Exception:  # noqa: BLE001
            return None
        if wins and wins[-1][1] >= now - timedelta(seconds=1):
            return wins[-1][0]
        return None

    def darks_owed(self, cfg) -> dict:
        """{rig: [{exp_s, owed}]}: the night dark quota deficit per length
        (calibration.night_dark_exposures + dark_quota, the Calibration owed
        view's rule)."""
        out = {}
        try:
            from photonscript.scheduler.calibration import dark_quota, night_dark_exposures
            from photonscript.shared.rigs import PIGGYBACK, rig_config
        except Exception:  # noqa: BLE001
            return out
        for rig in ("rc16", PIGGYBACK):
            if rig == PIGGYBACK and not getattr(cfg, "piggyback_enabled", False):
                continue
            try:
                rc = cfg if rig == "rc16" else rig_config(cfg, rig)
                out[rig] = [{"exp_s": float(e), "owed": int(dark_quota(rc, rig, e)["need"])}
                            for e in night_dark_exposures(rc, rig)]
            except Exception as e:  # noqa: BLE001
                logger.debug("night pilot: owed darks (%s) unavailable: %s", rig, e)
        return out

    def light_lengths(self, cfg, armer) -> dict:
        """{rig: [sub lengths tonight's lights use]} (the plan; the Piggy-600
        companion's lengths)."""
        out = {"rc16": [], "piggyback": []}
        plan = getattr(armer, "plan", None)
        for t in (plan or {}).get("targets") or [] if isinstance(plan, dict) else []:
            for e in t.get("exposures") or []:
                v = _num(e.get("exp_s"))
                if v:
                    out["rc16"].append(v)
        pl = ((plan or {}).get("piggy_length") or {}) if isinstance(plan, dict) else {}
        for seg in pl.get("segments") or []:
            v = _num((seg or {}).get("exp_s"))
            if v:
                out["piggyback"].append(v)
        v = _num(getattr(cfg, "piggyback_exposure_s", None))
        if v:
            out["piggyback"].append(v)
        return out

    async def guider_hook(self, cfg, rig: str, evidence: dict, now: datetime):
        """pilot_hooks.guider_active_unguided (the unguided guard's hook):
        a result dict when it handled the stop, None to fall back."""
        try:
            from photonscript.scheduler import pilot_hooks
            res = pilot_hooks.guider_active_unguided(cfg, rig, evidence, now)
            if asyncio.iscoroutine(res):
                res = await res
            return res if isinstance(res, dict) else None
        except Exception as e:  # noqa: BLE001
            logger.warning("night pilot: unguided guard hook failed: %s", e)
            return None


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
          now: datetime, virtual: bool = False) -> str | None:
    """Why this action must not run now, else None. virtual: the decision is
    advise only; the night's advised actions count toward the max and the
    cooldowns as if they had run, so the advise timeline is the one the
    pilot would have acted on (rate limited, no flapping)."""
    why = live_guard(cfg, armer, ctx)
    if why:
        return why
    action, rig = dec["action"], d["rig"]
    if group(action) == "shorten" and             (st.get("last_cause") or {}).get(rig) == "optics":
        return ("the sub doctor reads the elongation as optical (focus / tilt "
                "/ collimation): shorter subs will not help")
    if group(action) == "shorten":
        ls = _ladder_recent(st, rig, now, max(limits(cfg)["rig_cooldown_min"], 15.0))
        if ls is not None:
            return (f"the sub ladder changed the {_label(cfg, rig)} sub length "
                    f"({ls['from_s']:g} -> {ls['to_s']:g} s) at "
                    f"{_hm(cfg, _parse_t(ls.get('t')))}")
    if action in ("stop_phd2", "guider_stop") and not virtual:
        try:
            from photonscript.telescope_agent import phd2_ops
            if phd2_ops.busy():
                return (f"another PhotonScript actor holds PHD2 "
                        f"({phd2_ops.owner()})")
        except Exception:  # noqa: BLE001
            pass
    pol = POLICY[action]
    lim = limits(cfg)
    if pol["restart"] or action == "unguided":
        left = _dark_left_min(armer, now)
        if left is not None and left < RESTART_DARK_MIN:
            return f"only {max(0, int(left))} min of dark left"
    kinds = ("act", "advise") if virtual else ("act",)
    acts = [a for a in st.get("actions") or [] if a.get("kind") in kinds]
    if virtual:
        p = dec.get("params") or {}
        for a in acts:
            q = a.get("params") or {}
            if (a.get("kind") == "advise" and a.get("rig") == rig
                    and group(a.get("action")) == group(action) == "shorten"
                    and q.get("to_s") == p.get("to_s")
                    and (q.get("target") or a.get("target")) == ctx.get("target")):
                return (f"already advised {a.get('what')} at "
                        f"{_hm(cfg, _parse_t(a.get('t')))}")
    if len(acts) >= lim["max_actions"]:
        return f"{len(acts)} actions tonight (max {lim['max_actions']})"
    same = [a for a in acts if group(a.get("action")) == group(action)]
    if len(same) >= pol["max"]:
        return f"{action} done {len(same)} time(s) tonight (max {pol['max']})"
    pend = [a for a in acts if a.get("kind") == "act" and a.get("rig") == rig
            and a.get("result") is None
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
        if group(a.get("action")) == group(action) and age < pol["cooldown_min"]:
            return f"{action} cooldown ({pol['cooldown_min']:g} min)"
    if st.get("given_up", {}).get(f"{rig}|{d['family']}"):
        return f"gave up on {d['family']} tonight (pushed for a human)"
    rem = REMEDY_SAME.get({"guider_stop": "stop_phd2", "refocus": "run_af",
                           "limit_subs": "shorten_subs"}.get(action, action))
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
    if group(a["action"]) == "shorten":
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
                     live: bool = True, subs: list[dict] | None = None) -> dict:
    """What the decisions need. live=False (no new diagnosis this tick)
    skips the NINA #1 and PHD2 reads: verification needs only the logs."""
    running, target = (await io.nina1_running(cfg)) if live else (None, None)
    try:
        guided = bool(armer._use_guiding())
    except Exception:  # noqa: BLE001
        guided = bool(getattr(cfg, "guided_default", False))
    subs = io.subs(cfg, night) if subs is None else subs
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
    # the sub watch (scheduler/sub_watch.py): every sub graded since the
    # last tick, both rigs, paired by time
    from photonscript.scheduler import sub_watch
    subs = io.subs(cfg, night)
    fresh = sub_watch.watch_on(cfg) and _fresh_subs(subs, st.get("watch_seen"))
    ctx = await gather_ctx(cfg, armer, io, night, events,
                           live=bool(diags) or fresh, subs=subs)
    watch = []
    if fresh:
        ctx["limits"] = io.limits_tables(cfg)
        ctx.setdefault("dark_status", io.dark_status(cfg))
        ctx.setdefault("af_finished", None)
        try:
            seen0 = st.get("watch_seen")
            watch, seen = sub_watch.signals(cfg, subs, ctx, seen0, now=now)
            if any((w.get("evidence") or {}).get("needs_af") for w in watch):
                # NINA's AF history (GET), only when a focus signal needs it
                ctx["af_finished"] = await io.af_finished(cfg)
                watch, seen = sub_watch.signals(cfg, subs, ctx, seen0, now=now)
            st["watch_seen"] = seen
            g = [v for k, v in seen.items() if k.endswith("|guiding")]
            if g and not any(g):
                st["guider_alert_open"] = False
            # a sub doctor diagnosis of the same rig this tick speaks for it
            watch = [w for w in watch if w["rig"] not in diags]
        except Exception as e:  # noqa: BLE001
            logger.warning("night pilot sub watch failed: %s", e)
            watch = []
    await _follow_up(cfg, armer, io, st, ctx, now)
    await _verify(cfg, armer, io, st, ctx, now)
    # night ended without the dawn shutdown (disarmed, error): revert now
    from photonscript.scheduler.armer import LIVE_STATES
    if str(getattr(armer, "state", "")) not in LIVE_STATES and \
            any(not e.get("reverted") for e in (st.get("ledger") or {}).values()):
        revert(cfg, st, io.store(), now, why=f"armer {armer.state}")
    newest = max((d["dt"] for d in diags.values()), default=None)
    done = []
    # the sub-length ladder first: its step back down preempts a shorten
    # (which then waits out the ladder's step)
    try:
        done += await ladder(cfg, armer, io, st, ctx, now)
    except Exception as e:  # noqa: BLE001
        logger.warning("night pilot sub ladder failed: %s", e)
    for d in sorted([*diags.values(), *watch], key=lambda x: x["dt"]):
        done.append(await _handle(cfg, armer, io, st, d, ctx, now, m))
    if newest is not None:
        st["seen_t"] = _iso(newest)
    try:
        await closed_roof(cfg, armer, io, st, now)
    except Exception as e:  # noqa: BLE001
        logger.warning("night pilot closed-roof check failed: %s", e)
    save_state(cfg, st)
    MONITOR.last = {"t": _iso(now), "mode": m, "night": night, "handled": done}
    return {"state": m, "night": night, "handled": done}


def _fresh_subs(subs: list[dict], seen: dict | None) -> bool:
    """A sub graded after the watch's watermark on either rig."""
    from photonscript.scheduler.sub_watch import graded_at
    seen = seen or {}
    for r in subs or []:
        t = graded_at(r)
        mark = _parse_t(seen.get(r.get("rig") or "rc16"))
        if t is not None and (mark is None or t > mark):
            return True
    return False


# --------------------------------------------------------------------------
# closed roof: the idle time goes to owed darks (closed_roof_darks)
# --------------------------------------------------------------------------

def closed_roof_on(cfg) -> bool:
    return str(getattr(cfg, "closed_roof_darks", "on") or "on").strip().lower() == "on"


def closed_roof_min_left(cfg) -> float:
    return max(0.0, _f(cfg, "closed_roof_darks_min_left_min", 30.0))


ROOF_LOOKBACK_H = 26.0


def _plan_t(armer, key: str) -> datetime | None:
    try:
        return _parse_t((getattr(armer, "plan", None) or {}).get(key))
    except Exception:  # noqa: BLE001
        return None


async def closed_roof(cfg, armer, io, st: dict, now: datetime) -> dict | None:
    """The roof closed mid-night (the safety monitor unsafe, the mount parked
    by the sequence's UNSAFE branch) with more than closed_roof_darks_min_left_min
    to dawn: the sequences shoot the owed darks themselves (the RC16 UNSAFE
    branch's dark quota, PS-160; the Piggy-600 companion's
    OSC_CLOSED_ROOF_DARKS, calibration._osc_closed_roof_darks), so nothing is
    sent from here. The pilot records the spell and what fits in it (run event
    kind night_pilot "closed_roof", one push), and the morning report says
    what the closed roof was used for. Never opens, moves or commands
    anything. Returns the spell record or None.

    PS-228: one record per closure. The spell is latched
    (st["closed_roof_open"]) until the roof reads safe again, so a moving
    `since` can never make a new spell; nothing is checked outside the dark
    window (before dusk, from dawn on: a daytime closure is not a mid-night
    one); a spell that began before dusk is recorded once as "closed since
    dusk" without a push, and so is the "under N min, no darks" case (the
    morning report line is enough). Only a mid-night closure with time for
    darks pushes."""
    if not closed_roof_on(cfg):
        return None
    from photonscript.scheduler.armer import LIVE_STATES
    if str(getattr(armer, "state", "")) not in LIVE_STATES:
        return None
    since = io.roof_closed_since(cfg, now)
    if since is None:
        st.pop("closed_roof_open", None)
        return None
    if st.get("closed_roof_open"):
        return None
    dusk, dawn = _plan_t(armer, "dusk_utc"), _plan_t(armer, "dawn_utc")
    if dawn is None:
        try:
            dawn = armer._dawn()
        except Exception:  # noqa: BLE001
            dawn = None
    if (dusk is not None and now < dusk) or (dawn is not None and now >= dawn):
        return None
    key = _iso(since)
    spells = st.setdefault("closed_roof", [])
    if any(x.get("since") == key for x in spells):
        st["closed_roof_open"] = key
        return None
    st["closed_roof_open"] = key
    left = _dark_left_min(armer, now)
    from photonscript.scheduler import sub_watch
    rec = {"since": key, "t": _iso(now), "dark_left_min": None if left is None
           else round(left)}
    before_dusk = dusk is not None and since < dusk
    when = (f"closed since dusk ({_hm(cfg, dusk)})" if before_dusk
            else f"closed at {_hm(cfg, since)}")
    push = not before_dusk
    if left is None or left < closed_roof_min_left(cfg):
        push = False
        rec.update(plan=[], text=f"roof {when} with "
                   f"{'?' if left is None else int(left)} min to dawn: under "
                   f"{closed_roof_min_left(cfg):g} min, no closed-roof darks")
    else:
        plan = sub_watch.closed_roof_plan(io.darks_owed(cfg), left,
                                          light_lengths=io.light_lengths(cfg, armer),
                                          min_left_min=closed_roof_min_left(cfg))
        push = push and bool(plan)
        rec.update(plan=plan, text=(
            f"roof {when} with {int(left)} min to dawn: "
            + (f"the sequences shoot owed darks until safe or dawn: "
               f"{sub_watch.plan_text(cfg, plan)}" if plan else
               "no darks owed on either rig")))
    spells.append(rec)
    _event(cfg, st["night"], "closed_roof", "Night pilot: " + rec["text"], now,
           rig="rc16", plan=rec.get("plan"))
    rec["pushed"] = push
    if push:
        # PS-212: a mid-night closure with darks owed: act (not safety)
        await io.notify(cfg, "Night pilot: " + rec["text"] + ".", priority=0,
                        kind="act")
    return rec


async def _handle(cfg, armer, io, st, d, ctx, now, m) -> dict:
    dec = decide(cfg, d, ctx)
    rig, fam = d["rig"], d["family"]
    night = st["night"]
    if d.get("source") != "watch":
        # the rig's newest sub doctor reading (guard: no shortening while
        # it reads the elongation as optical)
        st.setdefault("last_cause", {})[rig] = (
            "optics" if dec["kind"] == "optics" else
            "optics_low" if fam == "optics" else fam)
    if dec["kind"] == "optics":
        return await _optics(cfg, io, st, d, dec, now)
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
            kind = "act"
            if fam == "focuser":
                try:   # the focuser guard already sent the act for this rig
                    from photonscript.scheduler.focuser_guard import pushed_tonight
                    if pushed_tonight(cfg, night, rig):
                        kind = "info"
                except Exception:  # noqa: BLE001
                    pass
            await io.notify(cfg, msg, priority=0, kind=kind)
        return {"rig": rig, "kind": "human", "cause": d["cause"]}
    action = dec["action"]
    advise = m == "advise" or not action_on(cfg, action)
    if action == "guider_stop" and live_guard(cfg, armer, ctx) is None:
        await _guider_alert(cfg, io, st, d, now, advise)
    why_not = guard(cfg, armer, st, d, dec, ctx, now, virtual=advise)
    if why_not:
        if _note_once(st, f"held|{rig}|{action}|" + re.sub(r"\d+", "#", why_not)):
            _event(cfg, night, "held", f"{action} on the {_label(cfg, rig)} held: "
                   f"{why_not}", now, rig=rig, cause=d["cause"], action=action)
        return {"rig": rig, "kind": "held", "action": action, "why": why_not}
    st["seq"] = int(st.get("seq") or 0) + 1
    pol = POLICY[action]
    a = {"id": f"a{st['seq']}", "t": _iso(now), "rig": rig, "cause": d["cause"],
         "family": fam, "action": action, "what": dec["what"],
         "because": _because(d), "params": dec["params"],
         "exp_s": ctx.get("exp_s"), "kind": "advise" if advise else "act",
         "baseline": {"metric": pol["metric"],
                      "value": baseline(d, pol["metric"], ctx["subs"])},
         "status": None, "result": None, "effective_t": None, "detail": ""}
    if d.get("source"):
        a["source"] = d["source"]
    a["target"] = ctx.get("target")
    if advise:
        why = ("night_pilot_mode=advise" if m == "advise"
               else switch_text(action))
        a["status"] = "advised"
        # every advised decision that passes the guards is the timeline
        # (the morning report's "would have"); one push and one run event
        # per rig and action per night
        st.setdefault("actions", []).append(a)
        if _note_once(st, f"advise|{rig}|{action}"):
            dk = (dec.get("params") or {}).get("darks_text") or \
                (dec.get("params") or {}).get("how")
            msg = (f"Night pilot (advise): would have {dec['what']} because "
                   f"{a['because']}" + (f" ({dk})" if dk else "")
                   + f" ({why}; nothing was sent).")
            _event(cfg, night, "advise", msg, now, rig=rig, action=action,
                   cause=d["cause"], aid=a["id"])
            await io.notify(cfg, msg, priority=0, kind="info")
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
        await io.notify(cfg, msg, priority=0, kind="info")
    else:
        await _fail(cfg, io, st, a, now, a.get("detail") or "failed")
    return {"rig": rig, "kind": "act", "action": action, "status": a["status"]}


def _evidence_line(d: dict) -> str:
    """The sub doctor's evidence for an optics reading: its headline, else
    the optics_vs_tracking reasons."""
    h = str(d.get("headline") or "").strip()
    if h and h != d.get("cause"):
        return _because(d)
    oc = (d.get("evidence") or {}).get("optics_check") or {}
    rs = [str(r) for r in oc.get("reasons") or [] if r]
    return "; ".join(rs[:2])[:220] if rs else _because(d)


async def _optics(cfg, io, st, d, dec, now) -> dict:
    """An optics diagnosis: one push per rig per night, a run event and the
    advice list. Nothing is sent to NINA and no sub length changes."""
    rig = d["rig"]
    ev_line = _evidence_line(d)
    if _note_once(st, f"optics|{rig}"):
        rec = {"t": _iso(now), "rig": rig, "cause": d["cause"],
               "confidence": d.get("confidence"), "text": dec["what"],
               "evidence": ev_line}
        st.setdefault("optics", []).append(rec)
        msg = f"Night pilot: {dec['what']}. {ev_line}"
        _event(cfg, st["night"], "optics", msg, now, rig=rig, cause=d["cause"],
               confidence=d.get("confidence"))
        await io.notify(cfg, msg, priority=0, kind="info")
    return {"rig": rig, "kind": "optics", "cause": d["cause"]}


async def _guider_alert(cfg, io, st, d, now, advise: bool) -> None:
    """The immediate alert for PHD2 active on an UNGUIDED night (sub watch):
    one priority push per spell (a spell ends when the newest sub of every
    rig was exposed with PHD2 idle again), whatever the guider_stop mode,
    and a run event."""
    if st.get("guider_alert_open"):
        return
    st["guider_alert_open"] = True
    st["guider_alert_t"] = _iso(now)
    what = ("the pilot stops it" if not advise else
            "the pilot only advises (night_pilot_guider_stop_mode); stop PHD2 "
            "or let the unguided guard do it")
    msg = f"Night pilot ALERT: {_because(d)}: {what}."
    _event(cfg, st["night"], "alert", msg, now, rig=d["rig"], cause=d["cause"])
    st.setdefault("alerts", []).append({"t": _iso(now), "rig": d["rig"], "text": msg})
    await io.notify(cfg, msg, priority=1, kind="act")


async def _execute(cfg, armer, io, st, a, ctx, now) -> None:
    action = a["action"]
    if action == "guider_stop":
        # the unguided guard's hook first (pilot_hooks; filled by the
        # ps-unguided-guard branch), then PHD2 stop_capture
        res = await io.guider_hook(cfg, a["rig"], {"because": a["because"],
                                                   **(a.get("params") or {})}, now)
        if isinstance(res, dict):
            ok = bool(res.get("ok"))
            a.update(status="done" if ok else "failed",
                     detail=str(res.get("detail") or "unguided guard hook"),
                     effective_t=_iso(now) if ok else None, hook=True)
            return
        action = "stop_phd2"
    elif action == "refocus":
        action = "run_af"
    elif action == "limit_subs":
        action = "shorten_subs"
    if action == "stop_phd2":
        ok, detail = await io.phd2_stop(cfg)
        a.update(status="done" if ok else "failed", detail=detail,
                 effective_t=_iso(now) if ok else None)
        return
    if action == "unguided":
        _job(a, armer.fallback_unguided(f"night pilot: {a['because']}"))
        a.update(status="running", detail="re-dispatching the remainder unguided")
        return
    if action == "run_af" and a["rig"] == "rc16" and af_direct_on(cfg):
        a["job"] = "direct_af"
        _job(a, io.nina1_autofocus(cfg))
        a.update(status="running", detail="direct autofocus between subs "
                 "(ninaAPI); a restart after the sub if NINA refuses it")
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


def _restart_when(armer) -> str:
    """How a requested restart takes effect, read BEFORE asking (the request
    turns RUNNING into the pause): from RUNNING NINA #1 stops after the
    current sub; from an operator pause NINA #1 has already stopped (or the
    pause's own stop is still waiting for the sub to end)."""
    if str(getattr(armer, "state", "")) == "RUNNING":
        return "restart after the current sub"
    p = getattr(armer, "pause_info", None)
    if isinstance(p, dict) and p.get("phase") == "stopping":
        return "restart once the pause's stop ends (after the current sub)"
    return "restart from the pause (NINA #1 already stopped)"


async def _restart(cfg, armer, a: dict, now: datetime, retry: bool = False) -> None:
    when = _restart_when(armer)
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
                + when


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
    await io.notify(cfg, msg, priority=1, kind="act")


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
            elif a.get("job") == "direct_af" and not a.get("fallback"):
                # the direct AF was refused or did not finish: the restart
                # path (every target start runs AF), once
                a["fallback"] = detail or "not done"
                _event(cfg, st["night"], "retry", f"direct autofocus not done "
                       f"({a['fallback']}); restart after the current sub "
                       "instead", now, rig=a["rig"], aid=a["id"])
                a["detail"] = f"direct autofocus not done ({a['fallback']})"
                await _restart(cfg, armer, a, now)
                if a["status"] == "failed":
                    await _fail(cfg, io, st, a, now, a["detail"])
            else:
                await _fail(cfg, io, st, a, now, detail or
                            f"{a['action']} failed ({getattr(armer, 'detail', '')})")
            continue
        if a.get("status") != "pending":
            continue

        async def _again(a=a):
            await _restart(cfg, armer, a, now, retry=True)
            return a["detail"] if a["status"] == "failed" else None
        out, at = await _pending_restart(cfg, armer, io, st, a.setdefault("restart", {}),
                                         now, what=a["what"], rig=a["rig"],
                                         aid=a["id"], retry=_again)
        if out == "ok":
            a.update(status="done", effective_t=_iso(at))
        elif out not in (None, "retried"):
            await _fail(cfg, io, st, a, now, out)
    # the restart that applies an undo (shorter subs that did not help) gets
    # the same follow-up: one retry once NINA #1 reads idle
    for a in st.get("actions") or []:
        r = (a.get("undo") or {}).get("restart")
        if isinstance(r, dict) and r.get("status") == "pending":
            await _follow_undo_restart(cfg, armer, io, st, a, r, now)


async def _pending_restart(cfg, armer, io, st, r: dict, now, *, what: str,
                           rig: str, aid: str, retry, label: str = "") -> tuple:
    """Follow up one requested restart `r` ({req_t, retried}) against the
    armer's last restart: ("ok", at) once dispatched; (None, None) while
    waiting; ("retried", None) after the one retry (2026-10-13: the
    restart's load ran before NINA #1 had wound down, so wait until it reads
    idle, then retry once); else (failure text, None). `retry` is an async
    callable that asks again and returns a failure text or None."""
    req = _parse_t(r.get("req_t"))
    lr = getattr(armer, "last_restart", None) or {}
    at = _parse_t(lr.get("at"))
    if req is not None and at is not None and at >= req - timedelta(seconds=1):
        if lr.get("ok"):
            _event(cfg, st["night"], "dispatched", f"{what}: restart "
                   "dispatched", now, rig=rig, aid=aid)
            return "ok", at
        if not r.get("retried"):
            idle = await io.nina1_wait_idle(cfg)
            _event(cfg, st["night"], "retry", f"{label}restart failed "
                   f"({lr.get('detail')}); NINA #1 idle={idle}; retrying once",
                   now, rig=rig, aid=aid)
            r["retried"] = True
            bad = await retry()
            return (bad, None) if bad else ("retried", None)
        return f"restart failed twice: {lr.get('detail')}", None
    if req is not None and (now - req).total_seconds() / 60 > RESTART_FOLLOW_MIN:
        return ("the restart was not dispatched within "
                f"{RESTART_FOLLOW_MIN:g} min"), None
    return None, None


async def _ask_restart(armer, now) -> dict:
    try:
        return await armer.restart(when="after_exposure", now=now)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": str(e)}


async def _follow_undo_restart(cfg, armer, io, st, a, r: dict, now) -> None:
    """An undo's restart: dispatched, retried once, or given up with a push
    (the restored lengths then apply from the next dispatch)."""
    what = f"undo of '{a['what']}'"

    async def _again():
        res = await _ask_restart(armer, now)
        if not res.get("ok"):
            return f"restart refused: {res.get('detail')}"
        r["req_t"] = _iso(now)
        if res.get("redispatched"):
            r.update(status="done", at=_iso(now))
        return None
    out, at = await _pending_restart(cfg, armer, io, st, r, now, what=what,
                                     rig=a["rig"], aid=a["id"], retry=_again,
                                     label=f"{what}: ")
    if out == "ok":
        r.update(status="done", at=_iso(at))
    elif out not in (None, "retried"):
        r.update(status="failed", ok=False, detail=out)
        msg = (f"Night pilot: the restart that applies the {what} did not "
               f"happen ({out}): the restored sub lengths apply from the next "
               "dispatch. Check NINA #1.")
        _event(cfg, st["night"], "failed", msg, now, rig=a["rig"], aid=a["id"],
               action="undo_restart")
        await io.notify(cfg, msg, priority=1, kind="act")


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
                await io.notify(cfg, msg, priority=1, kind="act")
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
            await io.notify(cfg, msg, priority=0, kind="info")
            continue
        undo = None
        if group(a["action"]) == "shorten":
            undo = await _undo_shorten(cfg, armer, io, st, a, now)
        esc, human = _escalate(cfg, st, a, now, undo)
        msg = f"Night pilot: '{a['what']}' did NOT help: {nums}. {esc}"
        _event(cfg, night, "verify", msg, now, rig=rig, aid=a["id"],
               result="not_improved", before=before, after=med, human=human)
        if group(a["action"]) == "shorten":
            _learn(cfg, st, a, before, med, now)
        await io.notify(cfg, msg, priority=1 if human else 0,
                        kind="act" if human else "info")


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
        when = _restart_when(armer)
        res = await _ask_restart(armer, now)
        ok = bool(res.get("ok"))
        # followed up in _follow_up like an action's restart (race retry)
        rec["restart"] = {"ok": ok, "detail": res.get("detail"),
                          "req_t": _iso(now), "retried": False,
                          "status": ("done" if res.get("redispatched") else "pending")
                          if ok else "refused"}
        how = (when if ok else
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
            and group(x.get("action")) == group(a["action"]) and x.get("rig") == a["rig"]]
    if group(a["action"]) == "shorten":
        # one step that did not help is enough: trailing that does not
        # follow the sub length is not a drift-per-second problem
        st.setdefault("given_up", {})[key] = _iso(now)
        return ("Shorter subs did not stop the trailing, so it is not a "
                f"drift-per-second problem: {undo or 'sub lengths unchanged'}. "
                "Needs a human (PHD2 stopped? TheSky tracking / ProTrack, "
                "flexure). No more shortening tonight.", True)
    if group(a["action"]) == "af":
        fails = [x for x in acts if x.get("result") == "not_improved"]
        if len(fails) < 2 and len(acts) < POLICY["run_af"]["max"]:
            return ("One more AF at the next diagnosis.", False)
        st.setdefault("given_up", {})[key] = _iso(now)
        return ("Autofocus did not fix it twice: check the focuser (position, "
                "USB) and the AF curve. No more tries tonight.", True)
    if group(a["action"]) == "guider":
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
    if group(a["action"]) == "shorten" and a["result"] == "improved":
        txt = (f"unguided RC16 needs <= {p.get('to_s'):g} s{model} "
               f"({p.get('from_s'):g} s ecc {before}, {p.get('to_s'):g} s ecc "
               f"{after}): consider unguided_max_exposure_s {p.get('to_s'):g} or "
               f"the goal's sub length for {p.get('target')}")
    elif group(a["action"]) == "shorten":
        txt = (f"RC16 trailing is not drift-per-second: it did not follow the "
               f"sub length ({p.get('from_s'):g} s ecc {before}, {p.get('to_s'):g} s "
               f"ecc {after}"
               + (f"; put back to {p.get('from_s'):g} s"
                  if (a.get("undo") or {}).get("ok") else "")
               + "): look for a "
               "non-drift cause (PHD2, flexure, mount) before shortening subs")
    elif group(a["action"]) == "guider":
        txt = ("a PHD2 session left guiding on an unguided night trailed the "
               f"RC16 (ecc {before} -> {after} once stopped): stop PHD2 before "
               "arming unguided")
    elif group(a["action"]) == "af":
        txt = (f"{_label(cfg, a['rig'])} went soft mid-night and an AF fixed it "
               f"(HFR {before} -> {after}): consider a shorter AF interval / "
               "HFR trigger")
    if txt and txt not in st.setdefault("learned", []):
        st["learned"].append(txt)
        _event(cfg, st["night"], "learned", txt, now, rig=a["rig"], aid=a["id"])


# --------------------------------------------------------------------------
# the sub-length ladder (scheduler/sub_ladder.py, 2026-10-25)
# --------------------------------------------------------------------------

def apply_ladder_length(st: dict, store, target: str | None, rig: str,
                        from_s: float, to_s: float,
                        filters=None) -> tuple[bool, str, dict | None]:
    """One ladder step as a tonight-only, ledgered goal override (reverted
    at dawn like a sub cap). RC16: the RC16 filters shot at from_s (the
    window's filters; else the plans at from_s) go to to_s. Piggy-600: the
    goal's "OSC" key. (ok, detail, the overrides before)."""
    from photonscript.scheduler.integrations import resolve_project
    proj = resolve_project(target, store.projects.values()) if target else None
    if proj is None:
        return False, f"no goal for target {target!r}", None
    prev = dict(proj.exposure_overrides or {})
    new = dict(prev)
    if rig == "piggyback":
        new = {k: v for k, v in new.items() if str(k).strip().upper() != "OSC"}
        new["OSC"] = float(to_s)
        changed = [f"OSC {from_s:g} -> {to_s:g} s"]
    else:
        fs = {str(f) for f in filters or [] if f}
        changed = []
        for p in _rc16_plans(proj):
            fv = p.filter_type.value
            cur = float(new.get(fv) or p.exposure_seconds)
            if (fv in fs) if fs else abs(cur - float(from_s)) < 0.5:
                if abs(cur - float(to_s)) >= 0.5 or fv not in new:
                    new[fv] = float(to_s)
                    changed.append(f"{fv} {from_s:g} -> {to_s:g} s")
        if not changed:
            return False, (f"{proj.target.name}: no RC16 filter shot at "
                           f"{from_s:g} s"), None
    e = _ledger_entry(st, proj)
    store.update(proj.id, exposure_overrides=new)
    e["applied_overrides"] = dict(new)
    e["changes"].append({"what": "sub ladder", "rig": rig, "from_s": from_s,
                         "to_s": to_s, "filters": changed})
    e["reverted"] = False
    return True, f"{proj.target.name}: " + ", ".join(changed), prev


def _undo_ladder_length(st: dict, store, target: str | None, prev: dict | None) -> None:
    """Put a step's goal override back (the step could not be applied)."""
    from photonscript.scheduler.integrations import resolve_project
    try:
        proj = resolve_project(target, store.projects.values()) if target else None
        if proj is None or prev is None:
            return
        store.update(proj.id, exposure_overrides=dict(prev))
        e = (st.get("ledger") or {}).get(proj.id)
        if e is not None:
            e["applied_overrides"] = dict(prev)
            e["changes"].append({"what": "sub ladder step not applied"})
    except Exception as e:  # noqa: BLE001
        logger.warning("night pilot: ladder override not put back: %s", e)


def _ladder_st(st: dict, rig: str) -> dict:
    return st.setdefault("ladder", {}).setdefault(rig, {"steps": [], "held": None})


def _ladder_recent(st: dict, rig: str, now: datetime, minutes: float) -> dict | None:
    """The rig's last applied (or applying) ladder step within `minutes`."""
    for s in reversed(_ladder_st(st, rig).get("steps") or []):
        if s.get("kind") != "act" or s.get("status") == "failed":
            continue
        t = _parse_t(s.get("t"))
        if t is not None and (now - t).total_seconds() / 60.0 < minutes:
            return s
        return None
    return None


def _ladder_optics(st: dict, rig: str) -> bool:
    if (st.get("last_cause") or {}).get(rig) in ("optics", "optics_low"):
        return True
    if any(x.get("rig") == rig for x in st.get("optics") or []):
        return True
    return f"ladder_optics|{rig}" in (st.get("notes") or [])


async def ladder(cfg, armer, io, st: dict, ctx: dict, now: datetime) -> list[dict]:
    """The sub-length ladder for each rig (scheduler/sub_ladder.py). Never
    raises."""
    from photonscript.scheduler import sub_ladder as sl
    m = sl.mode(cfg)
    if m == "off":
        return []
    out = []
    rigs = ["rc16"] + (["piggyback"] if ctx.get("piggy") else [])
    for rig in rigs:
        try:
            r = await _ladder_rig(cfg, armer, io, st, ctx, now, m, rig)
        except Exception as e:  # noqa: BLE001
            logger.warning("night pilot sub ladder (%s) failed: %s", rig, e)
            r = None
        if r:
            out.append(r)
    return out


def _ladder_event(cfg, st, now, rig, step, text, **extra) -> None:
    _event(cfg, st["night"], "ladder", text, now, rig=rig, step=step, **extra)


async def _ladder_rig(cfg, armer, io, st, ctx, now, m, rig) -> dict | None:
    from photonscript.scheduler import sub_ladder as sl
    L = _ladder_st(st, rig)
    steps = L.setdefault("steps", [])
    lab = _label(cfg, rig)
    pend = [s for s in steps if s.get("kind") == "act"
            and s.get("status") in ("pending", "running")]
    if pend:
        await _ladder_follow(cfg, armer, io, st, pend[-1], now)
        if pend[-1].get("status") in ("pending", "running"):
            return {"rig": rig, "kind": "ladder", "step": "applying"}
    # a sub doctor optics reading, once seen tonight, holds the ladder
    if (st.get("last_cause") or {}).get(rig) in ("optics", "optics_low"):
        _note_once(st, f"ladder_optics|{rig}")
    done = [s for s in steps if s.get("kind") == "act" and s.get("status") == "done"]
    last = done[-1] if done else None
    since = _parse_t(last.get("effective_t")) if last else None
    last_up = last if last and last.get("dir") == "up" else None
    shortened = any(a.get("rig") == rig and a.get("kind") == "act"
                    and group(a.get("action")) == "shorten"
                    for a in st.get("actions") or [])
    subs = ctx.get("subs") or []
    # a step that is not taking effect (subs still at the old length on
    # the same target): hold, never step the same rung again
    if last and not L.get("held"):
        rows = [r for r in sl.rig_lights(subs, rig)
                if since is None or _sub_start(r) >= since]
        rows = [r for r in rows if sl._tkey(r.get("target")) == sl._tkey(last.get("target"))]
        stale = [r for r in rows if sl._same(sl._exp(r), last.get("from_s"))]
        if len(stale) >= sl.limits(cfg)["up_subs"]:
            L["held"] = {"t": _iso(now), "at_s": float(last["from_s"]),
                         "why": f"subs still at {last['from_s']:g} s after the step to "
                                f"{last['to_s']:g} s"}
            _ladder_event(cfg, st, now, rig, "held", f"Night pilot: {lab} sub ladder "
                          f"holds: {L['held']['why']}")
    dec = sl.evaluate(cfg, rig, subs, since=since, last_up=last_up,
                      held=L.get("held"), optics=_ladder_optics(st, rig),
                      shortened=shortened,
                      dark_status=io.ladder_dark_status(cfg, rig))
    night = st["night"]
    tk = sl._tkey(dec.get("target"))
    if dec.get("proven_s"):
        failed = dec["from_s"] if dec["kind"] == "down" else None
        if dec["kind"] != "down" or m == "act":
            if sl.record_best(cfg, night, rig, dec["target"], dec["proven_s"],
                              ecc=dec.get("ecc"), n=dec.get("n") or 0,
                              failed_s=failed, now=now):
                L.setdefault("best", {})[tk] = {"target": dec["target"],
                                                "rung_s": dec["proven_s"],
                                                "failed_s": failed}
    kind = dec["kind"]
    if kind == "blocked":
        key = re.sub(r"\d+(\.\d+)?", "#", dec["why"])
        if _note_once(st, f"ladder_blocked|{rig}|{tk}|{dec['from_s']}|{key}"):
            _ladder_event(cfg, st, now, rig, "blocked",
                          f"Night pilot: {lab} sub ladder stays at {dec['from_s']:g} s"
                          f"{' on ' + str(dec['target']) if dec.get('target') else ''}"
                          f" (next {dec['to_s']:g} s): {dec['why']}"
                          if dec.get("to_s") else
                          f"Night pilot: {lab} sub ladder stays at "
                          f"{dec['from_s']:g} s: {dec['why']}",
                          target=dec.get("target"), from_s=dec["from_s"],
                          to_s=dec.get("to_s"))
        return {"rig": rig, "kind": "ladder", "step": "blocked", "why": dec["why"]}
    if kind not in ("up", "down"):
        return None
    would_key = f"ladder_would|{rig}|{tk}|{dec['from_s']}|{dec['to_s']}"
    if m == "advise" and would_key in (st.get("notes") or []):
        return {"rig": rig, "kind": "ladder", "step": "advised"}
    # the live guards (armer RUNNING, calibration, arm recipe, NINA #1
    # busy items, PHD2 calibrating): advise and act alike
    live = ctx.get("running") is not None
    running, target = (ctx.get("running"), ctx.get("target")) if live else \
        await io.nina1_running(cfg)
    why = live_guard(cfg, armer, {"running": running, "target": target or ctx.get("target"),
                                  "phd2": ctx.get("phd2")})
    if why is None and m == "act":
        left = _dark_left_min(armer, now)
        if left is not None and left < RESTART_DARK_MIN:
            why = f"only {max(0, int(left))} min of dark left"
        pend_a = [a for a in st.get("actions") or [] if a.get("kind") == "act"
                  and a.get("rig") == rig and a.get("result") is None
                  and a.get("status") in ("pending", "running", "done")]
        if why is None and pend_a:
            why = f"waiting for the {pend_a[-1]['action']} check on the {lab}"
    if why:
        if _note_once(st, f"ladder_held|{rig}|{kind}|" + re.sub(r"\d+", "#", why)):
            _ladder_event(cfg, st, now, rig, "blocked", f"Night pilot: {lab} sub "
                          f"ladder step to {dec['to_s']:g} s held: {why}")
        return {"rig": rig, "kind": "ladder", "step": "held", "why": why}
    st["seq"] = int(st.get("seq") or 0) + 1
    s = {"id": f"l{st['seq']}", "t": _iso(now), "rig": rig, "dir": kind,
         "target": dec.get("target"), "from_s": dec["from_s"], "to_s": dec["to_s"],
         "n": dec.get("n"), "ecc": dec.get("ecc"), "why": dec["why"],
         "filters": dec.get("filters") or [], "kind": m, "status": None,
         "effective_t": None, "detail": ""}
    if m == "advise":
        _note_once(st, would_key)
        s["status"] = "advised"
        steps.append(s)
        msg = (f"Night pilot (advise): {sl.would_text(cfg, dec)} "
               "(night_pilot_ladder_mode=advise; nothing was sent)")
        _ladder_event(cfg, st, now, rig, "would", msg, target=dec.get("target"),
                      from_s=dec["from_s"], to_s=dec["to_s"], aid=s["id"])
        await io.notify(cfg, msg, priority=0, kind="info")
        return {"rig": rig, "kind": "ladder", "step": "advised", "to_s": dec["to_s"]}
    steps.append(s)
    await _ladder_execute(cfg, armer, io, st, s, ctx, now)
    if s["status"] == "done":
        await _ladder_done(cfg, io, st, s, now)
    elif s["status"] == "failed":
        await _ladder_failed(cfg, io, st, s, now)
    else:
        _ladder_event(cfg, st, now, rig, "applying", f"Night pilot: {lab} sub ladder "
                      f"{s['from_s']:g} -> {s['to_s']:g} s on {s.get('target')}: "
                      f"{s.get('detail')}", aid=s["id"])
    return {"rig": rig, "kind": "ladder", "step": kind, "status": s["status"],
            "to_s": s["to_s"]}


async def _ladder_execute(cfg, armer, io, st, s, ctx, now) -> None:
    """Apply one step: the goal override, then the RC16 restart after the
    current sub, or the Piggy-600 companion stopped after its sub and
    re-dispatched with the new length."""
    store = io.store()
    target = _goal_target(store, s.get("target"), ctx.get("target"),
                          ctx.get("last_target"))
    ok, detail, prev = apply_ladder_length(st, store, target, s["rig"], s["from_s"],
                                           s["to_s"], s.get("filters"))
    if not ok:
        s.update(status="failed", detail=detail)
        return
    s.update(goal=target, prev_overrides=prev, detail=detail + " for tonight")
    if s["rig"] == "piggyback":
        from photonscript.scheduler import piggy_length as pl
        plan = getattr(armer, "plan", None)
        if not isinstance(plan, dict):
            plan = {}
            armer.plan = plan
        old = plan.get("piggy_length")
        rec, applied = pl.with_target_length(
            old, s.get("target") or target, s["to_s"],
            float(getattr(cfg, "piggyback_exposure_s", 120.0) or 120.0))
        if applied < float(s["to_s"]) - 0.5:
            _undo_ladder_length(st, store, target, prev)
            s.update(status="failed", detail=f"the applied unguided Piggy limit "
                     f"caps tonight at {applied:g} s", capped=True)
            return
        s["prev_piggy_length"] = old
        plan["piggy_length"] = rec
        _job(s, io.piggy_redispatch(cfg, armer))
        s["asked"] = True
        s.update(status="running", detail=s["detail"] + "; NINA #2 stops after "
                 "its sub, then the companion is re-dispatched")
        return
    await _restart(cfg, armer, s, now)
    if s["status"] == "failed":
        _undo_ladder_length(st, store, target, prev)
        return
    s["asked"] = True
    if s["status"] == "done":
        s["effective_t"] = _iso(now)


async def _ladder_follow(cfg, armer, io, st, s, now) -> None:
    """A step being applied: the RC16 restart dispatched (one retry on the
    race), or the Piggy-600 re-dispatch job finished."""
    if s.get("status") == "running":
        task = MONITOR.jobs.get(s["id"])
        t0 = _parse_t(s.get("t")) or now
        age = (now - t0).total_seconds() / 60
        if task is None or (not task.done() and age > JOB_TIMEOUT_MIN):
            if task is None and age < 1:
                return
            s.update(status="failed", detail=s.get("detail", "") + "; the "
                     "re-dispatch job was lost (service restart?) or timed out")
            await _ladder_failed(cfg, io, st, s, now)
            return
        if not task.done():
            return
        MONITOR.jobs.pop(s["id"], None)
        res = task.result()
        ok, detail = (res if isinstance(res, tuple) else (bool(res), ""))
        if ok:
            s.update(status="done", effective_t=_iso(now),
                     detail=s.get("detail", "") + (f"; {detail}" if detail else ""))
            await _ladder_done(cfg, io, st, s, now)
        else:
            s.update(status="failed", detail=s.get("detail", "") + f"; {detail}")
            await _ladder_failed(cfg, io, st, s, now)
        return
    if s.get("status") != "pending":
        return

    async def _again():
        await _restart(cfg, armer, s, now, retry=True)
        return s["detail"] if s["status"] == "failed" else None
    out, at = await _pending_restart(cfg, armer, io, st, s.setdefault("restart", {}),
                                     now, what=f"sub ladder {s['from_s']:g} -> "
                                     f"{s['to_s']:g} s", rig=s["rig"], aid=s["id"],
                                     retry=_again)
    if out == "ok":
        s.update(status="done", effective_t=_iso(at))
        await _ladder_done(cfg, io, st, s, now)
    elif out not in (None, "retried"):
        s.update(status="failed", detail=out)
        await _ladder_failed(cfg, io, st, s, now)


async def _ladder_done(cfg, io, st, s, now) -> None:
    """A step applied: the run event and one story push for the step; a
    step down holds the rig for the rest of the night."""
    from photonscript.scheduler import sub_ladder as sl
    rig = s["rig"]
    L = _ladder_st(st, rig)
    if s["dir"] == "down":
        L["held"] = {"t": _iso(now), "at_s": float(s["to_s"]), "why": s["why"]}
    dec = {"rig": rig, "kind": s["dir"], "from_s": s["from_s"], "to_s": s["to_s"],
           "n": s.get("n"), "ecc": s.get("ecc"), "why": s.get("why")}
    msg = f"Night pilot: {sl.step_text(cfg, dec)}"
    if s.get("target"):
        msg += f" on {s['target']}"
    _ladder_event(cfg, st, now, rig, s["dir"], msg + f" ({s.get('detail')})",
                  aid=s["id"], target=s.get("target"), from_s=s["from_s"],
                  to_s=s["to_s"])
    if not s.get("pushed"):
        s["pushed"] = True
        await io.notify(cfg, msg + ".", priority=0, kind="story",
                        fact=f"ladder:{st['night']}:{rig}:{s['id']}")


async def _ladder_failed(cfg, io, st, s, now) -> None:
    """A step that could not be applied: a run event; one act push when a
    sequence change was asked for and did not happen (a capped or goal-less
    step is a run event only). The rig holds tonight."""
    rig = s["rig"]
    L = _ladder_st(st, rig)
    L["held"] = L.get("held") or {"t": _iso(now), "at_s": float(s["from_s"]),
                                  "why": f"the step to {s['to_s']:g} s failed"}
    msg = (f"Night pilot: the {_label(cfg, rig)} sub ladder step "
           f"{s['from_s']:g} -> {s['to_s']:g} s on {s.get('target')} was not "
           f"applied: {s.get('detail')}. The {_label(cfg, rig)} stays at "
           f"{s['from_s']:g} s tonight.")
    _ladder_event(cfg, st, now, rig, "failed", msg, aid=s["id"])
    if s.get("asked") and not s.get("pushed"):
        s["pushed"] = True
        await io.notify(cfg, msg + " Check NINA.", priority=1, kind="act",
                        fact=f"ladder-failed:{st['night']}:{rig}:{s['id']}")


def ladder_lines(cfg, st: dict) -> list[str]:
    """The sub ladder in the morning report / status: one line per rig."""
    from photonscript.scheduler import sub_ladder as sl
    out = []
    for rig, L in (st.get("ladder") or {}).items():
        steps = L.get("steps") or []
        lab = sl._label(cfg, rig)
        acts = [s for s in steps if s.get("kind") == "act"]
        adv = [s for s in steps if s.get("kind") == "advise"]
        bits = []
        for s in acts:
            t = _hm(cfg, _parse_t(s.get("t")))
            r = {"done": "", "failed": f" NOT applied ({s.get('detail')})"}.get(
                s.get("status"), f" ({s.get('status')})")
            bits.append(f"{t} {s['from_s']:g} -> {s['to_s']:g} s"
                        + (f" on {s['target']}" if s.get("target") else "") + r)
        if adv:
            bits += [f"{_hm(cfg, _parse_t(s.get('t')))} would step {s['from_s']:g} "
                     f"-> {s['to_s']:g} s" + (f" on {s['target']}" if s.get("target") else "")
                     for s in adv]
        held = L.get("held")
        if held:
            bits.append(f"held at {held.get('at_s'):g} s ({held.get('why')})")
        for b in (L.get("best") or {}).values():
            bits.append(f"best {b.get('target')} {b.get('rung_s'):g} s")
        if bits:
            out.append(f"Night pilot sub ladder {lab}: " + "; ".join(bits))
    return out


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


def _would_line(cfg, a: dict) -> str:
    """One "would have" item: "02:31 stopped PHD2 guiding (unguided night)
    on the RC16"."""
    t = _hm(cfg, _parse_t(a.get("t")))
    what = str(a.get("what") or a.get("action"))
    lab = _label(cfg, a.get("rig") or "rc16")
    return f"{t} {what}" + ("" if lab in what else f" on the {lab}")


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
            "given_up": st.get("given_up") or {}, "tick": MONITOR.last,
            "closed_roof": st.get("closed_roof") or [],
            "alerts": st.get("alerts") or [],
            "optics": st.get("optics") or [],
            "advice": advice_lines(cfg, st),
            "ladder": {"mode": _ladder_mode(cfg), "rigs": st.get("ladder") or {},
                       "lines": ladder_lines(cfg, st)},
            "watch": {"on": _watch_on(cfg), "seen": st.get("watch_seen") or {},
                      "modes": {k: action_mode(cfg, k) for k in
                                ("refocus", "limit_subs", "guider_stop")}}}


def advice_lines(cfg, st: dict) -> list[str]:
    """What the pilot advises a human tonight without acting (optics)."""
    return [f"{_hm(cfg, _parse_t(x.get('t')))} {x.get('text')}"
            + (f" ({x['evidence']})" if x.get("evidence") else "")
            for x in st.get("optics") or []]


def _ladder_mode(cfg) -> str:
    from photonscript.scheduler.sub_ladder import mode as _m
    return _m(cfg)


def _watch_on(cfg) -> bool:
    from photonscript.scheduler.sub_watch import watch_on
    return watch_on(cfg)


def morning_lines(cfg, date: str) -> list[str]:
    """The morning report section: one line for the night's actions, one
    per learned suggestion. Empty when the pilot did nothing."""
    p = state_path(cfg, date)
    if not p.exists():
        return []
    st = load_state(cfg, date)
    acts = [a for a in st.get("actions") or [] if a.get("kind") == "act"]
    adv = [a for a in st.get("actions") or [] if a.get("kind") == "advise"]
    out = []
    if acts:
        out.append("Night pilot: " + "; ".join(_action_line(cfg, a) for a in acts[:6])
                   + (f"; +{len(acts) - 6} more" if len(acts) > 6 else ""))
    if adv:
        out.append("Night pilot would have: "
                   + "; ".join(_would_line(cfg, a) for a in adv[:10])
                   + (f"; +{len(adv) - 10} more" if len(adv) > 10 else "")
                   + " (advise: nothing was sent)")
    for x in advice_lines(cfg, st):
        out.append("Night pilot advice: " + x)
    for x in st.get("closed_roof") or []:
        out.append("Night pilot closed roof: " + str(x.get("text") or ""))
    out += ladder_lines(cfg, st)
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
