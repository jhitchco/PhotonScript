"""PS-67 (PS-27 item 5): the mount log, one file per night.

The RC16 telescope agent already reads ninaAPI ``/equipment/mount/info``
every 5 s. MountLogger turns that poll into a compact change log, with no
extra NINA calls:

    <data_dir>/runs/<night>_mount.jsonl

<night> is the local evening date (shared.phd2_store.night_of, noon to noon,
the same date the runs page uses). One JSON object per line, oldest first:

    t         "2026-10-04T03:12:05Z"  UTC of the poll
    rig       "rc16"                  the agent that owns the mount
    ra, dec   degrees (ninaAPI reports RightAscension in HOURS; stored x15)
    alt, az   degrees (None when the driver gives none)
    pier      "East" | "West" | None  (ninaAPI SideOfPier)
    slewing   bool
    tracking  bool
    parked    bool
    why       why this line was written:
              start      first poll after the agent started (or a new night)
              slew-start / slew-end   Slewing changed
              tracking-on / tracking-off
              park / unpark
              pier       pier side changed (a meridian flip)
              move       moved more than mount_log_move_arcmin since the
                         last line (dithers, centering nudges)
              heartbeat  no line for mount_log_heartbeat_s while tracking
              offline    NINA #1 reports the mount disconnected, or a poll
                         with RA 0, Dec 0 and no alt / az (a mount the
                         driver no longer has). The line carries
                         "offline": true and no position (ra / dec / alt /
                         az / pier / parked None); one line per outage.
              reconnect  the first real poll after an offline line

Readers: load() parses a night; position_at() gives the mount position at a
time (the Piggy-600's live pointing); state_at() the mount state at a time
(the Piggy guard); segments() turns the lines into
slewing / tracking / parked / idle spans. A slew window is a "slewing"
segment: it opens at the first line with slewing=true and closes at the
next line with slewing=false (PS-13 slew gating reads these). Lines are
written only on change, so a state holds until the next line; a gap longer
than STALE_S with no line means the poll stopped (agent down, NINA closed)
and the state is unknown after the last line + STALE_S. An offline line
(is_offline, which also knows the pre-marker 0 / 0 polls of older logs)
says nothing about where the mount is: position_at gives None there, its
segment state is "offline", and the slew gate reads it as not covered.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

STALE_S = 300.0   # no line for this long (while tracking) = poll stopped


def log_path(config, night: str) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "runs" / f"{night}_mount.jsonl"


def _f(v):
    """A finite float or None. ninaAPI can report NaN (e.g. Alt/Az while the
    driver has no fix); json.dumps writes NaN, which strict JSON (FastAPI's
    response encoder) refuses, so a NaN line made /api/status/mount-log 500
    (PS-173)."""
    try:
        x = float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
    return x if x is None or math.isfinite(x) else None


def _clean(v):
    """Non-finite floats in a loaded line -> None (lines written before
    PS-173 can hold NaN)."""
    if isinstance(v, float) and not math.isfinite(v):
        return None
    return v


def poll_offline(mount: dict) -> bool:
    """A ninaAPI mount poll that says nothing about the mount: NINA reports
    it disconnected, or RA 0, Dec 0 with no alt / az (2026-10-09 03:08Z,
    NINA #1 lost the mount and kept answering zeros)."""
    if not isinstance(mount, dict):
        return False
    if mount.get("Connected") is False:
        return True
    ra_h, dec = _f(mount.get("RightAscension")), _f(mount.get("Declination"))
    return (ra_h == 0.0 and dec == 0.0 and _f(mount.get("Altitude")) is None
            and _f(mount.get("Azimuth")) is None)


def is_offline(r: dict) -> bool:
    """A stored line that is an offline marker: "offline": true, or (logs
    written before the marker) RA 0, Dec 0 and no alt / az."""
    if not isinstance(r, dict):
        return False
    if r.get("offline"):
        return True
    try:
        zero = float(r.get("ra")) == 0.0 and float(r.get("dec")) == 0.0
    except (TypeError, ValueError):
        return False
    return zero and r.get("alt") is None and r.get("az") is None


def sample_from_nina(mount: dict) -> dict | None:
    """ninaAPI mount info -> {ra, dec (deg), alt, az, pier, slewing,
    tracking, parked}; None when the payload has no coordinates or the
    mount is offline (poll_offline: never a position at RA 0 / Dec 0)."""
    from photonscript.shared.pointing import pier_name
    if not isinstance(mount, dict) or poll_offline(mount):
        return None
    ra_h, dec = _f(mount.get("RightAscension")), _f(mount.get("Declination"))
    if ra_h is None or dec is None:
        return None
    tracking = mount.get("TrackingEnabled", mount.get("Tracking"))
    return {"ra": round((ra_h * 15.0) % 360.0, 5), "dec": round(dec, 5),
            "alt": _round(_f(mount.get("Altitude"))),
            "az": _round(_f(mount.get("Azimuth"))),
            "pier": pier_name(mount.get("SideOfPier")),
            "slewing": bool(mount.get("Slewing")),
            "tracking": bool(tracking),
            "parked": bool(mount.get("AtPark"))}


def _round(v, nd=3):
    return None if v is None else round(v, nd)


class MountLogger:
    """Change detector for the mount poll. observe() returns the line it
    wrote (or would write with write=False), else None."""

    def __init__(self, config, rig: str = "rc16"):
        self.config = config
        self.rig = rig
        self.last: dict | None = None
        self.last_t: datetime | None = None
        self.night: str | None = None
        self.offline = False   # the last line written is an offline marker

    def _why(self, s: dict, now: datetime) -> str | None:
        from photonscript.shared.pointing import sep_arcmin
        last = self.last
        if last is None:
            return "reconnect" if self.offline else "start"
        if s["slewing"] != last["slewing"]:
            return "slew-start" if s["slewing"] else "slew-end"
        if s["parked"] != last["parked"]:
            return "park" if s["parked"] else "unpark"
        if s["tracking"] != last["tracking"]:
            return "tracking-on" if s["tracking"] else "tracking-off"
        if s["pier"] != last["pier"] and s["pier"] is not None:
            return "pier"
        move = float(getattr(self.config, "mount_log_move_arcmin", 1.0) or 1.0)
        if sep_arcmin(last["ra"], last["dec"], s["ra"], s["dec"]) > move:
            return "move"
        beat = float(getattr(self.config, "mount_log_heartbeat_s", 60) or 60)
        if s["tracking"] and self.last_t is not None \
                and (now - self.last_t).total_seconds() >= beat:
            return "heartbeat"
        return None

    def observe(self, mount: dict, now: datetime | None = None,
                write: bool = True) -> dict | None:
        from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
        offline = poll_offline(mount)
        s = None if offline else sample_from_nina(mount)
        if s is None and not offline:
            return None
        now = now or datetime.utcnow()
        night = night_of(self.config, now)
        if night != self.night:   # a new night file starts with a full line
            self.night, self.last, self.offline = night, None, False
        if offline:
            # one marker per outage, never a position (no RA 0 / Dec 0)
            if self.offline:
                return None
            line = {"t": iso_z(now), "rig": self.rig, "offline": True,
                    "ra": None, "dec": None, "alt": None, "az": None,
                    "pier": None, "slewing": False, "tracking": False,
                    "parked": None, "why": "offline"}
            self.last, self.last_t, self.offline = None, now, True
            if write:
                append_jsonl(log_path(self.config, night), line)
            return line
        why = self._why(s, now)
        if why is None:
            return None
        line = {"t": iso_z(now), "rig": self.rig, **s, "why": why}
        self.last, self.last_t, self.offline = s, now, False
        if write:
            append_jsonl(log_path(self.config, night), line)
        return line


# ------------------------------------------------------------------ readers

def load(config, night: str) -> list[dict]:
    """A night's lines, time-ordered, each with a parsed "dt" (naive UTC)."""
    from photonscript.shared.phd2_store import parse_z, read_jsonl
    out = []
    for r in read_jsonl(log_path(config, night)):
        if not isinstance(r, dict):
            continue
        dt = parse_z(r.get("t"))
        if dt is not None:
            out.append({**{k: _clean(v) for k, v in r.items()}, "dt": dt})
    out.sort(key=lambda r: r["dt"])
    return out


def position_at(lines: list[dict], when: datetime,
                stale_s: float = STALE_S) -> dict | None:
    """The mount state in force at `when` (the last line at or before it),
    or None when there is none, it is older than stale_s, or the mount was
    slewing or parked then."""
    import bisect
    if not lines or when is None:
        return None
    ts = [r["dt"] for r in lines]
    i = bisect.bisect_right(ts, when) - 1
    if i < 0:
        return None
    r = lines[i]
    if is_offline(r):
        return None
    if (when - r["dt"]).total_seconds() > stale_s and r.get("tracking"):
        return None
    if r.get("slewing") or r.get("parked"):
        return None
    if r.get("ra") is None or r.get("dec") is None:
        return None
    return r


def moved_during(lines: list[dict], start: datetime, end: datetime) -> bool:
    """True when a slew, pier change or park happened inside [start, end]."""
    for r in lines:
        if start <= r["dt"] <= end and r.get("why") in (
                "slew-start", "slew-end", "pier", "park", "unpark"):
            return True
    return False


def _state(r: dict) -> str:
    if is_offline(r):
        return "offline"
    if r.get("parked"):
        return "parked"
    if r.get("slewing"):
        return "slewing"
    if r.get("tracking"):
        return "tracking"
    return "idle"


def state_at(lines: list[dict], when: datetime,
             stale_s: float = STALE_S) -> tuple[str, dict | None]:
    """(state, line) of the mount at `when` from the last line at or before
    it: tracking | slewing | parked | idle | offline | unknown. Unknown: no
    line, or a tracking / slewing line older than stale_s (tracking writes
    a heartbeat, so the poll stopped). A parked / idle line holds until the
    next line (they write no heartbeat)."""
    import bisect
    if not lines or when is None:
        return "unknown", None
    ts = [r["dt"] for r in lines]
    i = bisect.bisect_right(ts, when) - 1
    if i < 0:
        return "unknown", None
    r = lines[i]
    st = _state(r)
    if st in ("tracking", "slewing") and (when - r["dt"]).total_seconds() > stale_s:
        return "unknown", r
    return st, r


def segments(lines: list[dict], end: datetime | None = None,
             stale_s: float = STALE_S) -> list[dict]:
    """Merge lines into [{start, end, state}] (datetimes), state in
    slewing | tracking | parked | idle | offline. A tracking state with no newer line
    ends stale_s after its last line (the poll stopped)."""
    out: list[dict] = []
    for i, r in enumerate(lines):
        st = _state(r)
        nxt = lines[i + 1]["dt"] if i + 1 < len(lines) else end
        if nxt is None:
            nxt = r["dt"] + timedelta(seconds=stale_s)
        if st == "tracking":
            nxt = min(nxt, r["dt"] + timedelta(seconds=stale_s))
        if out and out[-1]["state"] == st and out[-1]["end"] >= r["dt"]:
            out[-1]["end"] = max(out[-1]["end"], nxt)
        else:
            out.append({"start": r["dt"], "end": nxt, "state": st})
    return [s for s in out if s["end"] > s["start"]]


def slew_windows(lines: list[dict]) -> list[tuple[datetime, datetime]]:
    """[(start, end)] of every slewing segment (PS-13 can pad these)."""
    return [(s["start"], s["end"]) for s in segments(lines)
            if s["state"] == "slewing"]
