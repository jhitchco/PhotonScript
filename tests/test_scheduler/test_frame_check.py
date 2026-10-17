"""frame-check: NINA's last frames (2026-10-13: two 180 s Imaging-tab
snapshots, ProTrack off then on, never graded and too slow to pull as raw
FITS over Tailscale) found under NINA's save path and measured with the sub
star QA, plus the elongation axis, the trail and the A / B verdict."""
import json
import math

import numpy as np
import pytest
from astropy.io import fits

from photonscript.scheduler import frame_check as fc
from photonscript.shared.config import PhotonScriptConfig

SCALE = 0.236          # RC16 "/px (config default)


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"),
                              image_watch_dir=str(tmp_path / "nina"), **kw)


def _frame(trail_px=0.0, n=90, seed=3, size=640, sigma=2.0):
    """Mono frame: background 500 + noise, n Gaussian stars, each smeared
    along image x over trail_px (a row of sub-stars)."""
    rng = np.random.default_rng(seed)
    img = rng.normal(500.0, 6.0, (size, size)).astype(np.float32)
    yy, xx = np.mgrid[0:size, 0:size]
    steps = max(1, int(round(trail_px)) * 2 + 1)
    for _ in range(n):
        x0, y0 = rng.uniform(30, size - 30, 2)
        amp = rng.uniform(800, 6000)
        for k in range(steps):
            dx = (k / (steps - 1) - 0.5) * trail_px if steps > 1 else 0.0
            sl = (slice(int(y0) - 12, int(y0) + 13), slice(int(x0 + dx) - 12, int(x0 + dx) + 13))
            img[sl] += (amp / steps) * np.exp(-((xx[sl] - x0 - dx) ** 2 + (yy[sl] - y0) ** 2)
                                               / (2 * sigma ** 2))
    return img


