"""tpoint-autocal setup fixes after 2026-10-28 (two runs solved 0 points,
the third, with L in the beam and bin 1 / 10 s, solved 12 of 12): --filter
(NINA #1's wheel to L before the lend), bin 1 / 10 s defaults and the real
binning of the saved frame, --stop-nina2 (implies --unpark), the parked
mount refused in the step 1 preflight, and each solved point's offset from
the commanded alt / az. Fakes only; nothing opens a socket."""
import math
from datetime import datetime

import pytest

from photonscript.scheduler import remote_cli as rc
from photonscript.scheduler import tpoint_mapping as tm
from photonscript.telescope_agent import tpoint_autocal as ta
from photonscript.telescope_agent import tpoint_autocal_live as tal
from photonscript.telescope_agent import tpoint_import as ti

from tests.test_scheduler.test_tpoint_autocal_import import ImportSky
from tests.test_scheduler.test_tpoint_autocal_loop import (
    _PTS, Clock, FakeNina, FakeSched, FakeSky, Files, _apps, _cfg)
from tests.test_scheduler.test_tpoint_autocal_unpark import Mount, ParkNina, ParkSky

LIST = {"files": [{"rel": r"TPoint\Super Model Indat.dat",
                   "path": r"C:\sky\Super Model Indat.dat", "size": 1}]}


def _deps(clock, files, sky, nina, out, **kw):
    return tal.Deps(sky=sky, nina=nina, sched=kw.pop("sched", FakeSched()),
                    count=files.count, list_files=lambda: LIST,
                    ask=lambda p: "yes", say=out.append, sleep=clock.sleep,
                    mono=clock.mono, now=clock.now, copy=lambda s, d: None,
                    notify=lambda m: None, **kw)


def _opts(tmp_path, **kw):
    kw.setdefault("add", "automap")
    kw.setdefault("csv_path", None)
    return tal.Opts(backup_root=str(tmp_path / "bk"), **kw)


# ------------------------------------------------------------- filter

WHEEL = [{"Id": 0, "Name": "Lum"}, {"Id": 1, "Name": "R"}, {"Id": 2, "Name": "G"},
         {"Id": 3, "Name": "B"}, {"Id": 4, "Name": "Ha 3nm"}]


def test_find_filter_by_name():
    assert tal.find_filter(WHEEL, "R")["Id"] == 1
    assert tal.find_filter(WHEEL, " r ")["Id"] == 1
    assert tal.find_filter(WHEEL, "ha 3NM")["Id"] == 4
    # L, Lum and Luminance stand for each other
    for name in ("L", "l", "Lum", "LUMINANCE"):
        assert tal.find_filter(WHEEL, name)["Id"] == 0
    assert tal.find_filter([{"Id": 7, "Name": "Luminance"}], "L")["Id"] == 7
    # an exact name wins over the alias
    two = [{"Id": 5, "Name": "Lum"}, {"Id": 6, "Name": "L"}]
    assert tal.find_filter(two, "L")["Id"] == 6
    assert tal.find_filter(WHEEL, "OIII") is None and tal.find_filter(None, "L") is None
    assert tal.find_filter(WHEEL, "R")["Name"] == "R"


def test_filter_in_place():
    want = {"Id": 0, "Name": "Lum"}
    assert tal.filter_in_place({"SelectedFilter": {"Id": 0, "Name": "Lum"},
                                "IsMoving": False}, want)
    assert not tal.filter_in_place({"SelectedFilter": {"Id": 0, "Name": "Lum"},
                                    "IsMoving": True}, want)
    assert not tal.filter_in_place({"SelectedFilter": {"Id": 4, "Name": "Ha 3nm"},
                                    "IsMoving": False}, want)
    assert tal.filter_in_place({"SelectedFilter": "lum", "IsMoving": False}, want)
    assert not tal.filter_in_place({}, want)


class FilterNina(FakeNina):
    """NINA #1 with a wheel that, after change-filter, reads the OLD filter
    (not moving) for `stale` reads, then moving for `moving` reads, then
    the new one (never, with reach=False)."""

    def __init__(self, clock, start=4, stale=3, moving=2, reach=True, wheel=WHEEL):
        super().__init__(clock)
        self.sel = start
        self.stale, self.moving, self.reach = stale, moving, reach
        self.wheel = wheel
        self.target = None
        self.after = 0
        self.reads = 0

    def _name(self, fid):
        return next(f["Name"] for f in self.wheel if f["Id"] == fid)

    def filter_info(self):
        self.reads += 1
        mv = False
        if self.target is not None:
            self.after += 1
            if self.after > self.stale + self.moving and self.reach:
                self.sel = self.target
            elif self.after > self.stale:
                mv = True
        return {"Connected": True, "AvailableFilters": self.wheel, "IsMoving": mv,
                "SelectedFilter": {"Id": self.sel, "Name": self._name(self.sel)}}

    def change_filter(self, fid):
        self.calls.append(f"filter {fid}")
        self.target = fid


