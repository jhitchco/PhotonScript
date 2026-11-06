"""PS-146: grading on narrowband.

RC16 3 nm subs (2026-10-05): faint stars read 0.03 to 0.15 more elongated
than the bright ones (noise in the second moments), and the grader's FWHM,
2.355 x sep's threshold moment, read faint defocused Ha stars at 1.6 to 2.8"
while their HFR said about 5". shared.star_measure now judges the ecc of
the brightest stars when qa_ecc_bright_rule says so (narrowband, under
exposed, few stars) and the FWHM from 2 x HFR when the moment is under
qa_fwhm_hfr_ratio x HFR. Both graders record what they judged and why
(measure_v ps83.2); a rescore derives it for older records.
"""

import math

import numpy as np
import pytest

from photonscript.shared import qa_rules as q
from photonscript.shared import star_measure as sm
from tests.test_scheduler.test_ps83_measure_parity import (NIGHT, _cfg,
                                                           _live, _need_sep,
                                                           _rows, _write)

SCALE = 0.24


def _mixed(n_bright=60, n_faint=200, size=640, fwhm=6.0,
           faint_amp=(100, 160), faint_q=1.0, noise=20, seed=5):
    """Bright round stars among many faint ones (round by default: their
    measured elongation is the noise in the moments)."""
    rng = np.random.default_rng(seed)
    d = rng.normal(600, noise, (size, size))
    s = fwhm / 2.3548
    yy, xx = np.mgrid[-15:16, -15:16]
    n = n_bright + n_faint
    g = int(math.ceil(math.sqrt(n)))
    step = (size - 40) // g
    for k, idx in enumerate(rng.permutation(n)):
        i, j = divmod(k, g)
        cy = 25 + i * step + rng.uniform(0, 1)
        cx = 25 + j * step + rng.uniform(0, 1)
        iy, ix = int(cy), int(cx)
        x, y = xx - (cx - ix), yy - (cy - iy)
        bright = idx < n_bright
        qr = 1.0 if bright else faint_q
        th = rng.uniform(0, math.pi)
        sa, sb = s / math.sqrt(qr), s * math.sqrt(qr)
        u = x * math.cos(th) + y * math.sin(th)
        v = -x * math.sin(th) + y * math.cos(th)
        amp = rng.uniform(15000, 30000) if bright else rng.uniform(*faint_amp)
        d[iy - 15:iy + 16, ix - 15:ix + 16] += amp * np.exp(
            -0.5 * (u ** 2 / sa ** 2 + v ** 2 / sb ** 2))
    return np.clip(d, 0, 65535).astype(np.float32)


def _defocused(amp, size=600, n=8, sigma=6.0, ring=None, noise=20, seed=1):
    """Broad defocused stars: a Gaussian of `sigma` px (true FWHM 2.355
    sigma), or with `ring` a donut (ring radius px, width sigma)."""
    rng = np.random.default_rng(seed)
    d = rng.normal(600, noise, (size, size))
    yy, xx = np.mgrid[-25:26, -25:26]
    step = (size - 80) // n
    for i in range(n):
        for j in range(n):
            cy = 50 + i * step + rng.uniform(0, 1)
            cx = 50 + j * step + rng.uniform(0, 1)
            iy, ix = int(cy), int(cx)
            r = np.hypot(xx - (cx - ix), yy - (cy - iy))
            prof = (np.exp(-0.5 * ((r - ring) / sigma) ** 2) if ring
                    else np.exp(-0.5 * (r / sigma) ** 2))
            d[iy - 25:iy + 26, ix - 25:ix + 26] += amp * prof
    return np.clip(d, 0, 65535).astype(np.float32)


def _m(tmp_path, data, flt="Ha", **kw):
    return sm.measure_frame(data, _cfg(tmp_path, **kw),
                            header={"FILTER": flt})


# ------------------------------------------------- (1) bright-star ecc

def test_faint_noise_elongation_is_not_judged_on_narrowband(tmp_path):
    _need_sep()
    d = _mixed()
    ha = _m(tmp_path, d, "Ha")
    # every star: the faint ones' noise pulls the median up
    assert ha["ecc_all"] > 0.25
    # the brightest 50 are the round ones
    assert ha["ecc_bright"] < 0.15 and ha["ecc_bright_n"] == 50
    assert ha["ecc"] == ha["ecc_bright"]
    assert ha["ecc_src"] == "bright" and ha["ecc_why"] == "narrowband"
    # the binned ecc follows the same choice
    assert ha["ecc_bin"] < ha["ecc_bin_all"]
    assert ha["measure_v"] == sm.MEASURE_VERSION == "ps83.2"
    # broadband, plenty of stars, well exposed: every star, as before
    lum = _m(tmp_path, d, "L")
    assert lum["exposure"] == "ok" and lum["stars"] >= 150
    assert lum["ecc"] == lum["ecc_all"] == ha["ecc_all"]
    assert lum["ecc_src"] == "all" and lum["ecc_why"] is None
    assert lum["ecc_bin"] == lum["ecc_bin_all"]


