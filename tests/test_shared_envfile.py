"""Tests for .env read/update round-trip."""

from pathlib import Path

from photonscript.shared.envfile import read_env, update_env


def test_update_preserves_comments_and_order(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text("# Observatory\nPS_OBSERVATORY_NAME=\"Old Name\"\nPS_PHD2_PORT=4400\n")
    update_env(p, {"PS_OBSERVATORY_NAME": "AARO Pier 3", "PS_NEW_KEY": "hello"})
    text = p.read_text()
    assert text.startswith("# Observatory\n")
    assert 'PS_OBSERVATORY_NAME="AARO Pier 3"' in text
    assert "PS_NEW_KEY=hello" in text
    vals = read_env(p)
    assert vals["PS_OBSERVATORY_NAME"] == "AARO Pier 3"
    assert vals["PS_PHD2_PORT"] == "4400"


def test_read_strips_quotes(tmp_path: Path):
    p = tmp_path / ".env"
    p.write_text('KEY1="quoted value"\nKEY2=bare\n# comment\n')
    vals = read_env(p)
    assert vals == {"KEY1": "quoted value", "KEY2": "bare"}


def test_update_creates_file(tmp_path: Path):
    p = tmp_path / ".env"
    update_env(p, {"PS_DEFAULT_GAIN": "200"})
    assert read_env(p)["PS_DEFAULT_GAIN"] == "200"
