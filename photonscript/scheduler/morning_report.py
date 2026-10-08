"""PS-166: the morning report card, one summary of last night per rig.

    report_card(config, date=None, *, projects=None, calibration=True) -> dict
    card_lines(card, calibration=True) -> list[str]   plain text, push / CLI

Built only from data other passes already keep (nothing is measured here):

  * lights: the night's subs log (runs/<night>_subs.jsonl), test subs left
    out: kept (passed QA; approved or waiting for review) and rejected,
    with the top reject drivers (scorecard check ids: ecc, hfr, stars, ...)
  * hours per goal: hours of the kept subs per target tonight, next to the
    goal's total integration and budget (project store)
  * calibration owed: calibration_owed.morning_note (PS-160)
  * library filed: the dawn filing record's line (dawn_autofile, PS-157)
  * guiding health (RC16, the guided mount): the live guard summary
    (episodes, PS-155 no-corrections episodes and the subs graded inside
    them, PS-165), the PS-156 unguided fallback, the median guide RMS
  * stalls: the NINA watchdog's alarms of the night (PS-150 / PS-154 run
    events kind nina_watch: not running, API down, silent, stuck, parked)
  * tracking (PS-168): the night's drift line from the PHD2 guide log
    (tracking_drift.morning_line: RA / Dec "/min, 300 s smear, wobble)

The dashboard shows it as the "Morning report" card (GET
/api/morning/report); the dawn "Night complete" push carries card_lines
(the calibration line is already its own line there). Never raises: a
section that cannot be built says so.
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter
from datetime import datetime

logger = logging.getLogger(__name__)

STALL_STATES = ("not_running", "api_down", "silent", "stuck", "parked")
TOP_REASONS = 3


def latest_night(config, today: str | None = None) -> str | None:
    """The newest night with a subs log, not after `today`."""
    from photonscript.scheduler.runs import runs_dir
    today = today or datetime.now().strftime("%Y-%m-%d")
    try:
        names = sorted(p.name[:10] for p in runs_dir(config).glob("*_subs.jsonl"))
    except OSError:
        return None
    names = [n for n in names if n <= today]
    return names[-1] if names else None


def _goal_index(projects) -> dict:
    from photonscript.shared.target_names import target_key
    out = {}
    for p in projects or []:
        try:
            out[target_key(p.target.name)] = p
        except Exception:  # noqa: BLE001
            continue
    return out


def _lights(config, subs: list[dict], rig: str, goals: dict) -> dict:
    from photonscript.shared.qa_rules import is_test_record
    from photonscript.shared.target_names import canonical_target, target_key
    mine = [s for s in subs if (s.get("rig") or "rc16") == rig
            and not is_test_record(s)]
    kept = [s for s in mine if s.get("passed_qa")]
    rejected = [s for s in mine if not s.get("passed_qa")]
    drivers = Counter(d for s in rejected for d in (s.get("drivers") or [])
                      or ["unspecified"])
    secs: dict[str, float] = {}
    for s in kept:
        name = canonical_target(s.get("target")) or "?"
        try:
            secs[name] = secs.get(name, 0.0) + float(s.get("exp_s") or 0)
        except (TypeError, ValueError):
            continue
    hours = []
    for name, sec in sorted(secs.items(), key=lambda kv: -kv[1]):
        p = goals.get(target_key(name)) if name != "?" else None
        hours.append({"target": name, "hours": round(sec / 3600.0, 2),
                      "goal_total_h": (round(float(p.total_integration_hours), 1)
                                       if p is not None else None),
                      "goal_budget_h": (round(float(p.budget_hours), 1)
                                        if p is not None else None)})
    return {"subs": len(mine), "kept": len(kept), "rejected": len(rejected),
            "approved": sum(1 for s in kept if s.get("reviewed")),
            "review": sum(1 for s in kept if not s.get("reviewed")),
            "top_reasons": [{"driver": d, "n": n}
                            for d, n in drivers.most_common(TOP_REASONS)],
            "hours": hours,
            "hours_total": round(sum(secs.values()) / 3600.0, 2)}


def _guiding(config, date: str, subs: list[dict]) -> dict:
    from photonscript.scheduler.routers.phd2 import guard_summary
    out: dict = {}
    try:
        g = guard_summary(config, date)
        fb = g.get("fallback") or {}
        out.update(episodes=g.get("episodes", 0), non_star=g.get("non_star", 0),
                   no_corrections=g.get("no_corrections", 0),
                   nocorr_subs=g.get("nocorr_subs", 0),
                   fallback=(fb.get("value") or fb.get("detail") or True)
                   if fb else None)
    except Exception as e:  # noqa: BLE001
        out["error"] = f"guard summary unavailable: {e}"
    from photonscript.shared.qa_rules import is_test_record
    rms = []
    for s in subs:
        if (s.get("rig") or "rc16") != "rc16" or is_test_record(s):
            continue
        if str(s.get("guide_state") or "").lower() not in ("guiding", "settling"):
            continue
        try:
            if s.get("guide_rms") is not None:
                rms.append(float(s["guide_rms"]))
        except (TypeError, ValueError):
            continue
    out["guided_subs"] = len(rms)
    out["rms_median"] = round(statistics.median(rms), 2) if rms else None
    bad = bool(out.get("no_corrections") or out.get("non_star")
               or out.get("fallback"))
    out["status"] = "attention" if bad else ("ok" if rms else "unguided")
    return out


def _stalls(config, date: str) -> dict[str, list[dict]]:
    """{rig: [{"t", "state"}]} of the night's NINA watchdog alarms."""
    from photonscript.shared import phd2_store as store
    from photonscript.shared.night_events import events_path
    out: dict[str, list[dict]] = {}
    for r in store.read_jsonl(events_path(config, date)):
        if r.get("kind") != "nina_watch" or r.get("value") not in STALL_STATES:
            continue
        out.setdefault(r.get("rig") or "rc16", []).append(
            {"t": r.get("t"), "state": r.get("value")})
    return out


