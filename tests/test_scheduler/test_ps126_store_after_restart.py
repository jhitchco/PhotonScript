"""PS-126: after a restart every `_projects` reader sees the stored projects.

get_store() fills app._projects lazily. Before the fix, /api/tonight, the
sequence downloads and the PS-123 sideload (_tonight_sequence) read the empty
dict right after a restart and planned from the seasonal fallback
(2026-10-05: Andromeda / Pacman / Owl instead of Heart / Cat's Eye).
"""
import asyncio

import pytest

from photonscript.scheduler import app
from photonscript.scheduler.project_store import ProjectStore
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Armer:
    state = "DISARMED"

    def _use_guiding(self):
        return False

    def _unguided_dither(self):
        return False


@pytest.fixture
def restarted(tmp_path, monkeypatch):
    """A store on disk with Heart (active) and Owl (inactive), and the app in
    its just-restarted state: no _store, empty _projects."""
    cfg = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                             quality_eccentricity_max=0.60)
    monkeypatch.setattr(ProjectStore, "SEED_PATH", tmp_path / "no_seed.json")
    store = ProjectStore(cfg)
    heart = store.add_from_target(CelestialTarget(
        name="Heart Nebula", catalog_id="IC 1805", ra_hours=2.55, dec_degrees=61.5))
    owl = store.add_from_target(CelestialTarget(
        name="Owl Cluster", catalog_id="NGC 457", ra_hours=1.33, dec_degrees=58.3))
    store.update(owl.id, active=False)
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(app, "get_armer", lambda: _Armer())
    monkeypatch.setattr(app, "_store", None)
    monkeypatch.setattr(app, "_projects", {})

    def no_seasonal(*a, **k):
        raise AssertionError("planned from the seasonal fallback")
    monkeypatch.setattr(app, "get_seasonal_targets", no_seasonal)
    seen = []

    def plan(projects, config, now):
        seen.append(sorted(p.target.name for p in projects))
        return []
    monkeypatch.setattr(app, "plan_night_sequence", plan)
    return {"cfg": cfg, "seen": seen, "heart": heart, "owl": owl}


def test_api_tonight_uses_stored_projects(restarted):
    _run(app.api_tonight_plan())
    assert "Heart Nebula" in restarted["seen"][0]
    assert app._store is not None                       # loaded on demand


def test_tonight_sequence_uses_stored_active_projects(restarted):
    app._tonight_sequence(False)
    assert restarted["seen"] == [["Heart Nebula"]]      # active only


def test_sequence_xml_uses_stored_projects(restarted):
    _run(app.api_tonight_sequence_xml())
    assert "Heart Nebula" in restarted["seen"][0]


def test_status_ws_and_list_payloads_see_the_store(restarted):
    st = _run(app.api_status())
    assert st["total_projects"] == 2 and st["active_projects"] == 1
    names = {p["target"]["name"] for p in app._state_payload()["projects"].values()}
    assert names == {"Heart Nebula", "Owl Cluster"}
    listed = _run(app.api_list_projects())
    assert {p["target"]["name"] for p in listed} == names
    got = _run(app.api_get_project(restarted["heart"].id))
    assert got["target"]["name"] == "Heart Nebula"


def test_startup_hook_loads_the_store(restarted):
    _run(app._load_project_store())
    assert app._store is not None and len(app._projects) == 2
