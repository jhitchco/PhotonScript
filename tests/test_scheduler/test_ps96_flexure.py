"""PS-96: Piggy-600 vs RC16 differential flexure (scheduler.flexure) and the
plate-solve store (scheduler.solve_store), on synthetic records + sidecars."""
import json
import math
from datetime import datetime, timedelta

import numpy as np
import pytest

from photonscript.scheduler import flexure, solve_store
from photonscript.scheduler.solve_store import nominal_cd, pix_to_sky
from photonscript.shared.config import PhotonScriptConfig

DATE = "2026-09-25"
T0 = datetime(2026, 9, 26, 6, 0, 0)
CD_P = nominal_cd(1.29, pa_deg=30.0)
CD_R = nominal_cd(0.239, pa_deg=100.0)
WP, HP = 6224, 4168
WR, HR = 6224, 4168


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _sky_to_pix(cd, e, n):
    m = np.array(cd) * 3600.0
    return np.linalg.solve(m, np.array([e, n]))


def _base(seed, w, h, n=250):
    rng = np.random.default_rng(seed)
    return np.column_stack([rng.uniform(80, w - 80, n), rng.uniform(80, h - 80, n)])


def _table(pts, w, h, ecc_def="sqrt(1-(b/a)^2)", theta=0.0):
    return {"v": 1, "x": [float(p[0]) for p in pts], "y": [float(p[1]) for p in pts],
            "w": w, "h": h, "ecc_def": ecc_def, "theta": [theta] * len(pts),
            "ecc": [0.3] * len(pts), "hfr": [2.0] * len(pts)}


def _night(diff=(1.0, 0.0), common=(0.4, -0.2), dithers=(), flip_at=None,
           slew_at=None, step_at=None, minutes=120, piggy_exp=120, rc_exp=300,
           seed=0, guided=True):
    """Records, tables and solves for one synthetic night.
    diff / common: sky "/min (east, north) of the STAR motion; diff is the
    Piggy-only part and reverses after a meridian flip (flip_at minutes).
    dithers: [(minute, (e, n))] common jumps. slew_at: minute of an RC16
    move to a new pointing. step_at: minute of a Piggy-only 4" jump."""
    bp, br = _base(seed, WP, HP), _base(seed + 1, WR, HR)
    dith = sorted(dithers)

    def common_at(t):
        e, n = common[0] * t, common[1] * t
        for tm, (de, dn) in dith:
            if t >= tm:
                e, n = e + de, n + dn
        return e, n

    def diff_at(t):
        if flip_at is None or t < flip_at:
            e, n = diff[0] * t, diff[1] * t
        else:
            e = diff[0] * flip_at - diff[0] * (t - flip_at)
            n = diff[1] * flip_at - diff[1] * (t - flip_at)
        if step_at is not None and t >= step_at:
            e += 4.0
        return e, n

    def pier(t):
        return "EAST" if flip_at is None or t < flip_at else "WEST"

    recs, tp, tr = [], {}, {}
    t = 0.0
    i = 0
    while t + piggy_exp / 60.0 <= minutes:
        mid = t + piggy_exp / 120.0
        ce, cn = common_at(mid)
        de, dn = diff_at(mid)
        d = _sky_to_pix(CD_P, ce + de, cn + dn)
        f = f"p_{i:03d}.fits"
        tp[f] = _table(bp + d, WP, HP)
        recs.append({"rig": "piggyback", "file": f, "exp_s": piggy_exp,
                     "date_obs": (T0 + timedelta(minutes=t)).isoformat(),
                     "target": "Crescent Nebula", "ecc": 0.5, "hfr": 2.8})
        t += piggy_exp / 60.0 + 0.0
        i += 1
    t, i = 0.0, 0
    while t + rc_exp / 60.0 <= minutes + 5:
        mid = t + rc_exp / 120.0
        ce, cn = common_at(mid)
        d = _sky_to_pix(CD_R, ce, cn)
        f = f"r_{i:03d}.fits"
        tr[f] = _table(br + d, WR, HR, ecc_def="1-b/a")
        ra = 305.0 if slew_at is None or t < slew_at else 306.5
        recs.append({"rig": "rc16", "file": f, "exp_s": rc_exp,
                     "date_obs": (T0 + timedelta(minutes=t)).isoformat(),
                     "target": "Crescent Nebula", "ecc": 0.3, "hfr": 3.0,
                     "ra": ra, "dec": 38.3, "pier_side": pier(mid),
                     "guide_rms": 0.5 if guided else None})
        t += rc_exp / 60.0
        i += 1
    sols_p = {f: {"solved": True, "cd": CD_P, "pa": 30.0} for f in tp}
    sols_r = {f: {"solved": True, "cd": CD_R, "pa": 100.0} for f in tr}
    dtimes = [T0 + timedelta(minutes=tm) for tm, _ in dith]
    return recs, tp, tr, sols_p, sols_r, dtimes


