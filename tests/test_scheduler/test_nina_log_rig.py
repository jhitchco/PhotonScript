"""/api/nina/log rig routing — tail NINA #2 (OSC) as well as NINA #1.

Both NINAs run as one Windows user and share a Logs folder, so each rig's log
is identified by the Advanced API port its startup line names."""
import os
import time

import pytest

from photonscript.shared.config import PhotonScriptConfig
import photonscript.scheduler.app as app


def _log(dir_, name, port, body, age_s=0):
    p = dir_ / name
    p.write_text(f"2026-09-26T16:14:49|INFO|API.cs|Start|134|starting web server, "
                 f"listening at 0.0.0.0:{port}\n{body}", encoding="utf-8")
    t = time.time() - age_s
    os.utime(p, (t, t))
    return p


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, nina_logs_dir=str(tmp_path),
                              nina_base_url="http://localhost:1888/v2/api",
                              piggyback_nina_base_url="http://localhost:1889/v2/api",
                              **kw)


@pytest.mark.asyncio
async def test_shared_dir_picks_each_rigs_own_log(tmp_path, monkeypatch):
    _log(tmp_path, "20260926-161000-3.2.0.19640-202609.log", 1888, "rc16 line\n", age_s=60)
    _log(tmp_path, "20260926-161449-3.2.0.16836-202609.log", 1889,
         "hello OSC\nstar lost near dawn\n")                     # newest = NINA #2
    monkeypatch.setattr(app, "_config", _cfg(tmp_path))
    rc = await app.api_nina_log()
    assert "[rc16]" in rc and "rc16 line" in rc and "19640" in rc
    pig = await app.api_nina_log(rig="piggyback", grep="star lost")
    assert "[piggyback]" in pig and "16836" in pig
    assert "star lost near dawn" in pig and "hello OSC" not in pig


@pytest.mark.asyncio
async def test_piggyback_log_missing_says_so(tmp_path, monkeypatch):
    _log(tmp_path, "a.log", 1888, "rc16 only\n")
    monkeypatch.setattr(app, "_config", _cfg(tmp_path))
    out = await app.api_nina_log(rig="piggyback")
    assert ":1889" in out and "rc16 only" not in out


@pytest.mark.asyncio
async def test_dedicated_piggyback_dir_tails_newest(tmp_path, monkeypatch):
    pdir = tmp_path / "osc"
    pdir.mkdir()
    (pdir / "20260926-nina.log").write_text("hello OSC\nstar lost\n", encoding="utf-8")
    monkeypatch.setattr(app, "_config", _cfg(tmp_path, piggyback_nina_logs_dir=str(pdir)))
    out = await app.api_nina_log(rig="piggyback", grep="star lost")
    assert "[piggyback]" in out and "star lost" in out


@pytest.mark.asyncio
async def test_rc16_falls_back_to_newest_without_port_line(tmp_path, monkeypatch):
    (tmp_path / "20260926-rc16.log").write_text("rc16 line\n", encoding="utf-8")
    monkeypatch.setattr(app, "_config", _cfg(tmp_path))
    out = await app.api_nina_log()
    assert "[rc16]" in out and "rc16 line" in out
