"""PS-17: 3x3 corner / edge / center mosaic.

Tile origins are exact (6248 x 4176 and small frames, even for OSC); the PNG
is the nine windows with gutters and is cached on disk; per-tile readouts
match runs._shape_diagnostics zones; mosaic-info falls back from the star
sidecar to corner_ecc to nothing; 503 when busy.
"""
from pathlib import Path

import numpy as np
import pytest

from tests.test_scheduler.test_ps21_grading import NIGHT
from tests.test_scheduler.test_ps80_viewer_stars import (  # noqa: F401
    _fresh_caches, _noise_frame, _png_array, _setup)


# ---------------------------------------------------------- PS-17 3x3

def test_tile_origins_exact():
    from photonscript.scheduler.sub_viewer import tile_origins
    o = tile_origins(6248, 4176, 256)
    assert o == [(0, 0), (2996, 0), (5992, 0),
                 (0, 1960), (2996, 1960), (5992, 1960),
                 (0, 3920), (2996, 3920), (5992, 3920)]
    assert tile_origins(6248, 4176, 128)[4] == (3060, 2024)
    assert tile_origins(100, 80, 128) == [(0, 0)] * 9          # clamped
    o = tile_origins(301, 201, 64, even=True)
    assert all(x % 2 == 0 and y % 2 == 0 for x, y in o)
    assert max(x for x, _ in o) + 64 <= 301 and max(y for _, y in o) + 64 <= 201


def test_mosaic_is_the_nine_crops_with_gutters_and_cached(tmp_path, monkeypatch):
    from photonscript.scheduler import runs
    from photonscript.scheduler import sub_viewer as sv
    cfg, rel = _setup(tmp_path, _noise_frame())
    p = sv.mosaic(cfg, NIGHT, rel, size=128)
    from PIL import Image
    a = np.asarray(Image.open(p))
    assert a.shape == (3 * 128 + 4, 3 * 128 + 4)
    assert np.all(a[128:130, :] == sv.GUTTER_GRAY)
    from astropy.io import fits
    raw = fits.getdata(Path(cfg.image_watch_dir) / NIGHT / rel)
    info = next(iter(sv._frame_cache.values()))
    for i, (x0, y0) in enumerate(sv.tile_origins(900, 600, 128)):
        r, c = divmod(i, 3)
        tile = a[r * 130:r * 130 + 128, c * 130:c * 130 + 128]
        want = sv.to_u8(raw[y0:y0 + 128, x0:x0 + 128].astype(np.float32),
                        info["lo"], info["hi"])
        assert np.array_equal(tile, want), sv.TILE_LABELS[i]
    monkeypatch.setattr(runs, "_sub_source", lambda *a: pytest.fail("reopened"))
    assert sv.mosaic(cfg, NIGHT, rel, size=128) == p             # disk cache
    assert p.parent == Path(cfg.data_dir) / "thumbs" / NIGHT


def test_zone_readouts_match_shape_diagnostics(tmp_path):
    from photonscript.scheduler.runs import _shape_diagnostics
    from photonscript.scheduler.sub_viewer import zone_stats
    from photonscript.shared.star_shape import ecc_sqrt
    rng = np.random.default_rng(7)
    W, H, n = 900, 600, 400
    objs = np.zeros(n, dtype=[("x", "f8"), ("y", "f8"), ("a", "f8"),
                              ("b", "f8"), ("theta", "f8")])
    objs["x"], objs["y"] = rng.uniform(0, W, n), rng.uniform(0, H, n)
    objs["a"] = rng.uniform(1.0, 3.0, n)
    objs["b"] = objs["a"] * rng.uniform(0.5, 1.0, n)
    objs["theta"] = rng.uniform(-1.5, 1.5, n)
    diag = _shape_diagnostics(objs, W, H)["corner_ecc"]
    table = {"w": W, "h": H, "x": list(objs["x"]), "y": list(objs["y"]),
             "hfr": list(objs["a"]), "ecc": list(ecc_sqrt(objs["a"], objs["b"]))}
    z = {d["zone"]: d for d in zone_stats(table)}
    for k in ("TL", "TR", "BL", "BR", "C"):
        assert z[k]["ecc"] == pytest.approx(diag[k], abs=0.006), k
    assert sum(d["n"] for d in z.values()) == n
    few = zone_stats({"w": W, "h": H, "x": [1.0] * 4, "y": [1.0] * 4,
                      "hfr": [2.0] * 4, "ecc": [0.3] * 4})
    assert few[0]["n"] == 4 and few[0]["ecc"] is None          # under 5 stars
    assert zone_stats(None) is None


def test_mosaic_info_sources(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    from fastapi.testclient import TestClient

    from photonscript.scheduler.runs import _rewrite_subs, _load_subs
    from photonscript.shared import star_table
    cfg, rel = _setup(tmp_path, _noise_frame())
    monkeypatch.setattr(app, "_config", cfg)
    client = TestClient(app.app)
    r = client.get(f"/api/runs/{NIGHT}/mosaic-info", params={"file": rel, "size": 256})
    j = r.json()
    assert r.status_code == 200 and j["source"] is None and j["zones"] is None
    assert (j["w"], j["h"], j["fits"]) == (900, 600, True)
    assert [t["zone"] for t in j["tiles"]] == ["TL", "T", "TR", "L", "C", "R", "BL", "B", "BR"]
    assert (j["tiles"][8]["x0"], j["tiles"][8]["y0"]) == (900 - 256, 600 - 256)
    # backfill corner_ecc fallback
    recs = _load_subs(cfg, NIGHT)
    recs[0]["corner_ecc"] = {"TL": 0.41, "TR": 0.52, "BL": None, "BR": 0.6, "C": 0.3}
    _rewrite_subs(cfg, NIGHT, recs)
    j = client.get(f"/api/runs/{NIGHT}/mosaic-info", params={"file": rel}).json()
    assert j["source"] == "corner_ecc" and j["zones"][0]["ecc"] == 0.41
    assert j["zones"][1]["ecc"] is None
    # the star sidecar wins
    xs = [50.0] * 6 + [450.0] * 6
    ys = [50.0] * 6 + [300.0] * 6
    star_table.write(cfg, NIGHT, rel, star_table.build(
        xs, ys, [2.0] * 6 + [4.0] * 6, [0.2] * 12, w=900, h=600), rig="rc16")
    j = client.get(f"/api/runs/{NIGHT}/mosaic-info", params={"file": rel}).json()
    assert j["source"] == "stars"
    assert j["zones"][0]["hfr"] == 2.0 and j["zones"][4]["hfr"] == 4.0
    assert j["zones"][2]["hfr"] is None
    assert client.get(f"/api/runs/{NIGHT}/mosaic-info",
                      params={"file": "LIGHT/no.fits"}).status_code == 404
    r = client.get(f"/api/runs/{NIGHT}/mosaic", params={"file": rel, "size": 128})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


def test_mosaic_endpoint_busy(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    from fastapi.testclient import TestClient

    from photonscript.scheduler import sub_viewer as sv
    cfg, rel = _setup(tmp_path, _noise_frame())
    monkeypatch.setattr(app, "_config", cfg)

    def busy(*a, **k):
        raise sv.Busy("x")
    monkeypatch.setattr(sv, "mosaic", busy)
    assert TestClient(app.app).get(f"/api/runs/{NIGHT}/mosaic",
                                   params={"file": rel}).status_code == 503
