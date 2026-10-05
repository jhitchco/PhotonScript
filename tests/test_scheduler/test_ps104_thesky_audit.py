"""PS-104 TheSky / TPoint settings audit (report only): the read-only script
denylist over every script the TheSky client can send, reply parsing, the
desired-state file, evaluation of every row form, the Image Link check with
an injected ASTAP runner and a fake TheSky, the TPoint rebuild flag, the
manual record, the NINA Center log parser, routes, the Guiding tab section
and the night report line.

The NINA fixture (fixtures/nina/documented-format_*.log) is written from
NINA's documented CenteringSolver message, not copied from a real night log
(none was available on the desktop): verify on a real log before trusting
the numbers (MAINTENANCE.md)."""
import asyncio
import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import nina_center_log as ncl
from photonscript.scheduler import thesky_audit as ta
from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import thesky_client as tc
from photonscript.telescope_agent.thesky_client import TheSkyClient, TheSkyError

ROOT = Path(__file__).resolve().parents[2]
JS = ROOT / "photonscript" / "scheduler" / "static" / "js" / "thesky_panels.js"
NINA_FIX = Path(__file__).parent / "fixtures" / "nina"
NINA_LOG = "documented-format_20260930-184001-3.2.0.9001.7000-202609.log"


def _cfg(tmp_path=None, **kw):
    if tmp_path is not None:
        kw.setdefault("data_dir", tmp_path / "data")
        kw.setdefault("nina_logs_dir", str(tmp_path / "ninalogs"))
    kw.setdefault("thesky_tcp_host", "127.0.0.1")
    kw.setdefault("thesky_tcp_port", 1)          # refused at once
    return PhotonScriptConfig(_env_file=None, **kw)


# --------------------------------------------------------------------------
# the read-only denylist (mandatory)
# --------------------------------------------------------------------------

_THESKY_OBJ = (r"(?:sky6\w+|ccdsoft\w+|SelectedHardware|AutomatedImageLinkSettings|"
               r"ImageLink|ImageLinkResults|Application|TheSkyXAction|ClosedLoopSlew|"
               r"TPoint\w*|ProTrack\w*)")
DENY = [
    (r"\bConnect\s*\(", "Connect()"),                    # may unpark / takes the camera
    (r"\bConnectAndDoNotUnpark\s*\(", "connect"),
    (r"\bDisconnect\w*\s*\(", "disconnect"),
    (r"Unpark", "unpark"),
    (r"\bPark\w*\s*\(", "park"),
    (r"\bSlew\w*\s*\(|SlewTo|ClosedLoopSlew", "slew"),
    (r"\bSync\w*\s*\(", "sync"),
    (r"\bJog\s*\(", "jog"),
    (r"SetTracking", "tracking"),
    (r"TakeImage|\bTakeImageLink|\bTakePicture", "take image"),
    (r"SetDocumentProperty", "document property write"),
    (r"\bAbort\w*\s*\(", "abort"),
    (r"FindHome|\bHome\s*\(", "home"),
    (r"DoCommandStr", "undocumented DoCommandStr"),
    (r"DoCommand\s*\(\s*\d+\s*,(?!\s*''\s*\)|\s*\"\"\s*\))", "DoCommand with a set argument"),
    (r"\b(?:Run|Execute)ScriptFile|\bShellExecute", "external run"),
]


def _violations(js: str) -> list[str]:
    bad = [what for pat, what in DENY if re.search(pat, js)]
    for m in re.finditer(_THESKY_OBJ + r"\.(\w+)\s*=(?!=)", js):
        prop = m.group(0).split("=")[0].strip()
        prop = re.sub(r"\s+", "", prop)
        if prop not in tc.IMAGELINK_INPUTS:
            bad.append(f"assignment to {prop}")
    return bad


def test_denylist_catches_known_bad_scripts():
    for js in ("sky6RASCOMTele.Connect();", "sky6RASCOMTele.Unpark();",
               "sky6RASCOMTele.Park();", "sky6RASCOMTele.SlewToRaDec(1,2,'x');",
               "sky6RASCOMTele.Sync(1,2,'x');", "sky6RASCOMTele.Jog(5,'N');",
               "sky6RASCOMTele.SetTracking(1,1,0,0);", "ccdsoftCamera.TakeImage();",
               "ccdsoftCamera.Connect();", "sky6StarChart.SetDocumentProperty(0, 31);",
               "ClosedLoopSlew.exec();", "sky6RASCOMTele.Abort();",
               "sky6RASCOMTele.DoCommandStr(\"ProTrack\", \"true\");",
               "sky6RASCOMTele.DoCommand(13, '1');",
               "AutomatedImageLinkSettings.imageScale = 0.48;",
               "ccdsoftCamera.BinX = 2;"):
        assert _violations(js), js
    # reads, comparisons and the allowed Image Link inputs are clean
    for js in ("var c = sky6RASCOMTele.IsConnected;", "if (a == ccdsoftCamera.BinX) {}",
               "sky6RASCOMTele.IsParked()", "sky6RASCOMTele.DoCommand(13, '')",
               "ImageLink.scale = 0.239;"):
        assert not _violations(js), js


