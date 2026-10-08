"""PS-177: star QA and stacking per exposure group; PS-178: the integrate
calibration matcher explains alternatives and refuses a length with no dark.

The 2026-10-08 M31 RC16 dry run: 63 lights (B 30s x5, B 300s x1, G 30s x3,
L 30s x42, L 300s x6, R 30s x5, R 300s x1) were judged against ONE L 300 s
reference and 54 were flagged; no 300 s darks and no matching bias."""
import math
from pathlib import Path

import numpy as np
import pytest

from photonscript.integration import pipeline as pl
from photonscript.integration import star_qa as q
from photonscript.integration.frames import Frame

W, H = 3000, 2000


def _field(n, seed):
    rng = np.random.default_rng(seed)
    return np.column_stack([rng.uniform(40, W - 40, n), rng.uniform(40, H - 40, n)])


def _row(i, xy, filt, exp, stars=None):
    return {"file": f"{filt}_{exp:g}_{i:03d}.fits", "filter": filt, "exp": exp, "time": f"T{i:03d}",
            "stars": stars or len(xy), "hfd_px": 3.5, "ecc": 0.45, "sky_adu": 1000.0,
            "double_frac": 0.0, "double_vec": "", "theta_R": 0.1, "xbin": 1, "readout": "HCG",
            "_xy": np.asarray(xy, np.float32), "_shape": (W, H)}


# ----------------------------------------------------------- star QA groups

def test_each_exposure_group_gets_its_own_reference_and_is_kept():
    deep = _field(900, seed=1)
    rows = [_row(i, deep + np.random.default_rng(i).normal(0, 0.2, deep.shape), "L", 300.0)
            for i in range(6)]
    # the 30 s subs: a field the 300 s reference cannot register (the old
    # single-reference QA called every one "not_registered"), few stars
    shallow = _field(120, seed=2)
    rows += [_row(10 + i, shallow + np.random.default_rng(50 + i).normal(0, 0.2, shallow.shape),
                  "L", 30.0) for i in range(12)]
    run_ref = q.assess(rows, q.Thresholds())
    assert rows[run_ref]["exp"] == 300.0 and rows[run_ref]["is_run_reference"]
    assert all(r["action"] == "keep" for r in rows), [r["reason"] for r in rows if r["action"] != "keep"]
    refs = {r["group"]: r["group_reference"] for r in rows}
    assert set(refs) == {"L 300 s HCG", "L 30 s HCG"}
    assert refs["L 30 s HCG"].startswith("L_30_") and refs["L 300 s HCG"].startswith("L_300_")
    assert sum(r["is_reference"] for r in rows) == 2


def test_groups_split_by_filter_binning_and_readout():
    a = {"filter": "L", "exp": 30.0, "xbin": 1, "readout": "HCG"}
    assert q.group_key(a) != q.group_key(dict(a, xbin=2))
    assert q.group_key(a) != q.group_key(dict(a, readout="LCG"))
    assert q.group_key(a) != q.group_key(dict(a, filter="R"))
    assert q.group_key({"filter": "L", "exp": 30.0}) == ("L", 30.0, 1, "")
    assert q.group_label(("L", 30.0, 2, "HCG")) == "L 30 s bin2 HCG"


def test_a_bad_sub_is_still_caught_inside_its_group():
    base = _field(500, seed=11)
    rows = [_row(i, base + np.random.default_rng(i).normal(0, 0.2, base.shape), "R", 30.0)
            for i in range(9)]
    rows[4]["stars"] = 100                         # clouded: low stars vs its 30 s neighbors
    q.assess(rows, q.Thresholds())
    assert rows[4]["action"] == "reject" and rows[4]["reason"].startswith("low_stars")
    assert sum(r["action"] == "reject" for r in rows) == 1


def test_group_summary_counts_and_orders_longest_first():
    rows = [{"file": "a", "filter": "L", "exp": 30.0, "action": "keep", "reason": "stars_ok",
             "group": "L 30 s", "group_reference": "a"},
            {"file": "b", "filter": "L", "exp": 30.0, "action": "reject",
             "reason": "soft(hfd 5 vs nb 3 px);trailed(ecc 0.8)", "group": "L 30 s",
             "group_reference": "a"},
            {"file": "c", "filter": "L", "exp": 300.0, "action": "keep", "reason": "stars_ok",
             "group": "L 300 s", "group_reference": "c"}]
    g = q.group_summary(rows)
    assert [x["group"] for x in g] == ["L 300 s", "L 30 s"]
    assert g[1] == {**g[1], "subs": 2, "keep": 1, "reject": 1,
                    "reasons": {"soft": 1, "trailed": 1}}


