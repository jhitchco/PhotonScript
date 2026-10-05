"""PS-119: the Guiding tab's "What to change" list (scheduler.guiding_attention,
GET /api/guiding/attention, the card, the nav counts, the "N passing"
collapse, the System page line and `photonscript guiding-status`), plus the
audit corrections that came with it: the search region rule (1 x the dither),
the Dec min-move row, stale guide-log readings, no Apply on profile-only rows,
the PHD2 profile read from the registry (the scope PC's 2026-10-04 reg
export as a fake winreg), the stored calibration graded by PS-93, TheSky's
strict site longitude, its time zone / DST row and the AutoSave base folder."""
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import pytest

from photonscript.scheduler import guiding_attention as ga
from photonscript.scheduler import phd2_audit as pa
from photonscript.scheduler import phd2_calibration as pc
from photonscript.scheduler import phd2_profile_store as ps
from photonscript.scheduler import thesky_audit as ta
from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig
from tests.test_scheduler.test_ps89_phd2_audit import FakeNina, FakeWinreg, _node

ROOT = Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
JS = ROOT / "static" / "js" / "phd2_panels.js"
TS_JS = ROOT / "static" / "js" / "thesky_panels.js"
NOW = datetime(2026, 10, 5, 2, 50)          # 2026-10-04 20:50 MDT


def _cfg(tmp_path, **kw):
    kw.setdefault("data_dir", tmp_path / "data")
    kw.setdefault("phd2_logs_dir", str(tmp_path / "logs"))
    kw.setdefault("phd2_darks_dir", str(tmp_path / "darks"))
    kw.setdefault("phd2_host", "127.0.0.1")
    kw.setdefault("phd2_port", 1)
    kw.setdefault("thesky_tcp_host", "127.0.0.1")
    kw.setdefault("thesky_tcp_port", 1)
    return PhotonScriptConfig(_env_file=None, **kw)


# --------------------------------------------------------------------------
# the live state of 2026-10-04 20:50 as cached audits (the ticket's example)
# --------------------------------------------------------------------------

def _prow(rid, status, *, group="Camera", source="api", apply="manual", target=None,
          current="x", note=None, label=None):
    return {"id": rid, "group": group, "label": label or rid, "status": status,
            "source": source, "apply": apply, "target": target, "current": current,
            "desired": "d", "why": "w", "fix": f"fix {rid}", "note": note,
            "severity": status if status in ("fail", "warn") else "fail",
            "applicable": apply in ("api", "profile") and status in ("fail", "warn")}


def _phd2_audit(**kw):
    rows = [
        _prow("bit_depth", "fail", source="log", apply="profile", target=16, current="8"),
        _prow("search_region_px", "fail", group="Guiding", apply="profile", target=35,
              current="35"),
        _prow("dec_algorithm", "fail", group="Algorithms", current="Lowpass2"),
        _prow("calibration_record", "fail", group="Calibration", source="calibration",
              current="none"),
        _prow("ra_min_move", "warn", group="Algorithms", apply="api", target=1.5,
              current="0.76"),
        _prow("exposure_ms", "pass", apply="api", target=3000, current="3000"),
        _prow("binning", "pass", current="2"),
        _prow("multi_star", "info", group="Guiding", source="profile", current="on"),
        _prow("min_hfd_px", "unknown", group="Guiding", source=None,
              note="registry name unverified (candidate guider/StarMinHFD = 1.5)"),
        _prow("auto_exposure", "unknown", source=None,
              note="registry name not known yet (reg export on the scope PC)"),
        _prow("ascom_direct_guide", "unknown", group="Mount driver", source=None,
              note="driver flag location not known yet: check it by hand"),
        _prow("ascom_pointing_state", "unknown", group="Mount driver", source=None,
              note="driver flag location not known yet: check it by hand"),
        _prow("nina_settle_px", "unknown", group="NINA", source=None,
              note="nina: no guider settings in the NINA profile"),
    ]
    a = {"t_utc": "2026-10-05T02:45:00Z", "reason": "manual", "autofix": False,
         "rows": rows, "counts": {}, "sources": {"log": {
             "ok": True, "note": "newest session in PHD2_GuideLog_2026-09-26_120453.txt"}},
         "source_times": {"log": "2026-09-27T03:54:00Z"}}
    a.update(kw)
    return a


