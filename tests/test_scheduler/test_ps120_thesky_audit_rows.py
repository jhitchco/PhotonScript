"""PS-120: TheSky audit rows that read false FAIL / WARN on the live audit of
2026-10-05T12:50Z (after Jeremy fixed TheSky's location and built the TPoint
model on 2026-10-04). The fixtures are the values that audit showed:
longitude +109.021 (TheSky set to 109 01' 16" W), tz -7 with DST index 17
(U.S. and Canada), first slews 51.9' E (n=3) / 4.6' W (n=13) all from before
the model, run binning 1 at 0.236"/px, All Sky Image Link off without its
database, polar error 2.79'."""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import nina_center_log as ncl
from photonscript.scheduler import thesky_audit as ta
from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import thesky_client as tc

AUDIT_T = datetime(2026, 10, 5, 12, 50)
AARO_E = -109.021111                                  # east-positive


def _cfg(tmp_path=None, **kw):
    if tmp_path is not None:
        kw.setdefault("data_dir", tmp_path / "data")
        kw.setdefault("nina_logs_dir", str(tmp_path / "ninalogs"))
    kw.setdefault("thesky_tcp_host", "127.0.0.1")
    kw.setdefault("thesky_tcp_port", 1)
    return PhotonScriptConfig(_env_file=None, **kw)


def _jd(t: datetime) -> float:
    return (t - datetime(1970, 1, 1)).total_seconds() / 86400.0 + 2440587.5


def _lst_h(jd: float, lon_east: float) -> float:
    return ((ta.gmst_deg(jd) + lon_east) % 360.0) / 15.0


def _eval(observed, cfg=None):
    cfg = cfg or _cfg()
    obs = {s: {} for s in ta.SOURCES}
    obs.update(observed)
    res = ta.evaluate(ta.load_desired(cfg), obs, cfg)
    return {r["id"]: r for r in res["rows"]}, res


def _live_site(lon_east=AARO_E, **over):
    jd = _jd(AUDIT_T)
    d = {"site_latitude": 31.9069, "site_longitude": 109.021, "site_elevation": 1300,
         "use_computer_clock": True, "site_clock": 0.0, "time_zone": -7.0,
         "dst_index": 17.0, "time_zone_dst": "tz -7, DST index 17",
         "site_jd": jd, "site_lst_h": _lst_h(jd, lon_east)}
    d.update(over)
    return d


LIVE_MANUAL_RECORD = {"model_date": "2026-10-04", "entered_at": "2026-10-05T12:04:00Z",
                      "points": 100, "rms_arcsec": 20.0, "polar_az_arcmin": 1.8,
                      "polar_alt_arcmin": 2.13, "model_active": True, "protrack_on": True,
                      "run_binning": 1, "allsky_automated": False,
                      "allsky_db_installed": False, "ucac4_installed": True,
                      "gaia_installed": True}


# --------------------------------------------------------------------------
# 1. longitude: magnitude from the script, E / W from TheSky's LST
# --------------------------------------------------------------------------

def test_site_script_reads_the_lst_and_stays_read_only():
    pairs = dict(tc.READ_PAIRS["site"][0])
    assert "sky6Utils.ComputeLocalSiderealTime()" in pairs["lst_h"]
    assert tc.READ_ONLY_JS["site"].isascii()
    assert "site.lst_h" in tc.onsite_script()
    jd = _jd(AUDIT_T)
    reply = (f"latitude=31.9069;longitude=109.021;time_zone=-7;elevation_m=1300;dst_index=17;"
             f"use_computer_clock=1;jd_now={jd:.8f};lst_h={_lst_h(jd, AARO_E):.6f}")

    class _C(tc.TheSkyClient):
        def run_script(self, js):
            return "ok" if js == tc.READ_ONLY_JS["ping"] else (
                reply if js == tc.READ_ONLY_JS["site"] else "a=1")
    o, _ = ta.collect_thesky(_cfg(), _C("x", 3040))
    assert o["site_jd"] == pytest.approx(jd) and o["site_lst_h"] is not None
    assert ta.lst_longitude({"thesky-script": o}) == pytest.approx(AARO_E, abs=0.01)


def test_live_west_setting_passes():
    rows, _ = _eval({"thesky-script": _live_site()})
    r = rows["site_longitude"]
    assert r["status"] == "pass", r
    assert "109.02 W" in r["current"] and "109.02 W" in r["note"]


