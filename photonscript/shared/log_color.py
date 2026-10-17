"""Colored console logging (2026-10-16).

When the console stream is a TTY and env PS_LOG_COLOR is not "0", the
console handler colors each record by level: ERROR / CRITICAL red (their
tracebacks too), WARNING yellow, INFO default, DEBUG dim. Log files stay
plain. On Windows ANSI is enabled first (colorama when installed, else
SetConsoleMode with ENABLE_VIRTUAL_TERMINAL_PROCESSING).
"""

from __future__ import annotations

import logging
import os

RESET = "\x1b[0m"
LEVEL_ANSI = {
    logging.DEBUG: "\x1b[2m",        # dim
    logging.WARNING: "\x1b[33m",     # yellow
    logging.ERROR: "\x1b[31m",       # red
    logging.CRITICAL: "\x1b[1;31m",  # bold red
}

_ansi_ready: bool | None = None


def _level_code(levelno: int) -> str:
    if levelno >= logging.CRITICAL:
        return LEVEL_ANSI[logging.CRITICAL]
    if levelno >= logging.ERROR:
        return LEVEL_ANSI[logging.ERROR]
    if levelno >= logging.WARNING:
        return LEVEL_ANSI[logging.WARNING]
    if levelno >= logging.INFO:
        return ""
    return LEVEL_ANSI[logging.DEBUG]


class ColorFormatter(logging.Formatter):
    """logging.Formatter that wraps the whole formatted record (message,
    traceback and stack included) in the level's ANSI color."""

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        code = _level_code(record.levelno)
        return f"{code}{text}{RESET}" if code else text


def enable_windows_ansi() -> bool:
    """Make the Windows console interpret ANSI escapes. True when it does
    (always True off Windows). Never raises."""
    global _ansi_ready
    if _ansi_ready is not None:
        return _ansi_ready
    if os.name != "nt":
        _ansi_ready = True
        return True
    try:
        import colorama  # type: ignore
        colorama.just_fix_windows_console()
        _ansi_ready = True
        return True
    except Exception:  # noqa: BLE001 - not installed or old: fall through
        pass
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(h, ctypes.byref(mode)):
            _ansi_ready = False
            return False
        _ansi_ready = bool(k32.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:  # noqa: BLE001
        _ansi_ready = False
    return _ansi_ready


def color_wanted(stream) -> bool:
    """A TTY stream, PS_LOG_COLOR not "0", and ANSI usable."""
    if os.environ.get("PS_LOG_COLOR", "").strip() == "0":
        return False
    try:
        if not (stream is not None and stream.isatty()):
            return False
    except Exception:  # noqa: BLE001
        return False
    return enable_windows_ansi()


def colorize_console(logger: logging.Logger | None = None) -> int:
    """Give every console StreamHandler (not a FileHandler) of `logger`
    (default root) a ColorFormatter with its current format, when its
    stream wants color. Returns how many handlers were switched."""
    lg = logger if logger is not None else logging.getLogger()
    n = 0
    for h in lg.handlers:
        if (not isinstance(h, logging.StreamHandler)
                or isinstance(h, logging.FileHandler)
                or isinstance(h.formatter, ColorFormatter)):
            continue
        if not color_wanted(getattr(h, "stream", None)):
            continue
        f = h.formatter or logging.Formatter()
        h.setFormatter(ColorFormatter(f._fmt, datefmt=f.datefmt))
        n += 1
    return n


__all__ = ["ColorFormatter", "colorize_console", "color_wanted",
           "enable_windows_ansi"]
