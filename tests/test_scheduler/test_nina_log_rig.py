"""/api/nina/log rig routing — tail NINA #2 (OSC) as well as NINA #1."""
import pytest

from photonscript.shared.config import PhotonScriptConfig
import photonscript.scheduler.app as app


@pytest.mark.asyncio
async def test_nina_log_piggyback_unconfigured(monkeypatch):
    monkeypatch.setattr(app, "_config",
                        PhotonScriptConfig(_env_file=None,
                                           piggyback_nina_logs_dir=""))
    out = await app.api_nina_log(rig="piggyback")
    assert "not configured" in out


@pytest.mark.asyncio
async def test_nina_log_piggyback_tails_and_greps(tmp_path, monkeypatch):
    (tmp_path / "20260926-nina.log").write_text(
        "hello OSC\nstar lost near dawn\nGuideStep 1\n", encoding="utf-8")
    monkeypatch.setattr(app, "_config",
                        PhotonScriptConfig(_env_file=None,
                                           piggyback_nina_logs_dir=str(tmp_path)))
    out = await app.api_nina_log(rig="piggyback", grep="star lost")
    assert "[piggyback]" in out
    assert "star lost near dawn" in out
    assert "hello OSC" not in out       # grep filtered it out


@pytest.mark.asyncio
async def test_nina_log_defaults_to_rc16(tmp_path, monkeypatch):
    (tmp_path / "20260926-rc16.log").write_text("rc16 line\n", encoding="utf-8")
    monkeypatch.setattr(app, "_config",
                        PhotonScriptConfig(_env_file=None,
                                           nina_logs_dir=str(tmp_path)))
    out = await app.api_nina_log()          # rig defaults to rc16
    assert "[rc16]" in out and "rc16 line" in out
