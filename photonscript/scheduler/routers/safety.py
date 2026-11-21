"""PS-1 safety-monitor analytics: was that unsafe AARO's weather, or a driver?

GET /api/safety/night?date=YYYY-MM-DD   one night: per-NINA safe / unsafe /
        offline timeline, clean changes, connection losses (NINA logs
        "Unsafe" then "Disconnected"), short flaps, the NINA #1 vs NINA #2
        cross-check (agreed / suspect / unverified), the armer's own safety
        history row, a confirm-hold (debounce) table and the local time of
        each unsafe onset. match_s / flap_s tune the pairing and flap length.
GET /api/safety/summary?days=14          one row per night, newest first.

Read only: NINA log files and safety_history.jsonl. Logic lives in
scheduler/safety_analytics.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


def _rig_logs(cfg, date: str) -> tuple[dict, str]:
    from photonscript.scheduler.routers.triage import _rig_night_logs
    out = {}
    for rig in ("rc16", "piggyback"):
        paths, note = _rig_night_logs(cfg, rig, date)
        if note.startswith("bad date"):
            return {}, note
        out[rig] = paths
    return out, ""


def _report(cfg, date: str, match_s: float, flap_s: float) -> dict:
    from photonscript.scheduler.safety_analytics import night_report
    logs, err = _rig_logs(cfg, date)
    if err:
        return {"ok": False, "note": err}
    return night_report(cfg, date, logs, match_s=float(match_s),
                        flap_s=float(flap_s))


@router.get("/api/safety/night")
def api_safety_night(date: str = "", match_s: float = 60.0,
                     flap_s: float = 300.0):
    from photonscript.shared.phd2_store import night_of
    cfg = _cfg()
    date = (date or "").strip() or night_of(cfg)
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return {"ok": False, "note": f"bad date {date!r} (use YYYY-MM-DD)"}
    try:
        return _report(cfg, date, match_s, flap_s)
    except Exception as e:  # noqa: BLE001 - analytics must never 500 the page
        return {"ok": False, "note": f"safety analytics failed: {e}"}


@router.get("/api/safety/summary")
def api_safety_summary(days: int = 14, match_s: float = 60.0,
                       flap_s: float = 300.0):
    from photonscript.scheduler.safety_analytics import summary_row
    from photonscript.shared.phd2_store import night_of
    cfg = _cfg()
    last = datetime.strptime(night_of(cfg), "%Y-%m-%d")
    rows = []
    for i in range(max(1, min(int(days), 60))):
        d = (last - timedelta(days=i)).strftime("%Y-%m-%d")
        try:
            rep = _report(cfg, d, match_s, flap_s)
        except Exception as e:  # noqa: BLE001
            rows.append({"date": d, "error": str(e)})
            continue
        if rep.get("ok") and rep.get("rigs"):
            rows.append(summary_row(rep))
    tot = {k: sum(r.get(k, 0) for r in rows)
           for k in ("agreed", "suspects", "losses", "losses_both", "flaps")}
    return {"nights": len(rows), "totals": tot, "rows": rows}
