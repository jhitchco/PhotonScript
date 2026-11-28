"""PS-162: one exposure window per sub record.

A subs record's legacy `time` field means two different things: the live
grader (telescope_agent) writes the processing time, just after readout,
as ISO with a trailing "Z" (the exposure END); the backfill grader
(runs.py) writes the FITS DATE-OBS, no "Z" (the exposure START). Code that
compared raw `time` values (the Piggy-600 correlation, the dawn timeline)
was off by one exposure between the two rigs.

Every record now carries explicit `start_utc` and `end_utc` (ISO, naive
UTC with a "Z", second resolution). New records are written with them;
older records get them on read (runs._load_subs, migrate on read), and a
later rewrite of the night's log persists them. `time` stays as written,
for compatibility.

Precedence for the start: `start_utc`, then `date_obs`, then `time`
(start when it has no "Z", end when it has one). The end is the start plus
`exp_s` unless `end_utc` is stored.
"""

from __future__ import annotations

from datetime import datetime, timedelta

START, END = "start_utc", "end_utc"


def parse_utc(ts) -> datetime | None:
    """ISO timestamp ('Z', offsets, naive, space separator) -> naive UTC."""
    from photonscript.shared.safety_history import _utc
    if ts is None or ts == "":
        return None
    if isinstance(ts, str):
        ts = ts.strip().replace(" ", "T")
    return _utc(ts)


def fmt_utc(dt: datetime | None) -> str | None:
    return None if dt is None else dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _exp(rec: dict) -> float:
    try:
        return max(0.0, float(rec.get("exp_s") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def time_is_end(rec: dict) -> bool:
    """True when the legacy `time` is the live grader's end time ("Z")."""
    return str(rec.get("time") or "").strip().endswith("Z")


def sub_window(rec: dict) -> tuple[datetime, datetime] | None:
    """(start, end) of the exposure, naive UTC, or None when unknown."""
    if not isinstance(rec, dict):
        return None
    exp = timedelta(seconds=_exp(rec))
    st = parse_utc(rec.get(START))
    if st is None:
        st = parse_utc(rec.get("date_obs"))
    if st is None:
        t = parse_utc(rec.get("time"))
        if t is None:
            return None
        st = t - exp if time_is_end(rec) else t
    en = parse_utc(rec.get(END)) if rec.get(START) else None
    if en is None or en < st:
        en = st + exp
    return st, en


def sub_start(rec: dict) -> datetime | None:
    w = sub_window(rec)
    return w[0] if w else None


def sub_end(rec: dict) -> datetime | None:
    w = sub_window(rec)
    return w[1] if w else None


def normalize(rec: dict) -> dict:
    """Fill `start_utc` / `end_utc` in place when missing (migrate on read).
    A record whose window cannot be told is left as is. Returns rec."""
    if not isinstance(rec, dict) or (rec.get(START) and rec.get(END)):
        return rec
    w = sub_window(rec)
    if w is not None:
        rec[START], rec[END] = fmt_utc(w[0]), fmt_utc(w[1])
    return rec


def window_fields(start: datetime | None, exp_s) -> dict:
    """The two fields for a writer that knows the exposure start."""
    if start is None:
        return {}
    try:
        exp = max(0.0, float(exp_s or 0.0))
    except (TypeError, ValueError):
        exp = 0.0
    return {START: fmt_utc(start),
            END: fmt_utc(start + timedelta(seconds=exp))}
