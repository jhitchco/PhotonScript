"""PS-83: live and backfill grading measure stars with ONE function
(shared.star_measure.measure_frame), so the same FITS gets the same HFR,
FWHM, eccentricity, star count, background, swamp and saturation numbers
from both graders.

The parity tests run the real live path (TelescopeAgent._process_new_image
-> image_validator.validate_image) and the real backfill path
(runs._fast_grade) on the same file and assert equal results. Synthetic
frames always run; real frames run when PS_PARITY_FITS points at a folder
(or a file) of FITS lights, e.g.
    $env:PS_PARITY_FITS = "C:\\Users\\sleep\\ninashare\\Library\\Crescent Nebula\\Ha"
(read only; at most PS_PARITY_FITS_MAX files, default 3).
"""

import asyncio
import math
import os
from pathlib import Path

import numpy as np
import pytest

from photonscript.shared import qa_rules as q
from photonscript.shared import star_measure as sm
from photonscript.shared.config import PhotonScriptConfig

NIGHT = "2026-10-05"

# what both graders record from the shared measure
RECORD_KEYS = ("hfr", "fwhm_arcsec", "stars", "ecc", "ecc_bin", "hfr_bin",
               "background", "noise", "swamp", "clipped_pct",
               "sat_stars_pct", "exposure", "corner_spread", "ecc_def",
               "measure_v")
# scorecard rows fed only by the shared measure (+ the same thresholds)
CARD_ROWS = ("ecc", "ecc_bin", "hfr", "fwhm", "stars", "exposure")


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
                quality_eccentricity_max=0.6, pixel_scale_arcsec=0.24,
                stamp_fits_object=False, library_attribute=False,
                guard_enabled=False)
    base.update(kw)
    (tmp_path / "data").mkdir(exist_ok=True)
    return PhotonScriptConfig(**base)


def _field(q_ratio=0.8, size=520, fwhm=8.0, n=9, hot=1500, seed=3,
           sat_every=0, bayer=False):
    """Gaussian stars (FWHM px, axis ratio q_ratio, random angles), hot
    pixels, optional saturated cores and an RGGB response pattern."""
    rng = np.random.default_rng(seed)
    d = rng.normal(600, 9, (size, size)).astype(np.float64)
    s = fwhm / 2.3548
    sa, sb = s / math.sqrt(q_ratio), s * math.sqrt(q_ratio)
    yy, xx = np.mgrid[-15:16, -15:16]
    step = (size - 60) // n
    k = 0
    for i in range(n):
        for j in range(n):
            th = rng.uniform(0, math.pi)
            cy = 40 + i * step + rng.uniform(0, 1)
            cx = 40 + j * step + rng.uniform(0, 1)
            iy, ix = int(cy), int(cx)
            x, y = xx - (cx - ix), yy - (cy - iy)
            u = x * math.cos(th) + y * math.sin(th)
            v = -x * math.sin(th) + y * math.cos(th)
            g = np.exp(-0.5 * (u ** 2 / sa ** 2 + v ** 2 / sb ** 2))
            amp = rng.uniform(3000, 9000)
            if sat_every and k % sat_every == 0:
                amp = 90000.0
            d[iy - 15:iy + 16, ix - 15:ix + 16] += amp * g
            k += 1
    if bayer:
        resp = np.ones_like(d)
        resp[0::2, 0::2] = 0.45
        resp[1::2, 1::2] = 0.30
        d = 400 + (d - 600) * resp + 0.0
    hy, hx = rng.integers(0, size, hot), rng.integers(0, size, hot)
    d[hy, hx] += rng.uniform(2000, 20000, hot)
    return np.clip(d, 0, 65535).astype(np.uint16)


