"""Long-dark QA: the per-camera dark model and the set-based light checks.

The Piggy-600 (AP26CC, gain 100, offset 256, LCG, 0 C) 450 s darks of
2026-10-16 sat at 303 ADU, +47 over a fresh 256 bias, and failed the
generic limit (10 + 0.02 ADU/s = 19 ADU at 450 s). A dark is now judged
against what the camera did at that length (earlier nights) or between its
known lengths, plus an allowance, and light is caught inside the set: a
level that moves through the set, a zone gradient one frame does not share
with its set, an outlier frame. Synthetic records and FITS, no hardware."""

from pathlib import Path

import numpy as np
import pytest

from photonscript.scheduler import calibration_qa as cq


def _r(typ, date, median, i, exp=None, t0="02:00:00", step_s=None, **kw):
    exp = exp if exp is not None else (0.001 if typ == "BIAS" else 450.0)
    step = step_s if step_s is not None else (exp + 10.0)
    sec = int(t0[:2]) * 3600 + int(t0[3:5]) * 60 + int(i * step)
    obs = f"{date}T{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
    r = {"type": typ, "imagetyp": typ, "date": date,
         "name": f"{typ}_{date}_{exp:g}_{i}.fits", "exptime": exp,
         "gain": 100, "offset": 256, "xbin": 1, "instrume": "AP26CC",
         "readout": "LCG", "settemp": 0.0, "ccdtemp": 0.0, "median": median,
         "mad": 4.0, "center": median, "corners": [median] * 4, "stars": 0,
         "date_obs": obs}
    r.update(kw)
    return r


def _add(frames, recs):
    for r in recs:
        frames[f"{r['type']}/{r['date']}/{r['name']}"] = r


def _bias(frames, date="2026-09-12", level=256.0, n=5):
    _add(frames, [_r("BIAS", date, level, i) for i in range(n)])


def _darks(frames, date, exp, levels, **kw):
    recs = [_r("DARK", date, m, i, exp=exp, **kw) for i, m in enumerate(levels)]
    _add(frames, recs)
    return recs


def _history(frames):
    """Accepted darks: 120 s +4, 300 s +13, 400 s +17 (09-12 bias 256)."""
    _bias(frames)
    _darks(frames, "2026-09-20", 120.0, [260.0] * 6)
    _darks(frames, "2026-10-10", 300.0, [269.0] * 6)
    _darks(frames, "2026-10-10", 400.0, [273.0] * 6)


def _judge(frames, **kw):
    store = {"frames": frames}
    cq.judge_all(store, rig="piggyback", tol=1.0, setpoint=0.0, **kw)
    return store


# --- the model --------------------------------------------------------------------------

def test_expectation_interpolates_and_extrapolates():
    anchors = [{"exptime": 120.0, "date": "2026-09-20", "excess": 4.0, "n": 6},
               {"exptime": 300.0, "date": "2026-10-10", "excess": 13.0, "n": 6}]
    e, lim, src = cq.dark_expectation(anchors, 210.0, "2026-10-16")
    assert e == pytest.approx(8.5) and "interpolated between 120 s" in src
    # 2026-10-28: the allowance is the larger of 0.25 x expected and 1.5 x
    # the measured dark current (17 ADU / 420 s) x 210 s
    assert lim == pytest.approx(8.5 + 5.0 + 1.5 * 17.0 / 420.0 * 210.0, abs=0.01)
    _e, lim0, _src = cq.dark_expectation(anchors, 210.0, "2026-10-16", rate_allow=0)
    assert lim0 == pytest.approx(8.5 + 5.0 + 0.25 * 8.5, abs=0.01)
    e, _lim, src = cq.dark_expectation(anchors, 60.0, "2026-10-16")
    assert e == pytest.approx(2.0)          # through the bias at 0 s
    e, _lim, src = cq.dark_expectation(anchors, 450.0, "2026-10-16")
    assert e == pytest.approx(13.0 + 0.05 * 150) and "extrapolated" in src
    assert cq.dark_expectation([], 450.0, "2026-10-16") is None


