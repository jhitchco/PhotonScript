"""PS-6: loupe crops from the full-resolution frame.

Crops clamp at the corners and return the requested size, match a direct
FITS slice through the preview stretch (whose points match the 1400 px
preview's), 2:1 is an exact nearest-neighbor upscale, OSC shows the 2x2
superpixel; the endpoint sends the window in headers, 404 without the
FITS, 503 when busy.
"""
import io
from pathlib import Path

import numpy as np
import pytest

from tests.test_scheduler.test_ps21_grading import NIGHT
from tests.test_scheduler.test_ps80_viewer_stars import (  # noqa: F401
    _fresh_caches, _noise_frame, _png_array, _setup)


# ----------------------------------------------------------- PS-6 crops

def test_crop_clamps_at_corners_and_matches_a_direct_slice(tmp_path):
    from astropy.io import fits

    from photonscript.scheduler import sub_viewer as sv
    data = _noise_frame()
    cfg, rel = _setup(tmp_path, data)
    raw = fits.getdata(Path(cfg.image_watch_dir) / NIGHT / rel).astype(np.float32)
    h, w = raw.shape
    for (x, y), (ex0, ey0) in {(0, 0): (0, 0), (w, 0): (w - 256, 0),
                               (0, h): (0, h - 256), (w, h): (w - 256, h - 256),
                               (-50, 99999): (0, h - 256)}.items():
        c = sv.crop(cfg, NIGHT, rel, x=x, y=y, size=256)
        assert (c["x0"], c["y0"]) == (ex0, ey0)
        a = _png_array(c["png"])
        assert a.shape == (256, 256)
    c = sv.crop(cfg, NIGHT, rel, x=450, y=300, size=256)
    info = next(iter(sv._frame_cache.values()))
    want = sv.to_u8(raw[c["y0"]:c["y0"] + 256, c["x0"]:c["x0"] + 256],
                    info["lo"], info["hi"])
    assert np.array_equal(_png_array(c["png"]), want)
    assert (c["w"], c["h"], c["osc"]) == (w, h, False)
    # fractions address the same spot as native px
    cf = sv.crop(cfg, NIGHT, rel, fx=450 / w, fy=300 / h, size=256)
    assert (cf["x0"], cf["y0"]) == (c["x0"], c["y0"])
    assert sv.crop(cfg, NIGHT, "LIGHT/none.fits", x=1, y=1) is None


def test_crop_stretch_matches_the_preview(tmp_path):
    from photonscript.scheduler import runs
    from photonscript.scheduler import sub_viewer as sv
    data = _noise_frame(h=1600, w=2400)
    cfg, rel = _setup(tmp_path, data)
    src = Path(cfg.image_watch_dir) / NIGHT / rel
    _, binned = runs._load_binned(src)
    plo, phi = runs._stretch_points(runs._decimate(binned))
    sv.crop(cfg, NIGHT, rel, x=10, y=10)
    info = next(iter(sv._frame_cache.values()))
    span = phi - plo
    assert info["lo"] == pytest.approx(plo, abs=0.05 * span)
    assert info["hi"] == pytest.approx(phi, abs=0.05 * span)


def test_two_to_one_is_exact_nearest_neighbor(tmp_path):
    from photonscript.scheduler import sub_viewer as sv
    cfg, rel = _setup(tmp_path, _noise_frame())
    c2 = sv.crop(cfg, NIGHT, rel, x=400, y=250, size=256, scale=2)
    c1 = sv.crop(cfg, NIGHT, rel, x=400, y=250, size=128, scale=1)
    assert c2["n"] == 128 and (c2["x0"], c2["y0"]) == (c1["x0"], c1["y0"])
    a2, a1 = _png_array(c2["png"]), _png_array(c1["png"])
    assert a2.shape == (256, 256)
    assert np.array_equal(a2, np.repeat(np.repeat(a1, 2, axis=0), 2, axis=1))


def test_osc_superpixel_on_a_synthetic_rggb_frame(tmp_path):
    from photonscript.scheduler import sub_viewer as sv
    h, w = 64, 96
    d = np.zeros((h, w))
    d[0::2, 0::2], d[0::2, 1::2], d[1::2, 0::2], d[1::2, 1::2] = 1000, 2000, 2200, 4000
    d[20:22, 30:32] += 800                       # one bright superpixel
    info = {"bscale": 1.0, "bzero": 0.0, "osc": True, "lo": 0.0}
    win = sv._window(d.astype(np.uint16), info, 28, 18, 8)
    assert win.shape == (8, 8)
    assert np.all(win[:2, :2] == 2300.0)         # (1000+2000+2200+4000) / 4
    assert np.all(win[2:4, 2:4] == 2300.0 + 800)  # the bright one, 2x2 px
    assert sv._clamp_origin(31.0, 8, w, even=True) % 2 == 0
    cfg, rel = _setup(tmp_path, d, rig="piggyback", BAYERPAT="RGGB")
    c = sv.crop(cfg, NIGHT, rel, x=31, y=21, size=128)
    assert c["osc"] is True and c["x0"] % 2 == 0 and c["y0"] % 2 == 0
    a = _png_array(c["png"])
    assert np.array_equal(a[0::2, 0::2], a[1::2, 1::2])   # 2x2 blocks


def test_crop_endpoint_headers_404_and_busy(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    from fastapi.testclient import TestClient

    from photonscript.scheduler import sub_viewer as sv
    cfg, rel = _setup(tmp_path, _noise_frame())
    monkeypatch.setattr(app, "_config", cfg)
    client = TestClient(app.app)
    r = client.get(f"/api/runs/{NIGHT}/crop",
                   params={"file": rel, "fx": 0.5, "fy": 0.5, "scale": 2})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert r.headers["X-Crop-N"] == "128" and r.headers["X-Crop-Scale"] == "2"
    assert r.headers["X-Frame-W"] == "900" and r.headers["X-Crop-OSC"] == "0"
    assert client.get(f"/api/runs/{NIGHT}/crop",
                      params={"file": "LIGHT/no.fits", "x": 1, "y": 1}).status_code == 404

    def busy(*a, **k):
        raise sv.Busy("x")
    monkeypatch.setattr(sv, "crop", busy)
    assert client.get(f"/api/runs/{NIGHT}/crop",
                      params={"file": rel, "x": 1, "y": 1}).status_code == 503
