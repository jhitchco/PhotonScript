"""tpoint-autocal --unpark (2026-10-24): on 2026-10-21 and 2026-10-23 the
night loop / dusk flats left the mount parked and TheSky refused the slew to
point 1 with Error 216. --unpark lets NINA #1 unpark it after the full
preflight and a fresh safety read, then waits for AtPark false. Fakes only
(the loop test's fakes plus a shared fake mount); nothing opens a socket."""
import re

from photonscript.scheduler import remote_cli as rc
from photonscript.telescope_agent import tpoint_autocal as ta
from photonscript.telescope_agent import tpoint_autocal_live as tal

from tests.test_scheduler.test_tpoint_autocal_loop import (
    Clock, FakeNina, FakeSched, FakeSky, Files, _PTS, _cfg)

PARKED_ERR = ("Error, the device is parked and must be unparked using the "
              "Unpark command before proceeding. Error _ 216.")


class Mount:
    def __init__(self, parked=True, unpark_after=2, sky_connected=True):
        self.parked = parked
        self.unpark_after = unpark_after   # NINA mount reads after the unpark; None = never
        self.sky_connected = sky_connected
        self.unpark_sent = False
        self.reads = 0

    def tick(self):
        if self.unpark_sent and self.parked and self.unpark_after is not None:
            self.reads += 1
            if self.reads >= self.unpark_after:
                self.parked = False


class ParkSky(FakeSky):
    def __init__(self, files, mount, **kw):
        super().__init__(files, **kw)
        self.mount = mount

    def run(self, js):
        if "IsParked()" in js:
            self.calls.append("park_read")
            self.scripts.append(js)
            if not self.mount.sky_connected:
                return {"connected": "false", "parked": None}
            return {"connected": "true", "parked": "true" if self.mount.parked else "false"}
        if "SlewToAzAlt" in js and self.mount.parked:
            self.calls.append("slew")
            self.scripts.append(js)
            return {"err": PARKED_ERR}
        return super().run(js)


class ParkNina(FakeNina):
    def __init__(self, clock, mount, safe_seq=None):
        super().__init__(clock)
        self.mount = mount
        self.safe_seq = list(safe_seq or [])
        self.safety_reads = 0

    def safety(self):
        self.safety_reads += 1
        s = self.safe_seq.pop(0) if self.safe_seq else self.safe
        return {"Connected": True, "IsSafe": s}

    def mount_info(self):
        self.mount.tick()
        return {"Connected": True, "AtPark": self.mount.parked}

    def unpark_mount(self):
        self.calls.append("unpark")
        self.mount.unpark_sent = True
        return {"Success": True}


def _run(tmp_path, mount, unpark=True, safe_seq=None, opts_kw=None):
    clock = Clock()
    files = Files()
    sky = ParkSky(files, mount)
    nina = ParkNina(clock, mount, safe_seq)
    out: list[str] = []
    deps = tal.Deps(sky=sky, nina=nina, sched=FakeSched(), count=files.count,
                    list_files=lambda: {"files": [
                        {"rel": r"TPoint\Super Model Indat.dat",
                         "path": r"C:\sky\Super Model Indat.dat", "size": 1}]},
                    ask=lambda p: "yes", say=out.append, sleep=clock.sleep,
                    mono=clock.mono, now=clock.now, copy=lambda s, d: None,
                    notify=lambda m: None)
    opts = tal.Opts(csv_path=str(tmp_path / "runs" / "x_tpoint_autocal.csv"),
                    backup_root=str(tmp_path / "bk"), add="automap",
                    unpark=unpark, **(opts_kw or {}))
    res = tal.run_loop(_cfg(tmp_path), _PTS, deps, opts)
    return res, sky, nina, "\n".join(out)


def _never_parks(sky):
    for js in sky.scripts:
        assert not re.search(r"\.Park\(|Unpark\(|Sync\w*\s*\(|ClosedLoopSlew|"
                             r"sky6RASCOMTele\.Connect|SetTracking", js), js


