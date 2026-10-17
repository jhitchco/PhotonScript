"""TPoint autocal live loop (docs/TPOINT_AUTOCAL.md), driven with fakes
only: a fake TheSky (script replies), a fake NINA #1, a fake scheduler and
a fake TPoint file count. Nothing here opens a socket."""
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import tpoint_autocal as ta
from photonscript.telescope_agent import tpoint_autocal_live as tal

ROOT = Path(__file__).resolve().parents[2]
T0 = datetime(2026, 10, 16, 3, 0)


def _cfg(tmp_path, **kw):
    kw.setdefault("thesky_tcp_host", "127.0.0.1")
    kw.setdefault("thesky_tcp_port", 1)
    kw.setdefault("thesky_user_dir", str(tmp_path / "nosky"))
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _apps():
    return {"sequence_running": {ta.NINA1_LABEL: [], ta.NINA2_LABEL: []},
            "stop_blockers": [],
            "thesky": {"answering": True, "mount_connected": True},
            "sun_alt_deg": -25.0}


# ------------------------------------------------------------- fakes

class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def mono(self):
        return self.t

    def now(self):
        return T0 + timedelta(seconds=self.t)


def kind(js: str) -> str:
    for k, needle in (("abort", "Abort()"), ("automap", "AutoMap()"),
                      ("slew", "SlewToAzAlt"), ("slew_poll", "IsSlewComplete"),
                      ("expose", "TakeImage()"),
                      ("expose_poll", "IsExposureComplete"),
                      ("camera_connect", "ccdsoftCamera.Connect()"),
                      ("camera_disconnect", "ccdsoftCamera.Disconnect()"),
                      ("ails_setup", "AutomatedImageLinkSettings.imageScale ="),
                      ("camera_setup", "ccdsoftCamera.ExposureTime ="),
                      ("ails_read", "fovsToSearch"),
                      ("camera_read", "cameraModel")):
        if needle in js:
            return k
    raise AssertionError(f"unexpected script {js[:80]}")


class FakeSky:
    def __init__(self, files, adds=True, solved=True, slew_done_after=2,
                 interrupt_at=None):
        self.files = files
        self.adds = adds
        self.solved = solved
        self.slew_done_after = slew_done_after
        self.interrupt_at = interrupt_at   # (kind, nth call)
        self.calls: list[str] = []
        self.scripts: list[str] = []
        self._polls = 0

    def run(self, js):
        k = kind(js)
        self.calls.append(k)
        self.scripts.append(js)
        if self.interrupt_at and k == self.interrupt_at[0] and \
                self.calls.count(k) == self.interrupt_at[1]:
            raise KeyboardInterrupt
        if k == "slew":
            self._polls = 0
            return {"err": None}
        if k == "slew_poll":
            self._polls += 1
            done = self.slew_done_after is not None and \
                self._polls >= self.slew_done_after
            return {"slew_complete": "1" if done else "0"}
        if k == "expose_poll":
            return {"complete": "1", "last_image": r"C:\img\x.fit"}
        if k == "automap":
            ok = self.solved(len([c for c in self.calls if c == "automap"])) \
                if callable(self.solved) else self.solved
            if ok and self.adds:
                self.files.total += 1
            return {"err": None, "succeeded": "1" if ok else "0",
                    "error_text": "" if ok else "no stars",
                    "solution_rms": "0.8", "image_scale": "0.472"}
        if k == "ails_read":
            return {"exposure_s": "5", "image_scale": "0.472"}
        if k == "camera_read":
            return {"camera_model": "ASCOM Camera"}
        return {"err": None, "status": "Ready"}


class Files:
    def __init__(self, total=99):
        self.total = total

    def count(self):
        return {"total": self.total, "files": {}, "newest": None}