def test_bright_rule_modes_and_triggers(tmp_path):
    _need_sep()
    d = _mixed()
    assert _m(tmp_path, d, "Ha", qa_ecc_bright_rule="off")["ecc_src"] == "all"
    m = _m(tmp_path, d, "L", qa_ecc_bright_rule="always")
    assert m["ecc_src"] == "bright" and m["ecc_why"] == "always"
    m = _m(tmp_path, d, "L", qa_ecc_bright_min_stars=1000)
    assert m["ecc_src"] == "bright" and m["ecc_why"] == "few stars"
    m = _m(tmp_path, d, "L", qa_ecc_bright_filters="L")
    assert m["ecc_why"] == "narrowband"
    cfg = _cfg(tmp_path)
    assert sm.bright_rule(cfg, "OIII", "ok", 400) == "narrowband"
    assert sm.bright_rule(cfg, "R", "under", 400) == "under-exposed"
    assert sm.bright_rule(cfg, "R", "ok", 400) is None
    # the NINA profile name maps to the filter class
    cfg2 = _cfg(tmp_path, nina_filter_names="Ha:H,L:Lum")
    assert sm.frame_filter(cfg2, {"FILTER": "H"}) == "Ha"
    assert sm.is_narrowband(cfg2, sm.frame_filter(cfg2, {"FILTER": "H"}))


def test_bright_by_snr_and_too_few(tmp_path):
    _need_sep()
    m = _m(tmp_path, _mixed(), "Ha", qa_ecc_bright_snr=100.0)
    assert m["ecc_bright_n"] == 60 and m["ecc_bright"] < 0.15
    cfg = _cfg(tmp_path)
    # fewer than BRIGHT_MIN with an ecc: no bright value, ecc_all judged
    e, n = sm.bright_ecc([0.1, 0.2, np.nan], [3, 2, 1], None, cfg)
    assert e is None and n == 2
    s = sm.shape_fields(cfg, ecc_all=0.5, ecc_bright=None, n_bright=2,
                        fwhm_moment_px=8.0, hfr_px=4.0, pixel_scale=SCALE,
                        filt="Ha", exposure_flag="ok", stars=3)
    assert s["ecc"] == 0.5 and s["ecc_src"] == "all"


# ------------------------------------------------------ (2) robust FWHM

def test_faint_defocused_stars_do_not_read_sharp(tmp_path):
    _need_sep()
    true_px = 2.3548 * 6.0
    faint = _m(tmp_path, _defocused(60))
    mom_px = faint["fwhm_moment_arcsec"] / SCALE
    # the threshold moment sees only the core: far too small
    assert mom_px < 0.6 * true_px
    assert faint["fwhm_unreliable"] is True and faint["fwhm_src"] == "hfr"
    assert faint["fwhm_px"] == pytest.approx(2 * faint["hfr"], abs=0.02)
    assert faint["fwhm_px"] == pytest.approx(true_px, rel=0.15)
    assert faint["fwhm_arcsec"] == pytest.approx(faint["fwhm_px"] * SCALE,
                                                 abs=0.01)
    # a bright copy: the moment is fine and kept
    bright = _m(tmp_path, _defocused(5000))
    assert bright["fwhm_src"] == "moment" and not bright["fwhm_unreliable"]
    assert bright["fwhm_px"] == pytest.approx(true_px, rel=0.1)
    # qa_fwhm_method: moment = as before PS-146, hfr = always 2 x HFR
    old = _m(tmp_path, _defocused(60), qa_fwhm_method="moment")
    assert old["fwhm_src"] == "moment" and old["fwhm_unreliable"] is True
    assert old["fwhm_px"] == pytest.approx(mom_px, abs=0.02)
    h = _m(tmp_path, _defocused(5000), qa_fwhm_method="hfr")
    assert h["fwhm_src"] == "hfr"


def test_faint_donut_stars_read_at_least_their_size(tmp_path):
    """Defocused donuts: a faint one breaks into pieces above the
    threshold, each with a tiny moment; the judged FWHM must not read
    smaller than the bright donut's."""
    _need_sep()
    faint = _m(tmp_path, _defocused(60, sigma=2.5, ring=8.0))
    bright = _m(tmp_path, _defocused(5000, sigma=2.5, ring=8.0))
    assert faint["fwhm_unreliable"] is True and faint["fwhm_src"] == "hfr"
    assert faint["fwhm_moment_arcsec"] < 0.75 * bright["fwhm_arcsec"]
    assert faint["fwhm_arcsec"] >= 0.9 * bright["fwhm_arcsec"]


