"""PS-94: the native-vs-binned eccentricity report (scheduler.ecc_scale) and
the formula-blind consumers (trends, tracking test) reading sqrt form."""

import json
import math
import time
from types import SimpleNamespace

import numpy as np
import pytest

from photonscript.shared.config import PhotonScriptConfig

NIGHT = "2026-09-26"


def _need_sep():
    try:
        import sep  # noqa: F401
    except ImportError:
        pytest.importorskip("sep_pjw")


def _cfg(tmp_path, **kw):
    base = dict(_env_file=None, data_dir=str(tmp_path / "data"),
                image_watch_dir=str(tmp_path / "fits"),
                library_dir=str(tmp_path / "lib"),
                nina_logs_dir=str(tmp_path / "logs"),
                quality_eccentricity_max=0.6)
    base.update(kw)
    (tmp_path / "data").mkdir(exist_ok=True)
    return PhotonScriptConfig(**base)


def _write(path, q, flt="Ha", obj="Test Nebula", seed=0):
    """FWHM 8 px stars with axis ratio q (sqrt-form ecc sqrt(1-q^2))."""
    from astropy.io import fits
    rng = np.random.default_rng(seed)
    d = rng.normal(600, 9, (320, 320)).astype(np.float32)
    s = 8.0 / 2.3548
    sa, sb = s / math.sqrt(q), s * math.sqrt(q)
    yy, xx = np.mgrid[-15:16, -15:16]
    for cy in range(30, 300, 40):
        for cx in range(30, 300, 40):
            th = rng.uniform(0, math.pi)
            u = xx * math.cos(th) + yy * math.sin(th)
            v = -xx * math.sin(th) + yy * math.cos(th)
            g = np.exp(-0.5 * (u ** 2 / sa ** 2 + v ** 2 / sb ** 2))
            d[cy - 15:cy + 16, cx - 15:cx + 16] += (6000 * g).astype(np.float32)
    h = fits.Header()
    h["IMAGETYP"], h["EXPTIME"], h["FILTER"], h["OBJECT"] = \
        "LIGHT", 300.0, flt, obj
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(d.astype(np.uint16), header=h).writeto(path, overwrite=True)
    return path


def _night(tmp_path, cfg):
    """Four RC16 lights + one calibration frame + a stored subs log with one
    old 1-b/a backfill record and one live record."""
    from photonscript.scheduler.runs import append_sub_record
    root = tmp_path / "fits" / NIGHT
    _write(root / "LIGHT" / "a_Ha_0001.fits", 1.0)
    _write(root / "LIGHT" / "b_Ha_0002.fits", 0.78, seed=1)
    _write(root / "LIGHT" / "c_Ha_0003.fits", 0.6, seed=2)
    _write(root / "LIGHT" / "d_OIII_0004.fits", 0.9, flt="OIII", seed=3)
    _write(root / "FLAT" / "f_Ha_0001.fits", 1.0)
    append_sub_record(cfg, NIGHT, {
        "rig": "rc16", "file": "LIGHT/b_Ha_0002.fits", "target": "Test Nebula",
        "filter": "Ha", "ecc": 0.2, "graded_by": "sep-binned",
        "passed_qa": True})
    append_sub_record(cfg, NIGHT, {
        "rig": "rc16", "file": "LIGHT/c_Ha_0003.fits", "target": "Test Nebula",
        "filter": "Ha", "ecc": 0.79, "passed_qa": False})
    return root


def test_compare_night_counts_flips_and_is_a_dry_run(tmp_path):
    from photonscript.scheduler import ecc_scale
    from photonscript.scheduler.runs import runs_dir
    _need_sep()
    cfg = _cfg(tmp_path)
    _night(tmp_path, cfg)
    log = runs_dir(cfg) / f"{NIGHT}_subs.jsonl"
    before = log.read_bytes()
    rep = ecc_scale.compare_night(cfg, NIGHT)
    assert log.read_bytes() == before                # never touches the log
    assert rep["dry_run"] and rep["n_files"] == 4 and rep["n_measured"] == 4
    assert rep["graded_by"] == {"sep-binned": 1, "live": 1}
    assert rep["n_records_lin"] == 1
    by = {s["file"]: s for s in rep["subs"]}
    # native tracks truth (sqrt form); binned reads rounder on elongated stars
    assert by["LIGHT/c_Ha_0003.fits"]["ecc"] == pytest.approx(0.8, abs=0.04)
    assert by["LIGHT/b_Ha_0002.fits"]["delta"] < 0
    # the stored 1-b/a record is shown in sqrt form next to the fresh measure
    assert by["LIGHT/b_Ha_0002.fits"]["stored"]["ecc_sqrt"] == 0.6
    assert by["LIGHT/a_Ha_0001.fits"]["stored"]["graded_by"] is None
    # pass counts and flips agree with the per-sub values
    for g in ("0.6", "0.7"):
        gv = float(g)
        assert rep["pass"][g]["native"] == sum(
            1 for s in rep["subs"] if s["ecc"] <= gv)
        assert rep["pass"][g]["binned"] == sum(
            1 for s in rep["subs"] if s["ecc_bin"] <= gv)
        assert set(rep["flips"][g]["pass_binned_only"]) == {
            s["file"] for s in rep["subs"] if s["ecc_bin"] <= gv < s["ecc"]}
    groups = {(g["target"], g["filter"]): g for g in rep["groups"]}
    assert groups[("Test Nebula", "Ha")]["n"] == 3
    assert groups[("Test Nebula", "OIII")]["n"] == 1
    saved = json.loads(ecc_scale.report_path(cfg, NIGHT).read_text())
    assert saved["n_measured"] == 4
    text = ecc_scale.format_report([rep])
    assert "PS-94 ecc scale report" in text and "gate 0.6" in text


