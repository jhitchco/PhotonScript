"""PS-174: NINA log size watch and top repeating messages.

The NINA #1 log 20261005-081746-...log reached 836 MB in 2.5 days (about
330 MB/day). Nothing here downloads a log: sizes come from the file system
(the same listing as /api/nina/logs) and the message census reads only the
last N MB through log_files.iter_reverse.

    log_sizes(config, now=None) -> [{rig, file, mb, mb_per_day, warn, ...}]
    morning_line(config) -> str | None     (only when a log is over the line)
    top_messages(path, mb=50, top=25) -> {lines, scanned_mb, top: [...]}

Warn rule (nina_log_warn_mb, default 500, 0 = off): the rig's newest log is
bigger than nina_log_warn_mb, or at its growth rate it would pass it within
two days of starting (mb_per_day * 2 > nina_log_warn_mb).
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from pathlib import Path

WARN_PACE_DAYS = 2.0
_MSG_NUM = re.compile(r"\d+(?:\.\d+)?")
_HEX = re.compile(r"\b[0-9a-fA-F]{8,}\b")


def _newest_per_rig(config) -> list[tuple[str, Path]]:
    from photonscript.scheduler.routers import triage
    from photonscript.shared.rigs import rig_ids
    out, seen = [], set()
    dirs = [config.nina_logs_dir]
    pdir = getattr(config, "piggyback_nina_logs_dir", "") or ""
    if pdir and Path(pdir) != Path(config.nina_logs_dir):
        dirs.append(pdir)
    want = set(rig_ids(config))
    for d in dirs:
        for p in triage._all_nina_logs(d, 20):
            rig = ("piggyback" if d == pdir and len(dirs) > 1
                   else triage._rig_of(config, p)) or "rc16"
            if rig in seen or rig not in want:
                continue
            seen.add(rig)
            out.append((rig, p))
    return out


def log_sizes(config, now: datetime | None = None) -> list[dict]:
    """The newest NINA log of each rig with its size and growth. Never
    raises (a rig whose folder is unreadable is left out)."""
    from photonscript.scheduler.log_files import file_start
    now = now or datetime.now()
    limit = float(getattr(config, "nina_log_warn_mb", 500.0) or 0.0)
    rows = []
    try:
        pairs = _newest_per_rig(config)
    except Exception:  # noqa: BLE001
        return rows
    for rig, p in pairs:
        try:
            st = p.stat()
        except OSError:
            continue
        mb = st.st_size / 1e6
        start = file_start(p.name)
        mt = datetime.fromtimestamp(st.st_mtime)
        age_h = ((mt - start).total_seconds() / 3600.0) if start else None
        rate = (mb / (age_h / 24.0)) if age_h and age_h >= 1.0 else None
        warn, why = False, ""
        if limit > 0 and mb > limit:
            warn, why = True, f"over {limit:g} MB"
        elif limit > 0 and rate is not None and rate * WARN_PACE_DAYS > limit:
            warn, why = True, (f"on pace for {limit:g} MB within "
                               f"{WARN_PACE_DAYS:g} days")
        rows.append({"rig": rig, "file": p.name, "mb": round(mb, 1),
                     "started": start.isoformat(sep=" ") if start else None,
                     "age_h": round(age_h, 1) if age_h is not None else None,
                     "mb_per_day": round(rate, 1) if rate is not None else None,
                     "warn": warn, "why": why})
    return rows


def morning_line(config, rows: list[dict] | None = None) -> str | None:
    """One line for the morning report when any NINA log is over the line."""
    rows = log_sizes(config) if rows is None else rows
    bad = [r for r in rows if r["warn"]]
    if not bad:
        return None
    from photonscript.shared.rigs import rig_label
    bits = []
    for r in bad:
        rate = f", {r['mb_per_day']:g} MB/day" if r["mb_per_day"] is not None else ""
        bits.append(f"{rig_label(config, r['rig'])} {r['file']} {r['mb']:g} MB"
                    f"{rate} ({r['why']})")
    return ("NINA log size: " + "; ".join(bits)
            + ". See /api/nina/log/top for what fills it")


def _key(line: str) -> str | None:
    """A NINA log line reduced to its source and message shape: the
    timestamp goes, numbers and long hex ids become '#'. NINA lines read
    'time|LEVEL|File.cs|Method|line|message'; continuation lines (stack
    frames, wrapped text) have no '|' and are counted as one group."""
    parts = line.split("|", 5)
    if len(parts) < 6:
        return "(continuation lines)" if line.strip() else None
    _t, level, src, method, _ln, msg = parts
    msg = _MSG_NUM.sub("#", _HEX.sub("#", msg.strip()))[:140]
    return f"{level}|{src}|{method}|{msg}"


def top_messages(path: Path, mb: float = 50.0, top: int = 25) -> dict:
    """Count the most repeated message shapes in the last `mb` MB of a log
    (seek-based, never the whole file)."""
    from photonscript.scheduler.log_files import iter_reverse
    cap = int(max(1.0, min(float(mb), 1024.0)) * 1048576)
    counts: Counter = Counter()
    sizes: Counter = Counter()
    example: dict = {}
    first = last = None
    st: dict = {}
    n = 0
    for line in iter_reverse(Path(path), cap, st):
        k = _key(line)
        if k is None:
            continue
        n += 1
        counts[k] += 1
        sizes[k] += len(line) + 2
        example.setdefault(k, line[:300])
        ts = line.split("|", 1)[0]
        if "|" in line and ts[:4].isdigit():
            last = last or ts
            first = ts
    total = sum(sizes.values()) or 1
    return {"file": Path(path).name, "lines": n,
            "scanned_mb": round(st.get("scanned_bytes", 0) / 1e6, 1),
            "reached_start": bool(st.get("reached_start")),
            "from": first, "to": last,
            "top": [{"count": c, "share_pct": round(100.0 * sizes[k] / total, 1),
                     "key": k, "example": example[k]}
                    for k, c in counts.most_common(max(1, min(int(top), 200)))]}
