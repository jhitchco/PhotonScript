"""Triage / log-tail endpoints — remote 2 AM debugging without the full bundle.

/api/nina/log (rig=rc16|piggyback), /api/phd2/log, /api/ascom/log, and
/api/notifications (Pushover audit). Extracted from app.py; handlers lazily
import get_config to avoid an import cycle.
"""
from __future__ import annotations

import glob as _glob
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


@router.get("/api/nina/log", response_class=PlainTextResponse)
async def api_nina_log(lines: int = 500, grep: str = "", rig: str = "rc16"):
    """Tail (and optionally filter) the newest NINA log - remote 2AM triage
    without pulling the whole bundle. rig='piggyback' (aliases: osc, nina2, 2)
    tails NINA #2's log instead of the RC16's, so the OSC is triageable too."""
    cfg = _cfg()
    if str(rig).lower() in ("piggyback", "osc", "nina2", "2"):
        logs_dir = getattr(cfg, "piggyback_nina_logs_dir", "") or ""
        if not logs_dir:
            return ("piggyback_nina_logs_dir not configured (NINA #2 log dir) — "
                    "set it in System config to tail the OSC's log")
    else:
        logs_dir = cfg.nina_logs_dir
    logs = sorted(_glob.glob(str(Path(logs_dir) / "*.log")))
    if not logs:
        return f"no NINA logs found for {rig} under {logs_dir}"
    rows = Path(logs[-1]).read_text(encoding="utf-8",
                                    errors="replace").splitlines()
    if grep:
        needles = [n.strip().lower() for n in grep.split("|") if n.strip()]
        rows = [r for r in rows if any(n in r.lower() for n in needles)]
    rows = rows[-min(max(1, lines), 5000):]
    return (f"# [{rig}] {Path(logs[-1]).name} - last {len(rows)} lines\n"
            + "\n".join(rows))


@router.get("/api/notifications")
def api_notifications(since_hours: float = 24.0, limit: int = 200,
                      title: str = ""):
    """Audit the Pushover stream: recent notification records (sent AND
    suppressed) plus a per-title tally over the window, so alert volume is
    reviewable later — 'how many of each did I get, and how many were throttled'.
    """
    from photonscript.shared.pushover import _audit_path
    p = _audit_path(_cfg())
    if not p.exists():
        return {"records": [], "summary": {}, "count": 0,
                "note": "no notifications.jsonl yet"}
    cutoff = datetime.now(timezone.utc) - timedelta(hours=float(since_hours))
    recs = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            r = json.loads(line)
        except Exception:  # noqa: BLE001
            continue
        try:
            if datetime.fromisoformat(r.get("ts", "")) < cutoff:
                continue
        except (ValueError, TypeError):
            pass  # keep undateable rows rather than drop silently
        if title and title.lower() not in str(r.get("title", "")).lower():
            continue
        recs.append(r)
    summary: dict = {}
    for r in recs:
        s = summary.setdefault(r.get("title", "?"),
                               {"total": 0, "sent": 0, "suppressed": 0})
        s["total"] += 1
        s["sent" if r.get("sent") else "suppressed"] += 1
    summary = dict(sorted(summary.items(), key=lambda kv: -kv[1]["total"]))
    return {"window_hours": float(since_hours), "count": len(recs),
            "sent_total": sum(s["sent"] for s in summary.values()),
            "suppressed_total": sum(s["suppressed"] for s in summary.values()),
            "summary": summary, "records": recs[-int(limit):]}


@router.get("/api/phd2/log", response_class=PlainTextResponse)
async def api_phd2_log(lines: int = 500, grep: str = "", kind: str = "guide"):
    """Tail (and optionally filter) the newest PHD2 log — remote guiding triage.

    kind='guide' (default) tails the newest PHD2_GuideLog_*.txt (per-frame RA/Dec
    error, star-lost, calibration); kind='debug' tails PHD2_DebugLog_*.txt.
    grep filters lines (case-insensitive, '|' for multiple needles), e.g.
    grep='star lost|GuideStep|calibration'. Mirrors /api/nina/log.
    """
    base = getattr(_cfg(), "phd2_logs_dir", "")
    if not base:
        return "phd2_logs_dir not configured (set it in System config)"
    pattern = ("PHD2_DebugLog*" if str(kind).lower().startswith("debug")
               else "PHD2_GuideLog*")
    logs = sorted(_glob.glob(str(Path(base) / "**" / pattern), recursive=True),
                  key=lambda p: Path(p).stat().st_mtime)
    if not logs:
        return (f"no PHD2 {pattern} logs found under {base} — check "
                "phd2_logs_dir, or PHD2 hasn't guided yet")
    rows = Path(logs[-1]).read_text(encoding="utf-8",
                                    errors="replace").splitlines()
    if grep:
        needles = [n.strip().lower() for n in grep.split("|") if n.strip()]
        rows = [r for r in rows if any(n in r.lower() for n in needles)]
    rows = rows[-min(max(1, lines), 5000):]
    return f"# {Path(logs[-1]).name} - last {len(rows)} lines\n" + "\n".join(rows)


def _latest_ascom_log(base: str, name: str = ""):
    """Newest ASCOM trace-log file under `base` (searched recursively, since the
    TraceLogger writes into dated subfolders like 'Logs YYYY-MM-DD'). Optional
    `name` filters by filename substring (e.g. 'Safety'). Returns a Path or None.
    """
    root = Path(base) if base else None
    if not root or not root.exists():
        return None
    cands = [p for p in root.rglob("*.txt") if p.is_file()]
    cands += [p for p in root.rglob("*.log") if p.is_file()]
    if name:
        cands = [p for p in cands if name.lower() in p.name.lower()]
    if not cands:
        return None
    return max(cands, key=lambda p: p.stat().st_mtime)


@router.get("/api/ascom/log", response_class=PlainTextResponse)
async def api_ascom_log(lines: int = 500, grep: str = "", name: str = "Safety"):
    """Tail (and optionally filter) the newest ASCOM trace log — the driver-level
    detail (HTTP calls, exceptions) behind a safety-monitor drop. Requires
    'Enable Trace' in the ASCOM Alpaca driver setup. name= filters by filename
    substring (default 'Safety' → the safety-monitor client's log; blank = any
    ASCOM device); grep= filters lines (case-insensitive, '|' for multiple)."""
    base = getattr(_cfg(), "ascom_logs_dir", "")
    if not base:
        return "ascom_logs_dir not configured (set it in System config)"
    p = _latest_ascom_log(base, name)
    if p is None:
        label = f'"{name}" ' if name else ""
        return (f"no ASCOM {label}logs found under {base} — enable 'Trace' in the "
                "ASCOM Alpaca driver setup, then reconnect and wait for activity")
    rows = p.read_text(encoding="utf-8", errors="replace").splitlines()
    if grep:
        needles = [n.strip().lower() for n in grep.split("|") if n.strip()]
        rows = [r for r in rows if any(n in r.lower() for n in needles)]
    rows = rows[-min(max(1, lines), 5000):]
    return f"# {p.name} - last {len(rows)} lines\n" + "\n".join(rows)