def _all_scripts() -> dict[str, str]:
    out = dict(tc.READ_ONLY_JS)
    out["imagelink_script"] = tc.imagelink_script(r"C:\data\thesky_audit\tmp\x.fits", 0.239)
    out["onsite_script"] = tc.onsite_script()
    return out


def test_every_script_is_read_only():
    for name, js in _all_scripts().items():
        assert not _violations(js), (name, _violations(js))
        assert js.isascii(), name


class _Capture(TheSkyClient):
    """Records every script; replies with a plausible value per script."""
    def __init__(self):
        super().__init__("x", 3040)
        self.sent: list[str] = []

    def run_script(self, js):
        self.sent.append(js)
        if js == tc.READ_ONLY_JS["ping"]:
            return "ok"
        if js == tc.READ_ONLY_JS["mount_status"]:
            return "0,,,"
        return "a=1"


def test_every_client_method_sends_only_clean_scripts():
    cl = _Capture()
    cl.ping()
    cl.get_mount_status()
    for name in ("selected_hardware", "mount_flags", "site", "ails", "camera_flags",
                 "version", "allsky_flags"):
        getattr(cl, name)()
    cl.imagelink_file(r"C:\tmp\a.fits", 0.239)
    assert len(cl.sent) == 10
    for js in cl.sent:
        assert not _violations(js), js
    # the two latent traps found while grooming are gone
    assert not hasattr(TheSkyClient, "set_protrack")
    src = (ROOT / "photonscript" / "telescope_agent" / "thesky_client.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines()
                     if '"' in line or "'" in line)        # string literals only
    assert "DoCommandStr(" not in code.replace("DoCommandStr, which", "")
    assert "Connect();" not in tc.READ_ONLY_JS["mount_status"]


def test_only_imagelink_inputs_are_assigned_and_bad_paths_refused():
    js = tc.imagelink_script(r"C:\d\thesky_audit\tmp\imagelink_1.fits", 0.239)
    assigned = {re.sub(r"\s+", "", m.group(0).split("=")[0])
                for m in re.finditer(_THESKY_OBJ + r"\.(\w+)\s*=(?!=)", js)}
    assert assigned == set(tc.IMAGELINK_INPUTS)
    assert "C:/d/thesky_audit/tmp/imagelink_1.fits" in js
    for bad in ("C:\\x';sky6RASCOMTele.Park();'.fits", "C:\\caf\u00e9.fits", "a|b.fits"):
        with pytest.raises(TheSkyError):
            tc.imagelink_script(bad, 0.239)
    with pytest.raises(TheSkyError):
        tc.imagelink_script("C:\\a.fits", 0)


def test_onsite_script_lists_every_read_but_not_the_docommand():
    js = tc.onsite_script()
    for name, (pairs, _pre) in tc.READ_PAIRS.items():
        for k, _expr in pairs:
            if name == "allsky_flags":
                assert f"{name}.{k}" not in js
            else:
                assert f"'{name}.{k} = '" in js
    assert "DoCommand" not in js


# --------------------------------------------------------------------------
# reply parsing
# --------------------------------------------------------------------------

def test_parse_kv_and_missing_properties():
    d = tc.parse_kv("camera=OGMA AP26MC;mount=Paramount MX+;focuser=?ERR;x=;y=undefined")
    assert d == {"camera": "OGMA AP26MC", "mount": "Paramount MX+", "focuser": None,
                 "x": None, "y": None}
    assert tc.parse_kv("") == {}


class _Reply(TheSkyClient):
    def __init__(self, replies):
        super().__init__("x", 3040)
        self.replies = replies

    def run_script(self, js):
        for name, reply in self.replies.items():
            if js == tc.READ_ONLY_JS.get(name):
                if isinstance(reply, Exception):
                    raise reply
                return reply
        raise TheSkyError("unexpected script")


def test_mount_status_never_connects_and_parses_both_states():
    assert _Reply({"mount_status": "0,,,"}).get_mount_status() == {
        "connected": False, "ra_hours": None, "dec_deg": None, "tracking": None}
    s = _Reply({"mount_status": "1,12.5,-3.25,1"}).get_mount_status()
    assert s == {"connected": True, "ra_hours": 12.5, "dec_deg": -3.25, "tracking": True}
    with pytest.raises(TheSkyError):
        _Reply({"mount_status": "garbage"}).get_mount_status()