def _trow(rid, status, group, source="thesky-script", note=None):
    return {"id": rid, "group": group, "label": rid, "status": status, "source": source,
            "current": "c", "desired": "d", "why": "w", "fix": f"fix {rid}", "note": note,
            "apply": "manual", "applicable": False, "confidence": "High"}


def _thesky_audit():
    rows = [
        _trow("tpoint_rebuild", "fail", "TPoint and ProTrack", source="manual"),
        _trow("first_slew_error", "fail", "Pointing", source="pointing-log"),
        _trow("site_longitude", "fail", "Site and time"),
        _trow("autosave_path", "warn", "Camera (TheSky camera add-on)"),
        _trow("ails_retries", "warn", "Automated Image Link settings"),
        _trow("site_latitude", "pass", "Site and time"),
        _trow("tpoint_points", "unknown", "TPoint and ProTrack", source=None,
              note="manual: no manual TPoint record: enter it on the Guiding tab"),
        _trow("gaia_installed", "unknown", "Catalogs", source=None,
              note="manual: no manual TPoint record: enter it on the Guiding tab"),
        _trow("true_scale", "unknown", "Image truth (ASTAP)", source=None,
              note="astap: no ASTAP Image Link check yet (Guiding tab or --imagelink)"),
    ]
    return {"t_utc": "2026-10-05T02:40:00Z", "reason": "arm", "rows": rows,
            "imagelink": None, "manual": None, "pointing": {"nights": 14}}


def _build(tmp_path, **kw):
    cfg = _cfg(tmp_path, **kw)
    return ga.build(cfg, NOW, phd2=_phd2_audit(autofix=kw.get("phd2_audit_autofix", False)),
                    thesky=_thesky_audit()), cfg


def test_summary_orders_fail_then_warn_and_tonight_first(tmp_path):
    s, _cfg_ = _build(tmp_path)
    ids = [i["id"] for i in s["items"]]
    sev = [i["severity"] for i in s["items"]]
    assert sev == sorted(sev, key=lambda x: {"fail": 0, "warn": 1}[x])     # fail, then warn
    # the site longitude first (invalidates pointing and TPoint), then what
    # tonight's guiding needs, then the TPoint / pointing work
    assert ids[:6] == ["site_longitude", "search_region_px", "dec_algorithm", "calibration",
                       "tpoint_rebuild", "first_slew_error"]
    warns = [i["id"] for i in s["items"] if i["severity"] == "warn"]
    assert warns == ["ra_min_move", "selftest", "hotpix", "autosave_path", "ails_retries"]
    # PS-93's own item replaces the audit's calibration row (no duplicate)
    assert "calibration_record" not in ids
    cal = next(i for i in s["items"] if i["id"] == "calibration")
    assert cal["current"] == "none on record" and cal["where"] == "PhotonScript"
    st = next(i for i in s["items"] if i["id"] == "selftest")
    assert "none in 30 nights" in st["current"]
    # every item: section, anchor, setting, current -> desired, fix, where
    for i in s["items"] + s["stale_items"]:
        for k in ("severity", "section", "anchor", "row_anchor", "setting", "current",
                  "desired", "fix", "where", "priority_label"):
            assert i.get(k) not in (None, ""), (i["id"], k)
        assert i["where"] in ("PHD2", "NINA", "TheSky", "mount driver", "PhotonScript")
    assert next(i for i in s["items"] if i["id"] == "dec_algorithm")["row_anchor"] == \
        "pa-dec_algorithm"
    assert next(i for i in s["items"] if i["id"] == "tpoint_rebuild")["row_anchor"] == \
        "ts-tpoint_rebuild"


def test_apply_only_where_the_audit_can_apply_live(tmp_path):
    s, _c = _build(tmp_path)
    by = {i["id"]: i for i in s["items"] + s["stale_items"]}
    assert by["ra_min_move"]["apply_id"] == "ra_min_move"           # API row
    assert by["search_region_px"]["apply_id"] is None              # profile-only row
    assert by["search_region_px"]["manual_hint"].startswith("set in PHD2: ")
    assert by["dec_algorithm"]["apply_id"] is None and not by["dec_algorithm"]["manual_hint"]
    assert by["bit_depth"]["apply_id"] is None
    assert by["bit_depth"]["manual_hint"] == "set in PHD2: Equipment > camera properties (16-bit)"
    # profile writes on, but the registry names are not writable yet: still no Apply
    s2, _c = _build(tmp_path, phd2_audit_autofix=True)
    assert {i["id"]: i for i in s2["items"]}["search_region_px"]["apply_id"] is None


