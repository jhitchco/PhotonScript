"""PS-111: mosaic planner v2. One mosaic goal made of RC16 panel goals:
geometry (TAN plane, overlap, rotation at the camera angle), panels as child
goals with per-panel plans and seconds crediting, in-order scheduling in the
night planner and the campaign, one DeepSkyObjectContainer per panel, the
Piggy-600 companion credit, the API and the Targets grouping.

First use (Jeremy, 2026-10-05): an RC16 2 x 2 of the M31 core and inner dust
lanes while the Piggy-600 shoots the whole galaxy."""

import json
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from photonscript.scheduler import app, campaign, runs, sub_index
from photonscript.scheduler import mosaic as mz
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.project_store import ProjectStore
from photonscript.scheduler.sequence_lint import lint
from photonscript.scheduler.target_planner import plan_night_sequence
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget, FilterType

RC16_W, RC16_H = 24.5 / 60, 16.4 / 60      # deg, 6224 x 4168 at 0.236"/px
M31 = CelestialTarget(name="Andromeda Galaxy", catalog_id="M 31",
                      ra_hours=0.712, dec_degrees=41.27, object_type="galaxy")
NIGHT = datetime(2026, 10, 11, 3, 0)       # M31 up most of the night


@pytest.fixture(autouse=True)
def _no_seed(tmp_path, monkeypatch):
    monkeypatch.setattr(ProjectStore, "SEED_PATH", tmp_path / "no_seed.json")


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _m31_goal(store):
    proj = store.add_from_target(M31, budget_hours=6.0)
    store.update(proj.id, osc_hours=6.0, drop_rc16=True,
                 driving_rig="piggyback")
    return proj


def _add_mosaic(cfg, store, **over):
    spec = {**mz.M31_SUGGESTION, **over}
    comp = mz.find_companion(store.projects.values(), spec.get("companion"))
    spec["companion_id"] = comp.id if comp else None
    built = mz.build_panels(cfg, spec, [p.target.name
                                        for p in store.projects.values()])
    assert not built["errors"], built["errors"]
    for p in built["panels"]:
        store.projects[p.id] = p
    store.save()
    return built


def _write_solves(cfg, night, rig, pas):
    from pathlib import Path
    d = Path(cfg.data_dir) / "solves" / night
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{rig}.jsonl").write_text("".join(
        json.dumps({"file": f"f{i}.fits", "solved": True, "pa": pa}) + "\n"
        for i, pa in enumerate(pas)), encoding="utf-8")


# --- geometry -------------------------------------------------------------------

def test_m31_2x2_span_is_about_45_by_30_arcmin():
    lay = mz.layout("M31 Core", 0.712306, 41.269167, 2, 2, 15.0, 0.0,
                    RC16_W, RC16_H)
    assert lay["span_w_arcmin"] == pytest.approx(45.3, abs=0.1)
    assert lay["span_h_arcmin"] == pytest.approx(30.3, abs=0.1)
    assert [p["name"] for p in lay["panels"]] == [
        "M31 Core P1", "M31 Core P2", "M31 Core P3", "M31 Core P4"]


def test_panel_one_is_top_left_north_up_east_left():
    lay = mz.layout("X", 0.712306, 41.269167, 2, 2, 15.0, 0.0, RC16_W, RC16_H)
    p1, p2, p3, p4 = lay["panels"]
    # P1 = row 1 (north), col 1 (east = higher RA)
    assert p1["north_deg"] > 0 and p1["east_deg"] > 0
    assert p1["ra_hours"] > p2["ra_hours"] and p1["dec_degrees"] > p3["dec_degrees"]
    assert (p4["row"], p4["col"]) == (2, 2)
    # symmetric about the center
    assert p1["east_deg"] == pytest.approx(-p4["east_deg"])
    assert p1["north_deg"] == pytest.approx(-p4["north_deg"])


