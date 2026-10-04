"""PS-81: Targets tab data layer (sub_index), reference image (refimage) and
the routers/targets.py endpoints."""

import json

import pytest

from photonscript.scheduler import refimage, runs, sub_index
from photonscript.shared import qa_rules
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (CelestialTarget, ExposurePlan,
                                        FilterType, ImagingProject)

NIGHT = "2026-09-26"
HEART_C = "Heart Nebula imaging (repeats while safe and up)_Container"
CATS_C = "Cat's Eye Nebula imaging (repeats while safe and up)_Container"
OSC_C = "OSC_LIGHT_LOOP_Container"

# Every reason string the graders wrote before the PS-21 scorecard
# (image_validator, the agent, runs._fast_grade, flag_hfr_outliers,
# set_manual_qa, PS-71 signatures) -> the check id it maps to.
LEGACY = [
    ("only 3 stars", "stars"),
    ("Only 2 stars detected (minimum 5)", "stars"),
    ("6000 stars > 5000 (defocus/false detections)", "stars"),
    ("elongated stars (ecc 0.81 > 0.7)", "ecc"),
    ("Eccentricity 0.78 > 0.6 (trailing/drift)", "ecc"),
    ("tracking jump: 40% of stars doubled", "tracking_jump"),
    ("HFR 11.2 > 10px (out of focus)", "hfr"),
    ("HFR 11.2px > 10px (out of focus)", "hfr"),
    ("HFR outlier: 6.1 vs night median 3.9 (x1.4 limit)", "hfr_rel"),
    ('FWHM 5.2" > 4.0"', "fwhm"),
    ('Tracking RMS 2.10" > 1.5"', "guide_rms"),
    ("sensor 31C vs setpoint 0C (cooler failure)", "temp"),
    ("sensor 12C above 10C limit; camera was set to 0C", "temp"),
    ("dark-frame signature: background 258 ADU at the bias floor and stars "
     '0.40" under the 1" physical floor (hot pixels, not stars): roof closed '
     "/ parked", "roof"),
    ('stars 0.40" under the 1" physical floor: hot pixels, not stars', "roof"),
    ("shot while the safety monitor read UNSAFE (300 s of 300 s): roof "
     "closed / parked", "roof"),
    ("rejected manually", "manual"),
    ("rejected manually: ecc", "manual"),
]


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _write(cfg, rows, date=NIGHT, mode="w"):
    with open(runs.runs_dir(cfg) / f"{date}_subs.jsonl", mode,
              encoding="utf-8") as f:
        f.write("".join(json.dumps(r) + "\n" for r in rows))


def _row(target, time, *, rig="rc16", filt="Ha", ok=True, reviewed=None,
         exp=300, name=None, hfr=2.0, ecc=0.4, reason="", **kw):
    name = name or f"{rig}_{time[11:19].replace(':', '-')}.fits"
    r = {"file": f"LIGHT/{name}", "abs_path": f"C:/NINA/{NIGHT}/LIGHT/{name}",
         "time": time, "target": target, "filter": filt, "exp_s": exp,
         "hfr": hfr, "ecc": ecc, "stars": 300, "passed_qa": ok,
         "reviewed": ok if reviewed is None else reviewed, "reason": reason}
    if rig is not None:
        r["rig"] = rig
    r.update(kw)
    return r


def _proj(pid, name, cid="", plans=(("Ha", 300, 20),), active=True, prio=50,
          ra=2.55, dec=61.45, size=60.0):
    return ImagingProject(
        id=pid, active=active, priority=prio,
        target=CelestialTarget(name=name, catalog_id=cid, ra_hours=ra,
                               dec_degrees=dec, angular_size_arcmin=size,
                               object_type="emission nebula"),
        exposure_plans=[ExposurePlan(filter_type=FilterType(f),
                                     exposure_seconds=e, count=n)
                        for f, e, n in plans])


PROJECTS = [_proj("p1", "Heart Nebula", "IC 1805", prio=80),
            _proj("p2", "Cat's Eye Nebula", "NGC 6543", ra=17.976,
                  dec=66.633, size=0.9),
            _proj("p3", "Crescent Nebula", "NGC 6888", active=False)]


# --- reason codes -------------------------------------------------------------

@pytest.mark.parametrize("text,code", LEGACY)
def test_legacy_reason_strings_map_to_check_ids(text, code):
    assert sub_index.legacy_reason_codes(text) == [code]