def _run(tmp_path, night, **kw):
    recs, tp, tr, sp, sr, dt = night
    return flexure.analyze(_cfg(tmp_path, **kw), DATE, recs, tp, tr, sp, sr,
                           dither_times=dt)


# --------------------------------------------------------------------------

def test_axis_ratio_definitions():
    assert flexure.axis_ratio(0.3, "1-b/a") == pytest.approx(0.7)
    assert flexure.axis_ratio(0.6, "sqrt(1-(b/a)^2)") == pytest.approx(0.8)
    assert flexure.axis_ratio(None) is None


def test_pair_subs_cover_rule():
    p = {"start": T0, "end": T0 + timedelta(seconds=120), "exp_s": 120}
    full = {"start": T0 - timedelta(seconds=10), "end": T0 + timedelta(seconds=290)}
    part = {"start": T0 + timedelta(seconds=60), "end": T0 + timedelta(seconds=360)}
    assert flexure.pair_subs([p], [full])[0]["rc16"] is full
    out = flexure.pair_subs([p], [part])[0]
    assert out["rc16"] is None and out["cover"] == 0.5


def test_injected_differential_drift_measured_and_flagged(tmp_path):
    rep = _run(tmp_path, _night(diff=(1.0, 0.0), common=(0.4, -0.2)))
    assert rep["ok"] and rep["flagged"]
    b = rep["blocks"][0]
    assert b["frame"] == "sky"
    assert b["diff_rate"][0] == pytest.approx(1.0, abs=0.05)
    assert b["diff_rate"][1] == pytest.approx(0.0, abs=0.05)
    assert b["rc16_rate"][0] == pytest.approx(0.4, abs=0.05)
    assert b["piggy_rate"][0] == pytest.approx(1.4, abs=0.05)
    assert b["diff_pa_deg"] == pytest.approx(90.0, abs=3)  # due east


def test_common_motion_and_dithers_cancel(tmp_path):
    night = _night(diff=(0.0, 0.0), common=(0.6, 0.3),
                   dithers=[(31.0, (5.0, -3.0)), (62.0, (-4.0, 6.0)), (93.0, (3.0, 3.0))])
    rep = _run(tmp_path, night)
    b = rep["blocks"][0]
    assert b["diff_rate_arcsec_min"] < 0.1
    assert b["piggy_rate_arcsec_min"] == pytest.approx(math.hypot(0.6, 0.3), abs=0.06)
    assert rep["flagged"] is False
    assert b["segments"] >= 3


def test_threshold_from_config(tmp_path):
    night = _night(diff=(0.3, 0.0), common=(0.0, 0.0))
    assert _run(tmp_path, night)["flagged"] is False
    assert _run(tmp_path, night, flexure_warn_arcsec_min=0.2)["flagged"] is True


def test_meridian_flip_splits_blocks_and_points_to_rings(tmp_path):
    night = _night(diff=(0.8, 0.4), common=(0.0, 0.0), flip_at=60.0, minutes=120)
    rep = _run(tmp_path, night)
    piers = [b["pier_side"] for b in rep["blocks"]]
    assert "EAST" in piers and "WEST" in piers
    east = next(b for b in rep["blocks"] if b["pier_side"] == "EAST")
    west = next(b for b in rep["blocks"] if b["pier_side"] == "WEST")
    assert east["diff_rate"][0] > 0.6 and west["diff_rate"][0] < -0.6
    assert rep["causes"][0]["cause"].startswith("rings")


def test_slew_straddlers_excluded_and_block_split(tmp_path):
    night = _night(diff=(0.0, 0.0), common=(0.0, 0.0), slew_at=60.0, minutes=120)
    rep = _run(tmp_path, night)
    assert rep["summary"]["straddled_slew"] >= 1
    assert len(rep["blocks"]) >= 2
    straddled = [s for b in rep["blocks"] for s in b["shapes"] if s["straddled"]]
    assert straddled
    for b in rep["blocks"]:
        assert b["piggy_registered"] <= b["piggy_subs"] - sum(
            1 for s in b["shapes"] if s["straddled"])


