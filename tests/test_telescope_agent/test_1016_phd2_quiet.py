"""2026-10-16: "WARNING Cannot connect to PHD2 at localhost:4400: [WinError
1225] ... (retrying quietly)" while nothing needed PHD2. DISARMED /
COMPLETE or an unguided night: DEBUG, once per state. Guided night (or no
readable armer state): WARNING as before."""
import asyncio
import json
import logging

from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent.phd2_client import PHD2Client

LOG = "photonscript.telescope_agent.phd2_client"


def _fail(client, n=1):
    for _ in range(n):
        assert asyncio.run(client.connect()) is False


def _client(reason):
    c = PHD2Client("127.0.0.1", 1)    # nothing listens on port 1
    c.quiet_reason_fn = lambda: reason[0]
    return c


def _lines(caplog, level):
    return [r for r in caplog.records if r.name == LOG and r.levelno == level
            and "Cannot connect to PHD2" in r.getMessage()]


def test_disarmed_logs_debug_once_per_state(caplog):
    reason = ["armer DISARMED"]
    c = _client(reason)
    with caplog.at_level(logging.DEBUG, LOG):
        _fail(c, 3)
        reason[0] = "armer COMPLETE"
        _fail(c, 2)
    assert not _lines(caplog, logging.WARNING)
    dbg = _lines(caplog, logging.DEBUG)
    assert len(dbg) == 2
    assert "armer DISARMED" in dbg[0].getMessage()
    assert "armer COMPLETE" in dbg[1].getMessage()


def test_guided_night_keeps_the_warning(caplog):
    c = _client([None])
    with caplog.at_level(logging.DEBUG, LOG):
        _fail(c)
    assert len(_lines(caplog, logging.WARNING)) == 1


def test_agent_reason_follows_the_armer(tmp_path, monkeypatch):
    import sys
    from photonscript.telescope_agent.agent import TelescopeAgent
    app_mod = sys.modules.get("photonscript.scheduler.app")
    if app_mod is not None:   # read the state file, not an in-process armer
        monkeypatch.setattr(app_mod, "_armer", None, raising=False)
    cfg = PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                             image_watch_dir=str(tmp_path),
                             nina_logs_dir=str(tmp_path))
    a = TelescopeAgent.__new__(TelescopeAgent)
    a.config = cfg
    assert a._phd2_quiet_reason() is None          # no state file: warn
    p = tmp_path / "armer_state.json"
    p.write_text(json.dumps({"state": "DISARMED"}))
    assert a._phd2_quiet_reason() == "armer DISARMED"
    p.write_text(json.dumps({"state": "RUNNING", "guiding_override": "unguided"}))
    assert a._phd2_quiet_reason() == "unguided night"
    p.write_text(json.dumps({"state": "RUNNING", "guiding_override": "guided"}))
    assert a._phd2_quiet_reason() is None
