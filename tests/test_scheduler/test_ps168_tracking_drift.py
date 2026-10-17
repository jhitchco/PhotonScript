"""PS-168: nightly tracking drift (unguided RA / Dec drift and wobble) from
the PHD2 guide log. Fixtures: excerpts of the 2026-10-06 and 2026-10-07
guide logs (PHD2 measured the star but sent 0 pulses, PS-167), plus a
synthetic guided log for the reconstruction (raw + applied correction)."""
import json
import math
import shutil
from pathlib import Path

import pytest

from photonscript.scheduler import morning_report as mr
from photonscript.scheduler import phd2_logs as pl
from photonscript.scheduler import runs
from photonscript.scheduler import tracking_drift as td
from photonscript.shared.config import PhotonScriptConfig

FIX = Path(__file__).parent / "fixtures" / "phd2"
N07 = "PHD2_GuideLog_2026-10-07_201941.txt"
N06 = "PHD2_GuideLog_2026-10-06_203731.txt"


@pytest.fixture(autouse=True)
def _clear_cache():
    td._CACHE.clear()
    td._darks_cache.clear()
    yield
    td._CACHE.clear()


def _cfg(tmp_path=None, logs=(), **kw):
    if tmp_path is not None:
        d = tmp_path / "phd2logs"
        d.mkdir(exist_ok=True)
        for n in logs:
            shutil.copy(FIX / n, d / n)
        kw.setdefault("data_dir", tmp_path / "data")
        kw.setdefault("phd2_logs_dir", str(d))
    kw.setdefault("piggyback_enabled", True)
    return PhotonScriptConfig(_env_file=None, **kw)


def _rows(name, cfg=None):
    secs = pl.parse_guide_log((FIX / name).read_text(encoding="utf-8"), name)
    return [r for s in secs for r in td.analyze_session(s, cfg or _cfg())]


# ---- one session ------------------------------------------------------------------

def test_1007_clean_session_is_unguided_raw_drift():
    rows = _rows(N07)
    r = next(x for x in rows if x["session"].endswith("01:03:36"))
    assert r["mode"] == td.RAW and r["pulses_ra"] == 0 and r["pulses_dec"] == 0
    assert r["excluded"] is None and r["frames"] == 194
    # header 0.25"/px; 206.265 x 2.0 um x bin 2 / 3248 mm = 0.254
    assert r["guide_scale_arcsec"] == pytest.approx(0.254, abs=0.001)
    assert r["ra_arcsec_min"] == pytest.approx(0.60, abs=0.02)
    assert r["dec_arcsec_min"] == pytest.approx(-0.48, abs=0.02)
    assert 0.5 < r["rms_arcsec"] < 1.5


def test_lost_star_and_short_sessions_are_excluded_with_a_reason():
    rows = _rows(N07)
    lost = next(x for x in rows if x["session"].endswith("04:30:44"))
    assert lost["excluded"].startswith("wobble") and "dropped frames" in lost["excluded"]
    assert lost["rms_arcsec"] > 5
    short = next(x for x in rows if x["session"].endswith("00:58:47"))
    assert short["excluded"].startswith("short")


def test_long_session_is_fit_in_windows():
    """The 10-06 excerpt is one 30 min session: two 15 min windows."""
    rows = _rows(N06)
    assert [r["window"] for r in rows] == [1, 2] and rows[0]["windows"] == 2
    assert all(r["excluded"] is None for r in rows)
    assert rows[0]["dec_arcsec_min"] == pytest.approx(0.615, abs=0.02)
    one = _rows(N06, _cfg(tracking_drift_window_min=60))
    assert len(one) == 1


def test_window_threshold_from_config():
    rows = _rows(N07, _cfg(tracking_drift_max_rms_arcsec=0.5))
    r = next(x for x in rows if x["session"].endswith("01:03:36"))
    assert r["excluded"].startswith("wobble")


# ---- reconstruction (pulses sent) -------------------------------------------------

_HDR = """Guiding Begins at 2026-10-01 21:00:00
Pixel scale = 0.25 arc-sec/px, Binning = 2, Focal length = 3248 mm
Mount = Test mount, connected, guiding enabled, xAngle = 0.0, xRate = 20.0, yAngle = 90.0, yRate = 30.0, parity = +/+
RA Guide Speed = 7.5 a-s/s, Dec Guide Speed = 7.5 a-s/s, Cal Dec = 0.0
RA = 5.00 hr, Dec = 60.0 deg, Hour angle = 1.00 hr, Pier side = East, Rotator pos = N/A, Alt = 60.0 deg, Az = 120.0 deg
Frame,Time,mount,dx,dy,RARawDistance,DECRawDistance,RAGuideDistance,DECGuideDistance,RADuration,RADirection,DECDuration,DECDirection,XStep,YStep,StarMass,SNR,ErrorCode
"""


