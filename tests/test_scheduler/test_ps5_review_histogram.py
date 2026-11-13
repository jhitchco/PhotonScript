"""PS-5: histogram in the review lightbox, on the PS-108 /hist endpoint.

The endpoint gives one L curve for a mono sub and R, G, B (Bayer sites
counted apart) for a Piggy-600 OSC sub, with the clip percentages; the dawn
warm caches the histograms (a page view never does); the lightbox panel
fetches lazily (only while its section is open, after a short dwell, once
per sub), draws per-channel medians, the bias / median / saturation marks
and black / white clip marks, and has a visible log / linear toggle.
"""
import time
from pathlib import Path

import numpy as np

from photonscript.scheduler import runs
from tests.test_scheduler.test_ps21_grading import NIGHT, _cfg, _rec, _seed, _write_light
from tests.test_scheduler.test_ps115_review_panel import _func, _page, _script


def _client(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    from fastapi.testclient import TestClient
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    monkeypatch.setattr(app, "_config", cfg)
    return cfg, TestClient(app.app)


def _osc_frame(path: Path, levels=(1000, 2000, 3000), bayerpat=None):
    """RGGB mosaic: R sites at `levels[0]`, both G at [1], B at [2], a few
    pixels at 0 and at 65535."""
    from astropy.io import fits
    a = np.zeros((64, 96), dtype=np.uint16)
    a[0::2, 0::2] = levels[0]
    a[0::2, 1::2] = levels[1]
    a[1::2, 0::2] = levels[1]
    a[1::2, 1::2] = levels[2]
    a[10, 10] = 0
    a[20, 21] = 65535
    h = fits.Header()
    h["IMAGETYP"] = "LIGHT"
    if bayerpat:
        h["BAYERPAT"] = bayerpat
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(a, header=h).writeto(path, overwrite=True)
    return path


def test_osc_sub_gets_r_g_b_curves_and_clip_numbers(tmp_path, monkeypatch):
    cfg, client = _client(tmp_path, monkeypatch)
    f = _osc_frame(tmp_path / "fits" / NIGHT / "LIGHT" / "osc.fits")
    _seed(cfg, [_rec("osc", rig="piggyback", filter="OSC", abs_path=str(f))])
    r = client.get(f"/api/runs/{NIGHT}/hist", params={"file": "LIGHT/osc.fits"})
    assert r.status_code == 200
    h = r.json()
    assert set(h["channels"]) == {"R", "G", "B"}
    med = {k: v["median"] for k, v in h["channels"].items()}
    assert med["R"] == 1000 and med["G"] == 2000 and med["B"] == 3000
    assert h["channels"]["G"]["n"] == 2 * h["channels"]["R"]["n"]
    assert h["bayer"] == "RGGB" and h["bayer_assumed"] is True   # no BAYERPAT
    n = 64 * 96
    assert h["black_clip_pct"] == round(100.0 / n, 4)
    assert h["white_clip_pct"] == round(100.0 / n, 4)


def test_mono_sub_gets_one_l_curve(tmp_path, monkeypatch):
    cfg, client = _client(tmp_path, monkeypatch)
    f = _write_light(tmp_path / "fits" / NIGHT / "LIGHT" / "m.fits")
    _seed(cfg, [_rec("m", abs_path=str(f))])
    h = client.get(f"/api/runs/{NIGHT}/hist", params={"file": "LIGHT/m.fits"}).json()
    assert list(h["channels"]) == ["L"] and h["bayer"] is None
    assert 550 <= h["median"] <= 650


def _wait_warm(date, timeout=30.0):
    t0 = time.time()
    while runs._thumbwarm_state.get(date, {}).get("running"):
        assert time.time() - t0 < timeout
        time.sleep(0.05)


def test_dawn_warm_caches_histograms_page_warm_does_not(tmp_path):
    cfg = _cfg(tmp_path)
    f = _write_light(tmp_path / "fits" / NIGHT / "LIGHT" / "w.fits")
    _seed(cfg, [_rec("w", abs_path=str(f))])
    cache = Path(cfg.data_dir) / "hist" / NIGHT / "LIGHT_w.fits.json"
    runs._thumbwarm_state.pop(NIGHT, None)
    runs.start_thumb_warm(cfg, NIGHT)                # the page's warm
    _wait_warm(NIGHT)
    assert not cache.exists()
    runs.start_thumb_warm(cfg, NIGHT, hist=True)     # post_night_warm's
    _wait_warm(NIGHT)
    assert cache.exists()


def test_post_night_warm_asks_for_histograms(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    (tmp_path / "fits" / NIGHT).mkdir(parents=True)
    got = []
    monkeypatch.setattr(runs, "backfill_status", lambda c, d: {"pending": 0})
    monkeypatch.setattr(runs, "start_thumb_warm",
                        lambda c, d, hist=False: got.append((d, hist)))
    runs.post_night_warm(cfg)
    assert (NIGHT, True) in got


def test_lightbox_histogram_is_lazy_with_marks_and_log_toggle(tmp_path, monkeypatch):
    html = _page(tmp_path, monkeypatch)
    js = _script(html)
    # a folded section is never fetched; the open state is remembered
    assert '<details id="lbHistBox" open>' in html and 'id="lbHistLogBtn"' in html
    assert html.index('id="lbRows"') < html.index('id="lbHistBox"')
    load = _func(js, "loadLbHist")
    assert "getElementById('lbHistBox').open" in load
    assert "HIST_MEMO[key]" in load and "setTimeout(go, HIST_DELAY_MS)" in load
    assert "/hist?file=" in load
    assert "/hist?file=" not in _func(js, "loadLbPanel")   # only through loadLbHist
    assert "localStorage.setItem('lbHistOpen'" in js
    # log / linear: the button and a canvas click, remembered
    tog = _func(js, "setHistLog")
    assert "localStorage.setItem('lbHistLog'" in tog and "lbHistLogBtn" in tog
    draw = _func(js, "drawLbHist")
    assert "Math.log10(1 + v)" in draw
    # OSC: R, G, B curves with their medians (dashed)
    assert "c.median" in draw and "setLineDash" in draw and "chans.length > 1" in draw
    # marks: bias floor, median, saturation, black / white clip at the edges
    for m in ("hd.bias_floor", "mark(hd.median", "mark(hd.sat_adu",
              "hd.black_clip_pct > 0", "hd.white_clip_pct > 0", "black clip", "white clip"):
        assert m in draw, m


def test_new_sources_are_ascii():
    b = Path(__file__).read_bytes()
    assert all(c < 128 for c in b)