def _fake_thesky(**over):
    jd = time.time() / 86400.0 + 2440587.5 + 0.5 / 86400.0
    r = {"ping": "ok",
         "version": "version=10.5.0;build=13400",
         "selected_hardware": "camera=ASCOM Camera (OGMA AP26MC);mount=Paramount MX+;"
                              "filter_wheel=<No Filter Wheel Selected>;focuser=?ERR;autoguider=?ERR",
         "mount_flags": "connected=1;parked=0;tracking=1;ra_h=5.1;dec_d=20.0;last_slew_error=0",
         "site": f"latitude=31.9069;longitude=109.0211;time_zone=-7;elevation_m=1300;"
                 f"dst_index=0;use_computer_clock=1;jd_now={jd:.8f}",
         "ails": "image_scale=0.48;position_angle=0;exposure_s=10;fovs=8;retries=2;filter=?ERR",
         "camera_flags": "status=Not Connected;bin_x=2;bin_y=2;autosave_path=;image_reduction=0",
         "allsky_flags": "allsky_scripted=1;allsky_automated=1"}
    r.update(over)
    return _Reply(r)


def test_collect_thesky_maps_every_read():
    o, src = ta.collect_thesky(_cfg(), _fake_thesky())
    assert src["ok"] and o["thesky_scripting"] is True
    assert o["thesky_version"] == "10.5.0 build 13400"
    assert o["mount_selected"] == "Paramount MX+" and o["mount_connected"] is True
    assert o["mount_parked"] is False and o["mount_tracking"] is True
    assert o["site_latitude"] == pytest.approx(31.9069)
    assert abs(o["site_clock"]) < 2.0 and o["site_clock_latency_s"] >= 0
    assert o["ails_image_scale"] == 0.48 and o["ails_fovs"] == 8
    assert o["camera_status"] == "Not Connected" and o["camera_bin"] == "2x2"
    assert o["autosave_path"] == ""                # set but empty
    assert "allsky_automated" not in o            # B2: no DoCommand read by default
    o2, _ = ta.collect_thesky(_cfg(thesky_audit_allsky_read=True), _fake_thesky())
    assert o2["allsky_automated"] is True


def test_one_failing_read_leaves_the_rest():
    o, src = ta.collect_thesky(_cfg(), _fake_thesky(ails=TheSkyError("TypeError. Error = 415.")))
    assert "ails_image_scale" not in o and o["mount_selected"] == "Paramount MX+"
    assert "ails" in src["note"]


def test_unreachable_thesky_is_unknown_not_an_error(tmp_path):
    a = ta.run_audit(_cfg(tmp_path))
    rows = {r["id"]: r for r in a["rows"]}
    assert rows["thesky_scripting"]["status"] == "warn"
    assert rows["site_latitude"]["status"] == "unknown"
    assert "not reachable" in rows["site_latitude"]["note"]
    assert (tmp_path / "data" / "thesky_audit" / f"{a['night']}.json").exists()
    assert ta.summary_line(a).startswith("TheSky audit: 0 fail")


# --------------------------------------------------------------------------
# desired file + evaluation
# --------------------------------------------------------------------------

def test_desired_file_lints_clean_and_has_every_groomed_row():
    d = ta.load_desired(_cfg())
    assert not d.get("error") and not d.get("lint"), d.get("lint")
    ids = {r["id"] for r in d["check"]}
    for want in ("thesky_scripting", "thesky_version", "mount_selected", "mount_connected",
                 "mount_tracking", "site_latitude", "site_longitude", "site_elevation",
                 "use_computer_clock", "site_clock", "time_zone_dst", "camera_selected",
                 "camera_status", "filter_wheel_selected", "autosave_path", "filter_in_beam",
                 "ails_exposure_s", "ails_image_scale", "ails_fovs", "ails_retries",
                 "ails_filter", "ails_position_angle", "allsky_automated", "allsky_scripted",
                 "run_binning", "imagelink_selftest", "true_scale", "camera_pa_stable",
                 "parity", "solve_stars", "allsky_db_installed", "ucac4_installed",
                 "gaia_installed", "tpoint_model_active", "tpoint_points", "tpoint_rms_arcsec",
                 "tpoint_polar_error_arcmin", "tpoint_model_age_days", "protrack_on",
                 "tpoint_rebuild", "first_slew_error", "first_slew_pattern", "mount_vs_solve"):
        assert want in ids, want
    assert not any("apply" in r for r in d["check"])            # report only
    bad = dict(d["check"][0], apply="api")
    assert ta.lint_desired({"check": [bad]})


def _eval(observed, cfg=None):
    cfg = cfg or _cfg()
    obs = {s: {} for s in ta.SOURCES}
    obs.update(observed)
    res = ta.evaluate(ta.load_desired(cfg), obs, cfg)
    return {r["id"]: r for r in res["rows"]}, res


