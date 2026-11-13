"""PS-48: per-target (goal) QA gate overrides: HFR / FWHM / ecc per rig,
stored on the project, applied by shared.qa_rules.thresholds for every
grader, shown as "target override" in the scorecard panel, edited on the
per-target page (routers/targets.py)."""

import pytest

from photonscript.scheduler.project_store import ProjectStore
from photonscript.shared import qa_rules as q
from photonscript.shared import qa_score
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget

GOOD = dict(hfr=5.0, fwhm_arcsec=2.4, ecc=0.40, stars=150, background=400.0,
            exp_s=300.0, ccd_temp=0.2, exposure="ok")
CATS = "Cat's Eye Nebula"
CATS_C = "Cat's Eye Nebula imaging (repeats while safe and up)_Container"


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"),
                              quality_eccentricity_max=0.6,
                              piggyback_enabled=True, **kw)


def _store(cfg):
    store = ProjectStore(cfg)
    p = next((x for x in store.projects.values()     # the seeded goal
              if x.target.name == CATS), None)
    if p is not None:
        return store, p
    p = store.add_from_target(CelestialTarget(
        name=CATS, catalog_id="NGC 6543", ra_hours=17.98, dec_degrees=66.63,
        object_type="planetary nebula"), budget_hours=6)
    return store, p


def _card(cfg, metrics=None, rig="rc16", target=CATS, flt="Ha"):
    m = dict(GOOD)
    m.update(metrics or {})
    return q.evaluate(m, q.context(cfg, rig, target, flt))


def _row(card, cid):
    return next(c for c in card.checks if c.id == cid)


def test_no_override_keeps_the_rig_gates(tmp_path):
    cfg = _cfg(tmp_path)
    _store(cfg)
    t = q.thresholds(cfg, "rc16", CATS, "Ha")
    assert t["hfr_max"] == 10.0 and "target_override" not in t
    assert "override" not in t


def test_goal_override_wins_on_its_rig_only(tmp_path):
    cfg = _cfg(tmp_path)
    store, p = _store(cfg)
    pig = q.thresholds(cfg, "piggyback")
    store.set_qa_overrides(p.id, "rc16",
                           {"hfr_max": 6, "fwhm_max": 2.5, "ecc_max": 0.5})
    t = q.thresholds(cfg, "rc16", CATS, "OIII")
    assert (t["hfr_max"], t["fwhm_max"], t["ecc_max"]) == (6.0, 2.5, 0.5)
    assert t["ecc_max_bin"] == 0.5           # binned ecc gate follows
    assert t["target_override"] == {"target": CATS, "gates": {
        "hfr_max": 6.0, "fwhm_max": 2.5, "ecc_max": 0.5}}
    assert t["override"] == ["ecc_max", "fwhm_max", "hfr_max"]
    # the Piggy-600 keeps its own (PS-114) gates for the same target
    tp = q.thresholds(cfg, "piggyback", CATS, "OSC")
    assert tp["hfr_max"] == pig["hfr_max"] and "target_override" not in tp
    # other targets and no-target lookups are untouched
    assert q.thresholds(cfg, "rc16", "Heart Nebula")["hfr_max"] == 10.0
    assert q.thresholds(cfg, "rc16")["hfr_max"] == 10.0


def test_goal_override_matches_catalog_id_and_container_name(tmp_path):
    cfg = _cfg(tmp_path)
    store, p = _store(cfg)
    store.set_qa_overrides(p.id, "rc16", {"hfr_max": 6})
    for name in ("NGC 6543", "ngc6543", "cats eye nebula", CATS_C):
        assert q.thresholds(cfg, "rc16", name)["hfr_max"] == 6.0, name


def test_override_rejects_and_says_so(tmp_path):
    cfg = _cfg(tmp_path)
    store, p = _store(cfg)
    assert _row(_card(cfg, {"hfr": 7.0}), "hfr").status == q.PASS
    store.set_qa_overrides(p.id, "rc16", {"hfr_max": 6, "fwhm_max": 2.0})
    card = _card(cfg, {"hfr": 7.0, "fwhm_arcsec": 2.4})
    hfr = _row(card, "hfr")
    assert hfr.status == q.FAIL and hfr.limit == 6.0
    assert hfr.reason.endswith("(out of focus) (target override)")
    assert _row(card, "fwhm").reason.endswith("(target override)")
    assert card.verdict == q.REJECTED
    # a rig gate (no override) says nothing extra
    e = _row(_card(cfg, {"ecc": 0.9}), "ecc")
    assert e.status == q.FAIL and "target override" not in e.reason


