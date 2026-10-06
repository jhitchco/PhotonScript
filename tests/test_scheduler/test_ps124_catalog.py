"""PS-124: NGC 604 and the RC16-scale autumn / winter targets in the catalog,
any target by RA/Dec through the Add box (user catalog in the data dir), and
size-aware creation defaults (rig hint, sub length, goal hours, mix, Piggy
OSC goal).
"""
import json

import pytest
from fastapi.testclient import TestClient

from photonscript.scheduler import app, catalog
from photonscript.scheduler.project_store import ProjectStore
from photonscript.shared import astronomy
from photonscript.shared.astronomy import (find_catalog_entry,
                                           get_seasonal_targets, months_for_ra,
                                           rig_hint)
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import FilterType


@pytest.fixture(autouse=True)
def no_user_catalog(monkeypatch):
    """Every test starts (and leaves) astronomy with an empty user catalog."""
    monkeypatch.setattr(astronomy, "_USER_TARGETS", [])


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path))
    monkeypatch.setattr(ProjectStore, "SEED_PATH", tmp_path / "no_seed.json")
    monkeypatch.setattr(app, "_config", cfg)
    monkeypatch.setattr(app, "_store", None)
    monkeypatch.setattr(app, "_projects", {})
    monkeypatch.setattr(app, "_dashboard_cache", {})
    return {"cfg": cfg, "client": TestClient(app.app), "dir": tmp_path}


def _plans(proj_json):
    return {p["filter_type"]: p for p in proj_json["exposure_plans"]}


# ------------------------------------------------------------- catalog rows

def test_ngc604_row_matches_the_ticket():
    e = find_catalog_entry("NGC 604")
    assert e["ra"] == pytest.approx(1.5758) and e["dec"] == pytest.approx(30.783)
    assert e["months"] == [9, 10, 11, 12, 1]
    assert e["mix"] == {"Ha": 50, "OIII": 40, "SII": 10}
    assert e["goal_hours"] == 10 and e["osc_hours"] == 10
    assert 1.5 <= e["size"] <= 2
    assert rig_hint(e["size"]) == "rc16"


@pytest.mark.parametrize("name", [
    "NGC 604", "ngc604", "NGC 7331", "Deer Lick Group", "NGC 891",
    "Stephan's Quintet", "stephans quintet", "HCG 92", "M76", "m 76",
    "Little Dumbbell", "NGC 7662", "Blue Snowball", "NGC 40", "M1", "Crab",
    "NGC 2392", "Eskimo Nebula", "M74", "M77", "NGC 7008", "IC 410",
    "Tadpoles"])
def test_every_ticket_target_is_findable(name):
    e = find_catalog_entry(name)
    assert e is not None, name
    assert e.get("goal_hours"), name          # PS-124 defaults attached


def test_ticket_targets_are_offered_in_their_season():
    oct_names = {t.catalog_id for t in get_seasonal_targets(10)}
    assert {"NGC 604", "NGC 7331", "NGC 891", "HCG092", "M 76", "NGC 7662",
            "NGC 40", "M 74", "M 77", "NGC 7008"} <= oct_names
    dec_names = {t.catalog_id for t in get_seasonal_targets(12)}
    assert {"M 1", "NGC 2392", "IC 410", "NGC 604"} <= dec_names
    assert "NGC 604" in {t.catalog_id for t in get_seasonal_targets(1)}


def test_no_duplicate_catalog_ids_for_the_new_rows():
    ids = [e["catalog_id"] for e in astronomy.SEASONAL_TARGETS]
    for cid in ("NGC 604", "IC 410"):
        assert ids.count(cid) == 1
    assert set(astronomy.CATALOG_EXTRAS) <= set(ids)   # no orphan extras


def test_near_equator_south_declinations_keep_their_sign():
    """The OpenNGC import dropped the sign of '-00' Decs (M 77 sat 1.6' and
    M 2 98' north of the real position)."""
    by_id = {e["catalog_id"]: e for e in astronomy.SEASONAL_TARGETS}
    assert by_id["M 77"]["dec"] == pytest.approx(-0.01328)
    assert by_id["M 2"]["dec"] == pytest.approx(-0.82331)
    assert by_id["NGC 6741"]["dec"] == pytest.approx(-0.44939)


def test_rig_hint_thresholds():
    assert rig_hint(None) == "rc16"
    assert rig_hint(2) == "rc16" and rig_hint(14.9) == "rc16"
    assert rig_hint(15) == "both" and rig_hint(60) == "both"
    assert rig_hint(60.1) == "piggyback"


def test_months_for_ra_follows_the_generated_rows():
    # spot checks against rows the 2026-09-18 generator wrote
    for cid in ("M 74", "NGC 7331", "NGC 2392", "NGC 891", "M 34"):
        e = next(x for x in astronomy.SEASONAL_TARGETS if x["catalog_id"] == cid)
        assert sorted(months_for_ra(e["ra"])) == sorted(e["months"]), cid