def report_card(config, date: str | None = None, *, projects=None,
                calibration: bool = True) -> dict:
    """The card for one night (default: the newest night with a subs log)."""
    from photonscript.shared.rigs import rig_ids, rig_label
    date = date or latest_night(config)
    out = {"date": date,
           "generated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
           "rigs": [], "calibration": None, "library": None, "tracking": None}
    if not date:
        out["note"] = "no subs log yet"
        return out
    try:
        from photonscript.scheduler.runs import _load_subs
        subs = _load_subs(config, date)
    except Exception as e:  # noqa: BLE001
        logger.warning("morning report: subs log %s unreadable: %s", date, e)
        subs = []
    if projects is None:
        try:
            from photonscript.scheduler.calibration_plan import load_projects
            projects = load_projects(config)
        except Exception:  # noqa: BLE001
            projects = []
    goals = _goal_index(projects)
    try:
        stalls = _stalls(config, date)
    except Exception as e:  # noqa: BLE001
        logger.debug("morning report: no events log: %s", e)
        stalls = {}
    for rig in rig_ids(config):
        r = {"rig": rig, "name": rig_label(config, rig),
             "lights": _lights(config, subs, rig, goals),
             "stalls": stalls.get(rig, [])}
        if rig == "rc16":
            r["guiding"] = _guiding(config, date, subs)
        out["rigs"].append(r)
    if calibration:
        try:
            from photonscript.scheduler.calibration_owed import morning_note
            out["calibration"] = morning_note(config) or "Calibration: nothing owed"
        except Exception as e:  # noqa: BLE001
            out["calibration"] = f"Calibration owed: unavailable ({e})"
    try:
        from photonscript.scheduler.dawn_autofile import morning_line
        out["library"] = morning_line(config, date)
    except Exception as e:  # noqa: BLE001
        logger.debug("morning report: no dawn filing record: %s", e)
    try:
        from photonscript.scheduler.tracking_drift import morning_line as drift_line
        out["tracking"] = drift_line(config, date)
    except Exception as e:  # noqa: BLE001
        logger.debug("morning report: no tracking drift: %s", e)
    out["lines"] = card_lines(out, calibration=calibration)
    return out


def _rig_line(r: dict) -> str:
    li = r["lights"]
    bits = [f"{li['kept']} kept / {li['rejected']} rejected"]
    if li["review"]:
        bits.append(f"{li['review']} to review")
    if li["top_reasons"]:
        bits.append("rejects: " + ", ".join(f"{x['driver']} {x['n']}"
                                            for x in li["top_reasons"]))
    if li["hours"]:
        hrs = []
        for h in li["hours"][:4]:
            t = f"{h['target']} {h['hours']:.1f} h"
            if h["goal_total_h"] is not None and h["goal_budget_h"]:
                t += f" ({h['goal_total_h']:g}/{h['goal_budget_h']:g} h)"
            hrs.append(t)
        bits.append("hours: " + ", ".join(hrs))
    g = r.get("guiding")
    if g:
        gb = []
        if g.get("rms_median") is not None:
            gb.append(f"RMS {g['rms_median']:g}")
        if g.get("no_corrections"):
            gb.append(f"{g['no_corrections']} no-corrections episode(s)"
                      + (f", {g['nocorr_subs']} subs unguided-in-name"
                         if g.get("nocorr_subs") else ""))
        if g.get("non_star"):
            gb.append(f"{g['non_star']} non-star lock(s)")
        if g.get("fallback"):
            gb.append("unguided fallback ran")
        bits.append("guiding: " + (", ".join(gb) if gb else g.get("status", "-")))
    if r.get("stalls"):
        c = Counter(s["state"] for s in r["stalls"])
        bits.append("stalls: " + ", ".join(f"{k} {v}" for k, v in sorted(c.items())))
    return f"{r['name']}: " + "; ".join(bits)


def card_lines(card: dict, calibration: bool = True) -> list[str]:
    """The card as text lines (one per rig that shot or stalled, then
    library and calibration)."""
    if not card.get("date"):
        return ["Morning report: no subs log yet"]
    lines = [_rig_line(r) for r in card.get("rigs") or []
             if r["lights"]["subs"] or r.get("stalls")]
    if card.get("tracking"):
        lines.append(card["tracking"])
    if card.get("library"):
        lines.append(card["library"])
    if calibration and card.get("calibration"):
        lines.append(card["calibration"])
    return lines


def push_text(config, date: str | None) -> str | None:
    """PS-166 lines for the dawn "Night complete" push (the calibration line
    is already its own line there). None when nothing can be said."""
    try:
        card = report_card(config, date, calibration=False)
    except Exception as e:  # noqa: BLE001 - the dawn push always goes out
        logger.debug("morning report card unavailable: %s", e)
        return None
    if not card.get("date"):
        return None
    return "\n".join(card_lines(card, calibration=False)) or None