def _filter_run(tmp_path, nina, **opts_kw):
    clock = Clock()
    files = Files()
    sky = FakeSky(files)
    out: list[str] = []
    res = tal.run_loop(_cfg(tmp_path), _PTS, _deps(clock, files, sky, nina, out),
                       _opts(tmp_path, **opts_kw))
    return res, sky, nina, "\n".join(out), clock


def test_filter_change_waits_out_the_stale_reads(tmp_path):
    nina = FilterNina(Clock(), start=4, stale=3, moving=2)
    res, sky, nina, text, clock = _filter_run(tmp_path, nina)
    assert res["status"] == "done", res["reason"]
    # the wheel goes first, then the camera is lent; the stale "Ha 3nm, not
    # moving" reads right after the call do not count as done
    assert nina.calls == ["filter 0", "disconnect", "connect"]
    assert nina.after == 3 + 2 + 1 and nina.sel == 0
    assert res["filter"] == "Lum" and "Ha 3nm in the beam; changing to Lum" in text
    assert "Lum in the beam after" in text


def test_filter_already_in_place_sends_no_change(tmp_path):
    nina = FilterNina(Clock(), start=0)
    res, sky, nina, text, _ = _filter_run(tmp_path, nina, filter="Luminance")
    assert res["status"] == "done" and nina.calls == ["disconnect", "connect"]
    assert "Lum is already in the beam" in text


def test_filter_never_arrives_stops_before_the_lend(tmp_path):
    nina = FilterNina(Clock(), start=4, reach=False)
    res, sky, nina, text, clock = _filter_run(tmp_path, nina)
    assert res["status"] == "stopped"
    assert "did not reach Lum within 60 s" in res["reason"]
    assert "not lending the camera" in res["reason"]
    assert nina.calls == ["filter 0"] and res["camera_returned"] is None
    assert sky.calls == [] and clock.t >= tal.FILTER_WAIT_S


def test_unknown_filter_or_no_wheel_stops_safely(tmp_path):
    nina = FilterNina(Clock())
    res, sky, nina, text, _ = _filter_run(tmp_path, nina, filter="OIII")
    assert res["status"] == "stopped" and "no filter named 'OIII'" in res["reason"]
    assert "Ha 3nm" in res["reason"] and nina.calls == [] and sky.calls == []

    class NoWheel(FakeNina):
        def filter_info(self):
            return {"Connected": False}
    res, sky, nina, text, _ = _filter_run(tmp_path, NoWheel(Clock()))
    assert res["status"] == "stopped" and "not connected" in res["reason"]
    assert nina.calls == []


def test_filter_none_leaves_the_wheel(tmp_path):
    class Untouchable(FakeNina):
        def filter_info(self):
            raise AssertionError("the wheel was read")
    res, sky, nina, text, _ = _filter_run(tmp_path, Untouchable(Clock()), filter="none")
    assert res["status"] == "done" and "left as it is" in text
    res, *_ = _filter_run(tmp_path, Untouchable(Clock()), filter=None)
    assert res["status"] == "done" and res["filter"] is None


# ------------------------------------------------------------- defaults, binning

def test_new_defaults():
    o = tal.Opts()
    assert (o.binning, o.exposure_s, o.filter, o.stop_nina2) == (1, 10.0, "L", False)
    assert (ta.DEFAULT_BINNING, ta.DEFAULT_EXPOSURE_S, ta.DEFAULT_FILTER) == (1, 10.0, "L")
    # still overridable
    o = tal.Opts(binning=2, exposure_s=8.0, filter="R")
    assert (o.binning, o.exposure_s, o.filter) == (2, 8.0, "R")


