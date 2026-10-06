"""PS-97: field rotation measured vs predicted (scheduler.field_rotation),
on synthetic star sidecars, solves and pointing records."""
import json
import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from photonscript.scheduler import field_rotation as fr
from photonscript.scheduler.solve_store import nominal_cd
from photonscript.shared.config import PhotonScriptConfig

ROOT = Path(__file__).resolve().parents[2]
NIGHT = "2026-10-05"
T0 = datetime(2026, 10, 6, 4, 0, 0)
W, H = 6224, 4168


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _sky(seed, n=220, scale=0.24):
    """Fixed star positions (east, north arcsec from the field center)."""
    rng = np.random.default_rng(seed)
    x = rng.uniform(120, W - 120, n) - W / 2.0
    y = rng.uniform(120, H - 120, n) - H / 2.0
    cd = np.array(nominal_cd(scale, 20.0)) * 3600.0
    return (cd @ np.vstack([x, y])).T


def _table(sky, scale, pa, shift=(0.0, 0.0)):
    """Star sidecar of the fixed sky seen with the camera at position angle
    pa (deg), plus a pixel shift (dithers / drift)."""
    m = np.array(nominal_cd(scale, pa)) * 3600.0
    px = np.linalg.solve(m, sky.T).T + np.array([W / 2.0 + shift[0], H / 2.0 + shift[1]])
    return {"v": 1, "x": [float(p[0]) for p in px], "y": [float(p[1]) for p in px],
            "w": W, "h": H, "ecc": [0.3] * len(px), "hfr": [2.0] * len(px)}


def _block(rig, rate, minutes=120, exp_s=300, t0=T0, pa0=20.0, dec=38.3,
           ra=None, pier="West", target="Crescent Nebula", seed=0, prefix=None,
           solve_every=0):
    """Records, pointing records, tables and solves for one block whose
    camera PA turns at `rate` deg/h."""
    scale = 0.24 if rig == fr.RC16 else 1.29
    sky = _sky(seed, scale=scale)
    if ra is None:  # on the meridian at t0 (HA about 0)
        from photonscript.shared.pointing import alt_ha
        ra = 0.0
        for _ in range(3):
            _, ha = alt_ha(ra, dec, t0, 31.906944, -109.021367)
            ra = (ra + ha * 15.0) % 360.0
    recs, pts, tabs, sols = [], {}, {}, {}
    t, i = 0.0, 0
    prefix = prefix or rig[:1]
    rng = np.random.default_rng(seed + 7)
    while t + exp_s / 60.0 <= minutes:
        mid = t + exp_s / 120.0
        pa = pa0 + rate * mid / 60.0
        f = f"{prefix}_{i:03d}.fits"
        tabs[f] = _table(sky, scale, pa, shift=tuple(rng.uniform(-6, 6, 2)))
        recs.append({"rig": rig, "file": f, "exp_s": exp_s,
                     "date_obs": (t0 + timedelta(minutes=t)).isoformat(),
                     "target": target})
        pts[(rig, f)] = {"rig": rig, "file": f, "mount_ra": ra, "mount_dec": dec,
                         "pier": pier}
        if solve_every and i % solve_every == 0:
            sols[f] = {"solved": True, "pa": pa % 360.0, "parity": -1, "scale": scale}
        t += exp_s / 60.0
        i += 1
    return recs, pts, tabs, sols


def _night(cfg, *blocks):
    recs, pts, tabs, sols = [], {}, {fr.RC16: {}, fr.PIGGY: {}}, {fr.RC16: {}, fr.PIGGY: {}}
    for rig, (r, p, t, s) in blocks:
        recs += r
        pts.update(p)
        tabs[rig].update(t)
        sols[rig].update(s)
    return fr.analyze_night(cfg, NIGHT, recs, pts, tabs, sols, read_headers=False)


# --------------------------------------------------------------------------
# the model
# --------------------------------------------------------------------------

