"""PS-172: the log tail endpoints never block the event loop, read big files
from the end only, page past one 5000-line page, and stream a whole file on
download=1 up to a cap.

2026-10-08 02:19Z: debug-log GETs stalled the scheduler's loop 6-7 s
(/api/health max_lag_ms_5min 7062); the NINA #1 log was 836 MB."""
import asyncio
import builtins
import os
import time
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.responses import FileResponse, PlainTextResponse

from photonscript.scheduler import app
from photonscript.scheduler import log_files as lf
from photonscript.scheduler.routers import triage
from photonscript.shared.config import PhotonScriptConfig


@pytest.fixture(autouse=True)
def _clear_port_cache():
    triage._PORT_CACHE.clear()


# --- iter_reverse / tail_page ------------------------------------------------

@pytest.mark.parametrize("body", [
    "", "a", "a\n", "\n", "a\nb", "a\nb\n", "a\r\nb\r\n", "a\n\nb\n\n",
    "x" * 50 + "\n" + "y" * 7 + "\nz",
])
@pytest.mark.parametrize("block", [1, 3, 1 << 20])
def test_iter_reverse_matches_splitlines(tmp_path, monkeypatch, body, block):
    monkeypatch.setattr(lf, "_BLOCK", block)
    p = tmp_path / "x.log"
    p.write_bytes(body.encode())
    st = {}
    got = list(lf.iter_reverse(p, None, st))
    assert got == list(reversed(body.splitlines()))
    assert st["reached_start"] is True


def test_iter_reverse_decodes_bad_bytes(tmp_path):
    p = tmp_path / "x.log"
    p.write_bytes(b"ok\n\xff\xfe bad\n")
    assert list(lf.iter_reverse(p)) == ["\ufffd\ufffd bad", "ok"]


def _numbered(p: Path, n: int, pad: int = 60):
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        for i in range(n):
            fh.write(f"{i:08d} " + "." * pad + ("" if i % 7 else " Reloading image") + "\n")
    return p


def test_big_file_tail_reads_only_the_tail_bytes(tmp_path, monkeypatch):
    p = _numbered(tmp_path / "big.log", 400_000)          # ~28 MB
    size = p.stat().st_size
    assert size > 20_000_000
    read = {"bytes": 0}
    real_open = builtins.open

    class Spy:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.fh.close()

        def seek(self, *a):
            return self.fh.seek(*a)

        def tell(self):
            return self.fh.tell()

        def read(self, n=-1):
            b = self.fh.read(n)
            read["bytes"] += len(b)
            return b

    monkeypatch.setattr(lf, "open", lambda *a, **k: Spy(real_open(*a, **k)),
                        raising=False)
    rows = lf.read_rows([p], lines=500)
    assert len(rows) == 500 and rows[-1].startswith("00399999 ")
    assert rows[0].startswith("00399500 ")
    assert read["bytes"] <= 2 * 1024 * 1024            # one or two 1 MB blocks
    assert read["bytes"] < size / 10


def test_paging_with_offset_walks_back_past_5000(tmp_path):
    p = _numbered(tmp_path / "n.log", 12_000, pad=4)
    rows, info = lf.tail_page([p], lines=99999)
    assert len(rows) == lf.MAX_PAGE_LINES and info["more"]
    assert rows[-1].startswith("00011999 ") and rows[0].startswith("00007000 ")
    assert info["next_offset"] == 5000
    rows2, info2 = lf.tail_page([p], lines=5000, offset=info["next_offset"])
    assert rows2[-1].startswith("00006999 ") and rows2[0].startswith("00002000 ")
    rows3, info3 = lf.tail_page([p], lines=5000, offset=info2["next_offset"])
    assert len(rows3) == 2000 and rows3[0].startswith("00000000 ")
    assert info3["more"] is False and info3["truncated"] is False


def test_grep_paging_and_scan_limit(tmp_path):
    p = _numbered(tmp_path / "g.log", 70_000, pad=60)      # ~5 MB
    rows, info = lf.tail_page([p], grep="reloading", lines=10)
    assert len(rows) == 10 and all("Reloading" in r for r in rows)
    assert rows[-1].startswith("00069999 ") or rows[-1].startswith("00069993 ")
    # a 1 MB scan cap stops well before the start and says so
    rows, info = lf.tail_page([p], grep="no such text", lines=10,
                              scan_bytes=1024 * 1024)
    assert rows == [] and info["truncated"] and info["more"]
    assert 1024 * 1024 <= info["scanned_bytes"] <= 1024 * 1024 + 1
    assert "scan_mb=" in lf.page_note(info, 0)