def _write(path, data, pa_wcs=True, exp=180.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    h = fits.Header()
    h["EXPTIME"] = exp
    h["FILTER"] = "L"
    h["IMAGETYP"] = "SNAPSHOT"
    if pa_wcs:     # PA 0: north up (image +y), east left (image -x)
        h["CD1_1"], h["CD1_2"], h["CD2_1"], h["CD2_2"] = -SCALE / 3600, 0.0, 0.0, SCALE / 3600
    fits.PrimaryHDU(np.round(data).astype(np.uint16), header=h).writeto(path)
    return path


def _entries():
    return [
        {"ImageType": "LIGHT", "Date": "2026-10-13T20:10:00.1-06:00", "ExposureTime": 300,
         "Filter": "Ha", "Filename": "2026-10-13_20-05-00_Heart_Ha_300.00s_0001.fits"},
        {"ImageType": "SNAPSHOT", "Date": "2026-10-13T20:25:54.1734868-06:00",
         "ExposureTime": 180, "Filter": "L",
         "Filename": "2026-10-13_20-22-52_Snapshot_L_180.00s_0000.fits"},
        {"ImageType": "SNAPSHOT", "Date": "2026-10-13T20:36:30.739614-06:00",
         "ExposureTime": 180, "Filter": "L",
         "Filename": "2026-10-13_20-33-29_Snapshot_L_180.00s_0000.fits"},
    ]


@pytest.fixture
def frames(tmp_path):
    """Snapshot A round, snapshot B trailed 8 px along image x (= RA at PA 0),
    filed as NINA's profile pattern does: <date-12h>\\SNAPSHOT\\<file>."""
    e = _entries()
    root = tmp_path / "nina" / "2026-10-13" / "SNAPSHOT"
    _write(root / e[1]["Filename"], _frame(0.0))
    _write(root / e[2]["Filename"], _frame(8.0, seed=4))
    return [dict(x, index=i) for i, x in enumerate(e)]


def test_history_indexes_and_pick():
    calls = []

    def get(url, timeout=10.0):
        calls.append(url)
        return _entries()
    h = fc.history(None, "rc16", base_url="http://scope:1888/v2/api/", get=get)
    assert calls == ["http://scope:1888/v2/api/image-history?all=true"]
    assert [x["index"] for x in h] == [0, 1, 2]
    assert [x["index"] for x in fc.pick(h, last=2)] == [1, 2]
    assert [x["index"] for x in fc.pick(h, last=1, image_type="light")] == [0]
    assert [x["index"] for x in fc.pick(h, last=2, index=[0, 2])] == [0, 2]


def test_locate_profile_pattern_then_search(tmp_path, frames):
    roots = [tmp_path / "nina"]
    p = fc.locate(roots, frames[1])
    assert p.parent.name == "SNAPSHOT" and p.parent.parent.name == "2026-10-13"
    # filed elsewhere under the save path: still found
    other = tmp_path / "nina" / "misc" / "x.fits"
    _write(other, _frame(0.0, n=5))
    assert fc.locate(roots, {"Filename": "x.fits", "Date": "", "ImageType": "LIGHT"}) == other
    assert fc.locate(roots, {"Filename": "x.fits"}, deep=False) is None
    assert fc.locate(roots, {"Filename": "", "Date": ""}) is None


def test_trail_from_moments():
    # a = sqrt(s^2 + L^2/12), b = s: L back
    s, L = 2.0, 9.0
    a = [math.sqrt(s * s + L * L / 12.0)] * 10
    assert fc.trail_px(a, [s] * 10) == pytest.approx(L)
    assert fc.trail_px([2.0] * 5, [2.0] * 5) == 0.0
    assert fc.trail_px([], []) is None


def test_measure_round_and_trailed(tmp_path, frames):
    cfg = _cfg(tmp_path)
    root = tmp_path / "nina" / "2026-10-13" / "SNAPSHOT"
    ra = fc.measure(cfg, root / frames[1]["Filename"], "rc16")
    rb = fc.measure(cfg, root / frames[2]["Filename"], "rc16")
    assert ra["stars"] > 50 and rb["stars"] > 50
    assert ra["hfr"] and ra["fwhm_arcsec"] and ra["background"] == pytest.approx(500, abs=20)
    assert ra["ecc"] < 0.35 < rb["ecc"]
    assert rb["pa_deg"] == 0.0 and rb["pa_src"] == "WCS"
    assert rb["axis"] == "RA" and rb["sky_axis"] is True
    assert rb["trail_px"] == pytest.approx(8.0, abs=2.0)
    assert rb["trail_arcsec"] == pytest.approx(rb["trail_px"] * SCALE, abs=0.01)
    assert rb["trail_rate"] == pytest.approx(rb["trail_arcsec"] / 3.0, abs=0.01)
    assert (ra["trail_arcsec"] or 0) < fc.TRAIL_ROUND_ARCSEC


def test_check_compare_verdict_and_table(tmp_path, frames):
    cfg = _cfg(tmp_path)
    rep = fc.check(cfg, "rc16", last=2, compare=True, entries=frames, profile_path="")
    a, b = rep["frames"]
    assert (a["label"], a["index"], b["label"], b["index"]) == ("A", 1, "B", 2)
    assert a["shape"] == "round" and b["shape"] == "trailed"
    assert rep["verdict"].startswith("A round; B trails along RA at ")
    assert "\"/min" in rep["verdict"]
    txt = fc.format_report(rep)
    assert "SNAPSHOT" in txt and "Verdict: A round; B trails along RA" in txt
    assert "camera PA: 0.0 deg (WCS)" in txt
    json.dumps(rep)                                   # the endpoint's answer


def test_both_round_and_no_pa(tmp_path):
    cfg = _cfg(tmp_path)
    e = _entries()
    root = tmp_path / "nina" / "2026-10-13" / "SNAPSHOT"
    _write(root / e[1]["Filename"], _frame(0.0), pa_wcs=False)
    _write(root / e[2]["Filename"], _frame(0.0, seed=5), pa_wcs=False)
    ents = [dict(x, index=i) for i, x in enumerate(e)]
    rep = fc.check(cfg, "rc16", compare=True, entries=ents, profile_path="")
    assert rep["verdict"] == "both round"
    assert "camera PA unknown" in fc.format_report(rep)
    # the --pa override names the sky axis
    assert fc.camera_pa(cfg, "rc16", {}, 90.0) == (90.0, "request")


def test_solve_pa_fallback(tmp_path):
    cfg = _cfg(tmp_path)
    d = tmp_path / "data" / "solves" / "2026-10-12"
    d.mkdir(parents=True)
    (d / "rc16.jsonl").write_text(json.dumps({"file": "a.fits", "pa": 181.5}) + "\n",
                                  encoding="utf-8")
    pa, src = fc.camera_pa(cfg, "rc16", {})
    assert pa == pytest.approx(181.5) and src == "solve 2026-10-12"


def test_missing_file_and_unreachable_nina_print_the_scope_command(tmp_path):
    cfg = _cfg(tmp_path)
    ents = [dict(x, index=i) for i, x in enumerate(_entries())]
    rep = fc.check(cfg, "rc16", last=2, compare=True, entries=ents, profile_path="")
    assert all(not r["measured"] and "file not found" in r["error"] for r in rep["frames"])
    txt = fc.format_report(rep)
    assert "On the scope PC run:" in txt
    assert "C:\\astro\\venv\\Scripts\\photonscript.exe frame-check --rig rc16 " \
           "--last 2 --compare" in txt

    def boom(*_a, **_k):
        raise OSError("timed out")
    import photonscript.scheduler.frame_check as mod
    orig = mod._get_json
    mod._get_json = boom
    try:
        rep = fc.check(cfg, "piggyback", index=[3], compare=True)
    finally:
        mod._get_json = orig
    assert rep["ok"] is False and "timed out" in rep["error"]
    assert rep["command"].endswith("frame-check --rig piggyback --index 3 --compare")
    assert "On the scope PC run:" in fc.format_report(rep)


def test_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from photonscript.scheduler import app as app_mod
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
    seen = {}

    def fake(config, **kw):
        seen.update(kw)
        return {"ok": True, "frames": [], "verdict": None, "command": "x"}
    monkeypatch.setattr(fc, "check", fake)
    c = TestClient(app_mod.app)
    r = c.get("/api/frame-check?rig=piggyback&last=50&index=1&index=2&type=SNAPSHOT")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert seen["rig"] == "piggyback" and seen["last"] == 10
    assert seen["index"] == [1, 2] and seen["image_type"] == "SNAPSHOT"
    assert c.get("/api/frame-check?rig=bogus").status_code == 400


def test_cli_url_reads_the_scope_endpoint(tmp_path, monkeypatch):
    import io

    from typer.testing import CliRunner

    from photonscript import cli
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(cli, "_config_for_repo", lambda *_a, **_k: cfg)
    got = {}
    body = {"ok": True, "rig": "rc16", "command": "c", "verdict": "both round",
            "frames": [{"label": "A", "index": 0, "date": "2026-10-13T20:25:54-06:00",
                        "image_type": "SNAPSHOT", "exp_s": 180, "filter": "L",
                        "measured": True, "stars": 120, "hfr": 2.1, "fwhm_arcsec": 1.9,
                        "ecc": 0.31, "axis": None, "trail_arcsec": 0.4,
                        "trail_rate": 0.13, "background": 512.0, "shape": "round",
                        "pa_deg": None, "pa_src": None}]}

    class _R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(u, timeout=0):
        got["u"] = u
        return _R(json.dumps(body).encode("utf-8"))
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    out = CliRunner().invoke(cli.app, ["frame-check", "--url", "http://scope:8100/",
                                       "--last", "1", "--compare"])
    assert out.exit_code == 0, out.output
    assert got["u"] == ("http://scope:8100/api/frame-check?rig=rc16&last=1&compare=true")
    assert "Verdict: both round" in out.output and "SNAPSHOT" in out.output