class FakeNina:
    def __init__(self, clock, reconnects=True):
        self.connected = True
        self.reconnects = reconnects
        self.state = "IDLE"
        self.safe = True
        self.calls: list[str] = []
        self.cooled = None

    def sequence_state(self):
        return {"State": self.state}

    def camera_info(self):
        return {"Connected": self.connected, "CoolerOn": True,
                "TemperatureSetPoint": -10.0}

    def disconnect_camera(self):
        self.calls.append("disconnect")
        self.connected = False

    def connect_camera(self):
        self.calls.append("connect")
        self.connected = self.reconnects

    def cool_camera(self, t):
        self.cooled = t

    def safety(self):
        return {"Connected": True, "IsSafe": self.safe}


class FakeSched:
    def __init__(self, states=("DISARMED",)):
        self.states = list(states)
        self.n = 0

    def arm(self):
        s = self.states[min(self.n, len(self.states) - 1)]
        self.n += 1
        return {"state": s}

    def apps(self):
        return _apps()


_PTS = [{"alt": 45.0, "az": 250.0, "ha_h": 2.0, "dec_deg": 10.0, "side": "west"},
        {"alt": 50.0, "az": 280.0, "ha_h": 2.5, "dec_deg": 30.0, "side": "west"},
        {"alt": 55.0, "az": 300.0, "ha_h": 3.0, "dec_deg": 45.0, "side": "west"}]


def _run(tmp_path, answers=("yes", "yes", "yes", "yes"), sky_kw=None,
         nina_kw=None, sched=None, test=False, total=99, opts_kw=None):
    clock = Clock()
    files = Files(total)
    sky = FakeSky(files, **(sky_kw or {}))
    nina = FakeNina(clock, **(nina_kw or {}))
    out: list[str] = []
    asked: list[str] = []
    ans = list(answers)
    notes: list[str] = []

    def ask(prompt):
        asked.append(prompt)
        return ans.pop(0) if ans else "no"

    deps = tal.Deps(sky=sky, nina=nina, sched=sched or FakeSched(),
                    count=files.count,
                    list_files=lambda: {"files": [
                        {"rel": r"TPoint\Super Model Indat.dat",
                         "path": r"C:\sky\Super Model Indat.dat", "size": 1}]},
                    ask=ask, say=out.append, sleep=clock.sleep, mono=clock.mono,
                    now=clock.now, copy=lambda s, d: None, notify=notes.append)
    opts = tal.Opts(test=test, csv_path=str(tmp_path / "runs" / "x_tpoint_autocal.csv"),
                    backup_root=str(tmp_path / "bk"), **(opts_kw or {}))
    res = tal.run_loop(_cfg(tmp_path), _PTS, deps, opts)
    return res, sky, nina, out, asked, notes


def _never_unsafe(sky):
    for js in sky.scripts:
        assert not re.search(r"Sync\w*\s*\(|ClosedLoopSlew|Park|"
                             r"sky6RASCOMTele\.Connect|SetTracking", js), js


# ------------------------------------------------------------- the loop

def test_happy_path(tmp_path):
    res, sky, nina, out, asked, _ = _run(tmp_path)
    assert res["status"] == "done", res["reason"]
    assert res["attempted"] == 3 and res["added_by_file"] == 3
    assert (res["samples_before"], res["samples_after"]) == (99, 102)
    assert res["camera_returned"] is True and nina.connected
    assert nina.calls == ["disconnect", "connect"] and nina.cooled == -10.0
    c = sky.calls
    assert c.index("camera_connect") < c.index("camera_setup") < c.index("slew")
    assert c[-1] == "camera_disconnect" and "abort" not in c
    assert c.count("automap") == 3
    _never_unsafe(sky)
    assert len(asked) == 1                          # only the backup question
    text = "\n".join(out)
    assert "refit the Super Model" in text and "99 -> 102" in text
    assert text.isascii() and "\u2014" not in text
    rows = (tmp_path / "runs" / "x_tpoint_autocal.csv").read_text().splitlines()
    assert len(rows) == 4 and rows[0].startswith("utc,point")


