"""The mount driver refusing PHD2's guide pulses, read from PHD2's debug log
(PS-167).

2026-10-05 22:55 to 2026-10-07 (and still on 10-07 at 19:42): every PHD2
guide pulse failed. PHD2's debug log, on every pulse:

    ScopeASCOM::IsSlewing failed: (ASCOM.SoftwareBisque.Telescope) Slewing
    pulseguide: [80020009] Exception occurred.
    Error thrown from ...scope_ascom.cpp:600->ASCOM Scope: pulseguide command
        failed: (ASCOM.SoftwareBisque.Telescope) PulseGuide
    GetBoolean("/Confirm/2/PulseGuideFailedAlertEnabled", 1) returns 0
    Suppressed alert:  PulseGuide command to mount has failed - ...
    Move returns status 1, amount 0

and the guide log logged 0 ms pulses on every frame with "Pier side =
Unknown" and no RA / Dec. What the lines say (PHD2 2.6.14 scope_ascom.cpp):
"IsSlewing failed" is the driver THROWING on the Slewing property (not
answering true), and the exception text is only the member name. The same
PHD2 connection also failed SideOfPier and could not read coordinates
(!m_canGetCoordinates) while GuideRateRightAscension still answered 7.48"/s
(a driver setting, no TheSky call). So PHD2's copy of the in-process Bisque
driver could not reach TheSky at all; NINA's copy (a separate process) slewed,
tracked and parked normally all night. PHD2 had connected its mount at
2026-10-05 08:50 (SideOfPier answered then) and never reconnected; that day
the scope PC ran two TheSky apps and TheSky was reworked (PS-138), so PHD2's
connection was left pointing at a TheSky that was gone. The cure is to
reconnect the mount in PHD2 (or `photonscript guide-recover`), not to
restart TheSky.

Scanner reads lines (incrementally) and keeps counts, the first exact error,
the failing members, the PHD2 mount connects, pulses that worked, and the
alerts PHD2 suppressed. diagnose() turns a summary into a cause and the
operator fix. Pure: no file or network access here; callers hand in lines
(the agent tails the live log, the morning analysis and the CLI read files).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

# HH:MM:SS.mmm <delta> <thread> <text>
_LINE = re.compile(r"^(\d\d):(\d\d):(\d\d)\.(\d{3})\s+\S+\s+\d+\s+(.*)$")
_FILE_TS = re.compile(r"PHD2_DebugLog_(\d{4}-\d\d-\d\d)_(\d{6})")

# "(ASCOM.SoftwareBisque.Telescope) Slewing": the driver's source and the
# member it threw on
_SRC_MEMBER = re.compile(r"\(([^()]+)\)\s*([A-Za-z]+)\s*$")
_HRESULT = re.compile(r"\[([0-9A-Fa-f]{8})\]")

IS_SLEWING = "ScopeASCOM::IsSlewing failed:"
PG_FAILED = "pulseguide command failed:"
SIDE_FAILED = "SideOfPier failed:"
NO_COORDS = "!m_canGetCoordinates"
RATES_OK = "ScopeASCOM::GetGuideRates returns 0"
SUPPRESSED = "Suppressed alert:"
MOVE = "Move returns status"
CONNECTING = "Connecting to mount ["
CONNECT_OK = "ASCOM Scope: Connect success"
DISCONNECTED = "ASCOM Scope: Disconnected"
STARTED = "begins execution"
_CONFIRM = re.compile(r'GetBoolean\("/Confirm/([^/"]+)/([^"]+)",\s*\d+\)\s*returns\s*0')
_MOVE = re.compile(r"Move returns status (\d+), amount (-?\d+)")

# the substrings a caller may pre-filter on (any line holding one of these
# can matter to the Scanner)
HRESULT_LINES = ("pulseguide: [", "invoke: [")
NEEDLES = (IS_SLEWING, PG_FAILED, SIDE_FAILED, NO_COORDS, RATES_OK, SUPPRESSED,
           MOVE, CONNECTING, CONNECT_OK, DISCONNECTED, STARTED, "/Confirm/",
           *HRESULT_LINES)

# what the operator does (the page, the morning finding and the CLI say it)
FIX_STALE = (
    "Reconnect the mount in PHD2, not TheSky: in PHD2 stop guiding, open "
    "Connect Equipment, Disconnect the mount, then Connect it again (TheSky64 "
    "running with the Paramount connected and only one TheSky app open). "
    "`photonscript guide-recover --dry-run` on the scope PC shows the same "
    "steps (stop_capture, set_connected false / true) and runs them without "
    "--dry-run. NINA restarts guiding at its next StartGuiding. Do not "
    "restart TheSky: that drops NINA's mount as well. Then re-enable PHD2's "
    "silenced 'PulseGuide failed' alert.")
FIX_SLEWING = (
    "TheSky reports a slew in progress, so the driver refuses pulses. If the "
    "mount is really slewing, wait. If not (a stuck slew after an aborted or "
    "closed-loop slew), abort it in TheSky (Telescope > Abort) or run "
    "`photonscript guide-recover --dry-run` on the scope PC, then reconnect "
    "the mount in PHD2.")
FIX_UNKNOWN = (
    "Read PHD2's debug log around the first failure; reconnect the mount in "
    "PHD2 (`photonscript guide-recover --dry-run` shows the steps).")


# the same, short enough for a Pushover page (1024 characters in all)
SHORT_FIX = {
    "stale_driver": ("Fix: in PHD2 stop guiding, Disconnect then Connect the mount "
                     "(TheSky64 running, mount connected), or on the scope PC run "
                     "photonscript guide-recover --dry-run. Do not restart TheSky."),
    "slewing": ("Fix: if the mount is not really slewing, abort the stuck slew in "
                "TheSky, then reconnect the mount in PHD2 (photonscript "
                "guide-recover --dry-run shows the steps)."),
    "unknown": ("Fix: reconnect the mount in PHD2 (photonscript guide-recover "
                "--dry-run shows the steps)."),
}


def file_start(name: str) -> datetime | None:
    """PHD2_DebugLog_2026-10-06_203731.txt -> 2026-10-06 20:37:31 (local)."""
    m = _FILE_TS.search(str(name))
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y-%m-%d%H%M%S")
    except ValueError:
        return None


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


class Scanner:
    """Feed PHD2 debug-log lines in order; summary() at any time.

    Times are the scope PC's local clock (as PHD2 writes them). The date
    comes from the file name and rolls forward when the clock goes back
    (PHD2 keeps one file across midnight)."""

    def __init__(self, start: datetime | None = None, source: str = "",
                 day: datetime | None = None):
        """start: the file's start (its name); day: only the date is known
        (a read that begins mid-file), the first line's clock is taken as is."""
        self.source = source
        self._day = (start or day or datetime(1970, 1, 1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        self._last_tod: timedelta | None = (
            timedelta(hours=start.hour, minutes=start.minute, seconds=start.second)
            if start else None)
        self.refusals = 0            # pulses the driver refused
        self.slewing_errors = 0      # IsSlewing threw
        self.side_errors = 0         # SideOfPier threw
        self.coords_unavailable = 0  # PHD2 could not get coordinates
        self.rates_ok = 0            # GuideRate answered (a driver setting)
        self.moves_ok = 0            # pulses that went out (status 0)
        self.moves_failed = 0
        self.members: set[str] = set()
        self.driver: str | None = None
        self.hresult: str | None = None
        self.first_error: str | None = None     # the exact line
        self.first_refusal: str | None = None   # the first pulseguide failure
        self.first_error_at: datetime | None = None
        self.last_error: str | None = None
        self.last_error_at: datetime | None = None
        self.last_ok_at: datetime | None = None
        self.last_connect_at: datetime | None = None   # mount Connect success
        self.connect_ok_after_error = False
        self.mount_name: str | None = None
        self.suppressed: dict[str, int] = {}
        self.silenced: set[tuple[str, str]] = set()   # (profile, value name)
        self.lines = 0

    # --- time -------------------------------------------------------------
    def _when(self, m) -> datetime:
        tod = timedelta(hours=int(m.group(1)), minutes=int(m.group(2)),
                        seconds=int(m.group(3)), milliseconds=int(m.group(4)))
        if self._last_tod is not None and tod < self._last_tod - timedelta(minutes=5):
            self._day += timedelta(days=1)
        self._last_tod = tod
        return self._day + tod

    def _error(self, when: datetime, text: str) -> None:
        if self.first_error is None:
            self.first_error, self.first_error_at = text.strip(), when
        self.last_error, self.last_error_at = text.strip(), when
        m = _SRC_MEMBER.search(text)
        if m:
            self.driver = self.driver or m.group(1).strip()
            self.members.add(m.group(2))

    # --- feeding ----------------------------------------------------------
    def feed(self, line: str) -> None:
        m = _LINE.match(line.rstrip("\r\n"))
        if not m:
            return
        text = m.group(5)
        if not any(n in text for n in NEEDLES):
            return
        self.lines += 1
        when = self._when(m)
        if IS_SLEWING in text:
            self.slewing_errors += 1
            self._error(when, text[text.index(IS_SLEWING):])
        elif PG_FAILED in text:
            self.refusals += 1
            msg = "pulseguide command failed: " + text.split(PG_FAILED, 1)[1].strip()
            self.first_refusal = self.first_refusal or msg
            self._error(when, msg)
        elif SIDE_FAILED in text:
            self.side_errors += 1
            m2 = _SRC_MEMBER.search(text)
            if m2:
                self.driver = self.driver or m2.group(1).strip()
                self.members.add(m2.group(2))
        elif NO_COORDS in text:
            self.coords_unavailable += 1
        elif RATES_OK in text:
            self.rates_ok += 1
        elif SUPPRESSED in text:
            msg = text.split(SUPPRESSED, 1)[1].strip()
            self.suppressed[msg] = self.suppressed.get(msg, 0) + 1
        elif MOVE in text:
            mv = _MOVE.search(text)
            if mv and mv.group(1) == "0":
                self.moves_ok += 1
                self.last_ok_at = when
            elif mv:
                self.moves_failed += 1
        elif CONNECTING in text:
            self.mount_name = text.split(CONNECTING, 1)[1].rstrip("]").strip()
        elif CONNECT_OK in text:
            self.last_connect_at = when
            self.connect_ok_after_error = self.first_error is not None
        elif "/Confirm/" in text:
            c = _CONFIRM.search(text)
            if c:
                self.silenced.add((c.group(1), c.group(2)))
        if self.hresult is None and any(h in text for h in HRESULT_LINES):
            hr = _HRESULT.search(text)
            if hr:
                self.hresult = hr.group(1)

    def feed_text(self, text: str) -> "Scanner":
        for line in text.splitlines():
            self.feed(line)
        return self

    # --- result -----------------------------------------------------------
    def summary(self) -> dict:
        return {
            "source": self.source or None,
            "refusals": self.refusals,
            "slewing_errors": self.slewing_errors,
            "side_of_pier_errors": self.side_errors,
            "coordinates_unavailable": self.coords_unavailable,
            "guide_rate_reads_ok": self.rates_ok,
            "moves_ok": self.moves_ok,
            "moves_failed": self.moves_failed,
            "members": sorted(self.members),
            "driver": self.driver,
            "hresult": self.hresult,
            "first_error": self.first_error,
            "first_refusal": self.first_refusal,
            "first_error_at": _iso(self.first_error_at),
            "last_error": self.last_error,
            "last_error_at": _iso(self.last_error_at),
            "last_ok_at": _iso(self.last_ok_at),
            "mount_connected_at": _iso(self.last_connect_at),
            "reconnected_after_error": self.connect_ok_after_error,
            "mount": self.mount_name,
            "suppressed_alerts": dict(self.suppressed),
            "silenced": sorted(f"/Confirm/{p}/{n}" for p, n in self.silenced),
        }


def scan_text(text: str, name: str = "") -> dict:
    return Scanner(file_start(name), name).feed_text(text).summary()


def diagnose(s: dict) -> dict:
    """{"cause", "title", "detail", "fix"} for a summary with refusals; cause
    is "stale_driver" (every TheSky-backed member throws: PHD2's driver
    connection lost TheSky), "slewing" (only Slewing / PulseGuide throw:
    TheSky's slew state), or "unknown"."""
    n = int(s.get("refusals") or 0)
    members = set(s.get("members") or [])
    others = bool(s.get("side_of_pier_errors") or s.get("coordinates_unavailable")
                  or members - {"Slewing", "PulseGuide"})
    drv = s.get("driver") or "the mount driver"
    first = s.get("first_error") or "?"
    since = s.get("first_error_at") or "?"
    conn = s.get("mount_connected_at")
    if n and others:
        cause, fix = "stale_driver", FIX_STALE
        title = "PHD2's mount connection cannot reach TheSky"
        detail = (f"{drv} refused {n} guide pulse(s) since {since} ('{first}'); the "
                  "same connection also fails "
                  + ", ".join(sorted(members - {'PulseGuide'}) or ["SideOfPier"])
                  + (" and cannot read coordinates" if s.get("coordinates_unavailable") else "")
                  + (", while its guide rate (a driver setting) still reads"
                     if s.get("guide_rate_reads_ok") else "")
                  + ". Not a slew in progress (a slewing mount still answers "
                  "SideOfPier and its position): PHD2's copy of the driver lost TheSky"
                  + (f" (PHD2 last connected the mount {conn})" if conn else
                     " (no PHD2 mount connect in this log: it is older)") + ".")
    elif n and members <= {"Slewing", "PulseGuide"}:
        cause, fix = "slewing", FIX_SLEWING
        title = "The mount driver refuses pulses while TheSky reports a slew"
        detail = (f"{drv} refused {n} guide pulse(s) since {since} ('{first}'); "
                  "other driver reads still work, so TheSky's slew state is the "
                  "likely blocker (a slew in progress, or one stuck after an "
                  "aborted / closed-loop slew).")
    else:
        cause, fix = "unknown", FIX_UNKNOWN
        title = "The mount driver refuses guide pulses"
        detail = f"{drv} refused {n} guide pulse(s) since {since} ('{first}')."
    sil = [x for x in s.get("silenced") or [] if "PulseGuide" in x]
    if sil:
        detail += (" PHD2's own alert for this was silenced (" + ", ".join(sil)
                   + " = 0), so PHD2 showed nothing.")
    return {"cause": cause, "title": title, "detail": detail, "fix": fix,
            "short_fix": SHORT_FIX[cause]}


# --------------------------------------------------------------------------
# ASCOM trace log (only when the driver's trace is on; the Bisque driver's
# "Trace Level" was False on 2026-10-08, so this usually finds no file)
# --------------------------------------------------------------------------

_ASCOM_HIT = re.compile(r"(PulseGuide|Slewing|SideOfPier)", re.I)
_ASCOM_BAD = re.compile(r"(exception|error|fail|80020009|8004)", re.I)


def scan_ascom(text: str) -> dict:
    """Lines of an ASCOM trace log naming PulseGuide / Slewing / SideOfPier
    together with an exception or error: count, first and last line."""
    hits = [ln.strip() for ln in (text or "").splitlines()
            if _ASCOM_HIT.search(ln) and _ASCOM_BAD.search(ln)]
    return {"errors": len(hits), "first": hits[0] if hits else None,
            "last": hits[-1] if hits else None,
            "members": sorted({m.group(1) for ln in hits
                               for m in [_ASCOM_HIT.search(ln)] if m})}
