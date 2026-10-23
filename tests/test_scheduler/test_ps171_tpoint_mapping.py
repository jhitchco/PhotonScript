"""PS-171: TPoint mapping run (sideload recipe tpoint_mapping_then_tonight)
and the tpoint-sample CLI.

Point grid (even spread, altitude band), alt/az -> HA/Dec, the meridian /
pole / moon skips, the run order (east then west, short slews), the
generated sequence and its lint rule, the splice and the preview recipe,
then the sample side: probe parsing, add-method selection, the CSV
fallback, the CLI dry run, and the denylist (the add call is the only
TheSky write and lives only in telescope_agent/tpoint_sample.py)."""
import asyncio
import csv
import json
import math
import re
from datetime import datetime
from pathlib import Path

import pytest

from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import sideload as sd
from photonscript.scheduler import tpoint_mapping as tm
from photonscript.scheduler.sequence_lint import lint
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.target_names import is_test_target
from photonscript.telescope_agent import thesky_client as tc
from photonscript.telescope_agent import tpoint_sample as ts
from tests.test_scheduler.test_ps104_thesky_audit import _THESKY_OBJ, _violations

ROOT = Path(__file__).resolve().parents[2]
LAT = 31.906944
WHEN = datetime(2026, 10, 9, 3, 0)


def _cfg(tmp_path, **kw):
    kw.setdefault("thesky_tcp_host", "127.0.0.1")
    kw.setdefault("thesky_tcp_port", 1)          # refused at once
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


