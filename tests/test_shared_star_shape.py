"""PS-94: one eccentricity formula (sqrt(1-(b/a)^2)) and one star-shape
measure at native and 2x2-binned scale (shared.star_shape).

Synthetic frames: Gaussian stars, FWHM 8 px (the RC16 at 0.24"/px in 2"
seeing), axis ratio b/a 1.0 / 0.9 / 0.8 / 0.7 at random angles, plus 2000
hot pixels. Measured on these frames, the binned measure reads elongated
stars 0.04 to 0.06 ROUNDER than truth and than the native measure (the
same stars: sep's isophotal moments at sigma 1.7 binned px plus the 3x3
median). That is a measurement offset, not stackability; it is why the gate
stays native until the ecc-scale report has real nights.
"""

import math

import numpy as np
import pytest

from photonscript.shared import star_shape as ss


def _need_sep():
    try:
        import sep  # noqa: F401
    except ImportError:
        pytest.importorskip("sep_pjw")


def _frame(q, seed=0, n=10, size=700, fwhm=8.0, hot=2000, theta=None):
    rng = np.random.default_rng(seed)
    d = rng.normal(600, 9, (size, size)).astype(np.float32)
    s = fwhm / 2.3548
    sa, sb = s / math.sqrt(q), s * math.sqrt(q)
    yy, xx = np.mgrid[-15:16, -15:16]
    step = (size - 60) // n
    for i in range(n):
        for j in range(n):
            th = rng.uniform(0, math.pi) if theta is None else theta
            cy = 40 + i * step + rng.uniform(0, 1)
            cx = 40 + j * step + rng.uniform(0, 1)
            iy, ix = int(cy), int(cx)
            x, y = xx - (cx - ix), yy - (cy - iy)
            u = x * math.cos(th) + y * math.sin(th)
            v = -x * math.sin(th) + y * math.cos(th)
            g = np.exp(-0.5 * (u ** 2 / sa ** 2 + v ** 2 / sb ** 2))
            d[iy - 15:iy + 16, ix - 15:ix + 16] += (
                rng.uniform(3000, 9000) * g).astype(np.float32)
    hy, hx = rng.integers(0, size, hot), rng.integers(0, size, hot)
    d[hy, hx] += rng.uniform(2000, 20000, hot).astype(np.float32)
    return d


# ------------------------------------------------------------- formula

def test_lin_to_sqrt_known_values():
    assert ss.lin_to_sqrt(0.20) == pytest.approx(0.60)
    assert ss.lin_to_sqrt(0.25) == pytest.approx(0.6614, abs=1e-4)
    assert ss.lin_to_sqrt(0.30) == pytest.approx(0.7141, abs=1e-4)
    assert ss.lin_to_sqrt(0.0) == 0.0 and ss.lin_to_sqrt(1.0) == 1.0
    assert ss.lin_to_sqrt(None) is None
    arr = ss.lin_to_sqrt(np.array([0.0, 0.2, 1.0]))
    assert np.allclose(arr, [0.0, 0.6, 1.0])


def test_lin_to_sqrt_is_monotonic_so_a_median_converts_exactly():
    rng = np.random.default_rng(3)
    lin = rng.uniform(0, 0.6, 101)
    assert ss.lin_to_sqrt(float(np.median(lin))) == pytest.approx(
        float(np.median(ss.lin_to_sqrt(lin))))


def test_ecc_sqrt_matches_lin_form_through_lin_to_sqrt():
    a, b = np.array([4.0, 3.0, 2.0, 0.0]), np.array([4.0, 2.4, 1.0, 0.0])
    e = ss.ecc_sqrt(a, b)
    assert np.allclose(e[:3], ss.lin_to_sqrt(1.0 - b[:3] / a[:3]))
    assert np.isnan(e[3])
    assert ss.ecc_sqrt(5.0, 3.0) == pytest.approx(0.8)
    assert ss.ecc_sqrt(0.0, 1.0) == 0.0


def test_to_sqrt_by_ecc_def():
    assert ss.to_sqrt(0.2, "1-b/a") == pytest.approx(0.6)
    assert ss.to_sqrt(0.2, " 1 - b/a ") == pytest.approx(0.6)
    assert ss.to_sqrt(0.6, ss.ECC_DEF) == 0.6
    assert ss.to_sqrt(0.6, None) == 0.6
    assert ss.to_sqrt(None, "1-b/a") is None


def test_bin2x2_mean_on_uint16_and_odd_shape():
    a = np.arange(5 * 7, dtype=np.uint16).reshape(5, 7) * 1000
    b = ss.bin2x2_mean(a)
    assert b.shape == (2, 3) and b.dtype == np.float32
    assert b[0, 0] == pytest.approx(a[0:2, 0:2].mean())
    assert b[1, 2] == pytest.approx(a[2:4, 4:6].mean())


# ------------------------------------------------------- measured shapes

@pytest.mark.parametrize("q", [0.9, 0.8, 0.7])
def test_native_and_binned_track_the_true_shape(q):
    _need_sep()
    truth = math.sqrt(1 - q * q)
    d = _frame(q)
    nat = ss.measure(d, binned=False)
    binned = ss.measure(ss.bin2x2_mean(d), binned=True)
    assert nat["n"] >= 90 and binned["n"] >= 90      # hot pixels rejected
    assert abs(nat["ecc"] - truth) < 0.03
    # binned reads rounder than truth (see the module docstring)
    assert -0.07 < binned["ecc"] - truth < 0.0
    assert binned["ecc"] < nat["ecc"]
    # sizes come back in native px at both scales
    assert nat["fwhm_px"] == pytest.approx(binned["fwhm_px"], rel=0.15)
    assert nat["hfr_px"] == pytest.approx(binned["hfr_px"], rel=0.15)
    assert max(binned["stars"]["x"]) > 600           # native coordinates


def test_round_stars_read_round_at_both_scales():
    _need_sep()
    d = _frame(1.0)
    assert ss.measure(d, False)["ecc"] < 0.10
    assert ss.measure(ss.bin2x2_mean(d), True)["ecc"] < 0.15


def test_theta_survives_binning():
    _need_sep()
    d = _frame(0.7, theta=0.6)
    nat = ss.measure(d, False)["stars"]["theta"]
    binned = ss.measure(ss.bin2x2_mean(d), True)["stars"]["theta"]
    assert float(np.median(nat)) == pytest.approx(0.6, abs=0.03)
    assert float(np.median(binned)) == pytest.approx(0.6, abs=0.03)


def test_native_measure_matches_the_live_grader():
    """star_shape.measure(native) is the live _detect_stars pipeline, so a
    backfill native ecc equals what the live watcher records."""
    _need_sep()
    from photonscript.telescope_agent.image_validator import _detect_stars
    d = _frame(0.8, seed=5)
    live = _detect_stars(d, 0.0, 1.0)
    m = ss.measure(d, False)
    assert m["n"] == len(live)
    assert m["ecc"] == pytest.approx(
        float(np.median([s["eccentricity"] for s in live])), abs=1e-6)
    assert m["hfr_px"] == pytest.approx(
        float(np.median([s["hfr"] for s in live])), abs=1e-3)


def test_empty_frame():
    _need_sep()
    rng = np.random.default_rng(0)
    m = ss.measure(rng.normal(600, 9, (200, 200)).astype(np.float32))
    assert m["n"] == 0 and m["ecc"] is None and m["stars"] is None