def test_step_jump_points_to_clamp_or_cables(tmp_path):
    night = _night(diff=(0.0, 0.0), common=(0.0, 0.0), step_at=61.0)
    rep = _run(tmp_path, night)
    assert rep["blocks"][0]["steps"] >= 1
    assert rep["causes"][0]["cause"].startswith("clamp slip")


def test_unsolved_rigs_compare_magnitudes_only(tmp_path):
    recs, tp, tr, _sp, _sr, dt = _night(diff=(1.0, 0.0), common=(0.0, 0.0),
                                        guided=False)
    rep = flexure.analyze(_cfg(tmp_path), DATE, recs, tp, tr, {}, {},
                          dither_times=dt)
    b = rep["blocks"][0]
    assert b["frame"] == "pixel" and b["diff_rate"] is None
    assert b["excess_arcsec_min"] == pytest.approx(1.0, abs=0.08)
    assert rep["flagged"] is True
    assert any("plate solution" in n for n in rep["notes"])


def test_guided_rc16_without_track_is_assumed_still(tmp_path):
    recs, tp, _tr, sp, _sr, dt = _night(diff=(1.0, 0.0), common=(0.0, 0.0))
    rep = flexure.analyze(_cfg(tmp_path), DATE, recs, tp, {}, sp, {},
                          dither_times=dt)
    b = rep["blocks"][0]
    assert b["rc16_assumed_still"] is True
    assert b["diff_rate_arcsec_min"] == pytest.approx(1.0, abs=0.05)


def test_shape_verdicts_use_axis_ratio(tmp_path):
    # Piggy ecc 0.5 (sqrt) = q 0.87; RC16 ecc 0.3 (1-b/a) = q 0.7: the RC16
    # is MORE elongated even though its ecc number is smaller
    rep = _run(tmp_path, _night())
    row = rep["blocks"][0]["shapes"][0]
    assert row["piggy_q"] == pytest.approx(0.866, abs=0.01)
    assert row["rc16_q"] == pytest.approx(0.7, abs=0.01)
    assert any("different definitions" in n for n in rep["notes"])


def test_no_piggy_subs(tmp_path):
    rep = flexure.analyze(_cfg(tmp_path), DATE, [], {}, {}, {}, {})
    assert rep["ok"] is False


# --------------------------------------------------------------------------
# solve store

def _kv_for(cd, ra=305.2, dec=38.3):
    return {"PLTSOLVD": "T", "CRVAL1": str(ra), "CRVAL2": str(dec),
            "CD1_1": str(cd[0][0]), "CD1_2": str(cd[0][1]),
            "CD2_1": str(cd[1][0]), "CD2_2": str(cd[1][1])}


def test_solution_geometry_round_trip():
    sol = solve_store.solution_from_kv(_kv_for(nominal_cd(1.29, 30.0)))
    assert sol["scale"] == pytest.approx(1.29, abs=1e-4)
    assert sol["pa"] == pytest.approx(30.0, abs=1e-3)
    assert sol["parity"] == -1
    # north-up, east-left: +x is west, +y is north
    e, n = pix_to_sky(nominal_cd(1.0, 0.0), 1.0, 0.0)
    assert e == pytest.approx(-1.0) and n == pytest.approx(0.0)
    e, n = pix_to_sky(nominal_cd(1.0, 0.0), 0.0, 1.0)
    assert e == pytest.approx(0.0) and n == pytest.approx(1.0)
    assert solve_store.solution_from_kv({"PLTSOLVD": "F"}) is None


def test_solution_from_cdelt_crota():
    sol = solve_store.solution_from_kv({"CRVAL1": "10", "CRVAL2": "20",
                                        "CDELT1": str(-1.29 / 3600),
                                        "CDELT2": str(1.29 / 3600),
                                        "CROTA2": "0"})
    assert sol["scale"] == pytest.approx(1.29, abs=1e-4)
    assert sol["pa"] == pytest.approx(0.0, abs=1e-3)