def test_csv_has_group_columns(tmp_path):
    rows = [{"file": "a", "filter": "L", "exp": 30.0, "action": "keep", "reason": "stars_ok",
             "group": "L 30 s", "group_reference": "a", "is_reference": True,
             "is_run_reference": False, "stars": 10, "hfd_px": math.nan}]
    q.write_csv(tmp_path / "x.csv", rows)
    head, line = (tmp_path / "x.csv").read_text().splitlines()
    cols = head.split(",")
    assert cols[7:11] == ["group", "group_reference", "is_reference", "is_run_reference"]
    assert line.split(",")[7] == "L 30 s"


# ----------------------------------------------------------- stacks per exposure

def _fr(name, filt, exp, osc=False):
    return Frame(path=Path(name), exp=exp, filter=filt, bayer="RGGB" if osc else "")


def _m31_lights():
    out = []
    for filt, exp, n in (("B", 30, 5), ("B", 300, 1), ("G", 30, 3), ("L", 30, 42),
                         ("L", 300, 6), ("R", 30, 5), ("R", 300, 1)):
        out += [_fr(f"{filt}_{exp}_{i}.fits", filt, float(exp)) for i in range(n)]
    return out


def test_plan_stacks_m31_one_length_per_stack():
    specs, left, notes = pl.plan_stacks(_m31_lights(), min_group=3)
    got = {s["name"]: (s["filter"], s["exps"]) for s in specs}
    assert got == {"B": ("B", [30.0]), "G": ("G", [30.0]), "L": ("L", [300.0]),
                   "L_30s": ("L", [30.0]), "R": ("R", [30.0])}
    assert sorted(f.name for f, _ in left) == ["B_300_0.fits", "R_300_0.fits"]
    assert any("fewer than 3" in n and n.startswith("left out B 300 s") for n in notes)
    assert any(n.startswith("L: 300 s x 6 -> master_L, 30 s x 42 -> master_L_30s") for n in notes)


def test_plan_stacks_keeps_osc_exposures_together():
    fs = [_fr(f"a{i}.fits", "OSC", 120.0, osc=True) for i in range(3)]
    fs += [_fr("b.fits", "OSC", 400.0, osc=True)]
    specs, left, _ = pl.plan_stacks(fs, min_group=3)
    assert specs == [{"name": "OSC", "filter": "OSC", "exps": [120.0, 400.0]}] and not left


def test_plan_stacks_primary_is_most_integration_time():
    fs = [_fr(f"s{i}.fits", "Ha", 60.0) for i in range(20)]       # 1200 s
    fs += [_fr(f"l{i}.fits", "Ha", 600.0) for i in range(3)]      # 1800 s
    specs, _, _ = pl.plan_stacks(fs)
    assert [s["name"] for s in specs] == ["Ha", "Ha_60s"]


def test_pjsr_never_integrates_mixed_lengths_with_equal_weights():
    from photonscript.integration import pjsr
    t = pjsr.template(pjsr.INTEGRATE_TEMPLATE)
    assert "stack.mixed_exposures ? ImageIntegration.prototype.ExposureTimeWeight" in t
    t.encode("ascii")


# ----------------------------------------------------------- mono hot pixels

def test_mono_detect_ignores_hot_pixels():
    """PS-177: raw RC16 subs gave sep the hot pixels (HFD 0.8 px, 4000 cap),
    so every sub registered on the sensor pattern. Mono frames are 3x3
    median filtered first; the stars survive, single hot pixels do not."""
    pytest.importorskip("sep")
    rng = np.random.default_rng(3)
    img = rng.normal(300.0, 5.0, (600, 800)).astype(np.float32)
    yy, xx = np.mgrid[0:600, 0:800]
    stars = [(100 + 60 * i, 80 + 45 * j) for i in range(10) for j in range(10)]
    for x, y in stars:
        img += 3000.0 * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * 3.0 ** 2))
    hx, hy = rng.integers(5, 795, 3000), rng.integers(5, 595, 3000)
    img[hy, hx] = 20000.0                                  # isolated hot pixels
    st = q.detect(img, osc=False)
    assert 90 <= len(st["x"]) <= 110
    assert np.median(st["hfd"]) > 3.0