def test_parked_and_safe_unparks_then_runs(tmp_path):
    m = Mount(parked=True, unpark_after=3)
    res, sky, nina, text = _run(tmp_path, m)
    assert res["status"] == "done", res["reason"]
    assert res["unparked"] is True and "unparked by NINA #1" in res["unpark"]
    # the unpark comes after the camera hand-off, before the first slew;
    # the camera goes back at the end; nothing parks
    assert nina.calls == ["disconnect", "unpark", "connect"]
    assert sky.calls[0] == "park_read" and "slew" in sky.calls
    assert res["attempted"] == 3 and res["camera_returned"] is True
    assert not m.parked
    _never_parks(sky)
    assert "never parks" in text and text.isascii() and "\u2014" not in text
    rows = (tmp_path / "runs" / "x_tpoint_autocal.csv").read_text().splitlines()
    assert ",unpark," in rows[0] and "unparked by NINA #1" in rows[1]


def test_parked_and_unsafe_refuses_and_sends_nothing(tmp_path):
    m = Mount(parked=True)
    # the preflight reads safe, the re-read after the park read does not
    res, sky, nina, text = _run(tmp_path, m, safe_seq=[True, False])
    assert res["status"] == "refused" and "not unparking" in res["reason"]
    # one park read in the step 1 preflight, one in step 2b
    assert nina.calls == [] and sky.calls == ["park_read", "park_read"]
    assert res["camera_returned"] is None and m.parked and not m.unpark_sent


def test_not_parked_makes_no_unpark_call(tmp_path):
    m = Mount(parked=False)
    res, sky, nina, text = _run(tmp_path, m)
    assert res["status"] == "done", res["reason"]
    assert "unpark" not in nina.calls and res["unparked"] is False
    assert nina.calls == ["disconnect", "connect"]
    assert "nothing to unpark" in text


def test_park_state_falls_back_to_nina_when_thesky_has_no_mount(tmp_path):
    m = Mount(parked=True, unpark_after=1, sky_connected=False)
    res, sky, nina, text = _run(tmp_path, m)
    assert res["status"] == "done", res["reason"]
    assert "parked (NINA #1)" in text and "unpark" in nina.calls


def test_unpark_timeout_stops_and_gives_the_camera_back(tmp_path):
    m = Mount(parked=True, unpark_after=None)
    res, sky, nina, text = _run(tmp_path, m)
    assert res["status"] == "stopped"
    assert f"still reads parked {ta.UNPARK_TIMEOUT_S:.0f} s" in res["reason"]
    assert nina.calls == ["disconnect", "unpark", "connect"]
    assert res["camera_returned"] is True and nina.connected
    assert "slew" not in sky.calls and sky.calls[-1] == "camera_disconnect"
    assert res["attempted"] == 0 and "unpark timeout" in res["unpark"]


def test_roof_unsafe_during_the_unpark_stops(tmp_path):
    m = Mount(parked=True, unpark_after=None)
    # preflight, park re-read, before the request: safe; then the first poll: unsafe
    res, sky, nina, text = _run(tmp_path, m, safe_seq=[True, True, True, False])
    assert res["status"] == "stopped" and "UNSAFE" in res["reason"]
    assert nina.calls == ["disconnect", "unpark", "connect"]
    assert res["camera_returned"] is True and "slew" not in sky.calls


def test_without_unpark_a_parked_mount_is_refused_in_the_preflight(tmp_path):
    # 2026-10-28: the refusal moved from point 1 (Error 216, after the camera
    # was lent) to the step 1 preflight
    m = Mount(parked=True)
    res, sky, nina, text = _run(tmp_path, m, unpark=False)
    assert res["status"] == "refused"
    assert "mount not parked" in res["reason"] and "re-run with --unpark" in res["reason"]
    assert "[FAIL   ] mount not parked: parked (TheSky)" in text
    assert nina.calls == [] and sky.calls == ["park_read"]
    assert res["camera_returned"] is None and res["unpark"] is None and m.parked


