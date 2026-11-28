"""PS-162: one exposure window per sub record (shared.sub_time)."""

import json
from datetime import datetime
from types import SimpleNamespace

from photonscript.shared import sub_time


def _dt(s):
    return datetime.fromisoformat(s)


def test_live_record_time_is_the_end():
    rec = {"time": "2026-09-27T04:10:00.123456Z", "exp_s": 600}
    st, en = sub_time.sub_window(rec)
    assert st == _dt("2026-09-27T04:00:00.123456")
    assert en == _dt("2026-09-27T04:10:00.123456")


def test_backfill_record_time_is_the_start():
    rec = {"time": "2026-09-27T04:00:00.500", "exp_s": 180}
    st, en = sub_time.sub_window(rec)
    assert st == _dt("2026-09-27T04:00:00.500")
    assert en == _dt("2026-09-27T04:03:00.500")


def test_explicit_fields_win_over_time():
    rec = {"time": "2026-09-27T05:00:00Z", "exp_s": 60,
           "start_utc": "2026-09-27T04:00:00Z",
           "end_utc": "2026-09-27T04:01:00Z"}
    assert sub_time.sub_window(rec) == (_dt("2026-09-27T04:00:00"),
                                        _dt("2026-09-27T04:01:00"))


def test_date_obs_wins_over_time():
    rec = {"time": "2026-09-27T05:00:00Z", "exp_s": 60,
           "date_obs": "2026-09-27T03:00:00"}
    assert sub_time.sub_start(rec) == _dt("2026-09-27T03:00:00")


def test_unknown_time_is_none_and_normalize_leaves_it():
    rec = {"time": "", "exp_s": 60}
    assert sub_time.sub_window(rec) is None
    assert sub_time.normalize(rec) == {"time": "", "exp_s": 60}


def test_normalize_fills_and_keeps_time():
    rec = {"time": "2026-09-27T04:10:00Z", "exp_s": 600}
    sub_time.normalize(rec)
    assert rec["time"] == "2026-09-27T04:10:00Z"
    assert rec["start_utc"] == "2026-09-27T04:00:00Z"
    assert rec["end_utc"] == "2026-09-27T04:10:00Z"
    # idempotent
    assert sub_time.normalize(dict(rec)) == rec


def test_window_fields():
    assert sub_time.window_fields(None, 60) == {}
    assert sub_time.window_fields(_dt("2026-09-27T04:00:00"), 90) == {
        "start_utc": "2026-09-27T04:00:00Z", "end_utc": "2026-09-27T04:01:30Z"}


def test_load_subs_migrates_on_read(tmp_path):
    from photonscript.scheduler import runs
    cfg = SimpleNamespace(data_dir=str(tmp_path), runs_dir=str(tmp_path),
                          reverse_filter_map=lambda: {})
    d = runs.runs_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-09-26_subs.jsonl").write_text(
        json.dumps({"rig": "piggyback", "file": "a.fits",
                    "time": "2026-09-27T04:02:00Z", "exp_s": 120}) + "\n"
        + json.dumps({"rig": "rc16", "file": "b.fits",
                      "time": "2026-09-27T04:00:00", "exp_s": 600}) + "\n",
        encoding="utf-8")
    a, b = runs._load_subs(cfg, "2026-09-26")
    assert a["start_utc"] == "2026-09-27T04:00:00Z"
    assert b["start_utc"] == "2026-09-27T04:00:00Z"
    assert b["end_utc"] == "2026-09-27T04:10:00Z"
    assert a["time"] == "2026-09-27T04:02:00Z"


def test_correlate_uses_the_start_of_live_piggy_subs():
    """A live Piggy sub (`time` = end, "Z") whose exposure started inside
    the RC16 window but ended past window + tolerance is still attributed;
    before PS-162 its end time was compared and it stayed '?'."""
    from photonscript.scheduler import runs
    subs = [
        {"rig": "rc16", "target": "M31", "time": "2026-09-27T04:00:00",
         "exp_s": 600},
        # starts 04:09 (inside), ends 04:29 (past 04:10 + 10 min tol)
        {"rig": "piggyback", "target": "?", "time": "2026-09-27T04:29:00Z",
         "exp_s": 1200},
    ]
    n, windows, _ = runs.correlate_piggyback_records(subs)
    assert n == 1 and windows == {"M31": 1}


def test_correlate_live_piggy_end_before_rc16_start_is_not_attributed():
    """A Piggy sub that ENDED just before the RC16 window but STARTED well
    before it (tolerance measured from the start) stays '?'."""
    from photonscript.scheduler import runs
    subs = [
        {"rig": "rc16", "target": "M31", "time": "2026-09-27T04:00:00",
         "exp_s": 600},
        {"rig": "piggyback", "target": "?", "time": "2026-09-27T03:55:00Z",
         "exp_s": 1200},   # started 03:35, 25 min before the RC16
    ]
    n, _, _ = runs.correlate_piggyback_records(subs)
    assert n == 0


def test_sub_start_utc_uses_start_utc():
    from photonscript.scheduler.phd2_analysis import sub_start_utc
    rec = {"file": "no_stamp.fits", "time": "2026-09-27T05:00:00Z",
           "exp_s": 60, "start_utc": "2026-09-27T04:00:00Z"}
    assert sub_start_utc(rec, SimpleNamespace()) == _dt("2026-09-27T04:00:00")