@pytest.fixture(autouse=True)
def _pinned(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(nsj, "_gen_cfg_cache", cfg)
    monkeypatch.setattr(nsj, "_filter_names_cache", cfg.filter_name_map())
    monkeypatch.chdir(tmp_path)
    return cfg


def _walk(n):
    if isinstance(n, dict):
        yield n
        for v in n.values():
            yield from _walk(v)
    elif isinstance(n, list):
        for x in n:
            yield from _walk(x)


def _types(seq, frag):
    return [d for d in _walk(seq) if frag in str(d.get("$type", ""))]


# ------------------------------------------------------------- geometry

def test_grid_count_band_and_even_spread():
    pts = tm.grid(60, 30.0)
    assert len(pts) == 60
    assert all(30.0 <= a <= tm.MAX_ALT and 0 <= z < 360 for a, z in pts)
    nearest = [min(tm.separation(a, z, b, y) for j, (b, y) in enumerate(pts)
                   if j != i) for i, (a, z) in enumerate(pts)]
    # an equal-area spiral: no clumps, no big holes (cap ~2.4 sr / 60 pts)
    assert min(nearest) > 5.0 and max(nearest) < 20.0
    # both halves of the sky get points
    assert sum(1 for _a, z in pts if z < 180) in range(24, 37)


@pytest.mark.parametrize("alt,az,lat,ha,dec", [
    (60.0, 180.0, 30.0, 0.0, 0.0),       # south, on the meridian
    (0.0, 90.0, 0.0, -6.0, 0.0),         # due east on the equator
    (0.0, 270.0, 0.0, 6.0, 0.0),         # due west
    (LAT, 0.0, LAT, 12.0, 90.0),         # the pole (HA undefined: either)
])
def test_altaz_to_hadec(alt, az, lat, ha, dec):
    h, d = tm.altaz_to_hadec(alt, az, lat)
    assert d == pytest.approx(dec, abs=0.01)
    if dec < 89:
        assert h == pytest.approx(ha, abs=0.01)


def test_plan_skips_meridian_and_pole_and_orders_east_then_west():
    plan = tm.plan_points(60, LAT, 30.0)
    pts = plan["points"]
    assert len(pts) == 60
    assert all(abs(p["ha_h"]) >= tm.MERIDIAN_BAND_H for p in pts)
    assert all(abs(p["ha_h"]) <= tm.DEFAULT_MAX_HA_H for p in pts)
    assert all(p["side"] == ("east" if p["ha_h"] < 0 else "west") for p in pts)
    sides = [p["side"] for p in pts]
    flips = sum(1 for a, b in zip(sides, sides[1:]) if a != b)
    assert sides[0] == "east" and flips == 1          # one pier flip
    assert plan["skipped"]["meridian"] + plan["skipped"]["pole"] > 0


def test_order_is_shorter_than_the_spiral_order():
    plan = tm.plan_points(60, LAT, 30.0)

    def path(ps):
        return sum(tm.separation(a["alt"], a["az"], b["alt"], b["az"])
                   for a, b in zip(ps, ps[1:]))
    spiral = sorted(plan["points"], key=lambda p: math.asin(
        math.sin(math.radians(p["alt"]))))
    assert path(plan["points"]) < 0.6 * path(spiral)


def test_moon_skip_and_a_moon_below_the_horizon():
    moon = [(50.0, 120.0), (52.0, 125.0), (54.0, 130.0)]
    plan = tm.plan_points(60, LAT, 30.0, moon_altaz=moon, moon_deg=15.0)
    assert len(plan["points"]) == 60 and plan["skipped"]["moon"] > 0
    for p in plan["points"]:
        assert all(tm.separation(p["alt"], p["az"], a, z) >= 15.0 for a, z in moon)
    down = tm.plan_points(60, LAT, 30.0, moon_altaz=[(-20.0, 120.0)])
    assert down["skipped"]["moon"] == 0


def test_params_clamped_and_defaults_are_safe(tmp_path):
    p = tm.params(_cfg(tmp_path))
    assert p == {"points": 60, "min_alt": 30.0, "exposure_s": 5.0, "binning": 2,
                 "moon_deg": 15.0, "max_ha_h": 6.0}
    big = tm.params(_cfg(tmp_path, tpoint_mapping_points=5000,
                         tpoint_mapping_binning=9))
    assert big["points"] == tm.MAX_POINTS and big["binning"] == 4
    c = _cfg(tmp_path)
    assert c.tpoint_sample_add == "off"            # no TheSky write by default
    assert c.tpoint_sample_script.endswith("deploy\\tpoint-sample.cmd")


# ------------------------------------------------------------- sequence

def _pts(n=6):
    return [[p["alt"], p["az"], p["side"]]
            for p in tm.plan_points(n, LAT, 30.0)["points"]]


def _seq(script="C:\\astro\\PhotonScript\\deploy\\tpoint-sample.cmd", n=6, **kw):
    return json.loads(nsj.generate_tpoint_mapping_json(
        _pts(n), ra_hours=20.0, dec_degrees=LAT, script=script, **kw))


def _points(seq):
    return [d for d in _types(seq, "DeepSkyObjectContainer")
            if str(d.get("Name", "")).startswith(nsj.TPOINT_POINT_PREFIX)]


def test_points_slew_altaz_expose_and_sample_without_center_or_sync():
    seq = _seq(n=6)
    tgt = [d for d in _types(seq, "DeepSkyObjectContainer")
           if d.get("Name") == "TPoint mapping 6 points"][0]
    for frag in ("Platesolving.Center", "MeridianFlipTrigger", "StartGuiding",
                 "SlewScopeToRaDec", "Sync"):
        assert not _types(tgt, frag), frag
    pts = _points(seq)
    assert len(pts) == 6
    for i, p in enumerate(pts, 1):
        kinds = [d["$type"].split(",")[0].split(".")[-1]
                 for d in p["Items"]["$values"]]
        assert kinds == ["SlewScopeToAltAz", "TakeExposure", "ExternalScript"]
        exp = p["Items"]["$values"][1]
        assert exp["ExposureTime"] == 5.0 and exp["Binning"]["X"] == 2
        assert exp["ImageType"] == "LIGHT"
        script = p["Items"]["$values"][2]["Script"]
        assert script.startswith('"C:\\astro\\PhotonScript\\deploy\\tpoint-sample.cmd" ')
        assert f"--rig rc16 --point {i} --of 6 " in script
        assert p["Items"]["$values"][2]["ErrorBehavior"] == 0
        assert p["Target"]["TargetName"] == p["Name"]
        conds = json.dumps(p["Conditions"])
        assert "SafetyMonitorCondition" in conds and "TimeCondition" in conds
    # the first point's slew is exact to 0.1"
    s = pts[0]["Items"]["$values"][0]["Coordinates"]
    alt = s["AltDegrees"] + s["AltMinutes"] / 60 + s["AltSeconds"] / 3600
    assert alt == pytest.approx(_pts(6)[0][0], abs=1e-3)


def test_standalone_lints_clean_with_a_tpoint_warning():
    r = lint(_seq(), guided=False)
    assert r.ok, [f.detail for f in r.findings if f.level == "ERROR"]
    rules = {f.rule for f in r.findings}
    assert "tpoint-mapping" in rules
    assert not rules & {"platesolve", "altitude", "meridian"}


def test_missing_script_frames_only_and_says_so():
    seq = _seq(script="")
    assert not _types(seq, "ExternalScript") or all(
        "tpoint" not in str(d.get("Script", "")).lower()
        for d in _types(seq, "ExternalScript"))
    assert "frames only" in json.dumps(seq)
    assert lint(seq, guided=False).ok


@pytest.mark.parametrize("bad", [
    {"$type": "NINA.Sequencer.SequenceItem.Platesolving.Center, NINA.Sequencer"},
    {"$type": "NINA.Sequencer.SequenceItem.Guider.StartGuiding, NINA.Sequencer"},
])
def test_lint_rule_refuses_center_and_guiding(bad):
    seq = _seq()
    _points(seq)[0]["Items"]["$values"].append(bad)
    r = lint(seq, guided=False)
    assert not r.ok and any(f.rule == "tpoint-mapping" and f.level == "ERROR"
                            for f in r.findings)


def test_lint_rule_refuses_a_flip_trigger_and_an_unguarded_point():
    seq = _seq()
    tgt = [d for d in _types(seq, "DeepSkyObjectContainer")
           if d.get("Name", "").startswith(nsj.TPOINT_MAPPING_PREFIX)][0]
    tgt["Triggers"]["$values"].append(nsj._meridian_flip_trigger())
    p = _points(seq)[1]
    p["Conditions"]["$values"] = [c for c in p["Conditions"]["$values"]
                                  if "Safety" not in c["$type"]]
    details = [f.detail for f in lint(seq, guided=False).findings
               if f.rule == "tpoint-mapping" and f.level == "ERROR"]
    assert any("MeridianFlipTrigger" in d for d in details)
    assert any("SafetyMonitorCondition" in d for d in details)


def test_test_target_names():
    for n in ("TPoint mapping 60 points",
              "TPoint point 07/60 alt 45.0 az 120.0 (east)"):
        assert is_test_target(n), n
    assert nsj.tpoint_point_name(7, 60, 45, 120, "east") == \
        "TPoint point 07/60 alt 45.0 az 120.0 (east)"
    assert nsj.tpoint_point_name(7, 120, 45, 120, "west").startswith(
        "TPoint point 007/120 ")


# ------------------------------------------------------------- sideload

def _night():
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget
    targets = [NinaSequenceTarget(
        name=n, ra_hours=ra, dec_degrees=dec,
        exposures=[ExposurePlan(filter_type=FilterType.HA, exposure_seconds=300,
                                count=10, gain=200, offset=256)])
        for n, ra, dec in (("Heart Nebula", 2.55, 61.5),
                           ("Pacman Nebula", 0.88, 56.6))]
    s = build_sequence_for_night("PhotonScript_20261009", targets)
    return json.loads(nsj.generate_nina_json(s))


def test_splice_runs_first_then_tonight_and_lints():
    seq = sd.splice_tpoint_mapping(_night(), _seq(), exclude=["Pacman Nebula"],
                                   name="X")
    assert sd.target_names(seq) == ["TPoint mapping 6 points", "Heart Nebula"]
    assert sd.SPLICED_TPM_NOTE in json.dumps(seq)
    r = lint(seq, guided=False)
    assert r.ok, [f.detail for f in r.findings if f.level == "ERROR"]
    warn = [f.detail for f in r.findings if f.rule == "tpoint-mapping"]
    assert warn and "Tonight's targets follow" in warn[0]
    # tonight's own targets still center and carry their flip trigger
    assert any(f.rule == "platesolve" for f in r.findings) is False


def test_preview_recipe(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import sideload as rt
    cfg = _cfg(tmp_path, piggyback_enabled=True, tpoint_mapping_points=12)

    class _Armer:
        state = "DISARMED"
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(app, "get_armer", lambda: _Armer())
    monkeypatch.setattr(app, "_stored_projects", lambda: {})
    monkeypatch.setattr(app, "_tonight_sequence", lambda now_mode=False: (
        "PhotonScript_20261009", json.dumps(_night()), False, False))
    monkeypatch.setattr(tm, "moon_track", lambda *a, **k: [(70.0, 200.0)] * 3)
    monkeypatch.setattr(tm, "start_time", lambda *a, **k: WHEN)
    assert sd.RECIPE_TPOINT_MAPPING in sd.RECIPES
    b = asyncio.run(rt.api_sideload_preview(
        recipe="tpoint_mapping_then_tonight", exclude=["Pacman Nebula"], at=""))
    rc = b["rigs"]["rc16"]
    assert rc["lint"]["ok"], rc["lint"]["text"]
    assert rc["targets"] == ["TPoint mapping 12 points", "Heart Nebula"]
    assert rc["name"].startswith("PhotonScript_20261009_TPM_")
    assert rc["field"]["points"] == 12 and "east" in rc["field"]["summary"]
    assert "MISSING" in rc["field"]["sample_script"]   # not on this machine
    assert b["rigs"]["piggyback"]["lint"]["ok"]


def test_dashboard_lists_the_recipe():
    dash = (ROOT / "photonscript" / "scheduler" / "templates" / "dashboard.html"
            ).read_text(encoding="utf-8")
    assert 'value="tpoint_mapping_then_tonight"' in dash


# ------------------------------------------------------------- sample: probe

def test_probe_script_is_read_only_and_ascii():
    js = ts.probe_script()
    assert not _violations(js), _violations(js)
    assert js.isascii()
    assert not re.search(_THESKY_OBJ + r"\.\w+\s*\(\s*\)\s*;", js)   # no calls
    for _cid, check, call in ts.ADD_CANDIDATES:
        assert check in js
        assert call not in js
    # nothing but the scripted ImageLink is ever executed, and not here
    assert "execute(" not in js
    for n in ts.ACTION_NAMES:
        assert f"typeof TheSkyXAction.{n}" in js
    assert "src(TheSkyXAction.execute)" in js
    for o in ("ImageLink", "sky6RASCOMTele", "sky6RASCOMTheSky", "OpticalTubeAssembly"):
        assert f"members({o}, true)" in js
    for o in ts.MODULES:
        assert f"objVals({o})" in js


def test_only_execute_forms_of_add_pointing_sample_are_candidates():
    ids = [c[0] for c in ts.ADD_CANDIDATES]
    assert ids == ["action_execute_id_AddPointingSample",
                   "action_execute_name_AddPointingSample",
                   "action_execute_upper_AddPointingSample"]
    assert [ts.add_line(i) for i in ids] == [
        "TheSkyXAction.execute(TheSkyXAction.AddPointingSample);",
        "TheSkyXAction.execute('AddPointingSample');",
        "TheSkyXAction.execute('ADD_POINTING_SAMPLE');"]
    assert all("TPoint." not in c[2] for c in ts.ADD_CANDIDATES)


# what build 14139 answers (coordinator's read-only check, 2026-10-08)
_SITE_PROBE = ("has_action_execute_id_AddPointingSample=1;"
               "has_action_execute_name_AddPointingSample=1;"
               "has_action_execute_upper_AddPointingSample=1;"
               "type_sky6RASCOMTheSky=object;members_sky6RASCOMTheSky=Connect~Quit;"
               "act_type_execute=function;"
               "act_src_execute=function execute(QString) { [native code] };"
               "act_len_execute=0;"
               "act_type_AddPointingSample=number;"
               "act_class_AddPointingSample=[object Number];"
               "act_value_AddPointingSample=197;act_members_AddPointingSample=;"
               "act_type_TPointAddOn2=number;act_value_TPointAddOn2=147;"
               "mod_values_sky6RASCOMTele=;mod_values_OpticalTubeAssembly=;"
               "flag_points=undefined;flag_rms_arcsec=?ERR")


def test_parse_probe_reads_the_numeric_actions_and_execute():
    p = ts.parse_probe(ts.parse_raw(_SITE_PROBE))
    assert p["use"] == "action_execute_id_AddPointingSample"
    assert len(p["found"]) == 3
    a = p["actions"]["AddPointingSample"]
    assert (a["type"], a["class"], a["value"]) == ("number", "[object Number]", "197")
    assert p["execute"] == {"type": "function", "length": "0",
                            "text": "function execute(QString) { [native code] }"}
    assert p["objects"]["sky6RASCOMTheSky"]["members"] == ["Connect", "Quit"]
    assert p["tpoint"]["points"] is None and p["tpoint"]["rms_arcsec"] is None
    text = ts.format_probe({"ok": True, "t_utc": "T", **p})
    assert "-> TheSkyXAction.execute(TheSkyXAction.AddPointingSample);" in text
    assert "execute: function, length 0, text: function execute(QString)" in text
    assert "TheSkyXAction.AddPointingSample: typeof number" in text
    assert "points: unavailable" in text
    assert ts.parse_probe({})["use"] is None


def test_choose_method_pin_and_fallbacks(tmp_path):
    p = ts.parse_probe(ts.parse_raw(_SITE_PROBE))
    assert ts.choose_method(_cfg(tmp_path), p) == (
        "action_execute_id_AddPointingSample", "first found by the probe")
    pin = _cfg(tmp_path, tpoint_sample_add_method="action_execute_name_AddPointingSample")
    assert ts.choose_method(pin, p)[0] == "action_execute_name_AddPointingSample"
    p2 = ts.parse_probe(ts.parse_raw("has_action_execute_name_AddPointingSample=1"))
    gone = _cfg(tmp_path, tpoint_sample_add_method="action_execute_id_AddPointingSample")
    m, why = ts.choose_method(gone, p2)
    assert m is None and "not found by the last probe" in why
    bad = _cfg(tmp_path, tpoint_sample_add_method="sky6RASCOMTele.Sync")
    assert ts.choose_method(bad, p)[0] is None
    assert ts.choose_method(_cfg(tmp_path), None)[0] is None
    assert _cfg(tmp_path).tpoint_sample_add_method == ""


def test_tpoint_flags_look_on_the_scripting_objects():
    js = tc.READ_ONLY_JS["tpoint_flags"]
    for o in ("sky6RASCOMTele", "sky6RASCOMTheSky", "OpticalTubeAssembly"):
        assert f"typeof {o}=='object'" in js
    assert "'numberOfPoints'" in js and "'skyRMS'" in js
    assert "TPoint.NumberOfPoints" in js            # the PS-138 read stays first
    assert "function tp(" in tc.onsite_script()
    assert not _violations(js) and js.isascii()


class _Fake(tc.TheSkyClient):
    def __init__(self, reply):
        super().__init__("x", 3040)
        self.reply, self.sent = reply, []

    def run_script(self, js):
        self.sent.append(js)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def test_run_probe_stores_and_compares_before_after(tmp_path):
    cfg = _cfg(tmp_path)
    r1 = ts.run_probe(cfg, client=_Fake("flag_points=60;flag_rms_arcsec=30.1"),
                      now=datetime(2026, 10, 8, 20, 0))
    assert r1["ok"] and r1["use"] is None and "previous" not in r1
    r2 = ts.run_probe(cfg, client=_Fake("flag_points=140;flag_rms_arcsec=18.2"),
                      now=datetime(2026, 10, 9, 13, 0))
    assert r2["previous"]["tpoint"]["points"] == "60"
    text = ts.format_probe(r2)
    assert "points: 140 (was 60" in text and "rms_arcsec: 18.2 (was 30.1" in text
    assert ts.load_probe(cfg)["tpoint"]["points"] == "140"
    assert len(list((tmp_path / "tpoint").glob("probe_2026*.json"))) == 2
    down = ts.run_probe(cfg, client=_Fake(tc.TheSkyError("refused")))
    assert not down["ok"] and "refused" in down["error"]
    assert ts.load_probe(cfg)["tpoint"]["points"] == "140"   # kept


# ------------------------------------------------------------- sample: scripts

def test_sample_script_read_only_unless_an_add_is_chosen():
    js = ts.sample_script(r"C:\N.I.N.A\TPoint point 01.fits", 0.472)
    assert not _violations(js), _violations(js)
    assert js.isascii()
    for _cid, _check, call in ts.ADD_CANDIDATES:
        assert call.rstrip(";") not in js
    for cid, check, call in ts.ADD_CANDIDATES:
        add = ts.sample_script(r"C:\x\a.fits", 0.472, add=cid)
        assert add.count(call) == 1
        assert not _violations(add), (cid, _violations(add))
        # the call is guarded by a successful solve and its own typeof check
        at = add.index(call)
        assert add.index("ImageLinkResults.succeeded == 1") < at
        assert add.index(f"can = ({check})") < at
    with pytest.raises(tc.TheSkyError):
        ts.sample_script(r"C:\x\a.fits", 0.472, add="sky6RASCOMTele.Sync")
    with pytest.raises(tc.TheSkyError):
        ts.sample_script("C:\\x';sky6RASCOMTele.Park();'.fits", 0.472)


def test_add_calls_live_only_in_the_sample_module():
    """Denylist: the candidate add calls are the only TheSky writes and
    appear in no other source file."""
    pats = [re.compile(re.escape(call.rstrip(";")))
            for _c, _k, call in ts.ADD_CANDIDATES]
    hits = []
    for f in (ROOT / "photonscript").rglob("*.py"):
        if f.name == "tpoint_sample.py":
            continue
        text = f.read_text(encoding="utf-8")
        hits += [f"{f.name}: {p.pattern}" for p in pats if p.search(text)]
    assert not hits, hits
    # only the CLI reaches the module (the scheduler never adds a point)
    users = sorted(f.name for f in (ROOT / "photonscript").rglob("*.py")
                   if f.name != "tpoint_sample.py" and re.search(
                       r"import tpoint_sample|tpoint_sample import",
                       f.read_text(encoding="utf-8")))
    assert users == ["cli.py"], users


# ------------------------------------------------------------- sample: run

def _frame(path: Path, binning=2):
    from astropy.io import fits
    import numpy as np
    path.parent.mkdir(parents=True, exist_ok=True)
    h = fits.Header()
    h["XBINNING"] = binning
    fits.writeto(path, np.zeros((4, 4), dtype="uint16"), h, overwrite=True)
    return path


_SOLVED = ("exec_error=;succeeded=1;error_text=;solved_ra_j2000_h=5.5;"
           "solved_dec_j2000_d=20.25;image_scale=0.472;position_angle=12.0;"
           "image_stars=210;solution_rms=0.3;connected=1;mount_ra_h=5.51;"
           "mount_dec_d=20.2;mount_az=101.0;mount_alt=45.0;lst_h=3.2;"
           "jd=2461322.6;apply_corrections=1;added=;add_error=")


def test_sample_off_mode_solves_and_writes_the_csv(tmp_path):
    cfg = _cfg(tmp_path, image_watch_dir=str(tmp_path / "nina"))
    f = _frame(tmp_path / "nina" / "2026-10-09" / "TPoint point 01_LIGHT.fits")
    cl = _Fake(_SOLVED)
    row = ts.run_sample(cfg, point=1, of=60, alt=45.0, az=101.0, side="east",
                        client=cl, now=WHEN, wait_s=2)
    assert row["file"] == str(f) and row["bin"] == 2
    assert row["scale"] == pytest.approx(0.472)
    assert row["solved"] and row["added"] is False and row["add_method"] == ""
    assert "pathToFITS" in cl.sent[0]
    assert not any(call in cl.sent[0] for _c, _k, call in ts.ADD_CANDIDATES)
    out = ts.csv_path(cfg, "2026-10-08")
    rows = list(csv.DictReader(open(out, newline="", encoding="utf-8")))
    assert rows[0]["solved_ra_j2000_h"] == "5.5" and rows[0]["side"] == "east"
    assert rows[0]["mount_ra_h"] == "5.51" and rows[0]["point"] == "1"
    ev = (tmp_path / "runs" / "2026-10-08_events.jsonl").read_text(encoding="utf-8")
    assert '"kind": "tpoint_sample"' in ev


def test_sample_auto_uses_the_probed_method_and_records_it(tmp_path):
    cfg = _cfg(tmp_path, image_watch_dir=str(tmp_path / "nina"),
               tpoint_sample_add="auto")
    _frame(tmp_path / "nina" / "a.fits")
    # auto without a probe: CSV only, says why
    row = ts.run_sample(cfg, client=_Fake(_SOLVED), now=WHEN, wait_s=2)
    assert row["add_method"] == "" and "no probe yet" in row["note"]
    assert "CSV only" in row["note"]
    ts.run_probe(cfg, client=_Fake(_SITE_PROBE))
    _frame(tmp_path / "nina" / "b.fits")
    cl = _Fake(_SOLVED.replace("added=;", "added=1;add_result=0;"))
    row = ts.run_sample(cfg, client=cl, now=WHEN, wait_s=2)
    assert row["file"].endswith("b.fits")          # not the frame sampled last
    assert row["add_method"] == "action_execute_id_AddPointingSample"
    assert row["added"] is True and row["add_result"] == "0"
    assert "TheSkyXAction.execute(TheSkyXAction.AddPointingSample)" in cl.sent[0]
    assert "added to TPoint via action_execute_id" in ts.format_row(row)


def test_sample_never_raises_and_still_writes_a_row(tmp_path):
    cfg = _cfg(tmp_path, image_watch_dir=str(tmp_path / "nina"))
    row = ts.run_sample(cfg, point=3, of=60, now=WHEN, wait_s=0)
    assert "no new frame" in row["note"]
    _frame(tmp_path / "nina" / "c.fits")
    row = ts.run_sample(cfg, client=_Fake(tc.TheSkyError("refused")), now=WHEN,
                        wait_s=2)
    assert not row["solved"] and "refused" in row["note"]
    row = ts.run_sample(cfg, str(tmp_path / "nina" / "c.fits"),
                        client=_Fake("succeeded=0;error_text=Not enough stars"),
                        now=WHEN)
    assert not row["solved"] and "Not enough stars" in row["note"]
    rows = list(csv.DictReader(open(ts.csv_path(cfg, "2026-10-08"),
                                    newline="", encoding="utf-8")))
    assert len(rows) == 3


def test_dry_run_sends_and_writes_nothing(tmp_path):
    cfg = _cfg(tmp_path, image_watch_dir=str(tmp_path / "nina"))
    _frame(tmp_path / "nina" / "d.fits", binning=1)
    cl = _Fake(_SOLVED)
    row = ts.run_sample(cfg, client=cl, now=WHEN, dry_run=True, wait_s=2)
    assert cl.sent == [] and "ImageLink.scale = 0.23600" in row["script"]
    assert not ts.csv_path(cfg, "2026-10-08").exists()


def test_cli_registered_and_always_exits_zero(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    names = {c.name for c in cli.app.registered_commands}
    assert "tpoint-sample" in names
    cfg = _cfg(tmp_path, image_watch_dir=str(tmp_path / "nina"))
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)
    _frame(tmp_path / "nina" / "e.fits")
    r = CliRunner().invoke(cli.app, ["tpoint-sample", "--dry-run"])
    assert r.exit_code == 0 and "dry run" in r.output and "pathToFITS" in r.output
    r = CliRunner().invoke(cli.app, ["tpoint-sample", "--probe"])   # TheSky down
    assert r.exit_code == 0 and "FAILED" in r.output
    r = CliRunner().invoke(cli.app, ["tpoint-sample", "--rig", "piggyback"])
    assert r.exit_code == 0 and "rc16 only" in r.output


def test_cmd_wrapper_always_exits_zero():
    cmd = (ROOT / "deploy" / "tpoint-sample.cmd").read_text(encoding="ascii")
    assert "tpoint-sample %* --from-nina" in cmd
    assert cmd.strip().splitlines()[-1].strip() == "exit /b 0"


def test_off_target_alert_ignores_the_mapping_run():
    from photonscript.scheduler.off_target import is_imaging
    pt = "TPoint point 01/60 alt 45.0 az 120.0 (east)"
    ok, why = is_imaging(["Targets", "TPoint mapping 60 points",
                          "TPoint mapping 60 points point loop", pt],
                         "Take Exposure", False, target=pt)
    assert not ok and "TPoint mapping" in why
    ok, _ = is_imaging(["Targets", "Heart Nebula"], "Take Exposure", False,
                       target="Heart Nebula")
    assert ok


def test_dry_run_prints_the_exact_add_call(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    line = "TheSkyXAction.execute(TheSkyXAction.AddPointingSample);"
    cfg = _cfg(tmp_path, image_watch_dir=str(tmp_path / "nina"))
    ts.run_probe(cfg, client=_Fake(_SITE_PROBE))
    _frame(tmp_path / "nina" / "f.fits")
    row = ts.run_sample(cfg, client=_Fake(_SOLVED), now=WHEN, dry_run=True, wait_s=2)
    assert row["add_call"] == line
    assert "PS_TPOINT_SAMPLE_ADD=auto" in row["add_call_note"]
    assert "AddPointingSample" not in row["script"]       # off: not sent
    auto = _cfg(tmp_path, image_watch_dir=str(tmp_path / "nina"),
                tpoint_sample_add="auto")
    row = ts.run_sample(auto, client=_Fake(_SOLVED), now=WHEN, dry_run=True, wait_s=2)
    assert line.rstrip(";") in row["script"]
    assert row["add_call_note"] == "runs after a successful Image Link"
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: auto)
    r = CliRunner().invoke(cli.app, ["tpoint-sample", "--dry-run"])
    assert r.exit_code == 0
    assert f"add call: {line}" in r.output


_NODE_FAKES = """
var calls = [];
var TheSkyXAction = { execute: function (a) { calls.push('execute:' + a); return 0; },
  AddPointingSample: 197, AutoPointingCalibration: 12, TPointAddOn2: 147,
  TPointModule: 148 };
var ImageLink = { execute: function () { calls.push('imagelink'); } };
var ImageLinkResults = { succeeded: 1, errorText: '', solutionRMS: 0.3 };
var sky6RASCOMTele = { IsConnected: 0, GetRaDec: function () { calls.push('getradec'); } };
var sky6RASCOMTheSky = { Connect: function () { calls.push('connect'); } };
var OpticalTubeAssembly = { name: 'RC16', modelPoints: 84 };
var sky6Utils = { ComputeLocalSiderealTime: function () {}, dOut0: 3.2 };
var sky6StarChart = { DocumentProperty: function () {}, DocPropOut: 1 };
var AutomatedImageLinkSettings = { exposureTimeAILS: 5 };
"""


@pytest.mark.skipif(__import__("shutil").which("node") is None,
                    reason="node not installed")
def test_scripts_run_in_a_js_engine_and_the_probe_calls_nothing(tmp_path):
    """The generated JS against fake TheSky objects shaped like build 14139
    (numeric action ids): the probe calls nothing at all; the add script
    runs execute(197) once, after the Image Link; read-only otherwise."""
    import subprocess

    def run(js):
        f = tmp_path / "t.js"
        f.write_text(_NODE_FAKES + js + "\nconsole.log(JSON.stringify("
                     "{out: Out, calls: calls}));", encoding="ascii")
        return json.loads(subprocess.run(["node", str(f)], capture_output=True,
                                         text=True, check=True).stdout)
    pr = run(ts.probe_script())
    assert pr["calls"] == []
    p = ts.parse_probe(ts.parse_raw(pr["out"]))
    assert p["use"] == "action_execute_id_AddPointingSample"
    assert p["execute"]["type"] == "function"
    assert p["actions"]["AddPointingSample"]["value"] == "197"
    assert p["modules"]["OpticalTubeAssembly"] == {"modelPoints": "84"}
    assert "Connect" in p["objects"]["sky6RASCOMTheSky"]["members"]
    sm = run(ts.sample_script(r"C:\x\a.fits", 0.472,
                              add="action_execute_id_AddPointingSample"))
    assert sm["calls"] == ["imagelink", "execute:197"]
    kv = tc.parse_kv(sm["out"])
    assert kv["added"] == "1" and kv["add_result"] == "0"
    ro = run(ts.sample_script(r"C:\x\a.fits", 0.472))
    assert ro["calls"] == ["imagelink"]
    # not solved: no add
    nsj_js = ts.sample_script(r"C:\x\a.fits", 0.472,
                              add="action_execute_id_AddPointingSample")
    f = tmp_path / "n.js"
    f.write_text(_NODE_FAKES.replace("succeeded: 1", "succeeded: 0") + nsj_js
                 + "\nconsole.log(JSON.stringify({out: Out, calls: calls}));",
                 encoding="ascii")
    out = json.loads(subprocess.run(["node", str(f)], capture_output=True,
                                    text=True, check=True).stdout)
    assert out["calls"] == ["imagelink"]
    assert "not solved" in tc.parse_kv(out["out"])["add_error"]


# ------------------------------------- probe robustness (2026-10-08 fix)

_HOSTILE = """
// build-14139-like engine quirks: execute's text cannot be read, a host
// property getter throws, Object.getOwnPropertyNames is missing
TheSkyXAction.execute.toString = function () { throw new Error('slot text'); };
Object.defineProperty(sky6RASCOMTheSky, 'pointingModel',
  {enumerable: true, get: function () { throw new Error('host getter'); }});
Object.defineProperty(OpticalTubeAssembly, 'modelRMS',
  {enumerable: true, get: function () { throw new Error('ota getter'); }});
Object.getOwnPropertyNames = undefined;
"""


def _node_client(tmp_path, extra="", fail_if=None):
    """A TheSkyClient whose run_script runs the JS in node against the
    build-14139 fakes; fail_if(js) -> True raises like an engine-level
    failure (TheSky answers with an error instead of Out)."""
    import subprocess

    class _Node(tc.TheSkyClient):
        def __init__(self):
            super().__init__("x", 3040)
            self.sent = []

        def run_script(self, js):
            self.sent.append(js)
            if fail_if and fail_if(js):
                raise tc.TheSkyError("TheSky script error: SyntaxError (out='')")
            f = tmp_path / f"p{len(self.sent)}.js"
            f.write_text(_NODE_FAKES + extra + js + "\nconsole.log(JSON.stringify("
                         "{out: Out, calls: calls}));", encoding="ascii")
            r = json.loads(subprocess.run(["node", str(f)], capture_output=True,
                                          text=True, check=True).stdout)
            assert r["calls"] == [], r["calls"]          # the probe calls nothing
            return r["out"]
    return _Node()


@pytest.mark.skipif(__import__("shutil").which("node") is None,
                    reason="node not installed")
def test_one_throwing_probe_item_does_not_blank_the_rest(tmp_path):
    """The 2026-10-08 scope run printed None for every probe field. Every
    item is now its own guarded push: items that throw read ERR:<why> and
    every other item still reads, in one script and stage by stage."""
    cl = _node_client(tmp_path, extra=_HOSTILE)
    kv = ts.parse_raw(cl.run_script(ts.probe_script()))      # all items, one script
    p = ts.parse_probe(kv)
    assert p["execute"]["type"] == "function"
    assert p["execute"]["text"] is None and "slot text" in p["item_errors"]["act_src_execute"]
    assert p["actions"]["AddPointingSample"]["value"] == "197"
    assert p["use"] == "action_execute_id_AddPointingSample"
    assert "Connect" in p["objects"]["sky6RASCOMTheSky"]["members"]
    assert "pointingModel" in p["objects"]["sky6RASCOMTheSky"]["members"]
    assert p["modules"]["OpticalTubeAssembly"] == {"modelPoints": "84"}  # bad getter skipped
    assert isinstance(p["globals"], list) and "globals" not in p["item_errors"]

    rec = ts.run_probe(_cfg(tmp_path), client=_node_client(tmp_path, extra=_HOSTILE),
                       persist=False)
    assert rec["ok"] and rec["stages"] == {s: "ok" for s in ts.PROBE_STAGES}
    assert rec["actions"]["TPointAddOn2"]["value"] == "147"
    assert "act_src_execute: Error: slot text" in ts.format_probe(rec)


@pytest.mark.skipif(__import__("shutil").which("node") is None,
                    reason="node not installed")
def test_a_stage_the_engine_rejects_is_resent_item_by_item(tmp_path):
    """An item that kills the whole script (an engine-level error TheSky
    reports instead of Out) costs only that item."""
    cl = _node_client(tmp_path, fail_if=lambda js: "ps_src(TheSkyXAction" in js)
    rec = ts.run_probe(_cfg(tmp_path), client=cl, persist=False)
    assert rec["ok"]
    assert rec["stages"]["execute"].startswith("per item")
    assert rec["execute"]["length"] == "1" and rec["execute"]["text"] is None
    assert "SyntaxError" in rec["item_errors"]["act_src_execute"]
    assert rec["execute"]["type"] == "function"               # basic stage intact
    assert rec["objects"]["ImageLink"]["members"] == ["execute"]
    assert len(cl.sent) == len(ts.PROBE_STAGES) + 2           # execute: 2 items


def test_probe_reply_without_pairs_is_not_silently_empty(tmp_path):
    """TheSky can answer an engine failure with just a message: that is a
    failed probe (named), not a probe whose every field is None."""
    rec = ts.run_probe(_cfg(tmp_path), client=_Fake("TypeError: Result of expression"),
                       persist=False)
    assert not rec["ok"] and "reply not parsed: TypeError" in rec["error"]
    assert "FAILED" in ts.format_probe(rec)


def test_probe_unreachable_fails_fast(tmp_path):
    class _Down(tc.TheSkyClient):
        n = 0

        def run_script(self, js):
            _Down.n += 1
            raise tc.TheSkyError("TheSky TCP x:3040: refused") from OSError("refused")
    rec = ts.run_probe(_cfg(tmp_path), client=_Down("x", 3040), persist=False)
    assert not rec["ok"] and "refused" in rec["error"] and _Down.n == 1


def test_probe_items_are_es5_and_individually_guarded():
    js = ts.probe_script()
    for _st, k, expr in ts.probe_items():
        assert f"ps_t('{k}', function(){{return {expr};}});" in js
    assert js.rstrip().endswith("Out=ps_o.join(';');")
    for bad in ("=>", "let ", "const ", "`", "class ", "...", "Object.keys(",
                "Array.isArray(", ".forEach(", ".map(", ".filter("):
        assert bad not in js, bad
    assert "typeof Object.getOwnPropertyNames=='function'" in js
    assert {i[0] for i in ts.probe_items()} == set(ts.PROBE_STAGES)
    for st in ts.PROBE_STAGES:
        part = ts.probe_script(st)
        assert part.count("ps_t('") == sum(1 for i in ts.probe_items() if i[0] == st)
