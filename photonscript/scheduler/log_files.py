"""Pick a night's log files by date or name (PS-73).

NINA and PHD2 both start a new log file per program start and put the local
start time in the name:
  NINA: 20260927-070419-3.2.0.9001.26856-202609.log
  PHD2: PHD2_GuideLog_2026-09-26_193012.txt / PHD2_DebugLog_...
A night (date=D, the evening date) runs from D 12:00 to D+1 12:00 local, so a
program restart at 07:04 the next morning (both NINAs on 2026-09-27) still
belongs to the night before, and the night's own log, started the afternoon
before, stays reachable after the restart. Times are the scope PC's local
clock, the same clock the programs name their files with.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

_NAME_TS = re.compile(r"(\d{4})-?(\d{2})-?(\d{2})[_-](\d{2})(\d{2})(\d{2})")


def file_start(name: str) -> datetime | None:
    """Local start time from a NINA / PHD2 log file name, or None."""
    m = _NAME_TS.search(Path(name).name)
    if not m:
        return None
    try:
        return datetime(*(int(x) for x in m.groups()))
    except ValueError:
        return None


def night_window(date: str) -> tuple[datetime, datetime]:
    """[date 12:00, date+1 12:00) local. Raises ValueError on a bad date."""
    d = datetime.strptime(date.strip(), "%Y-%m-%d")
    start = d.replace(hour=12)
    return start, start + timedelta(days=1)


def mtime_local(p: Path) -> datetime:
    return datetime.fromtimestamp(Path(p).stat().st_mtime)


def in_night(p: Path, date: str) -> bool:
    """True when the log covers any of the night: it started inside the
    window, or started before it and was still being written in it."""
    w0, w1 = night_window(date)
    st = file_start(Path(p).name)
    try:
        mt = mtime_local(p)
    except OSError:
        return False
    if st is None:
        return w0 <= mt < w1 + timedelta(hours=12)
    if w0 <= st < w1:
        return True
    return st < w0 and mt >= w0


def safe_name(file: str) -> str | None:
    """A bare file name, or None if it tries to leave the logs folder."""
    f = (file or "").strip()
    if not f or f != Path(f).name or "/" in f or "\\" in f or f in (".", ".."):
        return None
    return f


def read_rows(paths: list[Path], grep: str = "", lines: int = 500,
              header: bool = True) -> list[str]:
    """Concatenate log files (oldest first) with a header line per file, keep
    only lines matching grep ('|' separated, case-insensitive; headers always
    stay), and return the last `lines` rows."""
    needles = [n.strip().lower() for n in (grep or "").split("|") if n.strip()]
    rows: list[str] = []
    for p in paths:
        if header and len(paths) > 1:
            rows.append(f"# ===== {Path(p).name} =====")
        for r in Path(p).read_text(encoding="utf-8", errors="replace").splitlines():
            if needles and not any(n in r.lower() for n in needles):
                continue
            rows.append(r)
    return rows[-min(max(1, int(lines)), 5000):]


def describe(p: Path) -> dict:
    st = file_start(Path(p).name)
    try:
        s = Path(p).stat()
        size, mt = s.st_size, datetime.fromtimestamp(s.st_mtime)
    except OSError:
        size, mt = None, None
    return {"file": Path(p).name,
            "started": st.isoformat(sep=" ") if st else None,
            "modified": mt.isoformat(sep=" ", timespec="seconds") if mt else None,
            "bytes": size}