# ------------------------------------------------------------- text parsing

@pytest.mark.parametrize("text,expect", [
    ("NGC 604 01:34:33 +30:47", ("NGC 604", 1.57583, 30.78333)),
    ("NGC 604 1.5758 30.783", ("NGC 604", 1.5758, 30.783)),
    ("01h34m33s +30d47m", ("", 1.57583, 30.78333)),
    ("Foo 01 34 33 +30 47 00", ("Foo", 1.57583, 30.78333)),
    ("Sh2-155 22.945 62.62", ("Sh2-155", 22.945, 62.62)),
    ("South 05:35:17 -05:23:28", ("South", 5.58806, -5.39111)),
    ("Deg 354.2 +12.5", ("Deg", 23.61333, 12.5)),     # RA over 24 = degrees
    ("Sym 01h34m33s +30\u00b047'", ("Sym", 1.57583, 30.78333)),
])
def test_parse_coordinates(text, expect):
    name, ra, dec = catalog.parse_target_text(text)
    assert name == expect[0]
    assert ra == pytest.approx(expect[1], abs=1e-4)
    assert dec == pytest.approx(expect[2], abs=1e-4)


@pytest.mark.parametrize("text", ["NGC 7000", "M 31", "Sh2 155 22", "", "Heart"])
def test_names_alone_are_not_coordinates(text):
    assert catalog.parse_target_text(text) is None


@pytest.mark.parametrize("text", ["Bad 25:00:00 +10:00", "Bad 01:61:00 +10:00",
                                  "Bad 12.5 +95"])
def test_out_of_range_coordinates_raise(text):
    with pytest.raises(ValueError):
        catalog.parse_target_text(text)


# ------------------------------------------------------------- from_catalog

def test_from_catalog_ngc604_gets_the_two_rig_goal(env):
    r = env["client"].post("/api/projects2/from_catalog", json={"name": "ngc604"})
    assert r.status_code == 200
    p = r.json()
    assert p["target"]["name"] == "NGC 604" and p["budget_hours"] == 10
    plans = _plans(p)
    assert {k: plans[k]["exposure_seconds"] for k in ("Ha", "OIII", "SII")} == \
        {"Ha": 600, "OIII": 600, "SII": 600}
    assert plans["Ha"]["count"] == 30 and plans["OIII"]["count"] == 24 \
        and plans["SII"]["count"] == 6                      # 50 / 40 / 10 of 10 h
    osc = plans[FilterType.OSC.value]
    assert osc["rig"] == "piggyback"
    assert osc["count"] * osc["exposure_seconds"] == pytest.approx(10 * 3600)
    d = p["catalog_defaults"]
    assert d["rig_hint"] == "rc16" and d["osc_hours"] == 10
    assert d["sub_seconds"] == {"Ha": 600, "OIII": 600, "SII": 600}
    assert "M33" in d["note"]


def test_from_catalog_plain_row_keeps_the_old_default(env):
    p = env["client"].post("/api/projects2/from_catalog",
                           json={"name": "Andromeda Galaxy"}).json()
    assert p["budget_hours"] == 8
    assert FilterType.OSC.value not in _plans(p)
    assert p["catalog_defaults"]["rig_hint"] == "piggyback"
    assert p["catalog_defaults"]["sub_seconds"]["L"] == 180


def test_from_catalog_body_budget_wins(env):
    p = env["client"].post("/api/projects2/from_catalog",
                           json={"name": "Little Dumbbell", "budget_hours": 4}).json()
    assert p["target"]["catalog_id"] == "M 76" and p["budget_hours"] == 4
    assert p["filter_mix"] == {"Ha": 50, "OIII": 40, "SII": 10}


def test_from_catalog_galaxy_ignores_mix_and_unknown_is_404(env):
    p = env["client"].post("/api/projects2/from_catalog", json={"name": "M77"}).json()
    assert p["filter_mix"] is None and "L" in _plans(p)
    r = env["client"].post("/api/projects2/from_catalog", json={"name": "Nowhere 1"})
    assert r.status_code == 404


# ------------------------------------------------------------- custom / user catalog

def test_custom_text_creates_goal_and_user_catalog_row(env):
    c = env["client"]
    r = c.post("/api/projects2/custom",
               json={"text": "Sh2-188 01:30:33 +58:24:50"})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["target"]["name"] == "Sh2-188"
    assert p["target"]["ra_hours"] == pytest.approx(1.50917, abs=1e-4)
    assert p["budget_hours"] == 8
    rows = json.loads((env["dir"] / "user_catalog.json").read_text())["targets"]
    assert [x["name"] for x in rows] == ["Sh2-188"]
    assert rows[0]["months"] == months_for_ra(rows[0]["ra"])
    # the user row reaches every get_seasonal_targets caller
    names = {t.name for m in rows[0]["months"] for t in get_seasonal_targets(m)}
    assert "Sh2-188" in names
    assert find_catalog_entry("sh2 188")["user"] is True
    assert c.get("/api/catalog/user").json()["targets"][0]["name"] == "Sh2-188"