def test_frame_binning():
    sensor = (6248, 4176)
    assert tal.frame_binning({"NAXIS1": 6248, "NAXIS2": 4176}, sensor) == 1
    assert tal.frame_binning({"NAXIS1": 3124, "NAXIS2": 2088}, sensor) == 2
    # the size wins over a card that says otherwise
    assert tal.frame_binning({"NAXIS1": 6248, "NAXIS2": 4176, "XBINNING": 2}, sensor) == 1
    # no sensor size (or a cropped frame): the XBINNING card, else unknown
    assert tal.frame_binning({"NAXIS1": 6248, "NAXIS2": 4176, "XBINNING": 2}, None) == 2
    assert tal.frame_binning({"NAXIS1": 1000, "NAXIS2": 1000, "XBINNING": 1}, sensor) == 1
    assert tal.frame_binning({"NAXIS1": 1000, "NAXIS2": 1000}, sensor) is None
    assert tal.frame_binning({}, sensor) is None and tal.frame_binning(None, None) is None


class SensorNina(FakeNina):
    def camera_info(self):
        return {**super().camera_info(), "XSize": 6248, "YSize": 4176}


def _import_run(tmp_path, nina=None, sky=None, pts=_PTS, fits=None, **opts_kw):
    clock = Clock()
    files = Files(561)
    sky = sky or ImportSky(files)
    nina = nina or FakeNina(clock)
    out: list[str] = []
    deps = _deps(clock, files, sky, nina, out,
                 fits_header=fits or (lambda p: {}))
    opts_kw.setdefault("csv_path", str(tmp_path / "runs" / "x_tpoint_autocal.csv"))
    opts = _opts(tmp_path, add="import",
                 import_path=str(tmp_path / "imp" / "20261029T030000Z_both.dat"),
                 **opts_kw)
    res = tal.run_loop(_cfg(tmp_path), pts, deps, opts)
    return res, sky, nina, "\n".join(out), clock


def test_frame_shot_at_another_binning_is_solved_at_its_real_scale(tmp_path):
    # 2026-10-28: asked bin 2, TheSky's camera shot 1x1 (full sensor size)
    res, sky, nina, text, _ = _import_run(
        tmp_path, nina=SensorNina(Clock()), binning=2,
        fits=lambda p: {"NAXIS1": 6248, "NAXIS2": 4176, "XBINNING": 2})
    assert res["status"] == "done", res["reason"]
    assert text.count("WARNING: asked for bin 2, but the saved frame is bin 1") == 1
    il = [js for js in sky.scripts if "ImageLink.execute()" in js]
    assert len(il) == 3 and all("ImageLink.scale = 0.23600;" in js for js in il)
    assert all(r["binning_used"] == 1 for r in res["rows"])
    # the same frame asked at bin 1: no warning, same scale
    res, sky, nina, text, _ = _import_run(
        tmp_path, nina=SensorNina(Clock()),
        fits=lambda p: {"NAXIS1": 6248, "NAXIS2": 4176})
    assert "WARNING" not in text and res["rows"][0]["binning_used"] == 1


# ------------------------------------------------------------- NINA #2

class Nina2:
    """NINA #2: RUNNING until stopped, then its end steps run for `end_reads`
    state reads (and park the mount), then IDLE."""

    def __init__(self, mount=None, end_reads=3, stops=True):
        self.mount = mount
        self.end_reads = end_reads
        self.stops = stops
        self.stopped = False
        self.reads = 0
        self.calls: list[str] = []

    def stop_sequence(self):
        self.calls.append("stop")
        self.stopped = True
        if self.mount is not None:
            self.mount.parked = True        # the end steps park the mount

    def sequence_state(self):
        if self.stopped and self.stops:
            self.reads += 1
            if self.reads > self.end_reads:
                return {"State": "IDLE"}
        return {"State": "RUNNING"}


class Nina2Sched(FakeSched):
    def __init__(self, nina2, states=("DISARMED",)):
        super().__init__(states)
        self.nina2 = nina2

    def apps(self):
        a = _apps()
        if not self.nina2.stopped:
            a["sequence_running"][ta.NINA2_LABEL] = ["Piggy light loop"]
        return a


def _nina2_run(tmp_path, n2, stop_nina2=True, unpark=False, states=("DISARMED",),
               mount=None):
    mount = mount or Mount(parked=False, unpark_after=2)
    n2.mount = mount
    clock = Clock()
    files = Files()
    sky = ParkSky(files, mount)
    nina = ParkNina(clock, mount)
    out: list[str] = []
    deps = _deps(clock, files, sky, nina, out, sched=Nina2Sched(n2, states), nina2=n2)
    res = tal.run_loop(_cfg(tmp_path), _PTS, deps,
                       _opts(tmp_path, stop_nina2=stop_nina2, unpark=unpark))
    return res, sky, nina, "\n".join(out), mount


