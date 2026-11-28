"""PS-95: RC16 per-night tilt and collimation report from the PS-80 star
sidecars (scheduler.optics_report), the API / run-detail block, the retired
daily corner-spread Pushover, and the axial math shared with PS-84."""
import asyncio
import json
import math

import numpy as np
import pytest

from photonscript.scheduler import optics_report as orp
from photonscript.shared import star_table
from photonscript.shared.config import PhotonScriptConfig

NIGHT = "2026-09-26"
W, H = 6224, 4168
SCALE = 0.24


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _table(fwhm_fn, ecc=0.2, theta=None, nx=20, ny=14, ecc_def="sqrt(1-(b/a)^2)",
           seed=1):
    """Synthetic sidecar: a star grid over the full frame. fwhm_fn(u, v)
    gives FWHM-eq in arcsec at normalized position (u, v in -1..1)."""
    rng = np.random.default_rng(seed)
    xs, ys, hfr, ec, th = [], [], [], [], []
    for j in range(ny):
        for i in range(nx):
            x = (i + 0.5) * W / nx
            y = (j + 0.5) * H / ny
            u, v = (x - W / 2) / (W / 2), (y - H / 2) / (H / 2)
            xs.append(round(x, 1))
            ys.append(round(y, 1))
            hfr.append(round(fwhm_fn(u, v) / (2 * SCALE), 3))
            ec.append(ecc)
            th.append(float(theta) if theta is not None
                      else float(rng.uniform(-math.pi / 2, math.pi / 2)))
    return {"v": 1, "rig": "rc16", "grader": "test", "ecc_def": ecc_def,
            "w": W, "h": H, "n": len(xs), "x": xs, "y": ys, "hfr": hfr,
            "ecc": ec, "theta": th}


def _tilt(u, v):         # lower-right soft, upper-left sharp
    return 3.0 * (1 + 0.10 * (u + v))


def _flat(u, v):
    return 3.0


def _curved(u, v):       # every corner soft by the same amount
    return 2.5 * (1 + 0.125 * (u * u + v * v))


# ------------------------------------------------------------- per sub

def test_linear_gradient_is_tilt_with_the_right_soft_corner():
    m = orp.sub_field_map(_table(_tilt), SCALE)
    assert m["ok"] and m["soft_corner"] == "BR" and m["sharp_corner"] == "TL"
    assert m["tilt_dir"] == "lower-right"
    assert m["corner_ratio"] == pytest.approx(1.5, abs=0.05)
    assert m["zones"]["BR"]["fwhm"] > m["zones"]["C"]["fwhm"] \
        > m["zones"]["TL"]["fwhm"]
    c = orp.classify(m)
    assert c["verdict"] == orp.TILT and "lower-right" in c["why"]


def test_symmetric_corners_are_curvature_not_tilt():
    m = orp.sub_field_map(_table(_curved), SCALE)
    assert m["corner_ratio"] < 1.05
    assert m["corner_mean_ratio"] >= orp.CURVE_MIN
    assert m["sharp_offset"] is not None and m["sharp_offset"] < 0.1
    assert orp.classify(m)["verdict"] == orp.CURVATURE


def test_uniform_theta_is_tracking():
    m = orp.sub_field_map(_table(_flat, ecc=0.6, theta=0.3), SCALE)
    assert m["elongation"]["R"] == pytest.approx(1.0)
    assert m["elongation"]["radial_frac"] < orp.TRACK_RADIAL_MAX
    assert orp.classify(m)["verdict"] == orp.TRACKING


def test_elongated_center_random_direction_is_collimation():
    m = orp.sub_field_map(_table(_flat, ecc=0.6), SCALE)
    assert m["elongation"]["R"] < orp.DIRECTION_R_MIN
    assert m["center_ecc"] == pytest.approx(0.6)
    c = orp.classify(m)
    assert c["verdict"] == orp.COLLIMATION


def test_round_flat_field_is_fine():
    assert orp.classify(orp.sub_field_map(_table(_flat), SCALE))["verdict"] \
        == orp.FINE


def test_too_few_stars_per_zone_is_skipped():
    m = orp.sub_field_map(_table(_tilt, nx=6, ny=5), SCALE)   # 30 stars
    assert not m["ok"] and "too few stars" in m["why"]
    assert orp.classify(m)["verdict"] == orp.NO_DATA
    assert orp.sub_field_map(None, SCALE)["ok"] is False


def test_lin_ecc_def_converts_to_sqrt_form():
    assert orp._ecc_sqrt(0.2, "1-b/a") == pytest.approx(0.6)
    assert orp._ecc_sqrt(0.6, "sqrt(1-(b/a)^2)") == pytest.approx(0.6)
    assert orp._ecc_sqrt(0.0, "1-b/a") == pytest.approx(0.0)
    m = orp.sub_field_map(_table(_flat, ecc=0.2, ecc_def="1-b/a"), SCALE)
    assert m["center_ecc"] == pytest.approx(0.6)
    # 1-b/a 0.2 is a clearly elongated star: collimation, not "fine"
    assert orp.classify(m)["verdict"] == orp.COLLIMATION