def test_site_rows_and_a_strict_longitude():
    # PS-120: the script's sign cannot tell E from W; without TheSky's LST
    # the row is unknown (verify by eye), never a false FAIL
    rows, _ = _eval({"thesky-script": {"site_latitude": 31.9069, "site_longitude": 109.0212,
                                       "site_elevation": 1300, "use_computer_clock": True,
                                       "site_clock": 0.4}})
    assert rows["site_latitude"]["status"] == "pass"
    assert rows["site_longitude"]["status"] == "unknown"
    assert "verify by eye" in rows["site_longitude"]["note"]
    assert rows["site_clock"]["status"] == "pass"
    rows, _ = _eval({"thesky-script": {"site_latitude": 32.5, "site_longitude": -105.0,
                                       "site_clock": 5.0, "use_computer_clock": False}})
    assert rows["site_latitude"]["status"] == "fail"
    assert rows["site_longitude"]["status"] == "fail"
    assert rows["site_clock"]["status"] == "warn"
    assert rows["use_computer_clock"]["status"] == "fail"


def test_image_scale_pass_at_bin2_and_fail_in_the_4x4_era():
    astap = {"astap": {"true_scale": 0.239}}
    rows, _ = _eval({"thesky-script": {"ails_image_scale": 0.480}, **astap,
                     "manual": {"run_binning": 2}})
    assert rows["ails_image_scale"]["status"] == "pass"
    assert "0.478" in rows["ails_image_scale"]["desired"]
    # PS-120: no record: 1x1 assumed (the 2026-10-04 run)
    rows, _ = _eval({"thesky-script": {"ails_image_scale": 0.239}, **astap})
    assert rows["ails_image_scale"]["status"] == "pass"
    assert "assumed" in rows["ails_image_scale"]["desired"]
    rows, _ = _eval({"thesky-script": {"ails_image_scale": 0.942}, **astap})
    r = rows["ails_image_scale"]
    assert r["status"] == "fail" and "4x4" in r["note"]
    rows, _ = _eval({"thesky-script": {"ails_image_scale": 0.239}, **astap,
                     "manual": {"run_binning": 1}})
    assert rows["ails_image_scale"]["status"] == "pass"
    assert "manual record" in rows["ails_image_scale"]["desired"]
    # no ASTAP check yet: config pixel scale stands in
    rows, _ = _eval({"thesky-script": {"ails_image_scale": 0.480}, "manual": {"run_binning": 2}})
    assert rows["ails_image_scale"]["status"] == "pass"
    assert "config" in rows["ails_image_scale"]["desired"]


def test_mount_connected_fails_only_while_armed():
    rows, _ = _eval({"thesky-script": {"mount_connected": False}, "armer_state": "ARMED"})
    assert rows["mount_connected"]["status"] == "fail"
    rows, _ = _eval({"thesky-script": {"mount_connected": False}, "armer_state": "DISARMED"})
    assert rows["mount_connected"]["status"] == "info"
    rows, _ = _eval({})
    assert rows["mount_connected"]["status"] == "unknown"
    assert rows["mount_tracking"]["status"] == "unknown"
    rows, _ = _eval({"thesky-script": {"mount_connected": True, "mount_parked": True,
                                       "mount_tracking": False}})
    assert rows["mount_connected"]["status"] == "pass"
    assert rows["mount_tracking"]["current"] == "parked"


def test_camera_filter_and_autosave_rows(tmp_path):
    rows, _ = _eval({"thesky-script": {"camera_selected": "<No Camera Selected>",
                                       "autosave_path": ""},
                     "pointing-log": {"last_filter": "H", "last_filter_src": "NINA log x"}})
    assert rows["camera_selected"]["status"] == "warn"
    assert "cannot take a picture" in rows["camera_selected"]["note"]
    assert rows["autosave_path"]["status"] == "warn"
    assert rows["filter_in_beam"]["status"] == "warn"
    rows, _ = _eval({"thesky-script": {"camera_selected": "ASCOM Camera",
                                       "autosave_path": str(tmp_path)},
                     "pointing-log": {"last_filter": "L"}})
    assert rows["camera_selected"]["status"] == "info"
    assert rows["autosave_path"]["status"] == "pass"
    assert rows["filter_in_beam"]["status"] == "pass"


def test_manual_rows_and_staleness(tmp_path):
    cfg = _cfg(tmp_path)
    res = ta.save_manual(cfg, {"model_date": "2026-09-20", "points": 40, "rms_arcsec": 12,
                               "polar_az_arcmin": 1.2, "polar_alt_arcmin": 1.6,
                               "model_active": True, "protrack_on": "yes", "run_binning": 2,
                               "allsky_db_installed": False})
    assert res["ok"], res
    obs, src = ta.manual_observed(ta.load_manual(cfg), cfg)
    assert src["ok"] and obs["tpoint_polar_error_arcmin"] == 2.0
    rows, _ = _eval({"manual": obs, "manual_record": ta.load_manual(cfg)}, cfg)
    assert rows["tpoint_points"]["status"] == "warn"            # 40 < 50
    assert rows["tpoint_rms_arcsec"]["status"] == "pass"
    assert rows["tpoint_polar_error_arcmin"]["status"] == "pass"  # 2.0 <= 2
    assert rows["protrack_on"]["status"] == "pass"
    assert rows["allsky_db_installed"]["status"] == "warn"
    assert rows["run_binning"]["status"] == "pass"
    # older than thesky_manual_max_age_days: unknown, re-enter
    rec = ta.load_manual(cfg)
    rec["entered_at"] = (datetime.utcnow() - timedelta(days=31)).strftime("%Y-%m-%dT%H:%M:%SZ")
    obs, src = ta.manual_observed(rec, cfg)
    assert obs == {} and "re-enter" in src["note"]