def test_legacy_multi_part_and_unknown():
    assert sub_index.legacy_reason_codes(
        "only 3 stars; HFR 11 > 10px (out of focus); only 3 stars") == [
        "stars", "hfr"]
    assert sub_index.legacy_reason_codes("Could not load image data") == [
        "other"]
    assert sub_index.legacy_reason_codes("") == []


def _failing_reasons():
    """Reason text of every check the current rules can fail."""
    cfg = PhotonScriptConfig(_env_file=None, qa_guide_rms_mode="fail",
                             qa_guide_lock_mode="fail")
    ctx = qa_rules.context(cfg, "rc16", night={"hfr_median": 2.0, "n_hfr": 9})
    cases = [dict(ecc=0.95), dict(hfr=40.0), dict(hfr=3.5),
             dict(fwhm_arcsec=9.0), dict(stars=2), dict(stars=99999),
             dict(ccd_temp=30.0), dict(guide_lock="non-star"),
             dict(guide_rms=9.0, guide_state="guiding"),
             dict(doubled_frac=0.9), dict(pointing_offset_arcmin=30.0)]
    out = []
    for kw in cases:
        m = {"hfr": 2.0, "stars": 300, "ecc": 0.3, **kw}
        for c in qa_rules.evaluate(m, ctx).checks:
            if c.status == qa_rules.FAIL and c.reason:
                out.append((c.id, c.reason))
    return out


def test_current_scorecard_texts_also_map_by_regex():
    """A record whose scorecard is lost still lands on the right check."""
    seen = set()
    for cid, text in _failing_reasons():
        assert sub_index.legacy_reason_codes(text) == [cid], text
        seen.add(cid)
    assert {"ecc", "hfr", "hfr_rel", "fwhm", "stars", "temp", "guide_lock",
            "guide_rms", "tracking_jump", "pointing"} <= seen


def test_scorecard_ids_win_over_regex():
    rec = {"passed_qa": False, "reason": "only 3 stars",
           "drivers": ["roof", "stars"]}
    assert sub_index.reason_codes(rec, "rejected") == ["roof", "stars"]
    rec = {"passed_qa": False, "reason": "whatever",
           "scorecard": {"rows": [["ecc", 0.9, 0.7, "fail", "x"],
                                  ["hfr", 2, 10, "pass"]]}}
    assert sub_index.reason_codes(rec, "rejected") == ["ecc"]
    rec = {"passed_qa": False, "manual_qa": True, "reviewed": True,
           "reason": "rejected manually: ecc", "manual_reason": "ecc",
           "drivers": []}
    assert sub_index.reason_codes(rec, "rejected") == ["manual", "ecc"]
    assert sub_index.reason_codes({"passed_qa": True}, "approved") == []


def test_verdicts():
    v = sub_index.verdict_of
    assert v({"passed_qa": True, "reviewed": False}) == ("pending", "auto")
    assert v({"passed_qa": True, "reviewed": True,
              "review_source": "auto"}) == ("approved", "auto")
    assert v({"passed_qa": True, "reviewed": True}) == ("approved", "manual")
    assert v({"passed_qa": False}) == ("rejected", "auto")
    assert v({"passed_qa": False, "manual_qa": True,
              "review_source": "manual"}) == ("rejected", "manual")
    assert v({"passed_qa": True, "reviewed": False,
              "review_source": "manual"}) == ("pending", "manual")


# --- index ----------------------------------------------------------------------