# ------------------------------------------------------------- axial math

def test_axial_math_shared_with_the_tracking_test():
    from photonscript.scheduler import tracking_test as tt
    th = [math.radians(170)] * 10 + [math.radians(10)] * 10
    ax = orp.axial_stats(th, [0.5] * 20, floor=0.3, min_n=15)
    assert ax["R"] == pytest.approx(math.cos(math.radians(20)), abs=0.01)
    assert min(ax["axis_deg"], 180.0 - ax["axis_deg"]) < 0.1
    assert orp.axial_stats(th, [0.1] * 20) == {}   # all under the floor
    assert tt._axial_mean([170, 10]) == orp.axial_mean([170, 10])
    assert orp.sector(45) == "lower-right" and orp.sector(270) == "top"


# ------------------------------------------------------------- per night

def _rec(i, flt="L", exp=60.0, target="M31", hfr_ok=True):
    return {"rig": "rc16", "file": f"LIGHT/{flt}_{exp:g}_{i:03d}.fits",
            "filter": flt, "exp_s": exp, "target": target, "hfr": 6.0,
            "stars": 300, "time": f"2026-09-27T04:{i:02d}:00",
            "scorecard": {"rows": [["hfr", 6.0, 10.0,
                                    "pass" if hfr_ok else "fail"],
                                   ["stars", 300, 5, "pass"]]}}


def test_selection_prefers_tracking_test_rungs_then_shortest():
    tt_name = "Tracking test Heart Nebula"
    recs = ([_rec(i, exp=300.0) for i in range(6)]
            + [_rec(10 + i, exp=60.0, target=tt_name) for i in range(3)]
            + [_rec(20 + i, exp=300.0, target=tt_name) for i in range(3)])
    picked, basis = orp.select_subs(recs)
    assert {r["exp_s"] for r in picked} == {60.0}
    assert all(r["target"] == tt_name for r in picked)
    assert "tracking-test" in basis["L"]["basis"]
    assert basis["L"]["tracking_mixed"] is False
    # no tracking test: the shortest exposure wins when it has enough subs
    recs = [_rec(i, "Ha", 600.0) for i in range(5)] \
        + [_rec(10 + i, "Ha", 60.0) for i in range(3)]
    picked, basis = orp.select_subs(recs)
    assert {r["exp_s"] for r in picked} == {60.0}
    # too few at the shortest length: all subs, tracking flagged
    recs = [_rec(i, "Ha", 600.0) for i in range(5)] + [_rec(9, "Ha", 60.0)]
    picked, basis = orp.select_subs(recs)
    assert len(picked) == 6 and basis["Ha"]["tracking_mixed"] is True


def _write(cfg, recs, fn, **kw):
    for k, r in enumerate(recs):
        star_table.write(cfg, NIGHT, r["file"], _table(fn, seed=k, **kw))


def test_night_tilt_report_cache_and_compact(tmp_path):
    cfg = _cfg(tmp_path)
    recs = [_rec(i) for i in range(6)]
    _write(cfg, recs, _tilt)
    bad = _rec(50, hfr_ok=False)        # failed HFR: never measured
    star_table.write(cfg, NIGHT, bad["file"], _table(_flat, ecc=0.6))
    rep = orp.night_optics(cfg, NIGHT, records=recs + [bad])
    assert rep["n_usable"] == 6 and rep["n_selected"] == 6
    o = rep["overall"]
    assert o["verdict"] == orp.TILT and o["n_measured"] == 6
    assert o["tilt"]["stable"] and o["tilt"]["agree"] == 6
    assert o["zones"]["BR"]["ratio"] > 1.1 and o["zones"]["TL"]["ratio"] < 0.9
    head = rep["headline"]
    assert head.startswith("Tilt: lower-right") and "upper-left" in head
    assert "6 of 6 subs" in head and "tilt plate" in head
    assert rep["filters"][0]["filter"] == "L"
    cached = orp.load_cached(cfg, NIGHT)
    assert cached and cached["overall"]["verdict"] == orp.TILT
    c = orp.compact(rep)
    assert c["ok"] and c["verdict"] == orp.TILT and c["zones"]["C"]["fwhm"]
    assert "optics/report?date=" + NIGHT in c["detail_url"]
    assert "Tilt" in orp.format_report(rep)


def test_tilt_direction_not_stable_is_mixed(tmp_path):
    cfg = _cfg(tmp_path)
    recs = [_rec(i) for i in range(6)]
    _write(cfg, recs[:3], _tilt)
    _write(cfg, recs[3:], lambda u, v: 3.0 * (1 - 0.10 * (u + v)))
    o = orp.night_optics(cfg, NIGHT, records=recs)["overall"]
    assert o["verdict"] == orp.MIXED
    assert not o["tilt"]["stable"]