def test_apply_on_profile_rows_only_with_autofix_and_writable(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "WRITABLE", frozenset({"search_region_px"}))
    a = ga.annotate_phd2(_phd2_audit(autofix=True), _cfg(tmp_path), NOW)
    rows = {r["id"]: r for r in a["rows"]}
    assert rows["search_region_px"]["apply_live"] is True
    assert rows["bit_depth"]["apply_live"] is False                 # not writable
    a = ga.annotate_phd2(_phd2_audit(autofix=False), _cfg(tmp_path), NOW)
    assert {r["id"]: r for r in a["rows"]}["search_region_px"]["apply_live"] is False
    assert {r["id"]: r for r in a["rows"]}["exposure_ms"]["apply_live"] is False   # pass


def test_stale_guide_log_rows_get_their_own_group(tmp_path):
    s, _c = _build(tmp_path)
    assert [i["id"] for i in s["stale_items"]] == ["bit_depth"]
    b = s["stale_items"][0]
    assert b["stale"] and "guide log" in b["stale_why"]
    assert b["source_text"] == "guide log 2026-09-27"
    assert "next guiding session" in s["stale_hint"] and "reg export" in s["stale_hint"]
    assert all(not i["stale"] for i in s["items"])
    assert s["counts"]["stale"] == 1


@pytest.mark.parametrize("log_t,change_t,stale,why", [
    ("2026-10-05T00:00:00Z", None, False, None),                 # 2.8 h old, no change
    ("2026-10-04T12:00:00Z", None, True, "old"),                 # over STALE_HOURS
    ("2026-10-05T00:00:00Z", "2026-10-05T01:00:00Z", True, "changed"),   # PHD2 changed since
    (None, None, True, "unknown"),                               # no reading time at all
])
def test_stale_classification(tmp_path, log_t, change_t, stale, why):
    a = _phd2_audit(sources={}, source_times={"log": log_t} if log_t else {})
    lc = store.parse_z(change_t) if change_t else None
    ga.annotate_phd2(a, _cfg(tmp_path), NOW, last_change=lc)
    rows = {r["id"]: r for r in a["rows"]}
    assert rows["bit_depth"]["stale"] is stale
    if why:
        assert why in rows["bit_depth"]["stale_why"]
    # live sources are never stale; their text says live
    assert rows["search_region_px"]["stale"] is False
    assert rows["search_region_px"]["source_text"] == "PHD2 API, live"
    assert rows["multi_star"]["source_text"] == "PHD2 profile, live"


def test_stale_from_the_log_file_name_and_the_config_change_note(tmp_path):
    cfg = _cfg(tmp_path)
    a = _phd2_audit(source_times={})                # an older audit: only the note
    assert ga._log_time(a, cfg).strftime("%Y-%m-%d") == "2026-09-26"
    pa.note_config_change(cfg, datetime(2026, 10, 5, 2, 0))
    assert ga.last_config_change(cfg) == datetime(2026, 10, 5, 2, 0)
    a = _phd2_audit(source_times={"log": "2026-10-05T01:00:00Z"})
    ga.annotate_phd2(a, cfg, NOW)
    assert {r["id"]: r for r in a["rows"]}["bit_depth"]["stale"] is True


def test_not_checked_groups_by_reason_with_one_action_each(tmp_path):
    s, _c = _build(tmp_path)
    g = {x["key"]: x for x in s["not_checked"]}
    assert g["reg_export"]["count"] == 2 and "reg export" in g["reg_export"]["action"]
    assert g["manual_tpoint"]["count"] == 2 and g["manual_tpoint"]["anchor"] == "tsManualForm"
    assert g["driver_flags"]["count"] == 2 and "by hand" in g["driver_flags"]["action"]
    assert g["nina"]["count"] == 1 and "NINA" in g["nina"]["title"]
    assert g["astap"]["count"] == 1 and "ASTAP check now" in g["astap"]["action"]
    assert s["counts"]["not_checked"] == 8
    assert s["line"] == ("Guiding: 12 to change (7 fail, 5 warn), 8 not checked, "
                         "1 may already be fixed")
    sec = s["sections"]
    assert sec["auditSec"]["fail"] == 4 and sec["auditSec"]["warn"] == 1
    assert sec["tpointSec"] == {"fail": 3, "warn": 2, "pass": 1, "unknown": 3, "info": 0}
    assert sec["calSec"]["fail"] == 1 and sec["selftestSec"]["warn"] == 1
    assert sec["guardSec"]["warn"] == 1


