"""Live, colorized tail of the PhotonScript service log (`photonscript monitor`).

Local mode reads <data_dir>/logs/photonscript.log directly and survives log
rotation. Remote mode polls GET /api/logs/tail on a running scheduler (for
example over Tailscale from the desktop). Pure helpers here are unit-tested;
the CLI wraps them with rich for Windows-safe color.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) "
    r"\[(?P<name>[^\]]*)\] (?P<level>[A-Z]+)\s+(?P<msg>.*)$")

LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
LEVEL_STYLE = {"DEBUG": "dim", "INFO": "", "WARNING": "yellow",
               "ERROR": "bold red", "CRITICAL": "bold white on red"}

# Events worth spotting at a glance (first match wins; case-insensitive).
HIGHLIGHTS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bUNSAFE\b|unsafe"), "bold red"),
    (re.compile(r"\bSAFE\b"), "bold green"),
    (re.compile(r"\bARMED\b|\bDISARMED\b|arm(ed|ing)\b", re.I), "bold cyan"),
    (re.compile(r"cooler|setpoint|°C", re.I), "cyan"),
    (re.compile(r"\bpark(ed|ing)?\b|unpark", re.I), "magenta"),
    (re.compile(r"reject", re.I), "yellow"),
    (re.compile(r"accept|approved", re.I), "green"),
    (re.compile(r"pushover", re.I), "blue"),
    (re.compile(r"autofocus|\bAF\b", re.I), "bright_blue"),
]


@dataclass
class LogLine:
    raw: str
    ts: Optional[datetime] = None
    name: str = ""
    level: str = ""
    msg: str = ""


def parse_line(raw: str) -> LogLine:
    raw = raw.rstrip("\r\n")
    m = LINE_RE.match(raw)
    if not m:
        return LogLine(raw=raw)  # continuation line (traceback etc.)
    try:
        ts = datetime.strptime(m["ts"], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        ts = None
    return LogLine(raw=raw, ts=ts, name=m["name"].strip(),
                   level=m["level"], msg=m["msg"])


def parse_since(spec: str | None, now: datetime | None = None) -> Optional[datetime]:
    """'30m', '2h', '1d', '90s' -> the cutoff datetime; None/'' -> None."""
    if not spec:
        return None
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", spec.lower())
    if not m:
        raise ValueError(f"bad --since value {spec!r} (use e.g. 30m, 2h, 1d)")
    n, unit = int(m[1]), m[2]
    delta = {"s": timedelta(seconds=n), "m": timedelta(minutes=n),
             "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
    return (now or datetime.now()) - delta


class LineFilter:
    """Level / text / time filter. Continuation lines (tracebacks) follow the
    decision of the last parsed line so a kept ERROR keeps its traceback."""

    def __init__(self, min_level: str = "DEBUG", grep: str = "",
                 since: Optional[datetime] = None):
        self.min_level = LEVELS.get(min_level.upper(), 10)
        self.grep = grep.lower()
        self.since = since
        self._last = True

    def keep(self, ln: LogLine) -> bool:
        if not ln.level:  # continuation line
            return self._last
        ok = LEVELS.get(ln.level, 20) >= self.min_level
        if ok and self.since and ln.ts and ln.ts < self.since:
            ok = False
        if ok and self.grep and self.grep not in ln.raw.lower():
            ok = False
        self._last = ok
        return ok


def _escape(s: str) -> str:
    return s.replace("[", r"\[")


def to_markup(ln: LogLine) -> str:
    """rich markup for one line (brackets escaped)."""
    if not ln.level:
        return f"[dim]{_escape(ln.raw)}[/dim]"
    lvl_style = LEVEL_STYLE.get(ln.level, "")
    msg_style = lvl_style if ln.level in ("ERROR", "CRITICAL", "WARNING") else ""
    if not msg_style:
        for rx, style in HIGHLIGHTS:
            if rx.search(ln.msg):
                msg_style = style
                break
    ts = ln.raw[:19]
    lvl = f"{ln.level:<7}"
    lvl_m = f"[{lvl_style}]{lvl}[/{lvl_style}]" if lvl_style else lvl
    msg = _escape(ln.msg)
    msg_m = f"[{msg_style}]{msg}[/{msg_style}]" if msg_style else msg
    return f"[dim]{ts}[/dim] [dim]{_escape(ln.name)[:20]:<20}[/dim] {lvl_m} {msg_m}"


def last_lines(path: Path, n: int) -> list[str]:
    """Last n lines of a text file without reading it all."""
    if n <= 0 or not path.exists():
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        block, data = 8192, b""
        pos = size
        while pos > 0 and data.count(b"\n") <= n:
            step = min(block, pos)
            pos -= step
            f.seek(pos)
            data = f.read(step) + data
    return [ln for ln in data.decode("utf-8", "replace").splitlines()][-n:]


class FileFollower:
    """Follow a log file across rotation (RotatingFileHandler renames the file
    and starts a new one) and truncation. poll() returns new complete lines."""

    def __init__(self, path: Path, from_end: bool = True):
        self.path = Path(path)
        self._pos = self.path.stat().st_size if (from_end and self.path.exists()) else 0
        self._ident = self._stat_ident()
        self._partial = ""

    def _stat_ident(self):
        try:
            st = self.path.stat()
            return (st.st_ino, st.st_dev) if st.st_ino else None
        except OSError:
            return None

    def poll(self) -> list[str]:
        if not self.path.exists():
            return []
        ident = self._stat_ident()
        size = self.path.stat().st_size
        if (ident is not None and self._ident is not None and ident != self._ident) \
                or size < self._pos:
            self._pos, self._partial = 0, ""  # rotated or truncated
        self._ident = ident
        if size == self._pos:
            return []
        with open(self.path, "rb") as f:
            f.seek(self._pos)
            chunk = f.read(size - self._pos)
        self._pos = size
        text = self._partial + chunk.decode("utf-8", "replace")
        parts = text.split("\n")
        self._partial = parts.pop()  # incomplete last line (no newline yet)
        return [p.rstrip("\r") for p in parts]


def read_from_offset(path: Path, offset: int, max_bytes: int = 256_000,
                     tail_lines: int = 200) -> dict:
    """Server side of remote monitoring. offset<0 -> start with the last
    `tail_lines` lines. Returns {"offset": new_offset, "lines": [...],
    "rotated": bool}. Only whole lines are returned."""
    path = Path(path)
    if not path.exists():
        return {"offset": 0, "lines": [], "rotated": False, "missing": True}
    size = path.stat().st_size
    if offset < 0:
        return {"offset": size, "lines": last_lines(path, tail_lines),
                "rotated": False}
    rotated = offset > size
    if rotated:
        offset = 0
    end = min(size, offset + max_bytes)
    with open(path, "rb") as f:
        f.seek(offset)
        chunk = f.read(end - offset)
    cut = chunk.rfind(b"\n")
    if cut < 0:
        return {"offset": offset, "lines": [], "rotated": rotated}
    chunk = chunk[:cut + 1]
    return {"offset": offset + len(chunk),
            "lines": chunk.decode("utf-8", "replace").splitlines(),
            "rotated": rotated}


def service_log_path(config) -> Path:
    return Path(config.data_dir) / "logs" / "photonscript.log"


def render(lines: Iterable[str], flt: LineFilter, color: bool = True) -> list[str]:
    out = []
    for raw in lines:
        ln = parse_line(raw)
        if flt.keep(ln):
            out.append(to_markup(ln) if color else ln.raw)
    return out