def _seed(cfg):
    _write(cfg, [
        _row(HEART_C, f"{NIGHT}T02:00:00", filt="OIII", name="h1.fits",
             hfr=2.4),
        _row(HEART_C, f"{NIGHT}T02:10:00", filt="OIII", name="h2.fits",
             ok=False, reason="only 3 stars"),
        _row(HEART_C, f"{NIGHT}T02:20:00", filt="OIII", name="h3.fits",
             reviewed=False, hfr=1.9),
        _row("Heart Nebula", f"{NIGHT}T02:30:00", filt="OIII", name="h4.fits",
             hfr=1.5, ecc=0.9),                         # sharp but trailed
        _row("Heart Nebula", f"{NIGHT}T02:40:00", filt="OIII", name="h5.fits",
             ok=False, reason="rejected manually", manual_qa=True,
             review_source="manual"),
        _row("NGC 6543", f"{NIGHT}T04:00:00", filt="Ha", name="c1.fits",
             rig=None),                                 # missing rig = rc16
        _row(CATS_C, f"{NIGHT}T04:10:00", filt="Ha", name="c2.fits",
             ok=False, drivers=["roof"], reason="shot while ... parked"),
        _row(OSC_C, f"{NIGHT}T04:01:00", rig="piggyback", filt="OSC",
             name="p1.fits", reviewed=False),
        _row(OSC_C, f"{NIGHT}T04:02:00", rig="piggyback", filt="OSC",
             name="p2.fits", ok=False, reason="only 1 stars"),
        _row("Pelican Nebula", f"{NIGHT}T05:00:00", filt="Ha", name="x1.fits"),
    ])
    _write(cfg, [_row("Pelican Nebula", "2026-09-20T05:00:00", filt="Ha",
                      name="x0.fits")], date="2026-09-20")