def test_without_stop_nina2_the_fail_line_says_rerun_with_it(tmp_path):
    n2 = Nina2()
    res, sky, nina, text, m = _nina2_run(tmp_path, n2, stop_nina2=False)
    assert res["status"] == "refused"
    assert "re-run with --stop-nina2" in res["reason"]
    line = [ln for ln in text.splitlines() if "NINA #2 (Piggy-600) idle" in ln][0]
    assert "[FAIL" in line and "re-run with --stop-nina2" in line
    assert n2.calls == [] and nina.calls == []
    # the pure preflight says it too (the dry run shows the same line)
    pre = ta.preflight({"state": "DISARMED"}, {**_apps(), "sequence_running": {
        ta.NINA1_LABEL: [], ta.NINA2_LABEL: ["x"]}}, True, {"copies": [1]})
    assert any("re-run with --stop-nina2" in b for b in pre["blockers"])


def test_stop_nina2_stops_waits_and_implies_unpark(tmp_path):
    n2 = Nina2(end_reads=3)
    res, sky, nina, text, m = _nina2_run(tmp_path, n2)
    assert res["status"] == "done", res["reason"]
    assert n2.calls == ["stop"] and n2.reads == 4
    assert res["nina2_stopped"] is True
    # NINA #2's end steps parked the mount; --unpark was implied
    assert "--stop-nina2 implies --unpark" in text
    assert res["unparked"] is True and not m.parked
    assert nina.calls == ["disconnect", "unpark", "connect"]
    assert "[PASS   ] NINA #2 (Piggy-600) idle: was running; stopped by --stop-nina2" in text
    assert "[PASS   ] mount not parked: parked (TheSky): --unpark unparks it" in text


def test_stop_nina2_not_sent_while_other_checks_fail(tmp_path):
    n2 = Nina2()
    res, sky, nina, text, m = _nina2_run(tmp_path, n2, states=("ARMED",))
    assert res["status"] == "refused" and n2.calls == []
    assert "not stopped while other checks fail" in res["reason"]


def test_stop_nina2_that_never_goes_idle_refuses(tmp_path):
    n2 = Nina2(stops=False)
    res, sky, nina, text, m = _nina2_run(tmp_path, n2)
    assert res["status"] == "refused" and n2.calls == ["stop"]
    assert f"still not idle {tal.NINA2_STOP_WAIT_S:.0f} s after the stop" in res["reason"]
    assert nina.calls == []


# ------------------------------------------------------------- parked preflight

def test_parked_mount_is_refused_in_step_1_before_anything_is_sent(tmp_path):
    m = Mount(parked=True)
    clock = Clock()
    files = Files()
    sky = ParkSky(files, m)
    nina = ParkNina(clock, m)
    out: list[str] = []
    res = tal.run_loop(_cfg(tmp_path), _PTS, _deps(clock, files, sky, nina, out),
                       _opts(tmp_path))
    assert res["status"] == "refused" and "re-run with --unpark" in res["reason"]
    assert sky.calls == ["park_read"] and nina.calls == []
    assert "Step 3" not in "\n".join(out)


# ------------------------------------------------------------- residuals

LAT, LON, ELEV = 31.906944, -109.021367, 1300.0
T = datetime(2026, 10, 29, 4, 30, 0)


def _by_hand(alt, az, utc):
    """An independent route: Bennett's refraction (scaled to 870 hPa,
    10 C) off the altitude, alt / az to HA / Dec, RA = local apparent
    sidereal time - HA, then apparent of date -> J2000."""
    import erfa
    r = 1.0 / math.tan(math.radians(alt + 7.31 / (alt + 4.4)))      # arcmin
    r *= (870.0 / 1010.0) * (283.0 / (273.0 + 10.0))
    ha, dec = tm.altaz_to_hadec(alt - r / 60.0, az, LAT)
    u1, u2 = erfa.dtf2d("UTC", utc.year, utc.month, utc.day, utc.hour, utc.minute,
                        utc.second)
    t1, t2 = erfa.taitt(*erfa.utctai(u1, u2))
    last = math.degrees(erfa.gst06a(u1, u2, t1, t2)) / 15.0 + LON / 15.0
    return ti.apparent_to_j2000((last - ha) % 24.0, dec, utc)


@pytest.mark.parametrize("alt,az", [(45.0, 250.0), (60.0, 120.0), (35.0, 300.0),
                                    (75.0, 20.0)])