def test_expectation_longest_length_held_to_its_own_prior_nights():
    anchors = [{"exptime": 300.0, "date": "2026-10-10", "excess": 13.0, "n": 6},
               {"exptime": 450.0, "date": "2026-10-11", "excess": 45.0, "n": 6},
               {"exptime": 450.0, "date": "2026-10-20", "excess": 99.0, "n": 6}]
    e, _lim, src = cq.dark_expectation(anchors, 450.0, "2026-10-16")
    assert e == 45.0 and src == "450 s on 2026-10-11 (+45.0)"   # later night ignored


def test_settings_from_config():
    cfg = type("C", (), {"calibration_dark_model": False,
                         "calibration_dark_model_allow_adu": 8.0,
                         "calibration_dark_model_allow_frac": "bad"})()
    assert cq.dark_model_settings(cfg) == {"enabled": False, "allow_adu": 8.0,
                                           "allow_frac": cq.DARK_MODEL_ALLOW_FRAC,
                                           "rate_allow": cq.DARK_MODEL_RATE_ALLOW}


# --- the five behaviours ------------------------------------------------------------

def test_longest_length_judged_by_its_own_prior_nights():
    """The first 450 s night is extrapolated from 300 / 400 s and anchors
    the length; later 450 s nights are held to it."""
    frames = {}
    _history(frames)
    _darks(frames, "2026-10-11", 450.0, [275.0] * 6)   # +19 (expected +19)
    ok = _darks(frames, "2026-10-16", 450.0, [281.0] * 6, t0="01:35:00")   # +25
    # 2026-10-28: +47 (2.1 x the expected +22) is now inside the dark
    # current allowance (1.5 x 0.045 ADU/s x 450 s); +79 is not
    hot = _darks(frames, "2026-10-17", 450.0, [335.0] * 6, t0="01:35:00")  # +79
    store = _judge(frames)
    rows = {(s["date"], s["exptime"]): s for s in store["dark_model"]}
    first = rows[("2026-10-11", 450.0)]
    assert first["anchor"] and first["expected"] == pytest.approx(19.0)
    assert first["source"] == ("the dark model, extrapolated from 300 s (+13.0) "
                               "and 400 s (+17.0)")
    assert rows[("2026-10-16", 450.0)]["source"] == "450 s on 2026-10-11 (+19.0)"
    assert all(r["verdict"] == "pass" for r in ok)       # generic limit 19: would fail
    assert rows[("2026-10-17", 450.0)]["expected"] == 22.0   # median of 2 nights
    for r in hot:
        assert r["verdict"] == "fail" and r["level_verdict"] == "light"


def test_uniform_pedestal_passes():
    """A uniform pedestal over the generic limit passes when the model
    expects it, and the zone pattern the whole set shares is pardoned as
    the sensor's fixed pattern (it failed [leak] frame by frame)."""
    frames = {}
    _history(frames)
    pats = dict(center=282.0, corners=[281.75, 278.0, 282.5, 280.0])
    new = _darks(frames, "2026-10-16", 450.0, [281.0] * 6, t0="01:35:00", **pats)
    store = _judge(frames)
    assert cq.dark_excess_limit(450.0, 0.0) == pytest.approx(19.0)
    for r in new:
        assert r["verdict"] == "warn", r["reasons"]
        assert r["dark_excess"] == 25.0 and r["dark_expected"] == pytest.approx(19.0)
        # 19 + 5 + 1.5 x (34 ADU / 820 s) x 450 s (was 19 + 5 + 0.25 x 19)
        assert r["dark_limit"] == pytest.approx(19.0 + 5.0 + 1.5 * 34.0 / 820.0 * 450.0,
                                                abs=0.01)
        assert [w.split(":")[0] for w in r["warnings"]] == ["zone_pattern"]
    row = [s for s in store["dark_model"] if s["exptime"] == 450.0][0]
    assert row["anchor"] and row["trend"]["change"] == 0.0
    # the same set judged alone (no anchors: the generic formula) fails
    frames_off = {}
    _bias(frames_off)
    _add(frames_off, [dict(r) for r in new])
    _judge(frames_off)
    for r in frames_off.values():
        if r["type"] == "DARK":
            assert {"level", "leak"} <= set(r["codes"])