def test_solve_with_mock_runner_stores_record(tmp_path):
    cfg = _cfg(tmp_path)
    fits_path = tmp_path / "p_000.fits"
    fits_path.write_bytes(b"")
    seen = {}

    def runner(path, fov, hint, radius, timeout):
        seen.update(path=path, hint=hint)
        return _kv_for(CD_P)
    res = solve_store.solve(cfg, fits_path, rig="piggyback", night=DATE,
                            hint=(305.0, 38.3), rel_file="p_000.fits",
                            runner=runner)
    assert res["solved"] and res["pa"] == pytest.approx(30.0, abs=1e-3)
    assert seen["hint"] == (305.0, 38.3)
    stored = solve_store.lookup(cfg, DATE, "piggyback")
    assert np.allclose(stored["p_000.fits"]["cd"], CD_P)
    # an unsolved attempt is recorded too
    assert solve_store.solve(cfg, fits_path, rig="piggyback", night=DATE,
                             rel_file="p_001.fits",
                             runner=lambda *a: None) is None
    assert solve_store.lookup(cfg, DATE, "piggyback")["p_001.fits"]["solved"] is False


def test_solve_without_astap_is_none(tmp_path):
    cfg = _cfg(tmp_path, astap_exe=str(tmp_path / "nope.exe"))
    assert solve_store.solve(cfg, tmp_path / "x.fits") is None


def test_identify_astap_solve_goes_through_store(monkeypatch, tmp_path):
    from photonscript.scheduler import identify
    monkeypatch.setattr(solve_store, "solve",
                        lambda config, path, **k: {"ra": 10.5, "dec": 41.2})
    assert identify._astap_solve(_cfg(tmp_path), tmp_path / "x.fits") == (10.5, 41.2)
    monkeypatch.setattr(solve_store, "solve", lambda config, path, **k: None)
    assert identify._astap_solve(_cfg(tmp_path), tmp_path / "x.fits") is None


# --------------------------------------------------------------------------
# the night from disk (records + sidecars + sampled solves)

def test_build_report_from_disk_with_sampled_solves(tmp_path, monkeypatch):
    from photonscript.scheduler import runs
    from photonscript.shared import star_table
    cfg = _cfg(tmp_path)
    recs, tp, tr, _sp, _sr, _dt = _night(diff=(1.0, 0.0), common=(0.2, 0.0))
    for r in recs:
        r["abs_path"] = str(tmp_path / r["file"])
    p = runs.runs_dir(cfg) / f"{DATE}_subs.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    for f, t in tp.items():
        star_table.write(cfg, DATE, f, t, rig="piggyback")
    for f, t in tr.items():
        star_table.write(cfg, DATE, f, t, rig="rc16")
    solved = []

    def runner(path, fov, hint, radius, timeout):
        solved.append(path.name)
        return _kv_for(CD_R if path.name.startswith("r_") else CD_P)
    rep = flexure.build_report(cfg, DATE, solve=True, runner=runner,
                               dither_times=[])
    assert rep["ok"] and rep["flagged"]
    piggy_solved = [f for f in solved if f.startswith("p_")]
    assert len(piggy_solved) == 3            # first, middle, last
    assert sum(1 for f in solved if f.startswith("r_")) == 1
    assert rep["blocks"][0]["diff_rate"][0] == pytest.approx(1.0, abs=0.05)
    assert flexure.cached_report(cfg, DATE)["flagged"] is True
    # second run reads the store: no new solves
    solved.clear()
    flexure.build_report(cfg, DATE, solve=True, runner=runner, dither_times=[])
    assert solved == []
    # flexure_solve_all solves every Piggy sub
    cfg2 = _cfg(tmp_path / "all", flexure_solve_all=True)
    p2 = runs.runs_dir(cfg2) / f"{DATE}_subs.jsonl"
    p2.parent.mkdir(parents=True, exist_ok=True)
    p2.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    flexure.build_report(cfg2, DATE, solve=True, runner=runner, dither_times=[])
    assert sum(1 for f in solved if f.startswith("p_")) == len(tp)


def test_compact_and_format(tmp_path):
    rep = _run(tmp_path, _night())
    c = flexure.compact(rep)
    assert c["ok"] and c["blocks"][0]["diff_rate_arcsec_min"] > 0.9
    txt = flexure.format_report(rep)
    assert "FLAGGED" in txt and "differential" in txt
    assert all(ord(ch) < 128 for ch in txt)


def test_guide_timeline_dither_times():
    from photonscript.scheduler.phd2_analysis import GuideTimeline
    cfg = PhotonScriptConfig(_env_file=None, observatory_tz="UTC")
    frames = [{"t": float(i * 2), "drop": False, "ra": 0.0, "dec": 0.0,
               "settling": False, "epoch": 0, "ra_ms": 0, "dec_ms": 0,
               "snr": 20.0} for i in range(10)]
    sec = {"start_local": datetime(2026, 9, 26, 6, 0, 0), "end_local": None,
           "closed": "", "frames": frames,
           "events": [("dither", 5, 1.0, 1.0, "x"), ("settle", 6, "started")]}
    tl = GuideTimeline([(sec, 1.0)], cfg)
    assert tl.dithers == [datetime(2026, 9, 26, 6, 0, 8)]
    assert tl.dithers_between(datetime(2026, 9, 26, 6, 0, 0),
                              datetime(2026, 9, 26, 6, 0, 9)) == tl.dithers


