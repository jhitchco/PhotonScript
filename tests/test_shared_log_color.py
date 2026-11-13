"""2026-10-16: console logs colored by level on a TTY (PS_LOG_COLOR=0
turns it off), files plain; `photonscript monitor` colors tracebacks under
an ERROR red and DEBUG lines dim."""
import io
import logging

from photonscript.shared import log_color as lc
from photonscript.shared import logmonitor as lm


class _TTY(io.StringIO):
    def isatty(self):
        return True


def _rec(level, msg="hello", exc=None):
    return logging.LogRecord("x", level, __file__, 1, msg, None, exc)


def test_formatter_colors_by_level():
    f = lc.ColorFormatter("%(levelname)s %(message)s")
    assert f.format(_rec(logging.INFO)) == "INFO hello"
    assert f.format(_rec(logging.WARNING)).startswith("\x1b[33m")
    assert f.format(_rec(logging.ERROR)).startswith("\x1b[31m")
    assert f.format(_rec(logging.CRITICAL)).startswith("\x1b[1;31m")
    assert f.format(_rec(logging.DEBUG)).startswith("\x1b[2m")
    try:
        raise ValueError("boom")
    except ValueError:
        import sys
        out = f.format(_rec(logging.ERROR, exc=sys.exc_info()))
    assert out.startswith("\x1b[31m") and out.endswith(lc.RESET)
    assert "Traceback" in out


def test_colorize_console_tty_only_and_env_off(monkeypatch, tmp_path):
    monkeypatch.setattr(lc, "_ansi_ready", True)
    lg = logging.getLogger("ps_color_test")
    lg.handlers[:] = []
    tty, pipe = logging.StreamHandler(_TTY()), logging.StreamHandler(io.StringIO())
    fh = logging.FileHandler(tmp_path / "x.log", encoding="utf-8")
    for h in (tty, pipe, fh):
        h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        lg.addHandler(h)
    monkeypatch.setenv("PS_LOG_COLOR", "0")
    assert lc.colorize_console(lg) == 0
    monkeypatch.delenv("PS_LOG_COLOR")
    assert lc.colorize_console(lg) == 1
    assert isinstance(tty.formatter, lc.ColorFormatter)
    assert not isinstance(pipe.formatter, lc.ColorFormatter)
    assert not isinstance(fh.formatter, lc.ColorFormatter)
    lg.propagate = False
    lg.warning("w")
    fh.close()
    assert "\x1b" not in (tmp_path / "x.log").read_text(encoding="utf-8")
    assert tty.stream.getvalue().startswith("\x1b[33m")
    lg.handlers[:] = []


def test_monitor_traceback_red_and_debug_dim():
    lines = ["2026-10-16 20:00:00 [photonscript.x       ] ERROR   bad",
             "Traceback (most recent call last):",
             "ValueError: nan",
             "2026-10-16 20:00:01 [photonscript.x       ] DEBUG   quiet",
             "2026-10-16 20:00:02 [photonscript.x       ] INFO    fine"]
    out = lm.render(lines, lm.LineFilter())
    assert out[1].startswith("[red]") and out[2].startswith("[red]")
    assert "[dim]quiet[/dim]" in out[3]
    assert "fine" in out[4] and "[dim]fine" not in out[4]
    assert lm.render(lines, lm.LineFilter(), color=False) == lines
