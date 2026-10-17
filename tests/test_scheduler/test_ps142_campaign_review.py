"""PS-142: campaign review on the scheduler. Status states per goal + rig
(Acquiring / Ready to process / Processing / Processed (vN) / Published
(vN)), the processing notice from integrate-watch, the review panel data,
ask proposals (PATCH body + plan diff on a copy, projects.json untouched
until the browser PATCHes), the ready ping (new versions only, never
imports) and the dawn push line."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from photonscript.scheduler import app
from photonscript.scheduler import integrations as integ
from photonscript.scheduler.project_store import ProjectStore
from photonscript.shared import ledger as L
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget

M31 = CelestialTarget(name="Andromeda Galaxy", catalog_id="M 31",
                      ra_hours=0.712, dec_degrees=41.27, object_type="galaxy")
M27 = CelestialTarget(name="Dumbbell Nebula", catalog_id="M 27",
                      ra_hours=19.993, dec_degrees=22.72, object_type="planetary_nebula")
THR = {"rigs": ["piggyback"], "new_data_h": 1.0, "first_h": 0.0, "min_interval_h": 12.0,
       "require_calibration": False}


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
    monkeypatch.setattr(integ, "_approved_rows", lambda *a: [])
    pings = []

    async def _notify(config, message, title="PhotonScript", **kw):
        pings.append((title, message))
        return True
    import photonscript.shared.pushover as po
    monkeypatch.setattr(po, "notify", _notify)
    store = app.get_store()
    p = store.add_from_target(M31, budget_hours=6.0)
    store.update(p.id, osc_hours=6.0, drop_rc16=True, driving_rig="rc16")
    q = store.add_from_target(M27, budget_hours=4.0)
    c = TestClient(app.app)
    c.cfg, c.pid, c.qid, c.store, c.pings = cfg, p.id, q.id, store, pings
    return c


def _ledger(run="M31_piggyback_20261006-2200", version=1, campaign="M31", **kw):
    return {"schema": L.SCHEMA, "campaign": campaign, "rig": "piggyback", "run": run,
            "version": version, "created_at": "2026-10-06T22:00:00Z",
            "machine": {"acquisition": {"hours": 9.4},
                        "subs": [{"file": "a.fits", "exp_s": 120.0, "used": True}],
                        "integration": {"ok": True},
                        "astrobin": {"packet": r"D:\Staging\x\astrobin\M31_packet.md"}},
            **kw}


def _pj_hash(c):
    return hashlib.sha256((Path(c.cfg.data_dir) / "projects.json").read_bytes()).hexdigest()


# --- pure status rules -------------------------------------------------------------

def _cand(**kw):
    c = {"project_id": "p", "target": "Andromeda Galaxy", "rig": "piggyback",
         "goal": {"hours_goal": 6.0, "hours_done": 2.0, "pct": 33}, "approved_h": 2.0,
         "approved_subs": 60, "last": None, "new_data_h": 2.0, "readiness": {}}
    c.update(kw)
    return c


def _last(v=2, url=""):
    return {"version": v, "headline": f"Integrated 9.4 h on 2026-10-06 (v{v})",
            "astrobin_url": url}


def test_status_states():
    s = integ.status_of(_cand(), THR)
    assert s["state"] == "acquiring" and s["label"] == "Acquiring" and "33%" in s["detail"]
    s = integ.status_of(_cand(goal={"hours_goal": 6, "hours_done": 6, "pct": 100}), THR)
    assert s["state"] == "ready" and s["label"] == "Ready to process" and "goal met" in s["detail"]
    s = integ.status_of(_cand(last=_last(2), new_data_h=0.4), THR)
    assert s["state"] == "processed" and s["label"] == "Processed (v2)"
    s = integ.status_of(_cand(last=_last(2), new_data_h=1.5), THR)
    assert s["state"] == "ready" and "1.5 h new since v2" in s["detail"]
    s = integ.status_of(_cand(last=_last(3, "https://app.astrobin.com/i/stnh5q"), new_data_h=0.1),
                        THR, published={"version": 3, "url": "https://app.astrobin.com/i/stnh5q"})
    assert s["state"] == "published" and s["label"] == "Published (v3)"
    assert s["astrobin_url"].endswith("stnh5q")
    s = integ.status_of(_cand(last=_last(4), new_data_h=0.1), THR,
                        published={"version": 3, "url": "https://app.astrobin.com/i/x"})
    assert s["label"] == "Processed (v4)" and "v3 published" in s["detail"]
    proc = {"started_at": "2026-10-06T21:00:00Z", "reason": "goal met"}
    s = integ.status_of(_cand(last=_last(2), new_data_h=5), THR, processing=proc)
    assert s["state"] == "processing" and "goal met" in s["detail"]
    assert integ.status_of(_cand(approved_subs=0, goal={"pct": 100}), THR)["state"] == "acquiring"


def test_ready_waits_for_calibration_only_when_required():
    c = _cand(goal={"hours_goal": 6, "hours_done": 6, "pct": 100},
              readiness={"calibration_missing": ["flats OSC"]})
    s = integ.status_of(c, THR)
    assert s["state"] == "ready" and "calibration missing: flats OSC" in s["detail"]
    s = integ.status_of(c, {**THR, "require_calibration": True})
    assert s["state"] == "acquiring" and "waiting for calibration" in s["detail"]
    s = integ.status_of(_cand(approved_h=3.0), {**THR, "first_h": 2.5})
    assert s["state"] == "ready" and "first run at 2.5 h" in s["detail"]


# --- status API + processing notice ----------------------------------------------------

def test_status_endpoint_through_a_campaign(client):
    g = {x["project_id"]: x for x in client.get("/api/integrations/status").json()["goals"]}
    assert g[client.pid]["state"] == "acquiring" and g[client.qid]["label"] == "Acquiring"
    r = client.post("/api/integrations/processing",
                    json={"campaign": "M 31", "rig": "piggyback", "state": "start",
                          "reason": "goal met (6 of 6 h)"})
    assert r.status_code == 200 and r.json()["project_id"] == client.pid
    g = {x["project_id"]: x for x in client.get("/api/integrations/status").json()["goals"]}
    assert g[client.pid]["label"] == "Processing" and "goal met" in g[client.pid]["detail"]
    client.post("/api/integrations", json=_ledger(version=3))       # ends the notice
    g = {x["project_id"]: x for x in client.get("/api/integrations/status").json()["goals"]}
    assert g[client.pid]["label"] == "Processed (v3)"
    pub = {"astrobin": {"status": "published", "url": "https://app.astrobin.com/i/stnh5q"}}
    client.post("/api/integrations", json=_ledger(version=3, publish=pub))
    g = {x["project_id"]: x for x in client.get("/api/integrations/status").json()["goals"]}
    assert g[client.pid]["label"] == "Published (v3)"
    assert g[client.pid]["rigs"][0]["astrobin_url"].endswith("stnh5q")


def test_processing_notice_end_stale_and_errors(client, monkeypatch):
    body = {"campaign": "M31", "rig": "piggyback", "state": "start"}
    client.post("/api/integrations/processing", json=body)
    assert integ.processing_for(client.cfg, client.pid, "piggyback")
    later = datetime.now(timezone.utc) + timedelta(hours=integ.PROCESSING_STALE_H + 1)
    assert integ.processing_for(client.cfg, client.pid, "piggyback", now=later) is None
    client.post("/api/integrations/processing", json={**body, "state": "end"})
    assert integ.processing_for(client.cfg, client.pid, "piggyback") is None
    assert client.post("/api/integrations/processing",
                       json={**body, "campaign": "Crescent"}).status_code == 404
    assert client.post("/api/integrations/processing",
                       json={**body, "state": "maybe"}).status_code == 422
    assert client.post("/api/integrations/processing",
                       json={**body, "rig": "dob"}).status_code == 422


def test_paused_goal_with_a_ledger_still_has_a_status(client):
    client.post("/api/integrations", json=_ledger(version=2))
    client.store.update(client.pid, active=False)
    g = {x["project_id"]: x for x in client.get("/api/integrations/status").json()["goals"]}
    assert g[client.pid]["label"] == "Processed (v2)"


# --- review panel + proposals ---------------------------------------------------------

ASKS_V1 = [{"id": "mh1", "type": "more_hours", "filter": "OSC", "hours": 2.0,
            "why": "1.3 h of 6 h goal"},
           {"id": "rf1", "type": "reframe", "driving_rig": "piggyback"},
           {"id": "fb1", "type": "fix_blocker", "ticket": "Piggy-600 split pointing"}]


def test_review_panel_collects_verdict_notes_and_asks_of_every_version(client):
    client.post("/api/integrations", json=_ledger(
        run="M31_OSC3", version=3,
        review={"verdict": "needs_more_data", "notes": "core recovered", "asks": ASKS_V1}))
    client.post("/api/integrations", json=_ledger(run="M31_OSC4_v4b", version=4))
    client.post("/api/integrations/asks/fb1", json={"decision": "decline"})
    d = client.get("/api/integrations/review", params={"project_id": client.pid}).json()
    r = d["rigs"][0]
    assert r["rig"] == "piggyback" and r["latest"]["version"] == 4 and r["versions"] == 2
    assert r["verdict"] == "needs_more_data" and r["review_version"] == 3
    assert r["notes"] == "core recovered" and r["open_asks"] == 2
    asks = {a["id"]: a for a in r["asks"]}
    assert [a["id"] for a in r["asks"]][-1] == "fb1"                  # decided ones last
    assert asks["mh1"]["plan_change"] and asks["mh1"]["version"] == 3
    assert asks["rf1"]["plan_change"] and not asks["fb1"]["plan_change"]
    assert asks["fb1"]["status"] == "declined" and asks["fb1"]["decided_at"]
    assert client.get("/api/integrations/review",
                      params={"project_id": "nope"}).status_code == 404


def test_proposal_previews_on_a_copy_then_patch_applies_it(client):
    client.post("/api/integrations", json=_ledger(review={"asks": ASKS_V1}))
    before = _pj_hash(client)
    pr = client.get("/api/integrations/asks/mh1/proposal").json()
    assert pr["plan_change"] and pr["patch"] == {"osc_hours": 8.0}
    row = next(d for d in pr["diff"] if d["plan"] == "piggyback OSC")
    assert row["before"]["hours"] == 6.0 and row["after"]["hours"] == 8.0
    assert _pj_hash(client) == before                                # preview only
    assert client.store.projects[client.pid].exposure_plans[0].count == row["before"]["count"]
    # what the browser does after the confirm: PATCH, then mark applied
    r = client.patch(f"/api/projects2/{client.pid}", json=pr["patch"])
    assert r.status_code == 200
    plans = client.store.projects[client.pid].exposure_plans
    assert round(sum(e.count * e.exposure_seconds for e in plans) / 3600, 1) == 8.0
    res = client.post("/api/integrations/asks/mh1",
                      json={"decision": "applied", "patch": pr["patch"]}).json()
    assert res["ask"]["status"] == "applied" and res["ask"]["applied_patch"] == {"osc_hours": 8.0}
    rf = client.get("/api/integrations/asks/rf1/proposal").json()
    assert rf["patch"] == {"driving_rig": "piggyback"}
    assert any(d["plan"] == "driving_rig" for d in rf["diff"])
    fb = client.get("/api/integrations/asks/fb1/proposal").json()
    assert not fb["plan_change"] and fb["patch"] is None and "status only" in fb["note"]
    assert client.get("/api/integrations/asks/zz/proposal").status_code == 404


def test_rc16_asks_map_to_budget_mix_and_hdr(client):
    p = client.store.projects[client.qid]
    a = L.Ask(type="more_hours", rig="rc16", filter="OIII", hours=2.0)
    patch, note = integ.ask_patch(p, a)
    assert set(patch) == {"budget_hours", "filter_mix"} and "OIII" in note
    before, after = integ.preview_update(client.store, client.qid, patch)

    def hours(proj, f):
        return sum(e.count * e.exposure_seconds for e in proj.exposure_plans
                   if e.filter_type.value == f) / 3600
    assert hours(after, "OIII") == pytest.approx(hours(before, "OIII") + 2.0, abs=0.5)
    assert after.budget_hours == pytest.approx(before.budget_hours + 2.0, abs=0.2)
    assert client.store.projects[client.qid] is before                # store untouched
    patch, _ = integ.ask_patch(p, L.Ask(type="more_hours", rig="rc16", hours=1.0))
    assert patch == {"budget_hours": round(p.budget_hours + 1.0, 1)}
    patch, _ = integ.ask_patch(p, L.Ask(type="short_subs", rig="rc16", filter="Ha",
                                        exposure_s=60, count=20))
    assert patch == {"hdr": {"Ha": 60.0}}
    m31 = client.store.projects[client.pid]                          # Piggy-only goal
    patch, note = integ.ask_patch(m31, L.Ask(type="more_hours", rig="rc16", filter="L",
                                             hours=3.0))
    assert patch == {"rc16_hours": 3.0, "filter_mix": {"L": 100}}
    assert integ.ask_patch(m31, L.Ask(type="short_subs", filter="OSC", exposure_s=30))[0] is None
    assert integ.ask_patch(m31, L.Ask(type="reframe", driving_rig="rc16"))[0] is None
    assert integ.ask_patch(m31, L.Ask(type="need_calibration", what=["flats"]))[0] is None


# --- ready ping + dawn line ---------------------------------------------------------------

def test_new_version_pings_once_reposts_and_imports_do_not(client):
    r = client.post("/api/integrations", json=_ledger(version=3)).json()
    assert r["new_version"] is True
    assert client.pings == [("PhotonScript M31 integrated",
                             "M31 v3 integrated: 9.4 h, packet ready")]
    assert client.post("/api/integrations", json=_ledger(version=3)).json()["new_version"] is False
    imp = _ledger(run="M31_OSC4_v4b", version=4)
    imp["machine"]["imported"] = {"by": "photonscript ledger-import (PS-142)"}
    assert client.post("/api/integrations", json=imp).json()["new_version"] is True
    assert len(client.pings) == 1


def test_new_version_message_variants():
    led = L.parse(_ledger(version=2))
    assert integ.new_version_message(led) == "M31 v2 integrated: 9.4 h, packet ready"
    led.machine["integration"] = {"ok": False}
    assert integ.new_version_message(led) == "M31 v2 integration FAILED"
    led.machine["integration"] = {"ok": None}
    assert "not integrated" in integ.new_version_message(led)


def test_morning_note_lists_the_last_day_without_imports(client):
    assert integ.morning_note(client.cfg) == ""
    client.post("/api/integrations", json=_ledger(version=3))
    imp = _ledger(run="old", version=1)
    imp["machine"]["imported"] = {"by": "x"}
    client.post("/api/integrations", json=imp)
    assert integ.morning_note(client.cfg) == "Integrations: M31 v3 integrated: 9.4 h, packet ready"
    later = datetime.now(timezone.utc) + timedelta(hours=30)
    assert integ.morning_note(client.cfg, now=later) == ""


@pytest.mark.asyncio
async def test_night_complete_push_carries_the_integration_line(tmp_path, monkeypatch):
    from photonscript.scheduler import armer as armer_mod
    from photonscript.scheduler.armer import Armer
    sent = []

    async def _notify(cfg, msg, title="PhotonScript", priority=0, **kw):
        sent.append(msg)
    monkeypatch.setattr(armer_mod, "notify", _notify)
    monkeypatch.setattr(integ, "morning_note", lambda cfg: "Integrations: M31 v3 integrated")
    from photonscript.scheduler import calibration_owed   # PS-160 line: own test
    monkeypatch.setattr(calibration_owed, "morning_note", lambda cfg: None)
    a = Armer(_cfg(tmp_path))
    a.plan = {"night_of": "2026-10-06"}
    await a._notify_complete("Night complete.")
    assert sent == ["Night complete.\nIntegrations: M31 v3 integrated"]

    def boom(cfg):
        raise RuntimeError("bad ledger dir")
    monkeypatch.setattr(integ, "morning_note", boom)
    await a._notify_complete("Night complete.")
    assert sent[-1] == "Night complete."


def test_astrobin_url_sources():
    led = L.parse(_ledger())
    assert integ.astrobin_url(led) == ""
    led.publish = {"astrobin": {"url": "https://app.astrobin.com/i/a"}}
    assert integ.astrobin_url(led).endswith("/a")
    led.publish = {"astrobin_url": "https://app.astrobin.com/i/b"}
    assert integ.astrobin_url(led).endswith("/b")
    led.publish = {"astrobin": {"status": "packet_ready", "revision_of": "https://x/i/c"}}
    assert integ.astrobin_url(led) == ""
    assert json.loads(json.dumps(integ.ledger_brief(led)))["astrobin_url"] == ""


def test_pages_wire_the_chip_and_the_review_panel(client):
    js = client.get("/static/js/integrations.js").text
    for s in ("/api/integrations/status", "/api/integrations/review?project_id=",
              "/proposal", "'/api/projects2/'", "method: 'PATCH'", "decision: 'applied'",
              "if (!confirm(msg)) return;"):
        assert s in js, s
    assert js.isascii()
    root = Path(app.__file__).parent / "templates"
    dash = (root / "dashboard.html").read_text(encoding="utf-8")
    assert "Integrations.campaign('integ-', 'crev-', {onChange: loadProjects})" in dash
    assert "'<div id=\"crev-' + p.id" in dash
    tgt = (root / "target.html").read_text(encoding="utf-8")
    assert 'id="campPanel"' in tgt and "loadCampaign();" in tgt
    assert "Integrations.campaign('integ-', null)" in (root / "targets.html").read_text(
        encoding="utf-8")