def test_manual_validation():
    rec, bad = ta.validate_manual({"model_date": "20/09/2026", "points": 1.5,
                                   "run_binning": 9, "protrack_on": "maybe", "bogus": 1,
                                   "notes": "caf\u00e9"})
    assert {b.split(":")[0] for b in bad} == {"model_date", "points", "run_binning",
                                             "protrack_on", "bogus", "notes"}
    rec, bad = ta.validate_manual({"model_date": "2026-09-20", "points": "120"})
    assert not bad and rec == {"model_date": "2026-09-20", "points": 120}


# --------------------------------------------------------------------------
# TPoint rebuild flag
# --------------------------------------------------------------------------

def _days_ago(n):
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


def _rebuild(observed, cfg=None):
    rows, _ = _eval(observed, cfg)
    return rows["tpoint_rebuild"]


def test_rebuild_by_age():
    r = _rebuild({"manual_record": {"model_date": _days_ago(100)}})
    assert r["status"] == "fail" and r["current"] == "REBUILD" and "100 d old" in r["note"]
    r = _rebuild({"manual_record": {"model_date": _days_ago(70)}})
    assert r["status"] == "warn" and r["current"] == "watch"
    r = _rebuild({"manual_record": {"model_date": _days_ago(10)}})
    assert r["status"] == "pass" and r["current"] == "no"
    assert _rebuild({})["status"] == "unknown"


def test_rebuild_by_equipment_change():
    cfg = _cfg(piggyback_enabled=True)
    r = _rebuild({"manual_record": {"model_date": "2026-09-01"}}, cfg)
    assert r["status"] == "fail" and "dual-rig" in r["note"]
    r = _rebuild({"manual_record": {"model_date": _days_ago(5),
                                    "equipment_changed_on": _days_ago(1)}})
    assert r["status"] == "fail" and "equipment changed" in r["note"]
    r = _rebuild({"manual_record": {"model_date": _days_ago(5), "rigs": ["rc16"]},
                  "rigs": ["rc16", "piggyback"]})
    assert r["status"] == "fail" and "rigs changed" in r["note"]


def test_rebuild_by_camera_angle_and_scale():
    md = _days_ago(10)
    hist = [{"t_utc": md + "T05:00:00Z", "pa": 90.0, "parity": -1, "native_scale": 0.239}]
    base = {"manual_record": {"model_date": md}, "astap_history": hist}
    r = _rebuild(dict(base, astap={"astap_pa": 270.4, "true_scale": 0.239}))
    assert r["status"] == "pass"                     # a flip turns the image 180 deg
    r = _rebuild(dict(base, astap={"astap_pa": 92.5, "true_scale": 0.239}))
    assert r["status"] == "fail" and "camera angle" in r["note"]
    rows, _ = _eval(dict(base, astap={"astap_pa": 92.5, "true_scale": 0.239}))
    assert rows["camera_pa_stable"]["status"] == "warn"
    r = _rebuild(dict(base, astap={"astap_pa": 90.0, "true_scale": 0.2355}))
    assert r["status"] == "fail" and "image scale moved" in r["note"]


def test_rebuild_by_first_slew_trend():
    pl = {"by_side": {"E": {"n": 20, "median_arcmin": 3.9}, "W": {"n": 18, "median_arcmin": 4.2}},
          "side_src": ["ha"]}
    rows, _ = _eval({"manual_record": {"model_date": _days_ago(10)}, "pointing-log": pl})
    assert rows["tpoint_rebuild"]["status"] == "warn"
    assert rows["first_slew_error"]["status"] == "warn"
    assert rows["first_slew_error"]["current"] == "3.9' E (n=20) / 4.2' W (n=18)"
    pl["by_side"]["W"]["median_arcmin"] = 6.0
    rows, res = _eval({"manual_record": {"model_date": _days_ago(10)}, "pointing-log": pl})
    assert rows["tpoint_rebuild"]["status"] == "fail"
    assert rows["first_slew_error"]["status"] == "fail"
    assert res["rebuild"]["current"] == "REBUILD" and res["rebuild"]["reasons"]


# --------------------------------------------------------------------------
# Image Link check (injected ASTAP runner, fake TheSky)
# --------------------------------------------------------------------------

