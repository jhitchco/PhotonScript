"""PS-120 follow-up: the 2026-10-11 arm audit (16:37 local) still failed
time_zone_dst and first_slew_error / tpoint_rebuild. The fixtures are the
values that audit showed (GET /api/thesky/audit, t_utc 2026-10-11T22:37:48Z):

- TheSky (reinstalled 2026-10-08) read tz +0, DST not observed (index 0),
  while the same script read its Julian date 0.0 s from the PC's UTC and an
  LST that put the site at 109.02 W. TheSky's UT is right, so the zone only
  skews its local-time display: WARN with the fix, not a pointing FAIL.
- The first-slew cutoff came from the manual record (model 2026-10-04), so
  slews from 10-05 to 10-09 (old model, and a 751' slew during the
  reinstall) were judged against the 356-point model TPoint wrote at
  2026-10-10T13:10:24Z. The newer TPoint file now moves the cutoff: only the
  two 10-10 slews (3.2') count, which is a WARN, not a FAIL."""
import json
from datetime import datetime, timedelta

from photonscript.scheduler import nina_center_log as ncl
from photonscript.scheduler import thesky_audit as ta
from photonscript.scheduler import tpoint_files
from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig

AUDIT_T = datetime(2026, 10, 11, 22, 37, 48)
AARO_E = -109.021111
MODEL_MTIME = "2026-10-10T13:10:24Z"
RECORD = {"model_date": "2026-10-04", "entered_at": "2026-10-06T06:37:05Z",
          "points": 250, "rms_arcsec": 15.77, "polar_az_arcmin": -2.6,
          "polar_alt_arcmin": -1.0, "model_active": True, "protrack_on": True,
          "run_binning": 1, "allsky_automated": False, "rigs": ["rc16", "piggyback"]}


def _cfg(tmp_path=None, **kw):
    if tmp_path is not None:
        kw.setdefault("data_dir", tmp_path / "data")
        kw.setdefault("nina_logs_dir", str(tmp_path / "ninalogs"))
    kw.setdefault("thesky_tcp_host", "127.0.0.1")
    kw.setdefault("thesky_tcp_port", 1)
    return PhotonScriptConfig(_env_file=None, **kw)


def _jd(t: datetime) -> float:
    return (t - datetime(1970, 1, 1)).total_seconds() / 86400.0 + 2440587.5


def _site(lon_east=AARO_E, **over):
    jd = _jd(AUDIT_T)
    d = {"site_latitude": 31.9069, "site_longitude": 109.021, "site_elevation": 1300,
         "use_computer_clock": True, "site_clock": -0.0, "time_zone": 0.0,
         "dst_index": 0.0, "time_zone_dst": "tz 0, DST index 0", "site_jd": jd,
         "site_lst_h": ((ta.gmst_deg(jd) + lon_east) % 360.0) / 15.0}
    d.update(over)
    return d


def _rows(observed, cfg=None):
    cfg = cfg or _cfg()
    obs = {s: {} for s in ta.SOURCES}
    obs.update(observed)
    return {r["id"]: r for r in ta.evaluate(ta.load_desired(cfg), obs, cfg)["rows"]}


# ------------------------------------------------------------- time zone

def test_live_zone_0_with_a_right_clock_and_lst_warns(monkeypatch):
    monkeypatch.setattr(ta, "_utcnow", lambda: AUDIT_T)
    r = _rows({"thesky-script": _site()})["time_zone_dst"]
    assert r["status"] == "warn", r
    assert r["current"] == "tz +0, DST not observed (index 0)"
    assert "UT is right" in r["note"] and "109.02 W" in r["note"]
    assert "0.0 s from the PC's UTC" in r["note"]


def test_zone_off_still_fails_without_proof_of_ut(monkeypatch):
    monkeypatch.setattr(ta, "_utcnow", lambda: AUDIT_T)
    s = _site()
    s.pop("site_clock")                                   # clock not read
    assert _rows({"thesky-script": s})["time_zone_dst"]["status"] == "fail"
    # TheSky's UT off by the zone error: the zone does drive its UT here
    r = _rows({"thesky-script": _site(site_clock=-21600.0)})["time_zone_dst"]
    assert r["status"] == "fail" and "local time is off" in r["note"]
    # LST puts the site elsewhere
    assert _rows({"thesky-script": _site(lon_east=-19.0)})["time_zone_dst"]["status"] == "fail"


def test_zone_off_with_clock_but_no_lst_warns(monkeypatch):
    monkeypatch.setattr(ta, "_utcnow", lambda: AUDIT_T)
    s = _site()
    s.pop("site_lst_h")
    r = _rows({"thesky-script": s})["time_zone_dst"]
    assert r["status"] == "warn" and "Julian date" in r["note"]


def test_right_zone_still_passes(monkeypatch):
    monkeypatch.setattr(ta, "_utcnow", lambda: AUDIT_T)
    r = _rows({"thesky-script": _site(time_zone=-7.0, dst_index=17.0)})["time_zone_dst"]
    assert r["status"] == "pass", r


# ------------------------------------------------------------- first slews

