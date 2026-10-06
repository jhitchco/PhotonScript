"""PS-33: integrator ledger on the scheduler. Schema (0.1 upgrade of the
hand-written M31_OSC3 ledger), POST /api/integrations (store, idempotent
per run, version bump, 404 / 422), review kept on a re-post, asks change
status only (projects.json untouched), the summary's "new data since" and
the PS-31 candidates facts."""

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photonscript.scheduler import app
from photonscript.scheduler import integrations as integ
from photonscript.scheduler.project_store import ProjectStore
from photonscript.shared import ledger as L
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget

FIX = Path(__file__).parent / "fixtures" / "ledger" / "M31_OSC3_v01.json"
M31 = CelestialTarget(name="Andromeda Galaxy", catalog_id="M 31",
                      ra_hours=0.712, dec_degrees=41.27, object_type="galaxy")


@pytest.fixture(autouse=True)
def _no_seed(tmp_path, monkeypatch):
    monkeypatch.setattr(ProjectStore, "SEED_PATH", tmp_path / "no_seed.json")


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    monkeypatch.setattr(app, "_store", None)
    monkeypatch.setattr(app, "_projects", {})
    monkeypatch.setattr(app, "_dashboard_cache", {})
    store = app.get_store()
    p = store.add_from_target(M31, budget_hours=6.0)
    store.update(p.id, osc_hours=6.0, drop_rc16=True, driving_rig="piggyback")
    c = TestClient(app.app)
    c.cfg, c.pid = cfg, p.id
    return c


def _ledger(run="M31_piggyback_20261006-2200", version=1, files=("a.fits", "b.fits"),
            campaign="M31", **kw):
    return {"schema": L.SCHEMA, "campaign": campaign, "rig": "piggyback", "run": run,
            "version": version, "created_at": "2026-10-06T22:00:00Z",
            "machine": {"acquisition": {"hours": 9.4, "nights": ["2026-10-01"]},
                        "subs": [{"file": f, "night": "2026-10-01", "filter": "OSC",
                                  "exp_s": 120.0, "used": True} for f in files],
                        "integration": {"ok": True},
                        "astrobin": {"packet": r"D:\Staging\x\astrobin\M31_packet.md"},
                        "run_dir": r"D:\Staging\x"},
            **kw}


# --- schema ---------------------------------------------------------------------

def test_v01_hand_written_ledger_upgrades_without_loss():
    raw = json.loads(FIX.read_text())
    led = L.parse(raw)
    assert led.schema_ == L.SCHEMA and led.version == 3 and led.rig == "piggyback"
    assert led.campaign == "M31" and led.run == "M31_OSC3"
    assert led.created_at == "2026-09-26T17:58:00Z"
    m = led.machine
    assert m["acquisition"]["subs_by_filter"]["OSC"]["integrated"] == 39
    assert m["acquisition"]["rejects"] == {"split_pointing": 47, "other_pointing": 4,
                                           "dropped_at_registration": 2}
    assert m["calibration"]["status"] == "uncalibrated"
    assert m["rig_detail"]["name"] == "Piggy-600" and "metrics" in m and "finish" in m
    assert [a.type for a in led.review.asks] == ["need_calibration", "more_hours",
                                                 "reframe", "fix_blocker"]
    assert all(a.id and a.status == "open" for a in led.review.asks)
    assert led.review.asks[0].what == ["flats", "darks", "bias"]
    assert led.review.asks[1].model_extra["why"] == "1.3 h of 6 h goal"
    assert "core and M32 recovered" in led.review.notes and led.review.verdict == "needs_more_data"
    assert led.publish["astrobin"]["status"] == "published"
    again = L.parse(led.dump())                     # 0.2 round trip
    assert again.dump() == led.dump()


def test_unknown_ask_type_and_rig_are_rejected():
    with pytest.raises(Exception):
        L.parse(_ledger(review={"asks": [{"type": "buy_a_new_scope"}]}))
    with pytest.raises(Exception):
        L.parse({**_ledger(), "rig": "dobsonian"})


def test_headline():
    led = L.parse(_ledger(version=2))
    assert L.headline(led) == "Integrated 9.4 h on 2026-10-06 (v2), packet ready"
    led.machine["integration"] = {"ok": None}
    assert "not integrated" in L.headline(led)


# --- POST / GET -------------------------------------------------------------------

def test_post_stores_file_and_is_idempotent_per_run(client):
    r = client.post("/api/integrations", json=_ledger())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["project_id"] == client.pid and body["version"] == 1
    assert body["headline"].startswith("Integrated 9.4 h")
    d = Path(client.cfg.data_dir) / "ledgers" / client.pid
    assert [f.name for f in d.glob("*.json")] == ["v001.json"]
    stored = json.loads((d / "v001.json").read_text())
    assert stored["reported"] is True and stored["machine"]["project_id"] == client.pid
    r2 = client.post("/api/integrations", json=_ledger())
    assert r2.json()["version"] == 1
    assert [f.name for f in d.glob("*.json")] == ["v001.json"]


def test_a_new_run_with_a_taken_version_gets_the_next(client):
    client.post("/api/integrations", json=_ledger())
    r = client.post("/api/integrations", json=_ledger(run="M31_piggyback_20261008-2200"))
    assert r.json()["version"] == 2
    r = client.post("/api/integrations", json=_ledger(run="third", version=0))
    assert r.json()["version"] == 3
    got = client.get("/api/integrations", params={"name": "Andromeda Galaxy"}).json()
    assert [x["version"] for x in got["ledgers"]] == [3, 2, 1]