def _guided_log(ra_px_s, dec_px_s, n=200, dt=6.0, gain=0.7):
    """A guided session: the star drifts at ra/dec px/s; each frame PHD2
    sends a pulse (W / S for a positive error) of gain x the error."""
    scale = 0.25
    ra_rate = 7.5 * math.cos(math.radians(60.0)) / scale   # px/s of pulse
    dec_rate = 7.5 / scale
    corr_ra = corr_dec = 0.0
    lines = []
    for k in range(1, n + 1):
        t = k * dt
        ra = ra_px_s * t - corr_ra
        dec = dec_px_s * t - corr_dec
        ra_ms = int(abs(ra) * gain / ra_rate * 1000)
        dec_ms = int(abs(dec) * gain / dec_rate * 1000)
        ra_d = ("W" if ra > 0 else "E") if ra_ms else ""
        dec_d = ("S" if dec > 0 else "N") if dec_ms else ""
        lines.append(f"{k},{t:.3f},\"Mount\",0,0,{ra:.3f},{dec:.3f},0,0,"
                     f"{ra_ms},{ra_d},{dec_ms},{dec_d},,,100000,50.0,0")
        corr_ra += math.copysign(ra_ms / 1000.0 * ra_rate, ra) if ra_ms else 0.0
        corr_dec += math.copysign(dec_ms / 1000.0 * dec_rate, dec) if dec_ms else 0.0
    return _HDR + "\n".join(lines) + "\nGuiding Ends at 2026-10-01 21:20:00\n"


def test_reconstruction_recovers_the_drift_under_guiding():
    cfg = _cfg(tracking_drift_window_min=60)
    ra_px_s, dec_px_s = 2.0 / 60, -1.2 / 60          # px/min -> px/s
    secs = pl.parse_guide_log(_guided_log(ra_px_s, dec_px_s), "synthetic.txt")
    r = td.analyze_session(secs[0], cfg)[0]
    assert r["mode"] == td.RECON and r["pulses_ra"] > 100
    assert r["guide_scale_arcsec"] == pytest.approx(0.25)
    assert "cos(Dec)" in r["rate_source"]
    assert r["ra_arcsec_min"] == pytest.approx(2.0 * 0.25, abs=0.02)
    assert r["dec_arcsec_min"] == pytest.approx(-1.2 * 0.25, abs=0.02)
    # the raw (post-correction) error alone would read almost no drift
    raw = [f for f in secs[0]["frames"]]
    sl, _ = td._fit(raw, [f["ra"] for f in raw])
    assert abs(sl * 60 * 0.25) < 0.05
    assert "zero_pulse_ra_arcsec_min" in r


def test_reconstruction_without_a_rate_is_excluded():
    text = _guided_log(0.03, 0.0).replace(
        "RA Guide Speed = 7.5 a-s/s, Dec Guide Speed = 7.5 a-s/s, ", "").replace(
        "xRate = 20.0, ", "").replace("yRate = 30.0, ", "")
    secs = pl.parse_guide_log(text, "s.txt")
    r = td.analyze_session(secs[0], _cfg())[0]
    assert r["excluded"].startswith("pulses sent but no guide speed")


# ---- night summary, smear, line ---------------------------------------------------

def test_summary_smear_and_line():
    sm = td.summarize(_rows(N07), _cfg())
    assert sm["sessions_used"] == 1 and len(sm["sessions_excluded"]) == 3
    assert sm["mode"] == td.RAW
    s300 = next(x for x in sm["smear"] if x["exp_s"] == 300)
    assert s300["arcsec"] == pytest.approx(sm["total_arcsec_min"] * 5, abs=0.05)
    assert s300["px"]["rc16"] == pytest.approx(s300["arcsec"] / 0.236, abs=0.1)
    assert s300["px"]["piggyback"] == pytest.approx(s300["arcsec"] / 1.29, abs=0.1)
    line = td.report_line(sm)
    assert line.startswith('Tracking: RA 0.60"/min, Dec 0.48"/min, 300 s smear 3.9" (16 px RC16)')
    assert line.isascii()


