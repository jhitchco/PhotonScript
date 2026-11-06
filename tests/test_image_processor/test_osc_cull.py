"""Tests for the piggyback OSC pre-integration cull (split pointing, duplicates)."""

from pathlib import Path

import numpy as np
import pytest

from photonscript.image_processor import osc_cull as oc

H, W = 1024, 1536        # native px (small, but the same pipeline as a 26 MP sub)
SLEW = (300, 420)        # native px, the "other pointing" for split subs


def _sky(rng: np.random.Generator, n_stars: int = 6000) -> np.ndarray:
    """A star field twice the frame size so shifted views stay filled."""
    big = np.zeros((H * 2, W * 2), np.float32)
    ys = rng.integers(6, H * 2 - 6, n_stars)
    xs = rng.integers(6, W * 2 - 6, n_stars)
    amp = rng.pareto(2.5, n_stars).astype(np.float32) * 400 + 100
    yy, xx = np.mgrid[-6:7, -6:7]
    psf = np.exp(-(yy ** 2 + xx ** 2) / (2 * 1.3 ** 2)).astype(np.float32)  # FWHM ~3 px
    for y, x, a in zip(ys, xs, amp):
        big[y - 6:y + 7, x - 6:x + 7] += a * psf
    return big


def _view(big: np.ndarray, oy: int, ox: int) -> np.ndarray:
    y0, x0 = H // 2 + oy, W // 2 + ox
    return big[y0:y0 + H, x0:x0 + W]


def _frame(rng, big, w_main=1.0, jitter=(0, 0)) -> np.ndarray:
    a = w_main * _view(big, *jitter)
    if w_main < 1.0:
        a = a + (1 - w_main) * _view(big, jitter[0] + SLEW[0], jitter[1] + SLEW[1])
    a = a + 300 + rng.normal(0, 4, a.shape).astype(np.float32)
    hot = rng.integers(0, H * W, 60)          # fixed-pattern hot pixels
    a.flat[hot] = 60000
    return np.clip(a, 0, 65535)


def _write_fits(path: Path, a: np.ndarray, date_obs: str) -> None:
    cards = [
        "SIMPLE  =                    T", "BITPIX  =                   16",
        "NAXIS   =                    2", f"NAXIS1  = {a.shape[1]:>20d}",
        f"NAXIS2  = {a.shape[0]:>20d}", "BZERO   =                32768",
        "BSCALE  =                    1", f"DATE-OBS= '{date_obs}'",
        "INSTRUME= 'AP26CC'", "END",
    ]
    hdr = "".join(c.ljust(80) for c in cards).encode("ascii")
    hdr += b" " * (-len(hdr) % 2880)
    data = (np.round(a).astype(np.int32) - 32768).astype(">i2").tobytes()
    data += b"\0" * (-len(data) % 2880)
    path.write_bytes(hdr + data)


@pytest.fixture
def stage(tmp_path: Path) -> Path:
    rng = np.random.default_rng(7)
    big = _sky(rng)
    plan = [1.0] * 10 + [0.5, 0.7, 0.35, 0.85]     # 10 clean, 4 split
    for i, w in enumerate(plan):
        a = _frame(rng, big, w, jitter=(int(rng.integers(-3, 4)), int(rng.integers(-3, 4))))
        _write_fits(tmp_path / f"sub_{i:03d}.fits", a, f"2026-09-21T09:{i:02d}:00")
    # an exact duplicate with NINA's "_1" suffix
    (tmp_path / "sub_000_1.fits").write_bytes((tmp_path / "sub_000.fits").read_bytes())
    return tmp_path


def test_read_fits_roundtrip(tmp_path):
    a = np.arange(40, dtype=np.float32).reshape(5, 8) * 100
    _write_fits(tmp_path / "x.fits", a, "2026-01-01T00:00:00")
    hdr, b = oc.read_fits(tmp_path / "x.fits")
    assert hdr["DATE-OBS"].startswith("2026-01-01")
    np.testing.assert_allclose(a, b)


def test_box_blur_matches_naive():
    rng = np.random.default_rng(1)
    a = rng.random((20, 30))
    r = 2
    p = np.pad(a, r, mode="edge")
    naive = np.array([[p[y:y + 5, x:x + 5].mean() for x in range(30)] for y in range(20)])
    np.testing.assert_allclose(oc.box_blur(a, r), naive, rtol=1e-9)


def test_split_and_duplicate_detection(stage):
    subs = oc.cull(stage, dry_run=True, log=lambda *_: None)
    got = {s.path.name: s.reasons for s in subs}
    for i in range(10, 14):
        assert "split_pointing" in got[f"sub_{i:03d}.fits"], got[f"sub_{i:03d}.fits"]
    for i in range(10):
        assert "split_pointing" not in got[f"sub_{i:03d}.fits"]
    assert got["sub_000_1.fits"] == ["duplicate"]
    assert got["sub_000.fits"] == []
    # the split lag is the slew vector (dx, dy) = (SLEW[1], SLEW[0]); sign is ambiguous
    s = next(s for s in subs if s.path.name == "sub_010.fits")
    assert abs(abs(s.split.dx) - SLEW[1]) <= 8 and abs(abs(s.split.dy) - SLEW[0]) <= 8


def test_cull_moves_not_deletes_and_writes_report(stage):
    n_before = len(list(stage.glob("*.fits")))
    oc.cull(stage, log=lambda *_: None)
    kept = list(stage.glob("*.fits"))
    moved = list((stage / "REJECTED").rglob("*.fits"))
    assert len(kept) == 10 and len(moved) == 5 and len(kept) + len(moved) == n_before
    assert (stage / "cull_report.csv").read_text().count("\n") == n_before + 1
    ref = (stage / "reference.txt").read_text().strip()
    assert (stage / ref).exists()


def test_dry_run_moves_nothing(stage):
    n_before = len(list(stage.glob("*.fits")))
    oc.cull(stage, dry_run=True, log=lambda *_: None)
    assert len(list(stage.glob("*.fits"))) == n_before
    assert not (stage / "REJECTED").exists()


def test_second_pointing_is_separated_not_called_split(tmp_path):
    """A minority framing (mount parked at a second target) must be judged
    against its own star-field baseline: clean subs there go to
    other_pointing_1, not split_pointing (2026-09-21 M31 regression)."""
    rng = np.random.default_rng(11)
    big = _sky(rng)
    plan = [("A", 1.0)] * 9 + [("B", 1.0)] * 7 + [("A", 0.5)] * 2
    for i, (where, w) in enumerate(plan):
        jit = (int(rng.integers(-3, 4)), int(rng.integers(-3, 4)))
        if where == "B":
            jit = (jit[0] + SLEW[0], jit[1] + SLEW[1])
        a = _frame(rng, big, w, jitter=jit)
        _write_fits(tmp_path / f"sub_{i:03d}.fits", a, f"2026-09-21T09:{i:02d}:00")
    subs = oc.cull(tmp_path, dry_run=True, log=lambda *_: None)
    got = {s.path.name: s.reasons for s in subs}
    for i in range(9):
        assert got[f"sub_{i:03d}.fits"] == [], (i, got[f"sub_{i:03d}.fits"])
    for i in range(9, 16):
        assert got[f"sub_{i:03d}.fits"] == ["other_pointing_1"], (i, got[f"sub_{i:03d}.fits"])
    for i in (16, 17):
        assert "split_pointing" in got[f"sub_{i:03d}.fits"]