def test_never_raises_with_no_caches(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    s = ga.build(cfg, NOW)
    assert s["sources"] == {"phd2_audit": None, "thesky_audit": None}
    assert [i["id"] for i in s["items"]] == ["calibration", "selftest", "hotpix"]
    assert s["not_checked"] == [] and s["line"].startswith("Guiding: 3 to change")
    monkeypatch.setattr(pc, "summary", lambda *a, **k: 1 / 0)
    s = ga.build(cfg, NOW)
    assert not s["ok"] and any("calibration" in p for p in s["problems"])
    monkeypatch.setattr(ga, "build", lambda *a, **k: 1 / 0)
    s = ga.safe_build(cfg)
    assert s["ok"] is False and s["items"] == [] and "unavailable" in s["line"]


def test_format_text_and_cli(tmp_path, monkeypatch):
    s, _c = _build(tmp_path)
    txt = ga.format_text(s)
    assert txt.splitlines()[0] == s["line"]
    assert "[FAIL] site_longitude" in txt and "Apply on /guiding (ra_min_move)" in txt
    assert "May already be fixed" in txt and "Not checked yet:" in txt
    assert txt.isascii()
    from typer.testing import CliRunner
    from photonscript import cli
    names = {c.name for c in cli.app.registered_commands}
    assert "guiding-status" in names
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: _cfg(tmp_path))
    r = CliRunner().invoke(cli.app, ["guiding-status"])
    assert r.exit_code == 0 and "Guiding: 3 to change" in r.output
    r = CliRunner().invoke(cli.app, ["guiding-status", "--json"])
    assert r.exit_code == 0 and json.loads(r.output)["counts"]["to_change"] == 3


# --------------------------------------------------------------------------
# routes, template, JS
# --------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import phd2 as r
    monkeypatch.setattr(app, "_config", _cfg(tmp_path))

    async def _no_probe(cfg):
        return {"ok": False, "note": "PHD2 not reachable", "age_s": 0.0}
    monkeypatch.setattr(r, "_probe_phd2", _no_probe)
    return TestClient(app.app)


def test_attention_route_and_annotated_audits(client, tmp_path):
    d = client.get("/api/guiding/attention").json()
    assert set(d) >= {"line", "counts", "items", "stale_items", "not_checked", "sections"}
    cfg = _cfg(tmp_path)
    store.write_json(pa.audit_dir(cfg) / "latest.json", _phd2_audit())
    store.write_json(ta.audit_dir(cfg) / "latest.json", _thesky_audit())
    a = client.get("/api/phd2/audit").json()
    rows = {r["id"]: r for r in a["rows"]}
    assert a["cached"] and rows["bit_depth"]["stale"] and not rows["bit_depth"]["apply_live"]
    assert rows["ra_min_move"]["apply_live"] and rows["search_region_px"]["manual_hint"]
    t = client.get("/api/thesky/audit").json()
    assert {r["id"]: r for r in t["rows"]}["first_slew_error"]["source_text"] == \
        "NINA Center log, 14 nights"
    d = client.get("/api/guiding/attention").json()
    assert d["items"][0]["id"] == "site_longitude" and d["counts"]["stale"] == 1


def test_guiding_page_card_nav_counts_and_collapse(client):
    html = client.get("/guiding").text
    ids = re.findall(r'\bid="([^"]+)"', html)
    assert not [k for k, n in Counter(ids).items() if n > 1]
    assert html.index('id="attention"') < html.index('id="liveSec"')      # at the very top
    for i in ("attnLine", "attnItems", "attnStale", "attnNotChecked", "attnRefresh",
              "attnStatus"):
        assert i in ids, i
    navc = re.findall(r'data-navc="(\w+)"', html)
    assert set(navc) == {"auditSec", "selftestSec", "calSec", "guardSec", "tuneSec",
                         "tpointSec"}
    assert all(f'id="{a}"' in html for a in navc)
    assert '<a href="#attention">' in html
    for css in (".g-hide-ok .g-row-ok", ".g-row-fail > td:first-child", ".g-row-warn",
                ".badge-stale", ".g-nf", ".g-nw"):
        assert css in html, css
    # anchors the summary links to exist on the page (row anchors are made by the JS)
    for a in ("auditSec", "calSec", "selftestSec", "guardSec", "tuneSec", "tpointSec",
              "tsManualForm", "tsImagelink"):
        assert f'id="{a}"' in html, a