def test_model_scales_with_polar_error_and_dec():
    k = 15.0411 * math.radians(2.8 / 60.0)
    assert fr.predicted_rate(0.0, 2.8, 0.0, 0.0) == pytest.approx(k)
    assert fr.predicted_rate(2.8, 0.0, 6.0, 0.0) == pytest.approx(k)
    assert fr.predicted_rate(2.8, 0.0, 0.0, 0.0) == pytest.approx(0.0, abs=1e-12)
    assert fr.envelope_rate(2.8, 60.0) == pytest.approx(2 * k)
    # the envelope bounds every hour angle, any MA / ME split
    for ha in np.linspace(-6, 6, 25):
        assert abs(fr.predicted_rate(-2.6, -1.0, ha, 66.6)) <= \
            fr.envelope_rate(math.hypot(2.6, 1.0), 66.6) + 1e-12
    # min_polar_error inverts the envelope
    assert fr.min_polar_error(fr.envelope_rate(9.0, 50.0), 50.0) == pytest.approx(9.0)
    # TPoint 2026-10-04 (2.8') cannot give PS-96's 0.10 deg/h at Cat's Eye
    assert fr.envelope_rate(2.8, 66.6) == pytest.approx(0.031, abs=0.001)
    assert fr.min_polar_error(0.10, 66.6) > 8.5


def test_fit_polar_recovers_ma_me():
    blocks = [{"ha_mid_h": ha, "dec": dec, "rate_deg_h": fr.predicted_rate(-6.0, 4.0, ha, dec)}
              for ha, dec in ((-3, 30), (-1, 60), (1, 45), (2.5, 20), (0, 70))]
    f = fr.fit_polar(blocks)
    assert f["ma_arcmin"] == pytest.approx(-6.0, abs=0.01)
    assert f["me_arcmin"] == pytest.approx(4.0, abs=0.01)
    assert f["total_arcmin"] == pytest.approx(math.hypot(6, 4), abs=0.01)
    assert f["resid_rms_deg_h"] == pytest.approx(0.0, abs=1e-6)
    assert fr.fit_polar(blocks[:2]) is None                      # too few
    assert fr.fit_polar([dict(b, ha_mid_h=0.1 * i) for i, b in enumerate(blocks)]) is None


def test_model_grid_cuts_below_horizon():
    g = fr.model_grid(-2.6, -1.0, 31.907)
    assert g["has_h"] == [-4, -2, 0, 2, 4]
    row0 = next(r for r in g["rows"] if r["dec"] == 0)
    assert all(v is None or v <= row0["envelope"] + 1e-9 for v in row0["rates"])
    assert fr.model_grid(None, None, 31.9) is None


def test_corner_cost_rc16_and_piggy():
    c = fr.corner_cost(0.10, 600, 0.236, W, H)
    assert c["corner_px"] == pytest.approx(1.09, abs=0.02)
    assert c["edge_pivot_px"] == pytest.approx(2 * c["corner_px"], abs=0.02)
    assert c["corner_arcsec"] == pytest.approx(c["corner_px"] * 0.236, abs=0.01)
    n = fr.corner_cost(0.10, 6 * 3600, 1.29, W, H)
    assert n["turn_deg"] == pytest.approx(0.6)
    assert n["corner_px"] == pytest.approx(39.2, abs=0.2)
    assert fr.corner_cost(None, 600, 0.24, W, H) is None


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------

def test_registration_rate_sign_and_value(tmp_path):
    cfg = _cfg(tmp_path)
    m = _night(cfg, (fr.RC16, _block(fr.RC16, 0.08)))
    (b,) = m["blocks"]
    assert b["source"] == "registration" and b["n_registered"] == b["n_subs"] == 24
    assert b["rate_deg_h"] == pytest.approx(0.08, abs=0.003)
    assert b["resid_deg"] < 0.003 and b["err_deg_h"] < 0.002
    assert b["pier"] == "West" and b["dec"] == pytest.approx(38.3)
    assert abs(b["ha_start_h"]) < 0.1 and b["ha_end_h"] == pytest.approx(2.0, abs=0.1)
    m2 = _night(cfg, (fr.RC16, _block(fr.RC16, -0.05, seed=3)))
    assert m2["blocks"][0]["rate_deg_h"] == pytest.approx(-0.05, abs=0.003)