def test_an_east_setting_still_fails_whatever_the_script_sign():
    for v in (109.021, -109.021):
        rows, _ = _eval({"thesky-script": _live_site(lon_east=-AARO_E, site_longitude=v)})
        r = rows["site_longitude"]
        assert r["status"] == "fail", r
        assert "EAST" in r["note"] and "109.02 E" in r["current"]


def test_longitude_tolerates_tt_and_apparent_sidereal_time():
    jd = _jd(AUDIT_T)
    # TheSky's JD in TT (69 s) or its LST apparent (1 s): far inside 1 deg
    site = _live_site(site_jd=jd - 69.0 / 86400.0)
    assert _eval({"thesky-script": site})[0]["site_longitude"]["status"] == "pass"
    site = _live_site(site_lst_h=_lst_h(jd, AARO_E) * 15.0)      # a build in degrees
    assert _eval({"thesky-script": site})[0]["site_longitude"]["status"] == "pass"


def test_longitude_without_lst_is_unknown_and_wrong_magnitude_fails():
    site = _live_site()
    site.pop("site_lst_h")
    r = _eval({"thesky-script": site})[0]["site_longitude"]
    assert r["status"] == "unknown" and "verify by eye" in r["note"]
    assert "E or W" in r["current"]
    r = _eval({"thesky-script": _live_site(site_longitude=105.0)})[0]["site_longitude"]
    assert r["status"] == "fail" and "site is wrong" in r["note"]
    # LST far from both E and W (clock or another site)
    r = _eval({"thesky-script": _live_site(lon_east=-60.0)})[0]["site_longitude"]
    assert r["status"] == "fail" and "60.00 W" in r["note"]


# --------------------------------------------------------------------------
# 2. time zone and DST: index 17 = U.S. and Canada
# --------------------------------------------------------------------------

def test_live_time_zone_and_dst_passes(monkeypatch):
    monkeypatch.setattr(ta, "_utcnow", lambda: AUDIT_T)
    r = _eval({"thesky-script": _live_site()})[0]["time_zone_dst"]
    assert r["status"] == "pass", r
    assert r["current"] == "tz -7, DST U.S. and Canada (index 17)"
    monkeypatch.setattr(ta, "_utcnow", lambda: datetime(2026, 12, 1, 3, 0))
    assert _eval({"thesky-script": _live_site()})[0]["time_zone_dst"]["status"] == "pass"
    # DST not observed with -7 is an hour off while the PC is on MDT
    monkeypatch.setattr(ta, "_utcnow", lambda: AUDIT_T)
    r = _eval({"thesky-script": _live_site(dst_index=0.0)})[0]["time_zone_dst"]
    assert r["status"] == "fail" and r["current"].endswith("not observed (index 0)")
    r = _eval({"thesky-script": _live_site(dst_index=3.0)})[0]["time_zone_dst"]
    assert r["status"] == "warn" and "not U.S. and Canada" in r["note"]


# --------------------------------------------------------------------------
# 3. first slews: only after the model
# --------------------------------------------------------------------------

def test_model_cutoff_is_the_earlier_of_entry_and_model_night_end():
    cfg = _cfg()
    assert ta.model_cutoff_utc(LIVE_MANUAL_RECORD, cfg) == datetime(2026, 10, 5, 12, 4)
    # re-entered a week later: the end of the model night (local noon, MDT)
    rec = dict(LIVE_MANUAL_RECORD, entered_at="2026-10-12T15:00:00Z")
    assert ta.model_cutoff_utc(rec, cfg) == datetime(2026, 10, 5, 18, 0)
    assert ta.model_cutoff_utc(dict(rec, entered_at=None), cfg) == datetime(2026, 10, 5, 18, 0)
    assert ta.model_cutoff_utc({"entered_at": "2026-10-05T12:04:00Z"}, cfg) is None
    assert ta.model_cutoff_utc(None, cfg) is None


def _run(t_utc: datetime, sep: float, side: str) -> dict:
    return {"t_local": (t_utc - timedelta(hours=6)).isoformat(),
            "t_utc": store.iso_z(t_utc), "target_ra": 10.0, "target_dec": 20.0,
            "first_sep_arcmin": sep, "first_east_arcmin": sep * 0.6,
            "first_north_arcmin": sep * 0.8, "attempts": 2, "final_sep_arcmin": 0.5,
            "threshold_arcmin": 1.0, "converged": True, "ha_h": -1.0 if side == "E" else 1.0,
            "side": side, "side_src": "ha", "dec_band": "Dec 0-30", "ha_band": "x"}