@pytest.mark.parametrize("pa", [0.0, 35.0, 125.0, 271.5])
@pytest.mark.parametrize("ov", [10.0, 15.0, 30.0])
def test_overlap_is_exact_on_the_sky_at_any_angle(pa, ov):
    lay = mz.layout("X", 0.712306, 41.269167, 2, 3, ov, pa, RC16_W, RC16_H)
    for a, b in ((1, 2), (2, 3), (4, 5), (1, 4), (3, 6)):
        assert mz.overlap_fraction(lay, a, b) == pytest.approx(ov / 100,
                                                               abs=1e-4)


def test_tan_round_trip_and_single_panel_is_the_center():
    for e, n in ((0.3, -0.2), (-1.2, 0.8), (0.0, 0.0)):
        ra, dec = mz.deproject(0.712, 41.27, e, n)
        e2, n2 = mz.project(0.712, 41.27, ra, dec)
        assert (e2, n2) == pytest.approx((e, n), abs=1e-9)
    (p,) = mz.layout("X", 5.5, -5.4, 1, 1, 15, 0.0)["panels"]
    assert (p["ra_hours"], p["dec_degrees"]) == pytest.approx((5.5, -5.4))


def test_rotation_turns_the_frames_with_the_grid():
    # PA 90: the +y axis points east, the long side runs north-south
    c = mz.frame_corners(0.0, 0.0, RC16_W, RC16_H, 90.0)
    es = [x[0] for x in c]
    ns = [x[1] for x in c]
    assert max(ns) - min(ns) == pytest.approx(RC16_W, abs=1e-4)
    assert max(es) - min(es) == pytest.approx(RC16_H, abs=1e-4)
    lay = mz.layout("X", 0.7, 41.3, 1, 2, 15.0, 90.0, RC16_W, RC16_H)
    p1, p2 = lay["panels"]
    # stacked N-S; column 1 lies toward PA + 90 (south here)
    assert abs(p1["east_deg"]) < 1e-6 and p1["north_deg"] < 0 < p2["north_deg"]


def test_camera_pa_for_galaxy_major_axis():
    assert mz.camera_pa_for_major_axis(35) == 125.0
    assert mz.camera_pa_for_major_axis(170) == 80.0


def test_camera_pa_from_solves_wraps_mod_180(tmp_path):
    cfg = _cfg(tmp_path)
    assert mz.camera_pa(cfg)["pa_deg"] is None
    # 179.5 and 0.5 are the same frame angle; a flip adds 180
    _write_solves(cfg, "2026-10-01", "rc16", [179.5, 0.5, 180.4, 359.8, 1.0])
    pa = mz.camera_pa(cfg)
    assert pa["n"] == 5
    assert min(pa["pa_deg"], 180 - pa["pa_deg"]) < 0.6


def test_rotation_defaults_to_the_camera_and_flags_a_mismatch(tmp_path):
    cfg = _cfg(tmp_path)
    r = mz.resolve_rotation(cfg, "camera", 35.0)
    assert r["pa_deg"] == 0.0 and r["warnings"]          # unknown: north up
    assert r["major_axis"]["camera_pa_needed"] == 125.0
    assert not r["major_axis"]["grid_along_major_axis"]
    _write_solves(cfg, "2026-10-02", "rc16", [92.1, 92.3, 272.2])
    r = mz.resolve_rotation(cfg, "camera")
    assert r["pa_deg"] == pytest.approx(92.2, abs=0.2)
    assert r["aligned_to_camera"] and not r["warnings"]
    r = mz.resolve_rotation(cfg, 125.0, 35.0)
    assert r["pa_deg"] == 125.0 and not r["aligned_to_camera"]
    assert any("no rotator" in w for w in r["warnings"])


# --- panels as child goals --------------------------------------------------------