def test_rising_level_fails():
    frames = {}
    _history(frames)
    lit = _darks(frames, "2026-10-16", 300.0, [266.0, 268.0, 270.0, 272.0, 274.0, 276.0],
                 t0="01:35:00")
    _judge(frames)
    for r in lit:
        assert r["verdict"] == "fail" and "trend" in r["codes"]
    why = [x for x in lit[0]["reasons"] if x.startswith("trend")][0]
    assert "+10.0 ADU through the set" in why and "light" in why
    assert "trend" in cq.LIGHT_CODES


def test_falling_level_with_twilight_fails_and_names_the_sun():
    frames = {}
    _history(frames)
    lit = _darks(frames, "2026-10-16", 120.0, [268.0, 265.0, 263.0, 261.0, 260.0],
                 t0="01:00:00")
    for r, a in zip(lit, (-4.0, -7.0, -10.0, -13.0, -16.0)):
        r["sun_alt"] = a
    _judge(frames)
    for r in lit:
        assert "trend" in r["codes"]
    assert any("sun -4 to -16 deg" in x for x in lit[0]["reasons"])


def test_quadrant_gradient_fails():
    frames = {}
    _history(frames)
    recs = _darks(frames, "2026-10-16", 300.0, [269.0] * 6, t0="01:35:00")
    recs[3]["corners"] = [269.0, 269.0, 269.0, 274.5]   # one corner lit
    _judge(frames)
    bad = recs[3]
    assert bad["verdict"] == "fail"
    assert "gradient" in bad["codes"] and "leak" in bad["codes"]
    assert "gradient" in cq.LIGHT_CODES
    assert all(r["verdict"] == "pass" for i, r in enumerate(recs) if i != 3)


def test_gradient_shared_by_set_but_too_large_stays_leak():
    """A zone pattern every frame shares is pardoned only up to its cap
    (LEAK_ADU + PATTERN_FRAC x expected): a big steady gradient is light."""
    frames = {}
    _history(frames)
    recs = _darks(frames, "2026-10-16", 120.0, [260.0] * 5, t0="01:35:00",
                  center=262.0, corners=[262.0, 262.0, 262.0, 252.0])
    _judge(frames)
    for r in recs:
        assert "leak" in r["codes"] and r["verdict"] == "fail"


def test_outlier_frame_fails():
    frames = {}
    _history(frames)
    recs = _darks(frames, "2026-10-16", 300.0, [269.0, 269.0, 280.0, 269.0, 269.0, 269.0],
                  t0="01:35:00")
    _judge(frames)
    assert recs[2]["verdict"] == "fail" and "outlier" in recs[2]["codes"]
    assert all(r["verdict"] == "pass" for i, r in enumerate(recs) if i != 2)


def test_level_over_the_model_still_fails():
    frames = {}
    _history(frames)
    recs = _darks(frames, "2026-10-16", 300.0, [295.0] * 6, t0="01:35:00")
    _judge(frames)
    for r in recs:
        assert r["verdict"] == "fail" and r["level_verdict"] == "light"
    msg = [x for x in recs[0]["reasons"] if x.startswith("level")][0]
    assert "39.0 ADU over bias 256" in msg
    assert "(expected +13.0 from 300 s on 2026-10-10 (+13.0))" in msg