def _write_runs(cfg, runs):
    by_night: dict = {}
    for r in runs:
        n = store.night_of(cfg, store.parse_z(r["t_utc"]))
        by_night.setdefault(n, []).append(r)
    d = ncl.pointing_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    for n, rs in by_night.items():
        ncl.night_file(cfg, n).write_text("".join(json.dumps(r) + "\n" for r in rs),
                                          encoding="utf-8")


def _live_pre_model_runs(model_t: datetime) -> list[dict]:
    # 51.9' E (n=3) and 4.6' W (n=13), all before the model
    east = [_run(model_t - timedelta(days=3 + i, hours=4), 51.9, "E") for i in range(3)]
    west = [_run(model_t - timedelta(days=1 + i % 6, hours=2 + i % 3), 4.6, "W")
            for i in range(13)]
    return east + west


def test_summary_since_drops_older_runs(tmp_path):
    cfg = _cfg(tmp_path)
    cut = datetime(2026, 10, 5, 12, 4)
    _write_runs(cfg, _live_pre_model_runs(cut))
    s = ncl.summary(cfg, 14, end_night="2026-10-05", parse_missing=False)
    assert s["runs"] == 16
    assert s["by_side"]["E"]["median_arcmin"] == 51.9 and s["by_side"]["W"]["n"] == 13
    s = ncl.summary(cfg, 14, end_night="2026-10-05", parse_missing=False, since_utc=cut)
    assert s["runs"] == 0 and s["before_since"] == 16
    assert s["since_utc"] == "2026-10-05T12:04:00Z"
    # the stored files still hold every run
    assert ncl.summary(cfg, 14, end_night="2026-10-05", parse_missing=False)["runs"] == 16


def _audit_with_record(tmp_path, runs_after=()):
    cfg = _cfg(tmp_path)
    now = datetime.utcnow()
    rec = dict(LIVE_MANUAL_RECORD, model_date=(now - timedelta(days=1)).strftime("%Y-%m-%d"),
               entered_at=store.iso_z(now - timedelta(hours=1)))
    p = ta.manual_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rec), encoding="utf-8")
    cut = ta.model_cutoff_utc(rec, cfg)
    _write_runs(cfg, _live_pre_model_runs(cut) + list(runs_after))
    a = ta.run_audit(cfg, persist=False)
    return {r["id"]: r for r in a["rows"]}, a, cut


def test_pre_model_slews_are_pending_not_fail(tmp_path):
    rows, a, _cut = _audit_with_record(tmp_path)
    r = rows["first_slew_error"]
    assert r["status"] == "unknown", r
    assert r["current"] == "no slews since the model (n=0)"
    assert "16 older run(s) ignored" in r["note"]
    rb = rows["tpoint_rebuild"]
    assert rb["status"] == "unknown" and rb["current"] == "pending", rb
    assert "tpoint_rebuild" not in a["fail_ids"]
    assert "first_slew_error" not in a["fail_ids"]
    assert rows["first_slew_pattern"]["status"] == "unknown"


def test_slews_after_the_model_count(tmp_path):
    later = datetime.utcnow() - timedelta(minutes=30)
    rows, a, _cut = _audit_with_record(tmp_path, [_run(later, 1.2, "W")])
    r = rows["first_slew_error"]
    assert r["status"] == "pass" and r["current"].startswith("1.2' W (n=1) since the model")
    assert rows["tpoint_rebuild"]["status"] == "pass"
    rows, _a, _cut = _audit_with_record(tmp_path, [_run(later, 7.0, "E")])
    assert rows["first_slew_error"]["status"] == "fail"
    assert rows["tpoint_rebuild"]["status"] == "fail"


# --------------------------------------------------------------------------
# 4 to 6. binning, All Sky, polar alignment (the live manual record)
# --------------------------------------------------------------------------

def _manual_rows(rec, extra=None, cfg=None):
    cfg = cfg or _cfg()
    rec = dict(rec, entered_at=store.iso_z(datetime.utcnow()))
    obs, _src = ta.manual_observed(rec, cfg)
    return _eval({"manual": obs, "manual_record": rec, **(extra or {})}, cfg)[0]


