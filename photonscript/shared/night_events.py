"""PS-67: what each rig was doing, as a per-night change log.

    <data_dir>/runs/<night>_events.jsonl

One JSON object per line, written only when something changes:

    t      "2026-10-04T03:12:05Z"  UTC
    rig    "rc16" | "piggyback"
    src    "nina" | "phd2"
    kind   "instruction"  the running NINA sequence item (value = its name,
                          "" when the sequence is idle)
           "guider"       PHD2 state (guiding, settling, calibrating,
                          lost_star, stopped, error)
           "rms"          a 60 s guiding RMS sample while guiding (value =
                          total RMS, units = "arcsec" or "px": PHD2 guide
                          pixels until the scale is known, PS-70)
    value  see kind
    units  rms only

The mount itself is in runs/<night>_mount.jsonl (shared.mount_log). The
night timeline (scheduler.night_timeline) merges both with the safety
history and the sub exposure spans.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def events_path(config, night: str) -> Path:
    return Path(getattr(config, "data_dir", ".")) / "runs" / f"{night}_events.jsonl"


class EventLog:
    """Writes a line when (rig, kind) changes value; rms samples are rate
    limited to one per rms_every_s."""

    def __init__(self, config, rig: str, rms_every_s: float = 60.0):
        self.config = config
        self.rig = rig
        self.rms_every_s = rms_every_s
        self._last: dict = {}
        self._rms_at: datetime | None = None

    def change(self, src: str, kind: str, value, now: datetime | None = None,
               **extra) -> dict | None:
        from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
        now = now or datetime.utcnow()
        key = (src, kind)
        if key in self._last and self._last[key] == value:
            return None
        self._last[key] = value
        line = {"t": iso_z(now), "rig": self.rig, "src": src, "kind": kind,
                "value": value, **extra}
        append_jsonl(events_path(self.config, night_of(self.config, now)), line)
        return line

    def rms(self, value, units: str, now: datetime | None = None) -> dict | None:
        from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
        now = now or datetime.utcnow()
        if value is None:
            return None
        if self._rms_at is not None and \
                (now - self._rms_at).total_seconds() < self.rms_every_s:
            return None
        self._rms_at = now
        line = {"t": iso_z(now), "rig": self.rig, "src": "phd2", "kind": "rms",
                "value": round(float(value), 3), "units": units}
        append_jsonl(events_path(self.config, night_of(self.config, now)), line)
        return line


def load(config, night: str) -> list[dict]:
    """A night's events, time-ordered, each with a parsed "dt"."""
    from photonscript.shared.phd2_store import parse_z, read_jsonl
    out = []
    for r in read_jsonl(events_path(config, night)):
        dt = parse_z(r.get("t"))
        if dt is not None:
            out.append({**r, "dt": dt})
    out.sort(key=lambda r: r["dt"])
    return out
