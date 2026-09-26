"""Piggyback slew-gating (time-correlation) — tag OSC subs that straddle an
RC16 slew."""
from datetime import datetime, timedelta

from photonscript.scheduler import slew_gate as sg


def _t(sec):
    return datetime(2026, 9, 21, 3, 0, 0) + timedelta(seconds=sec)


def test_sep_arcmin_basic():
    assert sg.sep_arcmin(10.0, 20.0, 10.0, 20.0) == 0.0
    assert 59 < sg.sep_arcmin(0.0, 0.0, 1.0, 0.0) < 61   # ~1 deg = 60'
    assert sg.sep_arcmin(None, 0, 0, 0) == float("inf")  # unknown -> "moved"


def test_infer_windows_from_radec_move():
    # two frames on field A, a big move, then field B
    frames = [
        {"start": _t(0), "end": _t(120), "ra": 10.0, "dec": 41.0},
        {"start": _t(130), "end": _t(250), "ra": 10.0, "dec": 41.0},   # same field
        {"start": _t(320), "end": _t(440), "ra": 10.9, "dec": 41.0},   # moved ~40'
    ]
    w = sg.infer_slew_windows(frames, min_move_arcmin=5.0, settle_s=30.0)
    assert len(w) == 1
    # window spans prev end (250) - pad .. next start (320) + pad
    assert w[0][0] == _t(250) - timedelta(seconds=30)
    assert w[0][1] == _t(320) + timedelta(seconds=30)


def test_infer_windows_falls_back_to_target_change():
    frames = [
        {"start": _t(0), "end": _t(120), "ra": None, "dec": None, "target": "M31"},
        {"start": _t(130), "end": _t(250), "ra": None, "dec": None, "target": "M31"},
        {"start": _t(320), "end": _t(440), "ra": None, "dec": None, "target": "M33"},
    ]
    w = sg.infer_slew_windows(frames)
    assert len(w) == 1  # only the M31->M33 change


def test_tag_straddlers_marks_overlapping_osc_subs():
    rc16 = [
        {"start": _t(0), "end": _t(120), "ra": 10.0, "dec": 41.0},
        {"start": _t(320), "end": _t(440), "ra": 10.9, "dec": 41.0},  # moved
    ]
    # slew window = (prev end 120 − 30 pad, next start 320 + 30 pad) = (90, 350)
    osc = [
        {"start": _t(10), "end": _t(80)},     # clean — ends before the window (90)
        {"start": _t(100), "end": _t(340)},   # straddles the slew window
        {"start": _t(360), "end": _t(480)},   # clean — starts after the window (350)
    ]
    n = sg.tag_straddlers(osc, rc16)
    assert n == 1
    assert [s["straddled_slew"] for s in osc] == [False, True, False]


def test_tag_leaves_untimed_subs_untagged():
    rc16 = [{"start": _t(0), "end": _t(120), "ra": 10.0, "dec": 41.0},
            {"start": _t(320), "end": _t(440), "ra": 10.9, "dec": 41.0}]
    osc = [{"start": None, "end": None}]
    sg.tag_straddlers(osc, rc16)
    assert osc[0]["straddled_slew"] is False   # unknown -> never dropped


def test_no_windows_when_static():
    frames = [{"start": _t(0), "end": _t(120), "ra": 10.0, "dec": 41.0},
              {"start": _t(130), "end": _t(250), "ra": 10.0, "dec": 41.0}]
    assert sg.infer_slew_windows(frames) == []