def test_solve_track_used_without_sidecars(tmp_path):
    cfg = _cfg(tmp_path)
    r, p, t, s = _block(fr.RC16, 0.06, solve_every=5, pa0=359.9)  # wraps 360
    m = fr.analyze_night(cfg, NIGHT, r, p, {fr.RC16: {}}, {fr.RC16: s}, read_headers=False)
    b = m["blocks"][0]
    assert b["source"] == "solve" and b["n_solved"] == 5
    assert b["rate_deg_h"] == pytest.approx(0.06, abs=1e-4)
    assert b["scale_arcsec"] == pytest.approx(0.24)
    # with sidecars too, registration wins and both agree
    m = fr.analyze_night(cfg, NIGHT, r, p, {fr.RC16: t}, {fr.RC16: s}, read_headers=False)
    b = m["blocks"][0]
    assert b["source"] == "registration"
    assert b["rate_reg_deg_h"] == pytest.approx(b["rate_solve_deg_h"], abs=0.003)


def test_blocks_split_by_pier_and_target_and_piggy_borrows_pointing(tmp_path):
    cfg = _cfg(tmp_path)
    east = _block(fr.RC16, 0.05, minutes=60, pier="East", prefix="e")
    west = _block(fr.RC16, 0.05, minutes=60, pier="West", prefix="w",
                  t0=T0 + timedelta(minutes=62), seed=1)
    other = _block(fr.RC16, 0.05, minutes=60, target="Heart Nebula", prefix="h",
                   t0=T0 + timedelta(minutes=124), dec=61.5, seed=2)
    pg = _block(fr.PIGGY, 0.05, minutes=60, exp_s=120, pier=None, seed=4)
    pg = (pg[0], {}, pg[2], pg[3])                       # no Piggy position
    m = _night(cfg, (fr.RC16, east), (fr.RC16, west), (fr.RC16, other), (fr.PIGGY, pg))
    rc = [b for b in m["blocks"] if b["rig"] == fr.RC16]
    assert [(b["pier"], b["target"]) for b in rc] == [
        ("East", "Crescent Nebula"), ("West", "Crescent Nebula"), ("West", "Heart Nebula")]
    (p,) = [b for b in m["blocks"] if b["rig"] == fr.PIGGY]
    assert p["pier"] == "East" and p["dec"] == pytest.approx(38.3)
    assert p["rate_deg_h"] == pytest.approx(0.05, abs=0.005)


def test_short_block_not_rated(tmp_path):
    m = _night(_cfg(tmp_path), (fr.RC16, _block(fr.RC16, 0.05, minutes=15)))
    assert m["blocks"][0]["rate_deg_h"] is None and m["blocks"][0]["source"] is None


# --------------------------------------------------------------------------
# the report and its verdict
# --------------------------------------------------------------------------

def _b(night, rig, rate, dec=40.0, ha=0.5, start="04:00", pier="West", target="M 2"):
    return {"night": night, "rig": rig, "target": target, "pier": pier,
            "start_utc": f"{night}T{start}:00Z", "end_utc": f"{night}T23:00:00Z",
            "dec": dec, "ha_mid_h": ha, "ha_start_h": ha - 1, "ha_end_h": ha + 1,
            "rate_deg_h": rate, "err_deg_h": 0.001, "resid_deg": 0.001,
            "n_subs": 10, "n_registered": 10, "n_solved": 0, "exp_s": 600 if rig == fr.RC16 else 120,
            "source": "registration"}


def _rep(tmp_path, nights_blocks, **kw):
    measured = {n: {"blocks": bs} for n, bs in nights_blocks.items()}
    end = max(nights_blocks)
    kw.setdefault("ma", -2.6)
    kw.setdefault("me", -1.0)
    return fr.report(_cfg(tmp_path), nights=14, end_night=end, measured=measured, **kw)