def test_panel_js_collapse_attention_and_dry_run_first():
    js = JS.read_text(encoding="utf-8")
    assert "'/api/guiding/attention'" in js and "/guiding#attention" in js
    assert "g-hide-ok" in js and " passing" in js and "localStorage" in js
    assert "data-navc" in js and "navCounts(s.sections)" in js
    assert "id=\"pa-' + esc(r.id)" in js
    # the What to change Apply goes through the same dry-run-then-confirm path
    assert "applyAudit(b.getAttribute('data-attn-apply'), 'attnStatus')" in js
    i_dry, i_real = js.index("dry_run: true"), js.index("dry_run: false")
    assert i_dry < js.index("if (!dry.ok) return;") < js.index("confirm('Dry run for") < i_real
    assert js.count("dry_run: false") == 1
    # Apply only where apply_live says so
    assert "r.apply_live != null ? r.apply_live" in js
    ts = TS_JS.read_text(encoding="utf-8")
    assert "id=\"ts-' + esc(r.id)" in ts and "pp.wire('tsRows', 'tpointSec')" in ts
    assert "r.source_text || r.source" in ts


def test_system_page_line_links_to_the_card(client):
    html = client.get("/system").text
    assert "PHD2.systemSummary('phd2Summary')" in html
    js = JS.read_text(encoding="utf-8")
    i = js.index("async function systemSummary")
    body = js[i:js.index("// ---- Guiding tab wiring")]
    assert "safe('/api/guiding/attention')" in body and 'href="/guiding#attention"' in body
    assert "esc(w.line)" in body


# --------------------------------------------------------------------------
# PHD2 audit corrections
# --------------------------------------------------------------------------

def _eval(observed, cfg=None):
    cfg = cfg or PhotonScriptConfig(_env_file=None)
    obs = {s: {} for s in pa.SOURCES}
    obs.update(observed)
    return {r["id"]: r for r in pa.evaluate(pa.load_desired(cfg), obs, cfg)["rows"]}


@pytest.mark.parametrize("region,dither,status", [
    (35, 20.1, "pass"),      # Jeremy's setting: covers the dither, inside 30 to 40
    (15, 20.0, "fail"),      # 09-26: the star left the search region on dithers
    (45, 20.0, "warn"),      # covers the dither, outside 30 to 40
    (25, 20.0, "warn"),
])
def test_search_region_covers_one_dither(region, dither, status):
    r = _eval({"api": {"search_region_px": region}, "nina": {"dither_px": dither}})
    sr = r["search_region_px"]
    assert sr["status"] == status, sr
    assert f">= the dither ({dither:.1f} px)" in sr["desired"]
    if status == "fail":
        assert "under the dither" in sr["note"]


@pytest.mark.parametrize("dec_mm,status", [(20.0, "fail"), (1.5, "pass"), (0.2, "warn")])
def test_dec_min_move_against_the_guiding_assistant(dec_mm, status):
    r = _eval({"api": {"dec_min_move": dec_mm}, "ga": {"ra_min_move_rec": 1.5}})
    d = r["dec_min_move"]
    assert d["status"] == status and d["target"] == 1.5 and d["apply"] == "api"
    if status == "fail":
        assert "unguided" in d["note"]
    if status == "warn":
        assert "chases the seeing" in d["note"]
    r = _eval({"api": {"dec_min_move": 2.5}})                     # no GA: 0.5 to 3 px
    assert r["dec_min_move"]["status"] == "pass" and r["dec_min_move"]["target"] == 1.5
    r = _eval({"api": {"ra_aggressiveness": 55}})
    assert r["ra_aggressiveness"]["status"] == "info"


async def test_dec_min_move_applies_over_the_api(tmp_path):
    from tests.fakes.fake_phd2 import FakePHD2
    from photonscript.telescope_agent.phd2_client import PHD2Client
    cfg = _cfg(tmp_path)
    fake = FakePHD2(tmp_path / "phd2", app_state="Stopped")
    port = await fake.start()
    client = PHD2Client("127.0.0.1", port, config=cfg)
    assert await client.connect()
    audit = {"rows": [{"id": "dec_min_move", "apply": "api", "status": "fail",
                       "target": 1.5, "current": "20"}]}
    try:
        dry = await pa.apply(cfg, ["dec_min_move"], dry_run=True, client=client, audit=audit)
        assert dry["ok"] and fake.algo["dec"]["MinMove"] == 0.76
        r = await pa.apply(cfg, ["dec_min_move"], dry_run=False, client=client, audit=audit)
        assert r["ok"] and fake.algo["dec"]["MinMove"] == 1.5
    finally:
        await client.disconnect()
        await fake.close()


