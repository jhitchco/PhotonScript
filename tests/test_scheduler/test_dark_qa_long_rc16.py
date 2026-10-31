"""Dark QA: real dark signal on long darks (2026-10-28).

After the 9bf6c52 dark model the RC16 (gain 200, offset 256, HCG, 0 C)
750 s darks of 2026-10-20 and the 900 s darks of 10-22 were quarantined on
"level" (+38 to +90 ADU over the 10-10 bias at 254), while the 10-21 sets
at the same lengths (+28, +33.5) passed: the limit was one night's excess
+ 5 ADU + 25 %. The allowance now also scales with the camera's measured
dark current x the exposure (x 2^(dT/6) for the set's CCD-TEMP over
SET-TEMP), so the uniform frames of those nights pass the level check,
while a leak several times the dark current still fails it and a
non-uniform or changing one fails [gradient] / [leak] / [trend].

Levels below are the live /api/calibration/qa?rig=rc16 medians (read
2026-10-28) on synthetic records; no hardware.
"""

import pytest

from photonscript.scheduler import calibration_qa as cq

BIAS = 254.0

# 2026-10-20 750 s, in order (first three carry a corner gradient)
L1020 = [319, 325, 323, 310, 305, 300, 308, 304, 297, 292, 296, 297, 294, 292,
         295, 299, 294, 291, 291, 294, 291]
G1020 = {0: [2.75, 7.0, -1.5, -1.5], 1: [3.5, 7.25, -1.25, -1.5],
         2: [3.5, 7.25, -1.25, -1.5]}
# 2026-10-22 900 s, in order
L1022 = [344, 341, 342, 316, 310, 311, 309, 317, 311, 310, 310, 311, 306, 314]
G1022 = {0: [4.5, 9.75, -1.75, -1.5], 1: [4.0, 9.25, -1.75, -1.75],
         2: [4.25, 9.5, -2.0, -1.75]}


def _r(typ, date, median, i, exp, t0="01:30:00", pattern=None, flat_time=False):
    step = exp + 10.0
    sec = int(t0[:2]) * 3600 + int(t0[3:5]) * 60 + int(i * step)
    obs = f"{date}T{sec // 3600 % 24:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
    pat = pattern or [0.0, 0.0, 0.0, 0.0]
    return {"type": typ, "imagetyp": typ, "date": date,
            "name": f"{typ}_{date}_{exp:g}_{i}.fits", "exptime": exp,
            "gain": 200, "offset": 256, "xbin": 1, "instrume": "AP26MC",
            "readout": "HCG", "settemp": 0.0, "ccdtemp": 0.0, "median": float(median),
            "mad": 6.0, "center": float(median),
            "corners": [float(median) + p for p in pat], "stars": 2,
            "date_obs": obs}


def _set(frames, date, exp, levels, patterns=None, default_pat=None, **kw):
    recs = []
    for i, m in enumerate(levels):
        pat = (patterns or {}).get(i, default_pat)
        recs.append(_r("DARK" if exp > 0.01 else "BIAS", date, m, i, exp,
                       pattern=pat, **kw))
    for r in recs:
        frames[f"{r['type']}/{r['date']}/{r['name']}"] = r
    return recs


def _history(frames):
    """The RC16 HCG sets that passed before 10-20 (live medians), the
    10-10 bias at 254 in force for all of them."""
    _set(frames, "2026-10-10", 0.001, [BIAS] * 5)
    _set(frames, "2026-09-27", 300.0, [258.5] * 6)    # +4.5
    _set(frames, "2026-10-17", 120.0, [258.0] * 6)    # +4
    _set(frames, "2026-10-17", 180.0, [260.0] * 3)    # +6
    _set(frames, "2026-10-17", 300.0, [264.5] * 6)    # +10.5
    _set(frames, "2026-10-17", 600.0, [274.0] * 6)    # +20


def _judge(frames):
    store = {"frames": frames}
    cq.judge_all(store, rig="rc16", tol=1.0, setpoint=0.0)
    return store


def _rows(store):
    return {(s["date"], s["exptime"]): s for s in store["dark_model"]}


# --- the measured dark current ---------------------------------------------------

def test_dark_rate_pools_long_anchors_and_skips_short_and_later_ones():
    anchors = [{"exptime": 30.0, "date": "2026-10-06", "excess": 3.0, "n": 30},
               {"exptime": 300.0, "date": "2026-10-17", "excess": 10.5, "n": 6},
               {"exptime": 600.0, "date": "2026-10-17", "excess": 20.0, "n": 6},
               {"exptime": 900.0, "date": "2026-10-30", "excess": 99.0, "n": 6}]
    assert cq.dark_rate(anchors, "2026-10-20") == pytest.approx(30.5 / 900.0)
    assert cq.dark_rate(anchors[:1], "2026-10-20") is None     # 30 s: bias noise
    assert cq.dark_rate([], "2026-10-20") is None


def test_allowance_scales_with_exposure_and_temperature():
    anchors = [{"exptime": 600.0, "date": "2026-10-17", "excess": 20.0, "n": 6},
               {"exptime": 900.0, "date": "2026-10-21", "excess": 30.0, "n": 6}]
    rate = 50.0 / 1500.0
    e, lim, _src, note = cq.dark_expectation(anchors, 900.0, "2026-10-22", detail=True)
    assert e == 30.0 and lim == pytest.approx(30.0 + 5.0 + 1.5 * rate * 900, abs=0.01)
    assert "measured dark current 0.0333 ADU/s x 900 s" in note
    _e, lim600, _s = cq.dark_expectation(anchors, 600.0, "2026-10-22")
    assert lim600 == pytest.approx(20.0 + 5.0 + 1.5 * rate * 600, abs=0.01)
    _e, warm, _s, note = cq.dark_expectation(anchors, 900.0, "2026-10-22",
                                             temp_delta_c=0.9, detail=True)
    assert warm == pytest.approx(30.0 + 5.0 + 1.5 * rate * 900 * 2 ** (0.9 / 6),
                                 abs=0.01)
    assert "for +0.9 C" in note
    # 0 = the 9bf6c52 rule (0.25 x expected)
    _e, old, _s = cq.dark_expectation(anchors, 900.0, "2026-10-22", rate_allow=0)
    assert old == pytest.approx(30.0 + 5.0 + 0.25 * 30.0, abs=0.01)