def _kv(scale_arcsec, pa=10.0, parity=-1):
    from photonscript.scheduler.solve_store import nominal_cd
    cd = nominal_cd(scale_arcsec, pa, parity)
    return {"PLTSOLVD": "T", "CRVAL1": "310.35", "CRVAL2": "42.27",
            "CD1_1": str(cd[0][0]), "CD1_2": str(cd[0][1]),
            "CD2_1": str(cd[1][0]), "CD2_2": str(cd[1][1])}


def _night_with_subs(cfg, tmp_path, night=None, filters=("H", "L")):
    # the noon-to-noon night (store.night_of), not the calendar date: before
    # local noon "tonight" is still yesterday's night
    from photonscript.shared import phd2_store
    night = night or phd2_store.night_of(cfg)
    runs = Path(cfg.data_dir) / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    caps = tmp_path / "caps"
    caps.mkdir(exist_ok=True)
    lines = []
    for i, f in enumerate(filters):
        p = caps / f"sub_{i}_{f}.fits"
        p.write_bytes(b"not really a fits file")
        lines.append(json.dumps({"file": p.name, "abs_path": str(p), "filter": f,
                                 "rig": "rc16", "passed_qa": True,
                                 "time": f"{night}T22:0{i}:00"}))
    (runs / f"{night}_subs.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return night


def test_imagelink_check_solves_the_newest_l_frame(tmp_path):
    cfg = _cfg(tmp_path)
    night = _night_with_subs(cfg, tmp_path, filters=("L", "H", "L", "O"))
    seen = []

    def runner(path, fov, hint, r, t):
        seen.append(Path(path).name)
        return _kv(0.2391)
    chk = ta.imagelink_check(cfg, runner=runner)
    assert chk["ok"] and seen == ["sub_2_L.fits"]
    assert chk["astap"]["native_scale"] == pytest.approx(0.2391, abs=1e-4)
    assert chk["astap"]["parity"] == -1
    assert (Path(cfg.data_dir) / "solves" / night / "rc16.jsonl").exists()   # stored as usual
    a = ta.run_audit(cfg, armer_state="DISARMED")
    rows = {r["id"]: r for r in a["rows"]}
    assert rows["true_scale"]["status"] == "pass"
    assert rows["imagelink_selftest"]["status"] == "unknown"


def test_imagelink_check_without_a_frame_says_so(tmp_path):
    chk = ta.imagelink_check(_cfg(tmp_path), runner=lambda *a: _kv(0.239))
    assert not chk["ok"] and "no RC16 L" in chk["note"]


class _FakeImageLink(_Reply):
    def __init__(self, reply):
        super().__init__({})
        self.reply = reply
        self.paths = []

    def imagelink_file(self, path, scale):
        self.paths.append(path)
        assert Path(path).exists()                   # the copy, not the capture
        assert "thesky_audit" in str(path)
        return tc.parse_kv(self.reply)


def test_thesky_imagelink_only_when_idle_and_on_a_deleted_copy(tmp_path):
    cfg = _cfg(tmp_path)
    _night_with_subs(cfg, tmp_path, filters=("L",))
    fake = _FakeImageLink("exec_error=;succeeded=1;error_code=0;error_text=;image_scale=0.2393;"
                          "position_angle=10;mirrored=0;image_stars=300;solution_rms=0.6;"
                          "solution_stars=120;catalog_stars=200")
    run = lambda *a: _kv(0.239)                       # noqa: E731
    busy = ta.imagelink_check(cfg, runner=run, thesky=True, client=fake, armer_state="RUNNING")
    assert busy["thesky"]["skipped"] and not fake.paths
    unknown = ta.imagelink_check(cfg, runner=run, thesky=True, client=fake, armer_state="")
    assert unknown["thesky"]["skipped"]               # unknown state is not idle
    ok = ta.imagelink_check(cfg, runner=run, thesky=True, client=fake, armer_state="DISARMED")
    assert ok["thesky"]["succeeded"] is True and len(fake.paths) == 1
    assert not Path(fake.paths[0]).exists()           # copy deleted
    assert (tmp_path / "caps" / "sub_0_L.fits").exists()   # capture untouched
    rows = {r["id"]: r for r in ta.run_audit(cfg)["rows"]}
    assert rows["imagelink_selftest"]["status"] == "pass"
    fail = _FakeImageLink("succeeded=0;error_code=655;error_text=Not enough stars")
    ta.imagelink_check(cfg, runner=run, thesky=True, client=fail, armer_state="COMPLETE")
    rows = {r["id"]: r for r in ta.run_audit(cfg)["rows"]}
    assert rows["imagelink_selftest"]["status"] == "fail"
    assert "655" in rows["imagelink_selftest"]["note"]


# --------------------------------------------------------------------------
# NINA Center log
# --------------------------------------------------------------------------

def _nina_text():
    return (NINA_FIX / NINA_LOG).read_text(encoding="utf-8")


def test_center_log_parse_and_runs():
    lines = ncl.parse(_nina_text())
    assert len(lines) == 4                           # the garbled line is skipped
    runs = ncl.group_runs(lines, _cfg())
    assert len(runs) == 2
    a, b = runs
    assert a["first_sep_arcmin"] == pytest.approx(3.73, abs=0.02)
    assert a["first_east_arcmin"] == pytest.approx(2.78, abs=0.02)
    assert a["first_north_arcmin"] == pytest.approx(2.5, abs=0.01)
    assert a["attempts"] == 2 and a["converged"] is True
    assert b["first_sep_arcmin"] == pytest.approx(5.0, abs=0.01)
    assert b["dec_band"] == "Dec < 0"
    for r in runs:
        assert r["side"] == ("E" if r["ha_h"] < 0 else "W") and r["side_src"] == "ha"
    assert ncl.last_filter(_nina_text()) == "H"


def test_center_log_distance_fallback_and_pier_phrase():
    deg = "\u00b0"
    line = ("2026-09-30T21:10:05.5|INFO|CenteringSolver.cs|Center|1|Centering Solver - "
            f"Centering Coordinates: RA: 01:00:00; Dec: 10{deg} 00' 00\"; Epoch: J2000; "
            f"Separation RA: 00:00:00; Dec: 00{deg} 02' 00\"; Distance: 00{deg} 02' 30\"; "
            "pier side: pierWest; Threshold: 1")
    (r,) = ncl.parse(line)
    assert r["sep_arcmin"] == pytest.approx(2.5) and r["pier"] == "W"
    (run,) = ncl.group_runs([r], _cfg())
    assert run["side"] == "W" and run["side_src"] == "log"


def test_center_night_pass_and_summary_with_ps67(tmp_path):
    cfg = _cfg(tmp_path, nina_base_url="http://localhost:1888/v2/api")
    logs = Path(cfg.nina_logs_dir)
    logs.mkdir(parents=True)
    (logs / NINA_LOG).write_text(_nina_text(), encoding="utf-8")
    night = "2026-09-30"
    res = ncl.night_pass(cfg, night)
    assert res["ok"] and len(res["runs"]) == 2
    assert ncl.load_meta(cfg, night)["last_filter"] == "H"
    runs_dir = Path(cfg.data_dir) / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f"{night}_pointing.jsonl").write_text("\n".join(json.dumps(r) for r in (
        {"rig": "rc16", "file": "a.fits", "model_err_arcmin": 9.0, "pier": "East"},
        {"rig": "rc16", "file": "a.fits", "model_err_arcmin": 1.5, "pier": "East"},  # last wins
        {"rig": "rc16", "file": "b.fits", "model_err_arcmin": 2.5, "pier": "West"},
        {"rig": "piggyback", "file": "p.fits", "model_err_arcmin": 30.0})) + "\n",
        encoding="utf-8")
    s = ncl.summary(cfg, 3, end_night="2026-10-01")
    assert s["runs"] == 2 and s["overall"]["median_arcmin"] == pytest.approx(4.37, abs=0.02)
    assert s["by_side"]["E"]["n"] == 2
    assert s["ps67"] == {"n": 2, "median_arcmin": 2.0, "by_pier": {"E": 1.5, "W": 2.5}}
    assert s["per_night"][0]["night"] == night