def test_alias_resolves_and_unknown_campaign_is_404(client):
    assert client.post("/api/integrations", json=_ledger(campaign="M 31")).status_code == 200
    r = client.post("/api/integrations", json=_ledger(campaign="Crescent Nebula"))
    assert r.status_code == 404 and not r.json()["ok"]


def test_bad_payload_is_422(client):
    assert client.post("/api/integrations", json={"campaign": "M31"}).status_code == 422
    bad = _ledger(review={"asks": [{"type": "nope"}]})
    assert client.post("/api/integrations", json=bad).status_code == 422


def test_repost_with_empty_review_keeps_the_stored_review(client):
    first = _ledger(review={"verdict": "keep", "notes": "nice core",
                            "asks": [{"id": "a1", "type": "more_hours", "rig": "piggyback",
                                      "filter": "OSC", "hours": 2.0}]})
    client.post("/api/integrations", json=first)
    assert client.post("/api/integrations/asks/a1", json={"decision": "approve"}).json()["ok"]
    client.post("/api/integrations", json=_ledger())            # machine half only
    led = client.get("/api/integrations", params={"name": "M31"}).json()["ledgers"][0]
    assert led["review"]["verdict"] == "keep"
    assert led["review"]["asks"][0]["status"] == "approved"
    # a re-post WITH the review keeps the decided status of a known ask
    client.post("/api/integrations", json=first)
    led = client.get("/api/integrations", params={"name": "M31"}).json()["ledgers"][0]
    assert led["review"]["asks"][0]["status"] == "approved"


def test_ask_decisions_never_touch_projects_json(client):
    client.post("/api/integrations", json=_ledger(review={"asks": [
        {"id": "x1", "type": "more_hours", "filter": "OSC", "hours": 3.0},
        {"id": "x2", "type": "rest"}]}))
    pj = Path(client.cfg.data_dir) / "projects.json"
    before = hashlib.sha256(pj.read_bytes()).hexdigest()
    assert client.post("/api/integrations/asks/x1", json={"decision": "approve"}).json()["ask"][
        "status"] == "approved"
    assert client.post("/api/integrations/asks/x2", json={"decision": "decline"}).status_code == 200
    assert hashlib.sha256(pj.read_bytes()).hexdigest() == before
    assert client.post("/api/integrations/asks/zz", json={"decision": "approve"}).status_code == 404
    assert client.post("/api/integrations/asks/x1", json={"decision": "maybe"}).status_code == 422


# --- summary + candidates ---------------------------------------------------------

def _rows(files, exp=120.0, date="2026-10-02"):
    return [{"file": f, "exp_s": exp, "date": date} for f in files]


def test_new_data_since_counts_approved_subs_not_in_the_ledger():
    led = L.parse(_ledger(files=("a.fits", "b.fits")))
    rows = _rows(["a.fits", "b.fits", "c.fits", "d.fits"], exp=1800.0)
    assert integ.new_data_s(led, rows) == 3600.0
    assert integ.new_data_s(None, rows) == 4 * 1800.0
    led.machine["options"] = {"since": "2026-10-05"}            # --since run
    assert integ.new_data_s(led, rows) == 0.0


def test_summary_and_candidates(client, monkeypatch):
    monkeypatch.setattr(integ, "_approved_rows", lambda cfg, projects, name, rig:
                        _rows(["a.fits", "b.fits", "c.fits"], exp=1800.0) if rig == "piggyback" else [])
    client.post("/api/integrations", json=_ledger(version=2))
    s = client.get("/api/integrations/summary").json()["integrations"]
    assert len(s) == 1 and s[0]["rig"] == "piggyback" and s[0]["new_data_h"] == 0.5
    assert s[0]["latest"]["headline"] == "Integrated 9.4 h on 2026-10-06 (v2), packet ready"
    assert s[0]["latest"]["packet"].endswith("M31_packet.md")
    c = client.get("/api/integrations/candidates", params={"calibration": False}).json()
    assert c["thresholds"] == {"rigs": ["piggyback"], "new_data_h": 1.0, "first_h": 0.0,
                               "min_interval_h": 12.0, "require_calibration": False}
    row = next(x for x in c["candidates"] if x["rig"] == "piggyback")
    assert row["target"] == "Andromeda Galaxy" and row["approved_h"] == 1.5
    assert row["last"]["version"] == 2 and row["new_data_h"] == 0.5
    assert row["goal"]["hours_goal"] == 6.0


def test_candidates_with_calibration_reports_missing(client, monkeypatch):
    from photonscript.scheduler import readiness
    monkeypatch.setattr(integ, "_approved_rows", lambda *a: [])
    monkeypatch.setattr(readiness, "calibration_context", lambda cfg, rig="rc16", **k:
                        readiness.CalibrationContext(rig=rig, lib=Path(cfg.data_dir) / "Library"))
    monkeypatch.setattr(integ, "_owed_items", lambda cfg, rig, projects: ["Flats OSC: none"])
    c = client.get("/api/integrations/candidates").json()["candidates"]
    row = next(x for x in c if x["rig"] == "piggyback")
    assert row["last"] is None and row["calibration_owed"] == ["Flats OSC: none"]
    assert "bias" in row["readiness"]["calibration_missing"]


def test_summary_page_is_empty_without_ledgers(client):
    assert client.get("/api/integrations/summary").json() == {"integrations": []}