def _write(path: Path, data: np.ndarray, **hdr_kw) -> Path:
    from astropy.io import fits
    h = fits.Header()
    h["IMAGETYP"] = "LIGHT"
    h["EXPTIME"] = 300.0
    h["CCD-TEMP"] = 0.2
    h["SET-TEMP"] = 0.0
    h["FILTER"] = "Ha"
    h["OBJECT"] = "Test Nebula"
    h["DATE-OBS"] = "2026-10-06T04:00:00"
    for k, v in hdr_kw.items():
        h[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    # uint16 -> BITPIX 16 + BZERO 32768, NINA's format
    fits.PrimaryHDU(data, header=h).writeto(path, overwrite=True)
    return path


class _Bus:
    def __init__(self):
        self.msgs = []

    async def publish(self, msg):
        self.msgs.append(msg)


def _agent(cfg, rig="rc16"):
    from photonscript.shared.models import TelescopeState
    from photonscript.telescope_agent import agent as agent_mod
    a = agent_mod.TelescopeAgent.__new__(agent_mod.TelescopeAgent)
    a.config, a.rig, a.bus = cfg, rig, _Bus()
    a.state = TelescopeState()
    a.state.camera_temp_c = 0.2
    a._consecutive_rejects, a._alerted = 0, set()

    async def _esc(*_a, **_k):
        pass
    a._escalate = _esc
    return a


def _live(cfg, f):
    from photonscript.scheduler.runs import _load_subs
    asyncio.run(_agent(cfg)._process_new_image(f))
    (rec,) = [r for r in _load_subs(cfg, NIGHT)
              if Path(str(r.get("file"))).name == f.name]
    return rec


def _rows(rec):
    return {r[0]: r for r in rec["scorecard"]["rows"]}


def _assert_same(live: dict, back: dict):
    for k in RECORD_KEYS:
        assert live.get(k) == back.get(k), (k, live.get(k), back.get(k))


# sep's deblending assigns the pixels of a blend at random (SExtractor's
# Monte Carlo split, unseeded), so a frame with blended objects is not
# bit-reproducible: the SAME grader run six times on one real RC16 Ha sub
# gave corner_spread 0.084 to 0.097 and once moved the HFR by 0.01 px; on a
# real Piggy-600 sub ecc moved by 0.001. Parity on such frames is asserted
# to within that jitter; the synthetic frames above (no blends) are
# asserted bit-identical. The non-sep numbers (background, noise, swamp,
# clipping) are always exact.
JITTER_TOL = {"hfr": 0.1, "fwhm_arcsec": 0.1, "ecc": 0.01, "ecc_bin": 0.01,
              "hfr_bin": 0.1, "corner_spread": 0.05, "sat_stars_pct": 1.0,
              "background": 0, "noise": 0, "swamp": 0, "clipped_pct": 0}


def _assert_close(live: dict, back: dict):
    for k, tol in JITTER_TOL.items():
        if live.get(k) is None or back.get(k) is None:
            assert live.get(k) is back.get(k) is None, k
            continue
        assert live[k] == pytest.approx(back[k], abs=tol), k
    assert abs(live["stars"] - back["stars"]) <= max(2, 0.02 * back["stars"])
    assert live["exposure"] == back["exposure"]


_assert_close_osc = _assert_close


# ------------------------------------------------------------- parity

@pytest.mark.parametrize("kw", [
    {},                                  # elongated RC16 stars (b/a 0.8)
    {"q_ratio": 1.0, "seed": 7},         # round
    {"q_ratio": 0.6, "fwhm": 6.0},       # trailed
    {"sat_every": 3, "seed": 11},        # saturated cores
])
def test_live_and_backfill_record_the_same_numbers(tmp_path, kw):
    """The tentpole: same FITS through the real live path and the real
    backfill path gives identical measured fields and identical scorecard
    rows for every check the measure feeds."""
    _need_sep()
    from photonscript.scheduler.runs import _fast_grade
    cfg = _cfg(tmp_path)
    rel = "LIGHT/p_Ha_300s_0001.fits"
    f = _write(tmp_path / "fits" / NIGHT / rel, _field(**kw))
    live = _live(cfg, f)
    back = _fast_grade(f, cfg, [])
    _assert_same(live, back)
    lr, br = _rows(live), _rows(back)
    for cid in CARD_ROWS:
        assert lr[cid] == br[cid], cid
    assert back["ecc_at"] == "native"
    assert back["graded_by"] == "backfill-sep" and "graded_by" not in live


def test_star_sidecars_are_the_same_stars(tmp_path):
    _need_sep()
    from photonscript.scheduler.runs import _fast_grade
    from photonscript.shared import star_table
    cfg = _cfg(tmp_path)
    rel = "LIGHT/s_Ha_300s_0001.fits"
    f = _write(tmp_path / "fits" / NIGHT / rel, _field())
    _live(cfg, f)
    t_live = star_table.read(cfg, NIGHT, rel, "rc16")
    star_table.sidecar_path(cfg, NIGHT, rel, "rc16").unlink()
    _fast_grade(f, cfg, [], stars_to=(NIGHT, rel))
    t_back = star_table.read(cfg, NIGHT, rel, "rc16")
    assert t_live["grader"] == "live-sep" and t_back["grader"] == "backfill-sep"
    for k in ("w", "h", "n", "x", "y", "hfr", "ecc", "theta", "ecc_def"):
        assert t_live[k] == t_back[k], k


def test_piggyback_osc_frame_parity(tmp_path):
    """Piggy-600 one-shot-color: both graders take the PS-96 superpixel
    path with the rig's own pixel scale and record the same numbers."""
    _need_sep()
    from photonscript.scheduler.runs import _fast_grade
    from photonscript.shared.rigs import rig_config
    from photonscript.telescope_agent.image_validator import (image_metrics,
                                                              validate_image)
    cfg = _cfg(tmp_path)
    f = _write(tmp_path / "fits" / NIGHT / "LIGHT" / "o_300s_0001.fits",
               _field(q_ratio=1.0, fwhm=3.0, size=400, n=10, bayer=True, hot=0),
               BAYERPAT="RGGB")
    pcfg = rig_config(cfg, "piggyback")
    live = validate_image(str(f), pcfg, rig="piggyback")
    back = _fast_grade(f, cfg, [], rig="piggyback")
    lm = {**image_metrics(live), "hfr_bin": live.hfr_bin_px,
          "corner_spread": live.corner_spread, "noise": live.noise_adu}
    _assert_close_osc(lm, back)
    assert back["ecc"] < 0.3                # round stars read round
    assert live.star_table["grader"] == "live-sep-superpixel"
    assert lm["ecc_bin"] is None and back["ecc_bin"] is None   # RC16 only
    # the Piggy-600 scale (1.29"/px), not the RC16's, in both graders
    assert pcfg.pixel_scale_arcsec != cfg.pixel_scale_arcsec
    assert back["fwhm_arcsec"] == pytest.approx(
        live.fwhm_arcsec, abs=0.1) and back["fwhm_arcsec"] > 2.0


def test_bayerpat_on_the_rc16_takes_the_osc_path_in_both(tmp_path):
    _need_sep()
    from photonscript.scheduler.runs import _fast_grade
    cfg = _cfg(tmp_path)
    f = _write(tmp_path / "fits" / NIGHT / "LIGHT" / "b_Ha_300s_0001.fits",
               _field(q_ratio=1.0, fwhm=3.0, size=400, n=10, bayer=True, hot=0),
               BAYERPAT="RGGB")
    live = _live(cfg, f)
    back = _fast_grade(f, cfg, [])
    _assert_close_osc(live, back)
    assert live["ecc_bin"] is None and back["ecc_bin"] is None


# ---------------------------------------------------- the measure itself

def test_measure_reads_truth_and_both_loaders_agree(tmp_path):
    _need_sep()
    from photonscript.scheduler.runs import _load_native
    from photonscript.telescope_agent.image_validator import _load_image_data
    f = _write(tmp_path / "e.fits", _field(q_ratio=0.8))
    a, b = _load_image_data(str(f)), _load_native(f)
    assert a.dtype == b.dtype == np.float32 and np.array_equal(a, b)
    m = sm.measure_frame(a, _cfg(tmp_path))
    # b/a 0.8 -> sqrt(1-0.64) = 0.6; FWHM 8 px; HFR of a Gaussian ~ FWHM/2
    assert m["ecc"] == pytest.approx(0.6, abs=0.05)
    assert m["fwhm_px"] == pytest.approx(8.0, rel=0.25)
    assert m["hfr"] == pytest.approx(4.0, rel=0.3)
    assert m["stars"] == 81 and m["stars_detected"] == 81
    assert m["fwhm_arcsec"] == pytest.approx(m["fwhm_px"] * 0.24, abs=0.01)
    assert m["ecc_def"] == "sqrt(1-(b/a)^2)" and m["measure_at"] == "native"
    for k in sm.PARITY_KEYS:
        assert k in m, k


def test_star_count_is_capped_like_live_and_detected_is_not(tmp_path,
                                                            monkeypatch):
    _need_sep()
    monkeypatch.setattr(sm, "MAX_STARS", 30)
    m = sm.measure_frame(_field(), _cfg(tmp_path))
    assert m["stars"] == 30 and m["stars_detected"] == 81


def test_binned_fallback_is_close_to_native(tmp_path):
    """MemoryError fallback: the same function on the 2x2 mean reports
    native-px sizes near the native measure (not equal: different scale)."""
    _need_sep()
    from photonscript.shared.star_shape import bin2x2_mean
    cfg = _cfg(tmp_path)
    d = _field(q_ratio=0.8).astype(np.float32)
    nat = sm.measure_frame(d, cfg)
    b = sm.measure_frame(bin2x2_mean(d), cfg, binned_input=True)
    assert b["measure_at"] == "binned" and b["ecc_bin"] == b["ecc"]
    assert b["hfr"] == pytest.approx(nat["hfr"], rel=0.2)
    assert b["fwhm_px"] == pytest.approx(nat["fwhm_px"], rel=0.25)
    assert b["background"] == pytest.approx(nat["background"], abs=2)
    # noise / swamp are approximate on the binned fallback (star wings do
    # not average down like sky noise), so only presence is asserted
    assert b["swamp"] is not None
    assert b["stars"] == nat["stars"]


def test_backfill_memory_error_uses_the_binned_fallback(tmp_path,
                                                        monkeypatch):
    _need_sep()
    from photonscript.scheduler import runs
    cfg = _cfg(tmp_path)
    f = _write(tmp_path / "fits" / NIGHT / "LIGHT" / "m_Ha_300s_0001.fits",
               _field())

    def boom(p):
        raise MemoryError
    monkeypatch.setattr(runs, "_load_native", boom)
    rec = runs._fast_grade(f, cfg, [])
    assert rec["ecc_at"] == "binned" and rec["ecc"] == rec["ecc_bin"]
    assert rec["fwhm_arcsec"] is not None and rec["hfr"] is not None


def test_no_sep_invents_no_shape(tmp_path, monkeypatch):
    from photonscript.shared import star_shape
    monkeypatch.setattr(star_shape, "_sep", lambda: None)
    m = sm.measure_frame(_field(hot=0), _cfg(tmp_path))
    assert m["hfr"] is None and m["ecc"] is None and m["fwhm_px"] is None
    assert m["stars"] > 0 and m["graded_by"].startswith("no-sep")
    assert m["star_table"] is None


# --------------------------------------------- stored records / readers

def test_record_fwhm_trusts_live_and_ps83_backfill_only():
    assert q.record_fwhm({"fwhm_arcsec": 1.5}) == 1.5
    assert q.record_fwhm({"fwhm_arcsec": 1.5, "graded_by": "sep-binned"}) \
        is None
    assert q.record_fwhm({"fwhm_arcsec": 1.5, "graded_by": "backfill-sep",
                          "measure_v": sm.MEASURE_VERSION}) == 1.5
    m = q.metrics_from_record({"fwhm_arcsec": 1.5, "graded_by": "backfill-sep",
                               "measure_v": sm.MEASURE_VERSION})
    assert m["fwhm_arcsec"] == 1.5


def test_tracking_axis_normalizes_old_lin_sidecars():
    """PS-84 elongation floor: sidecar ecc goes to sqrt form by its ecc_def
    before the 0.71 floor (lin 0.30)."""
    from photonscript.scheduler import tracking_test as tt
    lin = {"grader": "sep-binned", "ecc": [0.40, 0.20, None]}
    out = tt._sidecar_ecc_sqrt(lin, {})
    assert out[0] == pytest.approx(0.8, abs=1e-6) and out[2] is None
    assert out[0] > tt.ELONG_FLOOR > out[1]
    sq = {"grader": "live-sep", "ecc_def": "sqrt(1-(b/a)^2)", "ecc": [0.5]}
    assert tt._sidecar_ecc_sqrt(sq, {}) == [0.5]
    assert tt.ELONG_FLOOR == pytest.approx(0.71)


def test_old_corner_ecc_shown_in_sqrt_form():
    from photonscript.scheduler.runs import _ecc_display_sqrt
    subs = [{"graded_by": "sep-binned", "ecc": 0.2,
             "corner_ecc": {"TL": 0.2, "TR": None}}]
    assert _ecc_display_sqrt(subs) == 1
    assert subs[0]["corner_ecc"] == {"TL": 0.6, "TR": None}
    assert subs[0]["corner_ecc_raw"] == {"TL": 0.2, "TR": None}
    _ecc_display_sqrt(subs)                     # idempotent
    assert subs[0]["corner_ecc"]["TL"] == 0.6


# ------------------------------------------------- real frames (optional)

def _real_files():
    p = os.environ.get("PS_PARITY_FITS", "").strip()
    if not p:
        return []
    root = Path(p)
    files = [root] if root.is_file() else sorted(
        list(root.glob("*.fits")) + list(root.glob("*.fit")))
    return files[:int(os.environ.get("PS_PARITY_FITS_MAX", "3") or 3)]


@pytest.mark.parametrize("src", _real_files() or [None])
def test_real_fits_parity(tmp_path, src):
    if src is None:
        pytest.skip("set PS_PARITY_FITS to a folder of real FITS lights")
    _need_sep()
    import shutil
    from photonscript.scheduler.runs import _fast_grade
    cfg = _cfg(tmp_path)
    f = tmp_path / "fits" / NIGHT / "LIGHT" / Path(src).name
    f.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, f)               # never touch the source folder
    from astropy.io import fits
    osc = bool(str(fits.getheader(f).get("BAYERPAT") or "").strip())
    if osc:
        cfg = _cfg(tmp_path, pixel_scale_arcsec=1.29)
    live = _live(cfg, f)
    back = _fast_grade(f, cfg, [])
    _assert_close(live, back)
    assert back["ecc_at"] == "native"
    assert (live["ecc_bin"] is None) == (back["ecc_bin"] is None) == osc
    lr, br = _rows(live), _rows(back)
    for cid in CARD_ROWS:          # same verdict per check (near-limit
        assert lr[cid][3] == br[cid][3], cid  # jitter aside)
