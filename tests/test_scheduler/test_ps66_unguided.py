"""PS-66 part 1: the unguided (TPoint + ProTrack) mode.

1. "unguided" name with the old "encoders" accepted as an alias.
"""
import json

import pytest

from photonscript.scheduler import armer as armer_mod
from photonscript.scheduler.armer import Armer, norm_guiding_mode
from photonscript.shared.config import PhotonScriptConfig


def _cfg(tmp_path=None, **kw):
    if tmp_path is not None:
        kw.setdefault("data_dir", tmp_path / "data")
    return PhotonScriptConfig(_env_file=None, **kw)


async def _noop(*a, **k):
    return None


# ---- 1. mode name and alias --------------------------------------------------

def test_norm_guiding_mode_aliases():
    assert norm_guiding_mode("encoders") == "unguided"
    assert norm_guiding_mode("Unguided") == "unguided"
    assert norm_guiding_mode(" guided ") == "guided"
    for junk in (None, "", "phd2", "default", 1, True):
        assert norm_guiding_mode(junk) is None


def test_encoders_override_is_unguided_and_status_says_unguided(tmp_path):
    a = Armer(_cfg(tmp_path, guided_default=True, noon_arm_guided=False))
    a.guiding_override = "encoders"
    assert a._use_guiding() is False
    st = a.status()
    assert st["guiding"] == "unguided"
    assert st["noon_arm"]["guiding"] == "unguided"


def test_persisted_encoders_restores_as_unguided(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, connect_all_on_arm=False)
    p = tmp_path / "data" / "armer_state.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"state": "ARMED", "plan": {"night_of": "2026-10-03"},
                             "guiding_override": "encoders"}))
    a = Armer(cfg)
    created = []
    monkeypatch.setattr(armer_mod.asyncio, "create_task",
                        lambda coro: (created.append(coro), coro.close()))
    assert a.restore() is True
    assert a.guiding_override == "unguided" and a._use_guiding() is False


async def test_arm_accepts_encoders_alias(tmp_path, monkeypatch):
    a = Armer(_cfg(tmp_path, connect_all_on_arm=False,
                   cooler_off_until_precool=False))
    monkeypatch.setattr(armer_mod, "notify", _noop)
    monkeypatch.setattr("photonscript.scheduler.night_plan.build_night_plan",
                        lambda cfg: {"night_of": "2026-10-03", "targets": ["Heart"],
                                     "preconfig_utc": "2026-10-04T00:30:00Z",
                                     "dark_hours": 8.0})
    monkeypatch.setattr(a, "_run", _noop)
    st = await a.arm("encoders")
    assert a.guiding_override == "unguided"
    assert st["guiding"] == "unguided"
    assert "unguided (TPoint + ProTrack)" in st["detail"]
    assert a._audit_task is None                      # no PS-89 audit unguided


def test_api_arm_normalizes_and_rejects_junk(monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app

    seen = []

    class _FakeArmer:
        async def arm(self, guiding=None):
            seen.append(guiding)
            return {"state": "ARMED", "guiding": guiding}

        async def disarm(self):
            return {"state": "DISARMED"}

    monkeypatch.setattr(app, "_armer", _FakeArmer())
    client = TestClient(app.app)
    r = client.post("/api/arm", json={"armed": True, "guiding": "warp drive"})
    assert r.status_code == 400 and "unknown guiding mode" in r.json()["detail"]
    assert seen == []
    assert client.post("/api/arm", json={"armed": True,
                                         "guiding": "encoders"}).status_code == 200
    assert client.post("/api/arm", json={"armed": True}).status_code == 200
    assert client.post("/api/arm", json={"armed": True,
                                         "guiding": "guided"}).status_code == 200
    assert seen == ["unguided", None, "guided"]
