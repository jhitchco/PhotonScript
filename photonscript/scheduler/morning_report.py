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
  * calibration completeness (PS-182): calibration_completeness.morning_line,
    "Calibration: complete for <targets> / missing <...>" (captured, QA,
    Library and the Syncthing cache's view of the desktop)
  * library filed: the dawn filing record's line (dawn_autofile, PS-157)
  * guiding health (RC16, the guided mount): the live guard summary
    (episodes, PS-155 no-corrections episodes and the subs graded inside
    them, PS-165), the PS-156 unguided fallback, the median guide RMS
  * stalls: the NINA watchdog's alarms of the night (PS-150 / PS-154 run
    events kind nina_watch: not running, API down, silent, stuck, parked)
  * NINA offline (run events kind nina_offline, value down): counted
    with the stalls as "offline"
  * autofocus not finishing (run events kind af_stall): "autofocus: N
    runs in a row did not finish (points 1, 1, 1)"
  * tracking (PS-168): the night's drift line from the PHD2 guide log
    (tracking_drift.morning_line: RA / Dec "/min, 300 s smear, wobble)
  * NINA log size (PS-174): a line only when a rig's newest NINA log is
    over nina_log_warn_mb or on pace to pass it (nina_log_watch)

The dashboard shows it as the "Morning report" card (GET
/api/morning/report); the dawn "Night complete" push carries card_lines
(the calibration line is already its own line there). Never raises: a
section that cannot be built says so.

fit_push (2026-10-12) keeps that push inside Pushover's 1024 characters:
every line has a priority (PRI_KEEP headline and shutdown failure, PRI_RIG
per-rig card lines and the split note, PRI_NOTE calibration owed and
integrations, PRI_DETAIL the rest of the card); details go first, a line is
cut with "..." before it is dropped, and a trimmed push ends with the
dashboard link (the card there has every line).
"""

from __future__ import annotations

import logging
import re
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
        if r.get("kind") == "nina_offline" and r.get("value") == "down":
            state = "offline"
        elif r.get("kind") == "nina_watch" and r.get("value") in STALL_STATES:
            state = r.get("value")
        else:
            continue
        out.setdefault(r.get("rig") or "rc16", []).append(
            {"t": r.get("t"), "state": state})
    return out


def _af_stalls(config, date: str) -> dict[str, dict]:
    """{rig: the night's last af_stall run event} (autofocus that kept
    restarting without finishing)."""
    from photonscript.shared import phd2_store as store
    from photonscript.shared.night_events import events_path
    out: dict[str, dict] = {}
    for r in store.read_jsonl(events_path(config, date)):
        if r.get("kind") == "af_stall":
            out[r.get("rig") or "rc16"] = {"t": r.get("t"),
                                           "runs": r.get("runs") or r.get("value"),
                                           "points": r.get("points") or []}
    return out


def report_card(config, date: str | None = None, *, projects=None,
                calibration: bool = True, completeness: bool = True) -> dict:
    """The card for one night (default: the newest night with a subs log)."""
    from photonscript.shared.rigs import rig_ids, rig_label
    date = date or latest_night(config)
    out = {"date": date,
           "generated": datetime.utcnow().isoformat(timespec="seconds") + "Z",
           "rigs": [], "calibration": None, "library": None, "tracking": None,
           "nina_logs": None, "completeness": None}
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
    try:
        af = _af_stalls(config, date)
    except Exception as e:  # noqa: BLE001
        logger.debug("morning report: no af stall events: %s", e)
        af = {}
    for rig in rig_ids(config):
        r = {"rig": rig, "name": rig_label(config, rig),
             "lights": _lights(config, subs, rig, goals),
             "stalls": stalls.get(rig, []), "af_stall": af.get(rig)}
        if rig == "rc16":
            r["guiding"] = _guiding(config, date, subs)
        out["rigs"].append(r)
    if calibration:
        try:
            from photonscript.scheduler.calibration_owed import morning_note
            out["calibration"] = morning_note(config) or "Calibration: nothing owed"
        except Exception as e:  # noqa: BLE001
            out["calibration"] = f"Calibration owed: unavailable ({e})"
    if completeness:   # PS-182
        try:
            from photonscript.scheduler.calibration_completeness import cached_line
            from photonscript.scheduler.routers.calibration import pending_names
            pending, capped = pending_names()
            out["completeness"] = cached_line(config, projects=projects,
                                              pending=pending, pending_capped=capped)
        except Exception as e:  # noqa: BLE001
            out["completeness"] = f"Calibration: completeness unavailable ({e})"
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
    try:   # PS-174: NINA log size watch (file sizes only, no log read)
        from photonscript.scheduler.nina_log_watch import log_sizes, morning_line
        sizes = log_sizes(config)
        out["nina_logs"] = {"logs": sizes, "line": morning_line(config, sizes)}
    except Exception as e:  # noqa: BLE001
        logger.debug("morning report: no NINA log sizes: %s", e)
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
    a = r.get("af_stall")
    if a:
        pts = ", ".join(str(p) for p in a.get("points") or [])
        bits.append(f"autofocus: {a.get('runs')} runs in a row did not finish"
                    + (f" (points {pts})" if pts else ""))
    return f"{r['name']}: " + "; ".join(bits)


def card_lines(card: dict, calibration: bool = True) -> list[str]:
    """The card as text lines (one per rig that shot or stalled, then
    library and calibration)."""
    if not card.get("date"):
        return ["Morning report: no subs log yet"]
    lines = [_rig_line(r) for r in card.get("rigs") or []
             if r["lights"]["subs"] or r.get("stalls") or r.get("af_stall")]
    if card.get("tracking"):
        lines.append(card["tracking"])
    if card.get("library"):
        lines.append(card["library"])
    if (card.get("nina_logs") or {}).get("line"):
        lines.append(card["nina_logs"]["line"])
    if card.get("completeness"):   # PS-182
        lines.append(card["completeness"])
    if calibration and card.get("calibration"):
        lines.append(card["calibration"])
    return lines


def push_text(config, date: str | None) -> str | None:
    """PS-166 lines for the dawn "Night complete" push (the calibration line
    is already its own line there). None when nothing can be said. PS-182:
    the completeness line rides along only when the card has something
    else to say (a night with subs or stalls)."""
    try:
        card = report_card(config, date, calibration=False)
    except Exception as e:  # noqa: BLE001 - the dawn push always goes out
        logger.debug("morning report card unavailable: %s", e)
        return None
    if not card.get("date"):
        return None
    lines = card_lines({**card, "completeness": None}, calibration=False)
    if not lines:
        return None
    if card.get("completeness"):
        lines.append(card["completeness"])
    return "\n".join(lines)


# --------------------------------------------------------------------------
# fitting the dawn push into Pushover's message limit (PS-166, 2026-10-12)
# --------------------------------------------------------------------------

# Pushover takes at most 1024 characters of message text; the rate limiter
# may append "\n[+N alert(s) suppressed]" (pushover.notify), so the push is
# fitted to the limit minus that margin. 2026-10-11: the 10-10 "PhotonScript
# complete" push was about 1.2k characters (shutdown steps, split, two
# integration lines, calibration owed, four card lines).
PUSH_MAX_CHARS = 1024
PUSH_NOTE_MARGIN = 40
PUSH_MIN_PART = 40          # shorter than this, a trimmed line is dropped
DASHBOARD_URL = "https://teles-feb25.lobster-bleak.ts.net/"

# Line priorities, lowest kept longest. KEEP is never dropped (the night's
# headline and a shutdown failure); RIG is a per-rig card line or the split
# note; NOTE the calibration owed and integration lines; DETAIL the rest of
# the card (tracking, library, NINA log size, calibration completeness).
PRI_KEEP, PRI_RIG, PRI_NOTE, PRI_DETAIL = 0, 1, 2, 3

_RIG_LINE = re.compile(r"^[^:\n]{1,40}: \d+ kept / \d+ rejected")


def card_line_priority(line: str) -> int:
    """PRI_RIG for a per-rig card line (_rig_line: "<rig>: N kept / M
    rejected..."), else PRI_DETAIL."""
    return PRI_RIG if _RIG_LINE.match(line or "") else PRI_DETAIL


def card_parts(text: str | None) -> list[tuple[int, str]]:
    """push_text's block as (priority, line) parts for fit_push."""
    return [(card_line_priority(ln), ln) for ln in (text or "").splitlines() if ln]


def _cut(line: str, n: int) -> str:
    return line[:max(0, n - 3)].rstrip(" ;,") + "..."


def fit_push(parts: list[tuple[int, str]], limit: int | None = None,
             link: str | None = DASHBOARD_URL) -> str:
    """Join (priority, line) parts with newlines so the result fits
    `limit` characters (default PUSH_MAX_CHARS - PUSH_NOTE_MARGIN). Lines
    keep their order. When it does not fit, the highest priority number
    goes first, last line first: a line is cut with "..." when at least
    PUSH_MIN_PART characters of it still fit, else dropped; PRI_KEEP lines
    are only cut as a last resort. A trimmed push ends with a link to the
    dashboard, whose Morning report card has every line."""
    limit = int(limit if limit is not None else PUSH_MAX_CHARS - PUSH_NOTE_MARGIN)
    lines = [[int(p), str(t)] for p, t in parts if t]

    def size(ls, extra=""):
        return len("\n".join(t for _p, t in ls)) + (len(extra) + 1 if extra else 0)

    if size(lines) <= limit:
        return "\n".join(t for _p, t in lines)
    tail = (f"(trimmed: full report {link})" if link else
            "(trimmed to fit Pushover)")
    order = sorted(range(len(lines)), key=lambda i: (-lines[i][0], -i))
    for i in order:
        if lines[i][0] == PRI_KEEP:
            break
        over = size([ln for ln in lines if ln[1]], tail) - limit
        if over <= 0:
            break
        t = lines[i][1]
        keep = len(t) - over
        if keep >= PUSH_MIN_PART:
            lines[i][1] = _cut(t, keep)
            break
        lines[i][1] = ""
    lines = [ln for ln in lines if ln[1]]
    over = size(lines, tail) - limit
    if over > 0:   # only KEEP lines left and still too long: cut the longest
        j = max(range(len(lines)), key=lambda k: len(lines[k][1]))
        lines[j][1] = _cut(lines[j][1], max(PUSH_MIN_PART, len(lines[j][1]) - over))
    out = "\n".join([t for _p, t in lines] + [tail])
    return out if len(out) <= limit else _cut(out, limit)
