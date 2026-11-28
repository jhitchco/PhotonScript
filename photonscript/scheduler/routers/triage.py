"""Triage / log-tail endpoints — remote 2 AM debugging without the full bundle.

/api/nina/log (rig=rc16|piggyback, date=, file=), /api/nina/logs,
/api/phd2/log (date=, file=), /api/phd2/logs, /api/phd2/summary,
/api/phd2/analysis (PS-88),
/api/ascom/log, and /api/notifications (Pushover audit). Extracted from app.py; handlers lazily
import get_config to avoid an import cycle.
"""
from __future__ import annotations

import asyncio
import glob as _glob
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, PlainTextResponse

router = APIRouter()


def _cfg():
    from photonscript.scheduler.app import get_config
    return get_config()


_LISTEN_RE_TMPL = r"listening at \S*:{port}\b"


def _rig_log(cfg, rig: str):
    """(path, note) of the newest NINA log belonging to `rig`.

    Both NINA instances run as the same Windows user, so they write into ONE
    Logs folder and "newest file" is whichever NINA started last. Each log
    records the Advanced API port it serves ("starting web server, listening
    at 0.0.0.0:1889"), so pick the newest log whose head names this rig's
    port. Falls back to the newest log for the RC16 (old behavior)."""
    import re
    from urllib.parse import urlparse
    pig = str(rig).lower() in ("piggyback", "osc", "nina2", "2")
    own_dir = (getattr(cfg, "piggyback_nina_logs_dir", "") or "") if pig else ""
    logs_dir = own_dir or cfg.nina_logs_dir
    # A dedicated NINA #2 folder holds only its logs: newest is right.
    dedicated = bool(own_dir) and Path(own_dir) != Path(cfg.nina_logs_dir)
    base = (getattr(cfg, "piggyback_nina_base_url", "") if pig
            else cfg.nina_base_url) or ""
    port = urlparse(base).port
    logs = sorted(_glob.glob(str(Path(logs_dir) / "*.log")),
                  key=lambda p: Path(p).stat().st_mtime, reverse=True)
    if not logs:
        return None, f"no NINA logs found for {rig} under {logs_dir}"
    if dedicated:
        return logs[0], ""
    if port:
        pat = re.compile(_LISTEN_RE_TMPL.format(port=port))
        for p in logs[:15]:
            try:
                with open(p, encoding="utf-8", errors="replace") as fh:
                    head = fh.read(2_000_000)
            except OSError:
                continue
            if pat.search(head):
                return p, ""
    if pig:
        return None, (f"no NINA log under {logs_dir} says it serves the "
                      f"Advanced API on :{port} (NINA #2) - is it running?")
    return logs[0], ""


_PORT_RE = None
_PORT_CACHE: dict[str, int | None] = {}


def _log_port(p) -> int | None:
    """The Advanced API port a NINA log says its instance serves (from the
    startup line), or None. Cached once the file is older than 10 min (its
    head no longer changes)."""
    import re
    import time
    global _PORT_RE
    if _PORT_RE is None:
        _PORT_RE = re.compile(r"listening at \S*:(\d+)\b")
    key = str(p)
    if key in _PORT_CACHE:
        return _PORT_CACHE[key]
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            head = fh.read(2_000_000)
        age = time.time() - Path(p).stat().st_mtime
    except OSError:
        return None
    m = _PORT_RE.search(head)
    port = int(m.group(1)) if m else None
    if port is not None or age > 600:
        _PORT_CACHE[key] = port
    return port


def _nina_setup(cfg, rig: str):
    """(logs_dir, dedicated, port, is_piggyback) for a rig."""
    from urllib.parse import urlparse
    pig = str(rig).lower() in ("piggyback", "osc", "nina2", "2")
    own_dir = (getattr(cfg, "piggyback_nina_logs_dir", "") or "") if pig else ""
    logs_dir = own_dir or cfg.nina_logs_dir
    dedicated = bool(own_dir) and Path(own_dir) != Path(cfg.nina_logs_dir)
    base = (getattr(cfg, "piggyback_nina_base_url", "") if pig
            else cfg.nina_base_url) or ""
    return logs_dir, dedicated, urlparse(base).port, pig


