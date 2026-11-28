"""PS-32: campaign mirror rows, ledger versions on goals, and the optional
Notion push (off by default, dry run unless configured)."""

import json

import httpx

from photonscript.scheduler import campaign_mirror as cm
from photonscript.shared import ledger as L
from photonscript.shared.config import PhotonScriptConfig

CAMPAIGN = {"goals": [
    {"id": "g1", "name": "Andromeda Galaxy", "priority": 1, "status": "active",
     "driving_rig": "piggyback", "goal_hours": 6.0, "hours_done": 1.5,
     "bb_remaining_h": 4.5, "nb_remaining_h": 0.0, "osc_remaining_h": 4.5,
     "eta": "2026-11-09", "returns": None, "visible_h_14n": 41.2,
     "calibration": {"ok": False, "missing": ["piggyback: flats OSC"]},
     "by_rig": {"piggyback": {"goal_h": 6.0, "done_h": 1.5,
                              "filters": ["OSC", "OSC"]}},
     "latest_version": {"version": 3, "rig": "piggyback", "date": "2026-10-30"},
     "astrobin_url": "https://app.astrobin.com/i/abc123"},
    {"id": "g2", "name": "Lagoon Nebula", "priority": 2, "status": "parked",
     "driving_rig": "rc16", "goal_hours": 0.0, "hours_done": 0.0,
     "eta": None, "returns": "2027-05-01", "calibration": {"ok": None}},
]}


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path, **kw)


def test_rows_one_per_goal_keyed_by_id():
    rows = cm.mirror_rows(CAMPAIGN, synced="2026-11-02T12:00:00Z")
    assert [r["Goal ID"] for r in rows] == ["g1", "g2"]
    a, b = rows
    assert a["Status"] == "Active" and b["Status"] == "Parked"
    assert a["Percent"] == 25 and b["Percent"] == 0
    assert a["Rigs"] == "piggyback: 1.5/6 h (OSC)"
    assert a["Calibration"] == "Missing" and b["Calibration"] == "Unknown"
    assert a["Calibration missing"] == "piggyback: flats OSC"
    assert a["Latest version"] == "v3 piggyback 2026-10-30"
    assert a["AstroBin"].startswith("https://") and b["AstroBin"] is None
    assert b["Returns"] == "2027-05-01" and b["ETA"] is None
    assert set(a) - {"id"} == set(cm.DB_SCHEMA)


def test_notion_properties_match_schema_types():
    props = cm.notion_properties(cm.mirror_rows(CAMPAIGN)[0])
    assert props["Target"]["title"][0]["text"]["content"] == "Andromeda Galaxy"
    assert props["Status"] == {"select": {"name": "Active"}}
    assert props["Hours goal"] == {"number": 6.0}
    assert props["ETA"] == {"date": {"start": "2026-11-09"}}
    assert props["Synced"] == {"date": None}
    empty = cm.notion_properties(cm.mirror_rows(CAMPAIGN)[1])
    assert empty["Latest version"] == {"rich_text": []}
    assert empty["AstroBin"] == {"url": None}


def test_attach_versions_newest_ledger_and_astrobin(tmp_path):
    cfg = _cfg(tmp_path)
    d = tmp_path / "ledgers" / "g1"
    d.mkdir(parents=True)
    old = L.Ledger(campaign="M31", run="r1", version=1,
                   created_at="2026-10-01T10:00:00Z",
                   publish={"astrobin": {"url": "https://app.astrobin.com/i/old"}})
    new = L.Ledger(campaign="M31", run="r2", version=2,
                   created_at="2026-10-20T10:00:00Z")
    for name, led in (("v001.json", old), ("v002.json", new)):
        (d / name).write_text(json.dumps(led.dump()), encoding="utf-8")
    goals = [{"id": "g1"}, {"id": "none"}]
    cm.attach_versions(cfg, goals)
    assert goals[0]["latest_version"] == {"version": 2, "rig": "piggyback",
                                          "date": "2026-10-20"}
    assert goals[0]["astrobin_url"] == "https://app.astrobin.com/i/old"
    assert goals[1]["latest_version"] is None and goals[1]["astrobin_url"] == ""