def test_m31_suggestion_builds_four_lrgb_panels(tmp_path):
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    m31 = _m31_goal(store)
    built = _add_mosaic(cfg, store)
    assert len(built["panels"]) == 4
    for i, p in enumerate(built["panels"], 1):
        plans = {e.filter_type.value: e for e in p.exposure_plans}
        assert {k: (e.count, e.exposure_seconds) for k, e in plans.items()} == {
            "L": (24, 300.0), "R": (8, 300.0), "G": (8, 300.0), "B": (8, 300.0)}
        assert sum(e.count * e.exposure_seconds for e in p.exposure_plans) \
            == 4 * 3600
        assert all(e.rig == "rc16" for e in p.exposure_plans)
        assert p.filter_mix == {"L": 50, "R": 17, "G": 17, "B": 17}
        assert p.mosaic["panel"] == i and p.mosaic["of"] == 4
        assert p.mosaic["companion"] == m31.id
        assert p.mosaic["layout"]["order"] == "in_order"
    # survives a reload
    again = ProjectStore(cfg)
    assert sum(1 for p in again.projects.values() if p.mosaic) == 4


def test_name_clash_and_bad_grid_are_refused(tmp_path):
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    _add_mosaic(cfg, store)
    built = mz.build_panels(cfg, dict(mz.M31_SUGGESTION),
                            [p.target.name for p in store.projects.values()])
    assert any("already" in e for e in built["errors"])
    built = mz.build_panels(cfg, {**mz.M31_SUGGESTION, "name": "Y",
                                  "rows": 5, "cols": 5}, [])
    assert any("panels" in e for e in built["errors"])


# --- crediting ------------------------------------------------------------------

def test_panel_seconds_are_credited_per_panel(tmp_path):
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    _add_mosaic(cfg, store)
    by = {p.target.name: p for p in store.projects.values()}
    assert store.record_accepted_sub("M31 Core P2", "L", 300, rig="rc16")
    # a container-named sub counts for its panel too (PS-78)
    assert store.record_accepted_sub(
        "M31 Core P2 imaging (repeats while safe and up)_Container", "L", 150,
        rig="rc16")
    l2 = next(e for e in by["M31 Core P2"].exposure_plans
              if e.filter_type == FilterType.LUMINANCE)
    assert l2.acquired_s == 450 and l2.acquired == 1
    for name in ("M31 Core P1", "M31 Core P3", "M31 Core P4"):
        assert all(e.acquired_s == 0 for e in by[name].exposure_plans)


def test_plain_m31_sub_never_lands_on_a_panel(tmp_path):
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    _add_mosaic(cfg, store, companion=None)
    # no M31 RC16 goal: an "M31" RC16 sub matches nothing (it used to hit
    # "M31 Core P1" by substring)
    assert not store.record_accepted_sub("M31", "L", 300, rig="rc16")
    assert all(e.acquired_s == 0 for p in store.projects.values()
               for e in p.exposure_plans)


def test_p1_never_credits_p10(tmp_path):
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    _add_mosaic(cfg, store, name="Veil", rows=3, cols=4, companion=None)
    by = {p.target.name: p for p in store.projects.values()}
    assert store.record_accepted_sub("Veil P1", "L", 300, rig="rc16")
    assert by["Veil P1"].exposure_plans[0].acquired_s == 300
    assert by["Veil P10"].exposure_plans[0].acquired_s == 0


def test_piggy_sub_during_a_panel_credits_the_companion(tmp_path):
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    m31 = _m31_goal(store)
    _add_mosaic(cfg, store)
    assert store.record_accepted_sub("M31 Core P1", "OSC", 120,
                                     rig="piggyback")
    osc = m31.exposure_plans[0]
    assert osc.rig == "piggyback" and osc.acquired_s == 120