def test_compare_night_with_no_folder(tmp_path):
    from photonscript.scheduler import ecc_scale
    rep = ecc_scale.compare_night(_cfg(tmp_path), "2026-01-01", write=False)
    assert rep["n_files"] == 0 and rep["verdict"] == "no measured subs"


@pytest.fixture
def client(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    import photonscript.scheduler.runs as runs
    from photonscript.scheduler import ecc_scale
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    monkeypatch.setattr(runs, "_backfill_state", {})
    monkeypatch.setattr(runs, "_regrade_all", {"running": False})
    monkeypatch.setattr(ecc_scale, "_jobs", {})
    from fastapi.testclient import TestClient
    c = TestClient(app.app)
    c.cfg, c.app = cfg, app
    return c


@pytest.mark.parametrize("state", ["ARMED", "RUNNING", "PAUSED_UNSAFE"])
def test_api_refused_while_armed(client, monkeypatch, state, tmp_path):
    monkeypatch.setattr(client.app, "get_armer",
                        lambda: SimpleNamespace(state=state))
    r = client.get("/api/qa/ecc-scale", params={"date": NIGHT})
    assert r.status_code == 409 and state in r.json()["detail"]


def test_api_refused_while_grading(client, monkeypatch):
    import photonscript.scheduler.runs as runs
    monkeypatch.setattr(client.app, "get_armer",
                        lambda: SimpleNamespace(state="DISARMED"))
    runs._backfill_state[NIGHT] = {"running": True}
    r = client.get("/api/qa/ecc-scale", params={"date": NIGHT})
    assert r.status_code == 409 and "grading" in r.json()["detail"]


def test_api_runs_in_background_then_serves_the_saved_report(
        client, monkeypatch, tmp_path):
    from photonscript.scheduler.runs import runs_dir
    _need_sep()
    monkeypatch.setattr(client.app, "get_armer",
                        lambda: SimpleNamespace(state="DISARMED"))
    _night(tmp_path, client.cfg)
    before = (runs_dir(client.cfg) / f"{NIGHT}_subs.jsonl").read_bytes()
    r = client.get("/api/qa/ecc-scale", params={"date": NIGHT})
    assert r.status_code == 202
    for _ in range(200):
        r = client.get("/api/qa/ecc-scale", params={"date": NIGHT})
        if r.status_code == 200:
            break
        time.sleep(0.05)
    assert r.status_code == 200 and r.json()["n_measured"] == 4
    assert (runs_dir(client.cfg) / f"{NIGHT}_subs.jsonl").read_bytes() == before
    # cached: served even while armed; refresh while armed is refused
    monkeypatch.setattr(client.app, "get_armer",
                        lambda: SimpleNamespace(state="ARMED"))
    assert client.get("/api/qa/ecc-scale",
                      params={"date": NIGHT}).status_code == 200
    assert client.get("/api/qa/ecc-scale", params={
        "date": NIGHT, "refresh": "true"}).status_code == 409


# ------------------------------------------ formula-blind consumers (sqrt)

def test_trends_convert_old_backfill_records(monkeypatch):
    """40 old backfill subs at lin 0.30 (sqrt 0.71) in one direction: the
    polar-drift alarm fires once the formula is unified (it read 0.30 < 0.60
    before PS-94 and stayed silent)."""
    from photonscript.scheduler import trends
    subs = [{"rig": "rc16", "ecc": 0.30, "graded_by": "sep-binned",
             "ecc_pa_R": 0.8, "ecc_radial_frac": 0.2, "hfr": 2.5,
             "_night": f"2026-09-{20 + i % 4}"} for i in range(40)]
    monkeypatch.setattr(trends, "_recent_light_subs", lambda c, n: subs)
    res = trends.analyze_trends(PhotonScriptConfig(_env_file=None))
    assert res["medians"]["ecc"] == pytest.approx(0.714, abs=1e-3)
    assert [f["kind"] for f in res["findings"]] == ["polar_drift"]


def test_tracking_test_groups_use_sqrt_form(tmp_path):
    from photonscript.scheduler.tracking_test import group_records
    cfg = _cfg(tmp_path)
    recs = [{"rig": "rc16", "filter": "Ha", "exp_s": 300.0, "target": "T",
             "ecc": 0.25, "graded_by": "sep-binned"} for _ in range(4)]
    (g,) = group_records(cfg, recs, read_headers=False)
    assert g["ecc_median"] == pytest.approx(0.661, abs=1e-3)
    assert g["tracking_pass"] == 0 and g["status"] == "fail"
    # binned gating, when chosen, judges ecc_bin against its own limit
    cfg_b = _cfg(tmp_path, qa_ecc_scale="binned",
                 quality_eccentricity_max_binned=0.7)
    recs = [{"rig": "rc16", "filter": "Ha", "exp_s": 300.0, "target": "T",
             "ecc": 0.66, "ecc_bin": 0.5} for _ in range(4)]
    (g,) = group_records(cfg_b, recs, read_headers=False)
    assert g["ecc_gate"] == 0.7 and g["ecc_median"] == 0.5
    assert g["status"] == "pass"