def test_automap_adds_nothing_stops_after_point_1(tmp_path):
    res, sky, nina, out, asked, _ = _run(tmp_path, answers=("yes", "no"),
                                         sky_kw={"adds": False})
    assert res["status"] == "stopped"
    assert res["reason"] == "AutoMap did not add a sample"
    assert res["attempted"] == 1 and sky.calls.count("automap") == 1
    assert "abort" not in sky.calls          # nothing moving between points
    assert res["camera_returned"] is True and nina.calls[-1] == "connect"
    assert "AutoMap did not add a sample" in "\n".join(out)


def test_file_not_live_but_operator_saw_it(tmp_path):
    res, sky, *_ = _run(tmp_path, answers=("yes", "yes"), sky_kw={"adds": False})
    assert res["status"] == "done"
    assert res["added_by_eye"] == 1 and res["added_by_file"] == 0
    assert sky.calls.count("automap") == 3


def test_test_mode_pauses_after_point_1_even_when_file_grows(tmp_path):
    res, sky, nina, out, asked, _ = _run(tmp_path, answers=("yes", "yes"), test=True)
    assert res["status"] == "done"
    assert len(asked) == 2 and "New sample" in asked[1]
    assert any("TheSky <- automap" in line for line in out)    # verbose


def test_two_unsolved_in_a_row_stop(tmp_path):
    res, sky, *_ = _run(tmp_path, sky_kw={"solved": False})
    assert res["status"] == "stopped" and "2 AutoMap failures" in res["reason"]
    assert res["attempted"] == 2 and res["camera_returned"] is True


def test_clouded_first_point_then_confirmed_on_the_next(tmp_path):
    res, sky, *_ = _run(tmp_path, sky_kw={"solved": lambda n: n != 1})
    assert res["status"] == "done"
    assert res["solved"] == 2 and res["added_by_file"] == 2


def test_slew_timeout_aborts_and_returns_camera(tmp_path):
    res, sky, nina, *_ = _run(tmp_path, sky_kw={"slew_done_after": None},
                              opts_kw={"slew_timeout_s": 30})
    assert res["status"] == "stopped" and "did not finish in 30 s" in res["reason"]
    assert "abort" in sky.calls and "expose" not in sky.calls
    assert sky.calls[-1] == "camera_disconnect"
    assert nina.connected and res["rows"][0]["note"] == "slew timeout"


def test_ctrl_c_aborts_and_returns_camera(tmp_path):
    res, sky, nina, out, *_ = _run(tmp_path, sky_kw={"interrupt_at": ("expose", 2)})
    assert res["status"] == "stopped" and res["reason"] == "operator Ctrl+C"
    i = sky.calls.index("abort")
    assert sky.calls[i:] == ["abort", "camera_disconnect"]
    assert nina.calls == ["disconnect", "connect"] and nina.connected
    assert res["attempted"] == 1


def test_armer_leaving_disarmed_mid_run_stops(tmp_path):
    sched = FakeSched(["DISARMED", "DISARMED", "DISARMED", "ARMED"])
    res, sky, nina, *_ = _run(tmp_path, sched=sched)
    assert res["status"] == "stopped" and "left DISARMED" in res["reason"]
    assert res["attempted"] == 2 and nina.connected


def test_camera_not_returned_is_loud(tmp_path):
    res, sky, nina, out, asked, notes = _run(tmp_path, nina_kw={"reconnects": False})
    assert res["camera_returned"] is False
    assert notes and "did NOT get the camera back" in notes[0]
    assert "NO, fix by hand" in "\n".join(out)


# ------------------------------------------------------------- refusals

@pytest.mark.parametrize("what", ["armed", "roof", "nina_running", "auto_arm",
                                  "no_yes"])
def test_refusals_send_nothing(tmp_path, what):
    kw: dict = {}
    if what == "armed":
        kw["sched"] = FakeSched(["ARMED"])
    if what == "auto_arm":
        kw["opts_kw"] = {"auto_arm_enabled": True}
    if what == "no_yes":
        kw["answers"] = ("no",)
    clock = Clock()
    if what in ("roof", "nina_running"):
        # patch the fake through the scheduler / NINA readings
        class Sched(FakeSched):
            def apps(self):
                a = _apps()
                if what == "nina_running":
                    a["sequence_running"][ta.NINA1_LABEL] = ["Smart Exposure"]
                return a
        kw["sched"] = Sched()
    res, sky, nina, out, *_ = _run(tmp_path, **kw) if what != "roof" else \
        _run_roof_unsafe(tmp_path, clock)
    assert res["status"] == "refused"
    assert sky.calls == [] and nina.calls == []
    assert res["camera_returned"] is None


