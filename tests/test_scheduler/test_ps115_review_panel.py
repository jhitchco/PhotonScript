"""PS-115: the review panel shows the bars only, plus a target section.

Panel rows come sorted by points lost with n/a and skipped rows marked for
the "not measured" line; /scorecard carries the target / pointing block
(with and without a PS-67 record); the page has no repeated metric text in
the panel header or the caption; every id the page script looks up exists
once; the gnomonic projection is shared with the Targets page, not forked.
"""
import json
import re
from collections import Counter
from pathlib import Path

from photonscript.shared import qa_rules as q
from photonscript.shared import qa_score as qs
from tests.test_scheduler.test_ps108_score import _scored
from tests.test_scheduler.test_ps21_grading import NIGHT, _cfg, _rec, _seed

ROOT = Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
RUNS = ROOT / "templates" / "runs.html"
GEOM = ROOT / "static" / "js" / "sky_geom.js"


def _card(cfg, **kw):
    r = _rec("x", **kw)
    k = q.group_key(r)
    return r, q.evaluate(q.metrics_from_record(r), q.context(cfg, k[0], k[1], k[2]))


# ------------------------------------------------------------ panel rows

def test_panel_rows_sorted_by_points_lost_then_measured_then_not(tmp_path):
    cfg = _cfg(tmp_path)
    rec, c = _card(cfg, ecc=0.62, hfr=9.6)
    rows = qs.panel_rows(rec, q.expand(c.compact()), c.thresholds,
                         c.score.as_dict())
    lost = [r["lost"] for r in rows if r["lost"]]
    assert len(lost) >= 2 and lost == sorted(lost, reverse=True)
    assert rows[0]["id"] == c.score.deductions[0][0]
    tiers = [0 if r["lost"] else 1 if r["measured"] else 2 for r in rows]
    assert tiers == sorted(tiers)                      # lost, measured, n/a
    by = {r["id"]: r for r in rows}
    assert by["sat_px"]["measured"] is False           # graded before PS-108
    assert by["corner_spread"]["measured"] is False
    assert by["stars"]["measured"] is True and by["background"]["measured"] is True
    skipped = [r for r in q.expand(c.compact()) if r["status"] == "skip"]
    for r in skipped:
        if r["id"] in by:
            assert by[r["id"]]["measured"] is False, r["id"]


def test_sort_rows_keeps_the_usual_order_inside_a_tier():
    rows = [{"id": "a", "lost": None, "measured": True},
            {"id": "b", "lost": 2.0, "measured": True},
            {"id": "c", "lost": None, "measured": False},
            {"id": "d", "lost": None, "measured": True},
            {"id": "e", "lost": 7.5, "measured": True}]
    assert [r["id"] for r in qs.sort_rows(rows)] == ["e", "b", "a", "d", "c"]


# ----------------------------------------------------- scorecard target block