def test_verdict_matches_polar_error(tmp_path):
    rep = _rep(tmp_path, {"2026-10-05": [_b("2026-10-05", fr.RC16, 0.015),
                                         _b("2026-10-05", fr.PIGGY, 0.016)]})
    assert rep["verdict"]["level"] == "ok"
    assert "matches 2.79' polar error" in rep["verdict"]["text"]
    assert "no action" in rep["verdict"]["text"]
    assert rep["eras"]["all"]["rig_agreement"]["agree"] is True
    assert rep["polar"]["source"] == "query"


def test_verdict_exceeds_both_rigs_common(tmp_path):
    rep = _rep(tmp_path, {"2026-09-25": [_b("2026-09-25", fr.RC16, 0.100, dec=66.6),
                                         _b("2026-09-25", fr.PIGGY, 0.093, dec=66.6)]})
    v = rep["verdict"]
    assert v["level"] == "warn" and "exceeds the polar prediction" in v["text"]
    assert "check camera / flexure" in v["text"] and "common to the mount" in v["text"]
    s = rep["eras"]["all"]
    assert s["median_min_polar_arcmin"] > 8.0
    assert s["by_rig"]["rc16"]["n"] == 1 and s["by_rig"]["piggyback"]["n"] == 1
    assert s["by_pier"]["West"]["n"] == 2 and s["by_dec"]["Dec > 60"]["n"] == 2


def test_verdict_rigs_disagree_points_at_camera(tmp_path):
    rep = _rep(tmp_path, {"2026-10-05": [_b("2026-10-05", fr.RC16, 0.012),
                                         _b("2026-10-05", fr.PIGGY, 0.090)]})
    v = rep["verdict"]
    assert v["level"] == "warn" and "rigs disagree" in v["text"]
    assert "Piggy-600 camera" in v["text"]


def test_verdict_unknown_without_polar_or_data(tmp_path):
    rep = fr.report(_cfg(tmp_path), nights=3, end_night="2026-10-05",
                    measured={"2026-10-05": {"blocks": [_b("2026-10-05", fr.RC16, 0.05)]}})
    assert rep["polar"]["source"] is None and rep["verdict"]["level"] == "unknown"
    assert "Enter the TPoint polar error" in rep["verdict"]["text"]
    assert rep["model_grid"] is None
    empty = fr.report(_cfg(tmp_path), nights=3, end_night="2026-10-05", measured={})
    assert empty["verdict"]["level"] == "unknown" and empty["blocks"] == []


def test_split_before_after_from_tpoint_record(tmp_path):
    from photonscript.scheduler import thesky_audit as ta
    cfg = _cfg(tmp_path)
    assert ta.save_manual(cfg, {"model_date": "2026-10-04", "points": 250, "rms_arcsec": 15.77,
                                "polar_az_arcmin": -2.6, "polar_alt_arcmin": -1.0,
                                "protrack_on": True})["ok"]
    measured = {"2026-09-25": {"blocks": [_b("2026-09-25", fr.RC16, 0.10, dec=66.6),
                                          _b("2026-09-25", fr.PIGGY, 0.095, dec=66.6)]},
                "2026-10-05": {"blocks": [_b("2026-10-05", fr.RC16, 0.014, dec=38.0),
                                          _b("2026-10-05", fr.PIGGY, 0.013, dec=38.0)]}}
    rep = fr.report(cfg, nights=14, end_night="2026-10-05", measured=measured)
    assert rep["polar"]["source"] == "TPoint record" and rep["polar"]["total_arcmin"] == 2.79
    assert rep["split"] == {"value": "2026-10-04", "source": "TPoint model date",
                            "rule": "night (evening date) on or after = after"}
    assert rep["main_era"] == "after" and rep["verdict"]["level"] == "ok"
    assert rep["eras"]["before"]["verdict"]["level"] == "warn"
    assert rep["eras"]["after"]["n"] == 2 and rep["eras"]["before"]["n"] == 2
    # an explicit timestamp split, and a query polar override
    rep = fr.report(cfg, nights=14, end_night="2026-10-05", measured=measured,
                    split="2026-10-06T00:00:00Z", ma=0.0, me=10.0)
    assert rep["split"]["source"] == "query" and rep["polar"]["total_arcmin"] == 10.0
    assert {b["era"] for b in rep["blocks"]} == {"before"}
    assert rep["main_era"] == "all"
    assert any("no rated blocks on or after" in n for n in rep["notes"])
    txt = fr.format_report(rep)
    assert "VERDICT" in txt and "cost rc16" in txt and txt.isascii()