def _run_roof_unsafe(tmp_path, clock):
    class Nina(FakeNina):
        def safety(self):
            return {"Connected": True, "IsSafe": False}
    files = Files()
    sky = FakeSky(files)
    nina = Nina(clock)
    out: list[str] = []
    deps = tal.Deps(sky=sky, nina=nina, sched=FakeSched(), count=files.count,
                    list_files=lambda: {"files": [{"rel": "TPoint\\a in.dat",
                                                   "path": "a", "size": 1}]},
                    ask=lambda p: "yes", say=out.append, sleep=clock.sleep,
                    mono=clock.mono, now=clock.now, copy=lambda s, d: None)
    res = tal.run_loop(_cfg(tmp_path), _PTS, deps, tal.Opts())
    return res, sky, nina, out


# ------------------------------------------------------------- the points

def _q(ha, dec, side, alt=45.0, az=200.0):
    return {"alt": alt, "az": az, "ha_h": ha, "dec_deg": dec, "side": side}


def test_side_filter_and_margin(tmp_path):
    cfg = _cfg(tmp_path)
    pts = [_q(-3.0, 10, "east"), _q(-0.3, 20, "east"), _q(0.3, 20, "west"),
           _q(2.0, 10, "west"), _q(3.0, 40, "west")]
    w = tal.select_points(cfg, 10, "west", T0, 5.0, sky_points=pts)
    assert [q["ha_h"] for q in w["points"]] == [2.0, 3.0]
    assert w["dropped"]["side"] == 3                  # 2 east + 1 near the meridian
    e = tal.select_points(cfg, 10, "east", T0, 5.0, sky_points=pts)
    assert [q["ha_h"] for q in e["points"]] == [-3.0]
    b = tal.select_points(cfg, 10, "both", T0, 5.0, sky_points=pts)
    assert [q["side"] for q in b["points"]][0] == "east"
    with pytest.raises(ValueError):
        tal.select_points(cfg, 3, "north", T0, sky_points=pts)


def test_visibility_during_the_run(tmp_path):
    cfg = _cfg(tmp_path)   # min alt 30, lat 31.9
    lat = cfg.observatory_lat
    # an east point whose dwell carries it into the meridian band
    edge = _q(-0.25 - 0.005, 10, "east")
    # a west point that sets below 30 deg during its dwell: find the HA where
    # Dec 0 sits just above 30 deg
    ha = 3.8
    while tal._alt_of(ha + 0.001, 0.0, lat) > 30.0:
        ha += 0.001
    setting = _q(round(ha - 0.005, 4), 0.0, "west",
                 alt=round(tal._alt_of(ha - 0.005, 0.0, lat), 3))
    ok = _q(2.0, 20, "west")
    sel = tal.select_points(cfg, 10, "both", T0, 5.0, sky_points=[edge, setting, ok])
    assert sel["points"] == [{**ok, "eta_utc": "2026-10-16T03:00:00Z"}]
    assert sel["dropped"]["sets_or_meridian"] == 2


def test_dawn_cuts_the_tail(tmp_path):
    cfg = _cfg(tmp_path)
    pts = [_q(1.0 + i * 0.2, 10 + i, "west") for i in range(10)]
    sel = tal.select_points(cfg, 10, "west", T0, 5.0, sky_points=pts,
                            dark_end=T0 + timedelta(seconds=40 * 4 + 1))
    assert len(sel["points"]) == 4 and sel["dropped"]["dawn"] == 6
    assert sel["points"][1]["eta_utc"] == "2026-10-16T03:00:40Z"