# --------------------------------------------------------------------------
# the PHD2 profile from the scope PC's reg export (2026-10-04)
# --------------------------------------------------------------------------

def _export_registry(extra_ga=None):
    """Profile 2 of the scope PC's export (no USB ids), as a fake winreg."""
    S, D = FakeWinreg.REG_SZ, FakeWinreg.REG_DWORD
    ga_runs = {"2026-09-25 20:55:30": _node({
        "timestamp": ("2026-09-25 20:55:30", S), "snr": ("137.1", S),
        "pa_error": (" 0.3 arc-min", S), "rec_ra_minmove": ("1.500000", S),
        "rec_dec_minmove": ("1.500000", S)}),
        "2026-09-21 06:07:57": _node({
            "timestamp": ("2026-09-21 06:07:57", S), "pa_error": (" 1464.9 arc-min", S),
            "rec_ra_minmove": ("0.260000", S), "rec_dec_minmove": ("0.260000", S)})}
    ga_runs.update(extra_ga or {})
    prof = _node({"name": ("Primary RC Profile (Guider)", S), "AutoLoadCalibration": (1, D),
                  "ExposureDurationMs": (4000, D), "DitherScaleFactor": ("1", S)}, {
        "camera": _node({"binning": (2, D), "LastMenuchoice": ("OGMA Camera", S),
                         "pixelsize": ("2", S), "SaturationADU": (255, D), "gain": (100, D),
                         "AutoLoadDefectMap": (0, D), "AutoLoadDarks": (1, D),
                         "SaturationByADU": (1, D)}, {
            "ogma": _node({"bpp": (16, D)})}),
        "frame": _node({"focalLength": (3248, D)}),
        "GA": _node(keys=ga_runs),
        "guider": _node({"StarMinHFD": ("1.6", S), "StarMinSNR": ("50", S)}, {
            "multistar": _node({"enabled": (1, D)}),
            "onestar": _node({"MassChangeThreshold": ("0.8", S),
                              "MassChangeThresholdEnabled": (1, D),
                              "SearchRegion": (35, D)})}),
        "scope": _node({"CalibrationDuration": (750, D), "CalibrationDistance": (25, D),
                        "DecGuideMode": (1, D), "XGuideAlgorithm": (3, D),
                        "YGuideAlgorithm": (4, D), "CalFlipRequiresDecFlip": (0, D),
                        "AssumeOrthogonal": (0, D), "UseDecComp": (1, D),
                        "StopGuidingWhenSlewing": (0, D), "BacklashCompEnabled": (0, D)}, {
            "calibration": _node({
                "timestamp": ("10/2/2026 00:28:55", S), "xAngle": ("-1.56579", S),
                "yAngle": ("0.0898217", S), "xRate": ("0.0194618", S),
                "yRate": ("0.0332379", S), "binning": (2, D), "declination": ("0.986998", S),
                "pierSide": (1, D), "raGuideParity": (1, D),
                "decGuideParity": (0xFFFFFFFF, D), "focal_length": (3248, D),
                "image_scale": ("0.254021", S), "ra_guide_rate": ("0.00207764", S),
                "dec_guide_rate": ("0.00207764", S), "ortho_error": ("4.85953", S),
                "ra_steps": ("{0.0 0.0}, {2.8 9.0}, {0.9 17.6}, {-0.1 26.3}, {-0.1 26.3}, "
                             "{3.7 7.5}", S),
                "dec_steps": ("{0.0 0.0}, {-14.5 -6.3}, {-29.8 -2.7}, {-29.8 -2.7}, "
                              "{-6.1 0.6}, {1.4 -1.0}", S),
                "ra_step_count": (3, D), "dec_step_count": (2, D), "last_issue": (1, D)}),
            "GuideAlgorithm": _node(keys={
                "X": _node(keys={"Hysteresis": _node({"minMove": ("0.2", S),
                                                      "aggression": ("0.7", S)}),
                                 "Lowpass2": _node({"minMove": ("1.5", S),
                                                    "Aggressiveness": ("55", S)})}),
                "Y": _node(keys={"Lowpass2": _node({"minMove": ("0.76", S),
                                                    "Aggressiveness": ("50", S)}),
                                 "ResistSwitch": _node({"minMove": ("20", S),
                                                        "aggression": ("1", S),
                                                        "fastSwitch": (1, D)})})})})})
    old = _node({"name": ("OAG Guider", S)}, {"frame": _node({"focalLength": (600, D)})})
    root = _node({"currentProfile": (2, D), "ConfigVersion": (2001, D)},
                 {"profile": _node(keys={"1": old, "2": prof})})
    return FakeWinreg(_node(keys={"Software": _node(keys={"StarkLabs": _node(
        keys={"PHDGuidingV2": root})})}))