# --------------------------------------------------------------------------
# wiring: API, night page, CLI, trends, config fields

@pytest.fixture
def disk_night(tmp_path, monkeypatch):
    from photonscript.scheduler import app, runs
    from photonscript.shared import star_table
    cfg = _cfg(tmp_path, observatory_tz="UTC")
    recs, tp, tr, _sp, _sr, _dt = _night(diff=(1.0, 0.0), common=(0.0, 0.0))
    p = runs.runs_dir(cfg) / f"{DATE}_subs.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    for f, t in tp.items():
        star_table.write(cfg, DATE, f, t, rig="piggyback")
    for f, t in tr.items():
        star_table.write(cfg, DATE, f, t, rig="rc16")
    for f in tp:
        solve_store.solve(cfg, tmp_path / f, rig="piggyback", night=DATE,
                          rel_file=f, runner=lambda *a: _kv_for(CD_P))
    monkeypatch.setattr(app, "_config", cfg)
    return cfg


def test_api_flexure_computes_then_serves_cache(disk_night):
    from photonscript.scheduler import app
    rep = app.api_flexure(DATE)
    assert rep["ok"] and rep["flagged"]
    b = rep["blocks"][0]
    # Piggy solved, RC16 not: magnitudes only (the RC16 track is in pixels)
    assert b["frame"] == "pixel" and b["diff_rate"] is None
    assert b["excess_arcsec_min"] == pytest.approx(1.0, abs=0.05)
    assert flexure.cached_report(disk_night, DATE) is not None
    assert app.api_flexure("2026-09-01")["ok"] is False


def test_night_report_gets_a_compact_flexure_block(disk_night, monkeypatch):
    from photonscript.scheduler import app, runs
    monkeypatch.setattr(runs, "night_detail",
                        lambda config, date, backfill=True: {"date": date, "table": [],
                                                             "subs": []})
    monkeypatch.setattr(app, "_syncthing_pending_names", lambda: set())
    d = app.api_run_detail(DATE, backfill=False)
    fx = d["flexure"]
    assert fx["ok"] and fx["flagged"] and "shapes" not in fx["blocks"][0]


def test_trends_lists_flagged_nights(disk_night, monkeypatch):
    from photonscript.scheduler import runs, trends
    flexure.build_report(disk_night, DATE, dither_times=[])
    monkeypatch.setattr(runs, "list_runs", lambda config: [{"date": DATE}])
    out = trends.flexure_nights(disk_night)
    assert out and out[0]["date"] == DATE and out[0]["flagged"] is True


def test_cli_flexure_report(disk_night, monkeypatch):
    from typer.testing import CliRunner

    from photonscript import cli
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: disk_night)
    r = CliRunner().invoke(cli.app, ["flexure-report", "--date", DATE, "--no-solve"])
    assert r.exit_code == 0, r.output
    assert "FLAGGED" in r.output
    r = CliRunner().invoke(cli.app, ["flexure-report", "--date", DATE,
                                     "--no-solve", "--json"])
    assert json.loads(r.output)["flagged"] is True


def test_config_fields_and_safe_defaults():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    assert by_env["PS_FLEXURE_WARN_ARCSEC_MIN"][4] == "float"
    assert by_env["PS_FLEXURE_SOLVE_ALL"][4] == "bool"
    c = PhotonScriptConfig(_env_file=None)
    assert c.flexure_warn_arcsec_min == 0.5 and c.flexure_solve_all is False


def test_fresh_report_recomputes_after_new_subs(disk_night, monkeypatch):
    import os
    import time
    from photonscript.scheduler import runs
    flexure.build_report(disk_night, DATE, dither_times=[])
    calls = []
    real = flexure.build_report
    monkeypatch.setattr(flexure, "build_report",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    assert flexure.fresh_report(disk_night, DATE)["ok"] and calls == []
    sp = runs.runs_dir(disk_night) / f"{DATE}_subs.jsonl"
    later = time.time() + 5
    os.utime(sp, (later, later))
    assert flexure.fresh_report(disk_night, DATE)["ok"] and calls == [1]
