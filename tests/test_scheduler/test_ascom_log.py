"""ASCOM trace-log finder: newest file across dated subfolders, name filter."""

import time
from pathlib import Path

from photonscript.scheduler.app import _latest_ascom_log


def test_finds_newest_across_dated_subfolders(tmp_path):
    base = tmp_path / "ASCOM"
    d1 = base / "Logs 2026-09-12"; d1.mkdir(parents=True)
    d2 = base / "Logs 2026-09-13"; d2.mkdir(parents=True)
    old = d1 / "ASCOM.AlpacaDynamic1.SafetyMonitor.120000.txt"
    old.write_text("old safety trace")
    new = d2 / "ASCOM.AlpacaDynamic1.SafetyMonitor.010000.txt"
    new.write_text("new safety trace")
    import os
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    got = _latest_ascom_log(str(base), name="Safety")
    assert got == new


def test_name_filter_and_missing(tmp_path):
    base = tmp_path / "ASCOM"
    d = base / "Logs 2026-09-13"; d.mkdir(parents=True)
    (d / "ASCOM.Telescope.txt").write_text("mount trace")
    # name filter excludes non-matching device
    assert _latest_ascom_log(str(base), name="Safety") is None
    # blank name picks any
    assert _latest_ascom_log(str(base), name="") is not None
    # missing base
    assert _latest_ascom_log(str(tmp_path / "nope"), name="") is None