# --------------------------------------------------------------------------
# routes, Guiding tab, night report, config
# --------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import phd2 as r
    from photonscript.scheduler.routers import thesky as tr
    monkeypatch.setattr(app, "_config", _cfg(tmp_path))

    async def _no_probe(cfg):
        return {"ok": False, "note": "PHD2 not reachable", "age_s": 0.0}
    monkeypatch.setattr(r, "_probe_phd2", _no_probe)
    monkeypatch.setattr(tr, "_armer_state", lambda: "RUNNING")
    return TestClient(app.app)


def test_routes(client, monkeypatch):
    from photonscript.scheduler.routers import thesky as tr
    a = client.get("/api/thesky/audit?refresh=1").json()
    assert a["cached"] is False and a["counts"]["unknown"] > 10
    assert a["imagelink_thesky_allowed"] is False
    assert client.get("/api/thesky/audit").json()["cached"] is True
    r = client.get("/api/thesky/imagelink-check?thesky=1")
    assert r.status_code == 409 and "RUNNING" in r.json()["note"]
    il = client.get("/api/thesky/imagelink-check").json()
    assert il["ok"] is False and "compare" in il
    assert client.post("/api/thesky/manual", json={"points": "lots"}).status_code == 400
    assert client.post("/api/thesky/manual", json=[1]).status_code == 400
    ok = client.post("/api/thesky/manual", json={"model_date": "2026-09-20", "points": 80})
    assert ok.status_code == 200 and ok.json()["record"]["points"] == 80
    assert client.get("/api/thesky/manual").json()["record"]["model_date"] == "2026-09-20"
    js = client.get("/api/thesky/onsite-script")
    assert js.status_code == 200 and "SelectedHardware.cameraModel" in js.text
    p = client.get("/api/thesky/pointing?nights=3").json()
    assert p["runs"] == 0 and p["logs_dir_found"] is False
    monkeypatch.setattr(tr, "_armer_state", lambda: "DISARMED")
    assert client.get("/api/thesky/audit").json()["imagelink_thesky_allowed"] is True