def test_judge_fwhm_never_invents_a_fwhm(tmp_path):
    cfg = _cfg(tmp_path)
    assert sm.judge_fwhm(None, 5.0, cfg)["fwhm_px"] is None
    j = sm.judge_fwhm(5.0, None, cfg)
    assert j["fwhm_px"] == 5.0 and not j["fwhm_unreliable"]
    j = sm.judge_fwhm(7.4, 5.0, cfg)          # under 1.5 x HFR (default)
    assert j["fwhm_px"] == 10.0 and j["fwhm_unreliable"]
    j = sm.judge_fwhm(7.6, 5.0, cfg)          # just above: kept
    assert j["fwhm_px"] == 7.6 and not j["fwhm_unreliable"]
    old = _cfg(tmp_path, qa_fwhm_hfr_ratio=1.2)   # the ratio is a knob
    assert not sm.judge_fwhm(6.1, 5.0, old)["fwhm_unreliable"]
    assert sm.judge_fwhm(5.9, 5.0, old)["fwhm_unreliable"]


def test_fwhm_ratio_default_is_1_5():
    """Jeremy 2026-10-06: 1.5 also catches partly under-read stars (a
    synthetic broad star read 38% low at ratio 1.27); healthy stars sit near
    2.0."""
    from photonscript.shared.config import PhotonScriptConfig
    assert PhotonScriptConfig().qa_fwhm_hfr_ratio == 1.5


# ------------------------------------------------------ (3) scorecard

def test_scorecard_rows_say_bright_stars_and_hfr(tmp_path):
    cfg = _cfg(tmp_path)
    m = q.record_metrics(ecc=0.45, ecc_all=0.71, ecc_src="bright",
                         ecc_bright_n=50, ecc_why="narrowband", hfr=6.0,
                         fwhm_arcsec=2.88, fwhm_src="hfr",
                         fwhm_moment_arcsec=1.6, fwhm_unreliable=True,
                         stars=300)
    card = q.evaluate(m, q.context(cfg, "rc16", "T", "Ha"))
    rows = {c.id: c for c in card.checks}
    assert rows["ecc"].status == "pass"
    assert "brightest 50 stars, narrowband (all stars 0.71)" in \
        rows["ecc"].reason
    assert "from 2 x HFR" in rows["fwhm"].reason
    assert "1.6\"" in rows["fwhm"].reason
    # the note survives the compact row (lightbox side panel)
    stored = {r[0]: r for r in card.compact()["rows"]}
    assert "brightest 50 stars" in stored["ecc"][4]
    # a failing ecc says both
    m2 = dict(m, ecc=0.7)
    r2 = {c.id: c for c in q.evaluate(
        m2, q.context(cfg, "rc16", "T", "Ha")).checks}
    assert r2["ecc"].status == "fail"
    assert r2["ecc"].reason.startswith("Eccentricity 0.70 > 0.6")
    assert "brightest 50 stars" in r2["ecc"].reason
    # nothing noted for an all-star ecc and a moment FWHM
    m3 = q.record_metrics(ecc=0.4, ecc_src="all", hfr=4.0, fwhm_arcsec=2.0,
                          fwhm_src="moment", stars=300)
    r3 = {c.id: c for c in q.evaluate(
        m3, q.context(cfg, "rc16", "T", "L")).checks}
    assert r3["ecc"].reason == "" and r3["fwhm"].reason == ""


# ------------------------------------------------------ (4) parity

@pytest.mark.parametrize("make,flt,ecc_src,fwhm_src", [
    (lambda: _mixed(), "Ha", "bright", "moment"),   # bright-star ecc
    (lambda: _mixed(), "L", "all", "moment"),       # every star
    (lambda: _defocused(60), "Ha", None, "hfr"),    # FWHM from 2 x HFR
])
def test_live_and_backfill_judge_the_same_way(tmp_path, make, flt, ecc_src,
                                              fwhm_src):
    _need_sep()
    from photonscript.scheduler.runs import _fast_grade
    # Parity of the two graders under one rule: the synthetic stars sit
    # between ratio 1.2 and 1.5, so pin the original 1.2 to keep the
    # moment / hfr cases distinct (the 1.5 default has its own tests).
    cfg = _cfg(tmp_path, qa_fwhm_hfr_ratio=1.2)
    data = np.clip(make(), 0, 65535).astype(np.uint16)
    f = _write(tmp_path / "fits" / NIGHT / "LIGHT" / f"p_{flt}_0001.fits",
               data, FILTER=flt)
    live = _live(cfg, f)
    back = _fast_grade(f, cfg, [])
    for k in ("ecc", "ecc_bin", "fwhm_arcsec", "hfr", "measure_v",
              *sm.SHAPE_RECORD_KEYS):
        assert live.get(k) == back.get(k), (k, live.get(k), back.get(k))
    assert live["measure_v"] == "ps83.2"
    lr, br = _rows(live), _rows(back)
    for cid in ("ecc", "ecc_bin", "fwhm", "hfr"):
        assert lr[cid] == br[cid], cid
    assert live["fwhm_src"] == fwhm_src
    if ecc_src is not None:
        assert live["ecc_src"] == ecc_src
    if live["ecc_src"] == "bright":
        assert "brightest 50 stars" in lr["ecc"][4]
    if fwhm_src == "hfr":
        assert "2 x HFR" in lr["fwhm"][4]


