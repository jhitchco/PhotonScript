"""PS-167 live watch: the mount driver refusing PHD2's guide pulses.

The RC16 agent tails PHD2's newest debug log (phd2_logs.find_logs, read
only) every PULSE_REFUSAL_TICK_S and feeds the new lines to
shared.pulse_refusal.Scanner. When phd2_pulse_refusal_min refusals have
been seen this night, poll() returns one hit (with the exact error line and
the diagnosis) so the agent can page once per night. PHD2 suppressed its own
alert for this on 2026-10-05 to 10-07 (/Confirm/2/PulseGuideFailedAlertEnabled
= 0) and its event server sends nothing for a suppressed alert, so the log
is the only live source.

Tail rules: the first poll starts START_BACK_BYTES before the end (an agent
restarted mid-failure still sees it; the page latch is per night, so old
lines never page twice); a new or shorter file is read from its start; a
partial last line waits for the next poll. Never writes anything.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from photonscript.shared import pulse_refusal as pr

logger = logging.getLogger(__name__)

START_BACK_BYTES = 256 * 1024
MAX_READ_BYTES = 8 * 1024 * 1024   # per poll: a night's worth is ~20 MB


class DebugLogTail:
    """New whole lines of the newest PHD2 debug log since the last read."""

    def __init__(self, finder):
        self._finder = finder          # () -> Path | None
        self.path: Path | None = None
        self.offset = 0
        self.started_mid = False      # the current file was joined mid-way
        self._rest = b""

    def skip_to_end(self) -> None:
        """Start the next read at the current end of the newest log."""
        p = self._finder()
        self.path, self._rest, self.started_mid = (Path(p) if p else None), b"", True
        try:
            self.offset = self.path.stat().st_size if self.path else 0
        except OSError:
            self.offset = 0

    def read_new(self) -> tuple[Path | None, list[str], bool]:
        """(path, lines, new_file). new_file is True when the file changed
        (rollover or first read): the caller restarts its Scanner."""
        p = self._finder()
        if p is None:
            return None, [], False
        p = Path(p)
        try:
            size = p.stat().st_size
        except OSError:
            return None, [], False
        new_file = False
        if self.path is None or p != self.path:
            first = self.path is None
            self.path, self._rest = p, b""
            self.offset = max(0, size - START_BACK_BYTES) if first else 0
            self.started_mid = self.offset > 0
            new_file = True
        elif size < self.offset:            # truncated / replaced
            self.offset, self._rest, new_file = 0, b"", True
            self.started_mid = False
        if size <= self.offset:
            return p, [], new_file
        try:
            with open(p, "rb") as fh:
                fh.seek(self.offset)
                data = fh.read(min(size - self.offset, MAX_READ_BYTES))
        except OSError as e:
            logger.debug("PHD2 debug log not readable: %s", e)
            return p, [], new_file
        self.offset += len(data)
        data = self._rest + data
        cut = data.rfind(b"\n")
        if cut < 0:
            self._rest = data
            return p, [], new_file
        self._rest = data[cut + 1:]
        text = data[:cut].decode("utf-8", "replace")
        return p, text.splitlines(), new_file


def newest_debug_log(config) -> Path | None:
    from photonscript.scheduler.phd2_logs import find_logs
    files = find_logs(config, "debug").get("files") or []
    return files[-1] if files else None


class PulseRefusalWatch:
    """poll() -> a hit dict once per night when refusals reach the minimum,
    else None. The night comes from night_fn (shared.phd2_store.night_of)."""

    def __init__(self, config, finder=None, night_fn=None):
        self.config = config
        self.tail = DebugLogTail(finder or (lambda: newest_debug_log(config)))
        self._night_fn = night_fn or self._night
        self.night: str | None = None
        self.scanner: pr.Scanner | None = None
        self.hit_nights: set[str] = set()

    def _night(self) -> str:
        from photonscript.shared.phd2_store import night_of
        return night_of(self.config)

    @staticmethod
    def _now_local() -> datetime:
        return datetime.now()

    def minimum(self) -> int:
        return int(getattr(self.config, "phd2_pulse_refusal_min", 3) or 0)

    def poll(self) -> dict | None:
        if self.minimum() <= 0:
            return None
        night = self._night_fn()
        path, lines, new_file = self.tail.read_new()
        if path is None:
            return None
        if new_file and not self.tail.started_mid:
            # a whole new file: its name gives the date its clock starts on
            self.night = night
            self.scanner = pr.Scanner(pr.file_start(path.name), path.name)
        elif new_file or night != self.night or self.scanner is None:
            # joined mid-file, or a new night inside the same file: count
            # afresh, dated today on the scope PC's clock (the one PHD2 uses)
            self.night = night
            self.scanner = pr.Scanner(None, path.name, day=self._now_local())
        for ln in lines:
            self.scanner.feed(ln)
        s = self.scanner.summary()
        if s["refusals"] < self.minimum() or night in self.hit_nights:
            return None
        self.hit_nights.add(night)
        d = pr.diagnose(s)
        return {"night": night, "file": path.name, "summary": s, **d,
                "t_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def page_text(hit: dict, target: str | None = None) -> str:
    """The page: what, since when, the exact error, the operator fix. Kept
    under Pushover's 1024 characters."""
    s = hit["summary"]
    since = (s.get("first_error_at") or "?").replace("T", " ")[:16]
    also = []
    if s.get("side_of_pier_errors") or "SideOfPier" in (s.get("members") or []):
        also.append("SideOfPier")
    if s.get("coordinates_unavailable"):
        also.append("coordinates")
    text = (f"{hit['title']}: {s['refusals']} guide pulses refused since {since} "
            f"local ({target or 'no target'}). Exact error: {s.get('first_error') or '?'}"
            + (f", then {s['first_refusal']}" if s.get("first_refusal")
               and s["first_refusal"] != s.get("first_error") else "")
            + (f" [{s['hresult']}]" if s.get("hresult") else "") + ". "
            + (f"{' and '.join(also)} fail too. " if also else "")
            + ("PHD2's own alert is silenced. " if any(
                "PulseGuide" in x for x in s.get("silenced") or []) else "")
            + hit.get("short_fix", ""))
    return text[:1000]