def test_scorecard_panel_marks_the_target_override(tmp_path):
    cfg = _cfg(tmp_path)
    store, p = _store(cfg)
    store.set_qa_overrides(p.id, "rc16", {"hfr_max": 6})
    t = q.thresholds(cfg, "rc16", CATS, "Ha")
    card = _card(cfg, {"hfr": 5.0})
    rows = q.expand(card.compact())
    panel = {r["id"]: r for r in qa_score.panel_rows(
        {"rig": "rc16"}, rows, t, None)}
    assert panel["hfr"]["gate"] == "<= 6 px (target override)"
    assert "target override" not in panel["stars"]["gate"]
    assert "target override" not in panel["ecc"]["gate"]


def test_set_qa_overrides_validates_and_clears(tmp_path):
    cfg = _cfg(tmp_path)
    store, p = _store(cfg)
    with pytest.raises(ValueError):
        store.set_qa_overrides(p.id, "mars", {"hfr_max": 6})
    with pytest.raises(ValueError):
        store.set_qa_overrides(p.id, "rc16", {"star_min": 6})
    with pytest.raises(ValueError):
        store.set_qa_overrides(p.id, "rc16", {"ecc_max": 1.2})
    with pytest.raises(ValueError):
        store.set_qa_overrides(p.id, "rc16", {"hfr_max": -1})
    with pytest.raises(ValueError):
        store.set_qa_overrides(p.id, "rc16", {"hfr_max": "abc"})
    assert store.set_qa_overrides("nope", "rc16", {}) is None
    store.set_qa_overrides(p.id, "rc16", {"hfr_max": "6.5", "fwhm_max": ""})
    store.set_qa_overrides(p.id, "piggyback", {"fwhm_max": 9})
    assert p.qa_overrides == {"rc16": {"hfr_max": 6.5},
                              "piggyback": {"fwhm_max": 9.0}}
    # persisted, and the grader sees the reload
    assert ProjectStore(cfg).projects[p.id].qa_overrides == p.qa_overrides
    assert q.thresholds(cfg, "piggyback", CATS)["fwhm_max"] == 9.0
    store.set_qa_overrides(p.id, "rc16", {"hfr_max": None})
    store.set_qa_overrides(p.id, "piggyback", {})
    assert p.qa_overrides is None
    assert q.thresholds(cfg, "rc16", CATS)["hfr_max"] == 10.0


def test_bad_values_in_projects_json_are_ignored(tmp_path):
    import json
    cfg = _cfg(tmp_path)
    store, p = _store(cfg)
    raw = json.loads((tmp_path / "projects.json").read_text(encoding="utf-8"))
    raw[p.id]["qa_overrides"] = {"rc16": {"hfr_max": "x", "fwhm_max": -2,
                                          "ecc_max": 0.55}, "piggyback": 3}
    (tmp_path / "projects.json").write_text(json.dumps(raw), encoding="utf-8")
    t = q.thresholds(cfg, "rc16", CATS)
    assert t["hfr_max"] == 10.0 and t["ecc_max"] == 0.55


@pytest.fixture
def api(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    store, p = _store(cfg)
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(app, "_store", store)
    from fastapi.testclient import TestClient
    return cfg, store, p, TestClient(app.app)


def test_endpoints_read_and_write_overrides(api):
    cfg, store, p, client = api
    r = client.get("/api/targets/qa-overrides", params={"name": "NGC 6543"})
    assert r.status_code == 200
    d = r.json()
    assert d["project_id"] == p.id
    assert [x["id"] for x in d["rigs"]] == ["rc16", "piggyback"]
    hfr = next(g for g in d["rigs"][0]["gates"] if g["key"] == "hfr_max")
    assert hfr["rig_gate"] == 10.0 and hfr["override"] is None
    r = client.post("/api/targets/qa-overrides", json={
        "name": CATS, "rig": "rc16", "gates": {"hfr_max": 6, "ecc_max": None}})
    assert r.status_code == 200
    hfr = next(g for g in r.json()["rigs"][0]["gates"] if g["key"] == "hfr_max")
    assert hfr["override"] == 6.0 and hfr["rig_gate"] == 10.0
    assert q.thresholds(cfg, "rc16", CATS)["hfr_max"] == 6.0
    assert client.post("/api/targets/qa-overrides", json={
        "name": CATS, "rig": "rc16", "gates": {"ecc_max": 2}}).status_code == 400
    assert client.post("/api/targets/qa-overrides", json={
        "name": CATS, "rig": "rc16", "gates": []}).status_code == 400
    assert client.post("/api/targets/qa-overrides", json={
        "name": "Nowhere", "rig": "rc16", "gates": {}}).status_code == 404
    assert client.get("/api/targets/qa-overrides",
                      params={"name": "Nowhere"}).status_code == 404


def test_target_page_has_the_gate_editor(api):
    _cfg_, _s, _p, client = api
    t = client.get("/target", params={"name": CATS}).text
    assert "/api/targets/qa-overrides" in t and 'id="qaPanel"' in t
    assert "target override" in t


def test_goal_card_flags_a_target_override():
    from pathlib import Path
    html = (Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
            / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert "p.qa_overrides ?" in html and "QA gates: target override" in html