def test_bias_drift_still_flagged_with_the_model():
    """A newer bias that explains a dark is still bias drift, not light,
    when the model sets the limit."""
    frames = {}
    _history(frames)
    _bias(frames, "2026-10-16", 290.0)                      # drifted +34
    recs = _darks(frames, "2026-10-12", 300.0, [302.0] * 6, t0="01:35:00")
    store = _judge(frames)
    for r in recs:
        assert r["verdict"] == "warn", r["reasons"]
        assert r["level_verdict"] == "bias drift"
        assert r["warnings"][0].startswith("bias_drift: median 302 is 46.0 ADU over bias 256")
    row = cq.drift_for(None, "piggyback", gain=100, offset=256, readout="LCG",
                       setpoint=0.0, store=store)
    assert row["drift"] == 34.0


def test_model_off_is_the_generic_formula():
    frames = {}
    _bias(frames)
    _darks(frames, "2026-09-20", 120.0, [270.0] * 6)
    _darks(frames, "2026-10-10", 300.0, [290.0] * 6)
    new = _darks(frames, "2026-10-16", 450.0, [303.0] * 6, t0="01:35:00")
    _judge(frames, dark_model={"enabled": False, "allow_adu": 5, "allow_frac": 0.25})
    assert all("level" in r["codes"] for r in new)


# --- the read-only release check ---------------------------------------------------

def _fits(path: Path, level: float, exp: float, obs: str, imagetyp="DARK"):
    from astropy.io import fits
    data = np.full((64, 64), level, dtype=np.uint16)
    h = fits.Header()
    h.update({"IMAGETYP": imagetyp, "EXPTIME": exp, "GAIN": 100, "OFFSET": 256,
              "SET-TEMP": 0.0, "CCD-TEMP": 0.0, "INSTRUME": "AP26CC",
              "READOUTM": "LCG", "DATE-OBS": obs})
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(data, h).writeto(path)


def _fake_measure(path, config=None):
    from astropy.io import fits
    h = fits.getheader(path)
    med = float(np.median(fits.getdata(path)))
    out = cq.header_fields(h)
    out.update({"median": med, "mad": 4.0, "center": med, "corners": [med] * 4,
                "stars": 0, "sat_pct": 0.0})
    return out


def test_release_check_is_read_only_and_lists_what_would_pass(tmp_path):
    lib = tmp_path / "piggyback"
    cal = lib / "Calibration"
    for i in range(5):
        _fits(cal / "BIAS" / "2026-10-16" / f"b{i}_0.00s_.fits", 256, 0.001,
              f"2026-10-16T19:24:{i:02d}", "BIAS")
        _fits(cal / "DARK" / "2026-09-20" / f"d{i}_120.00s_.fits", 260, 120.0,
              f"2026-09-21T03:{i * 3:02d}:00")
        _fits(cal / "DARK" / "2026-10-10" / f"d{i}_300.00s_.fits", 269, 300.0,
              f"2026-10-11T03:{i * 6:02d}:00")
    q = cal / "_quarantine" / "DARK" / "2026-10-16"
    for i in range(6):
        _fits(q / f"q{i}_450.00s_.fits", 281 if i < 5 else 300, 450.0, f"2026-10-17T01:{30 + i * 5:02d}:00")
    (q / "reasons.json").write_text('{"q0_450.00s_.fits": {"reasons": ["level: x"]}}',
                                    encoding="utf-8")
    before = sorted((p, p.stat().st_mtime_ns, p.stat().st_size)
                    for p in tmp_path.rglob("*"))
    rep = cq.release_check(None, "piggyback", lib, q, measure_fn=_fake_measure)
    after = sorted((p, p.stat().st_mtime_ns, p.stat().st_size)
                   for p in tmp_path.rglob("*"))
    assert before == after                      # nothing written, moved or removed
    assert len(rep["frames"]) == 6 and len(rep["would_pass"]) == 5
    assert rep["still_fail"] == ["q5_450.00s_.fits"]       # +44: over the model
    f0 = rep["frames"][0]
    assert f0["quarantined_for"] == ["level: x"] and f0["old_rule"] == "fail"
    txt = cq.format_release_check(rep)
    assert "DRY RUN" in txt and "would pass now: 5 of 6" in txt
    with pytest.raises(ValueError):
        cq.release_check(None, "piggyback", lib, tmp_path, measure_fn=_fake_measure)