def test_parked_at_point_1_still_says_rerun_with_unpark(tmp_path):
    # park state unreadable in the preflight (a warning, as before), parked
    # by the time of the slew: the old point 1 stop and its hint still hold
    m = Mount(parked=False, sky_connected=False)

    class LateSky(ParkSky):
        def run(self, js):
            if "SlewToAzAlt" in js:
                self.mount.parked = True
            return super().run(js)

    class NoMountNina(ParkNina):
        def mount_info(self):
            return {"Connected": False}
    clock = Clock()
    files = Files()
    sky = LateSky(files, m)
    out: list[str] = []
    deps = tal.Deps(sky=sky, nina=NoMountNina(clock, m), sched=FakeSched(),
                    count=files.count,
                    list_files=lambda: {"files": [
                        {"rel": r"TPoint\Super Model Indat.dat",
                         "path": r"C:\sky\Super Model Indat.dat", "size": 1}]},
                    ask=lambda p: "yes", say=out.append, sleep=clock.sleep,
                    mono=clock.mono, now=clock.now, copy=lambda s, d: None,
                    notify=lambda msg: None)
    res = tal.run_loop(_cfg(tmp_path), _PTS, deps,
                       tal.Opts(add="automap", csv_path=None,
                                backup_root=str(tmp_path / "bk")))
    assert "[WARN   ] mount not parked: park state unreadable" in "\n".join(out)
    assert res["status"] == "stopped"
    assert "Error _ 216" in res["reason"] and "re-run with --unpark" in res["reason"]
    assert res["camera_returned"] is True


def test_is_parked_error():
    assert ta.is_parked_error(PARKED_ERR)
    assert ta.is_parked_error("Error = 216.")
    assert not ta.is_parked_error("Error _ 2160") and not ta.is_parked_error("")
    assert not ta.is_parked_error("Error _ 224")
    js = ta.mount_park_state_script()
    assert "IsParked()" in js and not re.search(r"\.Park\(|Unpark\(|Connect\(", js)


def test_unpark_flag_is_remote_allowed_but_not_a_write_flag():
    cmd = rc.load_allowlist().commands["tpoint-autocal"]
    assert "--unpark" in cmd.flags and cmd.write_flags == ("--go",)


def test_cli_passes_unpark_to_the_loop(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)
    monkeypatch.setattr(tal, "live_deps", lambda *a, **k: None)
    monkeypatch.setattr(tal, "dark_end", lambda c, n=None: None)
    monkeypatch.setattr(tal.tm, "moon_track", lambda *a, **k: [])
    seen = {}

    def fake_run(config, points, deps, opts):
        seen["opts"] = opts
        return {"status": "done"}
    monkeypatch.setattr(tal, "run_loop", fake_run)
    r = CliRunner().invoke(cli.app, ["tpoint-autocal", "--go", "--unpark",
                                     "--unattended", "--points", "6"])
    assert r.exit_code == 0, r.output
    assert seen["opts"].unpark is True and "never parked" in r.output
    r = CliRunner().invoke(cli.app, ["tpoint-autocal", "--go", "--unattended",
                                     "--points", "6"])
    assert seen["opts"].unpark is False


def test_unpark_reply_timeout_still_polls_and_a_refused_request_stops(tmp_path):
    class ReadTimeout(Exception):
        pass

    class SlowNina(ParkNina):
        def unpark_mount(self):
            super().unpark_mount()
            raise ReadTimeout("timed out")
    clock = Clock()
    m = Mount(parked=True, unpark_after=2)
    files = Files()
    sky = ParkSky(files, m)
    nina = SlowNina(clock, m)
    deps = tal.Deps(sky=sky, nina=nina, sched=FakeSched(), count=files.count,
                    list_files=lambda: {"files": [
                        {"rel": r"TPoint\Super Model Indat.dat",
                         "path": r"C:\sky\Super Model Indat.dat", "size": 1}]},
                    ask=lambda p: "yes",
                    say=lambda s: None, sleep=clock.sleep, mono=clock.mono,
                    now=clock.now, copy=lambda s, d: None)
    res = tal.run_loop(_cfg(tmp_path), _PTS, deps,
                       tal.Opts(add="automap", unpark=True, csv_path=None,
                                backup_root=str(tmp_path / "bk")))
    assert res["status"] == "done", res["reason"]
    assert res["unparked"] is True

    class BadNina(ParkNina):
        def unpark_mount(self):
            self.calls.append("unpark")
            raise RuntimeError("500 Mount not connected")
    m = Mount(parked=True)
    nina = BadNina(clock, m)
    deps.nina, deps.sky = nina, ParkSky(files, m)
    res = tal.run_loop(_cfg(tmp_path), _PTS, deps,
                       tal.Opts(add="automap", unpark=True, csv_path=None,
                                backup_root=str(tmp_path / "bk")))
    assert res["status"] == "stopped" and "unpark request failed" in res["reason"]
    assert nina.calls == ["disconnect", "unpark", "connect"] and res["camera_returned"]
