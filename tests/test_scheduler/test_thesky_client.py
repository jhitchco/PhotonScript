"""Unit tests for the experimental TheSky TCP client (pure framing/parsing;
no socket)."""

import pytest

from photonscript.telescope_agent.thesky_client import (
    TheSkyClient, TheSkyError, client_from_config)


def test_frame_wraps_markers():
    f = TheSkyClient._frame("var Out; Out='x';")
    assert f.startswith("/* Java Script */")
    assert "Socket Start Packet" in f
    assert "Socket End Packet" in f
    assert "var Out; Out='x';" in f


def test_parse_ok_values():
    assert TheSkyClient._parse_reply("32.5|No error. Error = 0.") == "32.5"
    assert TheSkyClient._parse_reply("ok|Error = 0.") == "ok"
    # no error half at all -> return as-is
    assert TheSkyClient._parse_reply("plain") == "plain"


def test_parse_error_raises():
    with pytest.raises(TheSkyError):
        TheSkyClient._parse_reply("|TypeError: undefined. Error = 415.")


def test_client_from_config():
    class C:
        thesky_tcp_host = "scope-pc"
        thesky_tcp_port = 3040
    cl = client_from_config(C())
    assert cl.host == "scope-pc"
    assert cl.port == 3040