def test_guiding_tab_section_and_panel_js(client):
    html = client.get("/guiding").text
    assert "/static/js/thesky_panels.js" in html and "THESKY.init()" in html
    sec = html[html.index('id="tpointSec"'):]
    js = JS.read_text(encoding="utf-8")
    ids = set(re.findall(r"""(?:\$|setText|setHTML|\bon)\('([A-Za-z]\w*)'""", js))
    for i in ids - {i for i in ids if i.endswith("_")}:     # tsM_<field> prefixes
        assert f'id="{i}"' in sec, i
    from photonscript.scheduler.routers import thesky
    paths = {rt.path for rt in thesky.router.routes}
    for url in re.findall(r"'(/api/thesky/[a-z/-]+)", js):
        assert url.rstrip("/") in paths, url
    assert "method: 'POST'" in js and js.count("method: 'POST'") == 1   # the manual record only
    assert "'/api/thesky/manual'" in js
    assert "data-apply" not in js and "/apply" not in js


def test_panel_js_brackets_balance_outside_strings():
    src = JS.read_text(encoding="utf-8")
    assert src.isascii()
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"'(?:\\.|[^'\\\n])*'", "''", src)
    src = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', src)
    src = re.sub(r"/\[[^\]]*\]/g", "R", src)
    pairs, stack = {")": "(", "]": "[", "}": "{"}, []
    for i, ch in enumerate(src):
        if ch in "([{":
            stack.append(ch)
        elif ch in pairs:
            assert stack and stack.pop() == pairs[ch], f"unbalanced {ch} at {i}"
    assert not stack


def test_night_report_line(tmp_path):
    cfg = _cfg(tmp_path)
    a = ta.run_audit(cfg, "arm")
    s = ta.summary(cfg, a["night"])
    assert s["line"].startswith("TheSky audit: ") and "rebuild: unknown" in s["line"]
    a["first_slew"] = "3.9' E / 4.2' W"
    a["rebuild"] = {"current": "no"}
    assert ta.summary_line(a).endswith("rebuild: no, first slew median 3.9' E / 4.2' W")
    assert ta.summary(cfg, "1999-01-01") is None
    runs = (ROOT / "photonscript" / "scheduler" / "templates" / "runs.html").read_text(encoding="utf-8")
    assert "g.thesky" in runs and "tskLine" in runs


async def test_arm_hook_is_report_only_and_never_raises(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    a = await ta.at_arm(cfg, armer_state="ARMED")
    assert a["reason"] == "arm"
    assert await ta.at_arm(_cfg(tmp_path, thesky_audit_enabled=False)) is None
    from photonscript.shared import pushover
    calls = []

    async def _push(*a, **k):
        calls.append(a)
    monkeypatch.setattr(pushover, "notify", _push)
    await ta.at_arm(cfg, armer_state="ARMED")
    assert calls == []                                 # no Pushover


def test_config_defaults_are_report_only():
    d = PhotonScriptConfig(_env_file=None)
    assert d.thesky_audit_enabled is True and d.thesky_enabled is False
    assert d.thesky_audit_imagelink_thesky is False and d.thesky_audit_allsky_read is False
    assert (d.tpoint_max_age_days, d.tpoint_min_points, d.tpoint_rms_max_arcsec,
            d.tpoint_polar_max_arcmin, d.pointing_first_slew_warn_arcmin,
            d.pointing_first_slew_fail_arcmin, d.thesky_manual_max_age_days) == (
        90.0, 50, 30.0, 3.0, 2.0, 5.0, 30.0)                  # PS-120: polar 3'
    from photonscript.scheduler.app import _CONFIG_FIELDS as CONFIG_FIELDS
    keys = {f[0] for f in CONFIG_FIELDS}
    for k in ("thesky_audit_enabled", "thesky_audit_imagelink_thesky", "thesky_audit_allsky_read",
              "tpoint_max_age_days", "pointing_first_slew_fail_arcmin",
              "thesky_manual_max_age_days"):
        assert k in keys, k


def test_cli_registers_thesky_audit():
    from photonscript.cli import app as cli_app
    names = {c.name for c in cli_app.registered_commands}
    assert "thesky-audit" in names


def test_run_audit_from_a_thread_is_fast_when_thesky_is_down(tmp_path):
    t0 = datetime.now()
    asyncio.run(asyncio.to_thread(ta.run_audit, _cfg(tmp_path)))
    assert (datetime.now() - t0).total_seconds() < 15