def test_user_example_line_shape():
    sm = {"ra_arcsec_min": 0.41, "dec_arcsec_min": 0.31, "total_arcsec_min": 0.514,
          "rms_arcsec": 1.4, "mode": td.RAW,
          "smear": td.smear_table(0.514, {"rc16": 0.236})}
    assert td.report_line(sm) == ('Tracking: RA 0.41"/min, Dec 0.31"/min, 300 s smear '
                                  '2.6" (11 px RC16), wobble 1.4" RMS')


def test_no_clean_session_line():
    assert td.report_line({"sessions_excluded": [{}, {}]}).endswith("(2 excluded)")
    assert td.report_line({"sessions_excluded": []}) is None


def test_night_drift_keeps_only_the_nights_sessions(tmp_path):
    cfg = _cfg(tmp_path, logs=(N06, N07))
    n07 = td.night_drift(cfg, "2026-10-07")
    assert n07["ok"] and {s["session"][:10] for s in n07["sessions"]} == {"2026-10-08"}
    n06 = td.night_drift(cfg, "2026-10-06")
    assert {s["session"][:10] for s in n06["sessions"]} == {"2026-10-06"}
    assert td.night_drift(cfg, "2026-09-01")["ok"] is False


def test_trend_keyed_by_the_tpoint_model(tmp_path):
    cfg = _cfg(tmp_path, logs=(N06, N07))
    from photonscript.scheduler import thesky_audit as ta
    from photonscript.shared import phd2_store as store
    store.append_jsonl(ta.manual_path(cfg).with_name("manual_history.jsonl"),
                       {"model_date": "2026-10-04", "points": 250,
                        "entered_at": "2026-10-06T06:37:05Z"})
    store.write_json(ta.night_path(cfg, "2026-10-07"),
                     {"night": "2026-10-07", "manual": {"model_date": "2026-10-07",
                                                        "points": 180},
                      "rows": [{"id": "protrack_on", "current": "OFF (greyed)"}]})
    tr = td.trend(cfg, "2026-10-07", 3)
    assert [r["date"] for r in tr["rows"]] == ["2026-10-06", "2026-10-07"]
    assert tr["rows"][0]["model_date"] == "2026-10-04" and tr["rows"][0]["model_points"] == 250
    assert tr["rows"][1]["model_points"] == 180 and tr["rows"][1]["protrack"] == "OFF (greyed)"
    assert [m["model_date"] for m in tr["by_model"]] == ["2026-10-04", "2026-10-07"]


def test_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app as app_mod
    cfg = _cfg(tmp_path, logs=(N07,))
    monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
    d = TestClient(app_mod.app).get("/api/tracking/drift?date=2026-10-07&nights=2").json()
    assert d["ok"] and d["date"] == "2026-10-07" and d["line"].startswith("Tracking: RA")
    assert d["scales"] == {"rc16": 0.236, "piggyback": 1.29}
    assert d["limits"]["max_rms_arcsec"] == 5.0 and "trend" in d and "recommended" in d
    assert TestClient(app_mod.app).get("/api/tracking/drift?date=bad").json()["ok"] is False
    html = (Path(app_mod.__file__).parent / "templates" / "dashboard.html").read_text(
        encoding="utf-8")
    assert "driftCard" in html and "/api/tracking/drift" in html


def test_morning_report_carries_the_tracking_line(tmp_path, monkeypatch):
    from photonscript.scheduler import calibration_owed
    cfg = _cfg(tmp_path, logs=(N07,))
    monkeypatch.setattr(calibration_owed, "morning_note", lambda c: None)
    p = runs.runs_dir(cfg) / "2026-10-07_subs.jsonl"
    p.write_text(json.dumps({"rig": "rc16", "file": "a.fits", "target": "M31",
                             "filter": "L", "exp_s": 300, "passed_qa": False,
                             "drivers": ["ecc"]}) + "\n", encoding="utf-8")
    card = mr.report_card(cfg, "2026-10-07", projects=[])
    assert card["tracking"].startswith('Tracking: RA 0.60"/min')
    assert card["tracking"] in card["lines"]


def test_config_fields_exposed():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    for env in ("PS_TRACKING_DRIFT_WINDOW_MIN", "PS_TRACKING_DRIFT_MIN_SESSION_MIN",
                "PS_TRACKING_DRIFT_MAX_RMS_ARCSEC", "PS_TRACKING_DRIFT_TREND_NIGHTS"):
        assert hasattr(PhotonScriptConfig(), by_env[env][0])
    c = PhotonScriptConfig(_env_file=None)
    assert (c.tracking_drift_window_min, c.tracking_drift_max_rms_arcsec,
            c.tracking_drift_min_session_min) == (15.0, 5.0, 5.0)
