"""PS-34b: `photonscript monitor` helpers (parse, filter, colorize, follow)."""

from datetime import datetime

from photonscript.shared import logmonitor as lm

L1 = "2026-09-26 21:04:05 [photonscript.armer  ] INFO    ARMED for 2026-09-26 [guided]"
L2 = "2026-09-26 21:05:00 [photonscript.armer  ] WARNING cooler off at 22.1°C, setpoint 0°C"
L3 = "2026-09-26 21:06:00 [photonscript.app    ] ERROR   boom [x]"
TB = "Traceback (most recent call last):"


def test_parse_line():
    ln = lm.parse_line(L2 + "\r\n")
    assert ln.level == "WARNING" and ln.name == "photonscript.armer"
    assert ln.ts == datetime(2026, 9, 26, 21, 5, 0)
    assert "cooler" in ln.msg
    assert lm.parse_line(TB).level == ""  # continuation line


def test_parse_since():
    now = datetime(2026, 9, 26, 12, 0, 0)
    assert lm.parse_since("30m", now) == datetime(2026, 9, 26, 11, 30)
    assert lm.parse_since("2h", now) == datetime(2026, 9, 26, 10, 0)
    assert lm.parse_since("", now) is None
    try:
        lm.parse_since("yesterday", now)
        assert False
    except ValueError:
        pass


def test_filter_level_grep_and_traceback_follow():
    f = lm.LineFilter("warning")
    kept = [ln for ln in (L1, L2, L3, TB) if f.keep(lm.parse_line(ln))]
    assert kept == [L2, L3, TB]  # traceback follows the kept ERROR
    g = lm.LineFilter(grep="COOLER")
    assert [ln for ln in (L1, L2, L3) if g.keep(lm.parse_line(ln))] == [L2]


def test_filter_since():
    f = lm.LineFilter(since=datetime(2026, 9, 26, 21, 5, 30))
    assert [ln for ln in (L1, L2, L3) if f.keep(lm.parse_line(ln))] == [L3]


def test_markup_colors_and_escapes():
    m = lm.to_markup(lm.parse_line(L3))
    assert "bold red" in m and r"\[x]" in m  # brackets escaped for rich
    assert "bold cyan" in lm.to_markup(lm.parse_line(L1))  # ARMED highlighted
    from rich.console import Console
    Console(file=open("/dev/null", "w")).print(m)  # valid markup


def test_last_lines(tmp_path):
    p = tmp_path / "a.log"
    p.write_text("\n".join(f"line {i}" for i in range(1000)) + "\n")
    assert lm.last_lines(p, 3) == ["line 997", "line 998", "line 999"]


def test_follower_handles_partial_and_rotation(tmp_path):
    p = tmp_path / "photonscript.log"
    p.write_text("old\n")
    fol = lm.FileFollower(p, from_end=True)
    assert fol.poll() == []
    with open(p, "a") as f:
        f.write("one\ntw")
    assert fol.poll() == ["one"]
    with open(p, "a") as f:
        f.write("o\n")
    assert fol.poll() == ["two"]
    # rotation: file renamed, new file created
    p.rename(tmp_path / "photonscript.log.1")
    p.write_text("fresh\n")
    assert fol.poll() == ["fresh"]


def test_read_from_offset(tmp_path):
    p = tmp_path / "photonscript.log"
    p.write_text("a\nb\nc\n")
    d = lm.read_from_offset(p, -1, tail_lines=2)
    assert d["lines"] == ["b", "c"]
    off = d["offset"]
    with open(p, "a") as f:
        f.write("d\npartial")
    d2 = lm.read_from_offset(p, off)
    assert d2["lines"] == ["d"]
    # truncated/rotated below our offset -> restart from 0
    p.write_text("x\n")
    d3 = lm.read_from_offset(p, d2["offset"])
    assert d3["rotated"] and d3["lines"] == ["x"]