def test_rate_allow_setting():
    cfg = type("C", (), {"calibration_dark_model_rate_allow": 2.0})()
    assert cq.dark_model_settings(cfg)["rate_allow"] == 2.0
    cfg = type("C", (), {"calibration_dark_model_rate_allow": -1})()
    assert cq.dark_model_settings(cfg)["rate_allow"] == 0.0


# --- the 10-20 / 10-22 nights ----------------------------------------------------

def test_1020_750s_uniform_frames_pass_the_level_check():
    frames = {}
    _history(frames)
    recs = _set(frames, "2026-10-20", 750.0, L1020, patterns=G1020,
                default_pat=[3.0, 3.0, 0.0, -1.0])
    store = _judge(frames)
    row = _rows(store)[("2026-10-20", 750.0)]
    assert row["expected"] == pytest.approx(26.25)        # live: +26.2
    assert row["rate"] == pytest.approx(45.0 / 1500.0, abs=1e-4)
    for i, r in enumerate(recs):
        if i >= 3:          # +38..+56: real long-dark signal, not "level"
            assert "level" not in r["codes"], (i, r["reasons"])
            assert r["level_verdict"] is None
    # the gradient frames at the start still fail as light
    for i in (1, 2):        # +71, +69
        assert {"level", "gradient"} <= set(recs[i]["codes"])
    # the level falling 28 ADU through the set is still a [trend] fail
    # (unchanged; the moon set during this set)
    assert all("trend" in r["codes"] for r in recs)


def test_1022_900s_uniform_frames_pass_the_level_check():
    frames = {}
    _history(frames)
    _set(frames, "2026-10-21", 750.0, [282.0] * 6)       # +28
    _set(frames, "2026-10-21", 900.0, [287.5] * 6)       # +33.5
    recs = _set(frames, "2026-10-22", 900.0, L1022, patterns=G1022,
                default_pat=[4.0, 4.0, 0.0, -1.0])
    store = _judge(frames)
    row = _rows(store)[("2026-10-22", 900.0)]
    assert row["expected"] == pytest.approx(33.5)
    assert row["source"] == "900 s on 2026-10-21 (+33.5)"
    for i, r in enumerate(recs):
        if i >= 3:          # +52..+63
            assert "level" not in r["codes"], (i, r["reasons"])
    for i in range(3):      # +87..+90 with a corner gradient: light
        assert {"level", "leak", "gradient"} <= set(recs[i]["codes"])
        lvl = [x for x in recs[i]["reasons"] if x.startswith("level")][0]
        assert "x the measured dark current" in lvl
    assert all("trend" in r["codes"] for r in recs)


def test_steady_long_dark_at_the_1022_level_passes():
    """The 10-22 uniform level (+57) on a set that does not change through
    the night passes outright and anchors the length."""
    frames = {}
    _history(frames)
    _set(frames, "2026-10-21", 900.0, [287.5] * 6)
    recs = _set(frames, "2026-10-22", 900.0, [311.0] * 8,
                default_pat=[4.0, 4.0, 0.0, -1.0])
    store = _judge(frames)
    for r in recs:
        assert r["verdict"] in ("pass", "warn"), r["reasons"]
    assert _rows(store)[("2026-10-22", 900.0)]["anchor"]


def test_steady_long_dark_at_the_1020_level_passes():
    frames = {}
    _history(frames)
    recs = _set(frames, "2026-10-20", 750.0, [300.0] * 8)   # +46
    _judge(frames)
    assert all(r["verdict"] == "pass" for r in recs), recs[0]["reasons"]


# --- light leaks still fail -------------------------------------------------------

def test_uniform_leak_far_over_the_dark_current_fails_level():
    frames = {}
    _history(frames)
    _set(frames, "2026-10-21", 900.0, [287.5] * 6)
    recs = _set(frames, "2026-10-22", 900.0, [404.0] * 8)    # +150, steady
    _judge(frames)
    for r in recs:
        assert r["verdict"] == "fail" and "level" in r["codes"]
        assert r["level_verdict"] == "light"


def test_gradient_leak_at_a_passing_level_fails():
    """A leak that lights one corner in some frames fails [gradient] even
    though its level passes."""
    frames = {}
    _history(frames)
    _set(frames, "2026-10-21", 900.0, [287.5] * 6)
    pats = {i: [3.0, 14.0, 0.0, -1.0] for i in (2, 5)}
    recs = _set(frames, "2026-10-22", 900.0, [311.0] * 8, patterns=pats,
                default_pat=[3.0, 3.0, 0.0, -1.0])
    _judge(frames)
    for i, r in enumerate(recs):
        assert "level" not in r["codes"]
        if i in (2, 5):
            assert "gradient" in r["codes"] and r["verdict"] == "fail"


def test_rising_leak_at_a_passing_level_fails_trend():
    frames = {}
    _history(frames)
    _set(frames, "2026-10-21", 900.0, [287.5] * 6)
    recs = _set(frames, "2026-10-22", 900.0, [300.0 + 2 * i for i in range(8)])
    _judge(frames)
    assert all("trend" in r["codes"] for r in recs)
    assert all("level" not in r["codes"] for r in recs)