# ------------------------------------------- (5) older records: rescore

def _old_record(**kw):
    rec = {"rig": "rc16", "file": "LIGHT/old_Ha_0001.fits", "target": "T",
           "filter": "Ha", "exp_s": 300.0, "hfr": 6.9, "fwhm_arcsec": 1.5,
           "stars": 300, "ecc": 0.66, "ecc_bin": 0.6,
           "ecc_def": "sqrt(1-(b/a)^2)", "measure_v": "ps83.1",
           "graded_by": "backfill-sep", "exposure": "ok",
           "background": 600.0, "passed_qa": False,
           "reason": "Eccentricity 0.66 > 0.6 (trailing/drift)",
           "auto_verdict": "rejected", "drivers": ["ecc"]}
    rec.update(kw)
    return rec


def _sidecar(n_round=60, n_faint=200):
    """A PS-80 sidecar, brightest first: round bright stars, then faint
    noisy ones."""
    ecc = [0.1] * n_round + [0.75] * n_faint
    n = len(ecc)
    return {"v": 1, "grader": "backfill-sep", "ecc_def": "sqrt(1-(b/a)^2)",
            "n": n, "x": [0.0] * n, "y": [0.0] * n, "hfr": [6.9] * n,
            "ecc": ecc, "theta": [0.0] * n}


def test_record_shape_fields_from_stored_numbers(tmp_path):
    cfg = _cfg(tmp_path)
    f = sm.record_shape_fields(_old_record(), cfg, _sidecar())
    assert f["ecc"] == 0.1 and f["ecc_src"] == "bright"
    assert f["ecc_all"] == 0.66 and f["ecc_bright_n"] == 50
    assert f["ecc_bin_all"] == 0.6
    # 1.5" / 0.24 = 6.25 px < 1.2 x 6.9: 2 x HFR
    assert f["fwhm_src"] == "hfr" and f["fwhm_unreliable"] is True
    assert f["fwhm_arcsec"] == pytest.approx(2 * 6.9 * SCALE, abs=0.01)
    assert f["fwhm_moment_arcsec"] == 1.5 and f["shape_from"] == "stored"
    # no sidecar: the all-star ecc stays judged
    f2 = sm.record_shape_fields(_old_record(), cfg, None)
    assert f2["ecc"] == 0.66 and f2["ecc_src"] == "all"
    # broadband: every star
    f3 = sm.record_shape_fields(_old_record(filter="L"), cfg, _sidecar())
    assert f3["ecc"] == 0.66
    # left alone: already ps83.2, pre-PS-83 backfill, nothing measured
    assert sm.record_shape_fields(_old_record(ecc_src="all"), cfg) is None
    assert sm.record_shape_fields(
        _old_record(measure_v=None, graded_by="sep-binned"), cfg) is None
    assert sm.record_shape_fields(
        _old_record(ecc=None, fwhm_arcsec=None), cfg) is None


def test_rescore_applies_it_to_old_records(tmp_path):
    from photonscript.scheduler import runs
    from photonscript.shared import star_table
    cfg = _cfg(tmp_path)
    rec = _old_record()
    runs.append_sub_record(cfg, NIGHT, rec)
    star_table.write(cfg, NIGHT, rec["file"], _sidecar())
    dry = runs.rescore_night(cfg, NIGHT, apply=False, allow_unreject=True)
    assert dry["counts"]["shape_backfilled"] == 1
    assert runs._load_subs(cfg, NIGHT)[0].get("ecc_src") is None   # dry
    assert any(d["file"] == rec["file"] and d["new"] != "rejected"
               for d in dry["diffs"])
    runs.rescore_night(cfg, NIGHT, apply=True, allow_unreject=True)
    (after,) = runs._load_subs(cfg, NIGHT)
    assert after["ecc"] == 0.1 and after["ecc_all"] == 0.66
    assert after["ecc_src"] == "bright" and after["shape_from"] == "stored"
    assert after["fwhm_src"] == "hfr" and after["passed_qa"] is True
    rows = {r[0]: r for r in after["scorecard"]["rows"]}
    assert "brightest 50 stars" in rows["ecc"][4]
    # idempotent: a second rescore derives nothing new
    again = runs.rescore_night(cfg, NIGHT, apply=False)
    assert "shape_backfilled" not in again["counts"]