def test_report_cost_uses_rig_scale_and_sub(tmp_path):
    rep = _rep(tmp_path, {"2026-10-05": [_b("2026-10-05", fr.RC16, 0.10),
                                         _b("2026-10-05", fr.PIGGY, 0.10)]})
    c = rep["cost"]
    assert c["rc16"]["scale_arcsec"] == 0.236 and c["rc16"]["sub_s"] == 600
    assert c["rc16"]["measured"]["sub"]["corner_px"] == pytest.approx(1.09, abs=0.02)
    assert c["piggyback"]["scale_arcsec"] == 1.29 and c["piggyback"]["sub_s"] == 120
    assert c["piggyback"]["measured"]["night"]["corner_px"] == pytest.approx(39.2, abs=0.3)
    assert c["rc16"]["predicted_deg_h"] == pytest.approx(fr.envelope_rate(2.79, 40.0), abs=1e-3)


# --------------------------------------------------------------------------
# from disk, cache, route, page
# --------------------------------------------------------------------------

def _write_night(cfg, rate=0.07):
    from photonscript.shared import pointing, star_table
    r, p, t, _ = _block(fr.RC16, rate)
    runs = Path(cfg.data_dir) / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    (runs / f"{NIGHT}_subs.jsonl").write_text(
        "\n".join(json.dumps(x) for x in r) + "\n", encoding="utf-8")
    for x in r:
        star_table.write(cfg, NIGHT, x["file"], t[x["file"]], fr.RC16)
        pointing.append_record(cfg, NIGHT, p[(fr.RC16, x["file"])])


def test_measure_night_from_disk_and_cache(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _write_night(cfg)
    m = fr.measure_night(cfg, NIGHT)
    assert m["blocks"][0]["rate_deg_h"] == pytest.approx(0.07, abs=0.003)
    assert fr.cache_path(cfg, NIGHT).exists()
    calls = []
    monkeypatch.setattr(fr, "analyze_night", lambda *a, **k: calls.append(1) or {})
    assert fr.measure_night(cfg, NIGHT)["blocks"][0]["rate_deg_h"] == m["blocks"][0]["rate_deg_h"]
    assert calls == []
    fr.measure_night(cfg, NIGHT, refresh=True)
    assert calls == [1]


def test_route(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    _write_night(cfg)
    monkeypatch.setattr(app, "_config", cfg)
    c = TestClient(app.app)
    d = c.get(f"/api/rotation/report?nights=3&end={NIGHT}&ma=-2.6&me=-1.0&split=2026-10-04").json()
    assert d["ok"] and d["main_era"] == "after"
    assert d["blocks"][0]["rate_deg_h"] == pytest.approx(0.07, abs=0.003)
    assert d["verdict"]["level"] == "warn"            # 0.07 at Dec 38 > 2.8'
    assert c.get("/api/rotation/report?night_hours=40").status_code == 400


def test_guiding_tab_section_wired():
    html = (ROOT / "photonscript/scheduler/templates/guiding.html").read_text(encoding="utf-8")
    js = (ROOT / "photonscript/scheduler/static/js/thesky_panels.js").read_text(encoding="utf-8")
    assert 'id="tsRotation"' in html and "Field rotation" in html
    assert "/api/rotation/report?nights=14" in js and "loadRotation();" in js
    for p in ("photonscript/scheduler/field_rotation.py",
              "photonscript/scheduler/routers/rotation.py"):
        src = (ROOT / p).read_text(encoding="utf-8")
        assert src.isascii()