def test_custom_same_name_replaces_the_row(env):
    c = env["client"]
    c.post("/api/projects2/custom", json={"text": "Mine 01:00:00 +10:00"})
    c.post("/api/projects2/custom",
           json={"name": "mine", "ra_hours": 2.0, "dec_degrees": 20.0,
                 "type": "emission nebula", "size_arcmin": 30, "budget_hours": 5})
    rows = json.loads((env["dir"] / "user_catalog.json").read_text())["targets"]
    assert len(rows) == 1 and rows[0]["ra"] == 2.0
    e = find_catalog_entry("Mine")
    assert e["type"] == "emission nebula"
    assert catalog.creation_defaults(e, env["cfg"])["rig_hint"] == "both"


def test_custom_unnamed_coordinates_get_a_name(env):
    p = env["client"].post("/api/projects2/custom",
                           json={"text": "01h34m33s +30d47m"}).json()
    assert p["target"]["name"] == "RA 01h35m Dec +30d47m"


@pytest.mark.parametrize("body", [{"text": "Just a name"},
                                  {"text": "Bad 25:00:00 +10:00"},
                                  {"name": "No coords"}])
def test_custom_rejects_bad_input(env, body):
    r = env["client"].post("/api/projects2/custom", json=body)
    assert r.status_code == 400
    assert "RA" in r.json()["detail"]
    assert not (env["dir"] / "user_catalog.json").exists()


def test_bad_user_catalog_file_loads_nothing(env):
    path = env["dir"] / "user_catalog.json"
    path.write_text("{not json", encoding="utf-8")
    assert catalog.load_user_catalog(env["cfg"]) == []
    path.write_text(json.dumps({"targets": [
        {"name": "ok", "ra": 3.0, "dec": 10.0},
        {"name": "bad", "ra": 99, "dec": 0},
        {"ra": 1, "dec": 1}]}), encoding="utf-8")
    assert [e["name"] for e in catalog.load_user_catalog(env["cfg"])] == ["ok"]
    assert [e["name"] for e in astronomy._USER_TARGETS] == ["ok"]


def test_lookup_endpoint(env):
    c = env["client"]
    j = c.get("/api/catalog/lookup", params={"q": "NGC 604"}).json()
    assert j["match"] == "catalog" and j["defaults"]["osc_hours"] == 10
    j = c.get("/api/catalog/lookup", params={"q": "X 1.5 +30"}).json()
    assert j["match"] == "coordinates" and j["ra_hours"] == 1.5
    assert c.get("/api/catalog/lookup", params={"q": "nothing"}).status_code == 404


def test_router_loads_the_user_catalog_at_startup(env):
    from photonscript.scheduler.routers import catalog as router_mod
    assert router_mod._load_user_catalog in app.app.router.on_startup
    (env["dir"] / "user_catalog.json").write_text(json.dumps(
        {"targets": [{"name": "Boot", "ra": 4.0, "dec": 5.0}]}), encoding="utf-8")
    import asyncio
    asyncio.new_event_loop().run_until_complete(router_mod._load_user_catalog())
    assert find_catalog_entry("Boot") is not None


# ------------------------------------------------------------- picker / fallback

def test_picker_keeps_ps124_rows_below_the_cut():
    targets = get_seasonal_targets(10)
    ranked = [{"target": t} for t in targets]
    ngc604 = next(r for r in ranked if r["target"].catalog_id == "NGC 604")
    ranked.remove(ngc604)
    ranked.append(ngc604)                       # worst of the night
    out = catalog.picker_targets(ranked, limit=5)
    assert ranked[:5] == out[:5]
    assert ngc604 in out
    assert all(r["target"].catalog_id in astronomy.CATALOG_EXTRAS
               for r in out[5:])


def test_seasonal_fallback_plans_a_user_row(env, monkeypatch):
    """No goals: the fallback ranks get_seasonal_targets, which now carries
    the user catalog."""
    from datetime import datetime
    catalog.save_user_entry(env["cfg"], {"name": "Fallback Me",
                                         "ra": 1.0, "dec": 30.0,
                                         "months": list(range(1, 13))})
    month = datetime.utcnow().month
    seen = []
    monkeypatch.setattr(app, "rank_targets_for_night",
                        lambda ts, obs, now: [{"target": t} for t in ts
                                              if t.name == "Fallback Me"])
    monkeypatch.setattr(app, "plan_night_sequence",
                        lambda projects, config, now: seen.extend(
                            p.target.name for p in projects) or [])
    import asyncio
    asyncio.new_event_loop().run_until_complete(app.api_tonight_plan())
    assert seen == ["Fallback Me"]
    assert month in find_catalog_entry("Fallback Me")["months"]