def test_push_is_off_by_default_and_sends_nothing(tmp_path):
    calls = []
    client = httpx.Client(transport=httpx.MockTransport(
        lambda req: calls.append(req) or httpx.Response(500)))
    rows = cm.mirror_rows(CAMPAIGN)
    res = cm.push(_cfg(tmp_path), rows, dry_run=False, client=client)
    assert res["dry_run"] and res["settings"]["why_not"].startswith(
        "PS_CAMPAIGN_MIRROR_ENABLED")
    assert res["would_upsert"] == ["g1", "g2"] and calls == []
    # enabled but no token: still nothing sent
    res = cm.push(_cfg(tmp_path, campaign_mirror_enabled=True), rows,
                  dry_run=False, client=client)
    assert res["dry_run"] and "PS_NOTION_TOKEN" in res["settings"]["why_not"]
    # fully configured, but a dry run: nothing sent
    full = _cfg(tmp_path, campaign_mirror_enabled=True, notion_token="tok-SECRET",
                notion_campaigns_db="db1")
    assert cm.push(full, rows, client=client)["dry_run"] and calls == []
    assert "tok-SECRET" not in json.dumps(cm.push_settings(full))


def test_push_upserts_by_goal_id(tmp_path):
    cfg = _cfg(tmp_path, campaign_mirror_enabled=True, notion_token="secret-x",
               notion_campaigns_db="db1")
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        assert req.headers["Authorization"] == "Bearer secret-x"
        if req.url.path.endswith("/query"):
            gid = json.loads(req.content)["filter"]["rich_text"]["equals"]
            hits = [{"id": "page-1"}] if gid == "g1" else []
            return httpx.Response(200, json={"results": hits})
        return httpx.Response(200, json={"id": "ok"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    res = cm.push(cfg, cm.mirror_rows(CAMPAIGN), dry_run=False, client=client)
    assert not res["dry_run"] and not res["errors"]
    assert res["updated"] == ["g1"] and res["created"] == ["g2"]
    assert ("PATCH", "/v1/pages/page-1") in seen
    assert ("POST", "/v1/pages") in seen


def test_push_reports_errors_per_goal(tmp_path):
    cfg = _cfg(tmp_path, campaign_mirror_enabled=True, notion_token="t",
               notion_campaigns_db="db1")
    client = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(401, json={"message": "unauthorized"})))
    res = cm.push(cfg, cm.mirror_rows(CAMPAIGN), dry_run=False, client=client)
    assert [e.split(":")[0] for e in res["errors"]] == ["g1", "g2"]
    assert "Bearer" not in " ".join(res["errors"])


def test_mirror_endpoint_rows_and_versions(tmp_path, monkeypatch):
    import copy

    from fastapi.testclient import TestClient

    from photonscript.scheduler import app, campaign, forecast
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    monkeypatch.setattr(app, "get_store", lambda: None)

    async def _no_fc(config):
        raise RuntimeError("offline")
    monkeypatch.setattr(forecast, "get_forecast", _no_fc)
    goals = [{k: v for k, v in g.items()
              if k not in ("latest_version", "astrobin_url")}
             for g in copy.deepcopy(CAMPAIGN["goals"])]
    monkeypatch.setattr(campaign, "build_campaign",
                        lambda *a, **k: {"goals": copy.deepcopy(goals)})
    monkeypatch.setattr(campaign, "notify_transitions", lambda *a: [])
    monkeypatch.setattr(campaign, "suggest_targets", lambda *a: [])
    c = TestClient(app.app)
    g = c.get("/api/campaign").json()["goals"][0]
    assert g["latest_version"] is None and g["astrobin_url"] == ""
    body = c.get("/api/campaign/mirror").json()
    assert [r["Goal ID"] for r in body["rows"]] == ["g1", "g2"]
    assert body["rows"][0]["Synced"].endswith("Z")
    assert body["push"] == {"enabled": False, "token_set": False, "database": "",
                            "ready": False,
                            "why_not": "PS_CAMPAIGN_MIRROR_ENABLED is false"}
