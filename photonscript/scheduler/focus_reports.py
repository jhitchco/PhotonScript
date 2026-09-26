"""Post-night autofocus quality check.

NINA writes a JSON report for every autofocus run (Options → the AutoFocus
report folder, typically ``%LOCALAPPDATA%/NINA/AutoFocus``). PhotonScript's
sequence retries a failed AF (Attempts=2) but nothing ever LOOKS at the result —
so a soft/failed run (the 2026-09-26 "Stars detected: 1" through Ha → no HFR
curve → donuts) degrades every following sub and is only caught, after the fact,
by the passive QA gates. This reads those reports after a night and fires one
Pushover when an AF run's fit is bad (low R², too few points), naming the
offending filter so the fix (focus on a bright filter — see
config.autofocus_filter / NINA's global Autofocus Filter) is obvious.

Fails safe: no reports dir configured, or unreadable/foreign JSON, never raises
and never alarms — a missing signal is not a failure signal.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


def _num(v):
    try:
        f = float(v)
        return f if f == f else None  # drop NaN
    except (TypeError, ValueError):
        return None


def _best_r2(report: dict) -> float | None:
    """Pull the best fit R^2 from a NINA AF report across schema variants:
    a ``RSquares`` dict ({Quadratic, Hyperbolic, ...}), a scalar ``RSquare`` /
    ``RSquared``, or an ``Intersections``/``Fittings`` block that carries one."""
    cands: list[float] = []
    rs = report.get("RSquares")
    if isinstance(rs, dict):
        cands += [x for x in (_num(v) for v in rs.values()) if x is not None]
    elif (x := _num(rs)) is not None:
        cands.append(x)
    for key in ("RSquare", "RSquared", "RSquaredQuadratic", "RSquaredHyperbolic"):
        if (x := _num(report.get(key))) is not None:
            cands.append(x)
    return max(cands) if cands else None


def evaluate(report: dict, min_r2: float = 0.7) -> dict:
    """Grade one AF report. Returns
    {filter, timestamp, r2, hfr, points, ok, reason}. ``ok`` is False only on a
    positively-bad run (parsable R^2 below min_r2, or <3 measure points); an
    unparseable R^2 is 'unknown', not a failure (ok=True)."""
    filt = report.get("Filter") or report.get("AutoFocusFilter") or "?"
    ts = report.get("Timestamp") or report.get("Time") or ""
    r2 = _best_r2(report)
    cfp = report.get("CalculatedFocusPoint") or {}
    hfr = _num(cfp.get("Value")) if isinstance(cfp, dict) else None
    mp = report.get("MeasurePoints")
    points = len(mp) if isinstance(mp, list) else None

    ok, reason = True, "ok"
    if points is not None and points < 3:
        ok, reason = False, f"only {points} measure point(s)"
    elif r2 is not None and r2 < min_r2:
        ok, reason = False, f"R^2 {r2:.2f} < {min_r2:.2f}"
    return {"filter": str(filt), "timestamp": str(ts), "r2": r2, "hfr": hfr,
            "points": points, "ok": ok, "reason": reason}


def _report_date_ok(ts: str, date: str) -> bool:
    """Keep reports from the night's evening date or the following morning
    (a night spans two calendar dates). Lenient: unparseable timestamp -> keep."""
    if not ts:
        return True
    day = ts[:10]
    if day == date:
        return True
    try:
        nxt = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime(
            "%Y-%m-%d")
        return day == nxt
    except ValueError:
        return True


def load_reports(reports_dir: str, date: str | None = None) -> list[dict]:
    """Read + parse the AF report JSONs in reports_dir, newest first, optionally
    filtered to the given local night (evening date or next morning)."""
    out: list[dict] = []
    try:
        d = Path(reports_dir)
        if not d.is_dir():
            return out
        files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime,
                       reverse=True)
    except OSError as e:
        logger.warning("focus_reports: cannot list %s: %s", reports_dir, e)
        return out
    for p in files:
        try:
            rep = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rep, dict):
            continue
        rep.setdefault("_file", p.name)
        ts = rep.get("Timestamp") or rep.get("Time") or ""
        if date is not None and not _report_date_ok(str(ts), date):
            continue
        out.append(rep)
    return out


def check_and_alert(config, date: str) -> dict:
    """Grade the night's AF reports and Pushover once if any run was bad.
    Returns {"checked": n, "bad": [evaluations]}; a no-op ({} shaped) when no
    reports dir is configured. Never raises."""
    reports_dir = (getattr(config, "nina_autofocus_reports_dir", "") or "").strip()
    if not reports_dir:
        return {"checked": 0, "bad": [], "enabled": False}
    min_r2 = float(getattr(config, "af_min_r2", 0.7))
    try:
        reports = load_reports(reports_dir, date)
        graded = [evaluate(r, min_r2) for r in reports]
        bad = [g for g in graded if not g["ok"]]
        if bad:
            worst = ", ".join(f"{g['filter']} ({g['reason']})" for g in bad[:5])
            _fire(config,
                  f"Autofocus quality: {len(bad)}/{len(graded)} AF run(s) on "
                  f"{date} looked bad — {worst}. Likely focusing through "
                  "narrowband; set the AF filter to L (config.autofocus_filter "
                  "+ NINA's global Autofocus Filter) and check per-filter offsets.")
        logger.info("focus_reports: %s — %d AF report(s), %d bad",
                    date, len(graded), len(bad))
        return {"checked": len(graded), "bad": bad, "enabled": True}
    except Exception as e:  # noqa: BLE001
        logger.warning("focus_reports check failed for %s: %s", date, e)
        return {"checked": 0, "bad": [], "enabled": True, "error": str(e)}


def _fire(config, msg: str) -> None:
    """Send a Pushover from sync or async context (backfill runs in a thread)."""
    import asyncio
    from photonscript.shared.pushover import notify
    coro = notify(config, msg, title="PhotonScript autofocus", priority=1)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(coro)
        return
    loop.create_task(coro)