def test_rows_normalize_names_rigs_and_verdicts(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    rows = sub_index.all_rows(cfg, PROJECTS)
    by = {r["file"].split("/")[-1]: r for r in rows}
    h1 = by["h1.fits"]
    assert h1["target"] == "Heart Nebula" and h1["target_raw"] == HEART_C
    assert h1["project_id"] == "p1" and h1["key"] == f"{NIGHT}|rc16|LIGHT/h1.fits"
    assert h1["verdict"] == "approved" and h1["pointing"] is None
    assert h1["thumb"] == f"/api/runs/{NIGHT}/thumb?file=LIGHT%2Fh1.fits&w=264"
    assert by["h2.fits"]["reason_codes"] == ["stars"]
    assert by["h3.fits"]["verdict"] == "pending"
    assert by["h5.fits"]["reason_codes"] == ["manual"]
    assert by["h5.fits"]["verdict_by"] == "manual"
    assert by["c1.fits"]["rig"] == "rc16"
    assert by["c1.fits"]["target"] == "Cat's Eye Nebula"   # catalog id
    assert by["c2.fits"]["reason_codes"] == ["roof"]
    assert by["p1.fits"]["target"] == "?" and by["p1.fits"]["target_key"] == "?"
    assert by["x1.fits"]["project_id"] is None
    assert not any("other" in r["reason_codes"] for r in rows)


def test_hdr_short_flag(tmp_path):
    cfg = _cfg(tmp_path)
    p = _proj("p9", "Cat's Eye Nebula", "NGC 6543", plans=(("OIII", 600, 20),))
    p.exposure_plans[0].hdr_short_seconds = 30
    p.exposure_plans[0].hdr_short_count = 40
    _write(cfg, [_row("Cat's Eye Nebula", f"{NIGHT}T01:00:00", filt="OIII",
                      exp=30, name="s.fits"),
                 _row("Cat's Eye Nebula", f"{NIGHT}T01:10:00", filt="OIII",
                      exp=600, name="l.fits")])
    rows = {r["file"]: r["hdr_short"] for r in sub_index.all_rows(cfg, [p])}
    assert rows == {"LIGHT/s.fits": True, "LIGHT/l.fits": False}


def test_memo_reuses_and_invalidates_on_new_line(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    a = sub_index.all_rows(cfg, PROJECTS)
    assert sub_index.all_rows(cfg, PROJECTS) is a
    calls = []
    real = runs._load_subs
    monkeypatch.setattr(runs, "_load_subs",
                        lambda c, d: calls.append(d) or real(c, d))
    _write(cfg, [_row("Heart Nebula", f"{NIGHT}T06:00:00", name="new.fits")],
           mode="a")
    b = sub_index.all_rows(cfg, PROJECTS)
    assert b is not a and len(b) == len(a) + 1 and calls
    # a project change (rename) also rebuilds
    c = sub_index.all_rows(cfg, PROJECTS[:1])
    assert c is not b


def test_rows_filters_and_sort(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    r = sub_index.rows(cfg, PROJECTS, target=HEART_C)    # container name works
    assert len(r) == 5
    assert [x["file"] for x in sub_index.rows(
        cfg, PROJECTS, target="Heart Nebula", verdict="rejected",
        reason="stars")] == ["LIGHT/h2.fits"]
    hfr = [x["metrics"]["hfr"] for x in sub_index.rows(
        cfg, PROJECTS, target="heart nebula", sort="hfr")]
    assert hfr == sorted(hfr)
    assert len(sub_index.rows(cfg, PROJECTS, target="?")) == 2
    assert len(sub_index.rows(cfg, PROJECTS, night="2026-09-20")) == 1
    assert len(sub_index.rows(cfg, PROJECTS, rig="piggyback")) == 2


def test_best_sub_skips_trailed_and_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    rs = sub_index.rows(cfg, PROJECTS, target="Heart Nebula")
    best = sub_index.best_subs(rs, cfg)
    # h4 has the lowest HFR but ecc 0.9 > 0.7; h3 (pending, 1.9) wins
    assert best == {"rc16|OIII": f"{NIGHT}|rc16|LIGHT/h3.fits"}


def test_targets_cards_order_and_unattributed(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg)
    cards = sub_index.targets(cfg, PROJECTS)
    names = [c["name"] for c in cards]
    assert names[:2] == ["Heart Nebula", "Cat's Eye Nebula"]   # active, prio
    assert names[-1] == "?" and cards[-1]["unattributed"]
    assert names.index("Pelican Nebula") < names.index("Crescent Nebula")
    heart = cards[0]
    assert heart["counts"]["approved"] == 2 and heart["counts"]["pending"] == 1
    assert heart["counts"]["rejected"] == 2 and heart["counts"]["total"] == 5
    assert heart["top_reason"]["code"] in ("stars", "manual")
    assert heart["facts"]["catalog_id"] == "IC 1805"
    pel = next(c for c in cards if c["name"] == "Pelican Nebula")
    assert pel["nights"] == 2 and pel["last_night"] == NIGHT
    assert pel["facts"]["catalog_id"]                  # from the seasonal list
    cres = next(c for c in cards if c["name"] == "Crescent Nebula")
    assert cres["active"] is False and cres["counts"]["total"] == 0


def test_target_detail(tmp_path):
    cfg = _cfg(tmp_path, desktop_library_dir="D:/lib")
    _seed(cfg)
    d = sub_index.target_detail(cfg, HEART_C, PROJECTS)
    assert d["target"] == "Heart Nebula" and d["found"]
    assert d["raw_names"] == [HEART_C]
    assert d["totals"]["approved"] == 2 and d["totals"]["hours"]["approved"] \
        == round(600 / 3600, 2)
    assert d["nights"][0]["date"] == NIGHT and d["nights"][0]["total"] == 5
    assert [r["code"] for r in d["reasons"]] == ["stars", "manual"] or \
        [r["code"] for r in d["reasons"]] == ["manual", "stars"]
    assert d["reasons"][0]["example"]
    assert d["by_filter_rig"][0]["filter"] == "OIII"
    assert d["facts"]["constellation"]                 # filled from astropy
    assert d["facts"]["ra_text"].startswith("02h")
    assert d["goal"]["plans"][0]["filter"] == "Ha"
    assert d["desktop_path"].replace("\\", "/") == "D:/lib/Heart Nebula"
    u = sub_index.target_detail(cfg, "?", PROJECTS)
    assert u["unattributed"] and u["totals"]["total"] == 2 and u["facts"] is None
    assert not sub_index.target_detail(cfg, "Nope Nebula", PROJECTS)["found"]


# --- reference image --------------------------------------------------------------

JPEG = b"\xff\xd8\xff\xe0" + b"0" * 64


def test_rig_fov_matches_headers(tmp_path):
    cfg = _cfg(tmp_path)
    rc, pb = refimage.rig_fovs(cfg)
    assert (rc["w_arcmin"], rc["h_arcmin"]) == (24.9, 16.7)  # 0.24"/px
    assert (pb["w_arcmin"], pb["h_arcmin"]) == (133.8, 89.6)  # 1.29"/px
    assert rc["enabled"] and pb["enabled"] is False
    cfg2 = _cfg(tmp_path, piggyback_sensor_width_px=3000, piggyback_enabled=True)
    pb2 = refimage.rig_fov(cfg2, "piggyback")
    assert pb2["width_px"] == 3000 and pb2["height_px"] == 4168


def test_view_fov():
    assert refimage.view_fov("wide", 0.9) == 3.0
    assert refimage.view_fov("close", 0.9) == 0.62
    assert refimage.view_fov("close", 60) == 2.0


def test_reference_fetches_once_and_caches(tmp_path):
    cfg = _cfg(tmp_path)
    calls = []

    def fetch(url):
        calls.append(url)
        return JPEG
    a = refimage.reference(cfg, "Cat's Eye Nebula", 269.6, 66.6, 0.62,
                           fetch=fetch)
    b = refimage.reference(cfg, "Cat's Eye Nebula", 269.6, 66.6, 0.62,
                           fetch=fetch)
    assert len(calls) == 1 and a == b
    assert "hips=CDS%2FP%2FDSS2%2Fcolor" in calls[0] and "fov=0.6200" in calls[0]
    assert a["source"] == "dss2" and a["width"] == 1000 and a["credit"]
    assert (tmp_path / "refimg" / "cat_s_eye_nebula_0.62.jpg").read_bytes() == JPEG


def test_reference_failure_cached_for_an_hour(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    calls = []

    def boom(url):
        calls.append(url)
        raise OSError("offline")
    assert refimage.reference(cfg, "Heart", 38.0, 61.4, 3.0, fetch=boom) is None
    assert refimage.reference(cfg, "Heart", 38.0, 61.4, 3.0, fetch=boom) is None
    assert len(calls) == 1
    assert refimage.reference(cfg, "Heart2", 38.0, 61.4, 3.0,
                              fetch=lambda u: b"<html>err") is None
    t0 = __import__("time").time()
    monkeypatch.setattr(refimage.time, "time", lambda: t0 + 3700)
    assert refimage.reference(cfg, "Heart", 38.0, 61.4, 3.0,
                              fetch=lambda u: JPEG)["source"] == "dss2"


def test_gnomonic_center_and_east_left():
    assert refimage.gnomonic_px(100, 30, 100, 30, 1.0) == (500, 333.5)
    east = refimage.gnomonic_px(100.2, 30, 100, 30, 1.0)
    north = refimage.gnomonic_px(100, 30.2, 100, 30, 1.0)
    assert east[0] < 500 and north[1] < 333.5
    # 0.2 deg north = 0.2 x 1000 px/deg
    assert abs((333.5 - north[1]) - 200) < 0.1
    assert refimage.gnomonic_px(280, -30, 100, 30, 1.0) is None


# --- endpoints ------------------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    _seed(cfg)
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(app, "_store", type("S", (), {
        "projects": {p.id: p for p in PROJECTS}})())
    from fastapi.testclient import TestClient
    return cfg, TestClient(app.app)


def test_pages_render_with_targets_nav(api):
    _cfg_, client = api
    t = client.get("/targets")
    assert t.status_code == 200 and "/api/targets" in t.text
    assert 'href="/targets"' not in t.text                # own pill hidden
    r = client.get("/runs")
    assert 'href="/targets" class="nav-pill"' in r.text
    p = client.get("/target?name=Heart%20Nebula")
    assert p.status_code == 200 and "sub_tiles.js" in p.text
    assert 'href="/targets" class="nav-pill"' in p.text


def test_api_targets_detail_subs(api):
    _cfg_, client = api
    ts = client.get("/api/targets").json()["targets"]
    assert ts[0]["name"] == "Heart Nebula"
    d = client.get("/api/targets/detail", params={"name": HEART_C}).json()
    assert d["target"] == "Heart Nebula"
    assert client.get("/api/targets/detail",
                      params={"name": "Nope"}).status_code == 404
    s = client.get("/api/subs", params={"target": "Heart Nebula", "limit": 2,
                                        "offset": 1}).json()
    assert s["total"] == 5 and len(s["rows"]) == 2 and s["offset"] == 1
    s = client.get("/api/subs", params={"target": "Heart Nebula",
                                        "verdict": "pending"}).json()
    assert [r["file"] for r in s["rows"]] == ["LIGHT/h3.fits"]


def test_api_target_history_shape(api):
    _cfg_, client = api
    h = client.get("/api/target/history", params={"name": HEART_C}).json()
    assert set(h) == {"target", "nights", "totals", "library_dir",
                      "desktop_path", "desktop_hint"}
    assert h["totals"]["accepted"] == 3 and h["totals"]["rejected"] == 2
    row = h["nights"][0]["subs"][0]
    assert set(row) == {"time", "filter", "exp_s", "hfr", "ecc", "stars",
                        "passed", "reviewed", "reason", "in_library", "file",
                        "target_raw"}
    assert row["time"] == "02:00" and row["file"] == "h1.fits"


def test_api_readiness_for_target(api, monkeypatch):
    from photonscript.scheduler import calibration as cal
    _cfg_, client = api
    monkeypatch.setattr(cal, "calibration_health", lambda c: {
        "FLAT": {"detail": {}}, "BIAS": {"count_latest": 0}})
    monkeypatch.setattr(cal, "count_matching_darks", lambda c, e, **k: 0)
    r = client.get("/api/targets/readiness", params={"name": HEART_C}).json()
    assert r["project"] and r["target"] == "Heart Nebula" and r["ready"] is False
    r = client.get("/api/targets/readiness", params={"name": "Pelican Nebula"})
    assert r.json()["project"] is False


def test_api_refimage_meta_dss_then_fallbacks(api, monkeypatch):
    cfg, client = api
    monkeypatch.setattr(refimage, "_http_get", lambda url: JPEG)
    m = client.get("/api/targets/refimage/meta",
                   params={"name": "Cat's Eye Nebula", "view": "close"}).json()
    assert m["source"] == "dss2" and m["fov_deg"] == 0.62
    assert m["image_url"].startswith("/api/targets/refimage?")
    img = client.get(m["image_url"])
    assert img.status_code == 200 and img.content == JPEG
    assert [r["rig"] for r in m["rigs"]] == ["rc16", "piggyback"]

    def offline(url):
        raise OSError("offline")
    monkeypatch.setattr(refimage, "_http_get", offline)
    m = client.get("/api/targets/refimage/meta",
                   params={"name": "Heart Nebula", "view": "close"}).json()
    assert m["source"] == "own_sub" and m["rig"] == "rc16"
    assert "&w=1000" in m["image_url"] and m["approximate"]
    m = client.get("/api/targets/refimage/meta",
                   params={"name": "Crescent Nebula", "view": "wide"}).json()
    assert m["source"] == "none" and m["fov_deg"] == 3.0
    assert client.get("/api/targets/refimage",
                      params={"name": "Crescent Nebula"}).status_code == 404
    f = client.get("/api/rigs/fov").json()["rigs"]
    assert f[0]["w_arcmin"] == 24.9


def test_api_live_marker(api, monkeypatch):
    from photonscript.scheduler import app
    _cfg_, client = api

    async def rigs():
        return {"rigs": [{"rig": "rc16", "target": CATS_C,
                          "session_state": "imaging",
                          "devices": {"mount": {"ra": 17.976, "dec": 66.733,
                                                "tracking": True}}}]}
    monkeypatch.setattr(app, "api_rigs", rigs)
    on = client.get("/api/targets/live", params={"name": "Cat's Eye Nebula"}).json()
    assert on["active"] and abs(on["sep_arcmin"] - 6.0) < 0.1
    off = client.get("/api/targets/live", params={"name": "Heart Nebula"}).json()
    assert off["active"] is False and "sep_arcmin" not in off


def test_separation():
    from photonscript.scheduler.routers.targets import separation_arcmin
    assert separation_arcmin(1.0, 10.0, 1.0, 10.5) == pytest.approx(30.0)
    assert separation_arcmin(0.0, 0.0, 1.0, 0.0) == pytest.approx(900.0)


def test_no_syncthing_on_targets_endpoints(api, monkeypatch):
    _cfg_, client = api

    def boom(*a, **k):
        raise AssertionError("Syncthing called")
    from photonscript.scheduler import app
    monkeypatch.setattr(app, "_syncthing_pending_names", boom)
    for url in ("/api/targets", "/api/targets/detail?name=Heart%20Nebula",
                "/api/subs?target=Heart%20Nebula"):
        assert client.get(url).status_code == 200


def test_reason_examples_match_their_code():
    rows = [{"verdict": "rejected", "reason_codes": ["stars", "hfr"],
             "reasons": ["HFR 11.2px > 10px (out of focus)", "only 3 stars"]},
            {"verdict": "rejected", "reason_codes": ["roof"],
             "reasons": ["shot while the safety monitor read UNSAFE: roof "
                         "closed / parked"]}]
    ex = {r["code"]: r["example"] for r in sub_index._top_reasons(rows)}
    assert ex == {"stars": "only 3 stars",
                  "hfr": "HFR 11.2px > 10px (out of focus)",
                  "roof": rows[1]["reasons"][0]}