def test_profile_store_reads_the_export(monkeypatch):
    monkeypatch.setattr(ps, "_winreg", lambda: _export_registry())
    r = ps.read()
    assert r["profile_id"] == "2"                                  # currentProfile
    v = {k: x["value"] for k, x in r["values"].items()}
    assert v["bit_depth"] == 16 and r["values"]["bit_depth"]["location"] == "camera/ogma/bpp"
    assert all(x["verified"] for k, x in r["values"].items() if x["location"])
    d = r["derived"]
    assert d["ra_algorithm"] == "Lowpass2" and d["ra_min_move"] == 1.5
    assert d["dec_algorithm"] == "Resist Switch" and d["dec_min_move"] == 20.0
    assert d["dec_aggressiveness"] == 100.0 and d["ra_aggressiveness"] == 55.0
    assert d["dec_guide_mode"] == "Auto" and d["auto_exposure"] is False
    assert d["mass_change_pct"] == 80.0
    assert d["ga"]["time_local"] == "2026-09-25T20:55:30" and d["ga"]["ra_min_move_rec"] == 1.5
    assert d["calibration"]["ra_step_count"] == 3
    # a newer run with a wild polar error is skipped too
    bad = {"2026-10-01 01:00:00": _node({"timestamp": ("2026-10-01 01:00:00", 1),
                                         "pa_error": ("900 arc-min", 1),
                                         "rec_ra_minmove": ("0.3", 1)})}
    monkeypatch.setattr(ps, "_winreg", lambda: _export_registry(bad))
    assert ps.read()["derived"]["ga"]["ra_min_move_rec"] == 1.5
    # nothing is writable yet, whatever the audit says
    assert not any(ps.writable(k) for k in ps.KEYS)


def test_stored_calibration_graded_fail_for_few_steps(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "_winreg", lambda: _export_registry())
    cfg = _cfg(tmp_path)
    r = ps.read()
    rec = pc.record_from_registry(cfg, r["derived"]["calibration"], r["values"])
    assert rec["source"] == "registry" and rec["t_utc"] == "2026-10-02T06:28:55Z"
    assert rec["steps"] == {"West": 3, "North": 2} and rec["moved_px"]["West"] == 26.3
    assert 56 < rec["dec_deg"] < 57 and rec["ortho_err_deg"] == 4.86
    assert rec["ra"]["rate_px_s"] == pytest.approx(19.46, abs=0.01)
    assert rec["dec"]["parity"] == "-" and rec["pier_side"] == "West"
    assert rec["ra_speed"] == pytest.approx(7.48, abs=0.01)
    saved = pc.seed_from_registry(cfg, r["derived"]["calibration"], r["values"])
    assert saved["grade"] == pc.FAIL
    assert any("too few steps" in x and "North 2" in x for x in saved["reasons"])
    assert pc.summary(cfg)["grade"] == pc.FAIL                     # not "none" any more
    assert pc.seed_from_registry(cfg, r["derived"]["calibration"], r["values"]) is None