def test_cutoff_takes_the_newer_tpoint_file():
    cfg = _cfg()
    assert ta.model_cutoff_utc(RECORD, cfg) == datetime(2026, 10, 5, 18, 0)
    assert ta.model_cutoff_utc(RECORD, cfg, MODEL_MTIME) == datetime(2026, 10, 10, 13, 10, 24)
    # an older model file never moves the cutoff back
    assert ta.model_cutoff_utc(RECORD, cfg, "2026-10-03T10:00:00Z") == datetime(2026, 10, 5, 18, 0)
    assert ta.model_cutoff_utc(None, cfg, MODEL_MTIME) == datetime(2026, 10, 10, 13, 10, 24)
    assert ta.model_cutoff_utc(None, cfg, "") is None


def test_audit_observed_carries_the_active_model_mtime(monkeypatch, tmp_path):
    st = {"ok": True, "points": 356, "sky_rms_arcsec": 19.94, "model_date": "2026-10-10",
          "active_model_mtime": MODEL_MTIME, "polar": {"total_arcmin": 2.67}}
    monkeypatch.setattr(tpoint_files, "stats", lambda cfg: st)
    monkeypatch.setattr(tpoint_files, "save", lambda cfg, s: None)
    o, src = tpoint_files.audit_observed(_cfg(tmp_path))
    assert o["tpoint_model_mtime"] == MODEL_MTIME and src["ok"]


def _run(t_utc, sep, side):
    return {"t_local": (t_utc - timedelta(hours=6)).isoformat(), "t_utc": store.iso_z(t_utc),
            "target_ra": 10.0, "target_dec": 40.0, "first_sep_arcmin": sep,
            "first_east_arcmin": sep * 0.97, "first_north_arcmin": 0.0, "attempts": 2,
            "final_sep_arcmin": 0.5, "threshold_arcmin": 1.0, "converged": True,
            "ha_h": -1.0 if side == "E" else 1.0, "side": side, "side_src": "ha",
            "dec_band": "Dec 30 to 60", "ha_band": "x"}


def _live_runs():
    """The 14 runs the 10-11 audit counted: nights 10-05 (6), 10-06 (3),
    10-07 (2), 10-09 (1, 751' during the reinstall), 10-10 (2, 3.2')."""
    runs = [_run(datetime(2026, 10, 6, 3 + i, 0), 6.0 if i % 2 else 3.6,
                 "W" if i % 2 else "E") for i in range(6)]
    runs += [_run(datetime(2026, 10, 7, 4 + i, 0), 5.9, "W") for i in range(3)]
    runs += [_run(datetime(2026, 10, 8, 4 + i, 0), 6.4, "E") for i in range(2)]
    runs += [_run(datetime(2026, 10, 10, 5, 0), 751.5, "E")]
    runs += [_run(datetime(2026, 10, 11, 3, 0), 3.13, "E"),
             _run(datetime(2026, 10, 11, 4, 0), 3.33, "E")]
    return runs


def _write_runs(cfg, runs):
    by_night: dict = {}
    for r in runs:
        by_night.setdefault(store.night_of(cfg, store.parse_z(r["t_utc"])), []).append(r)
    ncl.pointing_dir(cfg).mkdir(parents=True, exist_ok=True)
    for n, rs in by_night.items():
        ncl.night_file(cfg, n).write_text("".join(json.dumps(r) + "\n" for r in rs),
                                          encoding="utf-8")


def test_live_first_slews_judge_only_the_new_model(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _write_runs(cfg, _live_runs())
    orig = ncl.summary
    monkeypatch.setattr(ncl, "summary", lambda c, n, *a, **k: orig(
        c, n, end_night="2026-10-11", parse_missing=False, since_utc=k.get("since_utc")))
    files = {"tpoint_model_date": "2026-10-10", "tpoint_model_mtime": MODEL_MTIME,
             "tpoint_points": 356}
    # before: the record's cutoff counts the old model's slews too
    assert ta.pointing_observed(cfg, RECORD)[0]["runs"] == 14
    pl, src = ta.pointing_observed(cfg, RECORD, files)
    assert pl["runs"] == 2 and pl["before_since"] == 12, pl
    assert pl["model_date"] == "2026-10-10"
    assert "TPoint files" in src["note"] and "12 older run(s) ignored" in src["note"]
    monkeypatch.setattr(ta, "_today", lambda c: datetime(2026, 10, 11).date())
    man = ta.manual_observed(dict(RECORD, entered_at=store.iso_z(datetime.utcnow())), cfg)[0]
    rows = _rows({"pointing-log": pl, "manual_record": RECORD, "tpoint-files": files,
                  "manual": man}, cfg)
    r = rows["first_slew_error"]
    assert r["status"] == "warn", r
    assert r["current"].startswith("3.2' E (n=2) since the model (2026-10-10)"), r
    rb = rows["tpoint_rebuild"]
    assert rb["status"] == "warn" and rb["current"] == "watch", rb
    # the model age follows the newer TPoint file, not the record
    assert rows["tpoint_model_age_days"]["current"].startswith("1 d"), \
        rows["tpoint_model_age_days"]