def test_commanded_target_matches_an_independent_conversion(alt, az):
    ra, dec = tal.commanded_j2000(alt, az, T, LAT, LON, ELEV)
    hra, hdec = _by_hand(alt, az, T)
    r = tal.residual_arcmin(ra, dec, hra, hdec)
    assert r["total"] < 0.2, r          # arcmin (diurnal aberration, refraction model)


def test_apparent_to_j2000_round_trip():
    a = ti.j2000_to_apparent(20.12, 15.08, T)
    assert a[0] != pytest.approx(20.12, abs=1e-4)
    back = ti.apparent_to_j2000(*a, T)
    assert back == pytest.approx((20.12, 15.08), abs=1e-7)


def test_residual_arcmin():
    r = tal.residual_arcmin(10.0, 20.0 + 80 / 60, 10.0, 20.0)
    assert r["dra"] == pytest.approx(0.0) and r["ddec"] == pytest.approx(80.0)
    assert r["total"] == pytest.approx(80.0, abs=0.01)
    # 4' in RA at Dec 60 is 2' on the sky; 3 / 4 / 5
    r = tal.residual_arcmin(10.0 + 8 / 60 / 15, 60.0 - 3 / 60, 10.0, 60.0)
    assert r["dra"] == pytest.approx(4.0, abs=0.01) and r["ddec"] == pytest.approx(-3.0)
    assert r["total"] == pytest.approx(5.0, abs=0.01)
    # across 0 h
    r = tal.residual_arcmin(0.001, 0.0, 23.999, 0.0)
    assert r["dra"] == pytest.approx(0.002 * 15 * 60)


def _shifted(ra_h, dec_d, dra_arcmin, ddec_arcmin):
    dec = dec_d + ddec_arcmin / 60.0
    mid = math.radians((dec + dec_d) / 2.0)
    return (ra_h + dra_arcmin / 60.0 / 15.0 / math.cos(mid)) % 24.0, dec


# the 2026-10-28 pattern (hand-checked): west points missed by 4 to 13'
# (typical 5'), east points all by +76' to +85' in Dec
NIGHT = [  # (side, alt, az, dRA', dDec')
    ("east", 50.0, 110.0, 1.0, 76.0), ("east", 60.0, 140.0, -2.0, 80.0),
    ("east", 40.0, 95.0, 0.5, 85.0), ("west", 45.0, 250.0, 3.0, -4.0),
    ("west", 55.0, 280.0, -4.0, 2.0), ("west", 65.0, 230.0, 12.0, -5.0),
    ("west", 40.0, 300.0, 3.0, 4.0)]


class TrueSky(ImportSky):
    """ImageLink returns the commanded target (as J2000, at the slew's end)
    moved by that point's (dRA', dDec')."""

    def __init__(self, files, clock, offsets):
        super().__init__(files)
        self.clock = clock
        self.offsets = offsets
        self.ends: list = []
        self.alts: list = []

    def run(self, js):
        r = super().run(js)
        if "SlewToAzAlt" in js:
            import re
            m = re.search(r"SlewToAzAlt\(([\d.]+), ([\d.]+)", js)
            self.alts.append((float(m.group(2)), float(m.group(1))))
        if "IsSlewComplete" in js and r.get("slew_complete") == "1":
            self.ends.append(self.clock.now())
        if "ImageLink.execute()" in js:
            i = len(self.ends) - 1
            alt, az = self.alts[i]
            ra, dec = tal.commanded_j2000(alt, az, self.ends[i], LAT, LON, ELEV)
            ra, dec = _shifted(ra, dec, *self.offsets[i])
            r = {**r, "ra_j2000_h": str(ra), "dec_j2000_d": str(dec)}
        return r


