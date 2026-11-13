"""PS-55: event-loop lag monitor, stall watchdog, process context, IERS pin."""

from __future__ import annotations

import asyncio
import sys
import threading
import time

import pytest

from photonscript.shared import health


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_record_tracks_last_and_window_max():
    c = Clock()
    m = health.LoopMonitor(window_s=300, clock=c)
    m.record(1000.0, 0.01)
    m.record(1100.0, 3.2)
    m.record(1200.0, 0.02)
    s = m.stats(now=1200.0)
    assert s["lag_ms"] == 20.0
    assert s["max_lag_ms_5min"] == 3200.0
    # the 3.2 s sample ages out of the 5 min window
    m.record(1500.0, 0.0)
    assert m.stats(now=1500.0)["max_lag_ms_5min"] == 20.0


def test_negative_lag_clamped():
    m = health.LoopMonitor(clock=Clock())
    m.record(1000.0, -0.3)
    assert m.stats(now=1000.0)["lag_ms"] == 0.0


def test_big_lag_logs_warning_rate_limited(caplog):
    m = health.LoopMonitor(warn_lag_s=2.0, repeat_s=60, clock=Clock())
    with caplog.at_level("WARNING"):
        m.record(1000.0, 5.0)
        m.record(1010.0, 5.0)      # within repeat_s: no second warning
        m.record(1100.0, 5.0)
    assert sum("Event loop lag" in r.message for r in caplog.records) == 2


def test_stall_detected_with_stack_and_repeat():
    c = Clock()
    m = health.LoopMonitor(stall_s=5, repeat_s=60, clock=c)
    m.record(1000.0, 0.0)
    m.loop_thread_id = threading.get_ident()
    assert m.check_stall(1004.0) is None                 # not stalled yet
    rep = m.check_stall(1006.0)
    assert rep and "STALLED for 6.0 s (new)" in rep
    assert "test_stall_detected_with_stack_and_repeat" in rep  # our frame
    assert m.check_stall(1030.0) is None                 # already reported
    rep2 = m.check_stall(1067.0)
    assert rep2 and "(still)" in rep2
    s = m.stats(now=1067.0)
    assert s["stalls_5min"] == 1 and s["last_stall_at"]
    assert s["heartbeat_age_s"] == 67.0
    # loop ticks again: episode over, a new stall counts again
    m.record(1070.0, 0.0)
    assert m.check_stall(1076.0).startswith("Event loop STALLED")
    assert m.stats(now=1076.0)["stalls_5min"] == 2


def test_stall_without_known_thread():
    m = health.LoopMonitor(stall_s=1, clock=Clock())
    m.record(1000.0, 0.0)
    assert "(loop thread unknown)" in m.check_stall(1002.0)


def test_watchdog_thread_catches_a_real_blocked_loop(tmp_path):
    """A coroutine that blocks the loop with time.sleep shows up in
    stalls.log with its own frame in the stack."""
    log = tmp_path / "logs" / "stalls.log"
    m = health.LoopMonitor(interval_s=0.05, stall_s=0.3, repeat_s=60,
                           stall_log=log)

    def blocking_call_under_test():
        time.sleep(1.0)

    async def main():
        task = asyncio.create_task(m.run())
        m.start_watchdog(poll_s=0.05)
        await asyncio.sleep(0.2)
        blocking_call_under_test()     # blocks the event loop
        await asyncio.sleep(0.2)
        m.stop()
        task.cancel()

    asyncio.run(main())
    text = log.read_text(encoding="utf-8")
    assert "Event loop STALLED" in text
    assert "blocking_call_under_test" in text
    assert m.stats()["stalls_5min"] == 1


def test_process_context_basic_fields(monkeypatch):
    monkeypatch.setenv("PS_LAUNCHER", "task-S4U")
    ctx = health.process_context()
    assert ctx["pid"] > 0 and ctx["user"]
    assert ctx["launcher"] == "task-S4U"


@pytest.mark.skipif(sys.platform == "win32", reason="non-Windows path")
def test_qos_guard_is_a_noop_off_windows():
    assert health.apply_process_qos() == {"applied": False, "reason": "not Windows"}


def test_iers_offline_pins_config_and_never_downloads(monkeypatch):
    """With the bundled table 45 days stale and every download failing (the
    2026-09-26 WinError 5 case), transforms must neither download nor raise."""
    import astropy.units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time
    from astropy.utils import iers
    import astropy.utils.iers.iers as iersmod

    monkeypatch.setattr(iers.conf, "auto_download", True)
    monkeypatch.setattr(iers.conf, "auto_max_age", 30.0)
    calls = []

    def fail_download(*a, **k):
        calls.append(a)
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(iersmod, "download_file", fail_download)
    info = health.configure_astropy_iers(offline=True)
    assert info["offline"] and info["auto_download"] is False
    assert iers.conf.auto_max_age is None
    assert info.get("predictions_from")

    pm = iers.IERS_Auto.open().meta["predictive_mjd"]
    fut = Time(pm + 45, format="mjd")
    monkeypatch.setattr(Time, "now", classmethod(lambda cls: fut))
    loc = EarthLocation(lat=31.9 * u.deg, lon=-109 * u.deg, height=1250 * u.m)
    SkyCoord(ra=10 * u.deg, dec=20 * u.deg).transform_to(
        AltAz(obstime=fut, location=loc))
    assert calls == []


def test_snapshot_shape():
    health.mark_started("scheduler")
    snap = health.snapshot()
    assert snap["mode"] == "scheduler" and snap["uptime_s"] >= 0
    assert snap["pid"] > 0 and "process" in snap and "iers" in snap


def test_cli_callback_pins_iers_offline_for_every_command(monkeypatch):
    """PS-62: any CLI command (not only `start`) pins IERS offline, cheaply
    (probe=False, no bundled-table read), and PS_IERS_OFFLINE=0 opts out."""
    from typer.testing import CliRunner
    from photonscript import cli

    seen = []
    monkeypatch.setattr(health, "configure_astropy_iers",
                        lambda offline=True, probe=True: seen.append((offline, probe)) or {})
    r = CliRunner().invoke(cli.app, ["notify", "--help"])
    assert r.exit_code == 0, r.output
    assert seen == [(True, False)]

    seen.clear()
    monkeypatch.setenv("PS_IERS_OFFLINE", "0")
    CliRunner().invoke(cli.app, ["notify", "--help"])
    assert seen == []


def test_iers_pin_without_probe_skips_table_read(monkeypatch):
    from astropy.utils import iers
    monkeypatch.setattr(iers.conf, "auto_download", True)
    monkeypatch.setattr(iers.conf, "auto_max_age", 30.0)
    monkeypatch.setattr(iers.IERS_Auto, "open",
                        classmethod(lambda cls, *a, **k: (_ for _ in ()).throw(AssertionError("read"))))
    info = health.configure_astropy_iers(offline=True, probe=False)
    assert info == {"offline": True, "auto_download": False}
    assert iers.conf.auto_max_age is None
