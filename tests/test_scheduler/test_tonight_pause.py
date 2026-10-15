"""Tonight-only goal pause (Notion P2, 2026-10-15): PATCH /api/projects2/{id}
{"paused_tonight": true | false}, the dawn shutdown restore, a night that
ended without one (stale restore), persistence across a restart, the morning
report lines and the dashboard / target page toggles."""
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from photonscript.scheduler import tonight_pause as tp
from photonscript.scheduler.project_store import ProjectStore
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget

HEART = "Heart Nebula"
NIGHT = "2026-10-15"


def _cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"),
                              observatory_tz="America/Denver")


def _store(cfg):
    store = ProjectStore(cfg)
    p = next((x for x in store.projects.values() if x.target.name == HEART), None)
    if p is None:
        p = store.add_from_target(CelestialTarget(
            name=HEART, catalog_id="IC 1805", ra_hours=2.55, dec_degrees=61.45,
            object_type="emission nebula"), budget_hours=6)
    store.update(p.id, active=True)
    return store, p


def _events(cfg, night=NIGHT):
    p = Path(cfg.data_dir) / "runs" / f"{night}_events.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def test_pause_then_dawn_restore_logs_and_morning_lines(tmp_path):
    cfg = _cfg(tmp_path)
    store, p = _store(cfg)
    ok, txt = tp.pause(cfg, store, p.id, NIGHT)
    assert ok and "paused for tonight" in txt
    assert store.projects[p.id].active is False
    assert tp.paused_ids(cfg, NIGHT) == {p.id}
    # a restart mid-night: the store and the pause file both persist
    again = ProjectStore(cfg)
    assert again.projects[p.id].active is False
    assert tp.paused_ids(cfg, NIGHT) == {p.id}
    assert tp.pause(cfg, store, p.id, NIGHT)[0]          # idempotent
    assert tp.morning_lines(cfg, NIGHT) == [
        f"Paused for tonight only, NOT restored yet: {HEART}"]
    step = tp.dawn_restore(cfg, NIGHT, store=store)
    assert step == f"tonight-only pauses restored: {HEART}: active again"
    assert store.projects[p.id].active is True
    assert tp.paused_ids(cfg, NIGHT) == set()
    assert tp.dawn_restore(cfg, NIGHT, store=store) is None   # once
    ev = [e for e in _events(cfg) if e["kind"] == "tonight_pause"]
    assert [e["value"] for e in ev] == ["pause", "restore"] and ev[1]["by"] == "dawn"
    assert tp.morning_lines(cfg, NIGHT) == [
        f"Paused for tonight only, back at dawn: {HEART}"]


def test_unpause_by_hand_and_never_wakes_an_inactive_goal(tmp_path):
    cfg = _cfg(tmp_path)
    store, p = _store(cfg)
    assert tp.unpause(cfg, store, p.id, NIGHT)[0] is False   # not paused
    tp.pause(cfg, store, p.id, NIGHT)
    ok, txt = tp.unpause(cfg, store, p.id, NIGHT)
    assert ok and store.projects[p.id].active is True
    assert tp.morning_lines(cfg, NIGHT) == [
        f"Paused for tonight, un-paused by hand: {HEART}"]
    # a goal inactive for good is not paused, so dawn never sets it active
    store.update(p.id, active=False)
    ok, txt = tp.pause(cfg, store, p.id, "2026-10-16")
    assert not ok and "inactive already" in txt
    assert tp.dawn_restore(cfg, "2026-10-16", store=store) is None
    assert store.projects[p.id].active is False


def test_hand_resumed_goal_is_left_alone_and_stale_night_restored(tmp_path):
    cfg = _cfg(tmp_path)
    store, p = _store(cfg)
    tp.pause(cfg, store, p.id, "2026-10-14")
    # no dawn shutdown that night (disarmed): the next night restores it
    out = tp.restore_stale(cfg, NIGHT, store)
    assert out == [f"{HEART}: active again"] and store.projects[p.id].active
    assert tp.restore_stale(cfg, NIGHT, store) == []
    tp.pause(cfg, store, p.id, NIGHT)
    store.update(p.id, active=True)                  # by hand, mid-night
    assert tp.restore(cfg, NIGHT, store) == [f"{HEART}: already active again"]


def test_current_night_follows_the_armed_plan(tmp_path):
    cfg = _cfg(tmp_path)
    assert tp.current_night(cfg, SimpleNamespace(plan={"night_of": "2026-10-14"})) == "2026-10-14"
    # 03:00 local on the 16th belongs to the evening of the 15th
    assert tp.current_night(cfg, None, datetime(2026, 10, 16, 9, 0)) == NIGHT


@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    store, p = _store(cfg)
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(app, "_store", store)
    monkeypatch.setattr(app, "_armer", SimpleNamespace(plan={"night_of": NIGHT}))
    return cfg, store, p, TestClient(app.app)


def test_patch_paused_tonight_round_trip(api):
    cfg, store, p, client = api
    r = client.patch(f"/api/projects2/{p.id}", json={"paused_tonight": True})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["paused_tonight"] is True and d["active"] is False
    assert "paused for tonight" in d["pause_note"]
    row = next(x for x in client.get("/api/projects2").json() if x["id"] == p.id)
    assert row["paused_tonight"] is True
    det = client.get("/api/targets/detail", params={"name": HEART}).json()
    assert det["paused_tonight"] is True
    r = client.patch(f"/api/projects2/{p.id}", json={"paused_tonight": False})
    assert r.status_code == 200 and r.json()["active"] is True
    assert r.json()["paused_tonight"] is False
    r = client.patch(f"/api/projects2/{p.id}", json={"paused_tonight": False})
    assert r.status_code == 409 and "not paused" in r.json()["detail"]
    # with another field in the same body: both apply
    r = client.patch(f"/api/projects2/{p.id}",
                     json={"paused_tonight": True, "priority": 77})
    assert r.status_code == 200 and r.json()["priority"] == 77
    assert r.json()["paused_tonight"] is True


def test_morning_report_and_armer_hook(tmp_path):
    from photonscript.scheduler import morning_report as mr
    card = {"date": NIGHT, "rigs": [], "paused_tonight": [
        f"Paused for tonight only, back at dawn: {HEART}"]}
    assert f"Paused for tonight only, back at dawn: {HEART}" in mr.card_lines(card)
    src = (Path(mr.__file__).parent / "armer.py").read_text(encoding="utf-8")
    assert "tonight_pause import dawn_restore" in src


def test_dashboard_and_target_page_toggles():
    root = Path(__file__).resolve().parents[2] / "photonscript" / "scheduler" / "templates"
    dash = (root / "dashboard.html").read_text(encoding="utf-8")
    assert "{paused_tonight: true}" in dash and "{paused_tonight: false}" in dash
    page = (root / "target.html").read_text(encoding="utf-8")
    assert "function pauseTonightLink" in page and "paused_tonight: !D.paused_tonight" in page