def test_residuals_per_point_csv_and_side_summary(tmp_path):
    pts = []
    for side, alt, az, *_ in NIGHT:
        ha, dec = tm.altaz_to_hadec(alt, az, LAT)
        pts.append({"alt": alt, "az": az, "ha_h": ha, "dec_deg": dec, "side": side})
    clock = Clock()
    files = Files(561)
    sky = TrueSky(files, clock, [o[3:] for o in NIGHT])
    out: list[str] = []
    deps = _deps(clock, files, sky, FakeNina(clock), out)
    imp = str(tmp_path / "imp" / "20261029T030000Z_both.dat")
    csv_p = tmp_path / "runs" / "x_tpoint_autocal.csv"
    res = tal.run_loop(_cfg(tmp_path), pts, deps,
                       tal.Opts(backup_root=str(tmp_path / "bk"), import_path=imp,
                                csv_path=str(csv_p)))
    assert res["status"] == "done", res["reason"]
    for row, (side, alt, az, dra, ddec) in zip(res["rows"], NIGHT):
        assert row["res_dra_arcmin"] == pytest.approx(dra, abs=0.05)
        assert row["res_ddec_arcmin"] == pytest.approx(ddec, abs=0.05)
        assert row["res_total_arcmin"] == pytest.approx(math.hypot(dra, ddec), abs=0.1)
    s = res["residuals"]
    assert s["east"]["n"] == 3 and s["west"]["n"] == 4
    assert 76 <= s["east"]["median_ddec"] <= 85 and s["east"]["max_total"] < 86
    assert s["east"]["median_ddec"] == pytest.approx(80.0, abs=0.05)
    assert 4 <= s["west"]["median_total"] <= 6 and s["west"]["max_total"] == pytest.approx(
        13.0, abs=0.05)
    text = "\n".join(out)
    assert "offset from the commanded alt / az: dRA*cos(Dec) +3.0', dDec -4.0', " \
           "total 5.0'" in text
    assert "east: n 3, offset median" in text and "dDec +80.0'" in text
    assert text.isascii() and "\u2014" not in text
    head = csv_p.read_text().splitlines()[0].split(",")
    for col in ("cmd_ra_j2000_h", "cmd_dec_j2000_d", "res_dra_arcmin",
                "res_ddec_arcmin", "res_total_arcmin", "filter", "binning_used"):
        assert col in head
    assert tal.residual_summary([]) == {}


# ------------------------------------------------------------- CLI, allowlist

def _cli(tmp_path, monkeypatch, args):
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
    r = CliRunner().invoke(cli.app, ["tpoint-autocal"] + args)
    return r, seen.get("opts")


def test_cli_defaults_and_new_flags(tmp_path, monkeypatch):
    r, o = _cli(tmp_path, monkeypatch, ["--go", "--unattended", "--points", "6"])
    assert r.exit_code == 0, r.output
    assert (o.binning, o.exposure_s, o.filter, o.stop_nina2, o.unpark) == \
        (1, 10.0, "L", False, False)
    assert "Frames: 10 s, bin 1; NINA #1's filter wheel: L" in r.output
    r, o = _cli(tmp_path, monkeypatch, ["--go", "--unattended", "--points", "6",
                                        "--filter", "none", "--stop-nina2",
                                        "--binning", "2", "--exposure-s", "8"])
    assert r.exit_code == 0, r.output
    assert (o.binning, o.exposure_s, o.filter, o.stop_nina2) == (2, 8.0, None, True)
    assert "--unpark is implied" in r.output
    r, o = _cli(tmp_path, monkeypatch, ["--go", "--unattended", "--points", "6",
                                        "--filter", "Ha"])
    assert o.filter == "Ha"


def test_dry_run_prints_the_filter_step(tmp_path, monkeypatch):
    r, o = _cli(tmp_path, monkeypatch, ["--dry-run", "--no-scripts", "--points", "6",
                                        "--stop-nina2"])
    assert r.exit_code == 0, r.output
    assert o is None                    # nothing ran
    assert "filterwheel/change-filter?filterId=<id>" in r.output
    assert "NINA #1 filter wheel to L" in r.output and "10 s bin 1" in r.output
    assert "/sequence/stop" in r.output and "re-run with --unpark" in r.output
    r, o = _cli(tmp_path, monkeypatch, ["--dry-run", "--no-scripts", "--points", "6",
                                        "--filter", "none"])
    assert "change-filter" not in r.output and "/sequence/stop" not in r.output


def test_new_flags_are_remote_allowed_and_not_write_flags():
    allow = rc.load_allowlist()
    cmd = allow.commands["tpoint-autocal"]
    assert "--filter" in cmd.flags and "--stop-nina2" in cmd.flags
    assert cmd.write_flags == ("--go",)
    assert rc.validate(["tpoint-autocal", "--dry-run", "--filter", "L",
                        "--stop-nina2"], allow).cls == "read"
    assert rc.validate(["tpoint-autocal", "--go", "--unattended", "--stop-nina2",
                        "--filter", "L"], allow).cls == "write"


def test_sources_stay_ascii_without_em_dashes():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for rel in ("photonscript/telescope_agent/tpoint_autocal_live.py",
                "photonscript/telescope_agent/tpoint_autocal.py",
                "photonscript/telescope_agent/tpoint_import.py",
                "docs/TPOINT_AUTOCAL.md", "config/remote_cli.toml"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "\u2014" not in src, rel
