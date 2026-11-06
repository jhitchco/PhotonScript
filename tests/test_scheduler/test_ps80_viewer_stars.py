"""PS-80: star overlay data and the viewer script.

/stars serves the stored sidecar with ecc in sqrt form and the record's rig
and measures a legacy sub once on view (medians from the sidecar equal the
grader's); the runs page loads viewer.js, which is ASCII with no raw
newline in a string and leaves the PS-24 verdict keys alone. Shared
helpers for the PS-6 and PS-17 tests live here.
"""
import io
import re
from pathlib import Path

import numpy as np
import pytest

from tests.test_dashboard_js_strings import _raw_newline_strings
from tests.test_scheduler.test_ps21_grading import (NIGHT, _cfg, _need_sep,
                                                    _rec, _seed, _write_light)

ROOT = Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
RUNS = ROOT / "templates" / "runs.html"
VIEWER = ROOT / "static" / "js" / "viewer.js"


@pytest.fixture(autouse=True)
def _fresh_caches():
    from photonscript.scheduler import sub_viewer
    sub_viewer.clear_caches()
    yield
    sub_viewer.clear_caches()


def _write_frame(path: Path, data: np.ndarray, **hdr):
    from astropy.io import fits
    h = fits.Header()
    h["IMAGETYP"] = "LIGHT"
    h["EXPTIME"] = 300.0
    for k, v in hdr.items():
        h[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(data.astype(np.uint16), header=h).writeto(path, overwrite=True)
    return path


def _noise_frame(h=600, w=900, seed=3):
    rng = np.random.default_rng(seed)
    d = rng.normal(800, 20, (h, w))
    yy, xx = np.mgrid[0:h, 0:w]
    for cy, cx in [(50, 60), (300, 450), (550, 850), (120, 700), (480, 100)]:
        d += 9000 * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 2.5 ** 2))
    return np.clip(d, 0, 65000)


def _png_array(b: bytes) -> np.ndarray:
    from PIL import Image
    return np.asarray(Image.open(io.BytesIO(b)))


def _setup(tmp_path, data, name="x", rig="rc16", **hdr):
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    rel = f"LIGHT/{name}.fits"
    _write_frame(Path(cfg.image_watch_dir) / NIGHT / rel, data, **hdr)
    _seed(cfg, [_rec(name, rig=rig)])
    return cfg, rel


# --------------------------------------------------------- PS-80 /stars

def test_stars_stored_sidecar_ecc_sqrt_and_record_rig(tmp_path):
    from photonscript.scheduler.sub_viewer import stars_for_view
    from photonscript.shared import star_table
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    _seed(cfg, [_rec("p", rig="piggyback")])
    t = star_table.build([10, 20], [30, 40], [2.0, 3.0], [0.2, 0.3],
                         theta=[0.1, 0.2], w=100, h=80, rig="piggyback",
                         grader="sep-binned")
    star_table.write(cfg, NIGHT, "LIGHT/p.fits", t, rig="piggyback")
    out = stars_for_view(cfg, NIGHT, "LIGHT/p.fits")      # rig from the record
    assert out is not None and out["rig"] == "piggyback"
    # an old backfill sidecar holds 1-b/a: 0.20 -> 0.60, 0.30 -> 0.71
    assert out["ecc"] == [pytest.approx(0.6, abs=1e-3), pytest.approx(0.714, abs=1e-3)]
    assert out["ecc_def"] == "sqrt(1-(b/a)^2)" and out["on_view"] is False
    assert stars_for_view(cfg, NIGHT, "LIGHT/p.fits", rig="rc16") is None
    # no FITS: nothing to measure, still None
    assert stars_for_view(cfg, NIGHT, "LIGHT/none.fits") is None


def test_stars_measured_once_on_view_match_the_grader(tmp_path, monkeypatch):
    _need_sep()
    from photonscript.scheduler import runs
    from photonscript.scheduler.sub_viewer import stars_for_view
    from photonscript.shared import star_measure, star_table
    from photonscript.shared.rigs import rig_config
    cfg = _cfg(tmp_path)
    rel = "LIGHT/legacy.fits"
    src = _write_light(Path(cfg.image_watch_dir) / NIGHT / rel)
    _seed(cfg, [_rec("legacy")])
    assert star_table.read(cfg, NIGHT, rel) is None
    calls = []
    real = runs._measure_native
    monkeypatch.setattr(runs, "_measure_native",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    t = stars_for_view(cfg, NIGHT, rel)
    assert t is not None and t["on_view"] is True and t["n"] > 20
    assert star_table.read(cfg, NIGHT, rel)["on_view"] is True   # cached
    assert stars_for_view(cfg, NIGHT, rel)["n"] == t["n"]
    assert len(calls) == 1                                        # measured once
    # the medians the grader records come from these stars
    m = star_measure.measure_frame(star_measure.load_native(src),
                                   rig_config(cfg, "rc16"), "rc16")
    assert float(np.median([h for h in t["hfr"] if h])) == pytest.approx(m["hfr"], abs=0.02)
    assert float(np.median(t["ecc"])) == pytest.approx(m["ecc"], abs=0.005)
    assert stars_for_view(cfg, NIGHT, "LIGHT/legacy.fits", compute=False)["n"] == t["n"]


# ---------------------------------------------------------------- page

def test_runs_page_loads_viewer_js_and_it_is_clean():
    page = RUNS.read_text(encoding="utf-8")
    assert '<script src="/static/js/viewer.js?v={{ version }}"></script>' in page
    for el_id in ("lbImg", "lightbox", "lbBar"):
        assert page.count(f'id="{el_id}"') == 1, el_id
    js = VIEWER.read_text(encoding="utf-8")
    js.encode("ascii")                       # pure ASCII
    assert chr(0x2014) not in js             # no em dash
    assert _raw_newline_strings(js) == []
    for path in ("/stars?file=", "/crop?file=", "/mosaic-info", "/mosaic'",
                 "/api/qa/thresholds"):
        assert path in js, path
    # the PS-24 verdict keys stay free: the viewer uses S, C, L, M only
    keys = set(re.findall(r"k === '(\w)'", js))
    assert keys == {"s", "c", "l", "m"}