def _all_nina_logs(logs_dir: str, newest: int = 60) -> list[Path]:
    logs = sorted((Path(p) for p in _glob.glob(str(Path(logs_dir) / "*.log"))),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    return logs[:newest]


def _rig_night_logs(cfg, rig: str, date: str) -> tuple[list[Path], str]:
    """PS-73: every log of this rig that covers the night of `date` (oldest
    first), so a NINA restart does not hide the night's log."""
    from photonscript.scheduler.log_files import in_night
    logs_dir, dedicated, port, pig = _nina_setup(cfg, rig)
    try:
        cands = [p for p in _all_nina_logs(logs_dir, 200) if in_night(p, date)]
    except ValueError:
        return [], f"bad date {date!r} (use YYYY-MM-DD)"
    if not dedicated and port:
        mine = [p for p in cands if _log_port(p) == port]
        if not mine and not pig:
            # RC16 fallback (old behavior): logs that name no port at all
            mine = [p for p in cands if _log_port(p) is None]
        cands = mine
    cands.sort(key=lambda p: p.stat().st_mtime)
    if not cands:
        return [], f"no NINA log for {rig} covers the night of {date} under {logs_dir}"
    return cands, ""


def _rig_of(cfg, p: Path) -> str | None:
    port = _log_port(p)
    for rig in ("rc16", "piggyback"):
        _d, _ded, rp, _pig = _nina_setup(cfg, rig)
        if port is not None and port == rp:
            return rig
    return None


# PS-172: the tail endpoints run their file work in a worker thread (a
# synchronous read of a big log inside an async handler stalled the event
# loop 6-7 s on 2026-10-08) and read each file backwards from its end, so
# an 836 MB NINA log costs only the tail bytes. offset= pages further back
# than one 5000-line page; download=1 streams one whole file up to a cap.
DOWNLOAD_MAX_BYTES = 300 * 1024 * 1024


def _scan_bytes(scan_mb) -> int:
    from photonscript.scheduler.log_files import MAX_SCAN_BYTES
    try:
        mb = float(scan_mb)
    except (TypeError, ValueError):
        mb = 64.0
    return int(min(max(1.0, mb), MAX_SCAN_BYTES / 1048576) * 1048576)


def _tail_text(paths, grep, lines, offset, scan_mb, label: str = "",
               extra: str = "") -> str:
    from photonscript.scheduler.log_files import page_note, tail_page
    rows, info = tail_page(paths, grep, lines, offset,
                           scan_bytes=_scan_bytes(scan_mb))
    names = ", ".join(Path(p).name for p in paths)
    n = sum(1 for r in rows if not r.startswith("# ===== "))
    return (f"# {label}{names} - last {n} lines{extra}"
            f"{page_note(info, offset)}\n" + "\n".join(rows))


def _download(paths):
    """A streamed whole-file response for one log, or a refusal."""
    if len(paths) != 1:
        return ("download=1 takes one file: pass file=<name> (see the logs "
                "listing) or neither file= nor date= for the newest")
    p = Path(paths[0])
    try:
        size = p.stat().st_size
    except OSError as exc:
        return f"cannot read {p.name}: {exc}"
    if size > DOWNLOAD_MAX_BYTES:
        return PlainTextResponse(
            f"{p.name} is {size / 1e6:.0f} MB, over the "
            f"{DOWNLOAD_MAX_BYTES / 1e6:.0f} MB download cap; page it with "
            "lines= and offset= (and scan_mb=) instead", status_code=413)
    return FileResponse(str(p), media_type="text/plain; charset=utf-8",
                        filename=p.name)


@router.get("/api/nina/log", response_class=PlainTextResponse)
async def api_nina_log(lines: int = 500, grep: str = "", rig: str = "rc16",
                       date: str = "", file: str = "", offset: int = 0,
                       scan_mb: float = 64, download: int = 0):
    """Tail (and optionally filter) the newest NINA log - remote 2AM triage
    without pulling the whole bundle. rig='piggyback' (aliases: osc, nina2, 2)
    tails NINA #2's log instead of the RC16's, so the OSC is triageable too.

    PS-73: date=YYYY-MM-DD reads every log of this rig covering that night
    (12:00 to 12:00 local, oldest first, one header per file), so the night
    stays readable after a NINA restart; file=<name> reads one log by name
    (see /api/nina/logs for the list).

    PS-172: runs off the event loop and reads from the end of the file.
    lines= caps at 5000 per page; offset=N skips the newest N matching lines
    (the header names the next offset); scan_mb= (default 64) bounds how far
    back a page looks; download=1 streams one whole log (size-capped)."""
    return await asyncio.to_thread(_nina_log, lines, grep, rig, date, file,
                                   offset, scan_mb, download)


def _nina_log(lines, grep, rig, date, file, offset, scan_mb, download):
    from photonscript.scheduler.log_files import safe_name
    cfg = _cfg()
    if file:
        logs_dir, _ded, _port, _pig = _nina_setup(cfg, rig)
        name = safe_name(file)
        p = Path(logs_dir) / name if name else None
        if p is None or not p.is_file():
            return f"no NINA log named {file!r} under {logs_dir}"
        paths = [p]
    elif date:
        paths, note = _rig_night_logs(cfg, rig, date)
        if not paths:
            return note
    else:
        path, note = _rig_log(cfg, rig)
        if path is None:
            return note
        paths = [Path(path)]
    if int(download or 0):
        return _download(paths)
    return _tail_text(paths, grep, lines, offset, scan_mb, label=f"[{rig}] ")


@router.get("/api/nina/logs")
def api_nina_logs(rig: str = "", date: str = "", limit: int = 60):
    """PS-73: the NINA log files on disk, newest first, with the rig each one
    belongs to (by the Advanced API port its startup line names) and its
    start time, for picking a file= or date= in /api/nina/log."""
    from photonscript.scheduler.log_files import describe, in_night
    cfg = _cfg()
    dirs = [cfg.nina_logs_dir]
    pdir = getattr(cfg, "piggyback_nina_logs_dir", "") or ""
    if pdir and Path(pdir) != Path(cfg.nina_logs_dir):
        dirs.append(pdir)
    out = []
    for d in dirs:
        for p in _all_nina_logs(d, max(1, min(int(limit), 200))):
            if date:
                try:
                    if not in_night(p, date):
                        continue
                except ValueError:
                    return {"error": f"bad date {date!r} (use YYYY-MM-DD)"}
            who = ("piggyback" if d == pdir and len(dirs) > 1 else _rig_of(cfg, p))
            if rig and who != rig:
                continue
            out.append(dict(describe(p), rig=who, dir=d))
    out.sort(key=lambda r: r["modified"] or "", reverse=True)
    return {"count": len(out), "logs": out[:max(1, min(int(limit), 200))]}


@router.get("/api/nina/log/top")
def api_nina_log_top(rig: str = "rc16", file: str = "", mb: float = 50,
                     top: int = 25):
    """PS-174: the most repeated message shapes in the last `mb` MB (default
    50, max 1024) of a rig's newest NINA log (or file=<name>), with count and
    share of bytes, to find what fills the log. Reads only the tail, in a
    worker thread (plain def). Also: the newest log's size and growth per
    rig (the morning report's log watch)."""
    from photonscript.scheduler.log_files import safe_name
    from photonscript.scheduler.nina_log_watch import log_sizes, top_messages
    cfg = _cfg()
    if file:
        logs_dir, _ded, _port, _pig = _nina_setup(cfg, rig)
        name = safe_name(file)
        p = Path(logs_dir) / name if name else None
        if p is None or not p.is_file():
            return {"error": f"no NINA log named {file!r} under {logs_dir}"}
    else:
        path, note = _rig_log(cfg, rig)
        if path is None:
            return {"error": note}
        p = Path(path)
    out = top_messages(p, mb=mb, top=top)
    out.update(rig=rig, sizes=log_sizes(cfg))
    return out


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
async def api_phd2_log(lines: int = 500, grep: str = "", kind: str = "guide",
                       date: str = "", file: str = "", offset: int = 0,
                       scan_mb: float = 64, download: int = 0):
    """Tail (and optionally filter) a PHD2 log: remote guiding triage.

    kind='guide' (default) reads PHD2_GuideLog_*.txt (per-frame RA/Dec
    error, star-lost, calibration); kind='debug' reads PHD2_DebugLog_*.txt.
    grep filters lines (case-insensitive, '|' for multiple needles), e.g.
    grep='star lost|GuideStep|calibration'. Mirrors /api/nina/log.

    PS-73: newest log by default; date=YYYY-MM-DD reads every log covering
    that night (12:00 to 12:00 local); file=<name> reads one log. The folder
    is phd2_logs_dir, or the first usual PHD2 folder that holds logs (see
    /api/phd2/logs for where it looked).

    PS-172: off the event loop, read from the end; offset=, scan_mb= and
    download=1 as on /api/nina/log.
    """
    return await asyncio.to_thread(_phd2_log, lines, grep, kind, date, file,
                                   offset, scan_mb, download)


def _phd2_log(lines, grep, kind, date, file, offset, scan_mb, download):
    from photonscript.scheduler.phd2_logs import find_logs, select
    found = find_logs(_cfg(), kind)
    if not found["files"]:
        where = "; ".join(s["dir"] for s in found["searched"])
        return (f"no PHD2 {found['pattern']} logs found (searched: {where}); "
                "check PHD2 logging (Tools > Enable Guide Log) and set "
                "phd2_logs_dir to PHD2's log folder")
    paths, note = select(found["files"], date=date, file=file)
    if not paths:
        return note
    if int(download or 0):
        return _download(paths)
    extra = "" if found["why"] == "configured" else f" [found in {found['dir']}]"
    return _tail_text(paths, grep, lines, offset, scan_mb, extra=extra)


@router.get("/api/phd2/logs")
def api_phd2_logs(kind: str = "guide", date: str = "", limit: int = 60):
    """PS-73: where PHD2 logs were looked for and found, and the files there
    (newest first), for picking a file= or date= in /api/phd2/log."""
    from photonscript.scheduler.log_files import describe, in_night
    from photonscript.scheduler.phd2_logs import find_logs
    found = find_logs(_cfg(), kind)
    files = list(reversed(found["files"]))
    if date:
        try:
            files = [p for p in files if in_night(p, date)]
        except ValueError:
            return {"error": f"bad date {date!r} (use YYYY-MM-DD)"}
    return {"dir": found["dir"], "why": found["why"],
            "searched": found["searched"], "pattern": found["pattern"],
            "count": len(files),
            "logs": [describe(p) for p in files[:max(1, min(int(limit), 500))]]}


@router.get("/api/phd2/summary")
def api_phd2_summary(date: str = "", file: str = ""):
    """PS-73: a night's PHD2 guide log in one read: RMS RA/Dec (arcsec, with
    the pixel figures), star-lost count and reasons, dithers, and every
    calibration with its Dec, hour angle, pier side and result. date=
    YYYY-MM-DD (the evening date) or file=<name>; default = newest log."""
    from photonscript.scheduler.phd2_logs import night_summary
    return night_summary(_cfg(), date=date, file=file)


@router.get("/api/phd2/analysis")
def api_phd2_analysis(date: str = "", file: str = "", subs: bool = True):
    """PS-88: why guiding went the way it did, for one night (date=YYYY-MM-DD,
    the evening date) or one log (file=). Per guiding session: RMS in arcsec
    (all / settled, about the lock and PHD2-style std), SNR, saturation,
    drops by PHD2's own reason, pulse balance and max-duration share, the
    correction commanded vs what the star did, dithers and settling, and the
    calibration in use with its quality and pier side. `findings` ranks
    rule-based problems with a fix each; `per_sub` gives every graded sub's
    guiding during its own exposure (subs=false skips it). Read-only."""
    from photonscript.scheduler.phd2_analysis import night_analysis
    return night_analysis(_cfg(), date=date, file=file, with_subs=subs)


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
async def api_ascom_log(lines: int = 500, grep: str = "", name: str = "Safety",
                        offset: int = 0, scan_mb: float = 64, download: int = 0):
    """Tail (and optionally filter) the newest ASCOM trace log: the driver-level
    detail (HTTP calls, exceptions) behind a safety-monitor drop. Requires
    'Enable Trace' in the ASCOM Alpaca driver setup. name= filters by filename
    substring (default 'Safety', the safety-monitor client's log; blank = any
    ASCOM device); grep= filters lines (case-insensitive, '|' for multiple).
    PS-172: off the event loop (the folder walk too), read from the end;
    offset=, scan_mb= and download=1 as on /api/nina/log."""
    return await asyncio.to_thread(_ascom_log, lines, grep, name, offset,
                                   scan_mb, download)


def _ascom_log(lines, grep, name, offset, scan_mb, download):
    base = getattr(_cfg(), "ascom_logs_dir", "")
    if not base:
        return "ascom_logs_dir not configured (set it in System config)"
    p = _latest_ascom_log(base, name)
    if p is None:
        label = f'"{name}" ' if name else ""
        return (f"no ASCOM {label}logs found under {base}: enable 'Trace' in the "
                "ASCOM Alpaca driver setup, then reconnect and wait for activity")
    if int(download or 0):
        return _download([p])
    return _tail_text([p], grep, lines, offset, scan_mb)


@router.get("/api/logs/tail")
async def api_logs_tail(offset: int = -1, lines: int = 200):
    """PhotonScript's own service log, incrementally, for `photonscript monitor
    --url ...` (PS-34b). offset=-1 returns the last `lines` lines plus the
    offset to poll from next; later calls return only new whole lines.
    Read-only."""
    from photonscript.shared.logmonitor import read_from_offset, service_log_path
    return read_from_offset(service_log_path(_cfg()), int(offset),
                            tail_lines=max(1, min(int(lines), 2000)))
