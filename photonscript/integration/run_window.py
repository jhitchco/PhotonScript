"""Desktop run window for the automatic PixInsight jobs (autointegrate,
integrate-watch).

PixInsight 1.9.5 crashed at startup (exit 0x80000003 inside
Qt6WebEngineCore) on every overnight automatic run (2026-10-14, 10-17,
10-22, 10-25, all near 01:00 local) even with
QTWEBENGINE_CHROMIUM_FLAGS=--disable-gpu and the software-rendering retry
(runner.py); daytime runs from the same Scheduled Task and CLI runs work.
The likely cause is the locked Windows session / display off. So:

  - autointegrate_window_local ("08:00-23:00", desktop .env): an automatic
    cycle outside it starts no PixInsight ("deferred: outside the run
    window"); the next tick inside it picks the work up (the decisions are
    idempotent, nothing is lost). Blank = any time. A window may wrap
    midnight ("22:00-06:00").
  - autointegrate_lock_check (True): a locked Windows session also defers
    ("deferred: Windows session locked"): OpenInputDesktop / SwitchDesktop
    fail on the Winlogon desktop, or LogonUI.exe runs in this session.

A reading that fails never blocks. `photonscript integrate` / `finish` /
`blend` run by hand are not gated; `autointegrate` / `integrate-watch` run
by hand take --anytime to skip the gate.
"""

from __future__ import annotations

import os
from datetime import datetime, time as dtime

DEFAULT_WINDOW = "08:00-23:00"


def parse_window(text: str | None) -> tuple[dtime, dtime] | None:
    """"HH:MM-HH:MM" -> (start, end); blank / "off" / "any" -> None (any
    time). Raises ValueError on a malformed window."""
    s = (text or "").strip().lower()
    if s in ("", "off", "any", "none", "0"):
        return None
    try:
        a, b = (p.strip() for p in s.split("-", 1))
        start = datetime.strptime(a, "%H:%M").time()
        end = datetime.strptime(b, "%H:%M").time()
    except ValueError as e:
        raise ValueError(f"run window {text!r}: expected HH:MM-HH:MM ({e})") from None
    return start, end


def in_window(window: tuple[dtime, dtime] | None, local_now: datetime) -> bool:
    """True inside [start, end); a window whose end is before its start
    wraps midnight; start == end means the whole day."""
    if window is None:
        return True
    start, end = window
    t = local_now.time().replace(tzinfo=None)
    if start == end:
        return True
    if start < end:
        return start <= t < end
    return t >= start or t < end


# --- locked Windows session ------------------------------------------------------

DESKTOP_SWITCHDESKTOP = 0x0100


def _input_desktop_locked() -> bool | None:
    """True when the input desktop is not this user's (locked: Winlogon owns
    it), False when it is, None when it cannot be read."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        h = user32.OpenInputDesktop(0, False, DESKTOP_SWITCHDESKTOP)
        if not h:
            return True
        try:
            return not bool(user32.SwitchDesktop(ctypes.c_void_p(h)))
        finally:
            user32.CloseDesktop(ctypes.c_void_p(h))
    except Exception:  # noqa: BLE001 - no user32: unknown
        return None


def _logonui_in_session() -> bool | None:
    """True when LogonUI.exe runs in this process's session (the lock
    screen), False when not, None when it cannot be read."""
    try:
        import ctypes
        import psutil
        k32 = ctypes.windll.kernel32

        def sid(pid: int) -> int | None:
            out = ctypes.c_uint(0)
            return out.value if k32.ProcessIdToSessionId(pid, ctypes.byref(out)) else None
        mine = sid(os.getpid())
        for p in psutil.process_iter(["name", "pid"]):
            if (p.info.get("name") or "").lower() == "logonui.exe":
                s = sid(p.info["pid"])
                if mine is None or s is None or s == mine:
                    return True
        return False
    except Exception:  # noqa: BLE001
        return None


def session_locked() -> str | None:
    """Why the Windows session counts as locked, else None (also off
    Windows and when nothing can be read)."""
    if os.name != "nt":
        return None
    d = _input_desktop_locked()
    if d:
        return "input desktop not available (OpenInputDesktop / SwitchDesktop failed)"
    if _logonui_in_session():
        return "LogonUI.exe running (lock screen)"
    return None


def gate(window_text: str | None, lock_check: bool, local_now: datetime | None = None,
         locked=session_locked) -> str | None:
    """Why an automatic PixInsight start is deferred now, else None. A
    malformed window never blocks (the CLI checks it with parse_window
    and says so)."""
    local_now = local_now or datetime.now()
    try:
        win = parse_window(window_text)
    except ValueError:
        win = None
    if not in_window(win, local_now):
        return (f"deferred: outside the run window ({window_text.strip()}, now "
                f"{local_now:%H:%M}, autointegrate_window_local)")
    if lock_check:
        why = locked()
        if why:
            return f"deferred: Windows session locked ({why}, autointegrate_lock_check)"
    return None
