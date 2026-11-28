"""2026-10-16: "ValueError: Out of range float values are not JSON
compliant: nan" once a minute in the scope log (Starlette JSONResponse):
a disconnected mount's alt / az reached the dashboard endpoints as NaN
(telescope_state_update carried 'mount_alt': 'NaN').

Pinned: the app's response class turns NaN / Infinity into null; the state
model reads NaN / "NaN" as None; the agent leaves a disconnected mount's
RA / Dec / alt / az None (not 0.0 / NaN).
"""
import asyncio
import json
import math

import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.json_safe import SafeJSONResponse, sanitize
from photonscript.shared.models import AgentMessage, AgentRole, TelescopeState


def _cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"))


def test_sanitize_and_render():
    raw = {"a": float("nan"), "b": [1.0, float("inf"), {"c": -math.inf}],
           "d": (2.5, "NaN")}
    assert sanitize(raw) == {"a": None, "b": [1.0, None, {"c": None}],
                             "d": [2.5, "NaN"]}
    body = json.loads(SafeJSONResponse(raw).body)
    assert body["a"] is None and body["b"][1] is None


def test_state_model_reads_nan_as_unknown():
    st = TelescopeState(mount_alt="NaN", mount_az=float("nan"),
                        mount_ra=float("inf"), mount_dec=12.5,
                        camera_temp_c="NaN")
    assert (st.mount_alt, st.mount_az, st.mount_ra) == (None, None, None)
    assert st.mount_dec == 12.5 and st.camera_temp_c is None


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    monkeypatch.setattr(app, "_config", _cfg(tmp_path))
    monkeypatch.setattr(app, "_syncthing_pending_names", lambda: None)
    monkeypatch.setattr(app, "_rig_states", {})
    monkeypatch.setattr(app, "_telescope_state", TelescopeState())
    return TestClient(app.app, raise_server_exceptions=False)


def test_dashboard_endpoints_answer_200_with_a_nan_state(client):
    from photonscript.scheduler import app
    # the agent's message, as the scope sent it
    msg = AgentMessage(sender=AgentRole.TELESCOPE, recipient=AgentRole.SCHEDULER,
                       msg_type="telescope_state_update",
                       payload={"rig": "rc16", "mount_alt": "NaN",
                                "mount_az": "NaN", "mount_connected": False})
    asyncio.run(app.on_agent_message(msg))
    assert app._telescope_state.mount_alt is None
    # and a state that bypassed validation still renders
    bad = TelescopeState.model_construct(**{**TelescopeState().model_dump(),
                                            "mount_alt": float("nan"),
                                            "mount_az": float("inf")})
    app._telescope_state = bad
    app._rig_states["rc16"] = bad
    for path in ("/api/status", "/api/telescope/state", "/api/night/where"):
        r = client.get(path)
        assert r.status_code == 200, (path, r.text[:300])
        assert "NaN" not in r.text and "Infinity" not in r.text
    assert client.get("/api/status").json()["telescope"]["mount_alt"] is None
