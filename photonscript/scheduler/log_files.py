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


MAX_PAGE_LINES = 5000                   # one page of a tail endpoint
DEFAULT_SCAN_BYTES = 64 * 1024 * 1024   # how far back a tail looks by default
MAX_SCAN_BYTES = 2048 * 1024 * 1024
_BLOCK = 1024 * 1024


def iter_reverse(path: Path, max_bytes: int | None = None, state: dict | None = None):
    """Yield a file's lines newest first, reading 1 MB blocks backwards from
    the end (PS-172), so a tail of an 836 MB NINA log touches only its last
    blocks. Stops after `max_bytes` from the end; `state` (if given) gets
    scanned_bytes and reached_start. Lines decode as UTF-8 with replacement
    and lose their line ending, like str.splitlines()."""
    st = state if state is not None else {}
    st["scanned_bytes"], st["reached_start"] = 0, False
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        end = pos = fh.tell()
        buf = b""
        first = True
        while pos > 0:
            if max_bytes is not None and end - pos >= max_bytes:
                return
            step = min(_BLOCK, pos)
            if max_bytes is not None:
                step = min(step, max_bytes - (end - pos))
            pos -= step
            fh.seek(pos)
            buf = fh.read(step) + buf
            st["scanned_bytes"] = end - pos
            parts = buf.split(b"\n")
            buf = parts[0]
            for raw in reversed(parts[1:]):
                if first:
                    first = False
                    if raw == b"":
                        continue        # the file's trailing newline
                yield raw.rstrip(b"\r").decode("utf-8", errors="replace")
        st["reached_start"] = True
        if end > 0:                     # the file's first line
            yield buf.rstrip(b"\r").decode("utf-8", errors="replace")


def tail_page(paths: list[Path], grep: str = "", lines: int = 500,
              offset: int = 0, header: bool = True,
              scan_bytes: int | None = DEFAULT_SCAN_BYTES) -> tuple[list[str], dict]:
    """One page of the concatenated logs (oldest file first), counted from
    the end: skip the newest `offset` matching lines, then return up to
    `lines` (capped at MAX_PAGE_LINES) in file order. grep is '|' separated
    and case-insensitive. With several files a '# ===== name =====' row
    precedes each file that was read to its start. Each file is scanned at
    most `scan_bytes` back from its end (None = no limit). Returns (rows,
    info) where info has scanned_bytes, truncated (a scan limit stopped it
    before the start of the oldest file it reached), more (older matching
    lines may exist) and next_offset."""
    needles = [n.strip().lower() for n in (grep or "").split("|") if n.strip()]
    want = min(max(1, int(lines)), MAX_PAGE_LINES)
    skip = max(0, int(offset))
    out: list[str] = []          # newest first
    seen = 0
    scanned = 0
    truncated = False
    full = False
    multi = header and len(paths) > 1
    for p in reversed(list(paths)):
        st: dict = {}
        for r in iter_reverse(Path(p), scan_bytes, st):
            if needles and not any(n in r.lower() for n in needles):
                continue
            seen += 1
            if seen <= skip:
                continue
            out.append(r)
            if len(out) >= want:
                full = True
                break
        scanned += st.get("scanned_bytes", 0)
        if full:
            break
        if not st.get("reached_start"):
            truncated = True
            break
        if multi:
            out.append(f"# ===== {Path(p).name} =====")
    out.reverse()
    return out, {"scanned_bytes": scanned, "truncated": truncated,
                 "more": full or truncated, "next_offset": skip + sum(
                     1 for r in out if not r.startswith("# ===== "))}


def read_rows(paths: list[Path], grep: str = "", lines: int = 500,
              header: bool = True) -> list[str]:
    """Concatenate log files (oldest first) with a header line per file, keep
    only lines matching grep ('|' separated, case-insensitive; headers always
    stay), and return the last `lines` rows. PS-172: reads backwards from the
    end of each file (see tail_page) instead of loading whole files."""
    return tail_page(paths, grep, lines, 0, header, scan_bytes=None)[0]


def page_note(info: dict, offset: int) -> str:
    """The header suffix a tail endpoint adds after a page."""
    bits = []
    if int(offset) > 0:
        bits.append(f"offset {int(offset)}")
    mb = info["scanned_bytes"] / 1e6
    if info["truncated"]:
        bits.append(f"scanned the last {mb:.0f} MB only (scan_mb= to look further, "
                    "or download=1)")
    if info["more"]:
        bits.append(f"older: offset={info['next_offset']}")
    return (" (" + "; ".join(bits) + ")") if bits else ""


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