def test_multi_file_headers_and_order(tmp_path):
    a = tmp_path / "a.log"
    b = tmp_path / "b.log"
    a.write_text("a1\na2\n", encoding="utf-8")
    b.write_text("b1\n", encoding="utf-8")
    assert lf.read_rows([a, b]) == ["# ===== a.log =====", "a1", "a2",
                                    "# ===== b.log =====", "b1"]
    rows, info = lf.tail_page([a, b], lines=2)
    assert rows == ["a2", "# ===== b.log =====", "b1"] and info["next_offset"] == 2


# --- endpoints -----------------------------------------------------------------

def _cfg(tmp_path, **kw):
    kw.setdefault("nina_logs_dir", str(tmp_path / "nina"))
    return PhotonScriptConfig(_env_file=None,
                              nina_base_url="http://localhost:1888/v2/api",
                              piggyback_nina_base_url="http://localhost:1889/v2/api",
                              **kw)


@pytest.fixture
def nina(tmp_path, monkeypatch):
    d = tmp_path / "nina"
    d.mkdir()
    p = d / "20261005-081746-3.2.0.9001-202610.log"
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("2026-10-05T08:17:46|INFO|API.cs|Start|1|starting web server, "
                 "listening at 0.0.0.0:1888\n")
        for i in range(8000):
            fh.write(f"line {i:05d}\n")
    t = datetime(2026, 10, 8, 2, 0).timestamp()
    os.utime(p, (t, t))
    monkeypatch.setattr(app, "_config", _cfg(tmp_path))
    return p


async def test_nina_log_offset_header_and_download(nina, monkeypatch):
    out = await triage.api_nina_log(lines=5000)
    head, body = out.split("\n", 1)
    assert "last 5000 lines" in head and "older: offset=5000" in head
    assert body.splitlines()[-1] == "line 07999"
    out2 = await triage.api_nina_log(lines=5000, offset=5000)
    assert out2.splitlines()[-1] == "line 02999"
    assert "offset 5000" in out2.splitlines()[0]
    resp = await triage.api_nina_log(download=1)
    assert isinstance(resp, FileResponse) and Path(resp.path) == nina
    many = await triage.api_nina_log(date="2026-10-07", download=1)
    assert isinstance(many, FileResponse)    # one log covers the night
    monkeypatch.setattr(triage, "DOWNLOAD_MAX_BYTES", 1000)
    resp = await triage.api_nina_log(download=1)
    assert isinstance(resp, PlainTextResponse) and resp.status_code == 413
    assert b"download cap" in resp.body


async def test_download_refuses_several_files(tmp_path):
    out = triage._download([tmp_path / "a", tmp_path / "b"])
    assert "takes one file" in out


async def test_ascom_log_tails_from_the_end(tmp_path, monkeypatch):
    d = tmp_path / "ascom" / "Logs 2026-10-08"
    d.mkdir(parents=True)
    (d / "ASCOM.SafetyMonitor.1234.txt").write_text(
        "".join(f"{i} {'Exception' if i % 100 == 0 else 'ok'}\n" for i in range(3000)),
        encoding="utf-8")
    monkeypatch.setattr(app, "_config",
                        _cfg(tmp_path, ascom_logs_dir=str(tmp_path / "ascom")))
    out = await triage.api_ascom_log(grep="exception", lines=5)
    assert out.splitlines()[1:] == ["2500 Exception", "2600 Exception",
                                    "2700 Exception", "2800 Exception",
                                    "2900 Exception"]


async def test_log_endpoints_do_not_block_the_event_loop(nina, monkeypatch):
    """A slow file read runs in a worker thread: the loop keeps ticking."""
    real = triage._nina_log

    def slow(*a):
        time.sleep(0.6)            # a synchronous stall, like a huge read
        return real(*a)

    monkeypatch.setattr(triage, "_nina_log", slow)
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    t = asyncio.create_task(ticker())
    t0 = time.monotonic()
    out = await triage.api_nina_log(lines=3)
    t.cancel()
    assert time.monotonic() - t0 >= 0.6
    assert "line 07999" in out
    assert ticks >= 10