def test_live_binning_1_and_its_image_scale_pass():
    rows = _manual_rows(LIVE_MANUAL_RECORD, {"astap": {"true_scale": 0.236},
                                             "thesky-script": {"ails_image_scale": 0.236}})
    assert rows["run_binning"]["status"] == "pass"
    r = rows["ails_image_scale"]
    assert r["status"] == "pass" and "x bin 1, manual record" in r["desired"]
    rows = _manual_rows(dict(LIVE_MANUAL_RECORD, run_binning=2))
    assert rows["run_binning"]["status"] == "pass"
    rows = _manual_rows(dict(LIVE_MANUAL_RECORD, run_binning=4))
    assert rows["run_binning"]["status"] == "warn"


def test_live_allsky_off_passes_and_on_without_db_warns():
    rows = _manual_rows(LIVE_MANUAL_RECORD)
    r = rows["allsky_automated"]
    assert r["status"] == "pass" and "manual record" in r["note"], r
    assert rows["allsky_db_installed"]["status"] == "info"
    # on, no database: the one real problem
    rows = _manual_rows(dict(LIVE_MANUAL_RECORD, allsky_automated=True))
    assert rows["allsky_automated"]["status"] == "warn"
    assert "not installed" in rows["allsky_automated"]["note"]
    assert rows["allsky_db_installed"]["status"] == "warn"
    rows = _manual_rows(dict(LIVE_MANUAL_RECORD, allsky_automated=True, allsky_db_installed=True))
    assert rows["allsky_automated"]["status"] == "pass"
    assert rows["allsky_db_installed"]["status"] == "pass"


def test_allsky_off_read_from_the_script_needs_a_catalog_solve():
    rec = {k: v for k, v in LIVE_MANUAL_RECORD.items()
           if k not in ("allsky_automated", "ucac4_installed", "gaia_installed")}
    script = {"thesky-script": {"allsky_automated": False}}
    assert _manual_rows(rec, script)["allsky_automated"]["status"] == "warn"
    ok = _manual_rows(rec, {**script, "imagelink": {"thesky": {"succeeded": True}}})
    assert ok["allsky_automated"]["status"] == "pass"
    assert "self-test" in ok["allsky_automated"]["note"]
    ok = _manual_rows(dict(rec, ucac4_installed=True), script)
    assert ok["allsky_automated"]["status"] == "pass" and "UCAC4" in ok["allsky_automated"]["note"]


def test_live_polar_error_passes_at_the_3_arcmin_limit():
    rows = _manual_rows(LIVE_MANUAL_RECORD)
    r = rows["tpoint_polar_error_arcmin"]
    assert r["current"] == "2.79" and r["status"] == "pass", r
    assert "3" in r["desired"]
    rows = _manual_rows(LIVE_MANUAL_RECORD, cfg=_cfg(tpoint_polar_max_arcmin=2.0))
    assert rows["tpoint_polar_error_arcmin"]["status"] == "warn"


def test_live_audit_has_none_of_the_false_rows(monkeypatch):
    """The six rows of the 2026-10-05T12:50Z audit, together."""
    monkeypatch.setattr(ta, "_utcnow", lambda: AUDIT_T)
    rec = dict(LIVE_MANUAL_RECORD, entered_at=store.iso_z(datetime.utcnow()))
    obs, _src = ta.manual_observed(rec, _cfg())
    pl = {"by_side": {"E": {"n": 0}, "W": {"n": 0}}, "runs": 0, "before_since": 16,
          "since_utc": "2026-10-05T12:04:00Z", "model_date": "2026-10-04",
          "note": "no slews since the model (n=0)"}
    rows, res = _eval({"thesky-script": dict(_live_site(), ails_image_scale=0.236),
                       "astap": {"true_scale": 0.236}, "manual": obs, "manual_record": rec,
                       "pointing-log": pl})
    for rid in ("site_longitude", "time_zone_dst", "run_binning", "allsky_automated",
                "tpoint_polar_error_arcmin", "ails_image_scale"):
        assert rows[rid]["status"] == "pass", rows[rid]
    for rid in ("tpoint_rebuild", "first_slew_error"):
        assert rows[rid]["status"] == "unknown", rows[rid]
    assert res["counts"]["fail"] == 0


def test_sources_ascii_without_em_dashes():
    root = Path(ta.__file__).resolve().parents[2]
    for p in (Path(ta.__file__), Path(ncl.__file__), Path(tc.__file__),
              root / "config" / "thesky" / "desired_rc16.toml", Path(__file__)):
        t = p.read_text(encoding="utf-8")
        assert t.isascii(), p