async def test_audit_reads_the_profile_live(tmp_path, monkeypatch):
    monkeypatch.setattr(ps, "_winreg", lambda: _export_registry())
    monkeypatch.setattr(pa, "_collect_thesky", lambda config: (
        {"thesky_scripting": False}, {"ok": True, "note": "refused"}))
    cfg = _cfg(tmp_path)
    a = await pa.run_audit(cfg, "test", nina=FakeNina(settle=4, timeout=60, dither=20.1))
    rows = {r["id"]: r for r in a["rows"]}

    def st(rid):
        return rows[rid]["status"], rows[rid]["source"]
    assert st("bit_depth") == ("pass", "profile")
    assert st("saturation_adu")[0] == "fail" and "65535" in rows["saturation_adu"]["desired"]
    assert st("search_region_px") == ("pass", "profile")
    assert st("ra_algorithm") == ("pass", "profile")
    assert st("ra_min_move") == ("pass", "profile")              # 1.5 vs the GA's 1.5
    assert st("dec_algorithm") == ("pass", "profile")
    assert st("dec_min_move") == ("fail", "profile")             # ResistSwitch 20 px
    cs = rows["calibration_step_ms"]
    assert cs["status"] == "fail" and 95 <= cs["target"] <= 110 and "step" in cs["note"]
    assert st("stop_guiding_when_slewing") == ("warn", "profile")
    assert st("dec_compensation") == ("pass", "profile")
    assert st("auto_restore_cal") == ("pass", "profile")
    assert st("focal_length_mm") == ("pass", "profile")
    assert st("mass_change")[0] == "pass" and st("min_hfd_px")[0] == "pass"
    assert rows["darks_or_defects"]["status"] == "info"
    assert rows["calibration_record"]["status"] == "fail"        # graded from the profile
    assert a["sources"]["ga"]["note"].endswith("(PHD2 profile)")
    ga.annotate_phd2(a, cfg)
    assert {r["id"]: r for r in a["rows"]}["bit_depth"]["source_text"] == "PHD2 profile, live"
    # no "registry names unverified" unknowns any more
    assert not [r for r in a["rows"] if "registry name" in str(r.get("note"))]


# --------------------------------------------------------------------------
# TheSky: longitude, time zone, AutoSave
# --------------------------------------------------------------------------

def _ts(observed, cfg=None):
    cfg = cfg or PhotonScriptConfig(_env_file=None, thesky_tcp_host="127.0.0.1")
    obs = {s: {} for s in ta.SOURCES}
    obs.update(observed)
    return {r["id"]: r for r in ta.evaluate(ta.load_desired(cfg), obs, cfg)["rows"]}


def test_site_longitude_is_strict():
    r = _ts({"thesky-script": {"site_longitude": 109.021}})["site_longitude"]
    assert r["status"] == "fail" and "EAST" in r["note"] and "TPoint" in r["note"]
    r = _ts({"thesky-script": {"site_longitude": -109.021}})["site_longitude"]
    assert r["status"] == "pass"
    r = _ts({"thesky-script": {"site_longitude": -105.0}})["site_longitude"]
    assert r["status"] == "fail"


@pytest.mark.parametrize("now,tz,dst,status", [
    (datetime(2026, 10, 5, 2, 50), -6, 0, "warn"),     # MDT: right today only
    (datetime(2026, 10, 5, 2, 50), -7, 1, "pass"),     # MST + US DST
    (datetime(2026, 12, 1, 3, 0), -7, 1, "pass"),
    (datetime(2026, 12, 1, 3, 0), -6, 0, "fail"),      # after 11-01: an hour off
    (datetime(2026, 10, 5, 2, 50), -7, 0, "fail"),
])
def test_time_zone_and_dst(monkeypatch, now, tz, dst, status):
    monkeypatch.setattr(ta, "_utcnow", lambda: now)
    r = _ts({"thesky-script": {"time_zone": tz, "dst_index": dst}})["time_zone_dst"]
    assert r["status"] == status, r
    if status == "warn":
        assert "one hour off" in r["note"]


def test_autosave_base_folder(tmp_path):
    base = tmp_path / "Camera AutoSave" / "Imager"
    base.mkdir(parents=True)
    leaf = str(base / "October 04 2026")
    assert ta.autosave_base(leaf) == (str(base), "October 04 2026")
    assert ta.autosave_base(str(base / "2026-10-04"))[1] == "2026-10-04"
    assert ta.autosave_base(str(base))[1] is None
    r = _ts({"thesky-script": {"autosave_path": leaf}})["autosave_path"]
    assert r["status"] == "pass" and str(base) in r["note"]
    r = _ts({"thesky-script": {"autosave_path": str(tmp_path / "nope" / "October 04 2026")}})
    assert r["autosave_path"]["status"] == "warn" and "does not exist" in r["autosave_path"]["note"]
    r = _ts({"thesky-script": {"autosave_path": leaf, "autosave_on": False}})
    assert r["autosave_path"]["status"] == "warn" and "off" in r["autosave_path"]["note"]


def test_new_sources_are_ascii_without_em_dashes():
    for p in (ROOT / "guiding_attention.py", JS, TS_JS, ROOT / "templates" / "guiding.html",
              ROOT / "phd2_profile_store.py", Path(__file__)):
        t = p.read_text(encoding="utf-8")
        assert t.isascii(), p
