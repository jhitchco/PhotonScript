"""PS-211: Piggy-600 300 s darks are judged by the dark model, not bias.

2026-11-02 the Piggy-600 default sub went to 300 s with darks at 120 s and
300 s (PS_PIGGYBACK_DARK_EXPOSURES="120,300"). The AP26CC (gain 100, offset
256, LCG, 0 C) carries real long-exposure signal over its 256 bias: 120 s
+4 to +8, 300 s +13, 450 s +19 to +47 (Library and /api/calibration/qa,
2026-10). These pin that the level limit follows the camera's measured
dark signal, scaled by the exposure and the set's CCD-TEMP over SET-TEMP,
with no length written into the rule: a first 300 s night extrapolated
from 120 s only, later 300 s nights held to their own history, 450 s, a
slightly warm set; and that light (a pedestal several times the dark
current) still fails. Synthetic records, no hardware."""

import pytest

from photonscript.scheduler import calibration_qa as cq

BIAS = 256.0


def _r(typ, date, median, i, exp, t0="02:00:00", **kw):
    sec = int(t0[:2]) * 3600 + int(t0[3:5]) * 60 + int(i * (exp + 10.0))
    obs = f"{date}T{sec // 3600 % 24:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"
    r = {"type": typ, "imagetyp": typ, "date": date,
         "name": f"{typ}_{date}_{exp:g}_{i}.fits", "exptime": exp,
         "gain": 100, "offset": 256, "xbin": 1, "instrume": "AP26CC",
         "readout": "LCG", "settemp": 0.0, "ccdtemp": 0.0, "median": median,
         "mad": 4.0, "center": median, "corners": [median] * 4, "stars": 0,
         "date_obs": obs}
    r.update(kw)
    return r


def _set(frames, date, exp, levels, **kw):
    typ = "BIAS" if exp <= 0.01 else "DARK"
    recs = [_r(typ, date, m, i, exp, **kw) for i, m in enumerate(levels)]
    for r in recs:
        frames[f"{typ}/{date}/{r['name']}"] = r
    return recs


def _judge(frames):
    store = {"frames": frames}
    cq.judge_all(store, rig="piggyback", tol=1.0, setpoint=0.0)
    return store, {(s["date"], s["exptime"]): s for s in store["dark_model"]}


def _base(frames):
    _set(frames, "2026-10-16", 0.001, [BIAS] * 5)
    _set(frames, "2026-10-20", 120.0, [261.0] * 10)       # +5


def test_first_300s_night_from_120s_only_passes():
    """The new dark list: the first 300 s night has only 120 s anchors. The
    generic formula (10 + 0.02 x 300 = 16) is not used; the model
    extrapolates 120 s through the bias and allows 1.5 x the measured
    dark current x 300 s on top."""
    frames = {}
    _base(frames)
    recs = _set(frames, "2026-11-03", 300.0, [256.0 + 18.0] * 10)   # +18
    _store, rows = _judge(frames)
    row = rows[("2026-11-03", 300.0)]
    assert row["expected"] == pytest.approx(12.5)        # 5 / 120 x 300
    assert row["limit"] == pytest.approx(12.5 + 5.0 + 1.5 * 5.0 / 120.0 * 300.0,
                                         abs=0.01)
    assert row["limit"] > cq.dark_excess_limit(300.0, 0.0) + 15
    assert all(r["verdict"] == "pass" for r in recs), recs[0]["reasons"]
    assert row["anchor"]


def test_300s_held_to_its_own_nights_and_450s_scaled_by_exposure():
    frames = {}
    _base(frames)
    _set(frames, "2026-10-10", 300.0, [269.0] * 10)      # +13
    _set(frames, "2026-10-11", 300.0, [269.0] * 10)      # +13
    _set(frames, "2026-10-21", 450.0, [276.0] * 10)      # +20
    r300 = _set(frames, "2026-11-03", 300.0, [256.0 + 24.0] * 10)  # +24
    r450 = _set(frames, "2026-11-03", 450.0, [256.0 + 36.0] * 10)  # +36
    _store, rows = _judge(frames)
    a, b = rows[("2026-11-03", 300.0)], rows[("2026-11-03", 450.0)]
    assert a["source"] == "300 s on 2026-10-10 (+13.0), 2026-10-11 (+13.0)"
    assert b["source"] == "450 s on 2026-10-21 (+20.0)"
    assert all(r["verdict"] == "pass" for r in r300 + r450), \
        (r300[0]["reasons"], r450[0]["reasons"])
    # the allowance over expected grows with the exposure
    assert (b["limit"] - b["expected"]) > (a["limit"] - a["expected"])


def test_warm_set_gets_a_temperature_scaled_allowance():
    frames = {}
    _base(frames)
    _set(frames, "2026-10-10", 300.0, [269.0] * 10)
    cold = _set(frames, "2026-11-03", 300.0, [269.0] * 10)
    warm = _set(frames, "2026-11-04", 300.0, [269.0] * 10, ccdtemp=0.9)
    _store, rows = _judge(frames)
    assert all(r["verdict"] == "pass" for r in cold + warm)
    c, w = rows[("2026-11-03", 300.0)], rows[("2026-11-04", 300.0)]
    gain = (w["limit"] - w["expected"] - 5.0) / (c["limit"] - c["expected"] - 5.0)
    assert gain == pytest.approx(2 ** (0.9 / cq.DARK_DOUBLING_C), rel=0.02)
    assert "for +0.9 C" in (w["allowance"] or "")


@pytest.mark.parametrize("exp", [120.0, 180.0, 300.0, 450.0, 600.0])
def test_the_limit_follows_the_exposure_not_a_list_of_lengths(exp):
    """Any length: the model's limit stays over the camera's own dark
    signal at that length (~0.042 ADU/s here) and under a leak of 3 x."""
    frames = {}
    _base(frames)
    _set(frames, "2026-10-10", 300.0, [269.0] * 10)
    _store, rows = _judge(frames)
    anchors = [{"exptime": s["exptime"], "date": s["date"], "excess": s["excess"],
                "n": s["n_pass"]} for s in rows.values() if s["anchor"]]
    e, lim, _src = cq.dark_expectation(anchors, exp, "2026-11-03")
    real = 13.0 / 300.0 * exp
    assert lim > real + 4.0
    assert lim < 3.0 * real + 10.0


def test_300s_light_leak_still_fails():
    frames = {}
    _base(frames)
    _set(frames, "2026-10-10", 300.0, [269.0] * 10)
    leak = _set(frames, "2026-11-03", 300.0, [256.0 + 70.0] * 10)    # +70
    _judge(frames)
    for r in leak:
        assert r["verdict"] == "fail" and "level" in r["codes"]
        assert r["level_verdict"] == "light"