def _api(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    from fastapi.testclient import TestClient
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    monkeypatch.setattr(app, "_config", cfg)
    return cfg, TestClient(app.app)


def _pointing(cfg, lines):
    from photonscript.shared import pointing
    p = pointing.sidecar_path(cfg, NIGHT)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def test_scorecard_target_block_with_and_without_pointing(tmp_path, monkeypatch):
    cfg, client = _api(tmp_path, monkeypatch)
    _seed(cfg, [_scored(cfg, "solved", target="M 31"),
                _scored(cfg, "hdr", target="M 31"),
                _scored(cfg, "pig", rig="piggyback", target="M 31", filter="OSC"),
                _scored(cfg, "bare", target="?")])
    _pointing(cfg, [
        {"rig": "rc16", "file": "LIGHT/solved.fits", "src": "solve",
         "mount_ra": 10.70, "mount_dec": 41.30, "solved_ra": 10.69,
         "solved_dec": 41.28, "alt": 61.2, "pier": "West", "ha_h": -1.42,
         "off_target_arcmin": 2.9, "off_target_dir": "NE", "off_target_pa": 40.0,
         "flag": ""},
        {"rig": "rc16", "file": "LIGHT/hdr.fits", "src": "header",
         "mount_ra": 11.0, "mount_dec": 41.0, "alt": 63.0, "pier": "East",
         "off_target_arcmin": 22.4, "off_target_dir": "SE", "flag": "flag"},
        {"rig": "piggyback", "file": "LIGHT/pig.fits", "src": "mount-log",
         "mount_ra": 10.3, "mount_dec": 41.5, "off_target_arcmin": 25.0}])

    def card(f):
        r = client.get(f"/api/runs/{NIGHT}/scorecard", params={"file": f"LIGHT/{f}.fits"})
        assert r.status_code == 200
        return r.json()["target"]
    t = card("solved")
    assert t["name"] == "M 31" and t["link"] == "/target?name=M%2031"
    assert t["filter"] == "Ha" and t["exp_s"] == 300.0
    assert t["frame"] == {"label": "RC16", "w_arcmin": 24.5, "h_arcmin": 16.4}
    p = t["pointing"]
    assert p["src_label"] == "plate solve" and p["confirmed"] is True
    assert (p["ra"], p["dec"], p["at"]) == (10.69, 41.28, "solve")   # solve center
    assert p["off_arcmin"] == 2.9 and p["off_dir"] == "NE"
    assert (p["alt"], p["pier"], p["ha_h"]) == (61.2, "West", -1.42)
    p = card("hdr")["pointing"]
    assert p["src_label"] == "header, unconfirmed" and p["confirmed"] is False
    assert (p["ra"], p["at"], p["flag"]) == (11.0, "mount", "flag")  # mount position
    t = card("pig")
    assert t["frame"]["label"] != "RC16" and t["frame"]["w_arcmin"] > 100
    assert t["pointing"]["src_label"] == "mount log, unconfirmed"
    t = card("bare")                                   # no target, no record
    assert t["name"] is None and t["link"] is None and t["pointing"] is None
    assert t["frame"]["w_arcmin"] == 24.5


def test_target_block_without_a_record_uses_the_graded_offset(tmp_path):
    from photonscript.scheduler.routers.review import target_block
    cfg = _cfg(tmp_path)
    rec = {"rig": "rc16", "file": "LIGHT/a.fits", "target": "NGC 7000",
           "filter": "Ha", "exp_s": 300.0, "pointing_offset_arcmin": 41.0,
           "pointing_src": "header"}
    p = target_block(cfg, NIGHT, rec)["pointing"]
    assert p["off_arcmin"] == 41.0 and p["src_label"] == "header, unconfirmed"
    assert p["ra"] is None and p["at"] is None         # nothing to mark
    rec.pop("pointing_offset_arcmin")
    assert target_block(cfg, NIGHT, rec)["pointing"] is None


# ------------------------------------------------------------- the page

def _page(tmp_path, monkeypatch) -> str:
    _cfg_, client = _api(tmp_path, monkeypatch)
    r = client.get(f"/runs/{NIGHT}")
    assert r.status_code == 200
    return r.text


def _script(html: str) -> str:
    return "\n".join(re.findall(r"<script>(.*?)</script>", html, flags=re.S))


def _func(js: str, name: str) -> str:
    i = js.index(f"function {name}(")
    j = js.find("\n    function ", i + 10)
    k = js.find("\n    async function ", i + 10)
    ends = [x for x in (j, k) if x > 0]
    return js[i:min(ends)] if ends else js[i:]


def test_runs_page_ids_exist_once(tmp_path, monkeypatch):
    html = _page(tmp_path, monkeypatch)
    static = re.sub(r"<script>.*?</script>", "", html, flags=re.S)
    ids = re.findall(r'\bid="([^"]+)"', static)
    dup = [k for k, n in Counter(ids).items() if n > 1]
    assert not dup, dup
    js = _script(html)
    wanted = set(re.findall(r"getElementById\('([A-Za-z]\w*)'\)", js))
    made = set(re.findall(r'id="([A-Za-z]\w*)"', js))        # built in JS strings
    missing = wanted - set(ids) - made
    assert not missing, missing
    for k in ("lbTarget", "lbTargetText", "lbRef", "lbRefImg", "lbRefSvg",
              "lbRefNote", "lbHead", "lbRows", "lbHist", "lbHistNote"):
        assert k in ids, k
    # the target section sits at the top of the panel, before the score line
    assert html.index('id="lbTarget"') < html.index('id="lbHead"') < html.index('id="lbRows"')


def test_panel_header_and_caption_repeat_no_metrics(tmp_path, monkeypatch):
    html = _page(tmp_path, monkeypatch)
    js = _script(html)
    assert "Top deductions" not in html
    panel = _func(js, "loadLbPanel")
    for gone in ("s.hfr", "s.stars", "s.background", "sat_stars_pct", "approve &ge;",
                 "(scored on read)</span>"):
        assert gone not in panel, gone
    assert "not measured: " in panel and "m.measured" in panel
    assert "scored on read" in panel and "head.title" in panel      # tooltip only
    cap = _func(js, "openLb")
    cap = cap[cap.index("lbCaption"):cap.index("lbQaBtn")]
    for gone in ("hfr", "stars", "background", "swamp", "exposure", "sat_stars"):
        assert gone not in cap, gone
    for kept in ("currentDate", "s.time", "s.target", "s.filter", "s.exp_s", "badge(s)"):
        assert kept in cap, kept
    # keyboard flow unchanged
    for key in ("'Escape'", "'ArrowLeft'", "'ArrowRight'", "=== 'x'", "lbZoomStep(0.5)"):
        assert key in js, key


def test_target_section_reuses_refimage_and_the_shared_projection(tmp_path, monkeypatch):
    html = _page(tmp_path, monkeypatch)
    js = _script(html)
    assert "/static/js/sky_geom.js" in html
    assert "SkyGeom.tanPx(" in js and "function tanPx" not in js
    assert "/api/targets/refimage/meta?name=" in js and 'href="/target?name=' in js
    for txt in ("solve center", "mount position", "no pointing record",
                "no reference image", "frame not rotated"):
        assert txt in js, txt
    target = (ROOT / "templates" / "target.html").read_text(encoding="utf-8")
    assert "/static/js/sky_geom.js" in target and "SkyGeom.tanPx" in target
    assert "function tanPx" not in target               # not forked
    geom = GEOM.read_text(encoding="utf-8")
    assert "window.SkyGeom = {tanPx: tanPx}" in geom


def test_new_sources_are_ascii_without_em_dashes():
    for p in (GEOM, ROOT / "routers" / "review.py", Path(__file__)):
        b = p.read_bytes()
        assert all(c < 128 for c in b), p
