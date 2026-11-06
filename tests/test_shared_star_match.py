"""PS-96: star-list registration from sidecars (shared.star_match)."""
import math

import numpy as np

from photonscript.shared.star_match import offset

W, H = 3000, 2000


def _field(n=300, seed=0):
    rng = np.random.default_rng(seed)
    return np.column_stack([rng.uniform(50, W - 50, n), rng.uniform(50, H - 50, n)])


def _tbl(pts):
    return {"x": [float(p[0]) for p in pts], "y": [float(p[1]) for p in pts],
            "w": W, "h": H}


def _move(pts, dx, dy, rot_deg=0.0):
    c = np.array([W / 2.0, H / 2.0])
    a = math.radians(rot_deg)
    r = np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])
    return (pts - c) @ r.T + c + np.array([dx, dy])


def test_known_shift():
    a = _field()
    res = offset(_tbl(a), _tbl(_move(a, 7.3, -12.6)))
    assert res is not None
    assert abs(res["dx"] - 7.3) < 0.05 and abs(res["dy"] + 12.6) < 0.05
    assert abs(res["rotation_deg"]) < 0.01
    assert res["n_matched"] >= 140


def test_shift_and_rotation():
    a = _field(seed=1)
    res = offset(_tbl(a), _tbl(_move(a, -20.0, 4.0, rot_deg=0.05)))
    assert abs(res["dx"] + 20.0) < 0.1 and abs(res["dy"] - 4.0) < 0.1
    assert abs(res["rotation_deg"] - 0.05) < 0.005


def test_dropped_stars_and_noise():
    rng = np.random.default_rng(3)
    a = _field(seed=2)
    b = _move(a, 3.0, 5.0) + rng.normal(0, 0.3, a.shape)
    keep_a = rng.random(len(a)) > 0.3
    keep_b = rng.random(len(a)) > 0.3
    # brightest-first order differs between subs: shuffle B
    bb = b[keep_b]
    rng.shuffle(bb)
    res = offset(_tbl(a[keep_a]), _tbl(bb))
    assert res is not None
    assert abs(res["dx"] - 3.0) < 0.15 and abs(res["dy"] - 5.0) < 0.15
    assert res["rms_px"] < 0.8


def test_unrelated_fields_do_not_match():
    assert offset(_tbl(_field(seed=4)), _tbl(_field(seed=5))) is None


def test_empty_tables():
    assert offset({}, _tbl(_field())) is None
    assert offset(_tbl(_field()[:3]), _tbl(_field())) is None