def test_grid_selection_is_west_serpentine(tmp_path):
    cfg = _cfg(tmp_path)
    sel = tal.select_points(cfg, 12, "west", T0, 5.0, moon_altaz=[])
    pts = sel["points"]
    assert len(pts) == 12
    assert all(q["side"] == "west" and q["ha_h"] >= tal.SIDE_MARGIN_H for q in pts)
    assert all(q["alt"] >= 30.0 for q in pts)
    strips = [int(q["ha_h"] // 1) for q in pts]
    assert strips == sorted(strips)                   # walked strip by strip
    assert "12 point(s), side west" in tal.format_points(sel)


def test_from_coverage_falls_back(tmp_path):
    cfg = _cfg(tmp_path)
    sel = tal.select_points(cfg, 5, "west", T0, 5.0, moon_altaz=[],
                            from_coverage=True)
    assert sel["source"] == "grid" and "grid + side filter" in sel["note"]
    assert len(sel["points"]) == 5


# ------------------------------------------------------------- fences / CLI

def test_live_module_fences():
    src = (ROOT / "photonscript" / "telescope_agent" /
           "tpoint_autocal_live.py").read_text(encoding="utf-8")
    assert src.isascii() and "\u2014" not in src
    for bad in ("Sync", "Park", "ClosedLoopSlew", "start_sequence",
                "load_sequence", "httpx.post", ".post(", "SetTracking"):
        assert bad not in src, bad
    # no JavaScript of its own: every TheSky script comes from the builders
    assert "sky6RASCOMTele." not in src and "ccdsoftCamera." not in src


def test_sample_count_reads_in_dat_only(tmp_path):
    d = tmp_path / "sky" / "TPoint"
    d.mkdir(parents=True)
    body = ("caption\n:NODA\n+31 54 25 2026 10 14 10 1010 1500 0.5 0.55 0.0065\n"
            + "".join(f"{i} 0 0 +10 0 0 {i} 0 0 +10 0 0 0 0\n" for i in range(5))
            + "END\n")
    (d / "Super Model Indat.dat").write_text(body)
    (d / "Super Model Outdat.dat").write_text(body)
    cfg = _cfg(tmp_path, thesky_user_dir=str(tmp_path / "sky"))
    c = tal.sample_count(cfg)
    assert c["total"] == 5 and list(c["files"]) == [r"TPoint\Super Model Indat.dat"]
    assert tal.sample_count(_cfg(tmp_path))["total"] is None


def test_cli_go_window_is_refused_and_go_loop_uses_the_live_runner(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    import socket
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)

    def boom(*a, **k):
        raise AssertionError("opened a connection")
    monkeypatch.setattr(socket, "create_connection", boom)
    r = CliRunner().invoke(cli.app, ["tpoint-autocal", "--go", "--mode", "window"])
    assert r.exit_code == 2 and "not implemented" in r.output
    r = CliRunner().invoke(cli.app, ["tpoint-autocal", "--go", "--side", "up"])
    assert r.exit_code == 2

    seen = {}
    clock = Clock()

    def fake_deps(config, url, ask=input, say=print):
        files = Files()
        seen["sky"] = FakeSky(files)
        return tal.Deps(sky=seen["sky"], nina=FakeNina(clock),
                        sched=FakeSched(["ARMED"]), count=files.count,
                        list_files=lambda: {"files": []}, ask=lambda p: "yes",
                        say=say, sleep=clock.sleep, mono=clock.mono,
                        now=clock.now)
    monkeypatch.setattr(tal, "live_deps", fake_deps)
    monkeypatch.setattr(tal, "dark_end", lambda c, n=None: None)
    monkeypatch.setattr(tal.tm, "moon_track", lambda *a, **k: [])
    r = CliRunner().invoke(cli.app, ["tpoint-autocal", "--go", "--test"])
    assert r.exit_code == 1, r.output
    assert "TEST: 3 west points" in r.output and "NO GO" in r.output
    assert "3 point(s), side west" in r.output
    assert seen["sky"].calls == []