def test_night_without_sidecars_is_no_data(tmp_path):
    cfg = _cfg(tmp_path)
    rep = orp.night_optics(cfg, NIGHT, records=[_rec(i) for i in range(3)])
    assert rep["overall"]["verdict"] == orp.NO_DATA
    assert not orp.compact(rep)["ok"]


def test_cached_report_rebuilds_when_the_subs_log_changes(tmp_path):
    from photonscript.scheduler.runs import append_sub_record
    cfg = _cfg(tmp_path)
    recs = [_rec(i) for i in range(4)]
    _write(cfg, recs, _tilt)
    for r in recs[:3]:
        append_sub_record(cfg, NIGHT, r)
    first = orp.night_optics_cached(cfg, NIGHT)
    assert first["overall"]["n_measured"] == 3
    assert orp.night_optics_cached(cfg, NIGHT)["sig"] == first["sig"]
    append_sub_record(cfg, NIGHT, recs[3])
    assert orp.night_optics_cached(cfg, NIGHT)["overall"]["n_measured"] == 4


# ------------------------------------------------------------- API + config

def test_api_report_trend_and_run_detail(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler.runs import append_sub_record
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
    recs = [_rec(i) for i in range(4)]
    _write(cfg, recs, _tilt)
    for r in recs:
        append_sub_record(cfg, NIGHT, r)
    c = TestClient(app_mod.app)
    rep = c.get(f"/api/optics/report?date={NIGHT}").json()
    assert rep["overall"]["verdict"] == "tilt"
    tr = c.get("/api/optics/trend?nights=10").json()
    assert tr["n_nights"] == 1 and tr["nights"][0]["tilt_dir"] == "lower-right"
    d = c.get(f"/api/runs/{NIGHT}?backfill=false").json()
    assert d["optics"]["ok"] and d["optics"]["verdict"] == "tilt"


def test_config_keys_and_defaults():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    assert by_env["PS_OPTICS_CORNER_ALERT"][4] == "bool"
    assert by_env["PS_OPTICS_MIN_STARS_ZONE"][4] == "int"
    assert by_env["PS_OPTICS_TILT_WARN"][4] == "float"
    c = PhotonScriptConfig(_env_file=None)
    assert c.optics_corner_alert is False
    assert c.optics_min_stars_zone == 8 and c.optics_tilt_warn == 1.20


# ------------------------------------------------------------- live alert

@pytest.mark.parametrize("enabled", [False, True])
def test_daily_corner_spread_pushover_is_behind_the_switch(tmp_path,
                                                         monkeypatch, enabled):
    from photonscript.shared.models import ImageQualityMetrics
    from photonscript.telescope_agent import agent as agent_mod
    from tests.test_scheduler.test_ps21_grading import (
        NIGHT as N21, _agent, _cfg as cfg21, _write_light)
    cfg = cfg21(tmp_path, optics_corner_alert=enabled)
    monkeypatch.setattr(agent_mod, "validate_image",
                        lambda *a, **k: ImageQualityMetrics(
                            hfr_pixels=5.0, star_count=100, eccentricity=0.3,
                            background_adu=400.0, exposure_flag="ok",
                            corner_spread=0.9))
    f = _write_light(tmp_path / "fits" / N21 / "LIGHT" / "c_Ha_300s_0001.fits",
                     size=64, n=2)
    a = _agent(cfg)
    keys = []

    async def _esc(key, *_a, **_k):
        keys.append(key)
    a._escalate = _esc
    asyncio.run(a._process_new_image(f))
    assert any(k.startswith("corners-") for k in keys) is enabled
    # corner_spread is still stored on the record either way
    from photonscript.scheduler.runs import _load_subs
    (rec,) = _load_subs(cfg, N21)
    assert rec["corner_spread"] == 0.9


def test_cli_optics_report(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    cfg = _cfg(tmp_path)
    recs = [_rec(i) for i in range(4)]
    _write(cfg, recs, _tilt)
    from photonscript.scheduler.runs import append_sub_record
    for r in recs:
        append_sub_record(cfg, NIGHT, r)
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)
    res = CliRunner().invoke(cli.app, ["optics-report", "--date", NIGHT,
                                       "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["overall"]["verdict"] == "tilt"


def test_trailing_wins_but_the_soft_side_is_still_reported(tmp_path):
    cfg = _cfg(tmp_path)
    recs = [_rec(i, exp=300.0) for i in range(4)]
    _write(cfg, recs, _tilt, ecc=0.6, theta=0.3)
    rep = orp.night_optics(cfg, NIGHT, records=recs)
    assert rep["overall"]["verdict"] == orp.TRACKING
    head = rep["headline"]
    assert "Underneath it the lower-right corner" in head
    assert "longer than 120 s" in head