def test_goal_sync_credits_panels_and_the_companion(tmp_path):
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    m31 = _m31_goal(store)
    _add_mosaic(cfg, store)
    rows = [
        {"rig": "rc16", "file": "LIGHT/a.fits", "time": "2026-10-11T03:00:00",
         "target": "M31 Core P1", "filter": "L", "exp_s": 300,
         "passed_qa": True},
        {"rig": "rc16", "file": "LIGHT/b.fits", "time": "2026-10-11T03:06:00",
         "target": "M31 Core P1", "filter": "R", "exp_s": 300,
         "passed_qa": True},
        {"rig": "piggyback", "file": "LIGHT/c.fits",
         "time": "2026-10-11T03:01:00", "target": "M31 Core P1",
         "filter": "OSC", "exp_s": 120, "passed_qa": True},
        {"rig": "piggyback", "file": "LIGHT/d.fits",
         "time": "2026-10-11T03:03:00", "target": "Andromeda Galaxy",
         "filter": "OSC", "exp_s": 120, "passed_qa": True}]
    (runs.runs_dir(cfg) / "2026-10-10_subs.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    saved = app._store
    app._store = store
    try:
        runs.sync_goal_progress(cfg)
    finally:
        app._store = saved
    p1 = next(p for p in store.projects.values()
              if p.target.name == "M31 Core P1")
    got = {e.filter_type.value: e.acquired_s for e in p1.exposure_plans}
    assert got == {"L": 300, "R": 300, "G": 0, "B": 0}
    assert m31.exposure_plans[0].acquired_s == 240


# --- night planner: in order ---------------------------------------------------------

def _owe(proj, frac):
    for e in proj.exposure_plans:
        e.acquired_s = e.count * e.exposure_seconds * frac
        e.acquired = e.subs_from_seconds(e.acquired_s)


def test_tonight_panels_in_order():
    class P:
        def __init__(self, i, owed):
            self.mosaic = {"id": "m", "panel": i}
            self.exposure_plans = []
            self.owed = owed
    # rc16_owed_hours reads the plans; drive it through a stub
    ps = [P(i, h) for i, h in ((3, 4.0), (1, 4.0), (2, 4.0), (4, 4.0))]
    orig = mz.rc16_owed_hours
    mz.rc16_owed_hours = lambda p: p.owed
    try:
        adm, held = mz.tonight_panels(ps, 8.0)
        assert [p.mosaic["panel"] for p in adm] == [1, 2]
        assert [p.mosaic["panel"] for p in held] == [3, 4]
        adm, _ = mz.tonight_panels(ps, 20.0)
        assert [p.mosaic["panel"] for p in adm] == [1, 2, 3, 4]
        adm, held = mz.tonight_panels(ps, 1.0)   # always the first one
        assert [p.mosaic["panel"] for p in adm] == [1]
    finally:
        mz.rc16_owed_hours = orig


def test_planner_finishes_panels_in_order(tmp_path):
    cfg = _cfg(tmp_path, moon_aware_planning=False)
    store = ProjectStore(cfg)
    _add_mosaic(cfg, store, companion=None)
    ts = plan_night_sequence(list(store.projects.values()), cfg, NIGHT)
    names = [t.name for t in ts]
    assert names == ["M31 Core P1", "M31 Core P2"]
    assert [t.repeat_while_up for t in ts] == [False, True]
    p1 = ts[0]
    assert sum(e.count * e.exposure_seconds for e in p1.exposures) == 4 * 3600
    assert "panel 1 of 4" in p1.mosaic_note
    assert "M31 Core P3" in p1.mosaic_note          # held for later
    # P1 and P2 done: P3 and P4 come up, still in order
    for p in store.projects.values():
        if p.mosaic["panel"] in (1, 2):
            _owe(p, 1.0)
    ts = plan_night_sequence(list(store.projects.values()), cfg, NIGHT)
    assert [t.name for t in ts] == ["M31 Core P3", "M31 Core P4"]


def test_planner_keeps_a_half_done_panel_first(tmp_path):
    cfg = _cfg(tmp_path, moon_aware_planning=False)
    store = ProjectStore(cfg)
    _add_mosaic(cfg, store, companion=None)
    p1 = next(p for p in store.projects.values() if p.mosaic["panel"] == 1)
    _owe(p1, 0.75)                                  # 1 h left
    ts = plan_night_sequence(list(store.projects.values()), cfg, NIGHT)
    assert [t.name for t in ts][:2] == ["M31 Core P1", "M31 Core P2"]
    assert ts[0].repeat_while_up is False
    assert sum(e.count * e.exposure_seconds for e in ts[0].exposures) == 3600


# --- sequence generator ---------------------------------------------------------------

def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_generator_one_container_per_panel_centered_on_it(tmp_path):
    cfg = _cfg(tmp_path, moon_aware_planning=False)
    store = ProjectStore(cfg)
    _add_mosaic(cfg, store, companion=None)
    ts = plan_night_sequence(list(store.projects.values()), cfg, NIGHT)
    seq = json.loads(nsj.generate_nina_json(build_sequence_for_night("T", ts)))
    dsos = [d for d in _walk(seq)
            if "DeepSkyObjectContainer" in d.get("$type", "")]
    assert [d["Name"] for d in dsos] == ["M31 Core P1", "M31 Core P2"]
    for d, t in zip(dsos, ts):
        c = d["Target"]["InputCoordinates"]
        ra = c["RAHours"] + c["RAMinutes"] / 60 + c["RASeconds"] / 3600
        dec = c["DecDegrees"] + c["DecMinutes"] / 60 + c["DecSeconds"] / 3600
        assert ra == pytest.approx(t.ra_hours, abs=1e-5)
        assert dec == pytest.approx(t.dec_degrees, abs=1e-5)
        centers = [x for x in _walk(d)
                   if "Platesolving.Center" in x.get("$type", "")]
        assert len(centers) == 1 and centers[0]["Inherited"] is True
        inner = next(x for x in d["Items"]["$values"]
                     if (x.get("Name") or "").endswith(nsj.TARGET_IMAGING_SUFFIX))
        conds = json.dumps(inner["Conditions"])
        assert ("LoopCondition" in conds) is (not t.repeat_while_up)
        notes = [x.get("Text", "") for x in d["Items"]["$values"]
                 if "Annotation" in x.get("$type", "")]
        assert any("panel" in n for n in notes)
    res = lint(seq, guided=None)
    assert res.ok, [f"{f.rule}: {f.detail}" for f in res.findings]


def test_ordinary_target_keeps_the_repeat_loop():
    from photonscript.shared.models import ExposurePlan, NinaSequenceTarget
    t = NinaSequenceTarget(name="Crescent", ra_hours=20.2, dec_degrees=38.4,
                           exposures=[ExposurePlan(filter_type=FilterType.HA,
                                                   exposure_seconds=600,
                                                   count=6)])
    c = nsj._build_target_container(t, 30.0)
    inner = next(x for x in c["Items"]["$values"]
                 if (x.get("Name") or "").endswith(nsj.TARGET_IMAGING_SUFFIX))
    assert "LoopCondition" not in json.dumps(inner["Conditions"])
    assert not any("Annotation" in x.get("$type", "")
                   for x in c["Items"]["$values"])


# --- campaign -------------------------------------------------------------------------

def test_campaign_takes_panels_in_order_and_credits_the_companion(tmp_path):
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    m31 = _m31_goal(store)
    store.update(m31.id, priority=10)      # the panels claim the mount first
    _add_mosaic(cfg, store)
    c = campaign.build_campaign(cfg, store, now=datetime(2026, 10, 8, 20),
                                days=6, with_calibration=False,
                                with_season=False)
    seen, osc_passenger = [], 0.0
    for n in c["nights"]:
        for a in n["assigned"]:
            if a["goal"].startswith("M31 Core") and a["goal"] not in seen:
                seen.append(a["goal"])
            if a["goal"] == "Andromeda Galaxy" and a["passenger"]:
                osc_passenger += a["hours"]
    assert seen and seen == sorted(seen)            # P1, then P2, ...
    assert seen[0] == "M31 Core P1"
    assert osc_passenger > 0                       # the Piggy rides the panels
    by = {g["name"]: g for g in c["goals"]}
    assert "_prev" not in by["M31 Core P2"] and "_companion" not in by["M31 Core P2"]


# --- API, dashboard, Targets ------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    monkeypatch.setattr(app, "_store", None)
    monkeypatch.setattr(app, "_projects", {})
    monkeypatch.setattr(app, "_dashboard_cache", {})
    _m31_goal(app.get_store())
    return TestClient(app.app)


def test_api_suggest_dry_run_create_patch_delete(client):
    sug = client.get("/api/mosaics/suggest?name=M31").json()
    assert (sug["rows"], sug["cols"], sug["hours_per_panel"],
            sug["exposure_s"]) == (2, 2, 4.0, 300)
    r = client.post("/api/mosaics", json={**sug, "dry_run": True})
    assert r.status_code == 200 and r.json()["dry_run"]
    assert r.json()["companion"]["name"] == "Andromeda Galaxy"
    assert not client.get("/api/mosaics").json()["mosaics"]   # nothing saved
    r = client.post("/api/mosaics", json=sug)
    assert r.status_code == 200 and len(r.json()["created"]) == 4
    assert all(pid in app._projects for pid in r.json()["created"])
    assert client.post("/api/mosaics", json=sug).status_code == 400
    (m,) = client.get("/api/mosaics").json()["mosaics"]
    assert m["n_panels"] == 4 and m["next_panel"] == "M31 Core P1"
    assert m["companion"]["name"] == "Andromeda Galaxy"
    assert m["preview"]["piggy"]["max_shift_arcmin"] == pytest.approx(12.5,
                                                                      abs=0.2)
    assert len(m["panels"][0]["corners"]) == 4
    r = client.patch(f"/api/mosaics/{m['id']}", json={"hours_per_panel": 2,
                                                      "priority": 70})
    assert r.status_code == 200
    (m,) = client.get("/api/mosaics").json()["mosaics"]
    assert m["goal_h"] == 8.0 and m["priority"] == 70
    assert m["hours_per_panel"] == 2.0
    projs = client.get("/api/projects2").json()
    assert sum(1 for p in projs if p.get("mosaic")) == 4
    assert client.delete(f"/api/mosaics/{m['id']}").status_code == 200
    assert not client.get("/api/mosaics").json()["mosaics"]
    assert [p["target"]["name"] for p in client.get("/api/projects2").json()] \
        == ["Andromeda Galaxy"]


def test_api_preview_and_pages(client):
    d = client.get("/api/mosaics/preview?name=X&ra_hours=0.7123&"
                   "dec_degrees=41.27&rows=2&cols=2&rotation=125&"
                   "major_axis_pa=35").json()
    assert d["rotation"]["pa_deg"] == 125.0
    assert d["rotation"]["major_axis"]["grid_along_major_axis"]
    assert d["layout"]["span_w_arcmin"] == pytest.approx(45.3, abs=0.1)
    assert "hips2fits" in d["preview"]["url"]
    assert client.get("/api/mosaics/preview?rotation=sideways").status_code \
        == 400
    assert client.get("/mosaic").status_code == 200
    assert client.get("/api/mosaic/plan?ra_hours=0.7&dec_degrees=41").json()[
        "panels"]                                  # v1 kept
    page = client.get("/").text
    assert "mosaic_preview.js" in page and "function mosaicCard" in page
    # two raw newlines inside JS string literals (PS-122 badge, PS-124 Add
    # box) broke the whole goal-card script block; they are \n escapes now
    assert ".join('\\n') || 'Nothing owed'" in page
    assert "is not in the catalog.\\n\"" in page


def test_targets_cards_name_their_mosaic(tmp_path):
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    _add_mosaic(cfg, store, companion=None)
    cards = sub_index.targets(cfg, store.projects.values())
    panels = [c for c in cards if c.get("mosaic")]
    assert sorted(c["mosaic"]["panel"] for c in panels) == [1, 2, 3, 4]
    assert {c["mosaic"]["name"] for c in panels} == {"M31 Core"}


def test_new_sources_are_ascii():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
    for rel in ("mosaic.py", "routers/mosaic.py", "templates/mosaic.html",
                "static/js/mosaic_preview.js"):
        text = (root / rel).read_text(encoding="utf-8")
        assert text.isascii(), rel
